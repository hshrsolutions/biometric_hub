"""Phase 8 acceptance: health, alerts, reconcile, retention and reports.

	bench --site <site> execute biometric_hub.tests.test_health.run
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from biometric_hub.engine import health
from biometric_hub.tests import test_ingest as fixtures

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def test_goes_silent_then_comes_back():
	print("Silence:")
	frappe.db.set_value(
		"Biometric Device", fixtures.DEVICE,
		{"last_contact": add_to_date(now_datetime(), minutes=-500), "heartbeat_grace_minutes": 60,
		 "health_status": "Online", "alert_recipients": None},
		update_modified=False,
	)
	frappe.clear_cache(doctype="Biometric Device")

	silent = health.check_devices()
	check("a device past its grace goes Silent", fixtures.DEVICE in silent, silent)
	check("and the record says so",
	      frappe.db.get_value("Biometric Device", fixtures.DEVICE, "health_status") == "Silent")

	# Alerting once, not every run, is the difference between a signal and noise.
	again = health.check_devices()
	check("it is not reported silent a second time", fixtures.DEVICE not in again, again)

	frappe.db.set_value("Biometric Device", fixtures.DEVICE, "last_contact", now_datetime(), update_modified=False)
	health.check_devices()
	check("it returns to Online when it reports again",
	      frappe.db.get_value("Biometric Device", fixtures.DEVICE, "health_status") == "Online")


def test_grace_zero_disables_alerting():
	print("Alerting off:")
	frappe.db.set_value(
		"Biometric Device", fixtures.DEVICE,
		{"heartbeat_grace_minutes": 0, "last_contact": add_to_date(now_datetime(), minutes=-5000),
		 "health_status": "Online"},
		update_modified=False,
	)
	silent = health.check_devices()
	check("grace of zero means never alert", fixtures.DEVICE not in silent, silent)
	check("and the status is left alone",
	      frappe.db.get_value("Biometric Device", fixtures.DEVICE, "health_status") == "Online")
	frappe.db.set_value("Biometric Device", fixtures.DEVICE, "heartbeat_grace_minutes", 60, update_modified=False)


def test_reconcile_never_retries_unmapped():
	"""The single most important thing reconcile does not do."""
	print("Reconcile:")
	from biometric_hub.engine import ingest as engine

	engine.ingest([fixtures._punch(fixtures.ID_UNKNOWN, minutes_ago=80)], source="Poll")
	before = frappe.db.count("Biometric Punch", {"status": "Unmapped"})
	result = health.reconcile()
	after = frappe.db.count("Biometric Punch", {"status": "Unmapped"})
	check("unmapped punches are not touched by reconcile", before == after, f"{before} -> {after}")
	check("reconcile reports what it considered", "considered" in result, result)


def test_reconcile_resolves_pending():
	print("Reconcile resolves what it should:")
	settings = frappe.get_single("Biometric Settings")
	settings.create_employee_checkin = 0
	settings.save(ignore_permissions=True)
	frappe.clear_cache(doctype="Biometric Settings")

	from biometric_hub.engine import ingest as engine

	out = engine.ingest([fixtures._punch(fixtures.ID_OK, minutes_ago=81)], source="Poll")
	check("with checkin creation off, a punch stages as Pending", out["pending"] == 1, out)

	settings = frappe.get_single("Biometric Settings")
	settings.create_employee_checkin = 1
	settings.save(ignore_permissions=True)
	frappe.clear_cache(doctype="Biometric Settings")

	result = health.reconcile()
	check("turning it back on and reconciling resolves them", result["resolved"] >= 1, result)
	check("and the punch is now Mapped",
	      frappe.db.count("Biometric Punch", {"device": fixtures.DEVICE, "status": "Mapped"}) >= 1)


def test_purge_keeps_unfinished_work():
	print("Retention:")
	old = add_to_date(now_datetime(), days=-400)
	keep, drop = [], []
	for status, bucket in (("Mapped", drop), ("Duplicate", drop), ("Unmapped", keep), ("Error", keep)):
		doc = frappe.get_doc(
			{
				"doctype": "Biometric Punch",
				"punch_key": f"{fixtures.PREFIX}-purge-{status}",
				"status": status,
				"device_serial": fixtures.SERIAL,
				"device_user_id": "0",
				"punch_time": old,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.set_value("Biometric Punch", doc.name, "creation", old, update_modified=False)
		bucket.append(doc.name)
	frappe.db.commit()

	health.purge()
	check("old mapped and duplicate punches are purged",
	      not any(frappe.db.exists("Biometric Punch", n) for n in drop), drop)
	check("unmapped and errored punches are never purged — that is unfinished work",
	      all(frappe.db.exists("Biometric Punch", n) for n in keep), keep)


def test_reports():
	print("Reports:")
	for name in ("Employees Without Biometric Enrolment", "Biometric Punch Outcomes", "Biometric Device Uptime"):
		check(f"{name} exists", frappe.db.exists("Report", name))
		try:
			report = frappe.get_doc("Report", name)
			columns, rows = report.execute_query_report(filters={})
			check(f"{name} runs", isinstance(columns, list), f"{len(rows)} row(s)")
		except Exception as e:
			check(f"{name} runs", False, f"{type(e).__name__}: {e}")


def run():
	print("Biometric Hub — health, reconcile, retention, reports (Phase 8)\n")
	active, left = fixtures._employees()
	fixtures.teardown()
	fixtures.setup(active, left)
	try:
		test_goes_silent_then_comes_back()
		test_grace_zero_disables_alerting()
		test_reconcile_never_retries_unmapped()
		test_reconcile_resolves_pending()
		test_purge_keeps_unfinished_work()
		test_reports()
	finally:
		fixtures.teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_health: {len(failed)} check(s) failed")
	print("All good.")
