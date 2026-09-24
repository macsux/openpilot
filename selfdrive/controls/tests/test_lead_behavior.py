import pytest

from openpilot.selfdrive.controls.lib.lead_behavior import (
  GAP_COAST_MIN_HEADWAY,
  GAP_COAST_RECOVER_RATE,
  compute_gap_coast,
  gap_coast_danger,
  recover_t_follow,
  get_tracked_lead_catchup_bias,
  is_radarless_matched_follow_window,
  should_hold_tracked_vision_lead,
  should_track_lead,
  should_disable_far_lead_throttle,
)


def test_tracked_lead_catchup_bias_for_hanging_gap():
  bias = get_tracked_lead_catchup_bias(31.4, 78.7, 38.0, 0.1)
  assert bias > 10.0


def test_tracked_lead_catchup_bias_ignores_near_desired_gap():
  bias = get_tracked_lead_catchup_bias(31.4, 50.0, 38.0, 0.1)
  assert bias == 0.0


def test_tracked_lead_catchup_bias_corrects_lightning_aggressive_follow_bookmark():
  default_bias = get_tracked_lead_catchup_bias(
    31.75,
    46.2,
    34.0,
    0.0,
    v_cruise=37.55,
    y_rel=0.1,
  )
  bias = get_tracked_lead_catchup_bias(
    31.75,
    46.2,
    34.0,
    0.0,
    v_cruise=37.55,
    y_rel=0.1,
    min_headway_margin=0.20,
    full_headway_margin=0.45,
  )

  assert default_bias == 0.0
  assert bias > 2.0


def test_tracked_lead_catchup_bias_handles_crv_hanging_gap():
  tuned_bias = get_tracked_lead_catchup_bias(
    19.8,
    85.0,
    43.636,
    1.7,
    v_cruise=20.44,
    y_rel=-0.54,
    min_headway_margin=0.10,
    full_headway_margin=0.35,
    bias_gain=1.25,
    bias_cap=65.0,
    speed_range=(10.0, 18.0),
    fade_margins=(0.75, 6.5),
    cruise_error_full=0.75,
  )

  assert tuned_bias > 40.0


def test_tracked_lead_catchup_bias_ignores_very_far_gap():
  bias = get_tracked_lead_catchup_bias(31.4, 110.0, 38.0, 0.1)
  assert bias == 0.0


def test_tracked_lead_catchup_bias_applies_to_two_second_highway_gap():
  bias = get_tracked_lead_catchup_bias(30.4, 63.0, 40.0, 0.4)
  assert bias > 9.0


def test_tracked_lead_catchup_bias_reduces_for_laterally_offset_lead():
  centered = get_tracked_lead_catchup_bias(34.0, 103.0, 73.0, 1.9, y_rel=0.2)
  offset = get_tracked_lead_catchup_bias(34.0, 103.0, 73.0, 1.9, y_rel=1.95)
  assert centered > 0.0
  assert offset == 0.0


def test_tracked_lead_catchup_bias_fades_before_very_far_gap_cutoff():
  near_upper = get_tracked_lead_catchup_bias(34.0, 103.0, 73.0, 1.9)
  smaller_gap = get_tracked_lead_catchup_bias(34.0, 96.0, 73.0, 1.9)
  assert near_upper > 0.0
  assert near_upper < smaller_gap


def test_tracked_lead_catchup_bias_stays_off_once_at_set_speed():
  bias = get_tracked_lead_catchup_bias(31.4, 78.7, 38.0, 0.1, v_cruise=31.4)
  assert bias == 0.0


def test_tracked_lead_catchup_bias_fades_smoothly_near_set_speed():
  below_set = get_tracked_lead_catchup_bias(27.70, 81.0, 46.0, 0.0, v_cruise=27.78, y_rel=0.8)
  at_set = get_tracked_lead_catchup_bias(27.78, 81.0, 46.0, 0.0, v_cruise=27.78, y_rel=0.8)
  assert 0.0 < below_set < 0.25
  assert at_set == 0.0


def test_tracked_lead_catchup_bias_dials_back_camry_bookmark_case():
  bias = get_tracked_lead_catchup_bias(27.40, 81.0, 46.0, 0.0, v_cruise=27.78, y_rel=0.8)
  assert 0.0 < bias < 2.0


def test_tracked_lead_catchup_bias_fades_smoothly_at_closing_limit():
  below_limit = get_tracked_lead_catchup_bias(31.4, 78.7, 38.0, 3.75)
  above_limit = get_tracked_lead_catchup_bias(31.4, 78.7, 38.0, 3.77)
  assert abs(below_limit - above_limit) < 0.1


def test_disable_far_lead_throttle_rejects_two_second_plus_gap():
  should_disable = should_disable_far_lead_throttle(31.4, 78.7, 38.0, 0.1, False)
  assert not should_disable


def test_disable_far_lead_throttle_keeps_mild_coast_near_target_gap():
  should_disable = should_disable_far_lead_throttle(31.4, 52.0, 38.0, 0.5, False)
  assert should_disable


