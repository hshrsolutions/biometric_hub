"""ADMS / iclock — the protocol push-mode ZKTeco readers speak.

These devices cannot be taught a URL shape. They connect outbound to a fixed set
of paths, send tab-separated text, and expect `OK` back. So this module speaks
their protocol rather than ours, and an additive nginx rule (see
`nginx/biometric-hub-iclock.conf`) maps `/iclock/*` onto it.

**On authentication.** The only identity a device presents is its serial number,
in the query string. That is the protocol, not a choice we made, and it is weak:
anyone who can reach the endpoint and knows a serial can post punches. Three
things follow, and all three are deliberate:

* a serial that is not registered is **rejected**, not quietly accepted — an
  unregistered reader is a fact somebody needs to see;
* a rejection returns a non-OK status, so the device **keeps** its records and
  retries rather than deleting them;
* the deployment notes tell you to restrict `/iclock/` by source IP at nginx,
  because the protocol cannot do it for you.
"""

import frappe
from frappe.utils import now_datetime

from biometric_hub.drivers.base import RawPunch
from biometric_hub.engine import ingest as engine

# What the device is told to do once it has connected. Realtime=1 asks it to
# push each punch as it happens rather than batching to a timetable.
DEVICE_OPTIONS = """GET OPTION FROM: {serial}
Stamp=9999
OpStamp=9999
ErrorDelay=30
Delay=10
TransTimes=00:00;14:05
TransInterval=1
TransFlag=1111000000
Realtime=1
Encrypt=0
"""


def _text(body):
	frappe.local.response["type"] = "download"
	frappe.local.response["filename"] = "response.txt"
	frappe.local.response["filecontent"] = body if isinstance(body, bytes) else body.encode("utf-8")
	frappe.local.response["content_type"] = "text/plain"
	frappe.local.response["display_content_as"] = "inline"
	return


def _device_for(serial):
	if not serial:
		return None
	return frappe.db.get_value(
		"Biometric Device", {"serial_no": serial}, ["name", "enabled"], as_dict=True
	)


def _reject_unregistered(serial):
	"""Record the sighting so it is visible in the desk, and refuse — so the
	device holds on to its punches instead of dropping them.

	The refusal goes back as JSON rather than plain text, because Frappe only
	honours `http_status_code` on that path, and the status is the part that
	matters: a device that sees 200 considers the exchange finished and may clear
	its memory. A device that sees 401 keeps its records and tries again.
	"""
	frappe.get_doc(
		{
			"doctype": "Biometric Sync Log",
			"started": now_datetime(),
			"finished": now_datetime(),
			"source": "Push",
			"status": "Failed",
			"received": 0,
			"message": f"An unregistered device tried to connect. Serial: {serial!r}. "
			f"Create a Biometric Device with this serial to accept its punches.",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.local.response["http_status_code"] = 401
	return "ERROR: device not registered"


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def cdata(SN=None, table=None, **kwargs):
	"""The device's main endpoint: handshake on GET, data on POST."""
	serial = SN or frappe.form_dict.get("SN")
	device = _device_for(serial)
	if not device:
		return _reject_unregistered(serial)
	if not device.enabled:
		# Registered but switched off. Accept and discard, so it stops asking.
		return _text("OK\n")

	# Frappe does not commit on a GET, and the handshake is a GET — without this
	# the device is never recorded as having been seen.
	frappe.db.set_value(
		"Biometric Device", device.name, {"last_contact": now_datetime(), "health_status": "Online"},
		update_modified=False,
	)
	frappe.db.commit()

	if frappe.request.method == "GET":
		return _text(DEVICE_OPTIONS.format(serial=serial))

	body = frappe.request.get_data(as_text=True) or ""
	table = (table or frappe.form_dict.get("table") or "").upper()

	if table and table != "ATTLOG":
		# Operation logs, user records and the rest. Acknowledged, not stored:
		# this app is about attendance.
		return _text("OK\n")

	punches = parse_attlog(serial, body)
	if not punches:
		return _text("OK: 0\n")

	result = engine.ingest(punches, source="Push")
	frappe.db.commit()
	return _text(f"OK: {result['received']}\n")


@frappe.whitelist(allow_guest=True, methods=["GET"])
def getrequest(SN=None, **kwargs):
	"""The device asking for commands. We have none — this app reads, it does not
	drive the reader."""
	serial = SN or frappe.form_dict.get("SN")
	device = _device_for(serial)
	if not device:
		return _reject_unregistered(serial)
	frappe.db.set_value(
		"Biometric Device", device.name, {"last_contact": now_datetime()}, update_modified=False
	)
	frappe.db.commit()
	return _text("OK\n")


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def devicecmd(SN=None, **kwargs):
	"""Command results coming back. Acknowledged."""
	return _text("OK\n")


def parse_attlog(serial, body):
	"""ATTLOG is tab-separated: PIN, timestamp, status, verify mode, work code.

	Field 3 is the punch direction on most firmware and the verification method
	on some — vendors disagree, and there is no reliable way to tell from the
	wire. It is passed through as `raw_log_type` and the engine interprets it;
	where a reader is known to be wrong, set **Fixed Log Type** on the device and
	the guess is bypassed entirely.
	"""
	punches = []
	for line in body.splitlines():
		line = line.strip()
		if not line:
			continue
		parts = line.split("\t")
		if len(parts) < 2:
			continue
		device_user_id = parts[0].strip()
		punch_time = parts[1].strip()
		if not device_user_id or not punch_time:
			continue
		punches.append(
			RawPunch(
				device_serial=serial,
				device_user_id=device_user_id,
				punch_time=punch_time,
				raw_log_type=parts[2].strip() if len(parts) > 2 else None,
				payload={"raw": line},
			)
		)
	return punches
