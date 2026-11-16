"""The ingestion engine.

**This is the only file in the app that may write to an HRMS document**, and it
may write exactly two things:

  1. create an `Employee Checkin`
  2. advance `Shift Type.last_sync_of_checkin`

The second is the field HRMS exposes for sync integrations; doing it here rather
than in the agent is what lets the credential on a customer machine drop
`Shift Type: write` entirely.

Pipeline, per `biometric-hub-prd.md` §6.1:

	stage -> resolve device -> normalise time -> map employee
	      -> resolve log type -> write checkin -> advance shift sync -> log

Two invariants hold at every step:

  * **Nothing is dropped.** The `Biometric Punch` row is inserted before any
    interpretation is attempted, so a punch that cannot be mapped is visible
    rather than lost.
  * **One bad punch fails one punch.** Every punch runs inside its own
    savepoint; a failure rolls back that punch and the batch carries on.
"""

import hashlib
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe.utils import cint, get_datetime, get_system_timezone, now_datetime

from biometric_hub.drivers.base import RawPunch

# ZK-family punch codes, the same defaults upstream BAST uses.
PUNCH_CODES_IN = {"0", "4"}
PUNCH_CODES_OUT = {"1", "5"}

TERMINAL_STATUSES = {"Mapped", "Unmapped", "Blocked", "Duplicate", "Ignored"}


# --------------------------------------------------------------------------- keys


def punch_key(device_serial: str, device_user_id: str, punch_time) -> str:
	"""The idempotency key. Stable across replays, and derived only from what the
	device actually reported — never from anything we compute."""
	raw = f"{(device_serial or '').strip()}|{(device_user_id or '').strip()}|{get_datetime(punch_time)}"
	return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------- helpers


def _settings():
	s = frappe.get_cached_doc("Biometric Settings")
	return frappe._dict(
		enable_ingestion=cint(s.enable_ingestion),
		create_employee_checkin=cint(s.create_employee_checkin),
		default_log_type_policy=s.default_log_type_policy or "Trust Device",
		max_future_skew_minutes=cint(s.max_future_skew_minutes) or 5,
	)


def _normalise_time(punch_time, device_timezone):
	"""Device-local -> (site-local naive, UTC naive).

	An unknown timezone name is an error rather than a silent fallback: a whole
	device worth of punches landing an hour out is worse than a punch that stops
	and says why.
	"""
	dt = get_datetime(punch_time)
	site_tz = get_system_timezone()
	if not device_timezone or device_timezone == site_tz:
		aware = dt.replace(tzinfo=ZoneInfo(site_tz))
		return dt, aware.astimezone(timezone.utc).replace(tzinfo=None)

	try:
		aware = dt.replace(tzinfo=ZoneInfo(device_timezone))
	except (ZoneInfoNotFoundError, ValueError, KeyError):
		raise ValueError(f"Unknown device timezone {device_timezone!r}")

	site_local = aware.astimezone(ZoneInfo(site_tz)).replace(tzinfo=None)
	return site_local, aware.astimezone(timezone.utc).replace(tzinfo=None)


def _new_context():
	"""Per-batch caches. A batch touches the same handful of devices and device
	user IDs over and over; there is no reason to ask the database each time."""
	return frappe._dict(devices={}, enrolments={}, access={}, geolocation=None, touched={})


def _geolocation_required(ctx):
	if ctx.geolocation is None:
		ctx.geolocation = bool(frappe.db.get_single_value("HR Settings", "allow_geolocation_tracking"))
	return ctx.geolocation


def _get_device(ctx, serial):
	if serial not in ctx.devices:
		ctx.devices[serial] = frappe.db.get_value(
			"Biometric Device",
			{"serial_no": serial},
			[
				"name", "enabled", "device_timezone", "fixed_log_type",
				"skip_auto_attendance", "ignore_punches_before", "latitude", "longitude",
			],
			as_dict=True,
		)
	return ctx.devices[serial]


