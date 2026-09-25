"""Red Home Assistant icon on the right screen edge while ha_pushd's MQTT session is down.

Nothing is drawn while it is up. State comes from ha_pushd's status file (see the
ha_pushd module docstring): missing, or not re-touched within STATUS_STALE_S = offline,
so a daemon that crashed or hung shows as offline too.
"""
import os
import time

import pyray as rl

from openpilot.starpilot.system.ha_pushd import STATUS_PATH, STATUS_STALE_S
from openpilot.system.ui.lib.application import gui_app

ICON_PATH = "icons/home_assistant_offline.png"
ICON_SIZE = 72
EDGE_MARGIN = 48  # clear of the onroad border
POLL_INTERVAL_S = 1.


class HaStatusReader:
  """Online while the status file exists and its mtime keeps changing. The change is timed on
  the monotonic clock, so the device's wall clock (bogus at boot, jumps on sync) never matters."""

  def __init__(self, path=STATUS_PATH):
    self.path = path
    self._mtime = None
    self._changed_at = None

  def online(self, now: float) -> bool:
    try:
      mtime = os.stat(self.path).st_mtime_ns
    except OSError:
      self._mtime = None
      return False
    if mtime != self._mtime:
      self._mtime, self._changed_at = mtime, now
    return now - self._changed_at <= STATUS_STALE_S


class HaOfflineIndicator:
  def __init__(self):
    self._reader = HaStatusReader()
    self._online = True
    self._next_poll = 0.

  def render(self, rect: rl.Rectangle):
    now = time.monotonic()
    if now >= self._next_poll:
      self._online = self._reader.online(now)
      self._next_poll = now + POLL_INTERVAL_S
    if self._online:
      return
    icon = gui_app.texture(ICON_PATH, ICON_SIZE, ICON_SIZE)
    x = rect.x + rect.width - EDGE_MARGIN - ICON_SIZE
    y = rect.y + (rect.height - ICON_SIZE) / 2
    rl.draw_texture_v(icon, rl.Vector2(x, y), rl.WHITE)
