"""HTTP surface for devices and connector agents.

Thin by design: parse, authorise, delegate to `engine.ingest`, return. No
business logic lives here — see `biometric-hub-prd.md` §7.3 for why.

Every method requires the `Biometric Integration` role, which can create punches
and checkins and do nothing else.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from biometric_hub.engine import ingest as engine

MAX_BATCH = 1000
ALLOWED_ROLES = {"Biometric Integration", "Biometric Manager", "System Manager"}


def _require_integration():
	if not (ALLOWED_ROLES & set(frappe.get_roles())):
		frappe.throw(_("Not permitted."), frappe.PermissionError)


def _parse(payload):
	if isinstance(payload, str):
		payload = json.loads(payload)
	return payload


@frappe.whitelist()
def push(punches, connector=None):
	"""Submit a batch of punches.

	Idempotent: re-sending a punch is free and returns `Duplicate`, so an agent
	that crashes mid-batch can safely replay the whole thing.

	Each punch: `device_serial`, `device_user_id`, `punch_time`, and optionally
	`raw_log_type`, `latitude`, `longitude`, `payload`.
	"""
	_require_integration()
	punches = _parse(punches)

	if not isinstance(punches, list):
		frappe.throw(_("punches must be a list."))
	if len(punches) > MAX_BATCH:
		frappe.throw(_("Batch too large: {0} punches, maximum is {1}.").format(len(punches), MAX_BATCH))

	if connector and not frappe.db.exists("Biometric Connector", connector):
		frappe.throw(_("Unknown connector {0}.").format(connector))

	allowed = {"device_serial", "device_user_id", "punch_time", "raw_log_type", "latitude", "longitude", "payload"}
	cleaned = []
	for row in punches:
		if not isinstance(row, dict):
			frappe.throw(_("Each punch must be an object."))
		cleaned.append({k: v for k, v in row.items() if k in allowed})

	result = engine.ingest(cleaned, source="Connector" if connector else "Push", connector=connector)

	if connector:
		frappe.db.set_value(
			"Biometric Connector", connector, "last_checkin", now_datetime(), update_modified=False
		)

	# Drop the internal punch names; the agent has no use for them and they are
	# not its business.
	return {
		"received": result["received"],
		"mapped": result["mapped"],
		"duplicates": result["duplicates"],
		"unmapped": result["unmapped"],
		"ignored": result["ignored"],
		"errors": result["errors"],
		"pending": result["pending"],
		"sync_log": result["sync_log"],
		"results": [
			{"punch_key": r["punch_key"], "status": r["status"], "message": r["message"]}
			for r in result["results"]
		],
	}


@frappe.whitelist()
def heartbeat(connector, agent_version=None, devices=None):
	"""Liveness. Sent every cycle, punches or not — silence is the signal."""
	_require_integration()

	if not frappe.db.exists("Biometric Connector", connector):
		frappe.throw(_("Unknown connector {0}.").format(connector))

	now = now_datetime()
	update = {"last_checkin": now}
	if agent_version:
		update["agent_version"] = agent_version
	frappe.db.set_value("Biometric Connector", connector, update, update_modified=False)

	seen = []
	for serial in _parse(devices) or []:
		name = frappe.db.get_value("Biometric Device", {"serial_no": serial}, "name")
		if not name:
			continue
		frappe.db.set_value(
			"Biometric Device", name, {"last_contact": now, "health_status": "Online"}, update_modified=False
		)
		seen.append(name)

	# Profiles HR is waiting for (agent 0.3.0+ answers via `profiles`; older
	# agents ignore the key).
	from biometric_hub.engine.device_users import pending_requests
	from biometric_hub.engine.user_push import pending as pending_pushes

	# Employees to create/update/remove in Easy Time Pro (agent 0.4.0+; only the
	# push connector gets them).
	return {"acknowledged": now, "devices": seen, "profile_requests": pending_requests(),
	        "user_pushes": pending_pushes(connector)}


MAX_PROFILES = 50


@frappe.whitelist()
def profiles(connector, profiles):
	"""The agent's answer to `profile_requests`: name and photo per device user ID,
	read from the attendance software. Writes only Biometric Device User."""
	_require_integration()
	if not frappe.db.exists("Biometric Connector", connector):
		frappe.throw(_("Unknown connector {0}.").format(connector))
	profiles = _parse(profiles) or []
	if len(profiles) > MAX_PROFILES:
		frappe.throw(_("At most {0} profiles per call.").format(MAX_PROFILES))

	from biometric_hub.engine.device_users import receive_profiles

	return receive_profiles(profiles)


MAX_PUSH_RESULTS = 100


@frappe.whitelist()
def user_push_results(connector, results):
	"""The agent's answer to `user_pushes`: what Easy Time Pro said for each
	employee. Writes only Biometric User Sync."""
	_require_integration()
	if not frappe.db.exists("Biometric Connector", connector):
		frappe.throw(_("Unknown connector {0}.").format(connector))
	results = _parse(results) or []
	if not isinstance(results, list) or len(results) > MAX_PUSH_RESULTS:
		frappe.throw(_("results must be a list of at most {0}.").format(MAX_PUSH_RESULTS))

	from biometric_hub.engine.user_push import receive

	return receive([r for r in results if isinstance(r, dict)])


@frappe.whitelist()
def bootstrap(connector):
	"""What the agent should be polling, and how often.

	The device list lives on the server so adding a reader is a desk action, not
	a site visit.
	"""
	_require_integration()

	if not frappe.db.exists("Biometric Connector", connector):
		frappe.throw(_("Unknown connector {0}.").format(connector))
	if not cint(frappe.db.get_value("Biometric Connector", connector, "enabled")):
		frappe.throw(_("Connector {0} is disabled.").format(connector))

	devices = frappe.get_all(
		"Biometric Device",
		filters={"connector": connector, "enabled": 1},
		fields=[
			"name", "serial_no", "driver", "connectivity_mode", "ip_address", "port",
			"device_timezone", "ignore_punches_before",
			# The cursor lives here, on the server — never in a log file on the
			# customer's machine (PRD §7.2 item 1). A stale one is harmless:
			# the idempotency key absorbs any overlap.
			"last_punch_time",
		],
		limit_page_length=0,
	)

	settings = frappe.get_cached_doc("Biometric Settings")
	return {
		"connector": connector,
		"server_time": now_datetime(),
		"batch_size": 200,
		"ingestion_enabled": cint(settings.enable_ingestion),
		"devices": devices,
	}
