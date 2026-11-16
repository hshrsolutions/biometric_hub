"""Phase 3 acceptance: the punch API, and least privilege proven with a real user.

	bench --site <site> execute biometric_hub.tests.test_api.run

Creates a throwaway integration user, a connector and a device, and removes all
of them again.
"""

import time

import frappe
from frappe.utils import add_to_date, now_datetime

from biometric_hub.api import ingest as api
from biometric_hub.tests import test_ingest as fixtures

USER = "bhtest-integration@example.com"
CONNECTOR = "BHTEST Connector"

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def setup_user():
	if frappe.db.exists("User", USER):
		frappe.delete_doc("User", USER, force=True, ignore_permissions=True)
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": USER,
			"first_name": "BHTest",
			"send_welcome_email": 0,
			"user_type": "System User",
		}
	)
	user.insert(ignore_permissions=True)
	# Exactly one role, and it is the point of the test.
	user.add_roles("Biometric Integration")
	frappe.db.commit()


def setup_connector():
	if not frappe.db.exists("Biometric Connector", CONNECTOR):
		frappe.get_doc(
			{
				"doctype": "Biometric Connector",
				"agent_name": CONNECTOR,
				"enabled": 1,
				"site_label": "Selftest",
				"api_user": USER,
			}
		).insert(ignore_permissions=True)
	frappe.db.set_value("Biometric Device", fixtures.DEVICE, "connector", CONNECTOR, update_modified=False)
	frappe.db.commit()


def teardown():
	frappe.set_user("Administrator")
	fixtures.teardown()
	for name in frappe.get_all("Biometric Connector", filters={"agent_name": CONNECTOR}, pluck="name"):
		frappe.delete_doc("Biometric Connector", name, force=True, ignore_permissions=True)
	if frappe.db.exists("User", USER):
		frappe.delete_doc("User", USER, force=True, ignore_permissions=True)
	frappe.db.commit()


def _punch_dict(user_id, minutes_ago):
	return {
		"device_serial": fixtures.SERIAL,
		"device_user_id": user_id,
		"punch_time": str(add_to_date(now_datetime(), minutes=-minutes_ago)),
		"raw_log_type": "IN",
	}


# ---------------------------------------------------------------------- tests


def test_push_as_integration_user():
	print("Push, as the integration user:")
	frappe.set_user(USER)
	out = api.push([_punch_dict(fixtures.ID_OK, 41)], connector=CONNECTOR)
	check("the integration user can post a batch", out["mapped"] == 1, out["results"])
	check("the response carries a per-punch status", out["results"][0]["status"] == "Mapped")
	check("a sync log was written", bool(out["sync_log"]))
	check("internal punch names are not leaked to the agent", "punch" not in out["results"][0])


def test_replay_is_free():
	print("Replay over the wire:")
	frappe.set_user(USER)
	punch = _punch_dict(fixtures.ID_OK, 42)
	first = api.push([punch], connector=CONNECTOR)
	second = api.push([punch], connector=CONNECTOR)
	check("first send maps", first["mapped"] == 1)
	check("resend is a Duplicate, not an error", second["duplicates"] == 1, second["results"])
	check("resend creates no checkin", second["mapped"] == 0)


def test_heartbeat_and_bootstrap():
	print("Heartbeat and bootstrap:")
	frappe.set_user(USER)
	hb = api.heartbeat(CONNECTOR, agent_version="0.1.0-test", devices=[fixtures.SERIAL])
	check("heartbeat is acknowledged", bool(hb["acknowledged"]))
	check("heartbeat resolves the device", fixtures.DEVICE in hb["devices"], hb["devices"])
	frappe.set_user("Administrator")
	check("agent version is recorded", frappe.db.get_value("Biometric Connector", CONNECTOR, "agent_version") == "0.1.0-test")
	check("last check-in is recorded", bool(frappe.db.get_value("Biometric Connector", CONNECTOR, "last_checkin")))

	frappe.set_user(USER)
	boot = api.bootstrap(CONNECTOR)
	serials = [d["serial_no"] for d in boot["devices"]]
	check("bootstrap returns the connector's devices", fixtures.SERIAL in serials, serials)
	check("bootstrap carries a batch size", boot["batch_size"] > 0)


def test_least_privilege():
	print("Least privilege:")
	frappe.set_user(USER)
	roles = set(frappe.get_roles())
	check("the user holds only the integration role", "Biometric Integration" in roles and "System Manager" not in roles, sorted(roles))
	check("can create Employee Checkin", frappe.has_permission("Employee Checkin", "create"))
	check("cannot write Employee Checkin", not frappe.has_permission("Employee Checkin", "write"))
	check("cannot delete Employee Checkin", not frappe.has_permission("Employee Checkin", "delete"))
	check("cannot write Shift Type", not frappe.has_permission("Shift Type", "write"))
	check("cannot read Biometric Enrolment", not frappe.has_permission("Biometric Enrolment", "read"))
	check("cannot read Employee", not frappe.has_permission("Employee", "read"))
	check("cannot read Salary Slip", not frappe.has_permission("Salary Slip", "read"))