def _resolve_employee(ctx, device_user_id, device_name, on_date):
	"""Device-specific enrolment first, then the global one, then the legacy
	`Employee.attendance_device_id`. Returns an Employee name or None."""
	if device_user_id not in ctx.enrolments:
		ctx.enrolments[device_user_id] = frappe.get_all(
			"Biometric Enrolment",
			filters={"device_user_id": device_user_id, "enabled": 1},
			fields=["name", "employee", "device", "valid_from", "valid_upto"],
			limit_page_length=0,
		)
	rows = ctx.enrolments[device_user_id]

	def valid(row):
		if row.valid_from and get_datetime(row.valid_from).date() > on_date:
			return False
		if row.valid_upto and get_datetime(row.valid_upto).date() < on_date:
			return False
		return True

	specific = [r for r in rows if r.device and r.device == device_name and valid(r)]
	if specific:
		return specific[0].employee

	glob = [r for r in rows if not r.device and valid(r)]
	if glob:
		return glob[0].employee

	# Legacy path: a site that has not been through the enrolment bootstrap yet,
	# or an ID added straight onto the Employee since.
	return frappe.db.get_value("Employee", {"attendance_device_id": device_user_id}, "name")


def _log_type_from_raw(raw_log_type):
	if raw_log_type is None or raw_log_type == "":
		return None
	value = str(raw_log_type).strip().upper()
	if value in ("IN", "OUT"):
		return value
	if value in PUNCH_CODES_IN:
		return "IN"
	if value in PUNCH_CODES_OUT:
		return "OUT"
	return None


def _resolve_log_type(raw_log_type, device, employee, site_local, policy):
	"""`device.fixed_log_type` wins, then whatever the device said, then policy."""
	if device.get("fixed_log_type"):
		return device.fixed_log_type

	from_device = _log_type_from_raw(raw_log_type)
	if from_device:
		return from_device

	if policy == "Alternate" and employee:
		previous = frappe.get_all(
			"Employee Checkin",
			filters={
				"employee": employee,
				"time": ["<", site_local],
				"log_type": ["in", ["IN", "OUT"]],
			},
			fields=["log_type"],
			order_by="time desc",
			limit=1,
		)
		if not previous:
			return "IN"
		return "OUT" if previous[0].log_type == "IN" else "IN"

	# "Trust Device" and "First/Last of Day" both mean: say nothing, and let HRMS
	# decide from the first and last punch of the day.
	return None


# ------------------------------------------------------------------------ ingest


def ingest(raw_punches, source="Connector", connector=None, write_sync_log=True):
	"""Ingest a batch. Returns counts plus a per-punch result, in order.

	`raw_punches` may be `RawPunch` objects or plain dicts with the same keys.
	"""
	started = now_datetime()
	settings = _settings()
	results = []

	if not settings.enable_ingestion:
		# The master kill switch. Refuse the batch whole rather than half-ingest it.
		return {
			"received": len(raw_punches),
			"mapped": 0,
			"duplicates": 0,
			"unmapped": 0,
			"errors": 0,
			"blocked": 0,
			"ignored": 0,
			"pending": 0,
			"results": [],
			"message": "Ingestion is disabled in Biometric Settings.",
			"sync_log": None,
		}

	ctx = _new_context()
	devices_touched = set()
	for raw in raw_punches:
		if isinstance(raw, dict):
			raw = RawPunch(**raw)
		result = _ingest_one(raw, source, connector, settings, ctx)
		results.append(result)
		if result.get("device"):
			devices_touched.add(result["device"])

	counts = {
		"received": len(results),
		"mapped": sum(1 for r in results if r["status"] == "Mapped"),
		"duplicates": sum(1 for r in results if r["status"] == "Duplicate"),
		"unmapped": sum(1 for r in results if r["status"] == "Unmapped"),
		"blocked": sum(1 for r in results if r["status"] == "Blocked"),
		"ignored": sum(1 for r in results if r["status"] == "Ignored"),
		"errors": sum(1 for r in results if r["status"] == "Error"),
		"pending": sum(1 for r in results if r["status"] == "Pending"),
	}

	for device_name, last_punch in ctx.touched.items():
		frappe.db.set_value(
			"Biometric Device", device_name,
			{"last_punch_time": last_punch, "last_contact": now_datetime(), "health_status": "Online"},
			update_modified=False,
		)

	try:
		advance_shift_sync(devices_touched)
	except Exception:
		# Never let a shift-watermark problem cost us an ingested batch.
		frappe.log_error(title="Biometric Hub: advance_shift_sync failed")

	sync_log = None
	if write_sync_log:
		sync_log = _write_sync_log(counts, source, connector, started)

	return {**counts, "results": results, "sync_log": sync_log}


