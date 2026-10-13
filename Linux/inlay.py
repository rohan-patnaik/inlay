#!/usr/bin/env python3
"""Headless Linux desktop adapter for the actual local Inlay server."""
import argparse
import array
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import http.client
import threading
import queue
import uuid

HOME = Path.home()
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', HOME / '.config')) / 'inlay'
STATE = Path(os.environ.get('XDG_STATE_HOME', HOME / '.local/state')) / 'inlay'
RUNTIME = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))) / 'inlay'
VISUAL = {'phase': 'Idle', 'level': 0.0, 'screen': ''}
DEFAULTS = {'max_seconds': 180, 'aliases': {}, 'preferred_words': [], 'snippets': {},
            'paste_command': None, 'idle_seconds': 60,
            'microphone_target': None, 'monitor_capture': False}
SERVER = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))) / 'inlay-server'



def read(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write('\n')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def config():
    return DEFAULTS | read(CONFIG / 'config.json', {})


def visual(phase=None, level=None, screen=None):
    """Disposable, numeric-only UI IPC in the user's runtime (RAM) directory."""
    if phase is not None:
        VISUAL['phase'] = phase
        if phase != 'Recording':
            VISUAL['level'] = 0.0
    if level is not None:
        VISUAL['level'] = max(0.0, min(1.0, level))
    if screen is not None:
        VISUAL['screen'] = screen
    atomic(RUNTIME / 'visual.json', VISUAL | {'updated_ms': round(time.time() * 1000)})


def microphone_level(pcm):
    """Same perceptual dBFS scale as the existing Wispr-style microphone pill."""
    samples = array.array('f', pcm[:len(pcm) // 4 * 4])
    if sys.byteorder != 'little':
        samples.byteswap()
    if not samples:
        return 0.0
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    if rms < 0.0025:
        return 0.0
    linear = max(0.0, min(1.0, (20 * math.log10(rms) + 46) / 34))
    return linear ** 0.82


def notify(message):
    if shutil.which('notify-send'):
        subprocess.run(['notify-send', '-a', 'inlay', '-t', '3500', 'inlay', message],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def live(meta):
    try:
        cmd = Path('/proc/' + str(meta['pid']) + '/cmdline').read_bytes().split(b'\0')
        return meta['session'].encode() in cmd and b'_worker' in cmd
    except (OSError, KeyError):
        return False


def publish(data):
    today = data.get('days', {}).get(dt.datetime.now(dt.timezone.utc).date().isoformat(), {})
    s = {k: today.get(k, 0) for k in ('words', 'dictations', 'audio_seconds', 'tokens', 'asr_requests', 'cleanup_requests')}
    s.update(phase=data.get('phase', 'Idle'), last_latency_ms=data.get('last_latency_ms', 0),
             error=data.get('error', ''), updated_at=data.get('updated_at', ''),
             rate_headers=data.get('rate_headers', {}))
    record = {'id': 'inlay', 'name': 'inlay', 'ready': (CONFIG / 'server-token').exists(),
              'tierLabel': 'Local Parakeet + Qwen', 'unit': 'words', 'inlay': s,
              'usageStatusText': s['error'], 'authHelpText': s['error']}
    atomic(STATE.parent / 'omarchy/agents/usage/inlay.json', record)
    return record


def update(phase=None, error=None, counters=None, latency=None, headers=None):
    if phase is not None:
        visual(phase)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(STATE / 'usage.json', {'days': {}})
        if phase is not None:
            data['phase'] = phase
        if error is not None:
            data['error'] = error
        if latency is not None:
            data['last_latency_ms'] = round(latency * 1000)
        if headers:
            data.setdefault('rate_headers', {}).update(headers)
        day = dt.datetime.now(dt.timezone.utc).date().isoformat()
        if counters:
            row = data.setdefault('days', {}).setdefault(day, {})
            for key, value in counters.items():
                row[key] = row.get(key, 0) + value
        data['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        atomic(STATE / 'usage.json', data)
        return publish(data)


def substitutions(text, mapping):
    """Longest whole phrase wins, once, including canonical output phrases."""
    lookup = {k.casefold(): v for k, v in mapping.items() if k}
    for value in mapping.values():
        lookup.setdefault(value.casefold(), value)
    if not lookup:
        return text
    pattern = r'(?<!\w)(?:' + '|'.join(re.escape(k) for k in sorted(lookup, key=len, reverse=True)) + r')(?!\w)'
    return re.sub(pattern, lambda m: lookup[m[0].casefold()], text, flags=re.I)


def basic_cleanup(text):
    text = re.sub(r'[ \t]+', ' ', text).strip()
    return re.sub(r' +([,.;!?])', r'\1', text)


def api(method, path, body=None, binary=False):
    token = (CONFIG / 'server-token').read_text().strip()
    connection = http.client.HTTPConnection('127.0.0.1', 8391, timeout=15)
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/octet-stream' if binary else 'application/json'}
    data = body if binary else json.dumps(body).encode() if body is not None else None
    try:
        connection.request(method, '/v1/' + path, data, headers)
        response = connection.getresponse()
        data = response.read()
        if response.status >= 400:
            raise RuntimeError('Inlay local server rejected the request (HTTP ' + str(response.status) + ')')
        return json.loads(data) if data else None
    finally:
        connection.close()


def ensure_server(result):
    try:
        subprocess.run(['systemctl', '--user', 'stop', 'inlay-idle.timer'], check=True)
        subprocess.run(['systemctl', '--user', 'start', 'inlay-server.service'], check=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                health = api('GET', 'health')
                if health.get('apiVersion') != 2:
                    raise RuntimeError('Inlay client/server API versions differ')
                if health.get('ready'):
                    result.put(None)
                    return
            except (ConnectionError, OSError):
                pass
            time.sleep(0.1)
        raise RuntimeError('Inlay models did not become available in time')
    except Exception as error:
        result.put(error)


def finish_dictation(pcm, session, cfg, prepared):
    error = prepared.get(timeout=125)
    if error:
        raise error
    record = api('POST', 'generations', {'requestID': session, 'device': {'id': cfg['device_id'], 'name': 'Linux desktop'}, 'mode': 'dictation'})
    generation = record['id']
    try:
        for sequence, offset in enumerate(range(0, len(pcm), 1048576)):
            api('POST', 'generations/' + generation + '/audio/inference?sequence=' + str(sequence) + '&sampleRate=16000&channels=1', bytes(pcm[offset:offset + 1048576]), True)
        api('POST', 'generations/' + generation + '/finish', {'inferenceFrames': len(pcm) // 4})
        phase = 'Transcribing'
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if (RUNTIME / ('cancel-' + session)).exists():
                return ''
            record = api('GET', 'generations/' + generation)
            if record['status'] == 'completed':
                return substitutions(substitutions(record['insertionText'], cfg['aliases']), cfg['snippets'])
            if record['status'] in ('failed', 'cancelled'):
                raise RuntimeError('Inlay could not complete dictation; no partial text inserted')
            next_phase = 'Cleaning' if record['status'] == 'proofreading' else 'Transcribing'
            if next_phase != phase:
                phase = next_phase
                update(phase)
            time.sleep(.08)
        raise RuntimeError('Inlay processing timed out')
    finally:
        try:
            api('POST', 'generations/' + generation + '/cancel')
            api('DELETE', 'generations/' + generation)
        except (OSError, RuntimeError):
            # Refuse to leave any failed cleanup archive resident.
            subprocess.run(['systemctl', '--user', 'stop', 'inlay-server.service'])


def active_window():
    if not shutil.which('hyprctl'):
        return {}
    result = subprocess.run(['hyprctl', '-j', 'activewindow'], capture_output=True, text=True, timeout=3)
    return json.loads(result.stdout) if result.returncode == 0 else {}


def copy_text(text):
    data = text.encode()
    owner = subprocess.Popen(['wl-copy', '--foreground', '--sensitive', '--type', 'text/plain;charset=utf-8'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    owner.stdin.write(data)
    owner.stdin.close()
    # Confirm the new selection before issuing paste; the clipboard offer is asynchronous.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        current = subprocess.run(['wl-paste', '--no-newline'], capture_output=True, timeout=1)
        if current.returncode == 0 and current.stdout == data:
            return
        if owner.poll() is not None:
            break
        time.sleep(.01)
    owner.terminate()
    raise RuntimeError('Clipboard selection could not be confirmed; nothing pasted')


def paste(text, target, cfg):
    copy_text(text)
    current = active_window()
    if not target.get('address') or current.get('address') != target.get('address'):
        notify('Text is on the clipboard. Focus changed; paste it when ready.')
        return 'Clipboard'
    if cfg['paste_command']:
        subprocess.run(cfg['paste_command'], check=True, timeout=3)
    else:
        terminal = any(word in str(target.get('class', '')).lower()
                       for word in ('ghostty', 'kitty', 'alacritty', 'foot', 'terminal'))
        mods, key = ('SHIFT', 'Insert') if terminal else ('CTRL', 'v')
        # Omarchy's Lua compositor: send explicit down/up with a one-shot release.
        lua = ('hl.dispatch(hl.dsp.send_key_state({mods=' + json.dumps(mods) + ',key=' + json.dumps(key) + ',state="down"}));'
               'hl.timer(function() hl.dispatch(hl.dsp.send_key_state({mods=' + json.dumps(mods) + ',key=' + json.dumps(key) +
               ',state="up"})) end,{timeout=50,type="oneshot"})')
        result = subprocess.run(['hyprctl', 'eval', lua], capture_output=True, timeout=3)
        if result.returncode:
            raise RuntimeError('Paste adapter unavailable; text remains on clipboard')
    return 'Idle'


def worker(session):
    meta = read(RUNTIME / 'worker.json', {})
    if meta.get('session') != session:
        return
    recorder = None
    try:
        cfg = config()
        prepared = queue.Queue()
        threading.Thread(target=ensure_server, args=(prepared,), daemon=True).start()
        visual(screen=meta.get('screen', ''))
        update('Recording', error='')
        capture = ['pw-record', '--rate=16000', '--channels=1', '--format=f32', '--raw']
        if cfg['microphone_target']:
            capture += ['--target', str(cfg['microphone_target'])]
        if cfg['monitor_capture']:
            capture += ['-P', '{"stream.capture.sink":true}']
        recorder = subprocess.Popen(capture + ['-'],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(recorder.stdout.fileno(), False)
        pcm = bytearray()
        started = time.monotonic()
        last_meter = started
        meter_pcm = bytearray()
        level = 0.0
        stop_path = RUNTIME / ('stop-' + session)
        cancel_path = RUNTIME / ('cancel-' + session)
        while time.monotonic() - started < cfg['max_seconds']:
            if stop_path.exists() or cancel_path.exists():
                break
            if recorder.poll() is not None:
                raise RuntimeError('Microphone capture failed')
            if select.select([recorder.stdout], [], [], 0.04)[0]:
                chunk = os.read(recorder.stdout.fileno(), 64000)
                pcm.extend(chunk)
                meter_pcm.extend(chunk)
                if time.monotonic() - last_meter >= 0.08:
                    target_level = microphone_level(meter_pcm)
                    weight = 0.68 if target_level > level else 0.24
                    level += (target_level - level) * weight
                    visual(level=round(level, 3))
                    meter_pcm.clear()
                    last_meter = time.monotonic()
        released = time.monotonic()
        recorder.send_signal(signal.SIGINT)
        tail, _ = recorder.communicate(timeout=3)
        pcm.extend(tail)
        if cancel_path.exists():
            update('Idle', error='')
            return
        seconds = len(pcm) / 64000
        if seconds < .25:
            update('Idle', error='')
            return
        update('Transcribing', counters={'audio_seconds': seconds})
        result = finish_dictation(pcm, session, cfg, prepared)
        if not result:
            update('Idle', error='')
            return
        if cancel_path.exists():
            update('Idle', error='')
            return
        if meta.get('keyboard'):
            deadline = time.monotonic() + 15
            while not (RUNTIME / ('released-' + session)).exists():
                if time.monotonic() > deadline:
                    copy_text(result)
                    notify('Modifiers are still held. Text copied; paste when ready.')
                    update('Clipboard', error='', counters={'words': len(result.split()), 'dictations': 1},
                           latency=time.monotonic() - released)
                    return
                time.sleep(0.04)
        if cancel_path.exists():
            update('Idle', error='')
            return
        update('Pasting')
        phase = paste(result, meta.get('target', {}), cfg)
        update(phase, error='', counters={'words': len(result.split()), 'dictations': 1}, latency=time.monotonic() - released)
    except Exception as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else 'Local operation failed (' + type(exc).__name__ + ')'
        update('Error', error=message)
        notify(message)
    finally:
        visual('Idle')
        subprocess.run(['systemctl', '--user', 'restart', 'inlay-idle.timer'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if recorder and recorder.poll() is None:
            recorder.terminate()
            recorder.wait(timeout=3)
        for prefix in ('stop-', 'cancel-', 'released-'):
            (RUNTIME / (prefix + session)).unlink(missing_ok=True)
        if read(RUNTIME / 'worker.json', {}).get('session') == session:
            (RUNTIME / 'worker.json').unlink(missing_ok=True)


def control(action, keyboard=False):
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUNTIME / 'control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        meta = read(RUNTIME / 'worker.json', {})
        if action == 'start':
            if live(meta):
                return
            if not (CONFIG / 'server-token').exists():
                notify('Configure the local Inlay server first.')
                return
            session = str(uuid.uuid4())
            target = active_window()
            screen = ''
            if target and shutil.which('hyprctl'):
                result = subprocess.run(['hyprctl', '-j', 'monitors'], capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    screen = next((m['name'] for m in json.loads(result.stdout)
                                   if m['id'] == target.get('monitor')), '')
            child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '_worker', session],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
            atomic(RUNTIME / 'worker.json', {'pid': child.pid, 'session': session,
                                            'target': target, 'keyboard': keyboard, 'screen': screen})
        elif live(meta):
            prefix = {'stop': 'stop-', 'cancel': 'cancel-', 'release': 'released-'}[action]
            (RUNTIME / (prefix + meta['session'])).touch(mode=0o600)


def server_start():
    cfg = config()
    if not os.path.ismount(cfg['artifact_mount']):
        raise RuntimeError('Inlay artifact drive is unavailable; refusing another drive')
    SERVER.mkdir(parents=True, exist_ok=True, mode=0o700)
    # This runtime directory is tmpfs; no recording/history is persisted to disk.
    shutil.copyfile(CONFIG / 'preferences.json', SERVER / 'preferences.json')
    (SERVER / 'preferences.json').chmod(0o600)
    package = Path(cfg['package'])
    args = [str(package / 'inlay-server'), '--host', '127.0.0.1', '--port', '8391',
            '--data-dir', str(SERVER), '--token-file', str(CONFIG / 'server-token'),
            '--speech-helper', str(package / 'helpers/inlay-engine'),
            '--speech-model', cfg['speech_model'], '--vad-model', str(package / 'resources/silero-vad.bin'),
            '--proof-helper', str(package / 'helpers/inlay-text-engine'), '--proof-model', cfg['text_model']]
    os.execv(args[0], args)


def idle_stop():
    if not live(read(RUNTIME / 'worker.json', {})):
        subprocess.run(['systemctl', '--user', 'stop', 'inlay-server.service'], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'stop', 'release', 'cancel', 'stats', 'doctor', 'server-start', 'idle-stop', 'warm', 'sleep', '_worker'])
    parser.add_argument('session', nargs='?')
    parser.add_argument('--keyboard', action='store_true')
    args = parser.parse_args()
    if args.command == '_worker':
        # Wait for the launcher to atomically publish this worker's identity.
        for _ in range(100):
            if read(RUNTIME / 'worker.json', {}).get('session') == args.session:
                break
            time.sleep(0.01)
        worker(args.session)
    elif args.command == 'server-start':
        server_start()
    elif args.command == 'idle-stop':
        idle_stop()
    elif args.command == 'warm':
        result = queue.Queue()
        ensure_server(result)
        error = result.get()
        if error: raise error
        subprocess.run(['systemctl', '--user', 'restart', 'inlay-idle.timer'], check=True)
    elif args.command == 'sleep':
        subprocess.run(['systemctl', '--user', 'stop', 'inlay-server.service', 'inlay-idle.timer'], check=True)
    elif args.command == 'stats':
        data = read(STATE / 'usage.json', {'days': {}})
        if not live(read(RUNTIME / 'worker.json', {})) and data.get('phase') in ('Recording', 'Transcribing', 'Cleaning', 'Pasting'):
            data['phase'] = 'Idle'
        print(json.dumps(publish(data), indent=2))
    elif args.command == 'doctor':
        print(json.dumps({'local_token_configured': (CONFIG / 'server-token').exists(),
                          'tools': {x: bool(shutil.which(x)) for x in ('pw-record', 'wl-copy', 'hyprctl')},
                          'models': {'speech': 'Parakeet-TDT-0.6B-v3 FP16', 'cleanup': 'Qwen3-4B-Instruct-2507 Q4_K_M'},
                          'worker_running': live(read(RUNTIME / 'worker.json', {}))}, indent=2))
    else:
        control(args.command, args.keyboard)


if __name__ == '__main__':
    os.umask(0o077)
    main()
