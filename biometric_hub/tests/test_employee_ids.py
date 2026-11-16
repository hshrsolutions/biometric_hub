"""Employee → Biometric IDs: several IDs per employee, every one counts toward
their attendance, and no ID can belong to two employees.

	bench --site <site> execute biometric_hub.tests.test_employee_ids.run

Uses two existing employees without a device ID, saves them through the real
Employee form path, and restores them (and removes every ID it added) on exit.
"""

import frappe

from biometric_hub.engine import ingest as engine
from biometric_hub.tests import test_ingest as fixtures

P = fixtures.PREFIX
ID_A1, ID_A2, ID_A3, ID_PARKED = f"{P}-EID-1", f"{P}-EID-2", f"{P}-EID-3", f"{P}-EID-9"

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def _two_free(exclude):
	rows = frappe.get_all(
		"Employee", filters={"status": "Active", "attendance_device_id": ["in", ["", None]], "name": ["!=", exclude]},
		pluck="name", order_by="name asc", limit=40,
	)
	free = [r for r in rows if not frappe.db.exists("Biometric Enrolment", {"employee": r})]
	return free[:2]


def _set_ids(employee, rows, device_id=None):
	doc = frappe.get_doc("Employee", employee)
	doc.set("bh_biometric_ids", [{"device_user_id": u, "device": d} for u, d in rows])
	if device_id is not None:
		doc.attendance_device_id = device_id
	# Demo employees lack site-specific mandatory fields (e.g. adhaar_num); not what is under test.
	doc.flags.ignore_mandatory = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return doc


def _ids(employee):
	return [(r.device_user_id, r.device) for r in frappe.get_doc("Employee", employee).get("bh_biometric_ids")]


def _punch(uid, minutes, serial=fixtures.SERIAL):
	out = engine.ingest([fixtures._punch(uid, minutes_ago=minutes, serial=serial)], source="Push")
	frappe.db.commit()
	return out["results"][0]


def test_several_ids(a):
	print("Several IDs for one employee:")
	out = engine.ingest([fixtures._punch(ID_PARKED, minutes_ago=140)], source="Push")
	frappe.db.commit()
	check("setup: an unknown ID's punch parks as Unmapped", out["unmapped"] == 1, out)

	_set_ids(a, [(ID_A1, None), (ID_A2, fixtures.DEVICE), (ID_PARKED, None)])
	check("each row becomes an enrolment",
	      all(frappe.db.exists("Biometric Enrolment", {"employee": a, "device_user_id": u}) for u in (ID_A1, ID_A2, ID_PARKED)))
	check("the first all-machines ID becomes the Attendance Device ID",
	      frappe.db.get_value("Employee", a, "attendance_device_id") == ID_A1,
	      frappe.db.get_value("Employee", a, "attendance_device_id"))
	check("every row links its enrolment",
	      all(r.enrolment for r in frappe.get_doc("Employee", a).get("bh_biometric_ids")))

	check("a punch on the first ID counts for the employee", _punch(ID_A1, 130)["status"] == "Mapped")
	r = _punch(ID_A2, 129)
	check("a machine-only ID counts on that machine",
	      r["status"] == "Mapped" and frappe.db.get_value("Biometric Punch", r["punch"], "employee") == a, r)
	check("but not on another machine", _punch(ID_A2, 128, serial=f"{P}-SN-NOT-REGISTERED")["status"] != "Mapped")

	engine.replay_unmapped(ID_PARKED)
	frappe.db.commit()
	parked = frappe.db.get_value("Biometric Punch", {"device_user_id": ID_PARKED}, ["status", "employee"], as_dict=True)
	check("adding an ID repairs punches already parked behind it",
	      parked.status == "Mapped" and parked.employee == a, parked)


def test_unique(a, b):
	print("One ID, one employee:")
	for label, rows in (("an ID another employee has", [(ID_A1, None)]),
	                    ("a machine-only ID another employee has on that machine", [(ID_A2, fixtures.DEVICE)])):
		try:
			_set_ids(b, rows)
			check(f"{label} is refused", False)
		except frappe.ValidationError as e:
			frappe.db.rollback()
			check(f"{label} is refused", True)
			if "global" not in label:
				check("and HR is told who has it", a in str(e), str(e))
	try:
		_set_ids(a, [(ID_A1, None), (ID_A1, None)])
		check("the same ID twice in one list is refused", False)
	except frappe.ValidationError:
		frappe.db.rollback()
		check("the same ID twice in one list is refused", True)
	check("nothing changed for the first employee after the refusals", len(_ids(a)) == 3, _ids(a))


def test_remove_and_elsewhere(a):
	print("Removing a row, and IDs linked elsewhere:")
	_set_ids(a, [(ID_A1, None), (ID_PARKED, None)])
	check("removing a row unlinks that ID",
	      not frappe.db.exists("Biometric Enrolment", {"device_user_id": ID_A2, "employee": a}))
	check("the other IDs stay", _ids(a) == [(ID_A1, None), (ID_PARKED, None)], _ids(a))

	frappe.get_doc({"doctype": "Biometric Enrolment", "employee": a, "device_user_id": ID_A3, "enabled": 1,
	                "notes": "Created from the unmapped punch queue."}).insert(ignore_permissions=True)
	frappe.db.commit()
	check("an ID linked from the workbench appears in the employee's list", (ID_A3, None) in _ids(a), _ids(a))

	doc = _set_ids(a, [(ID_A1, None), (ID_PARKED, None), (ID_A3, None)], device_id=ID_PARKED)
	check("choosing another listed ID as Attendance Device ID is allowed",
	      frappe.db.get_value("Employee", a, "attendance_device_id") == ID_PARKED)

	_set_ids(a, [(ID_A3, None)])
	check("removing the Attendance Device ID's row moves the field to the next ID",
	      frappe.db.get_value("Employee", a, "attendance_device_id") == ID_A3,
	      frappe.db.get_value("Employee", a, "attendance_device_id"))


def run():
	print("Biometric Hub — Employee → Biometric IDs\n")
	if not frappe.get_meta("Employee").has_field("bh_biometric_ids"):
		print("SKIPPED — Employee.bh_biometric_ids is not installed.")
		return
	active, left = fixtures._employees()
	free = _two_free(active)
	if len(free) < 2:
		print("SKIPPED — needs two active employees without a device ID.")
		return
	a, b = free
	fixtures.teardown()
	fixtures.setup(active, left)
	try:
		test_several_ids(a)
		test_unique(a, b)
		test_remove_and_elsewhere(a)
	finally:
		frappe.db.rollback()
		fixtures.teardown()  # deletes every BHTEST enrolment -> their rows go too
		for emp in (a, b):
			frappe.db.delete("Biometric Employee ID", {"parent": emp, "device_user_id": ["like", f"{P}%"]})
			if (frappe.db.get_value("Employee", emp, "attendance_device_id") or "").startswith(P):
				frappe.db.set_value("Employee", emp, "attendance_device_id", None, update_modified=False)
		frappe.db.commit()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_employee_ids: {len(failed)} check(s) failed")
	print("All good.")
