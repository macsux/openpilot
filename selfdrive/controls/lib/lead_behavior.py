#!/usr/bin/env python3
from openpilot.common.constants import CV


HIGHWAY_LEAD_BEHAVIOR_MIN_SPEED = 45. * CV.MPH_TO_MS
TRACKED_LEAD_CATCHUP_BIAS_FULL_SPEED = 52. * CV.MPH_TO_MS
TRACKED_LEAD_CATCHUP_BIAS_CRUISE_ERROR_FULL = 1.5
VISION_LEAD_TRACK_MIN_DISTANCE = 25.0
VISION_LEAD_TRACK_BASE_TIME_GAP = 1.75
VISION_LEAD_TRACK_CLOSING_GAIN = 0.20
VISION_LEAD_TRACK_CLOSING_CAP = 2.50
VISION_LEAD_TRACK_EXIT_TIME_GAP = 2.30
VISION_LEAD_TRACK_EXIT_MAX_LATERAL_OFFSET = 1.6
VISION_LEAD_TRACK_EXIT_MIN_MODEL_PROB = 0.70
VISION_LEAD_TRACK_CONTINUITY_MIN_MODEL_PROB = 0.95
VISION_LEAD_TRACK_CONTINUITY_MAX_LATERAL_OFFSET = 1.1
VISION_LEAD_TRACK_CONTINUITY_TIME_GAP_GAIN = 0.55
VISION_LEAD_TRACK_CONTINUITY_FULL_SPEED = 20.0
VISION_LEAD_TRACK_CONTINUITY_FADE_SPEED = 25.0
TRACKED_LEAD_CATCHUP_BIAS_MIN_HEADWAY_MARGIN = 0.40
TRACKED_LEAD_CATCHUP_BIAS_FULL_HEADWAY_MARGIN = 0.70
TRACKED_LEAD_CATCHUP_BIAS_MIN_FADE_START_MARGIN = 0.75
TRACKED_LEAD_CATCHUP_BIAS_MIN_FADE_END_MARGIN = 1.05
TRACKED_LEAD_CATCHUP_BIAS_ABSOLUTE_FADE_START = 2.75
TRACKED_LEAD_CATCHUP_BIAS_ABSOLUTE_FADE_END = 3.10
TRACKED_LEAD_CATCHUP_BIAS_FULL_LATERAL_OFFSET = 0.90
TRACKED_LEAD_CATCHUP_BIAS_MAX_LATERAL_OFFSET = 1.60
TRACKED_LEAD_CATCHUP_BIAS_GAIN = 0.45
TRACKED_LEAD_CATCHUP_BIAS_SPEED_FACTOR = 0.55
RADARLESS_MATCHED_FOLLOW_MIN_SPEED = 22.0
RADARLESS_MATCHED_FOLLOW_MAX_REL_SPEED = 2.0
RADARLESS_MATCHED_FOLLOW_MIN_HEADWAY = 0.95
RADARLESS_MATCHED_FOLLOW_HEADWAY_BELOW_TARGET = 0.35
RADARLESS_MATCHED_FOLLOW_HEADWAY_ABOVE_TARGET = 0.90
RADARLESS_MATCHED_FOLLOW_MAX_LEAD_BRAKE = 0.35
RADARLESS_MATCHED_FOLLOW_MIN_MODEL_PROB = 0.70
FAR_LEAD_COAST_MIN_GAP = 3.5


def _smoothstep(value: float, start: float, end: float) -> float:
  factor = min(1.0, max(0.0, (float(value) - float(start)) / max(float(end) - float(start), 1e-3)))
  return factor * factor * (3.0 - 2.0 * factor)


def should_track_lead(lead_status: bool, lead_distance: float, model_length: float, stop_distance: float,
                      v_ego: float, *, v_lead: float | None = None, radar: bool = False) -> bool:
  if not lead_status:
    return False

  tracking_buffer = max(float(stop_distance), 4.0)
  model_limit = float(model_length) + tracking_buffer
  if radar:
    return float(lead_distance) < model_limit

  closing_speed = max(0.0, float(v_ego) - float(v_lead if v_lead is not None else v_ego))
  vision_time_gap = VISION_LEAD_TRACK_BASE_TIME_GAP + min(closing_speed * VISION_LEAD_TRACK_CLOSING_GAIN,
                                                          VISION_LEAD_TRACK_CLOSING_CAP)
  vision_limit = max(VISION_LEAD_TRACK_MIN_DISTANCE, float(v_ego) * vision_time_gap + tracking_buffer)
  return float(lead_distance) < min(model_limit, vision_limit)


