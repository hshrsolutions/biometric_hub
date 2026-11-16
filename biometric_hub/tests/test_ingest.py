"""Phase 2 acceptance tests for engine/ingest.py.

	bench --site <site> execute biometric_hub.tests.test_ingest.run

Creates its own device, driver and enrolments, and deletes every row it made —
including the Employee Checkins — on the way out. Existing employees are read,
never written.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from biometric_hub.drivers.base import RawPunch
from biometric_hub.engine.ingest import ingest

PREFIX = "BHTEST"
DEVICE = f"{PREFIX} Device"
SERIAL = f"{PREFIX}-SN-1"
DRIVER = "Fake"

ID_OK = f"{PREFIX}-OK"
ID_LEFT = f"{PREFIX}-LEFT"
ID_UNKNOWN = f"{PREFIX}-NOBODY"

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


# ------------------------------------------------------------------- fixtures


def _employees():
	active = frappe.get_all("Employee", filters={"status": "Active"}, pluck="name", limit=1)
	left = frappe.get_all("Employee", filters={"status": ["!=", "Active"]}, pluck="name", limit=1)
	if not active:
		raise Exception("no active Employee on this site to test against")
	return active[0], (left[0] if left else None)


def setup(active, left):
	if not frappe.db.exists("Biometric Driver", DRIVER):
		frappe.get_doc(
			{
				"doctype": "Biometric Driver",
				"driver_name": DRIVER,
				"source_app": "biometric_hub",
				"python_path": "biometric_hub.drivers.fake.FakeDriver",
				"enabled": 1,
				"supports_pull": 1,
				"supports_push": 1,
				"driver_version": "1.0.0",
			}
		).insert(ignore_permissions=True)

	frappe.get_doc(
		{
			"doctype": "Biometric Device",
			"device_name": DEVICE,
			"enabled": 1,
			"driver": DRIVER,
			"connectivity_mode": "Push",
			"serial_no": SERIAL,
			"location": "Selftest",
			# HRMS requires these once "Allow Geolocation Tracking" is on.
			"latitude": 9.9252,
			"longitude": 78.1198,
		}
	).insert(ignore_permissions=True)

	frappe.get_doc(
		{"doctype": "Biometric Enrolment", "employee": active, "device_user_id": ID_OK, "enabled": 1}
	).insert(ignore_permissions=True)

	if left:
		frappe.get_doc(
			{"doctype": "Biometric Enrolment", "employee": left, "device_user_id": ID_LEFT, "enabled": 1}
		).insert(ignore_permissions=True)
	frappe.db.commit()


def teardown():
	for punch in frappe.get_all("Biometric Punch", filters={"device_serial": ["like", f"{PREFIX}%"]}, pluck="name"):
		checkin = frappe.db.get_value("Biometric Punch", punch, "employee_checkin")
		frappe.delete_doc("Biometric Punch", punch, force=True, ignore_permissions=True, delete_permanently=True)
		if checkin and frappe.db.exists("Employee Checkin", checkin):
			frappe.delete_doc("Employee Checkin", checkin, force=True, ignore_permissions=True, delete_permanently=True)
	for dt, filters in (
		("Biometric Enrolment", {"device_user_id": ["like", f"{PREFIX}%"]}),
		("Biometric Device", {"device_name": DEVICE}),
		("Biometric Sync Log", {"device": DEVICE}),
	):
		for name in frappe.get_all(dt, filters=filters, pluck="name"):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True, delete_permanently=True)

	# Nothing to restore on Employee: the mirror only ever writes an ID onto an
	# employee that had none, and on_trash takes back exactly what it wrote.
	frappe.db.commit()


def _punch(device_user_id, minutes_ago=30, log_type="IN", serial=SERIAL):
	return RawPunch(
		device_serial=serial,
		device_user_id=device_user_id,
		punch_time=add_to_date(now_datetime(), minutes=-minutes_ago),
		raw_log_type=log_type,
		payload={"test": True},
	)


def _checkins_for(punch_key):
	name = frappe.db.get_value("Biometric Punch", {"punch_key": punch_key}, "employee_checkin")
	return name


# ---------------------------------------------------------------------- tests


def test_happy_path():
	print("Happy path:")
	raw = _punch(ID_OK, minutes_ago=31)
	out = ingest([raw], source="Push")
	r = out["results"][0]
	check("one punch maps", r["status"] == "Mapped", r.get("message"))
	if r["status"] != "Mapped":
		return None
	checkin = frappe.get_doc("Employee Checkin", r["employee_checkin"])
	check("device_id is set on the checkin", checkin.device_id == DEVICE, checkin.device_id)
	check("log_type is set on the checkin", checkin.log_type == "IN", checkin.log_type)
	check("a sync log was written", bool(out["sync_log"]))
	check("device last_punch_time recorded", bool(frappe.db.get_value("Biometric Device", DEVICE, "last_punch_time")))
	return raw


def test_replay(raw):
	print("Replay:")
	before = frappe.db.count("Employee Checkin", {"device_id": DEVICE})
	out = ingest([raw], source="Push")
	r = out["results"][0]
	check("a replayed punch is Duplicate", r["status"] == "Duplicate", r.get("message"))
	after = frappe.db.count("Employee Checkin", {"device_id": DEVICE})
	check("replay creates no second checkin", before == after, f"{before} -> {after}")
	attempts = frappe.db.get_value("Biometric Punch", {"punch_key": r["punch_key"]}, "attempts")
	check("the replay is counted on the original punch", (attempts or 0) >= 2, attempts)


def test_unmapped():
	print("Unmapped:")
	raw = _punch(ID_UNKNOWN, minutes_ago=32)
	out = ingest([raw], source="Push")
	r = out["results"][0]
	check("an unknown device user ID parks as Unmapped", r["status"] == "Unmapped", r.get("message"))
	check("no checkin was created for it", not r["employee_checkin"])
	check("the punch is still visible", bool(r["punch"]))

	# and it is not retried on the wire — a resend collides with the same key
	again = ingest([raw], source="Push")["results"][0]
	check("resending an unmapped punch is a Duplicate, not a retry", again["status"] == "Duplicate")


def test_unknown_device():
	print("Unknown device:")
	raw = _punch(ID_OK, minutes_ago=33, serial=f"{PREFIX}-SN-GHOST")
	r = ingest([raw], source="Push")["results"][0]
	check("an unregistered serial is an Error, not a silent drop", r["status"] == "Error", r.get("message"))
	check("the reason names the serial", "GHOST" in (r.get("message") or ""), r.get("message"))


def test_clock_skew():
	print("Clock skew:")
	raw = RawPunch(
		device_serial=SERIAL,
		device_user_id=ID_OK,
		punch_time=add_to_date(now_datetime(), minutes=90),
		raw_log_type="IN",
	)
	r = ingest([raw], source="Push")["results"][0]
	check("a punch from the future is an Error", r["status"] == "Error", r.get("message"))
	check("the reason mentions the device clock", "clock" in (r.get("message") or "").lower(), r.get("message"))


def test_ignore_before():
	print("Cut-off:")
	frappe.db.set_value("Biometric Device", DEVICE, "ignore_punches_before", now_datetime(), update_modified=False)
	frappe.clear_cache(doctype="Biometric Device")
	raw = _punch(ID_OK, minutes_ago=60 * 24 * 30)
	r = ingest([raw], source="Push")["results"][0]
	check("a punch before the cut-off is Ignored", r["status"] == "Ignored", r.get("message"))
	check("no checkin for an ignored punch", not r["employee_checkin"])
	frappe.db.set_value("Biometric Device", DEVICE, "ignore_punches_before", None, update_modified=False)


def test_disabled_device():
	print("Disabled device:")
	frappe.db.set_value("Biometric Device", DEVICE, "enabled", 0, update_modified=False)
	r = ingest([_punch(ID_OK, minutes_ago=34)], source="Push")["results"][0]
	check("a punch from a disabled device is Ignored", r["status"] == "Ignored", r.get("message"))
	frappe.db.set_value("Biometric Device", DEVICE, "enabled", 1, update_modified=False)


def test_missing_geolocation():
	print("Geolocation:")
	if not frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"):
		check("SKIPPED — geolocation tracking is off on this site", True)
		return
	lat, lon = frappe.db.get_value("Biometric Device", DEVICE, ["latitude", "longitude"])
	frappe.db.set_value("Biometric Device", DEVICE, {"latitude": 0, "longitude": 0}, update_modified=False)
	r = ingest([_punch(ID_OK, minutes_ago=37)], source="Push")["results"][0]
	check("a device with no coordinates gives an actionable Error", r["status"] == "Error", r.get("message"))
	check(
		"the message names the setting and the fix",
		"Geolocation" in (r.get("message") or "") and "Latitude" in (r.get("message") or ""),
		r.get("message"),
	)
	frappe.db.set_value("Biometric Device", DEVICE, {"latitude": lat, "longitude": lon}, update_modified=False)


def test_batch_isolation():
	"""One bad punch must fail one punch. A device sending a malformed record is
	the realistic version of this — and the record cannot even be staged, since
	there is no usable timestamp to key it on. It must still be reported, and
	everything behind it must still land."""
	print("Batch isolation:")
	bad = RawPunch(
		device_serial=SERIAL,
		device_user_id=ID_OK,
		punch_time="not-a-timestamp",
		raw_log_type="IN",
	)
	good = _punch(ID_OK, minutes_ago=35)
	out = ingest([bad, good], source="Push")
	bad_result, good_result = out["results"]
	check("the malformed punch is reported as an Error", bad_result["status"] == "Error", bad_result.get("message"))
	check("the error says what was wrong", bool(bad_result.get("message")), bad_result.get("message"))
	check("the good punch behind it still maps", good_result["status"] == "Mapped", good_result.get("message"))
	check("the batch was not aborted", out["received"] == 2)
	check("the good punch produced a checkin", bool(good_result["employee_checkin"]))


def run():
	print("Biometric Hub — ingestion engine (Phase 2)\n")
	active, left = _employees()
	teardown()
	setup(active, left)
	try:
		raw = test_happy_path()
		if raw:
			test_replay(raw)
		test_unmapped()
		test_unknown_device()
		test_clock_skew()
		test_ignore_before()
		test_disabled_device()
		test_missing_geolocation()
		test_batch_isolation()
	finally:
		teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_ingest: {len(failed)} check(s) failed")
	print("All good.")
