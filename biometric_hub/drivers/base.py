"""Driver interface.

A driver converts one vendor's protocol into `RawPunch` objects and nothing
else. **A driver never touches the database** — that is what keeps vendor quirks
out of the engine and lets drivers be tested without hardware.

The full interface (clock sync, enrolment push, push parsing) lands with the
first real driver in Phase 5. What is here is what Phase 2 needs.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawPunch:
	"""One punch, exactly as the device reported it. No interpretation."""

	device_serial: str
	device_user_id: str
	punch_time: datetime
	"""In the device's own timezone. The engine normalises; the driver does not."""

	raw_log_type: str | None = None
	"""Whatever the device said — 'IN', 'OUT', a ZK punch code, or nothing."""

	latitude: float | None = None
	longitude: float | None = None
	payload: dict = field(default_factory=dict)
	"""The untouched source record, kept for audit."""


class BaseDriver:
	driver_name: str = ""
	driver_version: str = "0.0.1"

	supports_push = False
	supports_pull = False
	supports_realtime = False
	supports_clock_sync = False
	supports_enroll = False

	def test_connection(self, device) -> tuple[bool, str]:
		"""Reach the device and say something useful about it. Never raises."""
		raise NotImplementedError

	def fetch_punches(self, device, since: datetime | None = None) -> list[RawPunch]:
		"""Pull mode. Return every punch after `since`, oldest first."""
		raise NotImplementedError

	def parse_push(self, device, request) -> list[RawPunch]:
		"""Push mode. Turn one inbound request body into punches."""
		raise NotImplementedError

	def sync_clock(self, device) -> None:
		"""Set the device clock to server time. Only where `supports_clock_sync`."""
		raise NotImplementedError

	def push_enrolment(self, device, enrolments) -> None:
		"""Write user records onto the device. Only where `supports_enroll`."""
		raise NotImplementedError

	def capabilities(self) -> dict:
		return {
			"supports_push": int(self.supports_push),
			"supports_pull": int(self.supports_pull),
			"supports_realtime": int(self.supports_realtime),
			"supports_clock_sync": int(self.supports_clock_sync),
			"supports_enroll": int(self.supports_enroll),
		}