def test_rejects_outsiders():
	print("Outsiders:")
	frappe.set_user("Guest")
	try:
		api.push([_punch_dict(fixtures.ID_OK, 43)])
		check("a user without the role is refused", False, "push succeeded for Guest")
	except frappe.PermissionError:
		check("a user without the role is refused", True)
	except Exception as e:
		check("a user without the role is refused", False, f"{type(e).__name__}: {e}")
	frappe.set_user("Administrator")


def test_batch_limits():
	print("Batch limits:")
	frappe.set_user(USER)
	try:
		api.push([_punch_dict(fixtures.ID_OK, 1)] * (api.MAX_BATCH + 1))
		check("an oversized batch is refused", False)
	except Exception as e:
		check("an oversized batch is refused", "Batch too large" in str(e), str(e)[:120])


def _measure_floor(sample=40):
	"""What the same work costs with no engine at all: one Employee Checkin plus
	one Biometric Punch per punch, inserted bare. This is the number the engine
	is measured against — a wall-clock constant would only measure how busy the
	box is."""
	frappe.set_user("Administrator")
	employee = frappe.get_all("Employee", filters={"status": "Active"}, pluck="name", limit=1)[0]
	made, staged = [], []
	started = time.time()
	for i in range(sample):
		checkin = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": employee,
				"time": add_to_date(now_datetime(), minutes=-(9000 + i)),
				"device_id": f"{fixtures.PREFIX}-FLOOR",
				"log_type": "IN",
				"latitude": 9.9252,
				"longitude": 78.1198,
			}
		)
		checkin.insert(ignore_permissions=True)
		made.append(checkin.name)
		punch = frappe.get_doc(
			{
				"doctype": "Biometric Punch",
				"punch_key": f"{fixtures.PREFIX}-floor-{i}",
				"status": "Pending",
				"device_serial": f"{fixtures.PREFIX}-FLOOR",
				"device_user_id": "0",
				"punch_time": add_to_date(now_datetime(), minutes=-(9000 + i)),
			}
		)
		punch.insert(ignore_permissions=True)
		staged.append(punch.name)
	elapsed = time.time() - started
	for name in made:
		frappe.delete_doc("Employee Checkin", name, force=True, ignore_permissions=True, delete_permanently=True)
	for name in staged:
		frappe.delete_doc("Biometric Punch", name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	return elapsed / sample


def test_throughput():
	"""Per PRD §11.1: the target is the framework floor, not a constant. About
	80% of the cost is inside HRMS's own controller, and bypassing it is
	forbidden — so what we assert is that the engine adds little to it."""
	print("Throughput:")
	floor = _measure_floor()

	frappe.set_user(USER)
	count = 100
	batch = [_punch_dict(fixtures.ID_OK, 1000 + i) for i in range(count)]
	started = time.time()
	out = api.push(batch, connector=CONNECTOR)
	elapsed = time.time() - started
	per_punch = elapsed / count

	check(f"all {count} punches were ingested", out["received"] == count and out["mapped"] == count, out)
	print(
		f"        engine {per_punch * 1000:.0f} ms/punch ({1 / per_punch:.0f}/s)   "
		f"floor {floor * 1000:.0f} ms/punch ({1 / floor:.0f}/s)   "
		f"overhead {((per_punch / floor) - 1) * 100:+.0f}%"
	)
	# The engine necessarily does more than the floor: a device lookup, an
	# enrolment lookup, and an update of the staged row once the checkin exists —
	# three document writes where the floor does two.
	#
	# The band is deliberately loose. This runs on a shared bench whose load we do
	# not control, and both figures move together by a factor of two between runs;
	# a tight ratio would be measuring the box, not the code. What it is here to
	# catch is an order-of-magnitude regression — a query that has crept inside the
	# per-punch loop — and for that 3x is plenty. The printed ratio above is the
	# number worth reading.
	check(
		"the engine stays within 3x the bare-insert floor",
		per_punch <= floor * 3.0,
		f"{per_punch * 1000:.0f}ms vs floor {floor * 1000:.0f}ms",
	)


def run():
	print("Biometric Hub — punch API and least privilege (Phase 3)\n")
	active, left = fixtures._employees()
	teardown()
	fixtures.setup(active, left)
	setup_user()
	setup_connector()
	try:
		test_push_as_integration_user()
		test_replay_is_free()
		test_heartbeat_and_bootstrap()
		test_least_privilege()
		test_rejects_outsiders()
		test_batch_limits()
		test_throughput()
	finally:
		teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_api: {len(failed)} check(s) failed")
	print("All good.")
