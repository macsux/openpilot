# Project context for Claude Code

## What this repo is
A working tree of **StarPilot** (firestar5683/StarPilot, branch `StarPilot`) with personal customizations on branch `macsux-tweaks`. StarPilot is a GM-focused FrogPilot variant that runs on the original comma 3 (codename `tici`) — not just comma 3X. Built on openpilot 0.10.3.

## Hardware target
- Device: comma **3** (NOT 3X). Codename `tici`. Older hardware revision (the one twilsonco's fork supported, predating the C3X-era fan/screen change).
- Vehicle: 2017 Chevrolet Volt Premier with factory ACC (radar). L&P (Lee & Paulding) GM Volt harness — square.site listing 20.
- Expected fingerprint: `CHEVROLET_VOLT` (the 2017-18 ASCM platform config in `opendbc_repo/opendbc/car/gm/values.py`).

## Constitutional safety rule
Never install or push code that targets only `tizi` (comma 3X) or comma 4 AGNOS onto the device. The recovery path is `flash.comma.ai` (Android Chrome if desktop fails). Read `../openpilot/research/01-comma3-os-compatibility.md` before any AGNOS-touching change.

## Branches
- `StarPilot` — upstream, do not commit here directly.
- `macsux-tweaks` — our personal customizations branch. Default working branch.
- Future feature branches: `andrew-feature-XYZ` off `macsux-tweaks`.

## Customizations live on macsux-tweaks
1. **R1: stop disengaging on door open / seatbelt unlatched.** `selfdrive/selfdrived/events.py` — removed `ET.SOFT_DISABLE` for `EventName.doorOpen` and `EventName.seatbeltNotLatched`. NO_ENTRY still blocks engaging from those states; mid-drive the system stays engaged.
2. **R1b: kill the "needs internet / hasn't been updated" offroad prompts.** `system/updated/updated.py` — removed the conditional re-set of `Offroad_UpdateFailed`/`Offroad_ConnectivityNeeded`/`Offroad_ConnectivityNeededPrompt`; the unconditional clear earlier in `set_params()` keeps the alerts hidden permanently.
3. **Gap coast** (`selfdrive/controls/lib/lead_behavior.py: compute_gap_coast`, wired in `starpilot/controls/lib/starpilot_following.py`). Inside the target gap but not closing (cut-in pulling away, crept a bit close) → pin the MPC's target gap just under the real gap (publish a shorter `tFollow`) and inhibit throttle, i.e. lift off and let the gap regrow; `tFollow` ramps back at 0.3 s/s after a coast. Rapid closing / TTC < 6 s / lead braking / gap under 0.9 s + 6 m → normal MPC braking immediately. Ported 2026-09-24 from the old-base `macsux-dev` line. Two things differ on this base: the planner's `get_dynamic_t_follow` ramps a shorter `tFollow` down at 0.6 s/s (an upstream contract, see `test_personality_transient_contract.py`), so the coast takes up to ~1 s to fully land; and the old line's bounded-jerk output limiter was deliberately NOT ported (this planner has its own follow-policy smoothing plus ~30 output caps/floors that a blanket rate limiter would fight). Tests: `test_lead_behavior.py`, `starpilot/controls/tests/test_gap_coast_following.py`.
4. **Tailscale remote access.** `system/tailscale/ensure_tailscale.sh`, hooked from `agnos_init` in `launch_chffrplus.sh`. Every boot: downloads a pinned Tailscale build into `/data/tailscale/` if missing, starts `tailscaled` as a transient systemd unit (own cgroup — survives openpilot restarts; nothing written to the read-only AGNOS root), and logs in. **Credentials never live in the repo:** the node identity is `/data/tailscale/state/` on the device, created by a one-time browser login (URL lands in `/data/tailscale/login_url`). For a hands-off login on a freshly reset device, drop a pre-auth key in `/data/tailscale/authkey` — it's consumed and deleted. Node name `comma3`, IP `100.72.49.78`. Netfilter is off (AGNOS's nf_tables iptables doesn't work on the 4.9 kernel); `--accept-dns=false`.
5. **Home Assistant push over MQTT** (`starpilot/system/ha_pushd.py` + the dependency-free `starpilot/system/mqtt_min.py`; registered as `ha_pushd` in `system/manager/process_config.py`: always-run, nice 19). One persistent MQTT session over Tailscale to the Mosquitto broker on the Mac (the one HA's MQTT integration uses); HA discovery creates a single "Car" device with `device_tracker.car`, `sensor.car_speed`, `sensor.car_drive_state`, `binary_sensor.car_ignition`, `binary_sensor.car_device_online` (HA derives entity ids from device name + entity name). Topics `car/state`, `car/location`, `car/availability` (all retained; `offline` is the broker-sent last will). Immediate push on drive↔park / ignition edges, else every 15 s onroad and 10 min offroad; a ~100-byte PUBLISH per update instead of two HTTPS POSTs (this replaced the mobile_app webhook version on 2026-09-24 to save cellular data). Reconnects with 2→60 s backoff, one log line per streak; monotonic clock only. Tests: `starpilot/system/tests/test_ha_pushd.py` (fake broker). **Config `/data/ha_push.json` is never committed (public repo, it holds the broker password):** `{"host": "<Mac tailscale IP>", "port": 1883, "username": "ha", "password": "<mqtt_password>"}`. It lives outside `/data/openpilot`, so updates and branch switches don't touch it; only a factory reset / `/data` wipe does. Recovery from the Mac is one command (the daemon re-reads the file within 60 s, no restart):
   ```
   python3 -c "import json,subprocess,re; pw=re.search(r'^mqtt_password:\s*(\S+)', open('/Users/andrew/projects/macsux/home-assistant/config/secrets.yaml').read(), re.M).group(1).strip('\"\\''); ip=subprocess.check_output(['tailscale','ip','-4']).decode().split()[0]; print(json.dumps({'host': ip, 'port': 1883, 'username': 'ha', 'password': pw}))" | ssh comma 'umask 077; cat > /data/ha_push.json'
   ```
   The broker is `docker` container `mosquitto` (config in `~/projects/macsux/home-assistant/mosquitto/config`, `allow_anonymous false`, user `ha`), published on all Mac interfaces including Tailscale (`100.90.75.123`). The old mobile_app "Car" registration was deleted on 2026-09-24 21:50 and the MQTT discovery + a retained parked/off seed were published from the Mac in the same minute, so the entity ids carried over with no `_2` suffix (this is how to re-seed after any HA reset: delete first, then publish discovery). Seeding/probing from the Mac works with the same client: `MqttClient('100.90.75.123', 1883, 'ha', <pw>, 'comma3-car-seed')`.

## What's NOT a code change — toggles in StarPilot UI to flip after install
- **Always On Lateral** (Settings → Lateral → Always On Lateral) — covers the steering-wheel-touch nag.
- **Volt SNG Hack** (Settings → Vehicle → GM Settings → Volt SNG Hack) — auto-resume from stop. Only visible because our 2017 Volt has no factory SNG.
- **Personality profiles** (Settings → Longitudinal → Personalities) — follow distance tuning.
- Driver-distraction events `driverDistracted` and `driverUnresponsive` only carry `ET.PERMANENT` in StarPilot — they show alerts but don't disengage. No patch needed.
- **Car: CHEVROLET_VOLT with Force Fingerprint ON.** The live CAN fingerprint is ambiguous for this Volt and StarPilot then reuses the stored `CarModel`; it had drifted to `CADILLAC_ESCALADE_ESV`, which (not being in `kaofui_cars`) fed the radar its speed at 5 Hz instead of 50 → zero radar tracks → vision-only leads → rubber-banding. If following ever gets jerky again, check `CarParams.carFingerprint` first.

## Iteration workflow
See `../openpilot/research/04-iteration-workflow.md`. Key points:
- `ssh comma` goes over Tailscale (works from anywhere the Mac has Tailscale up); `ssh comma-lan` is the phone-hotspot path (subnet rotates, `10.x.y.188`).
- Push code via `rsync` from this dir to `comma:/data/openpilot/`.
- Restart with `ssh comma 'sudo systemctl restart comma'` — restarts the tmux session + manager + children. (There is no `rr.sh` on the device; `pkill controlsd` no longer works either.) Python edits need this full restart — manager re-forks children from its own preloaded modules, so killing a single process brings back the OLD code.
- **A "System reset triggered — erase all content and settings" screen after restarting openpilot is NOT a real reset request.** AGNOS's `comma.sh` re-runs its "5+ taps at boot → factory reset" check on every `comma.service` start once `/tmp/booted` is gone, and tmpfiles cleanup deletes that marker 15 min after boot (bogus boot clock makes it look months old). Tap **Cancel**; never Confirm. `agnos_init` now excludes the marker from cleanup, so this only recurs on a build without that fix.
- **Don't set "Enable Tethering" to "Only Onroad" or "Always"** (Settings → Network). StarPilot flips wlan0 into AP mode (`weedle-xxxx`) at ignition-on, which drops it off the hotspot. Keep it Off; Tailscale doesn't need it.
- `tmux a` over SSH to watch live build/controlsd output.
- StarPilot UI toggle "Use Precompiled Binaries": OFF during dev, ON for road-only days.
- Build time on c3 is 10–20 min for full clean, <1 min for Python-only edits.

## What lives in `../openpilot/research/`
The research directory in the SIBLING `openpilot/` dir (the old twilsonco fork — kept for rollback).

| File | Purpose |
|------|---------|
| `00-summary.md` | Executive summary of the migration. |
| `01-comma3-os-compatibility.md` | OS / AGNOS safety floor. |
| `02-fork-comparison.md` | Why StarPilot. |
| `03-volt-state-of-support.md` | Volt-specific gotchas (cruise fault, harness reliability, SNG hack). |
| `04-iteration-workflow.md` | The dev loop. |
| `05-discord-mining.md` | Source quotes from twilsonco discord. |
| `requirements.md` | Personal driving preferences and the implementation map. |

## Things to NOT touch
- `panda/` submodule — panda firmware is signed/checked. Don't modify, don't try to bypass safety.
- AGNOS image references (`*/agnos.json`, `system/hardware/tici/agnos.py`) — wrong AGNOS bricks the device's recoverability.
- `release/` — not relevant to dev.
- Git submodules in general unless coordinating a deliberate update.

## When you're confused, check
1. Is the file changed in `macsux-tweaks` and not yet pushed to the device? (`git diff StarPilot..macsux-tweaks`).
2. Is the device running a stale `prebuilt` marker? (`ssh comma 'ls -la /data/openpilot/prebuilt'` — if it exists, scons skips rebuilding).
3. Did `manager` actually restart? (check `tmux a` output for the rebanner).
4. Can't reach the device? `ssh comma` needs Tailscale up on the Mac; check `tailscale status` for `comma3`. On the device, `/data/tailscale/ensure.log` says what the bring-up did.
