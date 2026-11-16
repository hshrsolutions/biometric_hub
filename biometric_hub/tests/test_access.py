"""Employee device access: which devices, and whether the mobile app, a person
may use to check in.

	bench --site <site> execute biometric_hub.tests.test_access.run

Builds on the ingestion fixtures (test_ingest), adds a second device, and
removes every row it made — access records, enrolments, punches, checkins.
Existing employees are read, never written; an employee who already has an
access record is never used.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from biometric_hub.api import admin
from biometric_hub.engine import ingest as engine
from biometric_hub.tests import test_ingest as fixtures

DEVICE_B = f"{fixtures.PREFIX} Device B"
SERIAL_B = f"{fixtures.PREFIX}-SN-2"
ID_B_ONLY = f"{fixtures.PREFIX}-B-77"
ID_TAKEN = f"{fixtures.PREFIX}-B-88"

_results = []
_made = {"access": set(), "checkins": set()}


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


# ------------------------------------------------------------------- fixtures


def _free_employees(n, with_user=False):
	"""Active employees with no access record of their own."""
	filters = {"status": "Active"}
	if with_user:
		filters["user_id"] = ["not in", ["", "Administrator", "Guest"]]
	taken = set(frappe.get_all("Biometric Employee Access", pluck="employee"))
	rows = frappe.get_all("Employee", filters=filters, fields=["name", "user_id"], order_by="name asc")
	return [r for r in rows if r.name not in taken][:n]


def _setup_device_b():
	frappe.get_doc(
		{
			"doctype": "Biometric Device",
			"device_name": DEVICE_B,
			"enabled": 1,
			"driver": fixtures.DRIVER,
			"connectivity_mode": "Push",
			"serial_no": SERIAL_B,
			"location": "Selftest B",
			"latitude": 9.9252,
			"longitude": 78.1198,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _access(employee, mode="Allowed Devices Only", devices=(), mobile=1):
	name = frappe.db.exists("Biometric Employee Access", employee)
	doc = frappe.get_doc("Biometric Employee Access", name) if name else frappe.new_doc("Biometric Employee Access")
	doc.employee = employee
	doc.access_mode = mode
	doc.allow_mobile_checkin = mobile
	doc.set("devices", [])
	for d in devices:
		if isinstance(d, str):
			d = {"device": d}
		doc.append("devices", d)
	doc.save(ignore_permissions=True)
	_made["access"].add(doc.name)
	frappe.db.commit()
	return doc


def _teardown():
	frappe.set_user("Administrator")
	for name in list(_made["access"]):
		if frappe.db.exists("Biometric Employee Access", name):
			frappe.delete_doc("Biometric Employee Access", name, force=True, ignore_permissions=True)
	for name in list(_made["checkins"]):
		if frappe.db.exists("Employee Checkin", name):
			frappe.delete_doc("Employee Checkin", name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	fixtures.teardown()
	for name in frappe.get_all("Biometric Device", filters={"device_name": DEVICE_B}, pluck="name"):
		frappe.delete_doc("Biometric Device", name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	_made["access"].clear()
	_made["checkins"].clear()


def _punch(device_user_id, serial, minutes_ago):
	return fixtures.RawPunch(
		device_serial=serial,
		device_user_id=device_user_id,
		punch_time=add_to_date(now_datetime(), minutes=-minutes_ago),
		raw_log_type="IN",
		payload={"test": True},
	)


def _one(device_user_id, serial, minutes_ago):
	out = engine.ingest([_punch(device_user_id, serial, minutes_ago)], source="Push")
	frappe.db.commit()
	return out, out["results"][0]


# ---------------------------------------------------------------------- tests


def test_no_record_is_unrestricted():
	print("No access record:")
	_, r = _one(fixtures.ID_OK, SERIAL_B, 200)
	check("an employee with no record may punch on any device", r["status"] == "Mapped", r)


def test_allowed_devices_only(active):
	print("Allowed devices only:")
	_access(active, devices=[fixtures.DEVICE])

	raw = _punch(fixtures.ID_OK, SERIAL_B, 190)
	out = engine.ingest([raw], source="Push")
	frappe.db.commit()
	r = out["results"][0]
	check("a punch on a device not on the list is Blocked", r["status"] == "Blocked", r)
	check("no checkin is written for it", not r["employee_checkin"], r)
	check("the reason names the device", DEVICE_B in (r.get("message") or ""), r.get("message"))
	check("the batch counts it as blocked", out["blocked"] == 1, out)
	punch = frappe.db.get_value("Biometric Punch", r["punch"], ["employee", "status"], as_dict=True)
	check("the held punch still records who it was", punch.employee == active, punch)
	check("the sync log carries the blocked count",
	      frappe.db.get_value("Biometric Sync Log", out["sync_log"], "blocked") == 1)

	_, ok = _one(fixtures.ID_OK, fixtures.SERIAL, 189)
	check("a punch on an allowed device maps", ok["status"] == "Mapped", ok)

	again = engine.ingest([raw], source="Push")["results"][0]
	frappe.db.commit()
	check("replaying the blocked punch is a Duplicate, not a second row", again["status"] == "Duplicate", again)

	check("the workbench counts blocked punches", admin.recent_activity()["blocked"] >= 1)
	return r["punch"]


def test_allowing_the_device_releases(active, blocked_punch):
	print("Allowing the device releases held punches:")
	_access(active, devices=[fixtures.DEVICE, DEVICE_B])
	# Saving also queues this replay in the background; whichever runs first
	# wins and the other finds nothing — so assert the outcome, not who did it.
	result = engine.replay_blocked(active)
	frappe.db.commit()
	check("nothing is left blocked", result["still_blocked"] == 0, result)
	p = frappe.db.get_value("Biometric Punch", blocked_punch, ["status", "employee_checkin", "error_message"], as_dict=True)
	check("the punch is now Mapped with a checkin", p.status == "Mapped" and p.employee_checkin, p)
	check("and its old reason is cleared", not p.error_message, p)

	second = engine.replay_blocked(active)
	check("a second replay finds nothing to do", second["replayed"] == 0, second)
	before = frappe.db.count("Employee Checkin", {"name": p.employee_checkin})
	again = engine.reprocess(blocked_punch)
	check("reprocessing a resolved punch writes no second checkin",
	      again["status"] == "Mapped" and again["employee_checkin"] == p.employee_checkin and before == 1, again)
	frappe.db.commit()


def test_per_device_id(active):
	print("A different ID on one device:")
	doc = _access(active, devices=[fixtures.DEVICE, {"device": DEVICE_B, "device_user_id": ID_B_ONLY}])
	row = next(r for r in doc.devices if r.device == DEVICE_B)
	enrolment = frappe.db.get_value(
		"Biometric Enrolment", row.enrolment, ["employee", "device", "device_user_id", "enabled"], as_dict=True
	) if row.enrolment else None
	check("a device-specific enrolment is created", bool(enrolment), row.as_dict())
	check("for the right employee, device and ID",
	      enrolment and (enrolment.employee, enrolment.device, enrolment.device_user_id) == (active, DEVICE_B, ID_B_ONLY),
	      enrolment)

	_, r = _one(ID_B_ONLY, SERIAL_B, 180)
	check("that ID on that device maps to the employee", r["status"] == "Mapped", r)
	_, other = _one(ID_B_ONLY, fixtures.SERIAL, 179)
	check("the same ID on another device does not", other["status"] == "Unmapped", other)

	_access(active, devices=[fixtures.DEVICE, DEVICE_B])
	check("clearing the ID removes the enrolment it made",
	      not frappe.db.exists("Biometric Enrolment", {"device_user_id": ID_B_ONLY}))


def test_validation(active, other):
	print("Validation:")
	try:
		_access(active, devices=[fixtures.DEVICE, fixtures.DEVICE])
		check("a device listed twice is refused", False)
	except frappe.ValidationError:
		frappe.db.rollback()
		check("a device listed twice is refused", True)

	frappe.get_doc(
		{"doctype": "Biometric Enrolment", "employee": other, "device_user_id": ID_TAKEN,
		 "device": DEVICE_B, "enabled": 1}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	try:
		_access(active, devices=[{"device": DEVICE_B, "device_user_id": ID_TAKEN}])
		check("an ID that belongs to someone else on that device is refused", False)
	except frappe.ValidationError:
		frappe.db.rollback()
		check("an ID that belongs to someone else on that device is refused", True)
	check("and the other employee's enrolment is untouched",
	      frappe.db.get_value("Biometric Enrolment", {"device_user_id": ID_TAKEN}, "employee") == other)


def test_mobile_app(emp):
	print("Mobile app check-in:")

	def self_checkin(minutes_ago):
		frappe.set_user(emp.user_id)
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Employee Checkin",
					"employee": emp.name,
					"log_type": "IN",
					"time": add_to_date(now_datetime(), minutes=-minutes_ago),
					"latitude": 9.9252,
					"longitude": 78.1198,
				}
			).insert(ignore_permissions=True)
			_made["checkins"].add(doc.name)
			return doc
		finally:
			frappe.set_user("Administrator")

	_access(emp.name, mode="Any Device", mobile=0)
	try:
		self_checkin(170)
		check("an employee with mobile check-in off cannot check themselves in", False)
	except frappe.ValidationError as e:
		frappe.db.rollback()
		check("an employee with mobile check-in off cannot check themselves in", True)
		check("and is told why", "Mobile check-in is turned off" in str(e), str(e))

	hr = frappe.get_doc(
		{
			"doctype": "Employee Checkin", "employee": emp.name, "log_type": "IN",
			"time": add_to_date(now_datetime(), minutes=-169), "latitude": 9.9252, "longitude": 78.1198,
		}
	).insert(ignore_permissions=True)
	_made["checkins"].add(hr.name)
	check("HR can still record a checkin on their behalf", bool(hr.name))

	_access(emp.name, mode="Any Device", mobile=1)
	doc = self_checkin(168)
	check("switching it back on lets them check in", bool(doc and doc.name))
	frappe.db.commit()


def test_deleting_the_record(active):
	print("Deleting the record:")
	_access(active, devices=[{"device": DEVICE_B, "device_user_id": ID_B_ONLY}])
	check("setup: the managed enrolment exists", frappe.db.exists("Biometric Enrolment", {"device_user_id": ID_B_ONLY}))
	_, r = _one(fixtures.ID_OK, fixtures.SERIAL, 160)
	check("setup: a punch on the unlisted device is Blocked", r["status"] == "Blocked", r)

	frappe.delete_doc("Biometric Employee Access", active, ignore_permissions=True)
	frappe.db.commit()
	_made["access"].discard(active)
	check("its managed enrolments go with it",
	      not frappe.db.exists("Biometric Enrolment", {"device_user_id": ID_B_ONLY}))
	result = engine.replay_blocked(active)
	frappe.db.commit()
	status = frappe.db.get_value("Biometric Punch", r["punch"], "status")
	check("and the employee's held punches are released", status == "Mapped" and result["still_blocked"] == 0,
	      (status, result))


def run():
	print("Biometric Hub — employee device access\n")
	active, _left = fixtures._employees()
	if frappe.db.exists("Biometric Employee Access", active):
		print("SKIPPED — the fixture employee already has a real access record; not touching it.")
		return
	others = [e.name for e in _free_employees(5) if e.name != active]
	mobile = next((e for e in _free_employees(20, with_user=True) if e.name != active), None)

	_teardown()
	fixtures.setup(active, _left)
	_setup_device_b()
	try:
		test_no_record_is_unrestricted()
		blocked = test_allowed_devices_only(active)
		test_allowing_the_device_releases(active, blocked)
		test_per_device_id(active)
		if others:
			test_validation(active, others[0])
		else:
			print("  SKIP  validation — needs a second active employee")
		if mobile:
			test_mobile_app(mobile)
		else:
			print("  SKIP  mobile app — no active employee with a login")
		test_deleting_the_record(active)
	finally:
		_teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_access: {len(failed)} check(s) failed")
	print("All good.")
