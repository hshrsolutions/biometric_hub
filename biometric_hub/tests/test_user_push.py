"""GroHR → Easy Time Pro: an employee's biometric ID becomes a user in the
attendance software, through the real office-PC agent. End to end.

	bench --site <site> execute biometric_hub.tests.test_user_push.run

Starts the agent repo's in-memory Easy Time Pro (tests/fake_easytime.py) in this
process, turns on Push Employees with a rule for the test employee's company,
enrols IDs, runs the real agent once per step over HTTP and checks both sides.
Existing employees are read, never saved (the "leaves" step calls the hook on an
unsaved copy). Restores Biometric Settings and removes every row it made.
Skipped if the agent repo is absent.
"""

import copy
import json
import os

import frappe

from biometric_hub.engine import user_push
from biometric_hub.tests import test_agent_e2e as e2e
from biometric_hub.tests import test_ingest as fixtures

P = fixtures.PREFIX
ID_NEW = f"{P}-UP-1"       # not in Easy Time Pro: created
ID_DEVICE = f"{P}-UP-2"    # per-machine ID: never pushed
ID_OTHER = f"{P}-UP-3"     # Easy Time Pro has someone else: Conflict
ID_SAME = f"{P}-UP-4"      # Easy Time Pro has the same person: updated automatically
SETTINGS = ("push_users", "push_connector", "push_remove_on_exit", "push_area_from",
            "push_default_area_codes", "push_default_department_code")

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def _row(uid):
	name = frappe.db.get_value("Biometric User Sync", {"device_user_id": uid}, "name", order_by="creation desc")
	return frappe.get_doc("Biometric User Sync", name) if name else None


def _status(uid):
	r = _row(uid)
	return r.status if r else None


def _cleanup(employee=None, since=None):
	"""Our test IDs, plus anything queued for the test employee's real IDs during the
	run (the leaver step queues every global ID they hold)."""
	names = set(frappe.get_all("Biometric User Sync", filters={"device_user_id": ["like", f"{P}%"]}, pluck="name"))
	if employee and since:
		names |= set(frappe.get_all("Biometric User Sync", filters={"employee": employee, "creation": [">=", since]}, pluck="name"))
	for n in names:
		frappe.delete_doc("Biometric User Sync", n, force=True, ignore_permissions=True, delete_permanently=True)
	frappe.db.commit()


def _enrol(employee, uid, device=None):
	frappe.get_doc({"doctype": "Biometric Enrolment", "employee": employee, "device_user_id": uid,
	                "device": device, "enabled": 1}).insert(ignore_permissions=True)
	frappe.db.commit()


def _agent(workdir, label):
	result = e2e._run_agent(workdir)
	check(f"{label}: the agent exits cleanly", result.returncode == 0,
	      (result.stdout or "")[-600:] + (result.stderr or "")[-600:])
	frappe.db.rollback()  # see what the agent's requests committed
	return result


