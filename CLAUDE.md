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

## What's NOT a code change — toggles in StarPilot UI to flip after install
- **Always On Lateral** (Settings → Lateral → Always On Lateral) — covers the steering-wheel-touch nag.
- **Volt SNG Hack** (Settings → Vehicle → GM Settings → Volt SNG Hack) — auto-resume from stop. Only visible because our 2017 Volt has no factory SNG.
- **Personality profiles** (Settings → Longitudinal → Personalities) — follow distance tuning.
- Driver-distraction events `driverDistracted` and `driverUnresponsive` only carry `ET.PERMANENT` in StarPilot — they show alerts but don't disengage. No patch needed.

## Iteration workflow
See `../openpilot/research/04-iteration-workflow.md`. Key points:
- Push code via `rsync` from this dir to `comma:/data/openpilot/`.
- Restart with `./rr.sh` (rebootless, restarts manager + children) — not `pkill controlsd`, that no longer works.
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
