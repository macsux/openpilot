#!/usr/bin/env python3
"""Push the car's position and drive state to Home Assistant over MQTT.

One persistent MQTT session over Tailscale to the Mosquitto broker on the Mac (the one
HA's MQTT integration already uses); every publish is QoS 1, so a dead link shows up on
the next update rather than at the next keepalive. Entities are created by HA MQTT discovery under a
single "Car" device, so nothing is configured on the HA side. Config lives in
/data/ha_push.json rather than a param, because a new param key needs a rebuild of the
committed params_pyx.so, and it is never committed (public repo):

  {"host": "<Mac tailscale IP>", "port": 1883, "username": "ha", "password": "<mqtt_password from HA secrets.yaml>"}

Why MQTT and not the mobile_app webhook it replaced: every update is a ~100-byte PUBLISH
on an already-open socket instead of two HTTPS POSTs with headers, so the cellular cost
of a 15 s cadence drops by roughly an order of magnitude, and a dropped link is detected
by the keepalive rather than discovered on the next POST.

Topics (all retained, so HA still shows the last state after the car powers off):
  car/state         {"speed_kmh", "drive_state": driving|parked, "gear", "ignition", "fix_age_s"}
  car/location      {"latitude", "longitude", "gps_accuracy", "altitude", "course", "speed_ms"}
  car/availability  online | offline (offline is the broker-sent last will)
  homeassistant/<component>/car/<object_id>/config   discovery, published on every connect

Cadence: every PUSH_INTERVAL_S while onroad, and IMMEDIATELY on a drive <-> park change
or an ignition edge. Arriving somewhere means shifting to park and then switching the
car off, which takes the phone hotspot (and this device's power, depending on the
harness) with it, so the park transition goes out the moment the gear changes, from a
sender thread that owns the socket; the main loop never waits on the network.

Failure handling: on any socket or broker error the sender drops the session, backs off
2 s -> 60 s (logging once per failure streak), reconnects, re-publishes discovery and the
last snapshot. Only the monotonic clock is used: the device's wall clock is bogus at boot
and jumps when it syncs.

UI status: STATUS_PATH (tmpfs) exists and is re-touched every STATUS_REFRESH_S while the
session is up, and is removed while it is down (or when there is no config at all). The UI
reads its mtime and shows a red Home Assistant icon when the file is missing or stale, so a
crashed or hung daemon reads as offline too. A file rather than a param: a new param key
needs a rebuild of the committed params_pyx.so.
"""
import json
import os
import threading
import time

from cereal import car, messaging

from openpilot.common.gps import get_gps_location_service
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.starpilot.system.mqtt_min import MqttClient

CONFIG_PATH = "/data/ha_push.json"

PUSH_INTERVAL_S = 15.
OFFROAD_INTERVAL_S = 600.  # keep HA's "parked / ignition off" fresh while the device is still up
RETRY_MIN_S = 2.
RETRY_MAX_S = 60.
GPS_MAX_AGE_S = 5.         # a fix older than this is not sent as a fresh onroad position
KEEPALIVE_S = 600          # MQTT keepalive: the broker declares us dead (and sends the will) after 1.5x this
PING_IDLE_S = 240.         # send a PINGREQ when nothing has been published for this long
CONNECT_TIMEOUT_S = 10.
STATUS_PATH = "/dev/shm/ha_pushd_online"
STATUS_REFRESH_S = 10.
STATUS_STALE_S = 30.       # the UI treats an older status file as offline

CLIENT_ID = "comma3-car"
DISCOVERY_PREFIX = "homeassistant"
TOPIC_STATE = "car/state"
TOPIC_LOCATION = "car/location"
TOPIC_AVAILABILITY = "car/availability"

GearShifter = car.CarState.GearShifter

DEVICE = {"identifiers": ["comma3_car"], "name": "Car", "manufacturer": "comma", "model": "comma 3"}
# (component, object_id, config): one HA device, entity ids car / car_speed / car_drive_state / car_ignition / car_online
DISCOVERY = [
  ("device_tracker", "tracker", {
    "name": None, "unique_id": "car_tracker", "json_attributes_topic": TOPIC_LOCATION, "source_type": "gps", "icon": "mdi:car",
  }),
  ("sensor", "speed", {
    "name": "Speed", "unique_id": "car_speed", "state_topic": TOPIC_STATE, "value_template": "{{ value_json.speed_kmh }}",
    "unit_of_measurement": "km/h", "device_class": "speed", "state_class": "measurement", "icon": "mdi:speedometer",
  }),
  ("sensor", "drive_state", {
    "name": "Drive state", "unique_id": "car_drive_state", "state_topic": TOPIC_STATE, "value_template": "{{ value_json.drive_state }}",
    "json_attributes_topic": TOPIC_STATE, "json_attributes_template": "{{ {'gear': value_json.gear, 'fix_age_s': value_json.fix_age_s} | tojson }}",
    "icon": "mdi:car",
  }),
  ("binary_sensor", "ignition", {
    "name": "Ignition", "unique_id": "car_ignition", "state_topic": TOPIC_STATE,
    "value_template": "{{ 'ON' if value_json.ignition else 'OFF' }}", "device_class": "power", "icon": "mdi:engine",
  }),
  ("binary_sensor", "online", {
    "name": "Device online", "unique_id": "car_online", "state_topic": TOPIC_AVAILABILITY,
    "payload_on": "online", "payload_off": "offline", "device_class": "connectivity",
  }),
]


