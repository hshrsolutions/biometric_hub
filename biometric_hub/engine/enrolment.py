"""Enrolment side effects.

The second file in `engine/` permitted to write an HRMS document, and it makes
exactly one such write: mirroring a global enrolment onto
`Employee.attendance_device_id`.

Why mirror at all: HRMS's own `add_log_based_on_employee_field` reads that field,
and so does anything already pointed at it. Keeping it in step means installing
this app never breaks a sync that was already working — and removing this app
leaves the site exactly as it was found.
"""

import frappe


def sync_attendance_device_id(enrolment):
	"""Mirror a global, enabled enrolment onto the Employee.

	Device-specific enrolments are deliberately not mirrored: a single scalar on
	Employee cannot express "this ID, but only on that reader", and writing one
	of several IDs there would be worse than writing none.
	"""
	if enrolment.device:
		return None
	if enrolment.flags.get("from_employee_ids"):
		# Employee → Biometric IDs sets attendance_device_id itself, on the same save.
		return None
	if not enrolment.enabled:
		# Disabling a mapping has to actually unmap. The engine falls back to
		# Employee.attendance_device_id when no enrolment matches, so leaving the
		# mirror in place would mean a disabled enrolment kept resolving.
		return clear_attendance_device_id(enrolment)
	if not enrolment.employee or not enrolment.device_user_id:
		return None

	current = frappe.db.get_value("Employee", enrolment.employee, "attendance_device_id")
	if current == enrolment.device_user_id:
		return None

	# Never overwrite an ID the employee already has. Two global enrolments for one
	# person is a contradiction a single scalar cannot hold, and silently replacing
	# the old value would break the legacy lookup for it. Say so and leave it.
	if current:
		frappe.msgprint(
			frappe._(
				"{0} already has attendance device ID {1}; leaving it as it is. "
				"Disable or delete the other enrolment first if {2} is meant to replace it."
			).format(enrolment.employee, current, enrolment.device_user_id),
			alert=True,
		)
		return None

	# Never take an ID off another employee. If two enrolments disagree, that is a
	# data problem for a human, not something to resolve by overwriting.
	held_by = frappe.db.get_value(
		"Employee",
		{"attendance_device_id": enrolment.device_user_id, "name": ["!=", enrolment.employee]},
		"name",
	)
	if held_by:
		frappe.msgprint(
			frappe._("Device user ID {0} is already on Employee {1}; not mirroring it onto {2}.").format(
				enrolment.device_user_id, held_by, enrolment.employee
			),
			alert=True,
		)
		return None

	frappe.db.set_value(
		"Employee", enrolment.employee, "attendance_device_id", enrolment.device_user_id,
		update_modified=False,
	)
	return enrolment.employee


def clear_attendance_device_id(enrolment):
	"""Take the mirror back off the Employee when the global enrolment that put it
	there is deleted or disabled.

	Only ever clears a value this enrolment is responsible for — if the Employee
	now carries a different ID, somebody else set it and it is not ours to
	remove.
	"""
	if enrolment.device or not enrolment.employee or not enrolment.device_user_id:
		return None
	current = frappe.db.get_value("Employee", enrolment.employee, "attendance_device_id")
	if current != enrolment.device_user_id:
		return None
	frappe.db.set_value(
		"Employee", enrolment.employee, "attendance_device_id", None, update_modified=False
	)
	return enrolment.employee


def adopt_employee_device_id(doc, method=None):
	"""`Employee.on_update`: HR set a device ID straight on the Employee form.

	That is how most HR teams already work, so it must behave like mapping in the
	workbench: make the global enrolment, then replay every punch that was parked
	as Unmapped behind that ID. Only our own doctype is written here — the
	Employee is read, never changed. An existing enrolment for the ID, even one
	pointing at someone else, is left alone: that is a conflict for a person to
	resolve in the workbench, not something to overwrite silently.

	Never blocks the Employee save.
	"""
	device_user_id = (doc.get("attendance_device_id") or "").strip()
	if not device_user_id or frappe.flags.in_install or frappe.flags.in_migrate:
		return
	try:
		from biometric_hub.biometric_hub.doctype.biometric_enrolment.biometric_enrolment import enrolment_key

		key = enrolment_key(device_user_id)
		if frappe.db.exists("Biometric Enrolment", {"enrolment_key": key}):
			return
		frappe.get_doc(
			{
				"doctype": "Biometric Enrolment",
				"employee": doc.name,
				"device_user_id": device_user_id,
				"enabled": 1,
				"enrolment_key": key,
				"notes": "Created from Employee.attendance_device_id.",
			}
		).insert(ignore_permissions=True)
		frappe.enqueue(
			"biometric_hub.engine.ingest.replay_unmapped",
			device_user_id=device_user_id,
			enqueue_after_commit=True,
		)
	except Exception:
		frappe.log_error(title=f"Biometric Hub: could not enrol {doc.name} from attendance_device_id")
