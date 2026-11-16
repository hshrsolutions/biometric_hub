"""Demo data, so the workbench can be shown without hardware.

	bench --site <site> execute biometric_hub.tests.seed.run
	bench --site <site> execute biometric_hub.tests.seed.clear

Creates a device, a connector and a spread of punches — some mapped, some
unmapped, one errored — so the workbench, the reports and the device health view
all have something real to show.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

PREFIX = "DEMO"
DEVICE = f"{PREFIX} Front Door"
SERIAL = f"{PREFIX}-SN-0001"
CONNECTOR = f"{PREFIX} Office Agent"


def run():
	employees = frappe.get_all("Employee", filters={"status": "Active"}, pluck="name", limit=4)
	if not employees:
		print("No active employees on this site — nothing to map punches to.")
		return

	if not frappe.db.exists("Biometric Connector", CONNECTOR):
		frappe.get_doc(
			{"doctype": "Biometric Connector", "agent_name": CONNECTOR, "enabled": 1,
			 "site_label": "Demo office", "agent_version": "0.1.0", "last_checkin": now_datetime()}
		).insert(ignore_permissions=True)
		print(f"  + Connector {CONNECTOR}")

	if not frappe.db.exists("Biometric Device", DEVICE):
		frappe.get_doc(
			{
				"doctype": "Biometric Device", "device_name": DEVICE, "enabled": 1,
				"driver": "Fake", "connectivity_mode": "Via Connector", "connector": CONNECTOR,
				"serial_no": SERIAL, "location": "Reception",
				"latitude": 9.9252, "longitude": 78.1198,
				"heartbeat_grace_minutes": 60,
			}
		).insert(ignore_permissions=True)
		print(f"  + Device {DEVICE}")

	for index, employee in enumerate(employees[:3]):
		key = f"{PREFIX}-{100 + index}"
		if not frappe.db.exists("Biometric Enrolment", {"device_user_id": key}):
			frappe.get_doc(
				{"doctype": "Biometric Enrolment", "employee": employee,
				 "device_user_id": key, "enabled": 1, "notes": "Demo data."}
			).insert(ignore_permissions=True)

	from biometric_hub.drivers.base import RawPunch
	from biometric_hub.engine.ingest import ingest

	punches = []
	for day in range(3):
		for index in range(3):
			for hour, code in ((9, "0"), (18, "1")):
				punches.append(
					RawPunch(
						device_serial=SERIAL,
						device_user_id=f"{PREFIX}-{100 + index}",
						punch_time=add_to_date(now_datetime(), days=-day, hours=hour - now_datetime().hour),
						raw_log_type=code,
					)
				)
	# Two people nobody has enrolled — the queue needs something in it to be worth looking at.
	for day in range(3):
		for ghost in ("4417", "5290"):
			punches.append(
				RawPunch(device_serial=SERIAL, device_user_id=ghost,
				         punch_time=add_to_date(now_datetime(), days=-day, hours=-2), raw_log_type="0")
			)
	# And one from a reader nobody registered.
	punches.append(
		RawPunch(device_serial=f"{PREFIX}-UNKNOWN-SN", device_user_id="1",
		         punch_time=add_to_date(now_datetime(), hours=-1), raw_log_type="0")
	)

	result = ingest(punches, source="Connector", connector=CONNECTOR)
	frappe.db.commit()
	print(
		f"  + {result['received']} punches: {result['mapped']} mapped, "
		f"{result['unmapped']} unmapped, {result['errors']} error"
	)
	print("\nOpen /app/biometric-workbench to see it.")


def clear():
	for punch in frappe.get_all(
		"Biometric Punch", filters={"device_serial": ["like", f"{PREFIX}%"]}, pluck="name"
	):
		checkin = frappe.db.get_value("Biometric Punch", punch, "employee_checkin")
		frappe.delete_doc("Biometric Punch", punch, force=True, ignore_permissions=True, delete_permanently=True)
		if checkin and frappe.db.exists("Employee Checkin", checkin):
			frappe.delete_doc("Employee Checkin", checkin, force=True, ignore_permissions=True, delete_permanently=True)
	for dt, filters in (
		("Biometric Enrolment", {"device_user_id": ["like", f"{PREFIX}%"]}),
		("Biometric Device", {"device_name": DEVICE}),
		("Biometric Connector", {"agent_name": CONNECTOR}),
	):
		for name in frappe.get_all(dt, filters=filters, pluck="name"):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()
	print("Demo data removed.")