def load_config(path=CONFIG_PATH):
  try:
    with open(path) as f:
      cfg = json.load(f)
    return {"host": cfg["host"], "port": int(cfg.get("port", 1883)), "username": cfg.get("username"), "password": cfg.get("password")}
  except Exception:
    return None


def state_payload(s):
  return json.dumps({
    "speed_kmh": round(s["speed_kmh"], 1), "drive_state": s["drive_state"], "gear": s["gear"],
    "ignition": bool(s["started"]), "fix_age_s": s["fix_age_s"],
  }, separators=(",", ":"))


def location_payload(s):
  lat, lon, acc, alt, course = s["fix"]
  return json.dumps({
    "latitude": round(lat, 6), "longitude": round(lon, 6), "gps_accuracy": max(1, int(round(acc))),
    "altitude": round(alt, 1), "course": int(round(course)) % 360, "speed_ms": round(s["speed_ms"], 1),
  }, separators=(",", ":"))


class Sender(threading.Thread):
  """Owns the MQTT session. Latest-wins: each snapshot carries the full state, so a newer one
  always supersedes an unsent older one and a transition can never be lost behind it."""

  def __init__(self, cfg):
    super().__init__(daemon=True)
    self.cfg = cfg
    self.client = MqttClient(cfg["host"], cfg["port"], cfg["username"], cfg["password"], CLIENT_ID,
                             keepalive_s=KEEPALIVE_S, will=(TOPIC_AVAILABILITY, b"offline", True), timeout_s=CONNECT_TIMEOUT_S)
    self.cv = threading.Condition()
    self.pending = None
    self.last_sent = None
    self.failing = False
    self.failures = 0
    self.retry_s = RETRY_MIN_S

  def submit(self, snapshot):
    with self.cv:
      self.pending = snapshot
      self.cv.notify()

  def _connect(self):
    self.client.connect()
    for component, object_id, config in DISCOVERY:
      self.client.publish(f"{DISCOVERY_PREFIX}/{component}/car/{object_id}/config", json.dumps({**config, "device": DEVICE}))
    self.client.publish(TOPIC_AVAILABILITY, b"online")
    if self.last_sent is not None:
      self._publish(self.last_sent)

  def _publish(self, s):
    if s["fix"] is not None:
      self.client.publish(TOPIC_LOCATION, location_payload(s))
    self.client.publish(TOPIC_STATE, state_payload(s))

  def _take_pending(self, timeout):
    with self.cv:
      if self.pending is None:
        self.cv.wait(timeout)
      snapshot, self.pending = self.pending, None
    return snapshot

  def _restore_pending(self, snapshot):
    with self.cv:
      if self.pending is None:
        self.pending = snapshot

  def run(self):
    while True:
      snapshot = None
      try:
        if not self.client.connected:
          self._connect()
        snapshot = self._take_pending(max(0.5, PING_IDLE_S - self.client.idle_s()))
        if snapshot is not None:
          self._publish(snapshot)
          self.last_sent = snapshot
        elif self.client.idle_s() >= PING_IDLE_S:
          self.client.ping()
      except Exception as e:
        self.client.close(send_disconnect=False)
        if snapshot is not None:
          self._restore_pending(snapshot)
        if not self.failing:
          cloudlog.warning(f"ha_pushd: mqtt session lost ({e}); reconnecting, backing off up to {RETRY_MAX_S:.0f}s")
        self.failing = True
        self.failures += 1
        with self.cv:
          # a newer snapshot cuts the wait short; it is sent right after the reconnect
          if self.pending is None:
            self.cv.wait(self.retry_s)
        self.retry_s = min(self.retry_s * 2., RETRY_MAX_S)
        continue

      if self.failing:
        cloudlog.info(f"ha_pushd: mqtt session recovered after {self.failures} failed attempts")
      self.failing, self.failures, self.retry_s = False, 0, RETRY_MIN_S


