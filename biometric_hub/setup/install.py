"""Install hooks. The fresh-install half of the dual path."""

import frappe

from biometric_hub.drivers import registry
from biometric_hub.setup import desk, reports, scaffold


def after_install():
	# Doctypes already exist from their JSON at this point; run() is idempotent
	# and does the rest: roles, permissions, settings defaults, and the enrolment
	# bootstrap from Employee.attendance_device_id.
	scaffold.run()

	# The registry is reconciled on migrate, which a fresh install never runs — so
	# without this a new site has no drivers at all, and `Biometric Device.driver`
	# is a required link with nothing to point at.
	registry.sync()

	# Workspace, workbench page and reports ship as JSON, but seed them here too
	# so a site installed from a package rather than a git checkout is complete.
	desk.run()
	reports.run()

	frappe.db.commit()
