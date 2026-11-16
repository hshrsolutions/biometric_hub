"""Change a device ID in GroHR -> the office-PC agent sends that user's name and
photo from the attendance software. End to end, with the real agent process.

	bench --site <site> execute biometric_hub.tests.test_agent_profiles_e2e.run

Creates throwaway `iclock_transaction`- and `personnel_employee`-shaped tables in
this site's own database, turns on Fetch Profiles, enrols an ID, runs the agent
once over HTTP, and checks the Biometric Device User received name and photo.
Drops both tables, restores the setting and deletes the agent's working
directory (it held the database password). Skipped if the agent repo is absent.
"""

import io
import json
import os
import shutil
import subprocess

import frappe

from biometric_hub.tests import test_agent_e2e as e2e
from biometric_hub.tests import test_agent_sql_e2e as sqle2e
from biometric_hub.tests import test_ingest as fixtures

PEOPLE = "_bh_agent_src_people"
ID_WITH_PHOTO = f"{fixtures.PREFIX}-PF-1"
ID_NOT_THERE = f"{fixtures.PREFIX}-PF-2"

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def _jpeg():
	from PIL import Image

	buf = io.BytesIO()
	Image.new("RGB", (30, 40), (20, 160, 90)).save(buf, "JPEG")
	return buf.getvalue()


def _people_table():
	frappe.db.sql(f"DROP TABLE IF EXISTS `{PEOPLE}`")
	frappe.db.sql(f"CREATE TABLE `{PEOPLE}` (id INT AUTO_INCREMENT PRIMARY KEY, emp_code VARCHAR(40), "
	              f"first_name VARCHAR(140), photo LONGBLOB)")
	frappe.db.sql(f"INSERT INTO `{PEOPLE}` (emp_code, first_name, photo) VALUES (%s, %s, %s)",
	              (ID_WITH_PHOTO, "PF, PROFILE PERSON", _jpeg()))
	frappe.db.commit()


def _cleanup():
	for name in frappe.get_all("Biometric Device User", filters={"device_user_id": ["like", f"{fixtures.PREFIX}%"]}, pluck="name"):
		for f in frappe.get_all("File", filters={"attached_to_doctype": "Biometric Device User", "attached_to_name": name}, pluck="name"):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		frappe.delete_doc("Biometric Device User", name, force=True, ignore_permissions=True)
	frappe.db.commit()


def run():
	print("Biometric Hub — ID change fetches the profile from the office PC, end to end\n")
	if not os.path.isdir(e2e.AGENT_DIR):
		print(f"  SKIPPED — no agent repository at {e2e.AGENT_DIR}")
		return

	active, left = fixtures._employees()
	was_on = frappe.db.get_single_value("Biometric Settings", "fetch_profiles")
	_cleanup()
	e2e._teardown()
	fixtures.setup(active, left)
	workdir = e2e._setup(active)
	frappe.db.set_value("Biometric Device", fixtures.DEVICE, "driver", "SQL Database", update_modified=False)
	frappe.db.set_single_value("Biometric Settings", "fetch_profiles", 1)
	frappe.db.commit()
	try:
		sqle2e._make_table([])
		_people_table()
		config_path = sqle2e._config(workdir)
		with open(config_path) as fh:
			cfg = json.load(fh)
		cfg["profile"] = {"source": fixtures.SERIAL, "table": PEOPLE}
		with open(config_path, "w") as fh:
			json.dump(cfg, fh)

		print("HR sets two IDs in GroHR:")
		for uid in (ID_WITH_PHOTO, ID_NOT_THERE):
			frappe.get_doc({"doctype": "Biometric Enrolment", "employee": active, "device_user_id": uid,
			                "device": fixtures.DEVICE, "enabled": 1}).insert(ignore_permissions=True)
		frappe.db.commit()
		status = frappe.db.get_value("Biometric Device User", ID_WITH_PHOTO, ["profile_status", "employee"], as_dict=True)
		check("a profile request is queued for the new ID", status and status.profile_status == "Requested", status)
		check("against the employee HR linked it to", status and status.employee == active, status)

		print("The office-PC agent checks in:")
		env = {**os.environ, "PYTHONPATH": e2e.AGENT_DIR}
		result = subprocess.run([e2e.PYTHON, "-m", "grohr_agent", "--config", config_path, "--once", "--verbose"],
		                        cwd=e2e.AGENT_DIR, env=env, capture_output=True, text=True, timeout=180)
		check("the agent exits cleanly", result.returncode == 0, (result.stdout or "")[-500:] + (result.stderr or "")[-500:])
		frappe.db.rollback()

		got = frappe.db.get_value("Biometric Device User", ID_WITH_PHOTO,
		                          ["profile_status", "name_on_device", "photo"], as_dict=True)
		check("the profile arrives", got.profile_status == "Received", got)
		check("with the name from the attendance software", got.name_on_device == "PF, PROFILE PERSON", got)
		check("and the photo, as a private file", bool(got.photo) and got.photo.startswith("/private/files/"), got.photo)
		missing = frappe.db.get_value("Biometric Device User", ID_NOT_THERE, "profile_status")
		check("an ID the software does not know is reported Not Found", missing == "Not Found", missing)

		from biometric_hub.engine.device_users import employee_profiles, pending_requests

		check("nothing is left waiting", not [i for i in pending_requests() if i.startswith(fixtures.PREFIX)])
		shown = {p["name"] for p in employee_profiles(active)["profiles"]}
		check("the Employee form panel lists both IDs", {ID_WITH_PHOTO, ID_NOT_THERE} <= shown, shown)

		print("The vendor database was not written to:")
		check("the people table still has exactly its one row",
		      frappe.db.sql(f"SELECT COUNT(*) FROM `{PEOPLE}`")[0][0] == 1)
	finally:
		frappe.db.sql(f"DROP TABLE IF EXISTS `{PEOPLE}`")
		frappe.db.sql(f"DROP TABLE IF EXISTS `{sqle2e.TABLE}`")
		shutil.rmtree(workdir, ignore_errors=True)
		frappe.db.set_single_value("Biometric Settings", "fetch_profiles", was_on or 0)
		frappe.db.commit()
		_cleanup()
		e2e._teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_agent_profiles_e2e: {len(failed)} check(s) failed")
	print("All good.")