class Tracker:
  """Turns the latest deviceState / carState / GPS readings into push snapshots.
  All times are monotonic seconds; a fix with fix_time None has an unknown age."""

  def __init__(self, seed=None):
    self.fix = seed            # (lat, lon, accuracy_m, altitude_m, course_deg)
    self.fix_time = None
    self.fix_sent_time = None
    self.fix_ever_sent = False
    self.gear = "unknown"
    self.drive_state = None
    self.started = None
    self.speed_ms = 0.
    self.last_push = None

  def set_fix(self, lat, lon, accuracy, altitude, course, now):
    self.fix = (lat, lon, accuracy, altitude, course)
    self.fix_time = now

  def update(self, now, started, gear, v_ego):
    """gear is the carState gear name, or None when carState is unavailable. Returns the snapshot to push or None."""
    if started:
      self.speed_ms = max(0., v_ego)
      if gear not in (None, "unknown"):
        self.gear = gear
    else:
      self.speed_ms = 0.

    # Ignition off is parked whatever the last gear read said.
    drive_state = "driving" if started and self.gear not in ("park", "unknown") else "parked"
    transition = started != self.started or drive_state != self.drive_state
    self.started, self.drive_state = started, drive_state

    interval = PUSH_INTERVAL_S if started else OFFROAD_INTERVAL_S
    if not transition and self.last_push is not None and now - self.last_push < interval:
      return None

    fix_age = now - self.fix_time if self.fix is not None and self.fix_time is not None else None
    # Onroad, only a fix we haven't already sent and that is still fresh; offroad the GPS is
    # off, so on the park/ignition-off transition the last fix IS where the car is parked. A
    # seeded fix of unknown age is sent once so a boot while parked still reports a position.
    fresh = fix_age is not None and fix_age <= GPS_MAX_AGE_S and self.fix_time != self.fix_sent_time
    send_fix = self.fix if self.fix is not None and (fresh or (not started and transition) or not self.fix_ever_sent) else None
    if send_fix is not None:
      self.fix_sent_time = self.fix_time
      self.fix_ever_sent = True
    self.last_push = now

    return {
      "started": bool(started),
      "drive_state": drive_state,
      "gear": self.gear,
      "speed_ms": self.speed_ms,
      "speed_kmh": self.speed_ms * 3.6,
      "fix": send_fix,
      "fix_age_s": round(fix_age, 1) if fix_age is not None else None,
      "transition": transition,
    }


class StatusFile:
  """Mirrors the session state into STATUS_PATH for the UI (see the module docstring)."""

  def __init__(self, path=STATUS_PATH):
    self.path = path
    self.online = None
    self.last_write = None

  def update(self, online, now):
    if online == self.online and (not online or now - self.last_write < STATUS_REFRESH_S):
      return
    try:
      if online:
        with open(self.path, "a"):
          pass
        os.utime(self.path)
      else:
        try:
          os.unlink(self.path)
        except FileNotFoundError:
          pass
    except OSError:
      return  # left as-is; retried on the next update
    self.online, self.last_write = online, now


def load_seed(params):
  """The fix StarPilot saves on every offroad transition; its age is unknown here (wall clock)."""
  try:
    last = json.loads(params.get("LastGPSPosition") or "{}")
    if last.get("latitude") is not None:
      return (float(last["latitude"]), float(last["longitude"]), 20., 0., float(last.get("bearing") or 0.))
  except Exception:
    pass
  return None


def main():
  params = Params()
  status = StatusFile()
  status.update(False, time.monotonic())  # a file left by a previous run says nothing about this one

  cfg = load_config()
  if cfg is None:
    cloudlog.warning(f"ha_pushd: no config at {CONFIG_PATH} ({{\"host\", \"port\", \"username\", \"password\"}}); nothing is pushed until it appears")
  while cfg is None:
    time.sleep(60)
    cfg = load_config()
  cloudlog.info(f"ha_pushd: publishing to mqtt://{cfg['host']}:{cfg['port']} as {cfg['username']}")

  sender = Sender(cfg)
  sender.start()

  gps_service = get_gps_location_service(params)
  # Paced by deviceState (2 Hz): every socket is conflated, so each wake reads
  # only the newest carState/GPS instead of parsing carState at 100 Hz. A gear
  # change is therefore seen within 0.5 s.
  sm = messaging.SubMaster(["deviceState", "carState", gps_service], poll="deviceState")
  tracker = Tracker(load_seed(params))

  while True:
    sm.update(1000)
    now = time.monotonic()

    if sm.updated[gps_service] and sm[gps_service].hasFix:
      g = sm[gps_service]
      tracker.set_fix(g.latitude, g.longitude, g.horizontalAccuracy, g.altitude, g.bearingDeg, sm.recv_time[gps_service])

    started = bool(sm["deviceState"].started) if sm.seen["deviceState"] else False
    gear = str(sm["carState"].gearShifter) if sm.seen["carState"] else None
    v_ego = float(sm["carState"].vEgo) if sm.seen["carState"] else 0.

    status.update(sender.client.connected, now)

    snapshot = tracker.update(now, started, gear, v_ego)
    if snapshot is not None:
      sender.submit(snapshot)
      if snapshot["transition"]:
        cloudlog.info(f"ha_pushd: {'onroad' if started else 'offroad'}, {snapshot['drive_state']} ({snapshot['gear']}) — pushed immediately")


if __name__ == "__main__":
  main()
