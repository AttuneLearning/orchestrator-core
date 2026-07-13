#!/usr/bin/env bash
# Opt-in installer for the worker-watchdog cron. ASKS before installing (the cron
# hard-restarts a stalled worker once when work is waiting). Stamped per project.
#
#   ./install-watchdog.sh            # ask, then install (every 5 min)
#   ./install-watchdog.sh --yes      # install without prompting
#   ./install-watchdog.sh --status   # is it installed?
#   ./install-watchdog.sh --uninstall
#
# Toggle the relaunch model by editing the crontab line (WATCHDOG_MODEL=...).
set -euo pipefail
WS="__WORKSPACE_ROOT__"
CRON_LINE="*/5 * * * * $WS/qwen-worker-watchdog.sh >> /tmp/worker-watchdog.log 2>&1"
MARKER="qwen-worker-watchdog.sh"

MODE="--install"; YES=0
for a in "$@"; do
  case "$a" in
    --status|--uninstall|--install) MODE="$a" ;;
    --yes|-y) YES=1 ;;
  esac
done

case "$MODE" in
  --status)
    crontab -l 2>/dev/null | grep -F "$MARKER" || echo "worker-watchdog cron: NOT installed"
    exit 0 ;;
  --uninstall)
    crontab -l 2>/dev/null | grep -vF "$MARKER" | crontab - 2>/dev/null || true
    echo "worker-watchdog cron removed"
    exit 0 ;;
esac

# --install
if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
  echo "worker-watchdog cron already installed:"; crontab -l 2>/dev/null | grep -F "$MARKER"
  exit 0
fi
echo "The worker-watchdog cron runs every 5 min and, ONLY when a worker's heartbeat has"
echo "stopped AND implementation work is waiting, hard-restarts that worker ONCE (it kills"
echo "the leaked worker process tree and relaunches it). It will not restart again until the"
echo "worker heartbeats; if a restart doesn't recover it, it alerts a human instead."
if [ "$YES" != 1 ]; then
  read -r -p "Install it now? [y/N]: " ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "skipped — enable later with: $0"; exit 0 ;; esac
fi
( crontab -l 2>/dev/null | grep -vF "$MARKER"; echo "$CRON_LINE" ) | crontab -
echo "installed: $CRON_LINE"
echo "verify decisions any time (no action): $WS/qwen-worker-watchdog.sh --dry-run"
