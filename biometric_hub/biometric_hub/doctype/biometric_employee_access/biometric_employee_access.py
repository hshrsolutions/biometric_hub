# Copyright (c) 2026, GroHR and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BiometricEmployeeAccess(Document):
	"""Which biometric devices, and whether the mobile app, one employee may use.

	The rules live in engine/access.py; this controller only calls it.
	"""

	def validate(self):
		from biometric_hub.engine.access import validate_access

		validate_access(self)

	def on_update(self):
		from biometric_hub.engine.access import on_access_change, sync_enrolments

		sync_enrolments(self)
		on_access_change(self)

	def on_trash(self):
		from biometric_hub.engine.access import remove_enrolments

		remove_enrolments(self)
		# With the record gone the employee is unrestricted again, so anything
		# held back by it may now go through.
		frappe.enqueue(
			"biometric_hub.engine.ingest.replay_blocked",
			employee=self.employee,
			enqueue_after_commit=True,
		)


@frappe.whitelist()
def employee_overview(employee):
	"""Every device ID on file for the employee, and how their punches fared
	over the last 30 days per device — shown on the access form."""
	frappe.has_permission("Biometric Employee Access", "read", throw=True)
	enrolments = frappe.get_all(
		"Biometric Enrolment",
		filters={"employee": employee},
		fields=["name", "device_user_id", "device", "enabled", "valid_from", "valid_upto", "notes"],
		order_by="device asc",
	)
	since = frappe.utils.add_days(frappe.utils.today(), -30)
	punches = frappe.db.sql(
		"""SELECT device, status, COUNT(*) AS n, MAX(punch_time) AS last_punch
		   FROM `tabBiometric Punch`
		   WHERE employee = %s AND punch_time >= %s
		   GROUP BY device, status
		   ORDER BY device, status""",
		(employee, since),
		as_dict=True,
	)
	return {"enrolments": enrolments, "punches": punches}
