"""The device user directory — every user the attendance software knows, with a
photo, so HR can see a face and confirm which employee a machine ID belongs to.

Fed by two files from the customer's office PC:
  * the attendance software's employee export (.xlsx/.csv: ID, name, areas, ...)
  * the photo zip from the agent's `tools/export_photos.py`

Writes only our own doctypes. Confirming a match creates the `Biometric Enrolment`
and replays the punches parked behind that ID; the Employee itself is mirrored
by engine/enrolment.py exactly as for any other enrolment.
"""

import csv
import io
import os
import re
import zipfile
from difflib import SequenceMatcher

import frappe
from frappe import _
from frappe.utils import add_days, today

# Column names seen in eSSL Easy Time Pro / BioTime exports, lower-cased.
COLUMNS = {
	"device_user_id": ("employee id", "emp code", "emp_code", "user id", "userid", "badgenumber", "device_user_id"),
	"name_on_device": ("first name", "name", "employee name", "first_name"),
	"department_on_device": ("department", "dept"),
	"areas": ("area", "areas"),
	"has_fingerprint": ("fingerprint",),
	"has_face": ("face",),
}


def _norm(s):
	return re.sub(r"[^a-z]", "", str(s or "").lower())


def _bare_name(name_on_device):
	"""'VH, VENKATESH' / 'KARUPPASAMY-DKY' / 'SW42, VEERAMMAL' -> the person's name."""
	toks = [t.strip() for t in re.split(r"[,\-]", str(name_on_device or "")) if t.strip()]
	if len(toks) > 1:
		toks = [t for t in toks if not re.fullmatch(r"[A-Z]{1,4}\d{0,3}|[A-Z]{1,3}\s?\d+", t)] or toks
	return re.sub(r"\b(VNR|NTU|MDU|NOD|NDA)\b", "", " ".join(toks)).strip()


def _similar(a, b):
	a, b = _norm(a), _norm(b)
	return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


# --------------------------------------------------------------------- reading


def read_users(path_or_bytes, filename=None):
	"""Rows from the attendance software's employee export, keyed by our field names."""
	name = (filename or str(path_or_bytes)).lower()
	data = path_or_bytes if isinstance(path_or_bytes, bytes) else open(path_or_bytes, "rb").read()
	if name.endswith(".csv"):
		rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
	else:
		import openpyxl

		wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
		rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]

	# The header is the first row that names an ID column (exports often start with a title).
	for i, row in enumerate(rows):
		cells = [str(c or "").strip().lower() for c in row]
		if any(c in COLUMNS["device_user_id"] for c in cells):
			header, body = cells, rows[i + 1:]
			break
	else:
		frappe.throw(_("No ID column found. Expected one of: {0}").format(", ".join(COLUMNS["device_user_id"])))

	index = {}
	for field, names in COLUMNS.items():
		for n in names:
			if n in header:
				index[field] = header.index(n)
				break

	out = []
	for row in body:
		uid = str(row[index["device_user_id"]] or "").strip() if index["device_user_id"] < len(row) else ""
		if not uid:
			continue
		rec = {"device_user_id": uid}
		for field, col in index.items():
			if field != "device_user_id" and col < len(row) and row[col] not in (None, "", "-"):
				rec[field] = str(row[col]).strip()
		out.append(rec)
	return out


def read_photos(zip_path_or_bytes):
	"""{device_user_id: (filename, bytes)} from the agent's photo zip."""
	data = zip_path_or_bytes if isinstance(zip_path_or_bytes, bytes) else open(zip_path_or_bytes, "rb").read()
	photos = {}
	with zipfile.ZipFile(io.BytesIO(data)) as z:
		names = set(z.namelist())
		if "manifest.csv" in names:
			for r in csv.DictReader(io.StringIO(z.read("manifest.csv").decode("utf-8-sig"))):
				f = (r.get("photo_file") or "").strip()
				if f and f in names:
					photos[r["device_user_id"].strip()] = (os.path.basename(f), z.read(f))
		else:
			for f in names:
				stem, ext = os.path.splitext(os.path.basename(f))
				if stem and ext.lower() in (".jpg", ".jpeg", ".png", ".bmp"):
					photos[stem] = (os.path.basename(f), z.read(f))
	return photos