def should_hold_tracked_vision_lead(lead_status: bool, lead_distance: float, model_length: float, stop_distance: float,
                                    v_ego: float, *, model_prob: float,
                                    y_rel: float, path_y: float = 0.0, radar: bool = False) -> bool:
  if not lead_status or radar or float(model_prob) < VISION_LEAD_TRACK_EXIT_MIN_MODEL_PROB:
    return False
  if abs(float(y_rel) + float(path_y)) > VISION_LEAD_TRACK_EXIT_MAX_LATERAL_OFFSET:
    return False

  tracking_buffer = max(float(stop_distance), 4.0)
  model_limit = float(model_length) + tracking_buffer
  vision_exit_limit = max(VISION_LEAD_TRACK_MIN_DISTANCE,
                          float(v_ego) * VISION_LEAD_TRACK_EXIT_TIME_GAP + tracking_buffer)
  if float(lead_distance) < min(model_limit, vision_exit_limit):
    return True

  path_relative_offset = abs(float(y_rel) + float(path_y))
  if (float(model_prob) < VISION_LEAD_TRACK_CONTINUITY_MIN_MODEL_PROB or
      path_relative_offset > VISION_LEAD_TRACK_CONTINUITY_MAX_LATERAL_OFFSET):
    return False

  speed_factor = 1.0 - _smoothstep(
    v_ego,
    VISION_LEAD_TRACK_CONTINUITY_FULL_SPEED,
    VISION_LEAD_TRACK_CONTINUITY_FADE_SPEED,
  )
  continuity_time_gap = VISION_LEAD_TRACK_EXIT_TIME_GAP + VISION_LEAD_TRACK_CONTINUITY_TIME_GAP_GAIN * speed_factor
  continuity_exit_limit = max(VISION_LEAD_TRACK_MIN_DISTANCE,
                              float(v_ego) * continuity_time_gap + tracking_buffer)
  return float(lead_distance) < continuity_exit_limit


def is_radarless_matched_follow_window(v_ego: float, lead_distance: float, v_lead: float, t_follow: float, *,
                                       radar: bool = False, lead_brake: float = 0.0,
                                       lead_prob: float = 0.0,
                                       min_speed: float = RADARLESS_MATCHED_FOLLOW_MIN_SPEED) -> bool:
  if radar or float(t_follow) <= 0.0 or float(v_ego) < float(min_speed):
    return False
  if float(lead_prob) < RADARLESS_MATCHED_FOLLOW_MIN_MODEL_PROB:
    return False
  if float(lead_brake) > RADARLESS_MATCHED_FOLLOW_MAX_LEAD_BRAKE:
    return False

  relative_speed = float(v_ego) - float(v_lead)
  if abs(relative_speed) > RADARLESS_MATCHED_FOLLOW_MAX_REL_SPEED:
    return False

  actual_headway = float(lead_distance) / max(float(v_ego), 1e-3)
  min_headway = max(RADARLESS_MATCHED_FOLLOW_MIN_HEADWAY,
                    float(t_follow) - RADARLESS_MATCHED_FOLLOW_HEADWAY_BELOW_TARGET)
  max_headway = float(t_follow) + RADARLESS_MATCHED_FOLLOW_HEADWAY_ABOVE_TARGET
  return min_headway <= actual_headway <= max_headway


