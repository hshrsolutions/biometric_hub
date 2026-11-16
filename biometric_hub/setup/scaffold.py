"""Idempotent scaffolding of Biometric Hub roles, doctypes and settings.

This is the fresh-install path. Frappe writes the doctype JSON into the app when
developer mode is on, so the schema also ships as files and survives
`bench migrate` — the dual-path discipline every app on this bench follows.

	bench --site <site> execute biometric_hub.setup.scaffold.run

Re-running is safe: existing doctypes are left alone except that any missing
field is appended, and existing roles, settings and enrolments are not touched.
"""

import frappe

from biometric_hub.biometric_hub.doctype.biometric_enrolment.biometric_enrolment import enrolment_key

MODULE = "Biometric Hub"

ROLES = [
	("Biometric Manager", "Configures devices, maps employees, resolves unmapped punches."),
	("Biometric Integration", "Non-interactive account used by devices and connector agents."),
]

MANAGER = {"role": "Biometric Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1}
SYSMGR = {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1}
HR_READ = {"role": "HR Manager", "read": 1, "report": 1, "export": 1}
# Deciding who may punch where is an HR call, so HR owns the access records.
HR_FULL = {"role": "HR Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1, "import": 1}
INTEGRATION_READ = {"role": "Biometric Integration", "read": 1}
INTEGRATION_WRITE = {"role": "Biometric Integration", "read": 1, "create": 1}

PUNCH_STATUSES = "Pending\nMapped\nUnmapped\nBlocked\nDuplicate\nIgnored\nError"
SOURCES = "Push\nConnector\nPoll\nCloud\nManual"


def f(fieldname, label, fieldtype="Data", **kw):
	d = {"fieldname": fieldname, "label": label, "fieldtype": fieldtype}
	d.update(kw)
	return d


def sb(fieldname, label=""):
	return {"fieldname": fieldname, "label": label, "fieldtype": "Section Break"}


def cb(fieldname):
	return {"fieldname": fieldname, "fieldtype": "Column Break"}


def make_doctype(name, fields, permissions=None, **kw):
	"""Create the doctype if absent; otherwise append any field it is missing."""
	if frappe.db.exists("DocType", name):
		added = ensure_fields(name, fields)
		print(f"  = {name} (exists{', +' + str(added) + ' field(s)' if added else ''})")
		return
	doc = frappe.get_doc(
		{
			"doctype": "DocType",
			"name": name,
			"module": MODULE,
			"custom": 0,
			"fields": fields,
			"permissions": permissions or [MANAGER, SYSMGR],
			**kw,
		}
	)
	doc.insert(ignore_permissions=True)
	print(f"  + {name}")


def ensure_fields(name, fields):
	"""Append fields the doctype does not have yet. Appended at the end — order is
	cosmetic, and the exported JSON is the record of truth once it exists."""
	doc = frappe.get_doc("DocType", name)
	have = {df.fieldname for df in doc.fields}
	missing = [fld for fld in fields if fld["fieldname"] not in have]
	if not missing:
		return 0
	for fld in missing:
		doc.append("fields", fld)
	doc.save(ignore_permissions=True)
	return len(missing)


def ensure_select_options(doctype, fieldname, options):
	"""A Select whose options grew after the doctype was first created. ensure_fields
	only appends missing fields, so an added option needs its own idempotent step."""
	doc = frappe.get_doc("DocType", doctype)
	for df in doc.fields:
		if df.fieldname == fieldname and df.options != options:
			df.options = options
			doc.save(ignore_permissions=True)
			print(f"  ~ {doctype}.{fieldname} options updated")
			return 1
	return 0


def make_roles():
	for role_name, desc in ROLES:
		if frappe.db.exists("Role", role_name):
			print(f"  = Role {role_name} (exists)")
			continue
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": role_name,
				"desk_access": 1,
				"description": desc,
			}
		).insert(ignore_permissions=True)
		print(f"  + Role {role_name}")


