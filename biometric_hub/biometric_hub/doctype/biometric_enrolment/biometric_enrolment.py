# Copyright (c) 2026, GroHR and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BiometricEnrolment(Document):
	def validate(self):
		# The uniqueness of (device_user_id, device) is carried by this derived
		# column. Computing it here rather than only in the bootstrap means a row
		# created by hand in the desk is held to the same rule.
		self.enrolment_key = enrolment_key(self.device_user_id, self.device)

		if self.valid_from and self.valid_upto and self.valid_from > self.valid_upto:
			frappe.throw(frappe._("Valid Upto cannot be before Valid From."))

	def on_update(self):
		# Keep HRMS's own field in step, so installing this app never breaks a
		# sync that already worked. See engine/enrolment.py for why only global
		# enrolments are mirrored.
		from biometric_hub.engine.enrolment import sync_attendance_device_id

		sync_attendance_device_id(self)

		# A new or changed ID asks the office PC for that user's name and photo.
		from biometric_hub.engine.device_users import on_enrolment_change

		on_enrolment_change(self)

		# A new global ID is created in Easy Time Pro by the office-PC agent (if switched on).
		from biometric_hub.engine import user_push

		user_push.on_enrolment_change(self)

	def after_insert(self):
		# Shows up in Employee → Biometric IDs, wherever it was made.
		from biometric_hub.engine.employee_ids import on_enrolment_insert

		on_enrolment_insert(self)

	def on_trash(self):
		# Removing a mapping has to remove it everywhere, or the engine's legacy
		# fallback to Employee.attendance_device_id keeps resolving it.
		from biometric_hub.engine.enrolment import clear_attendance_device_id

		if not self.flags.get("from_employee_ids"):
			clear_attendance_device_id(self)

		from biometric_hub.engine.employee_ids import on_enrolment_trash

		on_enrolment_trash(self)


def enrolment_key(device_user_id, device=None):
	return f"{(device_user_id or '').strip()}::{device or '*'}"
