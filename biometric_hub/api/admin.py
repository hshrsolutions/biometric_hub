"""Desk actions. Manager-facing, never called by an agent."""

import frappe
from frappe import _

from biometric_hub.engine import ingest as engine


def _require_manager():
	if not ({"Biometric Manager", "System Manager"} & set(frappe.get_roles())):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


def _driver_for(device_name):
	from biometric_hub.drivers.registry import get_driver

	return get_driver(frappe.db.get_value("Biometric Device", device_name, "driver"))


@frappe.whitelist()
def test_device(device):
	_require_manager()
	driver = _driver_for(device)
	doc = frappe.get_doc("Biometric Device", device)
	try:
		ok, message = driver.test_connection(doc)
	except Exception as e:
		ok, message = False, f"{type(e).__name__}: {e}"
	frappe.db.set_value(
		"Biometric Device", device,
		{"health_status": "Online" if ok else "Error", "last_error": None if ok else message[:500]},
		update_modified=False,
	)
	return {"ok": ok, "message": message}


@frappe.whitelist()
def sync_now(device, since=None):
	"""One pull run, on demand."""
	_require_manager()
	doc = frappe.get_doc("Biometric Device", device)
	driver = _driver_for(device)
	if not driver.supports_pull:
		frappe.throw(_("Driver {0} does not support pulling.").format(doc.driver))

	punches = driver.fetch_punches(doc, since=frappe.utils.get_datetime(since) if since else None)
	return engine.ingest(punches, source="Poll")


@frappe.whitelist()
def map_unmapped(device_user_id, employee, device=None):
	"""Create the missing enrolment and re-run every punch parked behind it.

	This is the whole point of the unmapped queue: a mapping gap is a five-second
	fix, not 38,000 failed requests a day.
	"""
	_require_manager()

	if not frappe.db.exists("Employee", employee):
		frappe.throw(_("Unknown employee {0}.").format(employee))

	from biometric_hub.biometric_hub.doctype.biometric_enrolment.biometric_enrolment import enrolment_key

	key = enrolment_key(device_user_id, device)
	if not frappe.db.exists("Biometric Enrolment", {"enrolment_key": key}):
		frappe.get_doc(
			{
				"doctype": "Biometric Enrolment",
				"employee": employee,
				"device_user_id": device_user_id,
				"device": device,
				"enabled": 1,
				"notes": "Created from the unmapped punch queue.",
			}
		).insert(ignore_permissions=True)

	replayed = engine.replay_unmapped(device_user_id, device)
	return {"enrolment": key, **replayed}


@frappe.whitelist()
def unmapped_summary():
	"""Every unmapped device user ID with its punch count — the work queue."""
	_require_manager()
	return frappe.db.sql(
		"""
		SELECT device_user_id, device, COUNT(*) AS punches,
		       MIN(punch_time) AS first_punch, MAX(punch_time) AS last_punch
		FROM `tabBiometric Punch`
		WHERE status = 'Unmapped'
		GROUP BY device_user_id, device
		ORDER BY punches DESC
		""",
		as_dict=True,
	)


@frappe.whitelist()
def device_health():
	"""Every device, with how recently it was heard from and how much it sent."""
	_require_manager()
	return frappe.db.sql(
		"""
		SELECT d.name, d.location, d.health_status, d.last_contact, d.last_punch_time,
		       (SELECT COUNT(*) FROM `tabBiometric Punch` p
		         WHERE p.device = d.name AND p.punch_time >= %(since)s) AS punches_today
		FROM `tabBiometric Device` d
		WHERE d.enabled = 1
		ORDER BY FIELD(d.health_status, 'Error', 'Silent', 'Unknown', 'Online'), d.name
		""",
		{"since": frappe.utils.today()},
		as_dict=True,
	)


@frappe.whitelist()
def recent_activity(hours=24):
	"""Punch outcomes over the last day — the shape of what is going wrong."""
	_require_manager()
	since = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-frappe.utils.cint(hours))
	rows = frappe.db.sql(
		"""SELECT status, COUNT(*) AS n FROM `tabBiometric Punch`
		   WHERE creation >= %(since)s GROUP BY status""",
		{"since": since},
		as_dict=True,
	)
	counts = {r.status.lower(): r.n for r in rows}
	return {
		"mapped": counts.get("mapped", 0),
		"unmapped": counts.get("unmapped", 0),
		"blocked": counts.get("blocked", 0),
		"duplicates": counts.get("duplicate", 0),
		"errors": counts.get("error", 0) + counts.get("pending", 0),
	}
