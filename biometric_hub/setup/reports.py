"""The three reports worth having.

Each answers a question that, at a customer, nobody could answer without a database
session — which is most of why the problems there lasted as long as they did.

	bench --site <site> execute biometric_hub.setup.reports.run
"""

import frappe

MODULE = "Biometric Hub"

REPORTS = [
	{
		"name": "Employees Without Biometric Enrolment",
		"ref_doctype": "Employee",
		"description": "Active employees no device can identify. Every punch they make is rejected.",
		"query": """
			SELECT e.name AS "Employee:Link/Employee:120",
			       e.employee_name AS "Name::200",
			       e.department AS "Department::160",
			       e.designation AS "Designation::160",
			       e.date_of_joining AS "Joined:Date:100"
			FROM `tabEmployee` e
			LEFT JOIN `tabBiometric Enrolment` b
			       ON b.employee = e.name AND b.enabled = 1
			WHERE e.status = 'Active'
			  AND b.name IS NULL
			  AND (e.attendance_device_id IS NULL OR e.attendance_device_id = '')
			ORDER BY e.employee_name
		""",
	},
	{
		"name": "Biometric Punch Outcomes",
		"ref_doctype": "Biometric Punch",
		"description": "What happened to every punch, by day and device. The health of the pipe.",
		"query": """
			SELECT DATE(p.punch_time) AS "Date:Date:100",
			       p.device AS "Device:Link/Biometric Device:180",
			       SUM(p.status = 'Mapped') AS "Mapped:Int:90",
			       SUM(p.status = 'Unmapped') AS "Unmapped:Int:100",
			       SUM(p.status = 'Blocked') AS "Blocked:Int:90",
			       SUM(p.status = 'Duplicate') AS "Duplicate:Int:100",
			       SUM(p.status = 'Ignored') AS "Ignored:Int:90",
			       SUM(p.status = 'Error') AS "Error:Int:90",
			       COUNT(*) AS "Total:Int:90"
			FROM `tabBiometric Punch` p
			GROUP BY DATE(p.punch_time), p.device
			ORDER BY DATE(p.punch_time) DESC, p.device
		""",
	},
	{
		"name": "Biometric Device Uptime",
		"ref_doctype": "Biometric Device",
		"description": "When each reader was last heard from, and how much it has sent.",
		"query": """
			SELECT d.name AS "Device:Link/Biometric Device:180",
			       d.location AS "Location::150",
			       d.health_status AS "Health::100",
			       d.connectivity_mode AS "Mode::120",
			       d.last_contact AS "Last Contact:Datetime:160",
			       d.last_punch_time AS "Last Punch:Datetime:160",
			       TIMESTAMPDIFF(MINUTE, d.last_contact, NOW()) AS "Quiet For (min):Int:120",
			       (SELECT COUNT(*) FROM `tabBiometric Punch` p WHERE p.device = d.name) AS "Punches:Int:100"
			FROM `tabBiometric Device` d
			WHERE d.enabled = 1
			ORDER BY FIELD(d.health_status, 'Error', 'Silent', 'Unknown', 'Online'), d.name
		""",
	},
]


def run():
	for spec in REPORTS:
		if frappe.db.exists("Report", spec["name"]):
			# doc.save() rather than db.set_value, so developer mode re-exports the
			# JSON that actually ships.
			doc = frappe.get_doc("Report", spec["name"])
			if doc.query != spec["query"].strip():
				doc.query = spec["query"].strip()
				doc.save(ignore_permissions=True)
				print(f"  = {spec['name']} (query refreshed)")
			else:
				print(f"  = {spec['name']} (unchanged)")
			continue
		frappe.get_doc(
			{
				"doctype": "Report",
				"report_name": spec["name"],
				"ref_doctype": spec["ref_doctype"],
				"module": MODULE,
				"is_standard": "Yes",
				"report_type": "Query Report",
				"disabled": 0,
				"query": spec["query"].strip(),
				"roles": [{"role": "Biometric Manager"}, {"role": "HR Manager"}, {"role": "System Manager"}],
			}
		).insert(ignore_permissions=True)
		print(f"  + {spec['name']}")
	frappe.db.commit()
	print("Done.")