def _ingest_one(raw: RawPunch, source, connector, settings, ctx):
	# Keying comes first and is guarded on its own: without a parseable timestamp
	# there is no idempotency key, so the punch cannot be staged at all. It is
	# still reported and logged — and it must never take the batch down with it.
	try:
		key = punch_key(raw.device_serial, raw.device_user_id, raw.punch_time)
	except Exception as e:
		return _unkeyable(raw, e)

	savepoint = "bh_ingest"
	frappe.db.savepoint(savepoint)
	try:
		existing = frappe.db.get_value(
			"Biometric Punch", {"punch_key": key}, ["name", "status", "employee_checkin", "attempts"], as_dict=True
		)
		if existing:
			# A replay. Record that we saw it again, and do not create a second row —
			# the unique index is the point.
			frappe.db.set_value(
				"Biometric Punch",
				existing.name,
				{"attempts": (existing.attempts or 0) + 1, "last_attempt": now_datetime()},
				update_modified=False,
			)
			return {
				"punch_key": key,
				"status": "Duplicate",
				"punch": existing.name,
				"employee_checkin": existing.employee_checkin,
				"device": None,
				"message": f"Already ingested (currently {existing.status}).",
			}

		punch = _stage(raw, key, source, connector)
		return _process(punch, settings, ctx)
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		# The staged row was rolled back with everything else, so re-record the
		# punch on its own. A punch is never lost to an exception.
		return _record_failure(raw, key, source, connector, e)


def _unkeyable(raw: RawPunch, exception):
	"""A punch we cannot even identify. Storing it against a made-up timestamp
	would be worse than not storing it, so it is reported and logged instead —
	loudly, never silently."""
	message = f"Malformed punch, cannot be keyed: {type(exception).__name__}: {exception}"[:500]
	frappe.log_error(
		title="Biometric Hub: unkeyable punch",
		message=f"{message}\n\nserial={raw.device_serial!r} user={raw.device_user_id!r} "
		f"time={raw.punch_time!r}",
	)
	return {
		"punch_key": None,
		"status": "Error",
		"punch": None,
		"employee_checkin": None,
		"device": None,
		"message": message,
	}


def _stage(raw: RawPunch, key, source, connector):
	"""Persist before interpreting. Everything after this point can fail safely."""
	punch = frappe.get_doc(
		{
			"doctype": "Biometric Punch",
			"punch_key": key,
			"status": "Pending",
			"device_serial": (raw.device_serial or "").strip(),
			"device_user_id": (raw.device_user_id or "").strip(),
			"punch_time": get_datetime(raw.punch_time),
			"raw_log_type": raw.raw_log_type,
			"source": source,
			"connector": connector,
			"latitude": raw.latitude,
			"longitude": raw.longitude,
			"attempts": 1,
			"first_seen": now_datetime(),
			"last_attempt": now_datetime(),
			"raw_payload": json.dumps(raw.payload, default=str, indent=2) if raw.payload else None,
		}
	)
	punch.insert(ignore_permissions=True)
	return punch


def _finish(punch, status, message=None, **fields):
	punch.status = status
	punch.error_message = message if message else None
	for k, v in fields.items():
		setattr(punch, k, v)

	# db.set_value rather than doc.save(): the row was inserted moments ago and its
	# only controller logic is the punch_key, which is already set. A full save
	# here is a third document write on the hot path for no benefit.
	frappe.db.set_value(
		"Biometric Punch",
		punch.name,
		{
			"status": punch.status,
			"error_message": punch.error_message,
			"device": punch.device,
			"employee": punch.employee,
			"employee_checkin": punch.employee_checkin,
			"resolved_log_type": punch.resolved_log_type,
			"punch_time_utc": punch.punch_time_utc,
			"last_attempt": now_datetime(),
		},
		update_modified=False,
	)
	return {
		"punch_key": punch.punch_key,
		"status": status,
		"punch": punch.name,
		"employee_checkin": punch.employee_checkin,
		"device": punch.device,
		"message": message,
	}


