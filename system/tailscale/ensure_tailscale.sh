#!/usr/bin/env bash
# Bring up Tailscale on the device so it stays reachable when it's not on our LAN
# (phone hotspot, cellular, CGNAT). Runs at every boot from launch_chffrplus.sh
# (agnos_init) and is idempotent — safe to re-run by hand at any time.
#
# What lives where (all under /data, which survives reinstalls and branch switches;
# only a full device reset wipes it):
#   /data/tailscale/tailscale, tailscaled  binaries, downloaded on first run (pinned below)
#   /data/tailscale/state/                 node identity + keys. Created by the one-time
#                                          login. This is the credential — NEVER in git.
#   /data/tailscale/authkey                optional: a pre-auth key (tskey-auth-...) for a
#                                          hands-off login on a freshly reset device.
#                                          Consumed on first successful login, then deleted.
#   /data/tailscale/login_url              written when a browser login is needed and no
#                                          authkey was provided. Open it, done.
#   /data/tailscale/ensure.log             this script's log
#
# tailscaled runs as a transient systemd unit (systemd-run) in its own cgroup, outside
# comma.service, so restarting openpilot (rr.sh, `systemctl restart comma`, killing the
# tmux session) does not drop the tunnel. Nothing is written to the read-only AGNOS
# root, so an AGNOS update can't remove it. Layout matches The Pond's Tailscale
# installer; if that has installed a persistent unit, this script just uses it.

set -uo pipefail

TS_VERSION="1.102.4"
TS_SHA256="9dd1e6a592a014bbaea0103167ffe299adeda4ba14e078ce9c2895364f6c4c3f"
TS_ARCH="arm64"
TS_HOSTNAME="comma3"

BASE="/data/tailscale"
STATE="$BASE/state"
SOCK="$BASE/tailscaled.sock"
LOG="$BASE/ensure.log"
UNIT="tailscaled.service"

# Flags for `tailscale up`. --reset makes them the whole config every time (no "flags
# changed" errors). DNS is left alone: the device has no reason to resolve tailnet names
# and AGNOS's /etc is read-only anyway. Netfilter is off because AGNOS ships nf_tables
# iptables on a 4.9 kernel that doesn't support it; a plain node needs no firewall rules.
UP_FLAGS=(--reset --hostname="$TS_HOSTNAME" --accept-dns=false --netfilter-mode=off)

mkdir -p "$STATE"
chmod 700 "$STATE"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# keep the log from growing forever
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 200000 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# one instance at a time
exec 9>"$BASE/.lock"
if ! flock -n 9; then
  log "another instance is running; exiting"
  exit 0
fi

ts() { sudo -n "$BASE/tailscale" --socket="$SOCK" "$@"; }

install_binaries() {
  if [ -x "$BASE/tailscaled" ] && [ -x "$BASE/tailscale" ] && [ "$(cat "$BASE/VERSION" 2>/dev/null)" = "$TS_VERSION" ]; then
    return 0
  fi
  local tgz="$BASE/tailscale.tgz"
  local url="https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_${TS_ARCH}.tgz"
  log "downloading tailscale $TS_VERSION ($url)"
  if ! curl -fsSL --retry 5 --retry-delay 10 --connect-timeout 30 -o "$tgz" "$url"; then
    log "download failed"
    return 1
  fi
  if ! echo "$TS_SHA256  $tgz" | sha256sum -c --quiet; then
    log "sha256 mismatch on $tgz; refusing to install"
    rm -f "$tgz"
    return 1
  fi
  local dir="$BASE/tailscale_${TS_VERSION}_${TS_ARCH}"
  rm -rf "$dir"
  tar -xzf "$tgz" -C "$BASE" || { log "extract failed"; return 1; }
  install -m 755 "$dir/tailscaled" "$BASE/tailscaled.new" && mv -f "$BASE/tailscaled.new" "$BASE/tailscaled"
  install -m 755 "$dir/tailscale" "$BASE/tailscale.new" && mv -f "$BASE/tailscale.new" "$BASE/tailscale"
  echo "$TS_VERSION" > "$BASE/VERSION"
  rm -rf "$dir" "$tgz"
  log "installed tailscale $TS_VERSION"
  # a running daemon is the old version now; restart it below
  sudo -n systemctl stop "$UNIT" 2>/dev/null || true
}

ensure_daemon() {
  if sudo -n systemctl is-active --quiet "$UNIT"; then
    return 0
  fi
  if [ -f "/etc/systemd/system/$UNIT" ]; then
    # The Pond installed a persistent unit; use it rather than fight it.
    log "starting The Pond's persistent $UNIT"
    sudo -n systemctl start "$UNIT"
    return $?
  fi
  sudo -n systemctl reset-failed "$UNIT" 2>/dev/null || true
  log "starting tailscaled as a transient unit"
  sudo -n systemd-run --unit="${UNIT%.service}" --description="Tailscale node agent (openpilot)" \
    --collect -p Restart=on-failure -p RestartSec=5 \
    "$BASE/tailscaled" --state="$STATE/tailscaled.state" --statedir="$STATE" --socket="$SOCK" \
    >> "$LOG" 2>&1
}

backend_state() {
  ts status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState",""))' 2>/dev/null
}

ensure_login() {
  local state="" i
  for i in $(seq 1 30); do
    state="$(backend_state)"
    [ -n "$state" ] && break
    sleep 1
  done
  log "backend state: ${state:-unknown}"

  case "$state" in
    Running)
      rm -f "$BASE/login_url"
      log "up as $(ts ip -4 2>/dev/null) ($TS_HOSTNAME)"
      return 0
      ;;
    Stopped)
      # previously `tailscale down`; identity is still there
      ts up "${UP_FLAGS[@]}" >> "$LOG" 2>&1 && log "reconnected as $(ts ip -4 2>/dev/null)"
      return $?
      ;;
    NeedsLogin|NoState|"")
      ;;
    *)
      log "not touching login in state '$state'"
      return 0
      ;;
  esac

  if [ -s "$BASE/authkey" ]; then
    chmod 600 "$BASE/authkey"
    log "logging in with pre-auth key"
    if ts up "${UP_FLAGS[@]}" --auth-key="file:$BASE/authkey" --timeout=2m >> "$LOG" 2>&1; then
      rm -f "$BASE/authkey" "$BASE/login_url"
      log "up as $(ts ip -4 2>/dev/null) ($TS_HOSTNAME)"
      return 0
    fi
    log "pre-auth key login failed; leaving key in place"
  fi

  # No key: ask for a browser login. `tailscale up` blocks until it's approved (or 15 min),
  # so run it detached and just capture the URL. Get it via the LAN or The Pond, open it once,
  # and the identity is saved to $STATE for good.
  log "no authkey; requesting browser login (URL -> $BASE/login_url)"
  (
    ts up "${UP_FLAGS[@]}" --timeout=15m 2>&1 | while IFS= read -r line; do
      log "up: $line"
      case "$line" in
        *https://login.tailscale.com/*)
          printf '%s\n' "$line" | grep -oE 'https://login\.tailscale\.com/[^[:space:]]+' > "$BASE/login_url"
          chmod 600 "$BASE/login_url"
          ;;
      esac
    done
    [ "$(backend_state)" = "Running" ] && { rm -f "$BASE/login_url"; log "login complete, up as $(ts ip -4 2>/dev/null)"; }
  ) >/dev/null 2>&1 &
  disown
}

install_binaries || exit 1
ensure_daemon || exit 1
ensure_login
