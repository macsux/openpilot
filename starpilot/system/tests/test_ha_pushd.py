"""ha_pushd: the push decisions (Tracker) and the MQTT sender, against a local fake broker."""
import importlib
import json
import socket
import sys
import threading
import time
import socketserver
import struct
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


def test_payloads_are_compact_json_with_what_ha_needs():
  loc = json.loads(ha_pushd.location_payload({"fix": FIX, "speed_ms": 20.4}))
  assert loc == {"latitude": 43.65, "longitude": -79.38, "gps_accuracy": 4, "altitude": 120.0, "course": 272, "speed_ms": 20.4}
  assert json.loads(ha_pushd.location_payload({"fix": (43.65, -79.38, 0.4, 0.0, 12.0), "speed_ms": 0.0}))["gps_accuracy"] == 1
  state = json.loads(ha_pushd.state_payload({"speed_kmh": 72.04, "drive_state": "driving", "gear": "low", "started": True, "fix_age_s": 0.5}))
  assert state == {"speed_kmh": 72.0, "drive_state": "driving", "gear": "low", "ignition": True, "fix_age_s": 0.5}
  assert len(ha_pushd.state_payload({"speed_kmh": 72.04, "drive_state": "driving", "gear": "low", "started": True, "fix_age_s": 0.5})) < 100


def test_no_wall_clock_in_the_daemon():
  import inspect
  assert "time.time(" not in inspect.getsource(ha_pushd)


# ----------------------------------------------------------------------------- Sender (MQTT)

class _FakeBroker:
  """Enough MQTT 3.1.1 to serve a publish-only client: CONNECT/CONNACK (auth check), PUBLISH
  (recorded), PINGREQ/PINGRESP, DISCONNECT. Connections can be dropped to simulate a dead link."""

  def __init__(self, password="pw"):
    self.password = password
    self.connects, self.publishes, self.pings = [], [], 0
    self.lock = threading.Lock()
    self.conns = []
    broker = self

    class Handler(socketserver.BaseRequestHandler):
      def _exact(self, n):
        buf = b""
        while len(buf) < n:
          chunk = self.request.recv(n - len(buf))
          if not chunk:
            raise ConnectionError
          buf += chunk
        return buf

      def _packet(self):
        ptype = self._exact(1)[0]
        length, mult = 0, 1
        while True:
          d = self._exact(1)[0]
          length += (d & 0x7F) * mult
          mult *= 128
          if not d & 0x80:
            break
        return ptype, self._exact(length)

      def handle(self):
        with broker.lock:
          broker.conns.append(self.request)
        try:
          while True:
            ptype, body = self._packet()
            kind = ptype & 0xF0
            if kind == 0x10:
              i = 0
              (n,) = struct.unpack("!H", body[i:i + 2])
              i += 2 + n                                            # protocol name
              flags = body[i + 1]
              keepalive = struct.unpack("!H", body[i + 2:i + 4])[0]
              i += 4
              fields = []
              while i < len(body):                                  # length-prefixed payload fields
                (n,) = struct.unpack("!H", body[i:i + 2])
                fields.append(body[i + 2:i + 2 + n])
                i += 2 + n
              rec = {"client_id": fields.pop(0).decode(), "keepalive": keepalive, "will": None, "username": None, "password": None}
              if flags & 0x04:
                rec["will"] = (fields.pop(0).decode(), fields.pop(0), bool(flags & 0x20))
              if flags & 0x80:
                rec["username"] = fields.pop(0).decode()
              if flags & 0x40:
                rec["password"] = fields.pop(0).decode()
              with broker.lock:
                broker.connects.append(rec)
              rc = 0 if rec["password"] == broker.password else 5
              self.request.sendall(bytes([0x20, 2, 0, rc]))
              if rc:
                return
            elif kind == 0x30:
              qos = (ptype >> 1) & 0x03
              (n,) = struct.unpack("!H", body[:2])
              topic, rest = body[2:2 + n].decode(), body[2 + n:]
              packet_id, rest = (rest[:2], rest[2:]) if qos else (None, rest)
              with broker.lock:
                broker.publishes.append((topic, rest, bool(ptype & 0x01)))
              if qos:
                self.request.sendall(bytes([0x40, 2]) + packet_id)
            elif kind == 0xC0:
              with broker.lock:
                broker.pings += 1
              self.request.sendall(bytes([0xD0, 0]))
            elif kind == 0xE0:
              return
        except (ConnectionError, OSError):
          return

    class Server(socketserver.ThreadingTCPServer):
      allow_reuse_address = True
      daemon_threads = True

    self.server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=self.server.serve_forever, daemon=True).start()
    self.port = self.server.server_address[1]

  def cfg(self, password="pw"):
    return {"host": "127.0.0.1", "port": self.port, "username": "ha", "password": password}

  def topics(self):
    with self.lock:
      return [t for t, _, _ in self.publishes]

  def last(self, topic):
    with self.lock:
      return next((json.loads(p) if p[:1] == b"{" else p.decode() for t, p, _ in reversed(self.publishes) if t == topic), None)

  def drop_connections(self):
    with self.lock:
      conns, self.conns = self.conns, []
    for c in conns:
      try:
        c.shutdown(socket.SHUT_RDWR)
      except OSError:
        pass
      c.close()

  def close(self):
    self.server.shutdown()