def _process(punch, settings, ctx):
	# --- 2. resolve device --------------------------------------------------
	device = _get_device(ctx, punch.device_serial)
	if not device:
		# Not an error to swallow: an unregistered reader on the network is a fact
		# somebody needs to see.
		return _finish(punch, "Error", f"Unrecognised device serial {punch.device_serial!r}.")
	if not cint(device.enabled):
		return _finish(punch, "Ignored", f"Device {device.name} is disabled.", device=device.name)

	punch.device = device.name

	# --- 3. normalise time --------------------------------------------------
	try:
		site_local, utc = _normalise_time(punch.punch_time, device.device_timezone)
	except ValueError as e:
		return _finish(punch, "Error", str(e), device=device.name)

	punch.punch_time_utc = utc

	if device.ignore_punches_before and site_local < get_datetime(device.ignore_punches_before):
		return _finish(
			punch, "Ignored",
			f"Before the device cut-off of {device.ignore_punches_before}.",
			device=device.name,
		)

	skew_limit = frappe.utils.add_to_date(now_datetime(), minutes=settings.max_future_skew_minutes)
	if site_local > skew_limit:
		return _finish(
			punch, "Error",
			f"Punch time {site_local} is more than {settings.max_future_skew_minutes} "
			f"minute(s) in the future — check the device clock.",
			device=device.name,
		)

	# --- 4. map employee ----------------------------------------------------
	employee = _resolve_employee(ctx, punch.device_user_id, device.name, site_local.date())
	if not employee:
		# Terminal. It goes in the work queue and is never retried on the wire.
		return _finish(
			punch, "Unmapped",
			f"No enrolment for device user ID {punch.device_user_id!r}.",
			device=device.name,
		)
	punch.employee = employee

	# --- 4b. device access ----------------------------------------------------
	# HR may restrict an employee to named devices (Biometric Employee Access).
	# A punch elsewhere is held, not dropped: adding the device replays it.
	from biometric_hub.engine.access import check_device

	allowed, why = check_device(employee, device.name, ctx.access)
	if not allowed:
		return _finish(punch, "Blocked", why, device=device.name)

	# --- 5. resolve log type ------------------------------------------------
	log_type = _resolve_log_type(
		punch.raw_log_type, device, employee, site_local, settings.default_log_type_policy
	)
	punch.resolved_log_type = log_type

	# --- 6. write the checkin -----------------------------------------------
	if not settings.create_employee_checkin:
		# Parallel-run mode: stage everything, write nothing. Reconcile picks
		# these up when the switch is turned back on.
		return _finish(
			punch, "Pending",
			"Staged only — 'Create Employee Checkin' is off in Biometric Settings.",
			device=device.name,
		)

	latitude = punch.latitude if punch.latitude else device.latitude
	longitude = punch.longitude if punch.longitude else device.longitude

	# HRMS refuses a checkin with no coordinates once "Allow Geolocation Tracking"
	# is on. Say so in terms of the thing to fix, rather than letting a bare
	# ValidationError land on every punch this reader ever sends.
	if _geolocation_required(ctx) and not (latitude or longitude):
		return _finish(
			punch, "Error",
			f"HR Settings has 'Allow Geolocation Tracking' enabled, but neither this punch "
			f"nor device {device.name} carries coordinates. Set Latitude and Longitude on "
			f"the device.",
			device=device.name,
		)

	checkin = frappe.get_doc(
		{
			"doctype": "Employee Checkin",
			"employee": employee,
			"time": site_local,
			"device_id": device.name,
			"log_type": log_type,
			"skip_auto_attendance": cint(device.skip_auto_attendance),
			"latitude": latitude,
			"longitude": longitude,
		}
	)
	# Marks the checkin as ours, so the self-service access hook leaves it alone.
	checkin.flags.biometric_hub = True
	checkin.insert(ignore_permissions=True)

	# Recorded once for the whole batch, after the loop.
	previous = ctx.touched.get(device.name)
	if not previous or site_local > previous:
		ctx.touched[device.name] = site_local

	return _finish(punch, "Mapped", None, device=device.name, employee_checkin=checkin.name)


def _record_failure(raw: RawPunch, key, source, connector, exception):
	"""Re-stage a punch whose processing blew up, after its savepoint was rolled
	back. Best effort by definition — but a punch is never lost silently."""
	message = f"{type(exception).__name__}: {exception}"[:500]
	try:
		punch = _stage(raw, key, source, connector)
		punch.status = "Error"
		punch.error_message = message
		punch.save(ignore_permissions=True)
		name = punch.name
	except Exception:
		frappe.log_error(title=f"Biometric Hub: could not record failed punch {key}")
		name = None
	return {
		"punch_key": key,
		"status": "Error",
		"punch": name,
		"employee_checkin": None,
		"device": None,
		"message": message,
	}


# ------------------------------------------------------------------- shift sync


