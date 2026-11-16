"""Employee → Biometric IDs: the list of every attendance-machine ID that counts
toward one employee's attendance, kept unique across employees.

The table (custom field `bh_biometric_ids`, child doctype `Biometric Employee ID`)
and `Biometric Enrolment` are two views of the same thing:

  * HR edits the table on the Employee form → enrolments are created / removed;
  * an enrolment made anywhere else (workbench, photo confirm, the Attendance
    Device ID field) → a row appears in the table.

Rows that Employee Device Access manages (its per-machine IDs) live on that form
and are left out here, but they still count for uniqueness.

Writes: our own child rows and `Biometric Enrolment` only. On the Employee's own
save, `attendance_device_id` is kept equal to the first all-machines ID — that is
part of HR's save, not a background write.
"""

import frappe
from frappe import _

FIELD = "bh_biometric_ids"
CHILD = "Biometric Employee ID"
MANAGED = "Managed by Employee Biometric IDs"
ACCESS_PREFIX = "Managed by Employee Device Access"


def _enabled():
	return frappe.get_meta("Employee").has_field(FIELD)


def _key(device_user_id, device=None):
	from biometric_hub.biometric_hub.doctype.biometric_enrolment.biometric_enrolment import enrolment_key

	return enrolment_key(device_user_id, device)


def _label(uid, device):
	return f"{uid} ({device})" if device else uid


# -------------------------------------------------------- Employee.validate


def validate(doc, method=None):
	if not _enabled() or frappe.flags.in_install or frappe.flags.in_migrate:
		return

	rows = doc.get(FIELD) or []
	for row in rows:
		row.device_user_id = (row.device_user_id or "").strip()
		row.device = row.device or None
	rows = [r for r in rows if r.device_user_id]
	doc.set(FIELD, rows)

	# The Attendance Device ID field and the table agree: a newly typed ID joins
	# the table; otherwise the field follows the first all-machines row.
	field_id = (doc.get("attendance_device_id") or "").strip()
	global_ids = [r.device_user_id for r in rows if not r.device]
	if field_id and field_id not in global_ids:
		before = doc.get_doc_before_save()
		if before is None or (before.get("attendance_device_id") or "") != field_id:
			doc.append(FIELD, {"device_user_id": field_id})
			global_ids.append(field_id)
		else:
			doc.attendance_device_id = global_ids[0] if global_ids else None
	elif not field_id and global_ids:
		doc.attendance_device_id = global_ids[0]

	seen = set()
	for row in doc.get(FIELD):
		key = (row.device_user_id, row.device)
		if key in seen:
			frappe.throw(_("Biometric ID {0} is listed twice.").format(_label(*key)))
		seen.add(key)
		_check_unique(doc.name, row)


def _check_unique(employee, row):
	"""One ID, one person. A clash is a mistake for HR to resolve, never to overwrite."""
	owner = frappe.db.get_value(
		"Biometric Enrolment", {"enrolment_key": _key(row.device_user_id, row.device)}, ["employee", "name"], as_dict=True
	)
	if owner and owner.employee != employee:
		frappe.throw(
			_("Biometric ID {0} already belongs to {1} ({2}). Each ID can belong to only one employee.").format(
				_label(row.device_user_id, row.device), owner.employee,
				frappe.db.get_value("Employee", owner.employee, "employee_name") or "",
			),
			title=_("Biometric ID already used"),
		)
	if not row.device:
		holder = frappe.db.get_value(
			"Employee", {"attendance_device_id": row.device_user_id, "name": ["!=", employee]}, "name"
		)
		if holder:
			frappe.throw(
				_("Biometric ID {0} is the Attendance Device ID of {1}. Each ID can belong to only one employee.").format(
					row.device_user_id, holder),
				title=_("Biometric ID already used"),
			)


# ------------------------------------------------------- Employee.on_update


