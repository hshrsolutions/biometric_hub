"""Device health, the nightly reconcile, and retention.

The failure this exists to prevent is the one at a customer: an integration that
stopped working properly and stayed broken for months because nothing anywhere
said so. A device that has gone quiet, a punch that never became a checkin, and
a sync that has not run are all *states someone should be told about*, not
things to be discovered when payroll comes out wrong.
"""

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime


def _settings():
	return frappe.get_cached_doc("Biometric Settings")


# --------------------------------------------------------------------- health


def check_devices():
	"""Move devices between Online and Silent, and email once on the way down.

	Once, not every run: an alert that repeats every fifteen minutes is an alert
	people filter, and a filtered alert is the same as no alert.
	"""
	settings = _settings()
	now = now_datetime()
	went_silent = []

	for device in frappe.get_all(
		"Biometric Device",
		filters={"enabled": 1},
		fields=["name", "last_contact", "heartbeat_grace_minutes", "health_status", "alert_recipients"],
		limit_page_length=0,
	):
		grace = cint(device.heartbeat_grace_minutes)
		if not grace:
			continue  # alerting switched off for this device

		deadline = add_to_date(now, minutes=-grace)
		silent = not device.last_contact or get_datetime(device.last_contact) < deadline

		if silent and device.health_status != "Silent":
			frappe.db.set_value("Biometric Device", device.name, "health_status", "Silent", update_modified=False)
			went_silent.append(device)
		elif not silent and device.health_status == "Silent":
			frappe.db.set_value("Biometric Device", device.name, "health_status", "Online", update_modified=False)

	if cint(settings.alert_on_silent_device):
		for device in went_silent:
			_alert_silent(device)

	frappe.db.commit()
	return [d.name for d in went_silent]


def _alert_silent(device):
	recipients = [r.strip() for r in (device.alert_recipients or "").split(",") if r.strip()]
	if not recipients:
		return
	last_contact = device.last_contact or "never"
	frappe.sendmail(
		recipients=recipients,
		subject=f"Biometric device {device.name} has gone quiet",
		message=(
			f"<p><b>{frappe.utils.escape_html(device.name)}</b> has not been heard from since "
			f"<b>{last_contact}</b>.</p>"
			f"<p>Punches from this reader are not reaching the system, and attendance for the "
			f"people who use it will be incomplete until it is back.</p>"
			f"<p>Worth checking: is the device powered and on the network, and is the connector "
			f"agent for its location still running?</p>"
		),
		reference_doctype="Biometric Device",
		reference_name=device.name,
	)


# ------------------------------------------------------------------ reconcile


def reconcile():
	"""Retry punches that were staged but never resolved.

	The safety net for a punch that arrived while the site was mid-migrate, or
	during a transient failure. `Unmapped` is deliberately never included: it is
	terminal, and retrying it would be exactly the retry storm this app exists to
	stop. It is fixed by a human in the workbench, not by trying again.
	"""
	settings = _settings()
	if not cint(settings.reconcile_enabled):
		return {"considered": 0, "resolved": 0, "skipped": "disabled"}
	if not cint(settings.create_employee_checkin):
		return {"considered": 0, "resolved": 0, "skipped": "checkin creation is off"}

	since = add_to_date(now_datetime(), days=-cint(settings.reconcile_lookback_days or 3))
	names = frappe.get_all(
		"Biometric Punch",
		filters={"status": ["in", ["Pending", "Error"]], "creation": [">=", since]},
		pluck="name",
		limit_page_length=0,
	)

	from biometric_hub.engine.ingest import reprocess

	resolved = 0
	for name in names:
		if reprocess(name).get("status") == "Mapped":
			resolved += 1
	frappe.db.commit()
	return {"considered": len(names), "resolved": resolved}


# ------------------------------------------------------------------ retention


def purge():
	"""Delete punches that have done their job.

	`Mapped` and `Duplicate` are safe to lose — the `Employee Checkin` is the
	record of consequence and it stays. `Unmapped` and `Error` are never purged:
	they are unfinished work, and deleting unfinished work is how a problem
	becomes invisible.
	"""
	settings = _settings()
	days = cint(settings.punch_retention_days)
	if not days:
		return {"deleted": 0, "skipped": "retention disabled"}

	cutoff = add_to_date(now_datetime(), days=-days)
	names = frappe.get_all(
		"Biometric Punch",
		filters={"status": ["in", ["Mapped", "Duplicate"]], "creation": ["<", cutoff]},
		pluck="name",
		limit_page_length=0,
	)
	for name in names:
		frappe.delete_doc("Biometric Punch", name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	return {"deleted": len(names)}


# ------------------------------------------------------------------ scheduled


def hourly():
	check_devices()


def daily():
	reconcile()
	purge()