def test_disable_far_lead_throttle_waits_until_reduced_gap():
  should_disable = should_disable_far_lead_throttle(31.4, 43.5, 38.0, 0.5, False)
  assert should_disable


def test_disable_far_lead_throttle_rejects_fast_closing():
  should_disable = should_disable_far_lead_throttle(31.4, 52.0, 38.0, 3.5, False)
  assert not should_disable


def test_disable_far_lead_throttle_rejects_route_like_highway_stab_case():
  should_disable = should_disable_far_lead_throttle(34.69, 68.5, 63.0, 2.31, False)
  assert not should_disable


def test_disable_far_lead_throttle_rejects_large_gap_near_pace_matched_case():
  should_disable = should_disable_far_lead_throttle(32.43, 72.4, 56.0, 1.30, False)
  assert not should_disable


def test_should_track_lead_keeps_radar_leads_on_model_horizon():
  assert should_track_lead(True, 95.0, 100.0, 6.0, 30.0, v_lead=25.0, radar=True)


def test_should_track_lead_rejects_far_vision_only_highway_lead():
  assert not should_track_lead(True, 82.0, 140.0, 6.0, 29.0, v_lead=25.0, radar=False)


def test_should_track_lead_accepts_closer_vision_only_highway_lead():
  assert should_track_lead(True, 56.0, 140.0, 6.0, 29.0, v_lead=25.0, radar=False)


def test_should_track_lead_accepts_fast_closing_vision_lead_early():
  assert should_track_lead(True, 90.0, 140.0, 6.0, 20.0, v_lead=0.0, radar=False)


def test_should_hold_tracked_vision_lead_keeps_honda_bookmark_case():
  assert should_hold_tracked_vision_lead(
    True, 44.5, 174.0, 6.0, 16.8,
    model_prob=0.99, y_rel=-0.69, radar=False,
  )


def test_should_hold_tracked_vision_lead_does_not_expand_initial_tracking_gate():
  assert not should_track_lead(True, 44.5, 174.0, 6.0, 16.8, v_lead=16.7, radar=False)


def test_should_hold_tracked_vision_lead_releases_offcenter_lead():
  assert not should_hold_tracked_vision_lead(
    True, 44.5, 174.0, 6.0, 16.8,
    model_prob=0.99, y_rel=-1.7, radar=False,
  )


def test_should_hold_tracked_vision_lead_uses_path_relative_offset_on_curve():
  assert should_hold_tracked_vision_lead(
    True, 27.8, 174.0, 6.0, 16.0,
    model_prob=1.0, y_rel=-2.11, path_y=1.32, radar=False,
  )


def test_should_hold_tracked_vision_lead_releases_low_confidence_lead():
  assert not should_hold_tracked_vision_lead(
    True, 44.5, 174.0, 6.0, 16.8,
    model_prob=0.69, y_rel=0.0, radar=False,
  )


def test_should_hold_tracked_vision_lead_does_not_change_radar_tracking():
  assert not should_hold_tracked_vision_lead(
    True, 44.5, 174.0, 6.0, 16.8,
    model_prob=0.99, y_rel=0.0, radar=True,
  )


def test_should_hold_tracked_vision_lead_keeps_braking_lead():
  assert should_hold_tracked_vision_lead(
    True, 44.5, 174.0, 6.0, 16.8,
    model_prob=0.99, y_rel=0.0, radar=False,
  )


def test_should_hold_tracked_vision_lead_releases_beyond_exit_gap():
  assert not should_hold_tracked_vision_lead(
    True, 57.0, 174.0, 6.0, 16.8,
    model_prob=0.99, y_rel=0.0, radar=False,
  )


def test_should_hold_tracked_vision_lead_ignores_shortened_model_horizon_in_bolt_stutter_case():
  assert should_hold_tracked_vision_lead(
    True, 54.6, 40.0, 6.0, 19.4,
    model_prob=1.0, y_rel=0.05, radar=False,
  )


def test_should_hold_tracked_vision_lead_does_not_extend_low_confidence_short_horizon_case():
  assert not should_hold_tracked_vision_lead(
    True, 54.6, 40.0, 6.0, 19.4,
    model_prob=0.90, y_rel=0.05, radar=False,
  )


def test_radarless_matched_follow_window_accepts_pace_matched_highway_follow():
  assert is_radarless_matched_follow_window(31.0, 48.0, 30.4, 1.45, radar=False, lead_brake=0.05, lead_prob=0.95)


def test_radarless_matched_follow_window_rejects_large_relative_speed():
  assert not is_radarless_matched_follow_window(31.0, 48.0, 27.5, 1.45, radar=False, lead_brake=0.05, lead_prob=0.95)


def test_radarless_matched_follow_window_rejects_far_headway():
  assert not is_radarless_matched_follow_window(31.0, 82.0, 30.4, 1.45, radar=False, lead_brake=0.05, lead_prob=0.95)


def test_radarless_matched_follow_window_rejects_low_confidence_lead():
  assert not is_radarless_matched_follow_window(31.0, 48.0, 30.4, 1.45, radar=False, lead_brake=0.05, lead_prob=0.55)


