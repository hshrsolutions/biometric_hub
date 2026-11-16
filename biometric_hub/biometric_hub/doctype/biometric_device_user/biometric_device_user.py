# Copyright (c) 2026, GroHR and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BiometricDeviceUser(Document):
	"""One user of the attendance software, with their photo. Logic lives in
	engine/device_users.py; these are thin, permission-checked callers."""


def _can_edit():
	frappe.has_permission("Biometric Device User", "write", throw=True)


@frappe.whitelist()
def confirm(device_user, employee):
	_can_edit()
	from biometric_hub.engine.device_users import confirm as do_confirm

	return do_confirm(device_user, employee)


@frappe.whitelist()
def reject(device_user):
	_can_edit()
	from biometric_hub.engine.device_users import reject as do_reject

	return do_reject(device_user)


@frappe.whitelist()
def import_files(users_file_url, photos_file_url=None, apply=0):
	"""Import from files HR uploaded in the desk (dialog on the list view)."""
	_can_edit()
	frappe.has_permission("Biometric Device User", "create", throw=True)
	from biometric_hub.engine.device_users import import_users, read_photos, read_users

	def content(url):
		f = frappe.get_doc("File", {"file_url": url})
		return f.get_content(), f.file_name

	data, name = content(users_file_url)
	users = read_users(data, name)
	photos = read_photos(content(photos_file_url)[0]) if photos_file_url else {}
	return import_users(users, photos, apply=frappe.utils.cint(apply))


@frappe.whitelist()
def fetch_profile(device_user):
	"""'Fetch from office PC' button."""
	_can_edit()
	from biometric_hub.engine.device_users import request_profile

	return request_profile(device_user, force=True)


@frappe.whitelist()
def employee_profiles(employee):
	"""Machine photos for an employee's IDs — shown on the Employee form."""
	frappe.has_permission("Biometric Device User", "read", throw=True)
	from biometric_hub.engine.device_users import employee_profiles as get

	return get(employee)


@frappe.whitelist()
def fetch_for_employee(employee):
	"""Ask the office PC for every ID this employee holds."""
	_can_edit()
	from biometric_hub.engine.device_users import employee_profiles as get, request_profile

	data = get(employee)
	ids = [p["name"] for p in data["profiles"]] + data["ids_without_profile"]
	return [request_profile(i, employee, force=True) for i in ids]