# ---------------------------------------------------------------- suggestion


def _activity():
	since = add_days(today(), -30)
	rows = frappe.db.sql(
		"""SELECT device_user_id, SUM(punch_time >= %s) AS recent, MAX(punch_time) AS last
		   FROM `tabBiometric Punch` GROUP BY device_user_id""",
		(since,), as_dict=True,
	)
	return {r.device_user_id: r for r in rows}


def _owners():
	"""device_user_id -> employee, from global enrolments, then Employee.attendance_device_id."""
	owners = {}
	for r in frappe.get_all("Employee", filters={"attendance_device_id": ["is", "set"]},
	                        fields=["name", "attendance_device_id"], limit_page_length=0):
		owners[str(r.attendance_device_id).strip()] = r.name
	for r in frappe.get_all("Biometric Enrolment", filters={"enabled": 1, "device": ["is", "not set"]},
	                        fields=["employee", "device_user_id"], limit_page_length=0):
		owners[str(r.device_user_id).strip()] = r.employee
	return owners


def suggest(name_on_device, candidates):
	"""(employee, note) for the best clear name match among `candidates`
	(dicts with name, employee_name), or (None, reason)."""
	bare = _bare_name(name_on_device)
	scored = sorted(((_similar(bare, c["employee_name"]), c) for c in candidates), key=lambda x: -x[0])
	if not scored or scored[0][0] < 0.85:
		return None, "No clear name match."
	best, runner = scored[0], (scored[1][0] if len(scored) > 1 else 0)
	if best[0] - runner < 0.08:
		return None, "Several employees have a similar name - choose by photo."
	return best[1]["name"], f"Name match {best[0]:.0%} - check the photo."


# -------------------------------------------------------------------- import


def import_users(users, photos=None, apply=False):
	"""Create or update one Biometric Device User per row. Dry run unless `apply`.

	Never overwrites a match HR has already decided (Confirmed / Not This Person).
	"""
	photos = photos or {}
	activity = _activity()
	owners = _owners()
	unlinked = frappe.get_all(
		"Employee", filters={"status": "Active"}, fields=["name", "employee_name"], limit_page_length=0
	)
	owned = set(owners.values())
	unlinked = [e for e in unlinked if e.name not in owned]

	counts = {"created": 0, "updated": 0, "photos": 0, "already_linked": 0, "suggested": 0, "unknown": 0}
	for row in users:
		uid = row["device_user_id"]
		existing = frappe.db.exists("Biometric Device User", uid)
		doc = frappe.get_doc("Biometric Device User", uid) if existing else frappe.new_doc("Biometric Device User")
		doc.device_user_id = uid
		for field in ("name_on_device", "department_on_device", "areas", "has_fingerprint", "has_face"):
			if row.get(field):
				doc.set(field, row[field])
		act = activity.get(uid)
		doc.punches_30d = int(act.recent or 0) if act else 0
		doc.last_punch = act.last if act else None

		if doc.match_status not in ("Confirmed", "Not This Person"):
			if uid in owners:
				doc.employee, doc.match_status = owners[uid], "Confirmed"
				doc.match_note = "Already linked in GroHR."
				counts["already_linked"] += 1
			else:
				emp, note = suggest(doc.name_on_device, unlinked)
				doc.employee = emp
				doc.match_status = "Suggested" if emp else "Unchecked"
				doc.match_note = note
				counts["suggested" if emp else "unknown"] += 1

		counts["updated" if existing else "created"] += 1
		if not apply:
			continue
		doc.save(ignore_permissions=True) if existing else doc.insert(ignore_permissions=True)

		if uid in photos:
			fname, content = photos[uid]
			_attach_photo(doc, fname, content)
			counts["photos"] += 1

	if apply:
		frappe.db.commit()
	else:
		frappe.db.rollback()
		counts["photos"] = sum(1 for r in users if r["device_user_id"] in photos)
	return counts


