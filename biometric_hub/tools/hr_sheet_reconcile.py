"""READ-ONLY: reconcile an HR employee sheet against GroHR and the biometric punches.

For each row of HR's sheet: is the employee in GroHR, active, same name/branch,
do they have a device ID, is it unique, do the machines actually send it, and is
there an unmapped machine ID that looks like theirs. Also lists active GroHR
employees missing from the sheet and machine IDs nobody is mapped to.

Only SELECTs; never commits. Writes one .xlsx report. Run from the bench's
sites folder as the bench user:

    ../env/bin/python <path>/hr_sheet_reconcile.py --site SITE --hr-file FILE.xlsx         [--sheet "Jun 2026 Data"] [--out report.xlsx]

The sheet needs the columns "Employee ID - New", "First Name", "Location"
(and optionally "Code", "Designation").
"""
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher

import frappe
import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import argparse

ap = argparse.ArgumentParser()
ap.add_argument("--site", required=True)
ap.add_argument("--hr-file", required=True)
ap.add_argument("--sheet", default=None, help="default: first sheet with an 'Employee ID - New' header")
ap.add_argument("--out", default="hr-device-id-reconcile.xlsx")
args = ap.parse_args()
HR_FILE, SHEET, OUT = args.hr_file, args.sheet, args.out
RECENT_DAYS = 30


def norm_name(s):
	return re.sub(r"[^a-z]", "", str(s or "").lower())


def sim(a, b):
	a, b = norm_name(a), norm_name(b)
	return round(SequenceMatcher(None, a, b).ratio(), 2) if a and b else 0


def id_key(v):
	"""VNR000012 == VNR0012 == vnr12 : letters + integer value."""
	m = re.fullmatch(r"\s*([A-Za-z]*)0*(\d+)\s*", str(v or ""))
	return f"{m.group(1).upper()}{int(m.group(2))}" if m else str(v or "").strip().upper()


# ------------------------------------------------------------------ HR sheet
wb = openpyxl.load_workbook(HR_FILE, read_only=True, data_only=True)
hr, hdr = [], None
if not SHEET:
	SHEET = next(ws.title for ws in wb.worksheets
	             if "Employee ID - New" in [str(c).strip() for c in next(ws.iter_rows(max_row=1, values_only=True), ()) if c])
for i, r in enumerate(wb[SHEET].iter_rows(values_only=True)):
	if i == 0:
		hdr = [str(h).strip() if h else "" for h in r]
		continue
	if any(r):
		d = dict(zip(hdr, r))
		d["_row"] = i + 1
		hr.append(d)

# ------------------------------------------------------------------ GroHR (read-only)
frappe.init(site=args.site, sites_path=".")
frappe.connect()
try:
	cols = {d.fieldname for d in frappe.get_meta("Employee").fields}
	want = [c for c in ("employee_name", "status", "branch", "sub_branch", "staff_id", "attendance_device_id",
	                    "cell_number", "date_of_joining", "relieving_date") if c in cols]
	emps = frappe.db.sql(f"select name, {', '.join('`'+c+'`' for c in want)} from tabEmployee limit 100000", as_dict=True)
	since = frappe.utils.add_days(frappe.utils.today(), -RECENT_DAYS)
	punch = frappe.db.sql(
		"""select device_user_id, device, status, employee, count(*) n, max(punch_time) last,
		          sum(punch_time >= %s) recent
		   from `tabBiometric Punch` group by device_user_id, device, status, employee limit 200000""",
		(since,), as_dict=True)
	enr = frappe.db.sql("select employee, device_user_id, device, enabled from `tabBiometric Enrolment` limit 100000", as_dict=True)
finally:
	frappe.db.rollback()
	frappe.destroy()

E = {e.name: e for e in emps}
branch_of = lambda e: (e.get("branch") or e.get("sub_branch") or "")
by_devid = defaultdict(list)
for e in emps:
	if e.get("attendance_device_id"):
		by_devid[str(e.attendance_device_id).strip()].append(e.name)

mapped = defaultdict(lambda: {"n": 0, "recent": 0, "last": None, "ids": set(), "devices": set()})
unmapped = defaultdict(lambda: {"n": 0, "recent": 0, "last": None, "devices": set()})
sent_ids = set()
for p in punch:
	sent_ids.add(str(p.device_user_id).strip())
	if p.status in ("Mapped", "Pending", "Blocked") and p.employee:
		m = mapped[p.employee]
		m["n"] += p.n; m["recent"] += int(p.recent or 0); m["ids"].add(p.device_user_id); m["devices"].add(p.device or "")
		m["last"] = max(filter(None, [m["last"], str(p.last)]))
	elif p.status == "Unmapped":
		u = unmapped[str(p.device_user_id).strip()]
		u["n"] += p.n; u["recent"] += int(p.recent or 0); u["devices"].add(p.device or "")
		u["last"] = max(filter(None, [u["last"], str(p.last)]))