def advance_shift_sync(device_names):
	"""Advance `Shift Type.last_sync_of_checkin` for the shifts these devices feed.

	A shift only moves once **every** enabled device feeding it has reported —
	otherwise auto-attendance would run over a window one of its readers has not
	filled in yet. This is the one HRMS field we update, and doing it here is why
	the agent needs no write access to an HR master.
	"""
	if not device_names:
		return

	shifts = set()
	for device in device_names:
		shifts.update(
			r.shift_type
			for r in frappe.get_all(
				"Biometric Device Shift", filters={"parent": device}, fields=["shift_type"]
			)
		)

	for shift in shifts:
		feeding = frappe.get_all(
			"Biometric Device Shift", filters={"shift_type": shift}, fields=["parent"], limit_page_length=0
		)
		watermarks = []
		complete = True
		for row in feeding:
			device = frappe.db.get_value(
				"Biometric Device", row.parent, ["enabled", "last_punch_time"], as_dict=True
			)
			if not device or not cint(device.enabled):
				continue
			if not device.last_punch_time:
				complete = False
				break
			watermarks.append(get_datetime(device.last_punch_time))

		if not complete or not watermarks:
			continue

		watermark = min(watermarks)
		current = frappe.db.get_value("Shift Type", shift, "last_sync_of_checkin")
		if current and get_datetime(current) >= watermark:
			continue
		frappe.db.set_value("Shift Type", shift, "last_sync_of_checkin", watermark, update_modified=False)


# --------------------------------------------------------------------- sync log


def _write_sync_log(counts, source, connector, started):
	doc = frappe.get_doc(
		{
			"doctype": "Biometric Sync Log",
			"started": started,
			"finished": now_datetime(),
			"source": source,
			"connector": connector,
			"status": "Failed" if counts["errors"] and not counts["mapped"]
			else ("Partial" if counts["errors"] or counts["unmapped"] or counts.get("blocked") else "Success"),
			"received": counts["received"],
			"mapped": counts["mapped"],
			"duplicates": counts["duplicates"],
			"unmapped": counts["unmapped"],
			"errors": counts["errors"],
			"blocked": counts.get("blocked", 0),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


# ---------------------------------------------------------------------- replay


def reprocess(punch_name):
	"""Re-run a punch that is already staged. Used when the reason it parked has
	been fixed — almost always a missing enrolment that now exists."""
	settings = _settings()
	savepoint = "bh_reprocess"
	frappe.db.savepoint(savepoint)
	try:
		# Lock the row: two replays of the same punch (a background job and a
		# button press, or HR saving twice) must not both write a checkin.
		current = frappe.db.get_value(
			"Biometric Punch", punch_name, ["status", "employee_checkin", "device"], as_dict=True, for_update=True
		)
		if current and current.status in ("Mapped", "Duplicate"):
			return {"punch_key": None, "status": current.status, "punch": punch_name,
			        "employee_checkin": current.employee_checkin, "device": current.device,
			        "message": "Already resolved."}
		punch = frappe.get_doc("Biometric Punch", punch_name)
		return _process(punch, settings, _new_context())
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		message = f"{type(e).__name__}: {e}"[:500]
		frappe.db.set_value(
			"Biometric Punch", punch_name,
			{"status": "Error", "error_message": message, "last_attempt": now_datetime()},
			update_modified=False,
		)
		return {"punch_key": None, "status": "Error", "punch": punch_name,
		        "employee_checkin": None, "device": None, "message": message}


def replay_unmapped(device_user_id, device=None):
	"""Every punch parked behind one missing mapping, run again.

	This is what turns a mapping gap from a permanent hole in the attendance
	record into a five-second fix.
	"""
	filters = {"status": "Unmapped", "device_user_id": device_user_id}
	if device:
		filters["device"] = device
	names = frappe.get_all("Biometric Punch", filters=filters, pluck="name", limit_page_length=0)
	results = [reprocess(name) for name in names]
	return {
		"replayed": len(results),
		"mapped": sum(1 for r in results if r["status"] == "Mapped"),
		"still_unmapped": sum(1 for r in results if r["status"] == "Unmapped"),
		"errors": sum(1 for r in results if r["status"] == "Error"),
	}


def replay_blocked(employee, device=None):
	"""Every punch held back by an employee's device access rules, run again.

	Called after HR changes the rules. Punches the new rules allow become
	checkins; the rest stay Blocked with an up-to-date reason.
	"""
	filters = {"status": "Blocked", "employee": employee}
	if device:
		filters["device"] = device
	names = frappe.get_all("Biometric Punch", filters=filters, pluck="name", limit_page_length=0)
	results = [reprocess(name) for name in names]
	return {
		"replayed": len(results),
		"mapped": sum(1 for r in results if r["status"] == "Mapped"),
		"still_blocked": sum(1 for r in results if r["status"] == "Blocked"),
		"errors": sum(1 for r in results if r["status"] == "Error"),
	}