def _attach_photo(doc, fname, content):
	"""Private file — these are people's faces."""
	if doc.photo:
		old = frappe.db.get_value("File", {"file_url": doc.photo, "attached_to_doctype": doc.doctype,
		                                   "attached_to_name": doc.name}, "name")
		if old:
			frappe.delete_doc("File", old, ignore_permissions=True, force=True)
	f = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"device-user-{re.sub(r'[^A-Za-z0-9_.-]', '_', doc.name)}{os.path.splitext(fname)[1] or '.jpg'}",
			"is_private": 1,
			"content": content,
			"attached_to_doctype": doc.doctype,
			"attached_to_name": doc.name,
			"attached_to_field": "photo",
		}
	).insert(ignore_permissions=True)
	doc.db_set("photo", f.file_url, update_modified=False)


# ------------------------------------------------------------- HR decisions


def confirm(device_user, employee):
	"""HR says: this machine ID is this employee. Link it and repair their attendance."""
	from biometric_hub.biometric_hub.doctype.biometric_enrolment.biometric_enrolment import enrolment_key
	from biometric_hub.engine.ingest import replay_unmapped

	doc = frappe.get_doc("Biometric Device User", device_user)
	if not employee or not frappe.db.exists("Employee", employee):
		frappe.throw(_("Choose the employee first."))

	uid = doc.device_user_id
	enrolled = frappe.db.get_value(
		"Biometric Enrolment", {"enrolment_key": enrolment_key(uid)}, ["name", "employee", "enabled"], as_dict=True
	)
	if enrolled and enrolled.employee != employee:
		frappe.throw(
			_("ID {0} is already linked to {1} (enrolment {2}). If both people really use this number on "
			  "different machines, link it per machine in Employee Device Access instead.").format(
				uid, enrolled.employee, enrolled.name)
		)
	if not enrolled:
		frappe.get_doc(
			{"doctype": "Biometric Enrolment", "employee": employee, "device_user_id": uid, "enabled": 1,
			 "notes": f"Confirmed by photo in Biometric Device User {doc.name}."}
		).insert(ignore_permissions=True)
	elif not enrolled.enabled:
		frappe.db.set_value("Biometric Enrolment", enrolled.name, "enabled", 1)

	doc.employee = employee
	doc.match_status = "Confirmed"
	doc.match_note = f"Confirmed by {frappe.session.user}."
	doc.save(ignore_permissions=True)
	replayed = replay_unmapped(uid)
	return {"employee": employee, **replayed}


def reject(device_user):
	doc = frappe.get_doc("Biometric Device User", device_user)
	if doc.match_status == "Confirmed":
		frappe.throw(_("Already confirmed and linked. Change the enrolment if it is wrong."))
	doc.employee = None
	doc.match_status = "Not This Person"
	doc.match_note = f"Rejected by {frappe.session.user}."
	doc.save(ignore_permissions=True)
	return doc.match_status


# ------------------------------------------------- profiles from the office PC
#
# The office PC cannot be called — the agent there only ever calls out. So a
# request is a row in Requested state; the heartbeat reply lists them; the agent
# looks them up read-only in the attendance software and posts them back.

MAX_PHOTO_BYTES = 2 * 1024 * 1024
REQUESTS_PER_HEARTBEAT = 50


def profiles_enabled():
	return bool(frappe.db.get_single_value("Biometric Settings", "fetch_profiles"))


