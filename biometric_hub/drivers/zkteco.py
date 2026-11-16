"""ZKTeco / eSSL readers over the LAN, port 4370.

The protocol handling is `pyzk`, the same library upstream BAST uses — there is
no sense rediscovering a decade of device quirks. What is different is what this
driver deliberately does *not* do, and both are BAST behaviours we chose against
(PRD §7.2):

* **It never disables the device to read from it.** BAST calls
  `disable_device()` around every fetch, which takes the reader off the door for
  the duration. On a device with a large backlog that is a real outage for real
  people standing in front of it.
* **It never clears attendance from the device.** Upstream's own comment calls
  that a data-loss risk. Device memory is managed by retention, not by a
  destructive read.

`pyzk` is an optional dependency. It is only needed for `Direct Poll` mode,
where the *server* reaches the reader; in the far more common `Via Connector`
mode the agent holds it instead. If it is missing, this driver says so plainly
rather than failing somewhere confusing.
"""

from datetime import datetime

from biometric_hub.drivers.base import BaseDriver, RawPunch

DEFAULT_PORT = 4370
DEFAULT_TIMEOUT = 30


class ZKTecoDriver(BaseDriver):
	driver_name = "ZKTeco"
	driver_version = "1.0.0"
	supports_pull = True
	supports_clock_sync = True

	# -------------------------------------------------------------- plumbing

	def _zk(self):
		try:
			from zk import ZK
		except ImportError:
			raise RuntimeError(
				"pyzk is not installed in this environment. It is only required for "
				"'Direct Poll' devices, where the server reaches the reader itself. "
				"Install it with: ./env/bin/pip install pyzk"
			)
		return ZK

	def connect(self, device):
		"""Overridden in tests to hand back a recorded conversation."""
		ZK = self._zk()
		zk = ZK(
			device.ip_address,
			port=int(device.port or DEFAULT_PORT),
			timeout=DEFAULT_TIMEOUT,
			password=int(device.get_password("comm_key") or 0) if device.get("comm_key") else 0,
		)
		return zk.connect()

	# ---------------------------------------------------------------- driver

	def test_connection(self, device):
		conn = None
		try:
			conn = self.connect(device)
			serial = str(conn.get_serialnumber() or "").strip()
			if device.serial_no and serial and serial != device.serial_no:
				return False, (
					f"Reached a device, but it reports serial {serial!r} and this record "
					f"says {device.serial_no!r}. Check the IP address."
				)
			return True, f"Connected. Serial {serial or 'unknown'}."
		except Exception as e:
			return False, f"{type(e).__name__}: {e}"
		finally:
			if conn:
				try:
					conn.disconnect()
				except Exception:
					pass

	def fetch_punches(self, device, since: datetime | None = None) -> list[RawPunch]:
		conn = None
		try:
			conn = self.connect(device)
			records = conn.get_attendance() or []
		finally:
			if conn:
				try:
					conn.disconnect()
				except Exception:
					pass

		punches = []
		for record in records:
			timestamp = getattr(record, "timestamp", None)
			if timestamp is None:
				continue
			if since and timestamp <= since:
				continue
			punches.append(
				RawPunch(
					device_serial=device.serial_no,
					device_user_id=str(getattr(record, "user_id", "")).strip(),
					punch_time=timestamp,
					raw_log_type=_punch_code(record),
					payload={
						"uid": getattr(record, "uid", None),
						"status": getattr(record, "status", None),
						"punch": getattr(record, "punch", None),
					},
				)
			)
		punches.sort(key=lambda p: p.punch_time)
		return punches

	def sync_clock(self, device) -> None:
		conn = None
		try:
			conn = self.connect(device)
			conn.set_time(datetime.now())
		finally:
			if conn:
				try:
					conn.disconnect()
				except Exception:
					pass


def _punch_code(record):
	"""The engine maps 0/4 to IN and 1/5 to OUT — the ZK-family convention. The
	driver passes the code through untouched; interpreting it is not its job."""
	code = getattr(record, "punch", None)
	return None if code is None else str(code)
