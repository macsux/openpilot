"""macsux gap coast at the StarPilotFollowing level: real lead_behavior, stubbed heavy deps
(same stub pattern as test_personality_following_profiles.py)."""
import importlib
import sys
from enum import IntEnum
from types import ModuleType, SimpleNamespace

import pytest


def _module(name, **attributes):
  module = ModuleType(name)
  for key, value in attributes.items():
    setattr(module, key, value)
  return module


class LaneChangeState(IntEnum):
  off = 0
  preLaneChange = 1
  laneChangeStarting = 2
  laneChangeFinishing = 3


class LaneChangeDirection(IntEnum):
  none = 0
  left = 1
  right = 2


COMFORT_BRAKE = 2.5
STOP_DISTANCE = 6.0
DT = 0.05


def _desired_follow_distance(v_ego, v_lead, t_follow):
  return (v_ego ** 2 - v_lead ** 2) / (2 * COMFORT_BRAKE) + t_follow * v_ego + STOP_DISTANCE


def _get_t_follow(aggressive_follow=1.25, standard_follow=1.45, relaxed_follow=1.75,
                  custom_personalities=False, personality=1):
  configured = (aggressive_follow, standard_follow, relaxed_follow)
  return (configured if custom_personalities else (1.25, 1.45, 1.75))[int(personality)]


sys.modules["cereal"] = _module(
  "cereal", log=SimpleNamespace(LaneChangeState=LaneChangeState, LaneChangeDirection=LaneChangeDirection),
)
sys.modules["openpilot.common.constants"] = _module("openpilot.common.constants", CV=SimpleNamespace(MPH_TO_MS=0.44704))
sys.modules["openpilot.common.realtime"] = _module("openpilot.common.realtime", DT_MDL=DT)
sys.modules["openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc"] = _module(
  "openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc",
  COMFORT_BRAKE=COMFORT_BRAKE,
  LEAD_DANGER_FACTOR=0.75,
  STOP_DISTANCE=STOP_DISTANCE,
  desired_follow_distance=_desired_follow_distance,
  get_jerk_factor=lambda *_args: (1.0, 1.0, 1.0),
  get_T_FOLLOW=_get_t_follow,
)
sys.modules["openpilot.starpilot.common.starpilot_variables"] = _module(
  "openpilot.starpilot.common.starpilot_variables", CITY_SPEED_LIMIT=11.176, MAX_T_FOLLOW=3.0,
)
# Bind a fresh following module to the real lead_behavior, whatever another test module left cached.
sys.modules.pop("openpilot.selfdrive.controls.lib.lead_behavior", None)
sys.modules.pop("openpilot.starpilot.controls.lib.starpilot_following", None)
lead_behavior = importlib.import_module("openpilot.selfdrive.controls.lib.lead_behavior")
following_module = importlib.import_module("openpilot.starpilot.controls.lib.starpilot_following")

StarPilotFollowing = following_module.StarPilotFollowing

V_EGO = 30.0
T_STD = 1.45


def _planner(d_rel, v_lead=V_EGO, a_lead=0.0, tracking=True, status=True):
  lead = SimpleNamespace(status=status, dRel=d_rel, vLead=v_lead, aLeadK=a_lead)
  return SimpleNamespace(
    lead_one=lead,
    starpilot_weather=SimpleNamespace(weather_id=0, increase_following_distance=0.0),
    tracking_lead=tracking,
  )


def _sm(*, traffic=False, personality=1):
  return {
    "carState": SimpleNamespace(aEgo=0.0, standstill=False, leftBlindspot=False, rightBlindspot=False),
    "selfdriveState": SimpleNamespace(personality=personality),
    "starpilotCarState": SimpleNamespace(trafficModeEnabled=traffic),
  }


def _toggles():
  toggles = SimpleNamespace(
    aggressive_follow=1.25,
    standard_follow=1.45,
    relaxed_follow=1.75,
    conditional_slower_lead=False,
    custom_personalities=False,
    lane_change_close_gap=False,
    lane_change_close_gap_seconds=0.75,
    longitudinal_personality_profiles={},
    minimum_lane_change_speed=0.0,
    personality_ev_tuning=False,
    traffic_mode_follow=[0.75, 1.0],
  )
  for prefix in ("aggressive", "standard", "relaxed"):
    for suffix in ("acceleration", "danger", "deceleration", "speed", "speed_decrease"):
      setattr(toggles, f"{prefix}_jerk_{suffix}", 1.0)
  for suffix in ("acceleration", "danger", "deceleration", "speed", "speed_decrease"):
    setattr(toggles, f"traffic_mode_jerk_{suffix}", [1.0, 1.0])
  return toggles


def _set_lead(controller, d_rel, v_lead=V_EGO, a_lead=0.0):
  lead = controller.starpilot_planner.lead_one
  lead.dRel, lead.vLead, lead.aLeadK = d_rel, v_lead, a_lead