@pytest.fixture
def broker():
  b = _FakeBroker()
  yield b
  b.close()


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
  monkeypatch.setattr(ha_pushd, "RETRY_MIN_S", 0.05)
  monkeypatch.setattr(ha_pushd, "RETRY_MAX_S", 0.2)
  monkeypatch.setattr(ha_pushd, "CONNECT_TIMEOUT_S", 2.0)
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


def test_sender_connects_with_auth_and_will_then_publishes_discovery_and_state(broker):
  sender = Sender(broker.cfg())
  sender.start()
  sender.submit(_snapshot())
  assert _wait(lambda: broker.last("car/state") is not None)
  con = broker.connects[0]
  assert con["username"] == "ha" and con["password"] == "pw" and con["client_id"] == "comma3-car"
  assert con["will"] == ("car/availability", b"offline", True) and con["keepalive"] == ha_pushd.KEEPALIVE_S
  topics = broker.topics()
  configs = [t for t in topics if t.startswith("homeassistant/")]
  assert configs == [f"homeassistant/{c}/car/{o}/config" for c, o, _ in ha_pushd.DISCOVERY]
  assert topics.index("car/availability") < topics.index("car/location") < topics.index("car/state")
  assert all(retain for _, _, retain in broker.publishes)
  assert sender.client.packet_id == len(broker.publishes)          # every publish was QoS 1 and acknowledged
  tracker_cfg = broker.last("homeassistant/device_tracker/car/tracker/config")
  assert tracker_cfg["json_attributes_topic"] == "car/location" and tracker_cfg["device"]["identifiers"] == ["comma3_car"]
  assert broker.last("car/location")["latitude"] == 43.65
  assert broker.last("car/state") == {"speed_kmh": 72.0, "drive_state": "driving", "gear": "drive", "ignition": True, "fix_age_s": 0.5}
  assert broker.last("car/availability") == "online"
  assert not _log.warnings

  sender.submit(_snapshot(speed_ms=0.0, speed_kmh=0.0, fix=None, drive_state="parked", gear="park"))
  assert _wait(lambda: broker.last("car/state")["drive_state"] == "parked")
  assert broker.topics().count("car/location") == 1              # no location without a fix, and no re-discovery


def test_sender_reconnects_after_a_dropped_link_and_republishes_last_state(broker):
  sender = Sender(broker.cfg())
  sender.start()
  sender.submit(_snapshot())
  assert _wait(lambda: broker.last("car/state") is not None)
  n_pub = len(broker.publishes)

  broker.drop_connections()
  sender.submit(_snapshot(speed_ms=10.0, speed_kmh=36.0, fix=None))   # arrives on a dead socket
  assert _wait(lambda: len(broker.connects) >= 2 and broker.last("car/state")["speed_kmh"] == 36.0)
  assert len(_log.warnings) == 1 and "session lost" in _log.warnings[0]
  assert _wait(lambda: len(_log.infos) >= 1 and "recovered" in _log.infos[-1])
  assert not sender.failing and sender.retry_s == pytest.approx(0.05)
  new = broker.publishes[n_pub:]
  assert [t for t, _, _ in new if t.startswith("homeassistant/")]     # discovery re-published on reconnect
  assert broker.last("car/location")["latitude"] == 43.65             # last snapshot's fix carried over


def test_sender_backs_off_and_logs_once_when_the_broker_refuses(broker):
  sender = Sender(broker.cfg(password="wrong"))
  sender.start()
  sender.submit(_snapshot())
  assert _wait(lambda: sender.failures >= 3)
  assert len(_log.warnings) == 1 and "not authorized" in _log.warnings[0]
  assert sender.retry_s == pytest.approx(0.2)
  assert broker.publishes == []
  sender.cfg["password"] = "pw"
  sender.client.password = "pw"
  assert _wait(lambda: broker.last("car/state") is not None)
  assert len(_log.warnings) == 1 and len(_log.infos) == 1


def test_sender_pings_when_idle(broker, monkeypatch):
  monkeypatch.setattr(ha_pushd, "PING_IDLE_S", 0.1)
  sender = Sender(broker.cfg())
  sender.start()
  sender.submit(_snapshot())
  assert _wait(lambda: broker.pings >= 2, timeout=2.0)
  assert not sender.failing


def test_newer_snapshot_supersedes_a_pending_one_and_cuts_the_backoff_short(broker):
  sender = Sender(broker.cfg(password="wrong"))
  sender.start()
  sender.submit(_snapshot(drive_state="driving"))
  assert _wait(lambda: sender.failures >= 2)
  sender.cfg["password"] = "pw"
  sender.client.password = "pw"
  t0 = time.monotonic()
  sender.submit(_snapshot(drive_state="parked", gear="park", speed_ms=0.0, speed_kmh=0.0))
  assert _wait(lambda: broker.last("car/state") is not None)
  assert time.monotonic() - t0 < 0.15
  assert [json.loads(p)["drive_state"] for t, p, _ in broker.publishes if t == "car/state"] == ["parked"]
