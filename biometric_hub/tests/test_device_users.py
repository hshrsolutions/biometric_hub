"""Device user directory: import the attendance software's users and photos, let
HR confirm who each machine ID is, and repair the punches behind it.

	bench --site <site> execute biometric_hub.tests.test_device_users.run

Builds on the ingestion fixtures and removes everything it made.
"""

import csv
import io
import zipfile

import frappe

from biometric_hub.engine import device_users as du
from biometric_hub.engine import ingest as engine
from biometric_hub.tests import test_ingest as fixtures

ID_KNOWN = fixtures.ID_OK
ID_PHOTO = f"{fixtures.PREFIX}-DU-1"
ID_NOBODY = f"{fixtures.PREFIX}-DU-2"

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def _jpeg():
	from PIL import Image

	buf = io.BytesIO()
	Image.new("RGB", (40, 50), (109, 76, 232)).save(buf, "JPEG")
	return buf.getvalue()


def _unique_unlinked(exclude):
	"""An active employee with no device ID whose name no other unlinked employee resembles."""
	owners = set(du._owners().values())
	pool = [e for e in frappe.get_all("Employee", filters={"status": "Active"}, fields=["name", "employee_name"],
	                                  limit_page_length=0) if e.name not in owners and e.name != exclude]
	for e in pool:
		others = [o for o in pool if o.name != e.name]
		if du._norm(e.employee_name) and all(du._similar(e.employee_name, o.employee_name) < 0.85 for o in others):
			return e
	return None


def _export_xlsx(rows):
	import openpyxl

	wb = openpyxl.Workbook()
	ws = wb.active
	ws.append([None, "Employee"])  # title row, as the real export has
	ws.append(["New Em.ID", "Employee Id", "First Name", "Department", "Area", "Fingerprint", "Face"])
	for r in rows:
		ws.append(r)
	buf = io.BytesIO()
	wb.save(buf)
	return buf.getvalue()


def _photo_zip(photos):
	buf = io.BytesIO()
	manifest = io.StringIO()
	w = csv.writer(manifest)
	w.writerow(["device_user_id", "name", "photo_file", "source"])
	with zipfile.ZipFile(buf, "w") as z:
		for uid, data in photos.items():
			z.writestr(f"photos/{uid}.jpg", data)
			w.writerow([uid, "", f"photos/{uid}.jpg", "blob"])
		z.writestr("manifest.csv", manifest.getvalue())
	return buf.getvalue()