sent_by_key = defaultdict(set)
for s in sent_ids:
	sent_by_key[id_key(s)].add(s)
enr_by_id = defaultdict(set)
for x in enr:
	if x.enabled:
		enr_by_id[str(x.device_user_id).strip()].add(x.employee)

# ------------------------------------------------------------------ reconcile
rows, hr_ids = [], set()
for h in hr:
	hid = str(h.get("Employee ID - New") or "").strip()
	hr_ids.add(hid)
	e = E.get(hid)
	issues, fix = [], ""
	r = {
		"HR row": h["_row"], "Employee ID": hid or "(blank)", "HR name": h.get("First Name"),
		"HR location": h.get("Location"), "HR code": h.get("Code"), "HR designation": h.get("Designation"),
		"GroHR name": "", "GroHR status": "", "GroHR branch": "", "Device ID in GroHR": "",
		"Name match": "", "Punches (30d)": 0, "Last punch": "", "IDs machines send for them": "",
		"Suggested device ID": "",
	}
	if not hid:
		issues.append("HR row has no Employee ID")
	elif not e:
		issues.append("NOT IN GroHR (new joiner to create?)")
	else:
		dev = str(e.get("attendance_device_id") or "").strip()
		m = mapped.get(hid)
		r.update({
			"GroHR name": e.employee_name, "GroHR status": e.status, "GroHR branch": branch_of(e),
			"Device ID in GroHR": dev, "Name match": sim(h.get("First Name"), e.employee_name),
			"Punches (30d)": m["recent"] if m else 0, "Last punch": (m["last"] or "")[:16] if m else "",
			"IDs machines send for them": ", ".join(sorted(m["ids"])) if m else "",
		})
		if e.status != "Active":
			issues.append(f"HR says working, GroHR says {e.status}")
		if r["Name match"] and r["Name match"] < 0.6:
			issues.append("NAME MISMATCH - wrong person on this ID?")
		loc = str(h.get("Location") or "")
		if loc and branch_of(e) and loc.lower() not in branch_of(e).lower():
			issues.append(f"Branch differs (HR {loc} / GroHR {branch_of(e)})")
		if loc and re.fullmatch(r"[A-Z]{3}\d+", hid) and hid[:3] != loc:
			issues.append(f"Works at {loc} but ID is {hid[:3]} (transfer: allow both machines)")
		if not dev:
			issues.append("DEVICE ID MISSING")
			# suggest: an unmapped machine ID that equals the employee ID (ignoring zero padding)
			cand = [s for s in sent_by_key.get(id_key(hid), ()) if s in unmapped]
			if cand:
				r["Suggested device ID"] = cand[0]
				fix = "Machine already sends this ID - set it"
		else:
			others = [x for x in by_devid.get(dev, []) if x != hid]
			if others:
				issues.append(f"SAME device ID on {', '.join(others)}")
			enr_other = [x for x in enr_by_id.get(dev, ()) if x != hid]
			if enr_other:
				issues.append(f"Enrolment for {dev} points to {', '.join(enr_other)}")
			if dev not in sent_ids:
				near = [s for s in sent_by_key.get(id_key(dev), ()) if s != dev]
				if near:
					issues.append(f"MIS-TYPED: machines send {near[0]}, GroHR has {dev}")
					r["Suggested device ID"] = near[0]
				else:
					issues.append("Device ID never received from any machine (wrong ID or machine not connected)")
			elif not (m and m["recent"]):
				issues.append(f"No punch in {RECENT_DAYS} days")
	r["Problems"] = "; ".join(issues) or "OK"
	r["Category"] = (
		"OK" if not issues else
		"Not in GroHR" if any(i.startswith("NOT IN") or i.startswith("HR row has no") for i in issues) else
		"Wrong / duplicate ID" if any(k in r["Problems"] for k in ("SAME device", "Enrolment for", "MIS-TYPED", "NAME MISMATCH")) else
		"Missing device ID" if "DEVICE ID MISSING" in issues else
		"ID never punched" if "never received" in r["Problems"] else
		"Status / branch" if any(k in r["Problems"] for k in ("GroHR says", "Branch differs", "transfer")) else
		"Check"
	)
	r["Suggested fix"] = fix
	rows.append(r)

