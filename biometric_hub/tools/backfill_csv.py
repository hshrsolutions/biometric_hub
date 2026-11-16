"""One-off backfill / audit from a punch export.

Compares every punch in the attendance software (a CSV made on the customer PC by
the agent's `tools/export_punches.py`) with what GroHR already holds, and — only
when asked — imports the punches GroHR is missing through the normal ingest
engine. It is the tool for a first cutover, or for closing a gap left by an old
feed.

Each exported punch lands in exactly one bucket:

  unknown_device   reader serial is not a Biometric Device here (nothing is done)
  staged           already a Biometric Punch (any status) — the agent has it
  in_grohr         an Employee Checkin already exists for that employee at that
                   time (exact, or within --near seconds — the old feed's copy)
  to_import        employee known, no checkin -> becomes a checkin on --apply
  to_import_unmapped  no employee for that device ID -> staged Unmapped on --apply;
                   it turns into a checkin the moment HR sets the device ID

And in the other direction, `only_in_grohr` counts checkins inside the window that
match no exported punch (mobile/manual check-ins, or punches the software lost).

Report (read-only, writes an .xlsx and nothing else):

    cd <bench>/sites
    ../env/bin/python ../apps/biometric_hub/biometric_hub/tools/backfill_csv.py \\
        --site SITE --csv punches.csv [--people people.csv] --out report.xlsx

Import (writes, production needs sign-off): add `--apply`. It never imports a
bucket other than the two `to_import` ones, and it puts each device's
`last_punch_time` back afterwards so the running agent's cursor is not rewound.

The script can also be run by path against a bench whose installed copy of the
app is older: it only needs `biometric_hub.engine.ingest`.
"""

import argparse
import bisect
import csv
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import timedelta

# Small batches: each one holds the Employee Checkin naming-series lock until it
# commits, and the live agent needs that lock too.
BATCH = 50
RETRIES = 5


def _read_csv(path, date_from=None, date_to=None):
	rows = []
	with open(path, newline="", encoding="utf-8-sig") as fh:
		for r in csv.DictReader(fh):
			t = (r.get("punch_time") or "").strip()[:19]
			if not t or not (r.get("device_user_id") or "").strip():
				continue
			if date_from and t[:10] < date_from:
				continue
			if date_to and t[:10] > date_to:
				continue
			rows.append(
				{
					"device_serial": (r.get("device_serial") or "").strip(),
					"device_user_id": r["device_user_id"].strip(),
					"punch_time": t,
					"raw_log_type": (r.get("raw_log_type") or "").strip() or None,
					"payload": {"source": "backfill_csv"},
				}
			)
	return rows


def _people(path):
	"""device user id -> name, from the software's employee table export."""
	if not path:
		return {}
	out = {}
	with open(path, newline="", encoding="utf-8-sig") as fh:
		for r in csv.DictReader(fh):
			low = {k.lower(): (v or "").strip() for k, v in r.items() if k}
			code = low.get("emp_code") or low.get("badgenumber") or low.get("userid")
			name = " ".join(x for x in (low.get("first_name"), low.get("last_name")) if x) or low.get("name", "")
			if code:
				out[code] = name
	return out


