#!/usr/bin/env bash
set -euo pipefail

LABEL="com.mbeason.core-signal.morning-brief"
REPO_DIR="/Users/mbeason/core-signal"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3}"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
LOG_DIR="${REPO_DIR}/logs"

usage() {
  cat <<EOF
Usage: $0 [install|uninstall|print-plist|status]

Commands:
  install      Write and load the Core Signal LaunchAgent.
  uninstall    Unload and remove the Core Signal LaunchAgent.
  print-plist  Print the LaunchAgent plist without installing it.
  status       Show launchctl status for the LaunchAgent.
EOF
}

render_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
      <string>${PYTHON_BIN}</string>
      <string>-m</string>
      <string>core_signal.cli</string>
      <string>--reports-dir</string>
      <string>${REPO_DIR}/reports</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
      <key>PYTHONPATH</key>
      <string>${REPO_DIR}/src</string>
    </dict>

    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key>
      <integer>6</integer>
      <key>Minute</key>
      <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchagent.out.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchagent.err.log</string>

    <key>RunAtLoad</key>
    <false/>
  </dict>
</plist>
EOF
}

install_agent() {
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Error: Python executable not found or not executable: ${PYTHON_BIN}" >&2
    echo "Set PYTHON_BIN=/path/to/python3 and retry." >&2
    exit 2
  fi

  mkdir -p "${PLIST_DIR}" "${LOG_DIR}" "${REPO_DIR}/reports"
  render_plist > "${PLIST_PATH}"
  plutil -lint "${PLIST_PATH}"

  launchctl bootout "gui/${UID}" "${PLIST_PATH}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/${UID}" "${PLIST_PATH}"
  launchctl enable "gui/${UID}/${LABEL}"

  echo "Installed ${PLIST_PATH}"
  echo "Scheduled Core Signal daily at 6:00 AM."
}

uninstall_agent() {
  launchctl bootout "gui/${UID}" "${PLIST_PATH}" >/dev/null 2>&1 || true
  rm -f "${PLIST_PATH}"
  echo "Removed ${PLIST_PATH}"
}

status_agent() {
  launchctl print "gui/${UID}/${LABEL}"
}

command="${1:-install}"
case "${command}" in
  install)
    install_agent
    ;;
  uninstall|remove)
    uninstall_agent
    ;;
  print-plist)
    render_plist
    ;;
  status)
    status_agent
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

