"""Phase 4 acceptance: the workbench, and enrolment side effects.

	bench --site <site> execute biometric_hub.tests.test_workbench.run
"""

import frappe

from biometric_hub.api import admin
from biometric_hub.engine import ingest as engine
from biometric_hub.tests import test_ingest as fixtures

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def test_unmapped_queue_and_replay(active):
	print("Unmapped queue:")
	# Three punches from someone nobody has enrolled.
	batch = [fixtures._punch(fixtures.ID_UNKNOWN, minutes_ago=m) for m in (50, 51, 52)]
	out = engine.ingest(batch, source="Poll")
	check("all three park as Unmapped", out["unmapped"] == 3, out)

	summary = admin.unmapped_summary()
	row = next((r for r in summary if r["device_user_id"] == fixtures.ID_UNKNOWN), None)
	check("the queue shows the ID", bool(row), [r["device_user_id"] for r in summary])
	check("with its punch count", row and row["punches"] == 3, row)
	check("and when it was first seen", bool(row and row["first_punch"]))

	# The five-second fix.
	result = admin.map_unmapped(fixtures.ID_UNKNOWN, active)
	check("mapping replays every parked punch", result["replayed"] == 3, result)
	check("and they all become checkins", result["mapped"] == 3, result)
	check("none are left unmapped", result["still_unmapped"] == 0, result)

	remaining = [r for r in admin.unmapped_summary() if r["device_user_id"] == fixtures.ID_UNKNOWN]
	check("the queue is now clear for that ID", not remaining, remaining)

	checkins = frappe.get_all(
		"Biometric Punch",
		filters={"device_user_id": fixtures.ID_UNKNOWN, "status": "Mapped"},
		fields=["employee", "employee_checkin"],
	)
	check("the repaired punches point at the right employee", all(c.employee == active for c in checkins))
	check("and each carries a checkin", all(c.employee_checkin for c in checkins))


def test_error_is_cleared_on_repair():
	print("Repair clears the reason:")
	stale = frappe.get_all(
		"Biometric Punch",
		filters={"device_user_id": fixtures.ID_UNKNOWN, "status": "Mapped"},
		fields=["error_message"],
	)
	check("a repaired punch no longer carries its old error", all(not p.error_message for p in stale), stale)


def test_device_health():
	print("Device health:")
	rows = admin.device_health()
	row = next((r for r in rows if r["name"] == fixtures.DEVICE), None)
	check("the test device appears", bool(row), [r["name"] for r in rows])
	check("with a health status", bool(row and row["health_status"]), row)
	check("and today's punch count", row is not None and row["punches_today"] >= 1, row)


def test_recent_activity():
	print("Activity:")
	activity = admin.recent_activity()
	check("mapped punches are counted", activity["mapped"] >= 1, activity)
	check("every bucket is present", set(activity) == {"mapped", "unmapped", "blocked", "duplicates", "errors"}, activity)


def test_enrolment_mirrors_to_employee():
	"""Needs an employee with no device ID of their own — the fixtures already gave
	one to the employee they use, and the mirror correctly refuses to overwrite."""
	print("Enrolment mirror:")
	candidates = frappe.get_all(
		"Employee",
		filters={"status": "Active", "attendance_device_id": ["in", ["", None]]},
		pluck="name",
		limit=1,
	)
	if not candidates:
		check("SKIPPED — every active employee already has a device ID", True)
		return
	employee = candidates[0]

	created = []
	try:
		first = frappe.get_doc(
			{"doctype": "Biometric Enrolment", "employee": employee,
			 "device_user_id": f"{fixtures.PREFIX}-MIRROR", "enabled": 1}
		)
		first.insert(ignore_permissions=True)
		created.append(first.name)
		check(
			"a global enrolment mirrors onto Employee.attendance_device_id",
			frappe.db.get_value("Employee", employee, "attendance_device_id") == f"{fixtures.PREFIX}-MIRROR",
			frappe.db.get_value("Employee", employee, "attendance_device_id"),
		)

		# A device-specific enrolment must not: one scalar cannot hold two IDs.
		scoped = frappe.get_doc(
			{"doctype": "Biometric Enrolment", "employee": employee,
			 "device_user_id": f"{fixtures.PREFIX}-SCOPED", "device": fixtures.DEVICE, "enabled": 1}
		)
		scoped.insert(ignore_permissions=True)
		created.append(scoped.name)
		check(
			"a device-specific enrolment does not mirror",
			frappe.db.get_value("Employee", employee, "attendance_device_id") == f"{fixtures.PREFIX}-MIRROR",
			frappe.db.get_value("Employee", employee, "attendance_device_id"),
		)

		# Two global enrolments for one person is a contradiction one scalar cannot
		# hold. The second must not silently replace the first.
		second = frappe.get_doc(
			{"doctype": "Biometric Enrolment", "employee": employee,
			 "device_user_id": f"{fixtures.PREFIX}-SECOND", "enabled": 1}
		)
		second.insert(ignore_permissions=True)
		created.append(second.name)
		check(
			"a second global enrolment does not overwrite the existing device ID",
			frappe.db.get_value("Employee", employee, "attendance_device_id") == f"{fixtures.PREFIX}-MIRROR",
			frappe.db.get_value("Employee", employee, "attendance_device_id"),
		)

		# Deleting the one that set it takes it back off, so a removed mapping does
		# not keep resolving through the legacy fallback.
		frappe.delete_doc("Biometric Enrolment", first.name, force=True, ignore_permissions=True)
		created.remove(first.name)
		check(
			"deleting the enrolment that set it clears the mirror",
			not frappe.db.get_value("Employee", employee, "attendance_device_id"),
			frappe.db.get_value("Employee", employee, "attendance_device_id"),
		)
	finally:
		for name in created:
			if frappe.db.exists("Biometric Enrolment", name):
				frappe.delete_doc("Biometric Enrolment", name, force=True, ignore_permissions=True)
		frappe.db.set_value("Employee", employee, "attendance_device_id", None, update_modified=False)
		frappe.db.commit()