def run():
	print("Roles:")
	make_roles()
	print("Doctypes:")

	# ---------------------------------------------------------------- Driver
	make_doctype(
		"Biometric Driver",
		[
			f("driver_name", "Driver Name", reqd=1, unique=1, in_list_view=1),
			f("source_app", "Source App", read_only=1, in_list_view=1),
			f("python_path", "Python Path", read_only=1,
			  description="Dotted path to the driver class, registered via the biometric_drivers hook."),
			f("enabled", "Enabled", "Check", default="1"),
			sb("sb_caps", "Capabilities"),
			f("supports_push", "Supports Push", "Check", read_only=1),
			f("supports_pull", "Supports Pull", "Check", read_only=1),
			f("supports_realtime", "Supports Realtime", "Check", read_only=1),
			cb("cb_caps"),
			f("supports_clock_sync", "Supports Clock Sync", "Check", read_only=1),
			f("supports_enroll", "Supports Enrolment Push", "Check", read_only=1),
			f("driver_version", "Driver Version", read_only=1),
		],
		autoname="field:driver_name",
		description="Registry of available device drivers, synced from the biometric_drivers hook on migrate.",
	)

	# ---------------------------------------------------------------- Connector
	make_doctype(
		"Biometric Connector",
		[
			f("agent_name", "Agent Name", reqd=1, unique=1, in_list_view=1),
			f("enabled", "Enabled", "Check", default="1", in_list_view=1),
			f("site_label", "Site / Customer Location"),
			cb("cb_c1"),
			f("api_user", "API User", "Link", options="User",
			  description="The scoped Biometric Integration user this agent authenticates as."),
			f("agent_version", "Agent Version", read_only=1),
			f("last_checkin", "Last Check-in", "Datetime", read_only=1, in_list_view=1),
			sb("sb_c2", "Notes"),
			f("notes", "Notes", "Small Text"),
		],
		autoname="field:agent_name",
		permissions=[MANAGER, SYSMGR, INTEGRATION_READ],
		description="An on-prem connector agent that polls LAN-only devices and submits punches outbound-only.",
	)

	# ------------------------------------------------------- Device Shift (child)
	make_doctype(
		"Biometric Device Shift",
		[
			f("shift_type", "Shift Type", "Link", options="Shift Type", reqd=1, in_list_view=1),
		],
		istable=1,
		description="Child table: the Shift Types a device feeds. Used to advance "
		            "Shift Type.last_sync_of_checkin server-side.",
	)

	# ---------------------------------------------------------------- Device
	make_doctype(
		"Biometric Device",
		[
			f("device_name", "Device Name", reqd=1, unique=1, in_list_view=1),
			f("enabled", "Enabled", "Check", default="1", in_list_view=1),
			f("driver", "Driver", "Link", options="Biometric Driver", reqd=1, in_list_view=1),
			f("connectivity_mode", "Connectivity Mode", "Select",
			  options="Push\nDirect Poll\nVia Connector\nVendor Cloud", reqd=1, default="Push"),
			cb("cb_dev1"),
			f("serial_no", "Serial Number", unique=1, in_list_view=1,
			  description="The SN the device reports. Required for Push mode; the device identifies itself by it."),
			f("location", "Location / Branch"),
			f("device_timezone", "Device Timezone", description="Blank = use the site timezone."),
			sb("sb_conn", "Connection"),
			f("ip_address", "IP Address", depends_on="eval:doc.connectivity_mode!='Push'"),
			f("port", "Port", "Int", default="4370", depends_on="eval:doc.connectivity_mode!='Push'"),
			cb("cb_conn"),
			f("comm_key", "Communication Key", "Password", depends_on="eval:doc.connectivity_mode!='Push'"),
			f("connector", "Connector", "Link", options="Biometric Connector",
			  depends_on="eval:doc.connectivity_mode=='Via Connector'"),
			sb("sb_behaviour", "Punch Handling"),
			f("fixed_log_type", "Fixed Log Type", "Select", options="\nIN\nOUT",
			  description="Set only for single-direction readers (a dedicated entry or exit device). "
			              "Overrides whatever the device reports."),
			f("skip_auto_attendance", "Skip Auto Attendance", "Check"),
			cb("cb_behaviour"),
			f("ignore_punches_before", "Ignore Punches Before", "Datetime",
			  description="Guards against a device dumping years of old logs on first connection."),
			sb("sb_shifts", "Shifts Fed by This Device"),
			f("shift_types", "Shift Types", "Table MultiSelect", options="Biometric Device Shift",
			  description="Which shifts this reader feeds. The server advances "
			              "Shift Type.last_sync_of_checkin for these once every device in the "
			              "group has reported — so the agent never needs write access to an HR master."),
			sb("sb_geo", "Geolocation"),
			f("latitude", "Latitude", "Float", precision="6",
			  description="Passed to Employee Checkin when HRMS 'Allow Geolocation Tracking' is on. "
			              "Leave blank if unused."),
			cb("cb_geo"),
			f("longitude", "Longitude", "Float", precision="6"),
			sb("sb_health", "Health"),
			f("health_status", "Health Status", "Select", options="Unknown\nOnline\nSilent\nError",
			  default="Unknown", read_only=1, in_list_view=1),
			# NOT `last_seen`. frappe.get_all silently drops any field whose name
			# *contains* one of frappe.model.optional_fields — "_seen" among them
			# (db_query.py: `if f in fld`). The column exists and raw SQL returns
			# it, but every ORM read comes back without it. Renamed rather than
			# worked around, so nobody rediscovers this in a later phase.
			f("last_contact", "Last Contact", "Datetime", read_only=1),
			f("last_punch_time", "Last Punch Received", "Datetime", read_only=1),
			cb("cb_health"),
			f("heartbeat_grace_minutes", "Heartbeat Grace (minutes)", "Int", default="60",
			  description="Alert if the device has not been heard from for this long. 0 disables the alert."),
			f("alert_recipients", "Alert Recipients", "Small Text",
			  description="Comma-separated email addresses."),
			f("last_error", "Last Error", "Small Text", read_only=1),
		],
		autoname="field:device_name",
		permissions=[MANAGER, SYSMGR, HR_READ, INTEGRATION_READ],
		track_changes=1,
	)

	# ---------------------------------------------------------------- Enrolment
	make_doctype(
		"Biometric Enrolment",
		[
			f("employee", "Employee", "Link", options="Employee", reqd=1, in_list_view=1),
			f("employee_name", "Employee Name", "Data", fetch_from="employee.employee_name",
			  read_only=1, in_list_view=1),
			f("device_user_id", "Device User ID", reqd=1, in_list_view=1,
			  description="The numeric ID the reader reports for this person, e.g. 1434."),
			cb("cb_e1"),
			f("device", "Device", "Link", options="Biometric Device", in_list_view=1,
			  description="Blank means this ID is valid on every device. A device-specific row wins over a blank one."),
			f("enabled", "Enabled", "Check", default="1"),
			f("valid_from", "Valid From", "Date"),
			f("valid_upto", "Valid Upto", "Date",
			  description="Set when an ID is re-issued, so past punches stay with the right person."),
			sb("sb_e2", "Notes"),
			f("enrolment_key", "Enrolment Key", "Data", unique=1, read_only=1, hidden=1,
			  description="Derived uniqueness key for (device_user_id, device)."),
			f("notes", "Notes", "Small Text"),
		],
		autoname="hash",
		title_field="device_user_id",
		# No INTEGRATION_READ: the agent never needs the employee mapping, and it is HR data.
		permissions=[MANAGER, SYSMGR, HR_READ],
		track_changes=1,
		description="Maps a device user ID to an Employee. Supersedes Employee.attendance_device_id, which is kept in sync.",
	)

	# ---------------------------------------------------------------- Punch
	make_doctype(
		"Biometric Punch",
		[
			f("punch_key", "Punch Key", "Data", unique=1, read_only=1,
			  description="Idempotency key — a hash of (device serial, device user id, punch time). "
			              "A replayed punch collides here and is marked Duplicate."),
			f("status", "Status", "Select", options=PUNCH_STATUSES, default="Pending",
			  reqd=1, in_list_view=1),
			f("device", "Device", "Link", options="Biometric Device", in_list_view=1),
			f("device_serial", "Device Serial", reqd=1,
			  description="What the wire claimed, kept even when no device matches it."),
			cb("cb_p1"),
			f("device_user_id", "Device User ID", reqd=1, in_list_view=1),
			f("punch_time", "Punch Time", "Datetime", reqd=1, in_list_view=1,
			  description="As reported by the device, in the device's own timezone."),
			f("punch_time_utc", "Punch Time (UTC)", "Datetime", read_only=1),
			sb("sb_p2", "Interpretation"),
			f("raw_log_type", "Raw Log Type", description="Whatever the device reported, unaltered."),
			f("resolved_log_type", "Resolved Log Type", "Select", options="\nIN\nOUT", read_only=1),
			f("source", "Source", "Select", options=SOURCES),
			cb("cb_p2"),
			f("connector", "Connector", "Link", options="Biometric Connector"),
			f("latitude", "Latitude", "Float", precision="6"),
			f("longitude", "Longitude", "Float", precision="6"),
			sb("sb_p3", "Result"),
			f("employee", "Employee", "Link", options="Employee", read_only=1),
			f("employee_checkin", "Employee Checkin", "Link", options="Employee Checkin", read_only=1),
			f("error_message", "Error Message", "Small Text", read_only=1),
			cb("cb_p3"),
			f("attempts", "Attempts", "Int", read_only=1),
			f("first_seen", "First Seen", "Datetime", read_only=1),
			f("last_attempt", "Last Attempt", "Datetime", read_only=1),
			sb("sb_p4", "Raw"),
			f("raw_payload", "Raw Payload", "Code", read_only=1, options="JSON"),
		],
		autoname="hash",
		permissions=[MANAGER, SYSMGR, HR_READ, INTEGRATION_WRITE],
		description="Every punch that reached the server, staged before any interpretation. Nothing is dropped.",
	)

	# ---------------------------------------------------------------- Sync Log
	make_doctype(
		"Biometric Sync Log",
		[
			f("started", "Started", "Datetime", reqd=1, in_list_view=1),
			f("finished", "Finished", "Datetime"),
			f("source", "Source", "Select", options=SOURCES, in_list_view=1),
			cb("cb_s1"),
			f("connector", "Connector", "Link", options="Biometric Connector", in_list_view=1),
			f("device", "Device", "Link", options="Biometric Device"),
			f("status", "Status", "Select", options="Success\nPartial\nFailed",
			  default="Success", in_list_view=1),
			sb("sb_s2", "Counts"),
			f("received", "Received", "Int"),
			f("mapped", "Mapped", "Int"),
			f("duplicates", "Duplicates", "Int"),
			cb("cb_s2"),
			f("unmapped", "Unmapped", "Int"),
			f("errors", "Errors", "Int"),
			f("blocked", "Blocked", "Int",
			  description="Punches from an employee on a device HR has not allowed them to use."),
			sb("sb_s3", "Message"),
			f("message", "Message", "Small Text"),
		],
		autoname="hash",
		permissions=[MANAGER, SYSMGR, HR_READ],
		description="One row per ingestion batch or poll run. The operational record of whether sync actually ran.",
	)

	# ------------------------------------------------- Employee device access
	make_doctype(
		"Biometric Access Device",
		[
			f("device", "Device", "Link", options="Biometric Device", reqd=1, in_list_view=1),
			f("device_user_id", "ID on This Device", in_list_view=1,
			  description="Only if this machine knows the person by a different ID from their usual "
			              "one. Leave blank to use the usual ID."),
			f("enrolment", "Enrolment", "Link", options="Biometric Enrolment", read_only=1, in_list_view=1,
			  description="The device-specific enrolment kept in step with this row."),
		],
		istable=1,
		description="Child table: one device an employee is allowed to punch on.",
	)

	make_doctype(
		"Biometric Employee Access",
		[
			f("employee", "Employee", "Link", options="Employee", reqd=1, unique=1, in_list_view=1),
			f("employee_name", "Employee Name", "Data", fetch_from="employee.employee_name",
			  read_only=1, in_list_view=1),
			f("usual_device_user_id", "Usual Device ID", "Data",
			  fetch_from="employee.attendance_device_id", read_only=1,
			  description="Employee.attendance_device_id — used on any allowed device that has no ID of its own below."),
			cb("cb_a1"),
			f("access_mode", "Biometric Devices", "Select",
			  options="Any Device\nAllowed Devices Only", default="Any Device", reqd=1, in_list_view=1,
			  description="Allowed Devices Only: a punch on any other machine is held as Blocked "
			              "instead of becoming a checkin."),
			f("allow_mobile_checkin", "Allow Mobile App Check-in", "Check", default="1", in_list_view=1,
			  description="Off stops this employee checking themselves in from the mobile app or desk. "
			              "HR can still record checkins for them."),
			sb("sb_a2", "Allowed Devices"),
			f("devices", "Devices", "Table", options="Biometric Access Device"),
			sb("sb_a3", "All IDs on File"),
			f("enrolments_html", "Enrolments", "HTML"),
			sb("sb_a4", "Notes"),
			f("notes", "Notes", "Small Text"),
		],
		autoname="field:employee",
		title_field="employee_name",
		permissions=[MANAGER, SYSMGR, HR_FULL],
		allow_import=1,  # bulk mapping through Data Import
		track_changes=1,
		description="Which biometric devices, and whether the mobile app, an employee may use to "
		            "check in. No record means no restriction.",
	)

	# ---------------------------------------------------- Device user directory
	make_doctype(
		"Biometric Device User",
		[
			f("device_user_id", "Device User ID", reqd=1, unique=1, in_list_view=1),
			f("name_on_device", "Name on Device", in_list_view=1, in_standard_filter=1),
			f("photo", "Photo", "Attach Image"),
			cb("cb_du1"),
			f("areas", "Areas / Machines", "Small Text",
			  description="Where the attendance software has this user, as exported from it."),
			f("department_on_device", "Department on Device"),
			f("has_fingerprint", "Fingerprint"),
			f("has_face", "Face"),
			sb("sb_du2", "Who Is This?"),
			f("compare_html", "Compare", "HTML"),
			f("employee", "Employee", "Link", options="Employee", in_list_view=1, in_standard_filter=1),
			f("employee_name", "Employee Name", "Data", fetch_from="employee.employee_name", read_only=1),
			f("employee_photo", "Employee Photo (GroHR)", "Attach Image", fetch_from="employee.image", read_only=1),
			cb("cb_du2"),
			f("match_status", "Match Status", "Select",
			  options="Unchecked\nSuggested\nConfirmed\nNot This Person", default="Unchecked",
			  in_list_view=1, in_standard_filter=1),
			f("match_note", "Match Note", "Small Text", read_only=1),
			sb("sb_du3", "Activity"),
			f("punches_30d", "Punches (last 30 days)", "Int", read_only=1),
			cb("cb_du3"),
			f("last_punch", "Last Punch", "Datetime", read_only=1),
			sb("sb_du4", "Profile from the Office PC"),
			f("profile_status", "Profile Status", "Select", options="\nRequested\nReceived\nNot Found\nError",
			  read_only=1, in_standard_filter=1,
			  description="Requested when the ID changes in GroHR; the connector agent fetches the name and "
			              "photo from the attendance software on its next check-in."),
			f("profile_requested", "Requested On", "Datetime", read_only=1),
			cb("cb_du4"),
			f("profile_received", "Received On", "Datetime", read_only=1),
			f("profile_error", "Profile Error", "Small Text", read_only=1),
		],
		autoname="field:device_user_id",
		title_field="name_on_device",
		image_field="photo",
		permissions=[MANAGER, SYSMGR, HR_FULL],
		allow_import=1,
		track_changes=1,
		description="Every user known to the attendance software, with their photo, so HR can confirm "
		            "which employee each machine ID belongs to.",
	)

	# ------------------------------------------- Employee's list of biometric IDs
	make_doctype(
		"Biometric Employee ID",
		[
			f("device_user_id", "Biometric ID", reqd=1, in_list_view=1, columns=3,
			  description="The user number the attendance machine has for this person."),
			f("device", "Only on Device", "Link", options="Biometric Device", in_list_view=1, columns=3,
			  description="Leave blank: the ID counts on every machine. Set it only when this number means "
			              "this person on that one machine."),
			f("enrolment", "Enrolment", "Link", options="Biometric Enrolment", read_only=1, in_list_view=1, columns=2),
		],
		istable=1,
		description="Child table on Employee (custom field bh_biometric_ids): every biometric ID that "
		            "counts toward this employee's attendance.",
	)

	# ------------------------------------------- Push employees to Easy Time Pro
	make_doctype(
		"Biometric Area Rule",
		[
			f("grohr_value", "GroHR Value", reqd=1, in_list_view=1, columns=3,
			  description="A Branch, Department or Company name (whichever Biometric Settings → Area From says)."),
			f("area_codes", "Easy Time Pro Area Code(s)", reqd=1, in_list_view=1, columns=3,
			  description="Comma separated. The employee is put on every machine in these areas."),
			f("department_code", "Easy Time Pro Department Code", in_list_view=1, columns=2,
			  description="Blank: the default department code."),
		],
		istable=1,
		description="Child of Biometric Settings: which Easy Time Pro area(s) an employee goes to.",
	)
	make_doctype(
		"Biometric User Sync",
		[
			f("employee", "Employee", "Link", options="Employee", reqd=1, in_list_view=1, in_standard_filter=1),
			f("employee_name", "Employee Name", fetch_from="employee.employee_name", read_only=1),
			f("device_user_id", "Device User ID", reqd=1, in_list_view=1, in_standard_filter=1),
			cb("cb_us1"),
			f("action", "Action", "Select", options="Add / Update\nRemove from Machines", default="Add / Update",
			  reqd=1, in_list_view=1),
			f("status", "Status", "Select", options="Queued\nSent\nDone\nDry Run\nConflict\nSkipped\nError",
			  default="Queued", in_list_view=1, in_standard_filter=1, read_only=1),
			f("overwrite", "Overwrite Existing User", "Check", default="0", read_only=1,
			  description="Set when HR confirmed the user Easy Time Pro already has under this ID is this employee."),
			sb("sb_us2", "Result"),
			f("outcome", "Outcome", read_only=1, in_list_view=1),
			f("name_in_easytime", "Name in Easy Time Pro", read_only=1),
			f("remote_id", "Easy Time Pro Record ID", read_only=1),
			cb("cb_us2"),
			f("message", "Message", "Small Text", read_only=1),
			sb("sb_us3", "Tracking"),
			f("requested_on", "Requested On", "Datetime", read_only=1),
			f("sent_on", "Sent On", "Datetime", read_only=1),
			f("done_on", "Answered On", "Datetime", read_only=1),
			cb("cb_us3"),
			f("attempts", "Attempts", "Int", default="0", read_only=1),
			f("reason", "Why Queued", "Small Text", read_only=1),
			f("payload", "Last Payload Sent", "Code", options="JSON", read_only=1),
		],
		autoname="BUS-.#####",
		title_field="employee_name",
		permissions=[MANAGER, SYSMGR, {"role": "HR Manager", "read": 1, "write": 1, "report": 1, "export": 1}],
		track_changes=1,
		sort_field="modified",
		description="Employees GroHR asked the office-PC agent to create, update or remove in eSSL Easy Time "
		            "Pro, and what happened. Easy Time Pro then sends them to the machines in their area.",
	)

	# ---------------------------------------------------------------- Settings
	make_doctype(
		"Biometric Settings",
		[
			sb("sb_g1", "Ingestion"),
			f("enable_ingestion", "Enable Ingestion", "Check", default="1",
			  description="Master kill switch. Off means punches are rejected at the door."),
			f("create_employee_checkin", "Create Employee Checkin", "Check", default="1",
			  description="Off stages punches without writing to HRMS — used for a parallel-run cutover."),
			cb("cb_g1"),
			f("default_log_type_policy", "Default Log Type Policy", "Select",
			  options="Trust Device\nAlternate\nFirst/Last of Day", default="Trust Device",
			  description="Used only when the device reports no direction and no fixed log type is set."),
			f("max_future_skew_minutes", "Max Future Skew (minutes)", "Int", default="5",
			  description="A punch further in the future than this is an Error, not a checkin. Catches device clock drift."),
			sb("sb_g2", "Housekeeping"),
			f("punch_retention_days", "Punch Retention (days)", "Int", default="180",
			  description="Mapped and Duplicate punches older than this are purged nightly. "
			              "Unmapped and Error are never auto-purged."),
			f("reconcile_enabled", "Nightly Reconcile", "Check", default="1"),
			cb("cb_g2"),
			f("reconcile_lookback_days", "Reconcile Lookback (days)", "Int", default="3"),
			f("alert_on_silent_device", "Alert on Silent Device", "Check", default="1"),
			sb("sb_g3", "Profiles"),
			f("fetch_profiles", "Fetch Profiles from Connector", "Check", default="0",
			  description="When a device ID is set or changed in GroHR, ask the connector agent to send that "
			              "user's name and photo from the attendance software. Needs agent 0.3.0+ with a database source."),
			sb("sb_g4", "Push Employees to Easy Time Pro"),
			f("push_users", "Push Employees to Easy Time Pro", "Check", default="0",
			  description="When an active employee gets a biometric ID (or their name/branch changes), the "
			              "connector agent creates or updates them in Easy Time Pro, which sends them to the "
			              "machines. Needs agent 0.4.0+ with an `easytime` block in config.json."),
			f("push_connector", "Connector", "Link", options="Biometric Connector",
			  description="The agent on the PC that can reach Easy Time Pro.",
			  mandatory_depends_on="eval:doc.push_users"),
			f("push_remove_on_exit", "Remove from Machines When Employee Leaves", "Check", default="1",
			  description="Status Left/Inactive: resign the user in Easy Time Pro, which removes them from the machines."),
			cb("cb_g4"),
			f("push_area_from", "Area From", "Select", options="Branch\nDepartment\nCompany", default="Branch"),
			f("push_default_area_codes", "Default Area Code(s)",
			  description="Used when no rule matches. Blank: an employee with no matching rule is not pushed."),
			f("push_default_department_code", "Default Department Code", default="1",
			  description="Easy Time Pro department code. '1' is its built-in default department."),
			f("push_rules", "Area Rules", "Table", options="Biometric Area Rule"),
		],
		issingle=1,
		permissions=[MANAGER, SYSMGR],
		description="Global switches for Biometric Hub. Defaults are deliberately conservative.",
	)

	# Options added after first release: Blocked (employee device access).
	ensure_select_options("Biometric Punch", "status", PUNCH_STATUSES)

	print("Employee custom fields:")
	make_employee_fields()

	print("Permissions:")
	grant_integration_permissions()
	print("Settings:")
	seed_settings()
	print("Enrolments:")
	bootstrap_enrolments()
	frappe.db.commit()
	print("Done.")


