"""Phase 7 acceptance: push-mode / ADMS devices.

	bench --site <site> execute biometric_hub.tests.test_adms.run

The endpoints are exercised over real HTTP, as a device would, because the thing
most likely to break is the response shape rather than the parsing.
"""

import frappe
import requests
from frappe.utils import add_to_date, now_datetime

from biometric_hub.api.adms import parse_attlog
from biometric_hub.tests import test_ingest as fixtures

BASE = "http://127.0.0.1:8001"
PUSH_SERIAL = f"{fixtures.PREFIX}-PUSH-SN"
PUSH_DEVICE = f"{fixtures.PREFIX} Push Device"
GHOST_SERIAL = f"{fixtures.PREFIX}-NEVER-REGISTERED"

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def _url(method):
	return f"{BASE}/api/method/biometric_hub.api.adms.{method}"


def _headers():
	return {"Host": frappe.local.site}


def _setup(active):
	if not frappe.db.exists("Biometric Driver", "ZKTeco Push (ADMS)"):
		from biometric_hub.drivers import registry

		registry.sync()
	frappe.get_doc(
		{
			"doctype": "Biometric Device",
			"device_name": PUSH_DEVICE,
			"enabled": 1,
			"driver": "ZKTeco Push (ADMS)",
			"connectivity_mode": "Push",
			"serial_no": PUSH_SERIAL,
			"latitude": 9.9252,
			"longitude": 78.1198,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def _teardown():
	for punch in frappe.get_all(
		"Biometric Punch", filters={"device_serial": ["like", f"{fixtures.PREFIX}%"]}, pluck="name"
	):
		checkin = frappe.db.get_value("Biometric Punch", punch, "employee_checkin")
		frappe.delete_doc("Biometric Punch", punch, force=True, ignore_permissions=True, delete_permanently=True)
		if checkin and frappe.db.exists("Employee Checkin", checkin):
			frappe.delete_doc("Employee Checkin", checkin, force=True, ignore_permissions=True, delete_permanently=True)
	for name in frappe.get_all("Biometric Device", filters={"device_name": PUSH_DEVICE}, pluck="name"):
		frappe.delete_doc("Biometric Device", name, force=True, ignore_permissions=True)
	for name in frappe.get_all(
		"Biometric Sync Log", filters={"message": ["like", f"%{fixtures.PREFIX}%"]}, pluck="name"
	):
		frappe.delete_doc("Biometric Sync Log", name, force=True, ignore_permissions=True)
	fixtures.teardown()
	frappe.db.commit()


# ---------------------------------------------------------------------- tests


def test_parse():
	print("ATTLOG parsing:")
	body = (
		"1434\t2026-08-31 09:00:00\t0\t1\t0\t0\t0\n"
		"1530\t2026-08-31 17:30:00\t1\t1\t0\t0\t0\n"
		"\n"
		"garbage\n"
		"137\t2026-08-31 09:05:00\n"
	)
	punches = parse_attlog("SN-X", body)
	check("well-formed lines parse", len(punches) == 3, [p.device_user_id for p in punches])
	check("blank lines are skipped", all(p.device_user_id for p in punches))
	check("a line with no timestamp is skipped", "garbage" not in [p.device_user_id for p in punches])
	check("the punch code is passed through", punches[0].raw_log_type == "0", punches[0].raw_log_type)
	check("a line without a punch code still parses", punches[2].raw_log_type is None)
	check("the raw line is kept for audit", "1434" in punches[0].payload["raw"])


def test_unregistered_is_refused():
	print("Unregistered device:")
	before = frappe.db.count("Biometric Sync Log")
	response = requests.get(_url("cdata"), params={"SN": GHOST_SERIAL, "options": "all"},
	                        headers=_headers(), timeout=30)
	check("an unregistered serial is refused", response.status_code == 401, response.status_code)
	check("and told why", "not registered" in response.text, response.text[:120])

	frappe.db.rollback()
	after = frappe.db.count("Biometric Sync Log")
	check("the attempt is recorded where someone will see it", after > before, f"{before} -> {after}")
	logged = frappe.get_all(
		"Biometric Sync Log", filters={"message": ["like", f"%{GHOST_SERIAL}%"]}, pluck="name"
	)
	check("and the record names the serial", bool(logged), logged)


def test_handshake():
	print("Handshake:")
	response = requests.get(_url("cdata"), params={"SN": PUSH_SERIAL, "options": "all", "pushver": "2.4.1"},
	                        headers=_headers(), timeout=30)
	check("a registered device gets its options", response.status_code == 200, response.status_code)
	check("the body is the ADMS option block", "GET OPTION FROM" in response.text, response.text[:120])
	check("realtime push is requested", "Realtime=1" in response.text)
	check("the response is plain text, not JSON", "text/plain" in response.headers.get("Content-Type", ""),
	      response.headers.get("Content-Type"))

	frappe.db.rollback()
	check("the device is now marked seen", bool(frappe.db.get_value("Biometric Device", PUSH_DEVICE, "last_contact")))


def test_attlog_upload():
	print("Punch upload:")
	when = add_to_date(now_datetime(), minutes=-90)
	body = "\n".join(
		f"{fixtures.ID_OK}\t{add_to_date(when, minutes=i).strftime('%Y-%m-%d %H:%M:%S')}\t{i % 2}\t1\t0\t0\t0"
		for i in range(3)
	)
	response = requests.post(
		_url("cdata"), params={"SN": PUSH_SERIAL, "table": "ATTLOG", "Stamp": "9999"},
		data=body.encode(), headers={**_headers(), "Content-Type": "text/plain"}, timeout=60,
	)
	check("the device gets OK with a count", response.status_code == 200 and "OK: 3" in response.text,
	      f"{response.status_code} {response.text[:120]}")

	frappe.db.rollback()
	mapped = frappe.db.count("Biometric Punch", {"device_serial": PUSH_SERIAL, "status": "Mapped"})
	check("all three became checkins", mapped == 3, mapped)
	check("with the device recorded on them",
	      frappe.db.count("Employee Checkin", {"device_id": PUSH_DEVICE}) == 3)

	# A device that re-sends after a failed acknowledgement must not double up.
	requests.post(
		_url("cdata"), params={"SN": PUSH_SERIAL, "table": "ATTLOG"},
		data=body.encode(), headers={**_headers(), "Content-Type": "text/plain"}, timeout=60,
	)
	frappe.db.rollback()
	check("re-sending the same block creates no duplicate checkin",
	      frappe.db.count("Employee Checkin", {"device_id": PUSH_DEVICE}) == 3)


def test_other_tables_acknowledged():
	print("Other tables:")
	response = requests.post(
		_url("cdata"), params={"SN": PUSH_SERIAL, "table": "OPERLOG"},
		data=b"anything", headers={**_headers(), "Content-Type": "text/plain"}, timeout=30,
	)
	check("operation logs are acknowledged, not stored", response.status_code == 200 and "OK" in response.text,
	      response.text[:80])


def test_getrequest():
	print("Command polling:")
	response = requests.get(_url("getrequest"), params={"SN": PUSH_SERIAL}, headers=_headers(), timeout=30)
	check("a registered device is told there is nothing to do", "OK" in response.text, response.text[:80])
	response = requests.get(_url("getrequest"), params={"SN": GHOST_SERIAL}, headers=_headers(), timeout=30)
	check("an unregistered device is refused here too", response.status_code == 401, response.status_code)


def run():
	print("Biometric Hub — push-mode / ADMS (Phase 7)\n")
	active, left = fixtures._employees()
	_teardown()
	fixtures.setup(active, left)
	_setup(active)
	try:
		test_parse()
		test_unregistered_is_refused()
		test_handshake()
		test_attlog_upload()
		test_other_tables_acknowledged()
		test_getrequest()
	finally:
		_teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_adms: {len(failed)} check(s) failed")
	print("All good.")