extra = []
for e in emps:
	if e.status == "Active" and e.name not in hr_ids:
		m = mapped.get(e.name)
		extra.append({
			"Employee ID": e.name, "GroHR name": e.employee_name, "GroHR branch": branch_of(e),
			"Device ID in GroHR": e.get("attendance_device_id") or "",
			"Punches (30d)": m["recent"] if m else 0, "Last punch": (m["last"] or "")[:16] if m else "",
			"Problem": "Active in GroHR but NOT in HR sheet - left? (punching recently = still working)"
			if not (m and m["recent"]) else "Active in GroHR, NOT in HR sheet, but STILL PUNCHING - missing from HR sheet?",
		})

held_by = {}
for dev, names in by_devid.items():
	held_by[dev] = ", ".join(names)
hr_by_key = {id_key(h.get("Employee ID - New")): h for h in hr if h.get("Employee ID - New")}
um = []
for uid, u in sorted(unmapped.items(), key=lambda kv: -kv[1]["recent"]):
	h = hr_by_key.get(id_key(uid))
	um.append({
		"Machine user ID": uid, "Machines": ", ".join(sorted(u["devices"])), "Punches (all)": u["n"],
		"Punches (30d)": u["recent"], "Last punch": (u["last"] or "")[:16],
		"Looks like HR employee": (h or {}).get("Employee ID - New", ""), "HR name": (h or {}).get("First Name", ""),
		"Note": "ID matches an HR Employee ID ignoring zero padding" if h else "No HR match - ask branch who this is",
	})

# ------------------------------------------------------------------ write
out = openpyxl.Workbook()
summary = out.active
summary.title = "Summary"
cats = defaultdict(int)
for r in rows:
	cats[r["Category"]] += 1
summary.append(["a customer - HR sheet vs GroHR vs biometric machines (read-only)"])
summary.append([f"HR file: {HR_FILE.rsplit('/',1)[-1]} / {SHEET}"])
summary.append([])
summary.append(["HR sheet rows", len(hr)])
for k in ("OK", "Missing device ID", "Wrong / duplicate ID", "ID never punched", "Not in GroHR", "Status / branch", "Check"):
	summary.append([f"  {k}", cats.get(k, 0)])
summary.append(["Active in GroHR but not in HR sheet", len(extra)])
summary.append(["  of which still punching (30d)", sum(1 for x in extra if x["Punches (30d)"])])
summary.append(["Machine IDs nobody is mapped to", len(um)])
summary.append(["  of which punching in last 30 days", sum(1 for x in um if x["Punches (30d)"])])
summary.append(["  of which match an HR Employee ID", sum(1 for x in um if x["Looks like HR employee"])])
summary["A1"].font = Font(bold=True, size=13)
summary.column_dimensions["A"].width = 48

fills = {"OK": "E2F0D9", "Missing device ID": "FCE4D6", "Wrong / duplicate ID": "F8CBAD",
         "ID never punched": "FFF2CC", "Not in GroHR": "DDEBF7", "Status / branch": "EDEDED", "Check": "FFFFFF"}


def sheet(title, data, color_key=None):
	ws = out.create_sheet(title)
	if not data:
		ws.append(["(none)"])
		return
	keys = list(data[0].keys())
	ws.append(keys)
	for c in ws[1]:
		c.font = Font(bold=True)
	for d in data:
		ws.append([str(d.get(k)) if isinstance(d.get(k), (set, list)) else d.get(k) for k in keys])
		if color_key:
			for c in ws[ws.max_row]:
				c.fill = PatternFill("solid", fgColor=fills.get(d[color_key], "FFFFFF"))
	for i, k in enumerate(keys, 1):
		ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, len(k) + 2, *(len(str(d.get(k) or "")) for d in data[:200])))
	ws.freeze_panes = "A2"
	ws.auto_filter.ref = ws.dimensions


order = list(fills)
sheet("HR sheet vs GroHR", sorted(rows, key=lambda r: (order.index(r["Category"]), r["Employee ID"])), "Category")
sheet("In GroHR not in HR sheet", sorted(extra, key=lambda x: -x["Punches (30d)"]))
sheet("Unmapped machine IDs", um)
out.save(OUT)

print(f"HR rows {len(hr)} | " + " | ".join(f"{k} {v}" for k, v in sorted(cats.items())))
print(f"Active in GroHR not in HR sheet: {len(extra)} | unmapped machine IDs: {len(um)}")
print("Report:", OUT)