# The one non-negotiable grant, and its exact shape. Anything beyond this on the
# Biometric Integration role is the audit §3 failure coming back.
INTEGRATION_GRANTS = {
	"Employee Checkin": {
		"read": 1, "create": 1,
		"write": 0, "delete": 0, "submit": 0, "cancel": 0, "amend": 0,
		"report": 0, "export": 0, "import": 0, "share": 0, "print": 0, "email": 0,
	},
}


def grant_integration_permissions():
	"""Give the integration role create access on Employee Checkin and nothing
	else. Uses Custom DocPerm via frappe.permissions, which copies the standard
	permissions first — HRMS's own roles are untouched."""
	from frappe.permissions import add_permission, update_permission_property

	for doctype, props in INTEGRATION_GRANTS.items():
		if not frappe.db.exists("DocType", doctype):
			print(f"  ! {doctype} not installed, skipped")
			continue
		if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": "Biometric Integration"}):
			add_permission(doctype, "Biometric Integration", 0)
		for prop, value in props.items():
			update_permission_property(doctype, "Biometric Integration", 0, prop, value, validate=False)
		print(f"  + Biometric Integration: {doctype} (read, create)")


def seed_settings():
	"""Materialise the Single with its defaults, so a fresh site has sane values
	before anyone opens the form."""
	if frappe.db.get_singles_dict("Biometric Settings"):
		print("  = Biometric Settings (exists, left alone)")
		return
	doc = frappe.get_single("Biometric Settings")
	doc.enable_ingestion = 1
	doc.create_employee_checkin = 1
	doc.default_log_type_policy = "Trust Device"
	doc.max_future_skew_minutes = 5
	doc.punch_retention_days = 180
	doc.reconcile_enabled = 1
	doc.reconcile_lookback_days = 3
	doc.alert_on_silent_device = 1
	doc.save(ignore_permissions=True)
	print("  + Biometric Settings (defaults)")


