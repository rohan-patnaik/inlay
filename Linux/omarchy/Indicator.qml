pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons

Item {
  id: root

  property var shell: null
  property var manifest: null
  property string flowState: "idle"
  property real level: 0
  property bool windowAlive: false
  property bool revealed: false
  property int phase: 0

  readonly property bool listening: flowState === "listening"
  readonly property bool processing: flowState === "processing"
  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string runtime: Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/" + Quickshell.env("UID")
  property string preferredOutput: ""
  property double updatedMs: 0
  readonly property var flowPalette: [
    "#77d5ff", "#82ccff", "#8bc3ff", "#97b9ff", "#a5aeff", "#b6a2ff",
    "#a5aeff", "#97b9ff", "#8bc3ff", "#82ccff", "#77d5ff"
  ]

  readonly property string targetScreenName: {
    var screens = Quickshell.screens
    for (var i = 0; i < screens.length; i++)
      if (screens[i].name === root.preferredOutput) return screens[i].name
    for (var j = 0; j < screens.length; j++)
      if (screens[j].name.indexOf("HDMI") === 0 || screens[j].name.indexOf("DP-") === 0)
        return screens[j].name
    return screens.length > 0 ? screens[0].name : ""
  }

  function waveHeight(index) {
    var shape = [0.45, 0.60, 0.78, 0.92, 0.72, 1.0, 0.72, 0.92, 0.78, 0.60, 0.45]
    if (root.processing) {
      var travelling = 0.5 + 0.5 * Math.sin(root.phase * 0.23 - index * 0.85)
      return Style.space(4) + Style.space(10) * travelling
    }
    var audible = Math.max(0, Math.min(1, root.level))
    var movement = 0.86 + 0.14 * Math.sin(root.phase * 0.37 + index * 1.19)
    return Style.space(4) + Style.space(24) * audible * shape[index] * movement
  }

  function present(nextState) {
    idleDelay.stop()
    closeDelay.stop()
    root.windowAlive = true
    root.flowState = nextState
    if (nextState !== "listening") root.level = 0
    Qt.callLater(function() { root.revealed = true })
  }

  function settleIdle() {
    root.flowState = "idle"
    root.level = 0
    root.revealed = false
    closeDelay.restart()
  }

  function applyState(nextState) {
    if (nextState !== "listening" && nextState !== "processing") nextState = "idle"
    var previous = root.flowState
    if (nextState === "idle") {
      if (!root.windowAlive) {
        root.flowState = "idle"
        root.level = 0
        return
      }
      idleDelay.interval = previous === "processing" ? 260 : 100
      idleDelay.restart()
      return
    }
    root.present(nextState)
  }

  function acceptVisual(raw) {
    var event
    try { event = JSON.parse(String(raw || "")) } catch (error) { return }
    root.preferredOutput = String(event.screen || "")
    root.updatedMs = Number(event.updated_ms || 0)
    var age = Date.now() - root.updatedMs
    var state = String(event.phase || "Idle")
    if (age > (state === "Recording" ? 2000 : 45000)) state = "Idle"
    root.applyState(state === "Recording" ? "listening"
      : (["Transcribing", "Cleaning", "Pasting"].indexOf(state) >= 0 ? "processing" : "idle"))
    if (root.listening) root.level = Math.max(0, Math.min(1, Number(event.level) || 0))
  }

  FileView {
    path: root.runtime + "/inlay/visual.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.acceptVisual(text())
    onLoadFailed: root.applyState("idle")
  }

  // Crash/stale-file guard runs only while the bubble is visible.
  Timer {
    interval: 500
    running: root.windowAlive
    repeat: true
    onTriggered: {
      var timeout = root.listening ? 2000 : 45000
      if (Date.now() - root.updatedMs > timeout) root.applyState("idle")
    }
  }

  Timer {
    id: idleDelay
    interval: 180
    onTriggered: root.settleIdle()
  }

  Timer {
    id: closeDelay
    interval: 230
    onTriggered: root.windowAlive = false
  }

  Timer {
    interval: 58
    running: root.windowAlive
    repeat: true
    onTriggered: root.phase = (root.phase + 1) % 10000
  }

  IpcHandler {
    target: "inlay-indicator"
    function state(): string {
      var heights = []
      for (var i = 0; i < 11; i++) heights.push(root.waveHeight(i))
      return JSON.stringify({state: root.flowState, level: root.level,
        visible: root.windowAlive, revealed: root.revealed, screen: root.targetScreenName,
        barHeights: heights})
    }
  }

  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: overlay
      required property var modelData
      screen: modelData
      visible: root.windowAlive && modelData.name === root.targetScreenName
      anchors { top: true; bottom: true; left: true; right: true }
      color: "transparent"
      exclusionMode: ExclusionMode.Ignore
      WlrLayershell.namespace: "rohan-inlay-indicator"
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
      mask: Region {}

      Rectangle {
        id: halo
        width: Style.space(128)
        height: Style.space(48)
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Style.space(61)
        radius: height / 2
        color: "#0b0c10"
        border.width: Math.max(1, Style.space(1))
        border.color: root.processing ? "#675a9d" : "#536d86"
        opacity: root.revealed ? 0.98 : 0
        scale: root.revealed ? 1 : 0.86

        transform: Translate {
          y: root.revealed ? 0 : Style.space(28)
          Behavior on y {
            NumberAnimation { duration: 210; easing.type: Easing.OutCubic }
          }
        }

        Behavior on opacity {
          NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
        }
        Behavior on scale {
          NumberAnimation { duration: 220; easing.type: Easing.OutBack }
        }
        Behavior on border.color {
          ColorAnimation { duration: 160 }
        }

        Row {
          anchors.centerIn: parent
          spacing: Style.space(11)

          Rectangle {
            width: Style.space(7)
            height: width
            radius: width / 2
            anchors.verticalCenter: parent.verticalCenter
            color: root.processing ? "#a99aff" : "#ff5f7a"

            SequentialAnimation on opacity {
              running: root.listening && root.revealed
              loops: Animation.Infinite
              NumberAnimation { to: 0.48; duration: 620; easing.type: Easing.InOutSine }
              NumberAnimation { to: 1.0; duration: 620; easing.type: Easing.InOutSine }
            }
          }

          Row {
            height: Style.space(30)
            spacing: Style.space(3)
            anchors.verticalCenter: parent.verticalCenter

            Repeater {
              model: 11

              Rectangle {
                required property int index
                width: Style.space(3)
                height: root.waveHeight(index)
                anchors.verticalCenter: parent.verticalCenter
                radius: width / 2
                color: root.processing ? "#a99aff" : root.flowPalette[index]
                opacity: 1

                Behavior on height {
                  NumberAnimation { duration: 72; easing.type: Easing.OutCubic }
                }
                Behavior on color {
                  ColorAnimation { duration: 160 }
                }
              }
            }
          }
        }
      }
    }
  }

}