def request_profile(device_user_id, employee=None, force=False):
	"""Queue a fetch of this ID's name and photo. Creates the directory row if
	needed. Never raises into the caller — an ID change must never fail on this."""
	device_user_id = (device_user_id or "").strip()
	if not device_user_id or not (force or profiles_enabled()):
		return None
	try:
		if frappe.db.exists("Biometric Device User", device_user_id):
			doc = frappe.get_doc("Biometric Device User", device_user_id)
		else:
			doc = frappe.new_doc("Biometric Device User")
			doc.device_user_id = device_user_id
		if employee and doc.match_status not in ("Confirmed", "Not This Person"):
			doc.employee = employee
			doc.match_status = "Confirmed"
			doc.match_note = "Linked in GroHR - check the photo when it arrives."
		doc.profile_status = "Requested"
		doc.profile_requested = frappe.utils.now_datetime()
		doc.profile_error = None
		doc.save(ignore_permissions=True) if not doc.is_new() else doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(title=f"Biometric Hub: could not queue profile request for {device_user_id}")
		return None


def on_enrolment_change(enrolment):
	"""`Biometric Enrolment.on_update`: a new or changed ID asks for its profile."""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_import:
		return
	before = enrolment.get_doc_before_save()
	if before and before.device_user_id == enrolment.device_user_id and before.employee == enrolment.employee:
		return
	if enrolment.enabled:
		request_profile(enrolment.device_user_id, enrolment.employee)


def pending_requests(limit=REQUESTS_PER_HEARTBEAT):
	if not profiles_enabled():
		return []
	return frappe.get_all(
		"Biometric Device User", filters={"profile_status": "Requested"},
		pluck="device_user_id", order_by="profile_requested asc", limit=limit,
	)


def receive_profiles(profiles):
	"""What the agent found. Each: device_user_id, found, name, department,
	photo_base64, photo_ext, error."""
	import base64

	counts = {"received": 0, "not_found": 0, "errors": 0, "photos": 0, "ignored": 0}
	for p in profiles or []:
		uid = str(p.get("device_user_id") or "").strip()
		if not uid or not frappe.db.exists("Biometric Device User", uid):
			counts["ignored"] += 1
			continue
		doc = frappe.get_doc("Biometric Device User", uid)
		doc.profile_received = frappe.utils.now_datetime()
		if p.get("error"):
			doc.profile_status, doc.profile_error = "Error", str(p["error"])[:500]
			counts["errors"] += 1
		elif not p.get("found"):
			doc.profile_status, doc.profile_error = "Not Found", "The attendance software has no user with this ID."
			counts["not_found"] += 1
		else:
			doc.profile_status, doc.profile_error = "Received", None
			if p.get("name"):
				doc.name_on_device = str(p["name"])[:140]
			if p.get("department"):
				doc.department_on_device = str(p["department"])[:140]
			counts["received"] += 1
		doc.save(ignore_permissions=True)

		if p.get("found") and p.get("photo_base64"):
			try:
				data = base64.b64decode(p["photo_base64"])
				ok = data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
				if ok and len(data) <= MAX_PHOTO_BYTES:
					ext = "png" if data[:4] == b"\x89PNG" else "jpg"
					_attach_photo(doc, f"photo.{ext}", data)
					counts["photos"] += 1
				else:
					doc.db_set("profile_error", "Photo was not a JPEG/PNG under 2 MB; ignored.", update_modified=False)
			except Exception:
				doc.db_set("profile_error", "Photo could not be decoded.", update_modified=False)
	return counts


def employee_profiles(employee):
	"""Every machine profile for the IDs this employee holds — for the Employee form."""
	ids = set(frappe.get_all("Biometric Enrolment", filters={"employee": employee}, pluck="device_user_id"))
	own = frappe.db.get_value("Employee", employee, "attendance_device_id")
	if own:
		ids.add(own)
	ids |= set(frappe.get_all("Biometric Device User", filters={"employee": employee}, pluck="name"))
	rows = frappe.get_all(
		"Biometric Device User", filters={"name": ["in", list(ids) or [""]]},
		fields=["name", "name_on_device", "photo", "areas", "match_status", "profile_status",
		        "profile_requested", "profile_received", "profile_error", "punches_30d"],
	)
	missing = sorted(ids - {r.name for r in rows})
	return {"profiles": rows, "ids_without_profile": missing, "enabled": profiles_enabled()}