def test_cut_in_inside_the_gap_coasts_and_pins_the_target_gap():
  controller = StarPilotFollowing(_planner(40.0))

  controller.update(True, V_EGO, _sm(), _toggles())

  assert controller.gap_coast
  assert controller.disable_throttle
  assert controller.t_follow == pytest.approx(1.1)
  assert controller.desired_follow_distance == int(_desired_follow_distance(V_EGO, V_EGO, controller.t_follow))
  assert controller.desired_follow_distance < 40


def test_steady_following_at_the_target_does_not_coast():
  controller = StarPilotFollowing(_planner(48.0))

  controller.update(True, V_EGO, _sm(), _toggles())

  assert not controller.gap_coast
  assert not controller.disable_throttle
  assert controller.t_follow == pytest.approx(T_STD)
  assert controller.gap_coast_t_follow == 0.0


def test_coast_holds_through_hysteresis_then_ramps_the_gap_back():
  controller = StarPilotFollowing(_planner(40.0))
  sm, toggles = _sm(), _toggles()
  controller.update(True, V_EGO, sm, toggles)
  assert controller.gap_coast

  _set_lead(controller, 47.5)  # inside the 48.5 m exit line, outside the 45.5 m entry line
  controller.update(True, V_EGO, sm, toggles)
  assert controller.gap_coast
  assert controller.t_follow == pytest.approx(1.35)

  _set_lead(controller, 49.0)  # gap restored: leave the coast, ramp t_follow back at 0.3 s/s
  controller.update(True, V_EGO, sm, toggles)
  assert not controller.gap_coast
  assert not controller.disable_throttle
  assert controller.t_follow == pytest.approx(1.35 + lead_behavior.GAP_COAST_RECOVER_RATE * DT)
  assert controller.gap_coast_t_follow == pytest.approx(controller.t_follow)

  seen = [controller.t_follow]
  for _ in range(20):
    controller.update(True, V_EGO, sm, toggles)
    seen.append(controller.t_follow)
  steps = [b - a for a, b in zip(seen, seen[1:], strict=False)]
  assert all(step >= 0.0 for step in steps)
  assert max(steps) <= lead_behavior.GAP_COAST_RECOVER_RATE * DT + 1e-9
  assert controller.t_follow == pytest.approx(T_STD)
  assert controller.gap_coast_t_follow == 0.0


def test_hard_braking_lead_ends_the_coast_and_snaps_the_gap_back():
  controller = StarPilotFollowing(_planner(40.0))
  sm, toggles = _sm(), _toggles()
  controller.update(True, V_EGO, sm, toggles)
  assert controller.gap_coast

  _set_lead(controller, 40.0, a_lead=-2.0)
  controller.update(True, V_EGO, sm, toggles)

  assert not controller.gap_coast
  assert not controller.disable_throttle
  assert controller.t_follow == pytest.approx(T_STD)
  assert controller.gap_coast_t_follow == 0.0


def test_rapid_closing_ends_the_coast_but_rebuilds_the_gap_progressively():
  controller = StarPilotFollowing(_planner(40.0))
  sm, toggles = _sm(), _toggles()
  controller.update(True, V_EGO, sm, toggles)

  _set_lead(controller, 40.0, v_lead=27.0)  # closing 3 m/s, TTC 13 s: not yet dangerous
  controller.update(True, V_EGO, sm, toggles)

  assert not controller.gap_coast
  assert 1.1 < controller.t_follow < T_STD


@pytest.mark.parametrize("tracking, traffic, long_active", [
  (False, False, True),
  (True, True, True),
  (True, False, False),
])
def test_coast_is_gated_by_tracking_traffic_mode_and_long_control(tracking, traffic, long_active):
  controller = StarPilotFollowing(_planner(40.0, tracking=tracking))

  controller.update(long_active, V_EGO, _sm(traffic=traffic), _toggles())

  assert not controller.gap_coast
  assert controller.gap_coast_t_follow == 0.0


def test_recovery_ramp_only_follows_a_coast_so_other_t_follow_steps_pass_through():
  # A personality step with no coast in progress must see only upstream's lane-change gap
  # ramp (4 s/s), never the 0.3 s/s post-coast recovery.
  controller = StarPilotFollowing(_planner(60.0))
  toggles = _toggles()
  controller.update(True, V_EGO, _sm(personality=0), toggles)
  assert controller.t_follow == pytest.approx(1.25)

  seen = []
  for _ in range(3):
    controller.update(True, V_EGO, _sm(personality=2), toggles)
    seen.append(controller.t_follow)

  assert seen == pytest.approx([1.25 + following_module.LANE_CHANGE_GAP_RAMP_OUT_RATE * DT, 1.65, 1.75])
  assert controller.gap_coast_t_follow == 0.0
