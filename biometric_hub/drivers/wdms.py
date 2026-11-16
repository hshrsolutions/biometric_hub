"""eSSL Time Pro / WDMS as a source.

The punches live in the vendor's SQL Server database on the customer's own
Windows machine, which the GroHR server cannot reach. So this driver is a
registry entry and a configuration target: the actual reading is done by the
connector agent, which is inside that network.

It is listed here so a device can be configured against a named driver like any
other, and so the registry records honestly that the server itself cannot poll
it.
"""

import frappe

from biometric_hub.drivers.base import BaseDriver


class WDMSDriver(BaseDriver):
	driver_name = "WDMS"
	driver_version = "1.0.0"
	# Pull, but only from inside the customer network — hence Via Connector.
	supports_pull = True

	def test_connection(self, device):
		if device.connectivity_mode != "Via Connector":
			return False, (
				"A database source is read from the customer's own network, so this device must be "
				"set to 'Via Connector' and claimed by a connector agent."
			)
		if not device.connector:
			return False, "Set the Connector — the agent inside that network does the reading."
		last = frappe.db.get_value("Biometric Connector", device.connector, "last_checkin")
		if not last:
			return False, f"Connector {device.connector} has never checked in. Is the agent installed and running?"
		return True, f"Read by connector {device.connector}, last heard from at {last}."

	def fetch_punches(self, device, since=None):
		raise NotImplementedError(
			"A database source is polled by the connector agent, not by the server — the vendor "
			"database is on the customer's network."
		)
