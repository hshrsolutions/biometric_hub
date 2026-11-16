"""GroHR → eSSL Easy Time Pro: create, update and remove employees in the
attendance software, so they reach the machines without anyone typing them in
twice.

The office PC cannot be called, so this is a queue (`Biometric User Sync`):

  1. an enrolment, an Employee change or an HR button queues a row;
  2. the heartbeat reply hands Queued rows to the push connector's agent, with
     the payload worked out *here* (name, area codes, department code);
  3. the agent calls Easy Time Pro's REST API and posts the outcome to
     `api.ingest.user_push_results`, which lands in `receive()`.

Every decision lives here, none on the PC: which IDs, which area, and above all
whether a user Easy Time Pro already holds under that ID may be overwritten.
The agent never overwrites unless told to. When it finds someone there, we
compare names: the same person is updated automatically; anyone else becomes a
**Conflict** for HR — shared and mistyped IDs are a real problem at a customer, and
silently renaming a machine user would send another person's punches to this
employee.

Only global IDs (no device on the enrolment) are pushed. A per-machine ID means
"this number is this person on that one machine", which Easy Time Pro cannot
express — those stay HR's to set on the machine.

Writes: `Biometric User Sync` only. Never blocks an Employee or enrolment save.
Off unless Biometric Settings → Push Employees to Easy Time Pro is ticked.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, getdate, now_datetime, today

DOCTYPE = "Biometric User Sync"
ADD, REMOVE = "Add / Update", "Remove from Machines"
OPEN = ("Queued", "Sent", "Error", "Conflict", "Dry Run")
PER_HEARTBEAT = 20
RESEND_AFTER_MINUTES = 30
MAX_ATTEMPTS = 5
SAME_PERSON = 0.6  # name similarity at or above this is treated as the same person
WATCHED = ("employee_name", "first_name", "last_name", "branch", "department", "company", "status")


# ------------------------------------------------------------------ settings


def enabled():
	return bool(cint(frappe.db.get_single_value("Biometric Settings", "push_users")))


def _remove_on_exit():
	v = frappe.db.get_single_value("Biometric Settings", "push_remove_on_exit")
	return v is None or bool(cint(v))  # a field added after install has no stored value yet


def _codes(text):
	return [c.strip() for c in str(text or "").replace(";", ",").split(",") if c.strip()]


def placement(employee):
	"""(area_codes, department_code, why_not) for an Employee doc or dict."""
	s = frappe.get_doc("Biometric Settings")
	basis = (s.push_area_from or "Branch").lower()
	value = (employee.get(basis) or "").strip()
	rule = next((r for r in s.get("push_rules") or [] if (r.grohr_value or "").strip() == value and value), None)
	areas = _codes(rule.area_codes) if rule else _codes(s.push_default_area_codes)
	dept = (rule.department_code if rule and rule.department_code else None) or s.push_default_department_code or "1"
	if not areas:
		why = (_("No area rule for {0} {1!r} and no default area code in Biometric Settings.").format(s.push_area_from or "Branch", value)
		       if value else _("Employee has no {0}, and there is no default area code.").format(s.push_area_from or "Branch"))
		return [], dept, why
	return areas, str(dept).strip(), None


def global_ids(employee):
	"""Every ID that counts on all machines for this employee."""
	ids = frappe.get_all("Biometric Enrolment", filters={"employee": employee, "enabled": 1, "device": ["is", "not set"]},
	                     pluck="device_user_id", order_by="creation asc")
	own = (frappe.db.get_value("Employee", employee, "attendance_device_id") or "").strip()
	if own and own not in ids:
		ids.append(own)
	return [i.strip() for i in ids if i and i.strip()]


# --------------------------------------------------------------------- queue


def queue(employee, device_user_id, action=ADD, reason=None, overwrite=False, force=False):
	"""One row per ID while it is open: a newer request replaces an older one that
	has not finished. Returns the row name, or None when off / nothing to do."""
	device_user_id = (device_user_id or "").strip()
	if not device_user_id or not (force or enabled()):
		return None
	try:
		name = frappe.db.get_value(DOCTYPE, {"device_user_id": device_user_id, "status": ["in", OPEN]}, "name",
		                           order_by="creation desc")
		doc = frappe.get_doc(DOCTYPE, name) if name else frappe.new_doc(DOCTYPE)
		if doc.name and doc.employee != employee:
			# The ID moved to someone else before the old request finished.
			doc.db_set({"status": "Skipped", "message": _("Superseded: the ID now belongs to {0}.").format(employee)})
			doc = frappe.new_doc(DOCTYPE)
		# Overwrite permission is about one person; it does not survive a change of action.
		keep_overwrite = bool(doc.name) and doc.action == action and doc.overwrite
		doc.update({
			"employee": employee, "device_user_id": device_user_id, "action": action,
			"status": "Queued", "overwrite": 1 if (overwrite or keep_overwrite) else 0,
			"requested_on": now_datetime(), "attempts": 0, "reason": (reason or "")[:500],
			"outcome": None, "message": None,
		})
		doc.save(ignore_permissions=True) if doc.name else doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(title=f"Biometric Hub: could not queue Easy Time Pro push for {device_user_id}")
		return None


def queue_employee(employee, reason=None, force=False):
	"""Add / Update every global ID of an active employee. Returns names queued."""
	status = frappe.db.get_value("Employee", employee, "status")
	if status != "Active":
		return []
	return [n for n in (queue(employee, uid, ADD, reason, force=force) for uid in global_ids(employee)) if n]


def _pushed_before(device_user_id):
	return frappe.db.exists(DOCTYPE, {"device_user_id": device_user_id, "action": ADD,
	                                  "outcome": ["in", ("Created", "Updated")]})


# --------------------------------------------------------------------- hooks


def on_enrolment_change(enrolment):
	"""`Biometric Enrolment.on_update`: a new or changed global ID is pushed."""
	if not enabled() or frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_import:
		return
	if enrolment.device or not enrolment.enabled:
		return
	before = enrolment.get_doc_before_save()
	if before and before.device_user_id == enrolment.device_user_id and before.employee == enrolment.employee \
			and before.enabled == enrolment.enabled and not before.device:
		return
	if frappe.db.get_value("Employee", enrolment.employee, "status") == "Active":
		queue(enrolment.employee, enrolment.device_user_id, ADD, _("New biometric ID in GroHR."))


def on_employee_update(doc, method=None):
	"""`Employee.on_update`: leaving removes them from the machines; a name or
	placement change updates users we pushed before. Never blocks the save."""
	if not enabled() or frappe.flags.in_install or frappe.flags.in_migrate:
		return
	try:
		before = doc.get_doc_before_save()
		if not before or not any(before.get(f) != doc.get(f) for f in WATCHED):
			return
		ids = global_ids(doc.name)
		if before.status == "Active" and doc.status != "Active":
			if _remove_on_exit():
				for uid in ids:
					queue(doc.name, uid, REMOVE, _("Employee status changed to {0}.").format(doc.status))
			return
		if doc.status != "Active":
			return
		if before.status != "Active":
			for uid in ids:
				queue(doc.name, uid, ADD, _("Employee is active again."))
			return
		for uid in ids:
			if _pushed_before(uid):
				# We created or confirmed this user, so updating them is safe.
				queue(doc.name, uid, ADD, _("Name, branch, department or company changed."), overwrite=True)
	except Exception:
		frappe.log_error(title=f"Biometric Hub: Easy Time Pro push for {doc.name} failed to queue")


# ------------------------------------------------------------ to the agent


def _payload(row):
	emp = frappe.db.get_value(
		"Employee", row.employee,
		["employee_name", "first_name", "last_name", "branch", "department", "company", "gender",
		 "cell_number", "date_of_joining", "relieving_date", "status"], as_dict=True)
	if not emp:
		return None, _("Employee {0} no longer exists.").format(row.employee)
	base = {"sync_id": row.name, "emp_code": row.device_user_id}
	if row.action == REMOVE:
		return {**base, "action": "disable",
		        "resign_date": str(getdate(emp.relieving_date or today())),
		        "reason": f"{emp.status} - from GroHR"}, None
	areas, dept, why = placement(emp)
	if why:
		return None, why
	out = {**base, "action": "upsert", "first_name": (emp.employee_name or emp.first_name or "")[:25],
	       "last_name": "", "department_code": dept, "area_codes": areas, "overwrite": bool(row.overwrite)}
	if emp.cell_number:
		out["mobile"] = str(emp.cell_number)[:20]
	if emp.date_of_joining:
		out["hire_date"] = str(emp.date_of_joining)
	if (emp.gender or "").lower() in ("male", "female"):
		out["gender"] = emp.gender[0].upper()
	return out, None


def pending(connector, limit=PER_HEARTBEAT):
	"""Rows for this connector's heartbeat reply. Marks them Sent. A Sent row with
	no answer after RESEND_AFTER_MINUTES goes again (the API calls are idempotent);
	after MAX_ATTEMPTS it becomes an Error so it stops quietly retrying."""
	if not enabled() or frappe.db.get_single_value("Biometric Settings", "push_connector") != connector:
		return []
	stale = add_to_date(now_datetime(), minutes=-RESEND_AFTER_MINUTES)
	rows = frappe.db.sql(
		f"""SELECT name FROM `tab{DOCTYPE}`
		    WHERE status = 'Queued' OR (status = 'Sent' AND sent_on < %s)
		    ORDER BY requested_on ASC LIMIT %s""", (stale, limit), as_dict=True)
	out = []
	for r in rows:
		row = frappe.get_doc(DOCTYPE, r.name)
		if cint(row.attempts) >= MAX_ATTEMPTS:
			row.db_set({"status": "Error", "message": _("No answer from the agent after {0} attempts. Is it 0.4.0+?").format(MAX_ATTEMPTS)})
			continue
		payload, why = _payload(row)
		if why:
			row.db_set({"status": "Skipped", "message": why})
			continue
		row.db_set({"status": "Sent", "sent_on": now_datetime(), "attempts": cint(row.attempts) + 1,
		            "payload": json.dumps(payload, indent=1)})
		out.append(payload)
	return out


# ----------------------------------------------------------- from the agent


def _same_person(name_there, employee):
	from biometric_hub.engine.device_users import _bare_name, _similar

	ours = frappe.db.get_value("Employee", employee, "employee_name") or ""
	return max(_similar(name_there, ours), _similar(_bare_name(name_there), ours)) >= SAME_PERSON


def receive(results):
	"""What the agent did. Each: sync_id, outcome, message, remote_id, existing_name."""
	counts = {"done": 0, "conflict": 0, "requeued": 0, "error": 0, "dry_run": 0, "ignored": 0}
	for r in results or []:
		name = r.get("sync_id")
		if not name or not frappe.db.exists(DOCTYPE, name):
			counts["ignored"] += 1
			continue
		row = frappe.get_doc(DOCTYPE, name)
		if row.status != "Sent":
			counts["ignored"] += 1  # superseded or answered already
			continue
		outcome = str(r.get("outcome") or "Error")[:60]
		update = {"outcome": outcome, "done_on": now_datetime(), "message": (r.get("message") or "")[:1000] or None,
		          "remote_id": str(r.get("remote_id") or "") or row.remote_id,
		          "name_in_easytime": (r.get("existing_name") or "")[:140] or row.name_in_easytime}
		if outcome in ("Created", "Updated", "Resigned", "Not Found"):
			update["status"] = "Done"
			counts["done"] += 1
		elif outcome == "Dry Run":
			update["status"] = "Dry Run"
			counts["dry_run"] += 1
		elif outcome == "Exists":
			if _same_person(r.get("existing_name"), row.employee):
				update.update(status="Queued", overwrite=1, requested_on=now_datetime(),
				              message=_("Already in Easy Time Pro as {0!r} — same person, updating.").format(r.get("existing_name")))
				counts["requeued"] += 1
			else:
				update.update(status="Conflict", message=_(
					"Easy Time Pro already has ID {0} as {1!r}, which does not look like {2}. Nothing was changed. "
					"Check the ID; if it really is this person, use Overwrite.").format(
					row.device_user_id, r.get("existing_name"), row.employee_name))
				counts["conflict"] += 1
		else:
			update["status"] = "Error"
			counts["error"] += 1
		row.db_set(update)
	return counts


# ------------------------------------------------------------------ for HR


def overwrite(name):
	row = frappe.get_doc(DOCTYPE, name)
	if row.status not in ("Conflict", "Error", "Dry Run"):
		frappe.throw(_("Only a Conflict, Error or Dry Run row can be overwritten."))
	row.db_set({"status": "Queued", "overwrite": 1, "attempts": 0, "requested_on": now_datetime(),
	            "message": _("HR confirmed the existing user is {0}; overwrite on next check-in.").format(row.employee_name)})
	return row.name


def retry(name):
	row = frappe.get_doc(DOCTYPE, name)
	if row.status not in ("Error", "Skipped", "Dry Run", "Conflict"):
		frappe.throw(_("Only an Error, Skipped, Dry Run or Conflict row can be retried."))
	row.db_set({"status": "Queued", "attempts": 0, "requested_on": now_datetime(), "message": None})
	return row.name


def requeue_status(status):
	"""After commissioning: every Dry Run (or Error) row goes again."""
	if status not in ("Dry Run", "Error"):
		frappe.throw(_("Only Dry Run or Error rows can be requeued in bulk."))
	names = frappe.get_all(DOCTYPE, filters={"status": status}, pluck="name")
	for n in names:
		frappe.db.set_value(DOCTYPE, n, {"status": "Queued", "attempts": 0, "requested_on": now_datetime()})
	return len(names)


def preview_all(branch=None):
	"""What 'Push all active employees' would do — read-only."""
	filters = {"status": "Active"}
	if branch:
		filters["branch"] = branch
	emps = frappe.get_all("Employee", filters=filters,
	                      fields=["name", "employee_name", "branch", "department", "company"], limit_page_length=0)
	out = {"employees": len(emps), "ids": 0, "no_id": 0, "no_area": 0, "already_done": 0, "no_area_values": {}}
	for e in emps:
		ids = global_ids(e.name)
		if not ids:
			out["no_id"] += 1
			continue
		_areas, _dept, why = placement(e)
		if why:
			out["no_area"] += 1
			key = e.get((frappe.db.get_single_value("Biometric Settings", "push_area_from") or "Branch").lower()) or "(blank)"
			out["no_area_values"][key] = out["no_area_values"].get(key, 0) + 1
			continue
		for uid in ids:
			if _pushed_before(uid):
				out["already_done"] += 1
			else:
				out["ids"] += 1
	return out


def push_all(branch=None):
	"""Queue every active employee's IDs that were never pushed. Skips the ones
	with no area (preview_all lists them) — they would only be Skipped later."""
	filters = {"status": "Active"}
	if branch:
		filters["branch"] = branch
	n = 0
	for e in frappe.get_all("Employee", filters=filters, fields=["name", "branch", "department", "company"],
	                        limit_page_length=0):
		if placement(e)[2]:
			continue
		for uid in global_ids(e.name):
			if not _pushed_before(uid) and queue(e.name, uid, ADD, _("Bulk push from HR.")):
				n += 1
	return n


def employee_status(employee):
	"""Latest row per ID — for the Employee form."""
	rows = frappe.get_all(DOCTYPE, filters={"employee": employee},
	                      fields=["name", "device_user_id", "action", "status", "outcome", "message", "modified"],
	                      order_by="modified desc", limit=20)
	latest = {}
	for r in rows:
		latest.setdefault(r.device_user_id, r)
	return {"enabled": enabled(), "rows": list(latest.values())}