def _cleanup():
	for name in frappe.get_all("Biometric Device User", filters={"device_user_id": ["like", f"{fixtures.PREFIX}%"]}, pluck="name"):
		for f in frappe.get_all("File", filters={"attached_to_doctype": "Biometric Device User", "attached_to_name": name}, pluck="name"):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		frappe.delete_doc("Biometric Device User", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def test_read(target):
	print("Reading the export and the photo zip:")
	users = du.read_users(_export_xlsx([
		[None, ID_KNOWN, "XX, SOMEONE", "STAFF", "MDU SRM FO,CORPORATE OFFICE", "Ver 13:2", "-"],
		[None, ID_PHOTO, f"AB, {target.employee_name.upper()}", "STAFF", "MDU SRM FO", "Ver 13:2", "Ver 36:1"],
		[None, ID_NOBODY, "ZQ, XYLOPHONE QUENTIN", "STAFF", "BTC OFFICE", "-", "-"],
	]), "export.xlsx")
	check("the header is found below the title row", len(users) == 3, users)
	check("areas are kept", users[0].get("areas") == "MDU SRM FO,CORPORATE OFFICE", users[0])
	check("'-' means nothing", "has_face" not in users[0], users[0])
	photos = du.read_photos(_photo_zip({ID_PHOTO: _jpeg()}))
	check("the photo zip is read through its manifest", set(photos) == {ID_PHOTO}, list(photos))
	return users, photos


def test_import(users, photos, active, target):
	print("Import:")
	dry = du.import_users(users, photos, apply=False)
	check("a dry run writes nothing", not frappe.db.exists("Biometric Device User", ID_PHOTO), dry)
	check("but reports what it would do", dry["created"] == 3 and dry["photos"] == 1, dry)

	out = du.import_users(users, photos, apply=True)
	check("apply creates one user per row", out["created"] == 3, out)
	known = frappe.get_doc("Biometric Device User", ID_KNOWN)
	check("an ID already linked shows as Confirmed with its employee",
	      known.match_status == "Confirmed" and known.employee == active, known.as_dict())
	sug = frappe.get_doc("Biometric Device User", ID_PHOTO)
	check("a name match is Suggested, not confirmed", sug.match_status == "Suggested" and sug.employee == target.name,
	      (sug.match_status, sug.employee, sug.match_note))
	check("its photo is attached", bool(sug.photo))
	f = frappe.db.get_value("File", {"file_url": sug.photo}, ["is_private", "attached_to_name"], as_dict=True)
	check("as a private file", f and f.is_private == 1 and f.attached_to_name == ID_PHOTO, f)
	nobody = frappe.get_doc("Biometric Device User", ID_NOBODY)
	check("no match stays Unchecked", nobody.match_status == "Unchecked" and not nobody.employee, nobody.match_status)


def test_confirm_repairs_attendance(target):
	print("Confirm by photo:")
	out = engine.ingest([fixtures._punch(ID_PHOTO, minutes_ago=77)], source="Push")
	frappe.db.commit()
	check("setup: the machine ID's punch is Unmapped", out["unmapped"] == 1, out)

	result = du.confirm(ID_PHOTO, target.name)
	frappe.db.commit()
	check("confirming links the ID", frappe.db.exists("Biometric Enrolment", {"device_user_id": ID_PHOTO, "employee": target.name}))
	check("and repairs the parked punch", result["mapped"] == 1, result)
	check("status is Confirmed", frappe.db.get_value("Biometric Device User", ID_PHOTO, "match_status") == "Confirmed")

	try:
		du.confirm(ID_KNOWN, target.name)
		check("an ID linked to someone else cannot be confirmed for another", False)
	except frappe.ValidationError:
		frappe.db.rollback()
		check("an ID linked to someone else cannot be confirmed for another", True)


def test_reject_and_reimport(users, photos):
	print("Reject, and re-import keeps HR's decisions:")
	du.reject(ID_NOBODY)
	frappe.db.commit()
	check("rejecting marks Not This Person",
	      frappe.db.get_value("Biometric Device User", ID_NOBODY, "match_status") == "Not This Person")
	out = du.import_users(users, photos, apply=True)
	check("re-import updates rather than duplicates", out["updated"] == 3 and out["created"] == 0, out)
	check("a confirmed match survives re-import",
	      frappe.db.get_value("Biometric Device User", ID_PHOTO, "match_status") == "Confirmed")
	check("a rejection survives re-import",
	      frappe.db.get_value("Biometric Device User", ID_NOBODY, "match_status") == "Not This Person")
	check("the photo is replaced, not piled up",
	      frappe.db.count("File", {"attached_to_doctype": "Biometric Device User", "attached_to_name": ID_PHOTO}) == 1)


def run():
	print("Biometric Hub — device user directory (photos)\n")
	active, left = fixtures._employees()
	target = _unique_unlinked(active)
	if not target:
		print("SKIPPED — no active employee without a device ID and with a distinctive name.")
		return
	_cleanup()
	fixtures.teardown()
	fixtures.setup(active, left)
	try:
		users, photos = test_read(target)
		test_import(users, photos, active, target)
		test_confirm_repairs_attendance(target)
		test_reject_and_reimport(users, photos)
	finally:
		_cleanup()
		fixtures.teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_device_users: {len(failed)} check(s) failed")
	print("All good.")