def get_tracked_lead_catchup_bias(v_ego: float, lead_distance: float, desired_gap: float, closing_speed: float,
                                  v_cruise: float | None = None, y_rel: float | None = None,
                                  min_headway_margin: float = TRACKED_LEAD_CATCHUP_BIAS_MIN_HEADWAY_MARGIN,
                                  full_headway_margin: float = TRACKED_LEAD_CATCHUP_BIAS_FULL_HEADWAY_MARGIN,
                                  bias_gain: float = TRACKED_LEAD_CATCHUP_BIAS_GAIN,
                                  bias_cap: float | None = None,
                                  speed_range: tuple[float, float] | None = None,
                                  fade_margins: tuple[float, float] | None = None,
                                  cruise_error_full: float = TRACKED_LEAD_CATCHUP_BIAS_CRUISE_ERROR_FULL) -> float:
  gap_error = lead_distance - desired_gap
  actual_hw = lead_distance / max(v_ego, 1e-3)
  desired_hw = desired_gap / max(v_ego, 1e-3)
  headway_margin = actual_hw - desired_hw

  if gap_error <= 0.0:
    return 0.0

  speed_min, speed_full = speed_range or (HIGHWAY_LEAD_BEHAVIOR_MIN_SPEED, TRACKED_LEAD_CATCHUP_BIAS_FULL_SPEED)
  speed_factor = _smoothstep(v_ego, speed_min, speed_full)
  cruise_factor = 1.0
  if v_cruise is not None:
    cruise_factor = _smoothstep(v_cruise - v_ego, 0.0, cruise_error_full)
  if speed_factor == 0.0 or cruise_factor == 0.0:
    return 0.0

  # Encourage ACC to treat a tracked lead as the active constraint when we're
  # hanging far above the requested time gap, but don't override cruise for a
  # truly distant lead or one we're already closing on decisively.
  if fade_margins is None:
    fade_start_margin = max(TRACKED_LEAD_CATCHUP_BIAS_MIN_FADE_START_MARGIN,
                            TRACKED_LEAD_CATCHUP_BIAS_ABSOLUTE_FADE_START - desired_hw)
    fade_end_margin = max(TRACKED_LEAD_CATCHUP_BIAS_MIN_FADE_END_MARGIN,
                          TRACKED_LEAD_CATCHUP_BIAS_ABSOLUTE_FADE_END - desired_hw)
  else:
    fade_start_margin, fade_end_margin = fade_margins
  entry_factor = _smoothstep(headway_margin,
                             min_headway_margin,
                             full_headway_margin)
  exit_factor = 1.0 - _smoothstep(headway_margin, fade_start_margin, fade_end_margin)

  closing_fade_end = max(2.5, 0.12 * v_ego)
  closing_fade_start = max(1.75, 0.08 * v_ego)
  closing_factor = 1.0 - _smoothstep(closing_speed, closing_fade_start, closing_fade_end)

  lateral_factor = 1.0
  if y_rel is not None:
    lateral_offset = abs(float(y_rel))
    lateral_factor = 1.0 - _smoothstep(lateral_offset,
                                       TRACKED_LEAD_CATCHUP_BIAS_FULL_LATERAL_OFFSET,
                                       TRACKED_LEAD_CATCHUP_BIAS_MAX_LATERAL_OFFSET)

  if bias_cap is None:
    bias_cap = max(10.0, TRACKED_LEAD_CATCHUP_BIAS_SPEED_FACTOR * v_ego)
  return (min(gap_error * max(0.0, float(bias_gain)), float(bias_cap)) * speed_factor * cruise_factor *
          entry_factor * exit_factor * closing_factor * lateral_factor)


def should_disable_far_lead_throttle(v_ego: float, lead_distance: float, desired_gap: float,
                                     closing_speed: float, following_lead: bool) -> bool:
  actual_hw = lead_distance / max(v_ego, 1e-3)
  desired_hw = desired_gap / max(v_ego, 1e-3)

  if following_lead or v_ego <= HIGHWAY_LEAD_BEHAVIOR_MIN_SPEED:
    return False

  # Don't coast if we're already materially above the requested headway.
  if actual_hw > max(desired_hw + 0.15, 1.75):
    return False

  coast_window_open = lead_distance > desired_gap + max(4.0, 0.15 * v_ego)
  coast_window_far = lead_distance < desired_gap + max(12.0, 0.60 * v_ego)
  gentle_closing = 0.35 < closing_speed < max(1.35, 0.05 * v_ego)
  ttc = lead_distance / max(closing_speed, 1e-3) if closing_speed > 0.1 else 1e6

  return (coast_window_open and coast_window_far and gentle_closing and ttc > 7.5 and
          lead_distance > desired_gap + FAR_LEAD_COAST_MIN_GAP)


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
