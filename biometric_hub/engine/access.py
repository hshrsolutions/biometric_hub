"""Employee device access — which machines, and whether the mobile app, a person
may use to check in.

HR keeps one `Biometric Employee Access` record per employee. No record means no
restriction, so installing this changes nothing until HR starts mapping people.

Two channels are governed:

  * **Biometric devices.** With "Allowed Devices Only", a punch from any machine
    not on the employee's list is staged as `Blocked` — never dropped — and
    becomes a checkin by itself as soon as HR adds that machine (see
    `on_access_change`, which replays it).
  * **The mobile app.** An employee checking *themselves* in (the /hrms app, or
    the desk) is refused when HR has switched mobile check-in off for them. HR
    recording a checkin on someone's behalf is never blocked, and neither is
    anything this app's engine writes.

Writes here touch only our own doctypes (`Biometric Enrolment`). The HRMS
`Employee Checkin` hook is a validation: it reads, it never writes.
"""

import frappe
from frappe import _

ANY = "Any Device"
ONLY = "Allowed Devices Only"

MANAGED_NOTE = "Managed by Employee Device Access {0} — edit it there."


# ------------------------------------------------------------------- policy


def get_policy(employee, cache=None):
	"""`None` when the employee has no access record (unrestricted), else a dict
	with `mode`, `devices` (a set of device names) and `mobile`."""
	if cache is not None and employee in cache:
		return cache[employee]

	policy = None
	row = frappe.db.get_value(
		"Biometric Employee Access", {"employee": employee},
		["name", "access_mode", "allow_mobile_checkin"], as_dict=True,
	)
	if row:
		policy = frappe._dict(
			name=row.name,
			mode=row.access_mode or ANY,
			mobile=bool(row.allow_mobile_checkin),
			devices=set(
				frappe.get_all(
					"Biometric Access Device", filters={"parent": row.name, "parenttype": "Biometric Employee Access"},
					pluck="device",
				)
			),
		)
	if cache is not None:
		cache[employee] = policy
	return policy


def check_device(employee, device_name, cache=None):
	"""(allowed, reason). Called by the ingestion engine once the punch is mapped."""
	policy = get_policy(employee, cache)
	if not policy or policy.mode != ONLY:
		return True, None
	if device_name in policy.devices:
		return True, None
	allowed = ", ".join(sorted(policy.devices)) or (_("none — mobile app only") if policy.mobile else _("none"))
	return False, _(
		"{0} is not allowed to punch on device {1} (allowed: {2}). "
		"Add the device in Employee Device Access {3} to release this punch."
	).format(employee, device_name, allowed, policy.name)


# ---------------------------------------------------------- mobile app hook


def check_self_service_checkin(doc, method=None):
	"""`Employee Checkin.before_insert`.

	Only a person checking *themselves* in is governed — that is what the mobile
	app does. Everything else passes untouched: this app's own engine, data
	import, patches, and HR or an integration recording a checkin for someone.
	"""
	if doc.flags.get("biometric_hub") or frappe.flags.in_import or frappe.flags.in_install \
			or frappe.flags.in_migrate or frappe.flags.in_patch:
		return
	if not doc.employee or frappe.session.user in ("Administrator", "Guest"):
		return

	own_user = frappe.db.get_value("Employee", doc.employee, "user_id")
	if not own_user or own_user != frappe.session.user:
		return

	policy = get_policy(doc.employee)
	if policy and not policy.mobile:
		frappe.throw(
			_("Mobile check-in is turned off for you. Please punch on the attendance machine, "
			  "or ask HR if you think this is wrong."),
			title=_("Check-in not allowed"),
		)


# ---------------------------------------------------- access record upkeep


