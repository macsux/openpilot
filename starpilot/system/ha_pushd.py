#!/usr/bin/env python3
"""Push the car's position and drive state to Home Assistant.

The device is registered in HA as a mobile_app device ("Car"), so every push is
an unauthenticated POST to /api/webhook/<webhook_id> — the webhook id is the
secret. Config lives in /data/ha_push.json rather than a param, because a new
param key needs a rebuild of the committed params_pyx.so:

  {"url": "https://ha.almirex.com", "webhook_id": "<id from registration>"}

What is sent:
  * device_tracker.car          — lat/lon, accuracy, speed (m/s: what update_location takes), course, altitude
  * sensor.car_speed            — km/h
  * sensor.car_drive_state      — driving | parked
  * binary_sensor.car_ignition  — deviceState.started

Cadence: every PUSH_INTERVAL_S while onroad, and IMMEDIATELY on a drive <-> park
change or an ignition edge. Arriving somewhere means shifting to park and then
switching the car off, which takes the phone hotspot (and this device's power,
depending on the harness) with it — so the park transition is sent the moment
the gear changes, from a sender thread that retries with backoff, and the main
loop never waits on the network.

Failure handling: HA answers 200 with an EMPTY body for a webhook id it doesn't
know, so a bad config would otherwise push into the void forever. The sender
therefore asks for get_config first (a registered device answers with JSON) and
refuses to run until that succeeds. Per-sensor errors come back inside a 200 as
well; a sensor HA no longer knows (device deleted and re-added) is re-registered.
Retries back off 2 s -> 60 s and log once per failure streak, not per attempt:
parked in a garage with no hotspot is the normal state until the device powers down.

Only the monotonic clock is used for timing. The device's wall clock is bogus at
boot and jumps when it syncs, which would make a fresh fix look ancient (never
sent) or a stale one look fresh.
"""
import json
import threading
import time

import requests

from cereal import car, messaging

from openpilot.common.gps import get_gps_location_service
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog

CONFIG_PATH = "/data/ha_push.json"

PUSH_INTERVAL_S = 15.
OFFROAD_INTERVAL_S = 600.  # keep HA's "parked / ignition off" fresh while the device is still up
RETRY_MIN_S = 2.
RETRY_MAX_S = 60.
GPS_MAX_AGE_S = 5.         # a fix older than this is not sent as a fresh onroad position
HTTP_TIMEOUT = (3., 5.)    # connect, read
MOVING_MIN_SPEED = 1.0     # m/s; update_location's speed/course are only meaningful (and only accepted > 0) when moving

GearShifter = car.CarState.GearShifter

# Registered with concrete initial states: a null state is the one thing that could make
# registration fail on every start and leave the daemon stuck before its first push.
SENSORS = [
  {"type": "sensor", "unique_id": "car_speed", "name": "Speed", "icon": "mdi:speedometer",
   "device_class": "speed", "unit_of_measurement": "km/h", "state_class": "measurement", "state": 0},
  {"type": "sensor", "unique_id": "car_drive_state", "name": "Drive state", "icon": "mdi:car", "state": "parked"},
  {"type": "binary_sensor", "unique_id": "car_ignition", "name": "Ignition", "icon": "mdi:engine",
   "device_class": "power", "state": False},
]


def load_config(path=CONFIG_PATH):
  try:
    with open(path) as f:
      cfg = json.load(f)
    return f"{cfg['url'].rstrip('/')}/api/webhook/{cfg['webhook_id']}"
  except Exception:
    return None


def location_payload(s):
  lat, lon, acc, alt, course = s["fix"]
  data = {"gps": [lat, lon], "gps_accuracy": max(1, int(round(acc)))}
  if s["speed_ms"] >= MOVING_MIN_SPEED:
    data["speed"] = int(round(s["speed_ms"]))
    data["course"] = int(round(course)) % 360
  if alt > 0.:
    data["altitude"] = int(round(alt))
  return data


def sensor_states(s):
  return [
    {"type": "sensor", "unique_id": "car_speed", "state": round(s["speed_kmh"], 1), "icon": "mdi:speedometer"},
    {"type": "sensor", "unique_id": "car_drive_state", "state": s["drive_state"],
     "icon": "mdi:car-arrow-right" if s["drive_state"] == "driving" else "mdi:car-brake-parking",
     "attributes": {"gear": s["gear"], "fix_age_s": s["fix_age_s"]}},
    {"type": "binary_sensor", "unique_id": "car_ignition", "state": s["started"], "icon": "mdi:engine"},
  ]


class Sender(threading.Thread):
  """Latest-wins: each snapshot carries the full state, so a newer one always
  supersedes an unsent older one and a transition can never be lost behind it."""

  def __init__(self, url):
    super().__init__(daemon=True)
    self.url = url
    self.session = requests.Session()
    self.cv = threading.Condition()
    self.pending = None
    self.registered = False
    self.failing = False
    self.failures = 0
    self.retry_s = RETRY_MIN_S

  def submit(self, snapshot):
    with self.cv:
      self.pending = snapshot
      self.cv.notify()

  def _post(self, body):
    r = self.session.post(self.url, json=body, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r

  @staticmethod
  def _json(r):
    try:
      return r.json()
    except ValueError:
      return None

  def _register(self):
    cfg = self._json(self._post({"type": "get_config", "data": {}}))
    if not isinstance(cfg, dict) or not cfg:
      raise RuntimeError("webhook id is not registered in Home Assistant (empty get_config reply)")
    for sensor in SENSORS:
      self._post({"type": "register_sensor", "data": sensor})
    self.registered = True

  def _send(self, s):
    if not self.registered:
      self._register()

    if s["fix"] is not None:
      self._post({"type": "update_location", "data": location_payload(s)})

    results = self._json(self._post({"type": "update_sensor_states", "data": sensor_states(s)}))
    if isinstance(results, dict):
      rejected = {k: v for k, v in results.items() if isinstance(v, dict) and v.get("success") is False}
      if rejected:
        self.registered = False
        raise RuntimeError(f"sensor update rejected, re-registering: {rejected}")

  def run(self):
    while True:
      with self.cv:
        while self.pending is None:
          self.cv.wait()
        snapshot = self.pending

      try:
        self._send(snapshot)
      except Exception as e:
        if not self.failing:
          cloudlog.warning(f"ha_pushd: push failed ({e}); retrying, backing off up to {RETRY_MAX_S:.0f}s")
        self.failing = True
        self.failures += 1
        with self.cv:
          # a newer snapshot cuts the wait short; otherwise retry this one after the backoff
          if self.pending is snapshot:
            self.cv.wait(self.retry_s)
        self.retry_s = min(self.retry_s * 2., RETRY_MAX_S)
        continue

      if self.failing:
        cloudlog.info(f"ha_pushd: push recovered after {self.failures} failed attempts")
      self.failing, self.failures, self.retry_s = False, 0, RETRY_MIN_S
      with self.cv:
        if self.pending is snapshot:
          self.pending = None


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

  url = load_config()
  while url is None:
    time.sleep(60)
    url = load_config()

  sender = Sender(url)
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

    snapshot = tracker.update(now, started, gear, v_ego)
    if snapshot is not None:
      sender.submit(snapshot)
      if snapshot["transition"]:
        cloudlog.info(f"ha_pushd: {'onroad' if started else 'offroad'}, {snapshot['drive_state']} ({snapshot['gear']}) — pushed immediately")


if __name__ == "__main__":
  main()