def analyse(punches, near_seconds=120):
	import frappe
	from frappe.utils import get_datetime

	from biometric_hub.engine import ingest as eng

	ctx = eng._new_context()
	times = [p["punch_time"] for p in punches]
	lo = get_datetime(min(times)) - timedelta(days=1)
	hi = get_datetime(max(times)) + timedelta(days=1)

	checkins = defaultdict(list)
	for c in frappe.db.sql(
		"select employee, time, owner from `tabEmployee Checkin` where time between %s and %s",
		(lo, hi), as_dict=True,
	):
		checkins[c.employee].append(c.time)
	for v in checkins.values():
		v.sort()

	staged = dict(
		frappe.db.sql(
			"select punch_key, status from `tabBiometric Punch` where punch_time between %s and %s",
			(lo, hi),
		)
	)
	attendance = {
		(a.employee, a.attendance_date): a.status
		for a in frappe.db.sql(
			"select employee, attendance_date, status from tabAttendance "
			"where docstatus=1 and attendance_date between %s and %s",
			(lo.date(), hi.date()), as_dict=True,
		)
	}

	near = timedelta(seconds=near_seconds)
	matched = defaultdict(set)  # employee -> checkin times accounted for by the export
	resolved = []
	for p in punches:
		device = eng._get_device(ctx, p["device_serial"])
		if not device:
			resolved.append((p, None, None, None))
			continue
		site_local, _ = eng._normalise_time(p["punch_time"], device.device_timezone)
		employee = eng._resolve_employee(ctx, p["device_user_id"], device.name, site_local.date())
		resolved.append((p, device, site_local, employee))

	# Exact matches claim their checkin first, so a near-miss punch (a double tap)
	# can never take the checkin that belongs to an exact one.
	exact_set = {e: set(v) for e, v in checkins.items()}
	for _p, _d, t, employee in resolved:
		if employee and t in exact_set.get(employee, ()):
			matched[employee].add(t)

	def find(employee, t):
		if not employee:
			return None
		if t in exact_set.get(employee, ()):
			return t
		lst = checkins.get(employee, [])
		i = bisect.bisect_left(lst, t - near)
		best = None
		while i < len(lst) and lst[i] <= t + near:
			if lst[i] not in matched[employee] and (best is None or abs(lst[i] - t) < abs(best - t)):
				best = lst[i]
			i += 1
		if best is not None:
			matched[employee].add(best)
		return best

	out = []
	for p, device, site_local, employee in resolved:
		row = dict(p)
		if not device:
			row.update(bucket="unknown_device")
			out.append(row)
			continue
		row.update(device=device.name, time=site_local, employee=employee)
		key = eng.punch_key(p["device_serial"], p["device_user_id"], p["punch_time"])
		hit = find(employee, site_local)
		if key in staged:
			row.update(bucket="staged", status=staged[key])
		elif hit is not None:
			row.update(bucket="in_grohr", exact=(hit == site_local))
		elif device.ignore_punches_before and site_local < get_datetime(device.ignore_punches_before):
			row.update(bucket="before_cutoff")
		elif employee:
			row.update(bucket="to_import", attendance=attendance.get((employee, site_local.date())))
		else:
			row.update(bucket="to_import_unmapped")
		out.append(row)

	# Reverse direction: checkins in the exported window the software has no punch for.
	w_lo, w_hi = get_datetime(min(times)), get_datetime(max(times))
	only_grohr = []
	for c in frappe.db.sql(
		"select c.name, c.employee, c.employee_name, c.time, c.owner, c.device_id "
		"from `tabEmployee Checkin` c where c.time between %s and %s",
		(w_lo, w_hi), as_dict=True,
	):
		if c.time not in matched.get(c.employee, ()):
			only_grohr.append(c)
	return out, only_grohr


def write_report(rows, only_grohr, people, path):
	from openpyxl import Workbook
	from openpyxl.styles import Font

	buckets = ["in_grohr", "staged", "to_import", "to_import_unmapped", "before_cutoff", "unknown_device"]
	per_day = defaultdict(Counter)
	for r in rows:
		per_day[r["punch_time"][:10]][r["bucket"]] += 1
	only_day = Counter(str(c.time)[:10] for c in only_grohr)

	wb = Workbook()
	ws = wb.active
	ws.title = "By day"
	ws.append(["Date", "Punches in software", *buckets, "Checkins only in GroHR"])
	for day in sorted(set(per_day) | set(only_day)):
		c = per_day[day]
		ws.append([day, sum(c.values()), *[c[b] for b in buckets], only_day[day]])
	tot = Counter()
	for c in per_day.values():
		tot.update(c)
	ws.append(["TOTAL", sum(tot.values()), *[tot[b] for b in buckets], len(only_grohr)])

	um = defaultdict(list)
	for r in rows:
		if r["bucket"] == "to_import_unmapped" or (r["bucket"] == "staged" and r.get("status") == "Unmapped"):
			um[r["device_user_id"]].append(r)
	ws2 = wb.create_sheet("Unmapped IDs")
	ws2.append(["Device user ID", "Name in software", "Punches", "First", "Last", "Readers"])
	for uid, lst in sorted(um.items(), key=lambda kv: -len(kv[1])):
		ts = sorted(x["punch_time"] for x in lst)
		ws2.append([uid, people.get(uid, ""), len(lst), ts[0], ts[-1],
		            ", ".join(sorted({x["device_serial"] for x in lst}))])

	ws3 = wb.create_sheet("Attendance already marked")
	ws3.append(["Employee", "Date", "Attendance status", "Punches to import"])
	conflicts = Counter()
	for r in rows:
		if r["bucket"] == "to_import" and r.get("attendance"):
			conflicts[(r["employee"], str(r["time"].date()), r["attendance"])] += 1
	for (emp, day, status), n in sorted(conflicts.items(), key=lambda kv: (kv[0][1], kv[0][0])):
		ws3.append([emp, day, status, n])

	ws4 = wb.create_sheet("Only in GroHR")
	ws4.append(["Checkin", "Employee", "Name", "Time", "Created by", "Device ID"])
	for c in sorted(only_grohr, key=lambda c: c.time):
		ws4.append([c.name, c.employee, c.employee_name, str(c.time), c.owner, c.device_id or ""])

	for sheet in wb.worksheets:
		for cell in sheet[1]:
			cell.font = Font(bold=True)
		sheet.freeze_panes = "A2"
	wb.save(path)
	return tot


