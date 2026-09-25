"""ha_pushd: the push decisions (Tracker) and the sender's HA webhook handling, against a
local stand-in for Home Assistant's mobile_app webhook."""
import importlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType, SimpleNamespace

import pytest


def _stub_if_missing(name, **attributes):
  try:
    importlib.import_module(name)
  except Exception:
    module = ModuleType(name)
    for key, value in attributes.items():
      setattr(module, key, value)
    sys.modules[name] = module


_log = SimpleNamespace(warnings=[], infos=[])
_stub_if_missing("cereal.messaging", SubMaster=None)
_stub_if_missing("openpilot.common.params", Params=lambda: SimpleNamespace(get=lambda *_a, **_k: None))
_stub_if_missing("openpilot.common.gps", get_gps_location_service=lambda _p: "gpsLocationExternal")
_stub_if_missing("openpilot.common.swaglog", cloudlog=SimpleNamespace(warning=lambda *_a: None, info=lambda *_a: None))

ha_pushd = importlib.import_module("openpilot.starpilot.system.ha_pushd")
Tracker, Sender = ha_pushd.Tracker, ha_pushd.Sender

FIX = (43.65, -79.38, 4.2, 120.0, 271.6)


# ----------------------------------------------------------------------------- Tracker

def test_first_update_pushes_immediately_with_the_seed_of_unknown_age():
  tr = Tracker(seed=FIX)
  s = tr.update(100.0, False, None, 0.0)
  assert s is not None and s["transition"]
  assert s["drive_state"] == "parked" and s["started"] is False
  assert s["fix"] == FIX and s["fix_age_s"] is None
  assert tr.update(101.0, False, None, 0.0) is None


def test_onroad_pushes_every_interval_with_only_fresh_unsent_fixes():
  tr = Tracker()
  assert tr.update(0.0, True, "drive", 20.0)["transition"]  # ignition edge
  tr.set_fix(*FIX, now=10.0)
  assert tr.update(14.0, True, "drive", 20.0) is None      # inside the 15 s interval
  s = tr.update(15.0, True, "drive", 20.0)
  assert s["fix"] == FIX and s["fix_age_s"] == 5.0 and s["speed_ms"] == 20.0 and s["speed_kmh"] == pytest.approx(72.0)
  s = tr.update(30.0, True, "drive", 20.0)
  assert s is not None and s["fix"] is None               # same fix, already sent
  tr.set_fix(*FIX, now=38.0)
  s = tr.update(45.0, True, "drive", 20.0)
  assert s["fix"] is None and s["fix_age_s"] == 7.0       # newer fix, but stale


def test_gear_and_ignition_transitions_push_immediately_and_arrival_sends_the_last_fix():
  tr = Tracker()
  tr.update(0.0, True, "drive", 15.0)
  s = tr.update(1.0, True, "park", 0.0)
  assert s["transition"] and s["drive_state"] == "parked" and s["gear"] == "park"
  tr.set_fix(*FIX, now=1.5)
  s = tr.update(2.0, True, "park", 0.0)
  assert s is None                                        # no transition, inside the interval
  s = tr.update(40.0, False, None, 0.0)                   # ignition off: the fix is stale but this is the parking spot
  assert s["transition"] and s["started"] is False and s["speed_ms"] == 0.0
  assert s["fix"] == FIX
  assert tr.update(100.0, False, None, 0.0) is None       # offroad interval is 10 min
  assert tr.update(641.0, False, None, 0.0)["fix"] is None


def test_unknown_gear_keeps_the_last_gear_and_reverse_counts_as_driving():
  tr = Tracker()
  tr.update(0.0, True, "reverse", 2.0)
  assert tr.drive_state == "driving"
  tr.update(20.0, True, "unknown", 2.0)
  assert tr.gear == "reverse"


def test_location_payload_uses_m_per_s_and_omits_speed_course_when_stopped():
  moving = ha_pushd.location_payload({"fix": FIX, "speed_ms": 20.4})
  assert moving == {"gps": [43.65, -79.38], "gps_accuracy": 4, "speed": 20, "course": 272, "altitude": 120}
  stopped = ha_pushd.location_payload({"fix": (43.65, -79.38, 0.4, 0.0, 12.0), "speed_ms": 0.0})
  assert stopped == {"gps": [43.65, -79.38], "gps_accuracy": 1}


def test_no_wall_clock_in_the_daemon():
  import inspect
  assert "time.time(" not in inspect.getsource(ha_pushd)


# ----------------------------------------------------------------------------- Sender

class _FakeHA:
  """Mimics HA's mobile_app webhook: an unknown webhook id gets 200 with an empty body for
  everything; a known one answers get_config with JSON and reports per-sensor results."""

  def __init__(self):
    self.known = True
    self.reject_next_update = False
    self.posts = []
    self.lock = threading.Lock()
    fake = self

    class Handler(BaseHTTPRequestHandler):
      def log_message(self, *_a):
        pass

      def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with fake.lock:
          fake.posts.append(body)
          known, reject = fake.known, fake.reject_next_update
        if not known:
          self.send_response(200)
          self.send_header("Content-Length", "0")
          self.end_headers()
          return
        t = body["type"]
        if t == "get_config":
          reply, code = {"version": "2026.9.0", "location_name": "Home"}, 200
        elif t == "register_sensor":
          reply, code = {"success": True}, 201
        elif t == "update_location":
          reply, code = {}, 200
        elif t == "update_sensor_states":
          reply = {d["unique_id"]: {"success": True} for d in body["data"]}
          if reject:
            reply["car_speed"] = {"success": False, "error": {"code": "not_registered", "message": "Entity is not registered"}}
            with fake.lock:
              fake.reject_next_update = False
          code = 200
        else:
          reply, code = {}, 400
        raw = json.dumps(reply).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=self.server.serve_forever, daemon=True).start()
    self.url = f"http://127.0.0.1:{self.server.server_port}/api/webhook/abc"

  def types(self):
    with self.lock:
      return [p["type"] for p in self.posts]

  def close(self):
    self.server.shutdown()


