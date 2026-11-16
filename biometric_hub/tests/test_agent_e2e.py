"""Phase 6 acceptance: the real agent, as a separate process, over real HTTP.

	bench --site <site> execute biometric_hub.tests.test_agent_e2e.run

Not a mock. This starts `python -m grohr_agent --once` in its own process, which
authenticates with an API key, calls bootstrap, reads punches, posts them over
HTTP and heartbeats — and then we check the Employee Checkins landed.

Skipped cleanly if the agent repository is not on this machine.
"""

import json
import os
import subprocess
import tempfile

import frappe
from frappe.utils import add_to_date, now_datetime

from biometric_hub.tests import test_ingest as fixtures

AGENT_DIR = "/home/frappe/grohr-biometric-agent"
PYTHON = "/home/frappe/demo-bench/env/bin/python"
SITE_URL = "http://127.0.0.1:8001"
USER = "bhtest-agent@example.com"
CONNECTOR = "BHTEST Agent Connector"

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def _api_credentials():
	if frappe.db.exists("User", USER):
		frappe.delete_doc("User", USER, force=True, ignore_permissions=True)
	user = frappe.get_doc(
		{"doctype": "User", "email": USER, "first_name": "BHAgent",
		 "send_welcome_email": 0, "user_type": "System User"}
	)
	user.insert(ignore_permissions=True)
	user.add_roles("Biometric Integration")
	user.reload()
	user.api_key = frappe.generate_hash(length=15)
	user.api_secret = frappe.generate_hash(length=15)
	user.save(ignore_permissions=True)
	frappe.db.commit()
	return user.api_key, user.get_password("api_secret")


def _setup(active):
	api_key, api_secret = _api_credentials()

	if not frappe.db.exists("Biometric Connector", CONNECTOR):
		frappe.get_doc(
			{"doctype": "Biometric Connector", "agent_name": CONNECTOR, "enabled": 1,
			 "site_label": "Agent selftest", "api_user": USER}
		).insert(ignore_permissions=True)

	frappe.db.set_value(
		"Biometric Device", fixtures.DEVICE,
		{"connector": CONNECTOR, "connectivity_mode": "Via Connector"},
		update_modified=False,
	)
	frappe.db.commit()

	workdir = tempfile.mkdtemp(prefix="grohr-agent-e2e-")
	os.chmod(workdir, 0o777)
	config = {
		"site_url": SITE_URL,
		"host_header": frappe.local.site,
		"api_key": api_key,
		"api_secret": api_secret,
		"connector": CONNECTOR,
		"batch_size": 50,
		"poll_interval_seconds": 60,
		"verify_tls": False,
		"log_dir": os.path.join(workdir, "logs"),
		"spool_path": os.path.join(workdir, "spool.sqlite3"),
	}
	with open(os.path.join(workdir, "config.json"), "w") as handle:
		json.dump(config, handle)

	punches = [
		{"device_user_id": fixtures.ID_OK,
		 "punch_time": add_to_date(now_datetime(), minutes=-(60 + i)).strftime("%Y-%m-%d %H:%M:%S"),
		 "raw_log_type": "0" if i % 2 == 0 else "1"}
		for i in range(5)
	]
	# One from somebody nobody has enrolled: the server must keep it, not the agent.
	punches.append(
		{"device_user_id": fixtures.ID_UNKNOWN,
		 "punch_time": add_to_date(now_datetime(), minutes=-70).strftime("%Y-%m-%d %H:%M:%S"),
		 "raw_log_type": "0"}
	)
	with open(os.path.join(workdir, "punches.json"), "w") as handle:
		json.dump(punches, handle)

	return workdir


def _run_agent(workdir):
	env = {
		**os.environ,
		"GROHR_AGENT_FAKE_PUNCHES": os.path.join(workdir, "punches.json"),
		"PYTHONPATH": AGENT_DIR,
	}
	return subprocess.run(
		[PYTHON, "-m", "grohr_agent", "--config", os.path.join(workdir, "config.json"), "--once", "--verbose"],
		cwd=AGENT_DIR, env=env, capture_output=True, text=True, timeout=180,
	)


