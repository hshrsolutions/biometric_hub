"""Phase 5 acceptance: the driver framework and the ZKTeco driver.

	bench --site <site> execute biometric_hub.tests.test_drivers.run

No hardware. The ZKTeco driver is exercised against a recorded conversation, so
these tests run identically on a laptop and on a bench with a reader on the LAN.
"""

from datetime import datetime, timedelta

import frappe

from biometric_hub.drivers import registry
from biometric_hub.drivers.zkteco import ZKTecoDriver

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


# --------------------------------------------------- a recorded ZK conversation


class _Record:
	"""One row as pyzk hands it back."""

	def __init__(self, uid, user_id, timestamp, status=1, punch=0):
		self.uid = uid
		self.user_id = user_id
		self.timestamp = timestamp
		self.status = status
		self.punch = punch


class _RecordedConnection:
	"""Stands in for a live reader, and remembers what was done to it — which is
	how we assert the two things this driver must never do."""

	def __init__(self, records, serial="ZK-REC-001"):
		self.records = records
		self.serial = serial
		self.calls = []

	def get_attendance(self):
		self.calls.append("get_attendance")
		return self.records

	def get_serialnumber(self):
		self.calls.append("get_serialnumber")
		return self.serial

	def disable_device(self):
		self.calls.append("disable_device")

	def enable_device(self):
		self.calls.append("enable_device")

	def clear_attendance(self):
		self.calls.append("clear_attendance")

	def set_time(self, when):
		self.calls.append("set_time")

	def disconnect(self):
		self.calls.append("disconnect")


class _RecordedDriver(ZKTecoDriver):
	def __init__(self, connection):
		self.connection = connection

	def connect(self, device):
		return self.connection


BASE = datetime(2026, 8, 31, 9, 0, 0)
RECORDED = [
	_Record(1, "1434", BASE, punch=0),
	_Record(2, "1530", BASE + timedelta(minutes=5), punch=1),
	_Record(3, "137", BASE + timedelta(hours=8), punch=4),
	_Record(4, "1434", BASE + timedelta(hours=9), punch=5),
]


def _device():
	return frappe._dict(serial_no="ZK-REC-001", ip_address="192.0.2.10", port=4370, comm_key=None)


# ---------------------------------------------------------------------- tests


def test_registry_sync():
	print("Registry:")
	# A driver row for something no app declares any more.
	stray = "BHTEST Retired Driver"
	if not frappe.db.exists("Biometric Driver", stray):
		frappe.get_doc(
			{"doctype": "Biometric Driver", "driver_name": stray, "enabled": 1, "python_path": "gone.Gone"}
		).insert(ignore_permissions=True)

	try:
		names = registry.sync()
		check("the hook's drivers are registered", {"Fake", "ZKTeco"} <= set(names), names)
		check("ZKTeco is enabled", frappe.db.get_value("Biometric Driver", "ZKTeco", "enabled") == 1)
		check(
			"capabilities came from the class",
			frappe.db.get_value("Biometric Driver", "ZKTeco", "supports_pull") == 1
			and frappe.db.get_value("Biometric Driver", "ZKTeco", "supports_push") == 0,
		)
		check(
			"the python path is recorded",
			frappe.db.get_value("Biometric Driver", "ZKTeco", "python_path")
			== "biometric_hub.drivers.zkteco.ZKTecoDriver",
		)
		check(
			"a driver no app declares is disabled, not deleted",
			frappe.db.exists("Biometric Driver", stray)
			and frappe.db.get_value("Biometric Driver", stray, "enabled") == 0,
		)
	finally:
		frappe.delete_doc("Biometric Driver", stray, force=True, ignore_permissions=True)
		frappe.db.commit()


def test_get_driver():
	print("Lookup:")
	driver = registry.get_driver("ZKTeco")
	check("get_driver returns the class from the registry", isinstance(driver, ZKTecoDriver))
	try:
		registry.get_driver("No Such Driver")
		check("an unknown driver is refused", False)
	except Exception as e:
		check("an unknown driver is refused", "Unknown driver" in str(e), str(e)[:80])


def test_fetch_parses_the_recording():
	print("Fetch:")
	conn = _RecordedConnection(RECORDED)
	punches = _RecordedDriver(conn).fetch_punches(_device())

	check("every record became a punch", len(punches) == 4, len(punches))
	check("device user IDs come through as strings", punches[0].device_user_id == "1434", punches[0].device_user_id)
	check("the serial is stamped from the device record", all(p.device_serial == "ZK-REC-001" for p in punches))
	check("punch codes are passed through untouched", [p.raw_log_type for p in punches] == ["0", "1", "4", "5"],
	      [p.raw_log_type for p in punches])
	check("punches come back oldest first", punches == sorted(punches, key=lambda p: p.punch_time))
	check("the raw record is kept for audit", punches[0].payload.get("uid") == 1, punches[0].payload)
	check("the connection is closed", "disconnect" in conn.calls, conn.calls)


def test_since_filter():
	print("Incremental:")
	conn = _RecordedConnection(RECORDED)
	punches = _RecordedDriver(conn).fetch_punches(_device(), since=BASE + timedelta(minutes=5))
	check("only punches after the cursor come back", len(punches) == 2, [str(p.punch_time) for p in punches])
	check("the boundary punch is excluded", all(p.punch_time > BASE + timedelta(minutes=5) for p in punches))


def test_never_disables_or_clears():
	"""The two BAST behaviours we deliberately did not carry over (PRD §7.2,
	items 8 and 9). This is the test that keeps them from creeping back."""
	print("What it must never do:")
	conn = _RecordedConnection(RECORDED)
	_RecordedDriver(conn).fetch_punches(_device())
	check("the reader is never taken off the door to read it", "disable_device" not in conn.calls, conn.calls)
	check("attendance is never cleared from the device", "clear_attendance" not in conn.calls, conn.calls)


def test_connection_check():
	print("Test connection:")
	ok, message = _RecordedDriver(_RecordedConnection(RECORDED)).test_connection(_device())
	check("a reachable device reports OK", ok, message)
	check("and names the serial", "ZK-REC-001" in message, message)

	mismatched = _RecordedConnection(RECORDED, serial="SOMETHING-ELSE")
	ok, message = _RecordedDriver(mismatched).test_connection(_device())
	check("a serial mismatch is caught", not ok, message)
	check("and the message says what to check", "IP address" in message, message)

	class _Dead(_RecordedConnection):
		def get_serialnumber(self):
			raise OSError("timed out")

	ok, message = _RecordedDriver(_Dead(RECORDED)).test_connection(_device())
	check("an unreachable device never raises, it reports", not ok and "timed out" in message, message)


def run():
	print("Biometric Hub — drivers (Phase 5)\n")
	test_registry_sync()
	test_get_driver()
	test_fetch_parses_the_recording()
	test_since_filter()
	test_never_disables_or_clears()
	test_connection_check()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_drivers: {len(failed)} check(s) failed")
	print("All good.")