def sync(doc, method=None):
	"""Make the employee's enrolments match the table, then replay punches that
	were parked as Unmapped behind any newly added ID."""
	if not _enabled() or frappe.flags.in_install or frappe.flags.in_migrate:
		return

	wanted = {(r.device_user_id, r.device or None): r for r in doc.get(FIELD) or []}
	before = doc.get_doc_before_save()
	previous = {(r.device_user_id, r.device or None) for r in (before.get(FIELD) if before else []) or []}

	added = []
	for (uid, device), row in wanted.items():
		name = frappe.db.get_value("Biometric Enrolment", {"enrolment_key": _key(uid, device)}, ["name", "enabled"], as_dict=True)
		if not name:
			enr = frappe.get_doc(
				{"doctype": "Biometric Enrolment", "employee": doc.name, "device_user_id": uid,
				 "device": device, "enabled": 1, "notes": MANAGED}
			)
			enr.flags.from_employee_ids = True
			enr.insert(ignore_permissions=True)
			name = frappe._dict(name=enr.name, enabled=1)
			added.append(uid)
		elif not name.enabled:
			frappe.db.set_value("Biometric Enrolment", name.name, "enabled", 1)
			added.append(uid)
		if row.enrolment != name.name:
			row.db_set("enrolment", name.name, update_modified=False)

	# Rows HR removed: unlink those IDs (never Device Access's own rows).
	for uid, device in previous - set(wanted):
		name = frappe.db.get_value(
			"Biometric Enrolment", {"enrolment_key": _key(uid, device), "employee": doc.name}, ["name", "notes"], as_dict=True
		)
		if name and not (name.notes or "").startswith(ACCESS_PREFIX):
			enr = frappe.get_doc("Biometric Enrolment", name.name)
			enr.flags.from_employee_ids = True
			enr.delete(ignore_permissions=True, force=True)

	for uid in added:
		frappe.enqueue("biometric_hub.engine.ingest.replay_unmapped", device_user_id=uid, enqueue_after_commit=True)


# --------------------------------------------- enrolments made elsewhere


def on_enrolment_insert(enrolment):
	"""An ID linked from anywhere else shows up in the employee's table."""
	if enrolment.flags.get("from_employee_ids") or not _enabled():
		return
	if (enrolment.notes or "").startswith(ACCESS_PREFIX) or not enrolment.employee:
		return
	exists = frappe.db.exists(CHILD, {"parent": enrolment.employee, "parenttype": "Employee",
	                                  "device_user_id": enrolment.device_user_id, "device": enrolment.device or ["is", "not set"]})
	if exists:
		frappe.db.set_value(CHILD, exists, "enrolment", enrolment.name, update_modified=False)
		return
	_insert_row(enrolment.employee, enrolment.device_user_id, enrolment.device, enrolment.name)


def on_enrolment_trash(enrolment):
	if enrolment.flags.get("from_employee_ids") or not _enabled():
		return
	for name in frappe.get_all(CHILD, filters={"parent": enrolment.employee, "parenttype": "Employee",
	                                           "enrolment": enrolment.name}, pluck="name"):
		frappe.db.delete(CHILD, {"name": name})


def _insert_row(employee, uid, device, enrolment):
	idx = (frappe.db.sql(f"SELECT MAX(idx) FROM `tab{CHILD}` WHERE parent=%s AND parenttype='Employee'", employee)[0][0] or 0) + 1
	frappe.get_doc(
		{"doctype": CHILD, "parent": employee, "parenttype": "Employee", "parentfield": FIELD, "idx": idx,
		 "device_user_id": uid, "device": device, "enrolment": enrolment}
	).db_insert()


# -------------------------------------------------------------- backfill


def backfill():
	"""Fill every employee's table from the enrolments they already have.
	Idempotent: a row that exists is left alone. Run by the scaffold."""
	if not _enabled():
		return 0
	added = 0
	for e in frappe.get_all(
		"Biometric Enrolment", filters={"employee": ["is", "set"]},
		fields=["name", "employee", "device_user_id", "device", "notes"], order_by="device asc, creation asc",
		limit_page_length=0,
	):
		if (e.notes or "").startswith(ACCESS_PREFIX):
			continue
		if frappe.db.exists(CHILD, {"parent": e.employee, "parenttype": "Employee", "enrolment": e.name}):
			continue
		_insert_row(e.employee, e.device_user_id, e.device, e.name)
		added += 1
	return added
