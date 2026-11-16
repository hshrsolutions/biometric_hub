"""The agent reading a real MySQL/MariaDB attendance table, end to end.

	bench --site <site> execute biometric_hub.tests.test_agent_sql_e2e.run

Creates a throwaway `iclock_transaction`-shaped table in this site's own
database (never another one), runs the real agent as a separate process with a
`sources` entry pointing at it, and checks the punches land as Employee
Checkins over HTTP. Then it drops the table and deletes the agent's working
directory, which held the database password.

Dev/test sites only, like the rest of the selftest. Skipped cleanly if the agent
repository is not on this machine.
"""

import json
import os
import shutil
import subprocess

import frappe
from frappe.utils import add_to_date, now_datetime

from biometric_hub.tests import test_agent_e2e as e2e
from biometric_hub.tests import test_ingest as fixtures

TABLE = "_bh_agent_src_iclock"

_results = []


def check(label, condition, detail=""):
	_results.append((label, bool(condition), detail))
	print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  — ' + str(detail) if detail and not condition else ''}")
	return bool(condition)


def _make_table(rows):
	frappe.db.sql(f"DROP TABLE IF EXISTS `{TABLE}`")
	frappe.db.sql(
		f"""CREATE TABLE `{TABLE}` (
			id INT AUTO_INCREMENT PRIMARY KEY,
			emp_code VARCHAR(40), punch_time DATETIME,
			terminal_sn VARCHAR(60), punch_state VARCHAR(4))"""
	)
	for row in rows:
		frappe.db.sql(
			f"INSERT INTO `{TABLE}` (emp_code, punch_time, terminal_sn, punch_state) VALUES (%s, %s, %s, %s)",
			row,
		)
	frappe.db.commit()


def _config(workdir):
	path = os.path.join(workdir, "config.json")
	with open(path) as handle:
		config = json.load(handle)
	config["sources"] = {
		fixtures.SERIAL: {
			"engine": "mysql",
			"host": frappe.conf.db_host or "127.0.0.1",
			"port": int(frappe.conf.db_port or 3306),
			"user": frappe.conf.db_name,
			"password": frappe.conf.db_password,
			"database": frappe.conf.db_name,
			"table": TABLE,
			"max_rows": 3,  # forces paging over the six rows
		}
	}
	with open(path, "w") as handle:
		json.dump(config, handle)
	os.chmod(path, 0o600)
	return path


def run():
	print("Biometric Hub — the agent reading a MySQL attendance table, end to end\n")
	if not os.path.isdir(e2e.AGENT_DIR):
		print(f"  SKIPPED — no agent repository at {e2e.AGENT_DIR}")
		return

	active, left = fixtures._employees()
	e2e._teardown()
	fixtures.setup(active, left)
	workdir = e2e._setup(active)
	frappe.db.set_value("Biometric Device", fixtures.DEVICE, "driver", "SQL Database", update_modified=False)
	frappe.db.commit()

	stamp = lambda minutes: add_to_date(now_datetime(), minutes=-minutes).strftime("%Y-%m-%d %H:%M:%S")
	rows = [(fixtures.ID_OK, stamp(90 + i), fixtures.SERIAL, "0" if i % 2 == 0 else "1") for i in range(5)]
	rows.append((fixtures.ID_UNKNOWN, stamp(95), fixtures.SERIAL, "0"))
	# Another reader's punch in the same table: must not be claimed by this device.
	rows.append((fixtures.ID_OK, stamp(80), "SOME-OTHER-READER", "0"))
	try:
		_make_table(rows)
		config_path = _config(workdir)
		env = {**os.environ, "PYTHONPATH": e2e.AGENT_DIR}
		cmd = [e2e.PYTHON, "-m", "grohr_agent", "--config", config_path]

		print("Probe:")
		probe = subprocess.run(cmd + ["--probe"], cwd=e2e.AGENT_DIR, env=env, capture_output=True, text=True, timeout=120)
		check("--probe connects and passes", probe.returncode == 0, probe.stdout[-600:] + probe.stderr[-300:])
		check("it lists the readers found in the table", "SOME-OTHER-READER" in probe.stdout)

		print("First run:")
		result = subprocess.run(cmd + ["--once", "--verbose"], cwd=e2e.AGENT_DIR, env=env,
		                        capture_output=True, text=True, timeout=180)
		check("the agent exits cleanly", result.returncode == 0, (result.stdout or "")[-600:] + (result.stderr or "")[-600:])
		frappe.db.rollback()
		mapped = frappe.db.count("Biometric Punch", {"device": fixtures.DEVICE, "status": "Mapped"})
		unmapped = frappe.db.count("Biometric Punch", {"device": fixtures.DEVICE, "status": "Unmapped"})
		check("five enrolled punches became checkins", mapped == 5, mapped)
		check("the unenrolled punch is held by the server", unmapped == 1, unmapped)
		check("the other reader's row was not read for this device",
		      not frappe.db.exists("Biometric Punch", {"device_serial": "SOME-OTHER-READER"}))
		check("real Employee Checkins exist, with device and log type",
		      frappe.db.count("Employee Checkin", {"device_id": fixtures.DEVICE, "log_type": ["in", ["IN", "OUT"]]}) == 5)

		print("Second run — the look-back re-reads, the ledger stops the re-send:")
		before = frappe.db.count("Employee Checkin", {"device_id": fixtures.DEVICE})
		again = subprocess.run(cmd + ["--once", "--verbose"], cwd=e2e.AGENT_DIR, env=env,
		                       capture_output=True, text=True, timeout=180)
		frappe.db.rollback()
		after = frappe.db.count("Employee Checkin", {"device_id": fixtures.DEVICE})
		check("the agent exits cleanly again", again.returncode == 0, (again.stderr or "")[-400:])
		check("no duplicate checkin", before == after, f"{before} -> {after}")
		text = open(os.path.join(workdir, "logs", "agent.log")).read()
		check("nothing was re-sent", "0 new" in text, text[-400:])

		print("A late upload inside the look-back window:")
		frappe.db.sql(
			f"INSERT INTO `{TABLE}` (emp_code, punch_time, terminal_sn, punch_state) VALUES (%s, %s, %s, %s)",
			(fixtures.ID_OK, stamp(200), fixtures.SERIAL, "0"),
		)
		frappe.db.commit()
		late = subprocess.run(cmd + ["--once"], cwd=e2e.AGENT_DIR, env=env, capture_output=True, text=True, timeout=180)
		frappe.db.rollback()
		check("an older punch that arrived late is still picked up",
		      late.returncode == 0 and frappe.db.count("Employee Checkin", {"device_id": fixtures.DEVICE}) == after + 1,
		      frappe.db.count("Employee Checkin", {"device_id": fixtures.DEVICE}))

		print("The vendor database was not written to:")
		check("the source table is unchanged apart from our own inserts",
		      frappe.db.sql(f"SELECT COUNT(*) FROM `{TABLE}`")[0][0] == len(rows) + 1)
	finally:
		frappe.db.sql(f"DROP TABLE IF EXISTS `{TABLE}`")
		shutil.rmtree(workdir, ignore_errors=True)  # it held the database password
		e2e._teardown()

	failed = [r for r in _results if not r[1]]
	print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
	if failed:
		print("FAILED:")
		for label, _, detail in failed:
			print(f"  - {label}{'  — ' + str(detail) if detail else ''}")
		raise Exception(f"test_agent_sql_e2e: {len(failed)} check(s) failed")
	print("All good.")
