# Copyright (c) 2026, GroHR and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BiometricUserSync(Document):
	"""One request to create, update or remove an employee in Easy Time Pro. Logic
	lives in engine/user_push.py; these are thin, permission-checked callers."""


def _can_edit():
	frappe.has_permission("Biometric User Sync", "write", throw=True)


@frappe.whitelist()
def overwrite(name):
	"""HR checked: the user Easy Time Pro already has under this ID is this employee."""
	_can_edit()
	from biometric_hub.engine.user_push import overwrite as do

	return do(name)


@frappe.whitelist()
def retry(name):
	_can_edit()
	from biometric_hub.engine.user_push import retry as do

	return do(name)


@frappe.whitelist()
def requeue_status(status):
	_can_edit()
	from biometric_hub.engine.user_push import requeue_status as do

	return do(status)


@frappe.whitelist()
def preview_all(branch=None):
	_can_edit()
	from biometric_hub.engine.user_push import enabled, preview_all as do

	return {**do(branch or None), "enabled": enabled()}


@frappe.whitelist()
def push_all(branch=None):
	_can_edit()
	from biometric_hub.engine.user_push import enabled, push_all as do

	if not enabled():
		frappe.throw(frappe._("Switch on Biometric Settings → Push Employees to Easy Time Pro first."))
	return do(branch or None)


@frappe.whitelist()
def push_employee(employee):
	"""Employee form → Attendance → Send to Easy Time Pro."""
	_can_edit()
	frappe.has_permission("Employee", "read", doc=employee, throw=True)
	from biometric_hub.engine.user_push import enabled, global_ids, queue_employee

	if not enabled():
		frappe.throw(frappe._("Switch on Biometric Settings → Push Employees to Easy Time Pro first."))
	if not global_ids(employee):
		frappe.throw(frappe._("This employee has no biometric ID that counts on all machines."))
	names = queue_employee(employee, frappe._("Sent from the Employee form."))
	if not names:
		frappe.throw(frappe._("Only an active employee can be sent."))
	return names


@frappe.whitelist()
def employee_status(employee):
	frappe.has_permission("Biometric User Sync", "read", throw=True)
	from biometric_hub.engine.user_push import employee_status as do

	return do(employee)
