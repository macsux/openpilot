#!/usr/bin/env python3
from openpilot.common.constants import CV


HIGHWAY_LEAD_BEHAVIOR_MIN_SPEED = 45. * CV.MPH_TO_MS


def get_tracked_lead_catchup_bias(v_ego: float, lead_distance: float, desired_gap: float, closing_speed: float,
                                  v_cruise: float | None = None) -> float:
  gap_error = lead_distance - desired_gap
  actual_hw = lead_distance / max(v_ego, 1e-3)
  desired_hw = desired_gap / max(v_ego, 1e-3)

  if v_ego <= HIGHWAY_LEAD_BEHAVIOR_MIN_SPEED:
    return 0.0
  if v_cruise is not None and v_ego >= v_cruise:
    return 0.0
  if gap_error <= 0.0:
    return 0.0

  # Encourage ACC to treat a tracked lead as the active constraint when we're
  # hanging far above the requested time gap, but don't override cruise for a
  # truly distant lead or one we're already closing on decisively.
  if actual_hw <= max(desired_hw + 0.3, 1.72):
    return 0.0
  if actual_hw >= max(desired_hw + 1.6, 3.0):
    return 0.0
  if closing_speed > max(2.5, 0.12 * v_ego):
    return 0.0

  return min(gap_error * 0.65, max(14.0, 0.75 * v_ego))


def should_disable_far_lead_throttle(v_ego: float, lead_distance: float, desired_gap: float,
                                     closing_speed: float, following_lead: bool) -> bool:
  actual_hw = lead_distance / max(v_ego, 1e-3)
  desired_hw = desired_gap / max(v_ego, 1e-3)

  if following_lead or v_ego <= HIGHWAY_LEAD_BEHAVIOR_MIN_SPEED:
    return False

  # Don't coast if we're already materially above the requested headway.
  if actual_hw > max(desired_hw + 0.45, 1.85):
    return False

  coast_window_open = lead_distance > desired_gap + max(4.0, 0.15 * v_ego)
  coast_window_far = lead_distance < desired_gap + max(15.0, 0.75 * v_ego)
  gentle_closing = closing_speed < max(1.5, 0.08 * v_ego)
  ttc = lead_distance / max(closing_speed, 1e-3) if closing_speed > 0.1 else 1e6

  return coast_window_open and coast_window_far and gentle_closing and ttc > 6.0 and lead_distance > desired_gap + 6.0


# macsux: "gap coast". Inside the target gap but not closing (a cut-in that's pulling away,
# or we crept a little close): lift off and let the gap regrow instead of braking to
# restore it. Rapid closing, a braking lead, or a gap under the hard floor hand control
# straight back to the MPC's normal braking.
GAP_COAST_MIN_SPEED = 5.0          # m/s; below this stop/creep logic owns the gap
GAP_COAST_MIN_HEADWAY = 0.9        # s; hard floor on top of stop_distance
GAP_COAST_MARGIN = 1.0             # m; keep the MPC's target a hair under the real gap
GAP_COAST_RECOVER_RATE = 0.3       # s of t_follow per second: how fast the target gap grows back after a coast
# Deadband around the target gap so steady following (which lives right at the target) never
# trips the coast: enter only when clearly inside the target, leave once nearly back at it.
GAP_COAST_ENTER_FRAC, GAP_COAST_ENTER_MIN_M = 0.08, 3.0
GAP_COAST_EXIT_FRAC, GAP_COAST_EXIT_MIN_M = 0.02, 1.0


def gap_coast_thresholds(v_ego: float, coasting: bool) -> tuple[float, float, float]:
  """(max closing speed, min TTC, min lead accel) — looser while already coasting (hysteresis)."""
  closing_limit = max(1.0, 0.05 * v_ego)
  if coasting:
    return closing_limit * 1.5, 6.0, -1.5
  return closing_limit, 8.0, -1.0


def compute_gap_coast(v_ego: float, lead_distance: float, v_lead: float, a_lead: float, t_follow: float,
                      stop_distance: float, comfort_brake: float, coasting: bool) -> tuple[bool, float]:
  """Returns (coast, effective t_follow). t_follow is untouched when not coasting."""
  if v_ego <= GAP_COAST_MIN_SPEED or t_follow <= 0.0:
    return False, t_follow

  # same distance model as the MPC: desired = v²/(2b) + t·v + stop − v_lead²/(2b)
  brake_term = (v_ego ** 2 - v_lead ** 2) / (2.0 * comfort_brake)
  desired_gap = brake_term + t_follow * v_ego + stop_distance
  floor_gap = stop_distance + GAP_COAST_MIN_HEADWAY * v_ego
  if coasting:
    upper = desired_gap - max(GAP_COAST_EXIT_MIN_M, GAP_COAST_EXIT_FRAC * desired_gap)
  else:
    upper = desired_gap - max(GAP_COAST_ENTER_MIN_M, GAP_COAST_ENTER_FRAC * desired_gap)
  if not (floor_gap < lead_distance < upper):
    return False, t_follow

  closing = v_ego - v_lead
  closing_limit, min_ttc, min_a_lead = gap_coast_thresholds(v_ego, coasting)
  ttc = lead_distance / closing if closing > 0.1 else float("inf")
  if closing > closing_limit or ttc < min_ttc or a_lead < min_a_lead:
    return False, t_follow

  # t_follow that puts the MPC's target gap just under the gap we actually have
  t_eq = (lead_distance - GAP_COAST_MARGIN - stop_distance - brake_term) / v_ego
  t_eff = min(max(t_eq, GAP_COAST_MIN_HEADWAY), t_follow)
  return True, t_eff


def gap_coast_danger(v_ego: float, lead_distance: float, v_lead: float, a_lead: float, stop_distance: float) -> bool:
  """True when the lead situation needs braking now: no gradual recovery of the target gap."""
  closing = v_ego - v_lead
  ttc = lead_distance / closing if closing > 0.1 else float("inf")
  floor_gap = stop_distance + GAP_COAST_MIN_HEADWAY * v_ego
  return ttc < 6.0 or a_lead < -1.5 or lead_distance < floor_gap


def recover_t_follow(t_follow: float, prev_t_follow: float, dt: float, danger: bool) -> float:
  """After a coast, grow the target gap back at GAP_COAST_RECOVER_RATE so braking builds up
  progressively instead of stepping in; a dangerous lead skips the ramp."""
  if danger or prev_t_follow <= 0.0 or prev_t_follow >= t_follow:
    return t_follow
  return min(t_follow, prev_t_follow + GAP_COAST_RECOVER_RATE * dt)


# macsux: bounded jerk on the planner's output so braking builds up progressively (and eases
# off progressively) like a human foot, instead of stepping between "pull" and the decel floor.
ACCEL_JERK_DOWN = 0.6   # m/s^3 toward more braking (+0.5 -> -0.5 takes ~1.7 s, 0 -> -0.5 ~0.8 s)
ACCEL_JERK_UP = 1.0     # m/s^3 toward less braking / more throttle
ACCEL_JERK_MIN_SPEED = 5.0  # m/s; below this the stop/creep logic owns the pedal


def limit_accel_jerk(target: float, prev: float, dt: float, v_ego: float, urgent: bool) -> float:
  """Rate-limit the change of the commanded accel. `urgent` (close/braking lead, FCW, hard brake
  request) passes the target straight through."""
  if urgent or v_ego < ACCEL_JERK_MIN_SPEED:
    return target
  lo = prev - ACCEL_JERK_DOWN * dt
  hi = prev + ACCEL_JERK_UP * dt
  return min(max(target, lo), hi)
