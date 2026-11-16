# Copyright (c) 2026, GroHR and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class BiometricPunch(Document):
	def validate(self):
		# The engine always sets this. A punch entered by hand gets one too, so the
		# UNIQUE index means the same thing whichever door the punch came in by.
		if not self.punch_key:
			from biometric_hub.engine.ingest import punch_key

			self.punch_key = punch_key(self.device_serial, self.device_user_id, self.punch_time)