def test_employee_form_adopts_device_id():
	"""HR typing a device ID onto the Employee form must work like mapping it in
	the workbench: an enrolment appears and the parked punches are replayed."""
	print("Device ID set on the Employee form:")
	from biometric_hub.engine import enrolment as enrolment_engine

	hooked = frappe.get_hooks("doc_events").get("Employee", {}).get("on_update") or []
	check("the Employee on_update hook is registered",
	      "biometric_hub.engine.enrolment.adopt_employee_device_id" in hooked, hooked)

	candidates = frappe.get_all(
		"Employee", filters={"status": "Active", "attendance_device_id": ["in", ["", None]]},
		pluck="name", limit=1,
	)
	if not candidates:
		check("SKIPPED — every active employee already has a device ID", True)
		return
	employee = candidates[0]
	device_user_id = f"{fixtures.PREFIX}-FORM"

	out = engine.ingest([fixtures._punch(device_user_id, minutes_ago=m) for m in (40, 41)], source="Poll")
	check("two punches park as Unmapped first", out["unmapped"] == 2, out)

	queued = []
	real_enqueue = frappe.enqueue
	frappe.enqueue = lambda method, **kw: queued.append((method, kw))
	try:
		frappe.db.set_value("Employee", employee, "attendance_device_id", device_user_id, update_modified=False)
		enrolment_engine.adopt_employee_device_id(frappe.get_doc("Employee", employee))
		made = frappe.db.get_value("Biometric Enrolment", {"device_user_id": device_user_id}, "employee")
		check("an enrolment is created for that employee", made == employee, made)
		check("a replay of the parked punches is queued",
		      queued and queued[0][0].endswith("replay_unmapped") and queued[0][1]["device_user_id"] == device_user_id,
		      queued)
		result = engine.replay_unmapped(device_user_id)
		check("and the replay maps them", result["mapped"] == 2, result)

		queued.clear()
		enrolment_engine.adopt_employee_device_id(frappe.get_doc("Employee", employee))
		check("saving again changes nothing", not queued and frappe.db.count(
			"Biometric Enrolment", {"device_user_id": device_user_id}) == 1)
	finally:
		frappe.enqueue = real_enqueue
		for name in frappe.get_all("Biometric Enrolment", filters={"device_user_id": device_user_id}, pluck="name"):
			frappe.delete_doc("Biometric Enrolment", name, force=True, ignore_permissions=True)
		frappe.db.set_value("Employee", employee, "attendance_device_id", None, update_modified=False)
		frappe.db.commit()


def run():
	print("Biometric Hub — workbench (Phase 4)\n")
	active, left = fixtures._employees()
	fixtures.teardown()
	fixtures.setup(active, left)
	try:
		test_unmapped_queue_and_replay(active)
		test_error_is_cleared_on_repair()
		test_device_health()
		test_recent_activity()
		test_enrolment_mirrors_to_employee()
		test_employee_form_adopts_device_id()
	finally:
		fixtures.teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_workbench: {len(failed)} check(s) failed")
	print("All good.")
