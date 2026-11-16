"""A driver with no hardware behind it.

Exists so the engine, the API and the workbench can be exercised end to end on
a bench with no reader on the network. Never registered on a customer site.
"""

from datetime import datetime

from biometric_hub.drivers.base import BaseDriver, RawPunch


class FakeDriver(BaseDriver):
	driver_name = "Fake"
	driver_version = "1.0.0"
	supports_pull = True
	supports_push = True

	def __init__(self, punches: list[RawPunch] | None = None):
		self.punches = punches or []

	def test_connection(self, device) -> tuple[bool, str]:
		return True, "Fake driver: always reachable."

	def fetch_punches(self, device, since: datetime | None = None) -> list[RawPunch]:
		if since is None:
			return list(self.punches)
		return [p for p in self.punches if p.punch_time > since]