def apply(rows):
	"""Import the two to_import buckets through the engine, a batch at a time."""
	import frappe
	from frappe.utils import get_datetime

	from biometric_hub.engine import ingest as eng

	todo = [r for r in rows if r["bucket"] in ("to_import", "to_import_unmapped")]
	cursors = {
		d.name: d.last_punch_time
		for d in frappe.get_all("Biometric Device", fields=["name", "last_punch_time"])
	}
	total = Counter()
	for i in range(0, len(todo), BATCH):
		chunk = [
			{k: r[k] for k in ("device_serial", "device_user_id", "punch_time", "raw_log_type", "payload")}
			for r in todo[i:i + BATCH]
		]
		for attempt in range(1, RETRIES + 1):
			try:
				res = eng.ingest(chunk, source="Manual", connector=None, write_sync_log=False)
				break
			except Exception as e:
				# A deadlock with the live agent rolls back the whole transaction, so
				# the batch left nothing behind; wait and run it again.
				frappe.db.rollback()
				if attempt == RETRIES:
					raise
				print(f"  batch at {i}: {type(e).__name__}, retry {attempt}/{RETRIES - 1}", flush=True)
				time.sleep(3 * attempt)
		for k in ("mapped", "unmapped", "duplicates", "errors", "ignored", "pending"):
			total[k] += res[k]
		# The engine moves a device's cursor to the newest punch of the batch; for a
		# backfill that is a rewind. Put back whichever is later.
		for name, before in cursors.items():
			now = frappe.db.get_value("Biometric Device", name, "last_punch_time")
			if before and (not now or get_datetime(now) < get_datetime(before)):
				frappe.db.set_value("Biometric Device", name, "last_punch_time", before, update_modified=False)
		frappe.db.commit()
		done = min(i + BATCH, len(todo))
		if done % 1000 == 0 or done == len(todo):
			print(f"  {done}/{len(todo)}  {dict(total)}", flush=True)
	return total


def main():
	p = argparse.ArgumentParser(prog="backfill_csv")
	p.add_argument("--site", required=True)
	p.add_argument("--csv", required=True)
	p.add_argument("--people")
	p.add_argument("--from", dest="date_from")
	p.add_argument("--to", dest="date_to")
	p.add_argument("--near", type=int, default=120, help="seconds either side that count as the same punch")
	p.add_argument("--out", default="backfill-report.xlsx")
	p.add_argument("--apply", action="store_true")
	p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
	a = p.parse_args()

	import frappe

	frappe.init(site=a.site, sites_path=os.getcwd())
	frappe.connect()
	try:
		punches = _read_csv(a.csv, a.date_from, a.date_to)
		if not punches:
			sys.exit("No punches in the CSV for that range.")
		print(f"{len(punches)} punches read from {a.csv}")
		rows, only_grohr = analyse(punches, a.near)
		tot = write_report(rows, only_grohr, _people(a.people), a.out)
		print(f"Report: {a.out}")
		for k, v in sorted(tot.items()):
			print(f"  {k:<20} {v}")
		print(f"  {'only_in_grohr':<20} {len(only_grohr)}")

		if not a.apply:
			print("Read-only run: nothing written. Add --apply to import the to_import buckets.")
			return
		n = tot["to_import"] + tot["to_import_unmapped"]
		if not a.yes and input(f"Import {n} punches into {a.site}? Type YES: ").strip() != "YES":
			print("Cancelled.")
			return
		result = apply(rows)
		print(f"Done: {dict(result)}")
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
