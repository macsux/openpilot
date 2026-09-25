"""Minimal MQTT 3.1.1 publish-only client: one TCP socket, username/password, keepalive,
last-will, retained QoS 1 publishes. No third-party dependency, because the device's venv
sits on the read-only AGNOS root and paho isn't in it.

Synchronous by design: a publish-only session never receives anything unsolicited, so
the owning thread can send, and read the CONNACK / PUBACK / PINGRESP replies inline.
QoS 1 rather than 0 on purpose: a publish-only QoS 0 client never reads, so a link that
died underneath it is only noticed at the next keepalive ping (the first send after the
peer closed still succeeds into the kernel buffer). Waiting for the PUBACK turns every
publish into a liveness check, so a dead hotspot is caught on the very next update."""
import socket
import struct
import time

CONNECT, CONNACK, PUBLISH, PUBACK, PINGREQ, PINGRESP, DISCONNECT = 0x10, 0x20, 0x30, 0x40, 0xC0, 0xD0, 0xE0
CONNACK_ERRORS = {
  1: "unacceptable protocol version", 2: "identifier rejected", 3: "server unavailable",
  4: "bad user name or password", 5: "not authorized",
}


class MqttError(Exception):
  pass


def encode_string(s: str) -> bytes:
  b = s.encode()
  return struct.pack("!H", len(b)) + b


def encode_bytes(b: bytes) -> bytes:
  return struct.pack("!H", len(b)) + b


def encode_length(n: int) -> bytes:
  out = b""
  while True:
    digit, n = n % 128, n // 128
    out += bytes([digit | (0x80 if n else 0)])
    if not n:
      return out


class MqttClient:
  def __init__(self, host: str, port: int, username: str | None, password: str | None, client_id: str, *,
               keepalive_s: int = 600, will: tuple[str, bytes, bool] | None = None, timeout_s: float = 10.0):
    self.host, self.port = host, port
    self.username, self.password = username, password
    self.client_id = client_id
    self.keepalive_s = keepalive_s
    self.will = will  # (topic, payload, retain)
    self.timeout_s = timeout_s
    self.sock: socket.socket | None = None
    self.last_tx = 0.0
    self.packet_id = 0

  @property
  def connected(self) -> bool:
    return self.sock is not None

  def connect(self) -> None:
    sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
    sock.settimeout(self.timeout_s)
    self.sock = sock
    flags = 0x02  # clean session
    payload = encode_string(self.client_id)
    if self.will is not None:
      topic, message, retain = self.will
      flags |= 0x04 | (0x20 if retain else 0)
      payload += encode_string(topic) + encode_bytes(message)
    if self.username is not None:
      flags |= 0x80
      payload += encode_string(self.username)
    if self.password is not None:
      flags |= 0x40
      payload += encode_bytes(self.password.encode())
    variable = encode_string("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive_s)
    try:
      self._send(bytes([CONNECT]) + encode_length(len(variable) + len(payload)) + variable + payload)
      ptype, body = self._read_packet()
    except Exception:
      self.close(send_disconnect=False)
      raise
    if ptype != CONNACK or len(body) < 2:
      self.close(send_disconnect=False)
      raise MqttError("expected CONNACK")
    if body[1] != 0:
      self.close(send_disconnect=False)
      raise MqttError(f"connection refused: {CONNACK_ERRORS.get(body[1], body[1])}")

  def publish(self, topic: str, payload: bytes | str, retain: bool = True, qos: int = 1) -> None:
    if isinstance(payload, str):
      payload = payload.encode()
    variable = encode_string(topic)
    packet_id = None
    if qos:
      self.packet_id = self.packet_id % 65535 + 1
      packet_id = self.packet_id
      variable += struct.pack("!H", packet_id)
    self._send(bytes([PUBLISH | (qos << 1) | (0x01 if retain else 0)]) + encode_length(len(variable) + len(payload)) + variable + payload)
    if qos:
      ptype, body = self._read_packet()
      if ptype != PUBACK or len(body) < 2 or struct.unpack("!H", body[:2])[0] != packet_id:
        raise MqttError("expected PUBACK")

  def ping(self) -> None:
    self._send(bytes([PINGREQ, 0]))
    ptype, _ = self._read_packet()
    if ptype != PINGRESP:
      raise MqttError("expected PINGRESP")

  def idle_s(self) -> float:
    return time.monotonic() - self.last_tx

  def close(self, send_disconnect: bool = True) -> None:
    sock, self.sock = self.sock, None
    if sock is None:
      return
    try:
      if send_disconnect:
        sock.sendall(bytes([DISCONNECT, 0]))
    except OSError:
      pass
    try:
      sock.close()
    except OSError:
      pass

  def _send(self, data: bytes) -> None:
    if self.sock is None:
      raise MqttError("not connected")
    try:
      self.sock.sendall(data)
    except OSError as e:
      raise MqttError(f"send failed: {e}") from e
    self.last_tx = time.monotonic()

  def _recv_exact(self, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
      try:
        chunk = self.sock.recv(n - len(buf))
      except OSError as e:
        raise MqttError(f"recv failed: {e}") from e
      if not chunk:
        raise MqttError("connection closed by broker")
      buf += chunk
    return buf

  def _read_packet(self) -> tuple[int, bytes]:
    ptype = self._recv_exact(1)[0] & 0xF0
    length, multiplier = 0, 1
    while True:
      digit = self._recv_exact(1)[0]
      length += (digit & 0x7F) * multiplier
      multiplier *= 128
      if not digit & 0x80:
        break
    return ptype, (self._recv_exact(length) if length else b"")
