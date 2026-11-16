"""Desk furniture: the workspace and the workbench page.

Created programmatically so developer mode exports the JSON into the app —
same dual-path discipline as the doctypes.

	bench --site <site> execute biometric_hub.setup.desk.run
"""

import frappe

WORKSPACE = "Biometric Hub"
PAGE = "biometric-workbench"


def run():
	make_page()
	make_workspace()
	ensure_workspace_link("Employee Device Access", "Biometric Employee Access", "Configuration")
	ensure_workspace_link("Easy Time Pro Sync", "Biometric User Sync", "Operations")
	frappe.db.commit()
	print("Done.")


def make_page():
	if frappe.db.exists("Page", PAGE):
		print(f"  = Page {PAGE} (exists)")
		return
	frappe.get_doc(
		{
			"doctype": "Page",
			"name": PAGE,
			"page_name": PAGE,
			"title": "Biometric Workbench",
			"module": "Biometric Hub",
			"standard": "Yes",
			"roles": [{"role": "Biometric Manager"}, {"role": "System Manager"}],
		}
	).insert(ignore_permissions=True)
	print(f"  + Page {PAGE}")


def make_workspace():
	if frappe.db.exists("Workspace", WORKSPACE):
		print(f"  = Workspace {WORKSPACE} (exists)")
		return

	links = [
		{"type": "Card Break", "label": "Operations"},
		{"type": "Link", "label": "Biometric Punch", "link_type": "DocType", "link_to": "Biometric Punch", "onboard": 1},
		{"type": "Link", "label": "Biometric Sync Log", "link_type": "DocType", "link_to": "Biometric Sync Log"},
		{"type": "Link", "label": "Employee Checkin", "link_type": "DocType", "link_to": "Employee Checkin"},
		{"type": "Card Break", "label": "Configuration"},
		{"type": "Link", "label": "Biometric Device", "link_type": "DocType", "link_to": "Biometric Device", "onboard": 1},
		{"type": "Link", "label": "Biometric Connector", "link_type": "DocType", "link_to": "Biometric Connector"},
		{"type": "Link", "label": "Biometric Enrolment", "link_type": "DocType", "link_to": "Biometric Enrolment", "onboard": 1},
		{"type": "Link", "label": "Employee Device Access", "link_type": "DocType", "link_to": "Biometric Employee Access", "onboard": 1},
		{"type": "Link", "label": "Biometric Driver", "link_type": "DocType", "link_to": "Biometric Driver"},
		{"type": "Link", "label": "Biometric Settings", "link_type": "DocType", "link_to": "Biometric Settings"},
	]

	doc = frappe.get_doc(
		{
			"doctype": "Workspace",
			"name": WORKSPACE,
			"label": WORKSPACE,
			"title": WORKSPACE,
			"module": "Biometric Hub",
			"icon": "clock",
			"public": 1,
			"is_hidden": 0,
			"content": frappe.as_json(
				[
					{"id": "bh_hd", "type": "header", "data": {"text": "<span class='h4'>Biometric Hub</span>", "col": 12}},
					{"id": "bh_sc", "type": "card", "data": {"card_name": "Shortcuts", "col": 4}},
					{"id": "bh_op", "type": "card", "data": {"card_name": "Operations", "col": 4}},
					{"id": "bh_cf", "type": "card", "data": {"card_name": "Configuration", "col": 4}},
				]
			),
			"links": links,
			"shortcuts": [
				{"type": "Page", "link_to": PAGE, "label": "Workbench"},
				{"type": "DocType", "link_to": "Biometric Punch", "label": "Unmapped Punches"},
				{"type": "DocType", "link_to": "Biometric Device", "label": "Devices"},
			],
		}
	)
	doc.insert(ignore_permissions=True)
	print(f"  + Workspace {WORKSPACE}")


def ensure_workspace_link(label, doctype, card):
	"""Add a link to an existing workspace — make_workspace() only builds a new one,
	so links added after first release need this idempotent step."""
	if not frappe.db.exists("Workspace", WORKSPACE):
		return
	ws = frappe.get_doc("Workspace", WORKSPACE)
	if any(l.link_to == doctype for l in ws.links):
		print(f"  = Workspace link {label} (exists)")
		return
	# Insert at the end of the named card, keeping the card grouping intact.
	links = [l.as_dict() for l in ws.links]
	pos = len(links)
	in_card = False
	for i, l in enumerate(links):
		if l.type == "Card Break":
			if in_card:
				pos = i
				break
			in_card = l.label == card
	new = {"type": "Link", "label": label, "link_type": "DocType", "link_to": doctype, "onboard": 1}
	links.insert(pos, new)
	ws.set("links", [])
	for l in links:
		l = {k: v for k, v in l.items() if k not in ("name", "idx", "parent", "parentfield", "parenttype", "creation", "modified", "owner", "modified_by", "docstatus")}
		ws.append("links", l)
	ws.save(ignore_permissions=True)
	print(f"  + Workspace link {label}")