def run():
	print("Biometric Hub — push employees to Easy Time Pro, end to end\n")
	if not os.path.isdir(e2e.AGENT_DIR):
		print(f"  SKIPPED — no agent repository at {e2e.AGENT_DIR}")
		return
	# By path: another app on the bench already owns the module name `tests`.
	import importlib.util

	spec = importlib.util.spec_from_file_location("bh_fake_easytime", os.path.join(e2e.AGENT_DIR, "tests", "fake_easytime.py"))
	fake = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(fake)
	FakeEasyTime = fake.FakeEasyTime

	active, left = fixtures._employees()
	emp = frappe.db.get_value("Employee", active, ["employee_name", "company"], as_dict=True)
	s = frappe.get_doc("Biometric Settings")
	saved = {k: s.get(k) for k in SETTINGS}
	saved_rules = [r.as_dict() for r in s.get("push_rules") or []]

	started = frappe.utils.now_datetime()
	_cleanup()
	e2e._teardown()
	fixtures.setup(active, left)
	workdir = e2e._setup(active)
	srv = FakeEasyTime().start()
	try:
		print("Switched off:")
		_enrol(active, f"{P}-UP-0")
		check("a new ID queues nothing", _status(f"{P}-UP-0") is None)

		print("Switched on, rule: the employee's company → area MDU:")
		s = frappe.get_doc("Biometric Settings")
		s.update({"push_users": 1, "push_connector": e2e.CONNECTOR, "push_remove_on_exit": 1,
		          "push_area_from": "Company", "push_default_area_codes": "", "push_default_department_code": "1"})
		s.set("push_rules", [{"grohr_value": emp.company, "area_codes": "MDU", "department_code": "HR"}])
		s.save(ignore_permissions=True)
		frappe.db.commit()

		check("placement follows the rule", user_push.placement({"company": emp.company})[:2] == (["MDU"], "HR"))
		check("no rule and no default → refused with a reason", bool(user_push.placement({"company": "Nowhere Ltd"})[2]))

		_enrol(active, ID_NEW)
		_enrol(active, ID_DEVICE, device=fixtures.DEVICE)
		_enrol(active, ID_OTHER)
		_enrol(active, ID_SAME)
		check("a new global ID is queued", _status(ID_NEW) == "Queued", _status(ID_NEW))
		check("a per-machine ID is not", _status(ID_DEVICE) is None)
		check("another connector is given nothing", user_push.pending("Some Other Agent") == [])
		frappe.db.rollback()

		srv.add(ID_OTHER, "MURUGAN SOMEONE ELSE")
		srv.add(ID_SAME, (emp.employee_name or "").upper())

		print("Commissioning in dry run:")
		cfg_path = os.path.join(workdir, "config.json")
		with open(cfg_path) as fh:
			cfg = json.load(fh)
		cfg["easytime"] = {"url": srv.url, "username": "api", "password": "secret", "dry_run": True}
		with open(cfg_path, "w") as fh:
			json.dump(cfg, fh)
		_agent(workdir, "dry run")
		check("the new ID comes back Dry Run", _status(ID_NEW) == "Dry Run", _row(ID_NEW) and _row(ID_NEW).message)
		check("and Easy Time Pro was not written to", not srv.calls, srv.calls)
		check("someone else under the ID is a Conflict even in dry run", _status(ID_OTHER) == "Conflict")
		check("the conflict names who Easy Time Pro has", "MURUGAN" in (_row(ID_OTHER).message or ""), _row(ID_OTHER).message)
		same = _row(ID_SAME)
		check("the same person already there is requeued with overwrite", same.status == "Queued" and same.overwrite,
		      (same.status, same.message))

		print("Live:")
		cfg["easytime"]["dry_run"] = False
		with open(cfg_path, "w") as fh:
			json.dump(cfg, fh)
		check("Resend all Dry Run requeues them", user_push.requeue_status("Dry Run") >= 1)
		frappe.db.commit()
		_agent(workdir, "live")
		made = next((e for e in srv.employees if e["emp_code"] == ID_NEW), None)
		r = _row(ID_NEW)
		check("the new ID is Created in Easy Time Pro", r.status == "Done" and r.outcome == "Created", (r.status, r.outcome, r.message))
		check("with the employee's name", made and made["first_name"] == (emp.employee_name or "")[:25], made)
		check("in area MDU and department HR (as ids)", made and made["area"] == [2] and made["department"] == 7, made)
		check("the remote id is recorded", r.remote_id == str(made and made["id"]), r.remote_id)
		check("the payload sent is kept on the row", json.loads(r.payload or "{}").get("area_codes") == ["MDU"])
		other_name = next(e for e in srv.employees if e["emp_code"] == ID_OTHER)["first_name"]
		check("the conflicting user was not touched", other_name == "MURUGAN SOMEONE ELSE", other_name)

		same = _row(ID_SAME)
		check("…and Updated in place", same.status == "Done" and same.outcome == "Updated", (same.status, same.outcome))

		print("HR resolves the conflict:")
		user_push.overwrite(_row(ID_OTHER).name)
		frappe.db.commit()
		_agent(workdir, "overwrite")
		other = next(e for e in srv.employees if e["emp_code"] == ID_OTHER)
		check("Overwrite replaces the user in Easy Time Pro", _row(ID_OTHER).outcome == "Updated"
		      and other["first_name"] == (emp.employee_name or "")[:25], other)

		print("Employee leaves (hook called on an unsaved copy):")
		doc = frappe.get_doc("Employee", active)
		doc._doc_before_save = copy.deepcopy(doc)
		doc.status = "Left"
		user_push.on_employee_update(doc)
		frappe.db.commit()
		rem = _row(ID_NEW)
		check("each global ID is queued for removal", rem.action == "Remove from Machines" and rem.status == "Queued",
		      (rem.action, rem.status))
		check("the per-machine ID still is not", _status(ID_DEVICE) is None)
		_agent(workdir, "leaver")
		check("Easy Time Pro resigns them", _row(ID_NEW).outcome == "Resigned"
		      and any(x["employee"] == made["id"] and x["disableatt"] for x in srv.resigns), srv.resigns)
		check("the Employee record itself was never saved",
		      frappe.db.get_value("Employee", active, "status") == "Active")

		print("Safety:")
		check("an unknown or already-answered result is ignored",
		      user_push.receive([{"sync_id": "BUS-NOPE", "outcome": "Created"},
		                         {"sync_id": _row(ID_NEW).name, "outcome": "Created"}])["ignored"] == 2)
		check("the Employee form sees the latest row per ID",
		      {r.device_user_id for r in user_push.employee_status(active)["rows"]} >= {ID_NEW, ID_OTHER, ID_SAME})
	finally:
		srv.stop()
		import shutil

		shutil.rmtree(workdir, ignore_errors=True)
		s = frappe.get_doc("Biometric Settings")
		s.update(saved)
		s.set("push_rules", [{k: r[k] for k in ("grohr_value", "area_codes", "department_code")} for r in saved_rules])
		s.save(ignore_permissions=True)
		frappe.db.commit()
		_cleanup(active, started)
		e2e._teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_user_push: {len(failed)} check(s) failed")
	print("All good.")