@pytest.fixture
def ha():
  fake = _FakeHA()
  yield fake
  fake.close()


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
  monkeypatch.setattr(ha_pushd, "RETRY_MIN_S", 0.05)
  monkeypatch.setattr(ha_pushd, "RETRY_MAX_S", 0.2)
  # capture the daemon's log lines whether the real cloudlog or a stub was imported
  monkeypatch.setattr(ha_pushd, "cloudlog", SimpleNamespace(warning=_log.warnings.append, info=_log.infos.append))
  _log.warnings.clear()
  _log.infos.clear()


def _wait(pred, timeout=3.0):
  t0 = time.monotonic()
  while not pred():
    if time.monotonic() - t0 > timeout:
      return False
    time.sleep(0.01)
  return True


def _snapshot(**kw):
  s = {"started": True, "drive_state": "driving", "gear": "drive", "speed_ms": 20.0, "speed_kmh": 72.0, "fix": FIX,
       "fix_age_s": 0.5, "transition": True}
  s.update(kw)
  return s


def test_sender_registers_with_concrete_states_then_pushes(ha):
  sender = Sender(ha.url)
  sender.start()
  sender.submit(_snapshot())
  assert _wait(lambda: sender.pending is None)
  assert ha.types() == ["get_config", "register_sensor", "register_sensor", "register_sensor", "update_location", "update_sensor_states"]
  regs = [p["data"] for p in ha.posts if p["type"] == "register_sensor"]
  assert all(r["state"] is not None for r in regs)
  assert not any(r.get("device_class") == "enum" for r in regs)
  loc = next(p["data"] for p in ha.posts if p["type"] == "update_location")
  assert loc["speed"] == 20 and loc["gps"] == [43.65, -79.38]
  states = {d["unique_id"]: d["state"] for d in next(p["data"] for p in ha.posts if p["type"] == "update_sensor_states")}
  assert states == {"car_speed": 72.0, "car_drive_state": "driving", "car_ignition": True}
  assert not _log.warnings

  sender.submit(_snapshot(speed_ms=0.0, speed_kmh=0.0, fix=None, drive_state="parked", gear="park"))
  assert _wait(lambda: sender.pending is None)
  assert ha.types()[6:] == ["update_sensor_states"]          # registered once, no location without a fix


def test_sender_refuses_an_unknown_webhook_backs_off_and_logs_once(ha):
  ha.known = False
  sender = Sender(ha.url)
  sender.start()
  sender.submit(_snapshot())
  assert _wait(lambda: sender.failures >= 3)
  assert sender.registered is False
  assert set(ha.types()) == {"get_config"}                      # never registers or pushes into the void
  assert len(_log.warnings) == 1 and "not registered" in _log.warnings[0]
  assert sender.retry_s == pytest.approx(0.2)                   # backed off to the cap

  ha.known = True                                               # config fixed on the HA side: recovers on its own
  assert _wait(lambda: sender.pending is None)
  assert sender.registered and not sender.failing and sender.retry_s == pytest.approx(0.05)
  assert len(_log.warnings) == 1 and len(_log.infos) == 1


def test_sender_reregisters_when_ha_reports_a_sensor_not_registered(ha):
  sender = Sender(ha.url)
  sender.start()
  sender.submit(_snapshot())
  assert _wait(lambda: sender.pending is None)
  ha.reject_next_update = True
  sender.submit(_snapshot(speed_ms=10.0, speed_kmh=36.0, fix=None))
  assert _wait(lambda: sender.pending is None)
  assert ha.types().count("register_sensor") == 6
  assert ha.types()[-1] == "update_sensor_states" and ha.types()[-2:-1] == ["register_sensor"]
  assert len(_log.warnings) == 1 and "re-registering" in _log.warnings[0]


def test_newer_snapshot_supersedes_a_failing_one_without_waiting_out_the_backoff(ha):
  ha.known = False
  sender = Sender(ha.url)
  sender.start()
  sender.submit(_snapshot(drive_state="driving"))
  assert _wait(lambda: sender.failures >= 2)
  ha.known = True
  t0 = time.monotonic()
  sender.submit(_snapshot(drive_state="parked", gear="park", speed_ms=0.0, speed_kmh=0.0))
  assert _wait(lambda: sender.pending is None)
  assert time.monotonic() - t0 < 0.15                            # the notify cut the 0.2 s backoff short
  sent = [d["state"] for p in ha.posts if p["type"] == "update_sensor_states" for d in p["data"] if d["unique_id"] == "car_drive_state"]
  assert sent == ["parked"]
