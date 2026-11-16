"""Driver lookup.

Apps register drivers through the `biometric_drivers` hook; the `Biometric
Driver` doctype is the reconciled registry of what is available. This module is
the bridge between the two.
"""

import frappe
from frappe import _

from biometric_hub.drivers.base import BaseDriver


def registered_paths() -> list[str]:
	"""Every dotted path any installed app declares via the hook."""
	paths = []
	for app_paths in (frappe.get_hooks("biometric_drivers") or []):
		if isinstance(app_paths, str):
			paths.append(app_paths)
		else:
			paths.extend(app_paths)
	return paths


def load(python_path: str) -> BaseDriver:
	cls = frappe.get_attr(python_path)
	if not issubclass(cls, BaseDriver):
		frappe.throw(_("{0} is not a BaseDriver.").format(python_path))
	return cls()


def get_driver(driver_name: str) -> BaseDriver:
	if not driver_name:
		frappe.throw(_("No driver set on this device."))
	row = frappe.db.get_value(
		"Biometric Driver", driver_name, ["python_path", "enabled"], as_dict=True
	)
	if not row:
		frappe.throw(_("Unknown driver {0}.").format(driver_name))
	if not row.enabled:
		frappe.throw(_("Driver {0} is disabled.").format(driver_name))
	if not row.python_path:
		frappe.throw(_("Driver {0} has no Python path — is its app installed?").format(driver_name))
	return load(row.python_path)


def sync():
	"""Reconcile the `Biometric Driver` registry against the hook.

	New drivers appear, missing ones are disabled but never deleted — devices
	link to them, and deleting a driver would orphan a device rather than tell
	anyone about a problem.
	"""
	seen = set()
	for path in registered_paths():
		try:
			driver = load(path)
		except Exception as e:
			frappe.log_error(title=f"Biometric Hub: driver {path} failed to load", message=str(e))
			continue

		name = driver.driver_name or path.rsplit(".", 1)[-1]
		seen.add(name)
		values = {
			"source_app": path.split(".")[0],
			"python_path": path,
			"driver_version": driver.driver_version,
			"enabled": 1,
			**driver.capabilities(),
		}
		if frappe.db.exists("Biometric Driver", name):
			frappe.db.set_value("Biometric Driver", name, values, update_modified=False)
		else:
			frappe.get_doc({"doctype": "Biometric Driver", "driver_name": name, **values}).insert(
				ignore_permissions=True
			)

	for row in frappe.get_all("Biometric Driver", filters={"enabled": 1}, fields=["name", "python_path"]):
		if row.name not in seen:
			frappe.db.set_value("Biometric Driver", row.name, "enabled", 0, update_modified=False)

	frappe.db.commit()
	return sorted(seen)