def _teardown():
	fixtures.teardown()
	for name in frappe.get_all("Biometric Connector", filters={"agent_name": CONNECTOR}, pluck="name"):
		frappe.delete_doc("Biometric Connector", name, force=True, ignore_permissions=True)
	if frappe.db.exists("User", USER):
		frappe.delete_doc("User", USER, force=True, ignore_permissions=True)
	frappe.db.commit()


def run():
	print("Biometric Hub — the agent, end to end over HTTP (Phase 6)\n")
	if not os.path.isdir(AGENT_DIR):
		print(f"  SKIPPED — no agent repository at {AGENT_DIR}")
		return

	active, left = fixtures._employees()
	_teardown()
	fixtures.setup(active, left)
	workdir = _setup(active)
	try:
		print("First run:")
		result = _run_agent(workdir)
		check("the agent process exits cleanly", result.returncode == 0,
		      (result.stdout or "")[-600:] + (result.stderr or "")[-600:])

		frappe.db.rollback()  # see what the agent's own transaction committed
		mapped = frappe.db.count("Biometric Punch", {"device": fixtures.DEVICE, "status": "Mapped"})
		unmapped = frappe.db.count("Biometric Punch", {"device": fixtures.DEVICE, "status": "Unmapped"})
		check("five enrolled punches became checkins", mapped == 5, mapped)
		check("the unenrolled punch is held by the server, not the agent", unmapped == 1, unmapped)
		check("real Employee Checkins exist",
		      frappe.db.count("Employee Checkin", {"device_id": fixtures.DEVICE}) == 5)

		connector = frappe.db.get_value(
			"Biometric Connector", CONNECTOR, ["last_checkin", "agent_version"], as_dict=True
		)
		check("the agent heartbeat was recorded", bool(connector.last_checkin))
		check("and it reported its version", bool(connector.agent_version), connector.agent_version)

		log_type = frappe.db.get_value(
			"Employee Checkin", {"device_id": fixtures.DEVICE, "log_type": "IN"}, "name"
		)
		check("ZK punch codes were interpreted into log types", bool(log_type))

		print("Second run — the crash-safe replay:")
		before = frappe.db.count("Employee Checkin", {"device_id": fixtures.DEVICE})
		result = _run_agent(workdir)
		frappe.db.rollback()
		after = frappe.db.count("Employee Checkin", {"device_id": fixtures.DEVICE})
		check("the agent exits cleanly again", result.returncode == 0, (result.stderr or "")[-400:])
		check("re-sending everything creates no duplicate checkin", before == after, f"{before} -> {after}")

		print("Offline behaviour:")
		bad = os.path.join(workdir, "offline.json")
		with open(os.path.join(workdir, "config.json")) as handle:
			config = json.load(handle)
		config["site_url"] = "http://127.0.0.1:9"  # nothing listening
		with open(bad, "w") as handle:
			json.dump(config, handle)
		offline = subprocess.run(
			[PYTHON, "-m", "grohr_agent", "--config", bad, "--once"],
			cwd=AGENT_DIR,
			env={**os.environ, "GROHR_AGENT_FAKE_PUNCHES": os.path.join(workdir, "punches.json"),
			     "PYTHONPATH": AGENT_DIR},
			capture_output=True, text=True, timeout=180,
		)
		check("an unreachable server is a failed cycle, not a crash", offline.returncode == 1,
		      (offline.stderr or "")[-300:])
		agent_log = os.path.join(workdir, "logs", "agent.log")
		text = open(agent_log).read() if os.path.exists(agent_log) else ""
		check("and it says so in the log", "Server unreachable" in text, text[-300:])
	finally:
		_teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_agent_e2e: {len(failed)} check(s) failed")
	print("All good.")