def validate_access(doc):
	"""Called from the doctype controller before save."""
	has_global_id = bool(
		frappe.db.exists("Biometric Enrolment", {"employee": doc.employee, "device": ["is", "not set"], "enabled": 1})
		or frappe.db.get_value("Employee", doc.employee, "attendance_device_id")
	)
	seen = set()
	for row in doc.devices:
		if row.device in seen:
			frappe.throw(_("Row {0}: device {1} is listed twice.").format(row.idx, row.device))
		seen.add(row.device)
		row.device_user_id = (row.device_user_id or "").strip() or None

		if not row.device_user_id:
			if not has_global_id:
				frappe.msgprint(
					_("Row {0}: {1} has no usual device ID and none is given for {2}, so punches "
					  "there cannot be matched to them yet.").format(row.idx, doc.employee, row.device),
					alert=True, indicator="orange",
				)
			continue
		# A device-specific ID must not already belong to somebody else on that
		# device. That is a data problem for a person, not something to overwrite.
		from biometric_hub.biometric_hub.doctype.biometric_enrolment.biometric_enrolment import enrolment_key

		owner = frappe.db.get_value(
			"Biometric Enrolment", {"enrolment_key": enrolment_key(row.device_user_id, row.device)},
			["name", "employee"], as_dict=True,
		)
		if owner and owner.employee != doc.employee:
			frappe.throw(
				_("Row {0}: ID {1} on device {2} already belongs to {3} (enrolment {4}).").format(
					row.idx, row.device_user_id, row.device, owner.employee, owner.name
				)
			)


def sync_enrolments(doc):
	"""Keep one device-specific `Biometric Enrolment` per row that carries its own
	ID, and remove the ones this record made for rows since deleted or cleared.

	Enrolments this record did not create — the global one, anything mapped from
	the workbench — are never touched.
	"""
	from biometric_hub.biometric_hub.doctype.biometric_enrolment.biometric_enrolment import enrolment_key

	note = MANAGED_NOTE.format(doc.name)
	keep = set()
	for row in doc.devices:
		if not row.device_user_id:
			if row.enrolment:
				row.enrolment = None
				row.db_set("enrolment", None, update_modified=False)
			continue

		key = enrolment_key(row.device_user_id, row.device)
		name = frappe.db.get_value("Biometric Enrolment", {"enrolment_key": key}, "name")
		if name:
			enrolment = frappe.get_doc("Biometric Enrolment", name)
			if not enrolment.enabled:
				enrolment.enabled = 1
				enrolment.save(ignore_permissions=True)
		else:
			enrolment = frappe.get_doc(
				{
					"doctype": "Biometric Enrolment",
					"employee": doc.employee,
					"device_user_id": row.device_user_id,
					"device": row.device,
					"enabled": 1,
					"notes": note,
				}
			).insert(ignore_permissions=True)
		keep.add(enrolment.name)
		if row.enrolment != enrolment.name:
			row.db_set("enrolment", enrolment.name, update_modified=False)

	stale = frappe.get_all(
		"Biometric Enrolment",
		filters={"employee": doc.employee, "notes": note, "name": ["not in", list(keep) or [""]]},
		pluck="name",
	)
	for name in stale:
		# force: our own child row still links to it while this runs.
		frappe.delete_doc("Biometric Enrolment", name, ignore_permissions=True, force=True)
	return {"kept": len(keep), "removed": len(stale)}


def remove_enrolments(doc):
	"""The access record is being deleted: take its managed enrolments with it."""
	for name in frappe.get_all(
		"Biometric Enrolment", filters={"employee": doc.employee, "notes": MANAGED_NOTE.format(doc.name)}, pluck="name"
	):
		# force: our own child row still links to it while this runs.
		frappe.delete_doc("Biometric Enrolment", name, ignore_permissions=True, force=True)


def on_access_change(doc):
	"""After HR saves: release punches that the new rules now allow, and pick up
	punches that parked as Unmapped behind a newly added per-device ID."""
	frappe.enqueue(
		"biometric_hub.engine.ingest.replay_blocked",
		employee=doc.employee,
		enqueue_after_commit=True,
	)
	for row in doc.devices:
		if row.device_user_id:
			frappe.enqueue(
				"biometric_hub.engine.ingest.replay_unmapped",
				device_user_id=row.device_user_id,
				enqueue_after_commit=True,
			)
