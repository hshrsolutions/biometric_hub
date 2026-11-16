"""Selftest — asserts the invariants Biometric Hub is supposed to guarantee.

	bench --site <site> execute biometric_hub.tests.selftest.run

Read-only except for one deliberately-colliding pair of Biometric Punch rows,
which are removed again. Safe to run on any site, including one with live data.

Covers schema, roles, settings and the idempotency guarantee, then runs the
ingestion acceptance in tests/test_ingest.py — which creates its own device and
enrolments and deletes everything it made, including the Employee Checkins.
"""

import frappe

DOCTYPES = [
	"Biometric Driver",
	"Biometric Device",
	"Biometric Connector",
	"Biometric Enrolment",
	"Biometric Punch",
	"Biometric Sync Log",
	"Biometric Settings",
	"Biometric Employee Access",
	"Biometric Access Device",
	"Biometric Device User",
	"Biometric Employee ID",
	"Biometric Area Rule",
	"Biometric User Sync",
]

# The only doctypes the non-interactive integration account may touch. Anything
# else showing up here is the audit §3 failure returning, so it is a hard fail.
INTEGRATION_ALLOWLIST = {
	"Biometric Punch",
	"Biometric Device",
	"Biometric Connector",
	"Employee Checkin",  # granted in Phase 3
}

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + detail if detail and not condition else ''}")
	return bool(condition)


def test_doctypes():
	print("Schema:")
	for dt in DOCTYPES:
		exists = frappe.db.exists("DocType", dt)
		module = frappe.db.get_value("DocType", dt, "module") if exists else None
		check(f"{dt} exists in module {module or '?'}", exists and module == "Biometric Hub")


def _unique_index_exists(doctype, column):
	rows = frappe.db.sql(
		f"SHOW INDEX FROM `tab{doctype}` WHERE Column_name = %s AND Non_unique = 0", (column,)
	)
	return bool(rows)


def test_idempotency_index():
	print("Idempotency:")
	check(
		"Biometric Punch.punch_key carries a UNIQUE index",
		_unique_index_exists("Biometric Punch", "punch_key"),
		"without this, a replayed punch becomes a duplicate Employee Checkin",
	)
	check(
		"Biometric Enrolment.enrolment_key carries a UNIQUE index",
		_unique_index_exists("Biometric Enrolment", "enrolment_key"),
		"without this, one device ID could map to two employees",
	)


def test_idempotency_enforced():
	"""Prove the guarantee rather than trusting the schema: two punches with the
	same key must not both survive."""
	key = "selftest::collision::do-not-keep"
	made = []
	collided = False
	try:
		for _ in range(2):
			doc = frappe.get_doc(
				{
					"doctype": "Biometric Punch",
					"punch_key": key,
					"status": "Pending",
					"device_serial": "SELFTEST",
					"device_user_id": "0",
					"punch_time": "2000-01-01 00:00:00",
				}
			)
			try:
				doc.insert(ignore_permissions=True)
				made.append(doc.name)
			except frappe.exceptions.DuplicateEntryError:
				collided = True
			except Exception as e:  # MariaDB may surface it unwrapped
				collided = "Duplicate entry" in str(e)
		check("a replayed punch_key is rejected by the database", collided and len(made) == 1)
	finally:
		for name in made:
			frappe.delete_doc("Biometric Punch", name, force=True, ignore_permissions=True)
		frappe.db.commit()


def test_settings():
	print("Settings:")
	s = frappe.db.get_singles_dict("Biometric Settings")
	check("Biometric Settings is materialised", bool(s))
	if not s:
		return
	check("ingestion is enabled", frappe.utils.cint(s.get("enable_ingestion")) == 1)
	check("max_future_skew_minutes is set", frappe.utils.cint(s.get("max_future_skew_minutes")) > 0)
	check("punch_retention_days is set", frappe.utils.cint(s.get("punch_retention_days")) > 0)
	check(
		"default_log_type_policy is a known value",
		s.get("default_log_type_policy") in ("Trust Device", "Alternate", "First/Last of Day"),
	)


def test_roles():
	print("Roles and least privilege:")
	for role in ("Biometric Manager", "Biometric Integration"):
		check(f"role {role} exists", frappe.db.exists("Role", role))

	granted = frappe.get_all(
		"DocPerm",
		filters={"role": "Biometric Integration"},
		fields=["parent"],
		limit_page_length=0,
	)
	custom = frappe.get_all(
		"Custom DocPerm",
		filters={"role": "Biometric Integration"},
		fields=["parent"],
		limit_page_length=0,
	)
	touched = {r.parent for r in granted} | {r.parent for r in custom}
	stray = sorted(touched - INTEGRATION_ALLOWLIST)
	check(
		"Biometric Integration holds no permission outside its allowlist",
		not stray,
		f"unexpected: {stray}",
	)
	check(
		"Biometric Integration cannot write Shift Type",
		"Shift Type" not in touched,
		"upstream BAST needs this; we advance last_sync_of_checkin server-side so we must not",
	)


def test_enrolment_bootstrap():
	print("Enrolment bootstrap:")
	employees = frappe.get_all(
		"Employee",
		filters={"attendance_device_id": ["is", "set"]},
		fields=["name", "attendance_device_id"],
		limit_page_length=0,
	)
	# A global enrolment has device NULL, which SQL `IN ('', NULL)` will never match —
	# compare in Python instead of trying to be clever in the filter.
	global_ids = {
		e.device_user_id
		for e in frappe.get_all(
			"Biometric Enrolment", fields=["device_user_id", "device"], limit_page_length=0
		)
		if not e.device
	}
	missing = []
	for emp in employees:
		device_user_id = (emp.attendance_device_id or "").strip()
		if not device_user_id:
			continue
		if device_user_id not in global_ids:
			missing.append(emp.name)
	check(
		f"every employee with attendance_device_id has an enrolment ({len(employees)} checked)",
		not missing,
		f"missing for {missing[:5]}{'...' if len(missing) > 5 else ''}",
	)


def run():
	print("Biometric Hub selftest\n")
	test_doctypes()
	test_idempotency_index()
	test_idempotency_enforced()
	test_settings()
	test_roles()
	test_enrolment_bootstrap()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} schema checks passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + detail if detail else ''}")
		raise Exception(f"selftest: {len(failed)} check(s) failed")

	# The suites below need at least one Employee to map punches to. On a bare
	# site — a fresh install, before the setup wizard — say so and stop, rather
	# than failing in a way that looks like a defect.
	if not frappe.get_all("Employee", filters={"status": "Active"}, limit=1):
		print("\nSKIPPED — no active Employee on this site, so the ingestion, API,")
		print("workbench, agent, ADMS and health suites cannot run. Schema is verified.")
		return

	# The ingestion guarantees, then the API and least privilege.
	print()
	from biometric_hub.tests import (
		test_access,
		test_adms,
		test_agent_profiles_e2e,
		test_device_users,
		test_employee_ids,
		test_agent_e2e,
		test_agent_sql_e2e,
		test_api,
		test_drivers,
		test_health,
		test_ingest,
		test_user_push,
		test_workbench,
	)

	test_ingest.run()
	print()
	test_api.run()
	print()
	test_workbench.run()
	print()
	test_access.run()
	print()
	test_device_users.run()
	print()
	test_employee_ids.run()
	print()
	test_drivers.run()
	print()
	test_agent_e2e.run()
	print()
	test_agent_sql_e2e.run()
	print()
	test_agent_profiles_e2e.run()
	print()
	test_user_push.run()
	print()
	test_adms.run()
	print()
	test_health.run()