def bootstrap_enrolments():
	"""Fill Biometric Enrolment from every non-empty Employee.attendance_device_id,
	so a site that already runs a legacy sync keeps working from minute one.

	Global rows only (device is blank) — the legacy field cannot express a
	per-device ID, and pretending otherwise would invent data.
	"""
	rows = frappe.get_all(
		"Employee",
		filters={"attendance_device_id": ["is", "set"]},
		fields=["name", "attendance_device_id"],
		limit_page_length=0,
	)
	created = skipped = 0
	for row in rows:
		device_user_id = (row.attendance_device_id or "").strip()
		if not device_user_id:
			continue
		key = enrolment_key(device_user_id)
		if frappe.db.exists("Biometric Enrolment", {"enrolment_key": key}):
			skipped += 1
			continue
		frappe.get_doc(
			{
				"doctype": "Biometric Enrolment",
				"employee": row.name,
				"device_user_id": device_user_id,
				"enabled": 1,
				"enrolment_key": key,
				"notes": "Bootstrapped from Employee.attendance_device_id.",
			}
		).insert(ignore_permissions=True)
		created += 1
	print(f"  + {created} created, {skipped} already present (from {len(rows)} employees with a device ID)")


# Shipped as fixtures/custom_field.json too, so `bench migrate` creates it on sites
# that never ran this scaffold — the dual path.
EMPLOYEE_FIELDS = {
	"Employee": [
		{
			"fieldname": "bh_biometric_ids",
			"label": "Biometric IDs",
			"fieldtype": "Table",
			"options": "Biometric Employee ID",
			"insert_after": "attendance_device_id",
			"description": "Every attendance-machine ID for this person. Each ID can belong to only one "
			               "employee. The first ID that counts on every machine is the Attendance Device ID.",
		}
	]
}


def make_employee_fields():
	if not frappe.db.exists("DocType", "Employee"):
		print("  ! Employee not installed, skipped")
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(EMPLOYEE_FIELDS, update=True)
	print("  + Employee.bh_biometric_ids")
	from biometric_hub.engine.employee_ids import backfill

	print(f"  + {backfill()} existing ID(s) listed on their employees")
