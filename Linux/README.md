# Headless Inlay on Omarchy Linux

This is a Linux desktop adapter for Ben Davis's **actual Inlay server** and native inference helpers, not a cloud STT replacement. The upstream Swift UI is macOS-only. The adapter captures through PipeWire, calls Inlay API v2 locally, and inserts into the focused Hyprland field.

- Speech: pinned Parakeet-TDT-0.6B-v3 FP16 (1.256 GB).
- Cleanup: pinned Qwen3-4B-Instruct-2507 Q4_K_M (2.497 GB), through Inlay's own prompt, dictionary/list pipeline and rewrite guards.
- RTX 3060 Laptop CUDA acceleration verified. The current upstream Linux coordinator is paired with upstream CUDA helpers from workflow run 37159822540; both passed real local inference. No CUDA toolkit, PyTorch or Python packages are required at runtime.
- Hold Ctrl+Super, speak, release both modifiers. Only one local take is active at a time. Target-window changes fall back to the sensitive clipboard.
- Wispr-style bubble uses the captured PCM, with live recording bars and a processing wave. No extra microphone reader; no keyboard/pointer focus.
- Models warm while you record, stay available for repeated takes, then the entire inference service stops after 60 seconds of inactivity. `inlay warm` prepares it manually; `inlay sleep` unloads immediately. Nothing eagerly loads models at login. Cold takes can wait for loading; two already-warm 11-second public speech operations took 0.94 seconds each. This is not an accuracy benchmark.

## Privacy

Inlay upstream normally keeps inference audio and history. This adapter instead gives the server a private **RAM-only** XDG runtime directory, disables original-audio retention, deletes each generation on completion/cancellation/error, and lets systemd remove the whole runtime archive on idle stop. Audio/text are transient memory/runtime buffers, never written to persistent history. The destination and current clipboard retain inserted text as normal; clipboard output carries the sensitive hint supported by Omarchy. Numeric usage alone persists.

Token and personal vocabulary/preferences are private files under `~/.config/inlay` (0700 directory, 0600 files). No cloud API is used. No credentials, personal words, recordings or transcripts belong in this repo.

## Installation layout

Read upstream `Server/README.md` for model URLs, hashes and Linux CUDA package installation. Place weights and downloaded packages under your configured artifact drive. Keep its `server/helpers/resources` package together; set `package`, `speech_model`, and `text_model` in the private client config. Missing storage refuses startup; there is no alternate-drive/model/cloud fallback.

Install `inlay.py` to `~/.local/lib/inlay/inlay.py` and a `~/.local/bin/inlay` wrapper that executes it with Python 3 (`-B`). Install the three user systemd files in this directory, then `systemctl --user daemon-reload`. They run only on demand; don't enable the model service at boot.

Create a random local `server-token`; create an Inlay `preferences.json` snapshot with `language: en`, `textCorrectionEnabled: true`, `keepOriginalAudio: false`, and your own dictionary. Use the upstream cleanup prompt. Configure a stable `device_id` in the private client config. The local URL is fixed to 127.0.0.1:8391 and sends a bearer token; there are no redirects or remote providers.

Copy `hyprland-omarchy.lua` to `~/.config/hypr/inlay.lua` and load it from your existing bindings. Copy `omarchy/` to the user `rohan.inlay-indicator` plugin and enable it. Remove competing dictation chords before activation. Reload Hyprland and inspect `hyprctl configerrors`.

## Verification and limits

Use `inlay doctor`, real microphone capture/cancel, a real local dictation into a disposable field, and service/GPU readback after the idle timeout. A healthy HTTP endpoint alone is not dictation verification. This fork adds no automated Inlay test suite or fake inference.

The first machine's dictionary migration, local pipeline, bubble, Ctrl+Super capture/cancel, field paste, empty history and idle model/GPU release were checked. Its laptop microphone gain was raised from 35% to 70%; calibrate your own microphone rather than treating that as a universal setting. A different model cannot recover words buried in noise. Real-user accent and distant-mic accuracy remain an empirical trial.

The Omarchy adapter is the tested Linux desktop target. Other desktops need their own hotkey, paste/focus and overlay adapters. Same-window caret movement is not detected. This is a Linux port of the desktop controls; the native Mac UI/features are not claimed to run on Linux.