def test_radarless_matched_follow_window_keeps_default_low_speed_guard():
  assert not is_radarless_matched_follow_window(14.4, 25.4, 16.2, 1.25, radar=False, lead_brake=0.0, lead_prob=1.0)


def test_radarless_matched_follow_window_accepts_lower_speed_when_requested():
  assert is_radarless_matched_follow_window(14.4, 25.4, 16.2, 1.25, radar=False, lead_brake=0.0, lead_prob=1.0, min_speed=12.0)


# macsux gap coast. Scenario base: 30 m/s, standard 1.45 s follow, pace-matched lead.
# desired gap = 1.45*30 + 6 = 49.5 m; floor = 0.9*30 + 6 = 33 m; enter below 45.5 m, exit above 48.5 m.
_V, _T, _STOP, _BRAKE = 30.0, 1.45, 6.0, 2.5


def _coast(d_rel, v_lead=_V, a_lead=0.0, coasting=False, v_ego=_V, t_follow=_T):
  return compute_gap_coast(v_ego, d_rel, v_lead, a_lead, t_follow, _STOP, _BRAKE, coasting)


def test_gap_coast_engages_inside_target_gap_when_not_closing():
  coast, t_eff = _coast(40.0)
  assert coast
  # target gap pinned 1 m under the real gap: (40 - 1 - 6) / 30
  assert t_eff == pytest.approx(1.1)


def test_gap_coast_never_lengthens_t_follow():
  coast, t_eff = _coast(45.0)
  assert coast
  assert t_eff < _T


def test_gap_coast_ignores_steady_following_at_the_target_deadband():
  coast, t_eff = _coast(47.0)
  assert not coast
  assert t_eff == _T


def test_gap_coast_stays_off_under_the_headway_floor():
  coast, t_eff = _coast(30.0)
  assert not coast
  assert t_eff == _T


def test_gap_coast_clamps_to_the_headway_floor_when_barely_above_it():
  coast, t_eff = _coast(34.0)
  assert coast
  assert t_eff == GAP_COAST_MIN_HEADWAY


@pytest.mark.parametrize("v_lead, a_lead", [
  (27.0, 0.0),    # closing 3 m/s > 1.5 m/s limit at 30 m/s
  (30.0, -1.2),   # lead braking harder than -1.0
])
def test_gap_coast_hands_back_to_mpc_when_closing_or_lead_braking(v_lead, a_lead):
  coast, t_eff = _coast(40.0, v_lead=v_lead, a_lead=a_lead)
  assert not coast
  assert t_eff == _T


def test_gap_coast_tolerates_gentle_closing():
  coast, _ = _coast(40.0, v_lead=29.0)  # closing 1 m/s, TTC 40 s
  assert coast


def test_gap_coast_hysteresis_holds_near_the_exit_and_loosens_thresholds():
  assert not _coast(47.5)[0]
  assert _coast(47.5, coasting=True)[0]
  # 2 m/s closing is over the 1.5 m/s entry limit but under the 2.25 m/s coasting limit
  assert not _coast(40.0, v_lead=28.0)[0]
  assert _coast(40.0, v_lead=28.0, coasting=True)[0]
  assert not _coast(49.0, coasting=True)[0]


def test_gap_coast_is_off_at_low_speed_and_without_a_follow_time():
  assert _coast(10.0, v_ego=4.0, v_lead=4.0) == (False, _T)
  assert _coast(40.0, t_follow=0.0) == (False, 0.0)


def test_gap_coast_uses_the_mpc_distance_model_for_a_slower_lead():
  # a slower lead needs more room (the MPC's brake term), so the same 40 m is further inside
  # the target and the pinned t_follow comes out shorter: (40 - 1 - 6 - 5.95) / 30
  coast, t_eff_slower = _coast(40.0, v_lead=29.5)
  _, t_eff_matched = _coast(40.0)
  assert coast
  assert t_eff_slower == pytest.approx(0.9017, abs=1e-3)
  assert t_eff_slower < t_eff_matched


@pytest.mark.parametrize("d_rel, v_lead, a_lead, expected", [
  (40.0, 30.0, 0.0, False),
  (40.0, 22.0, 0.0, True),    # TTC 5 s
  (40.0, 30.0, -2.0, True),   # hard-braking lead
  (30.0, 30.0, 0.0, True),    # under the floor
])
def test_gap_coast_danger(d_rel, v_lead, a_lead, expected):
  assert gap_coast_danger(_V, d_rel, v_lead, a_lead, _STOP) is expected


def test_recover_t_follow_ramps_back_and_snaps_on_danger():
  dt = 0.05
  assert recover_t_follow(_T, 1.1, dt, False) == pytest.approx(1.1 + GAP_COAST_RECOVER_RATE * dt)
  assert recover_t_follow(_T, 1.44, dt, False) == _T
  assert recover_t_follow(_T, 1.1, dt, True) == _T
  assert recover_t_follow(_T, 0.0, dt, False) == _T
  assert recover_t_follow(_T, 1.75, dt, False) == _T
