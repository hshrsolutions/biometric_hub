"""Attendance Device ID clean-up workbook — build it, let HR fill it, apply it.

Why this exists: a punch reaches an employee only through a device user ID. When
the Employee's Attendance Device ID is empty or wrong, every punch is parked as
Unmapped and the attendance job marks the person Absent. Fixing that by hand on
the Employee form is slow, and on its own it is not enough: a global
`Biometric Enrolment` for the old ID outranks the Employee field, so the old
mapping keeps winning. This tool does the whole job in three steps.

1. build  (read-only)  ->  an .xlsx with every employee, what is wrong with their
   ID, how often the machines actually send it, and a suggested machine ID
   matched by name or by ID format. HR fills two columns: Action and New ID.
2. apply  (dry run by default)  ->  reads the filled workbook back, checks every
   row (unique, not held by somebody else, not a placeholder) and writes a result
   workbook saying exactly what would happen.
3. apply --apply  ->  does it: frees old IDs first (disables their enrolments),
   then sets the new IDs, enables a global enrolment for each, comments on the
   Employee and replays the Unmapped punches behind every new ID.

The Employee is written with `frappe.db.set_value`, not `doc.save()`: the save
would re-run every Employee validation (mandatory fields added since, other
apps' hooks) and one broken unrelated rule would block the whole clean-up.

Run from the bench's `sites` folder, as the bench user:

    ../env/bin/python <path>/device_id_workbook.py build --site SITE --out ids.xlsx \
        [--names people.csv|backfill-report.xlsx ...]
    ../env/bin/python <path>/device_id_workbook.py apply --site SITE --in ids-filled.xlsx
    ../env/bin/python <path>/device_id_workbook.py apply --site SITE --in ids-filled.xlsx --apply

`--names` gives the names the attendance software holds for each machine user
ID (the agent's `export_punches.py` people CSV, or the backfill report's
"Unmapped IDs" sheet). Without it, suggestions come from ID format only.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher

SHEET = "Fix list"
FIRST_ROW = 5  # header on row 4: rows 1-3 carry the title and the how-to line

ACTION_SUGGESTED = "Use suggested ID"
ACTION_NEW = "Use New ID (col Q)"
ACTION_KEEP = "Keep current"
ACTION_CLEAR = "Clear ID"
ACTION_LATER = "Machine not connected - later"
ACTIONS = [ACTION_SUGGESTED, ACTION_NEW, ACTION_KEEP, ACTION_CLEAR, ACTION_LATER]

COLUMNS = [
	# (header, key, width)
	("Employee", "employee", 12),
	("Name", "employee_name", 26),
	("Branch", "branch", 10),
	("Department", "department", 18),
	("Designation", "designation", 18),
	("Status", "status", 8),
	("Current Device ID", "current_id", 14),
	("Problem", "problem", 30),
	("Punches on current ID", "cur_punches", 11),
	("Last punch on current ID", "cur_last", 17),
	("Suggested machine ID", "sug_id", 14),
	("Name in machine", "sug_name", 24),
	("Punches on suggested ID", "sug_punches", 11),
	("Last punch on suggested ID", "sug_last", 17),
	("Why suggested", "sug_why", 30),
	("ACTION (pick)", "action", 26),
	("NEW Device ID (type)", "new_id", 14),
	("HR remarks", "remarks", 28),
]
COL = {key: i + 1 for i, (_h, key, _w) in enumerate(COLUMNS)}

PLACEHOLDER = re.compile(r"^(?:[A-Za-z\- ]+|.*test.*)$", re.I)  # no digit at all, or "test"


# ----------------------------------------------------------------------------- helpers


def _connect(site):
	import frappe

	frappe.init(site=site, sites_path=os.getcwd())
	frappe.connect()
	return frappe


def _norm_name(s):
	return " ".join(re.sub(r"[^a-z ]", " ", (s or "").lower()).split())


def _name_score(a, b):
	"""0..1 similarity of two person names, word order ignored."""
	a, b = _norm_name(a), _norm_name(b)
	if not a or not b:
		return 0.0
	ta, tb = a.split(), b.split()
	longest = max(ta, key=len)
	# The longest word of the machine name must be in the employee name — stops
	# "R Kumar" matching every Kumar.
	if len(longest) >= 3 and not any(longest == w or (len(w) >= 4 and SequenceMatcher(None, longest, w).ratio() >= 0.85) for w in tb):
		return 0.0
	return max(
		SequenceMatcher(None, a, b).ratio(),
		SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio(),
		SequenceMatcher(None, "".join(ta), "".join(tb)).ratio(),
	)


def _id_shape(device_id):
	"""('VNR', 18) for VNR00018 / VNR0018, ('', 1530) for 1530, None if no digits."""
	m = re.match(r"^\s*([A-Za-z]*)\s*0*(\d+)\s*$", device_id or "")
	return (m.group(1).upper(), int(m.group(2))) if m else None


def _load_names(paths):
	"""device user id -> name in the attendance software."""
	out = {}
	for path in paths or []:
		if path.lower().endswith(".xlsx"):
			from openpyxl import load_workbook

			wb = load_workbook(path, read_only=True, data_only=True)
			for ws in wb.worksheets:
				rows = ws.iter_rows(values_only=True)
				header = None
				for row in rows:
					cells = [str(c).strip().lower() if c is not None else "" for c in row]
					if header is None:
						if "device user id" in cells and any("name" in c for c in cells):
							header = cells
							i_id = cells.index("device user id")
							i_nm = next(i for i, c in enumerate(cells) if "name" in c)
						continue
					if row[i_id] not in (None, "") and row[i_nm]:
						out.setdefault(str(row[i_id]).strip(), str(row[i_nm]).strip())
		else:
			with open(path, newline="", encoding="utf-8-sig") as fh:
				for r in csv.DictReader(fh):
					low = {k.lower(): (v or "").strip() for k, v in r.items() if k}
					code = low.get("emp_code") or low.get("badgenumber") or low.get("userid") or low.get("device user id")
					name = " ".join(x for x in (low.get("first_name"), low.get("last_name")) if x) or low.get("name") or low.get("name in software", "")
					if code and name:
						out.setdefault(code, name)
	return out


# ----------------------------------------------------------------------------- build


def collect(frappe, names):
	emp_meta = frappe.get_meta("Employee")
	want = ["name", "employee_name", "branch", "department", "designation", "status", "attendance_device_id", "relieving_date"]
	fields = [f for f in want if f == "name" or emp_meta.has_field(f)]
	emps = frappe.db.sql(
		f"select {', '.join('`%s`' % f for f in fields)} from `tabEmployee` order by name limit 1000000", as_dict=True
	)
	for e in emps:
		for f in want:
			e.setdefault(f, None)
		e.attendance_device_id = (e.attendance_device_id or "").strip()

	since_30 = datetime.now() - timedelta(days=30)
	stats = {}
	for r in frappe.db.sql(
		"""select device_user_id, count(*) n, max(punch_time) last, min(punch_time) first,
		       sum(status='Unmapped') unmapped, sum(punch_time >= %s) n30,
		       group_concat(distinct device separator ', ') devices
		   from `tabBiometric Punch` group by device_user_id limit 1000000""",
		since_30,
		as_dict=True,
	):
		stats[(r.device_user_id or "").strip()] = r

	# which employee each ID's mapped punches went to
	mapped_to = defaultdict(set)
	for r in frappe.db.sql(
		"""select device_user_id, employee from `tabBiometric Punch`
		   where status='Mapped' and employee is not null group by device_user_id, employee limit 1000000"""
	):
		mapped_to[(r[0] or "").strip()].add(r[1])

	enrol = defaultdict(list)  # employee -> [(id, device, enabled)]
	enrol_by_id = defaultdict(list)
	for r in frappe.db.sql(
		"select name, employee, device_user_id, device, enabled from `tabBiometric Enrolment` limit 1000000", as_dict=True
	):
		enrol[r.employee].append(r)
		enrol_by_id[(r.device_user_id or "").strip()].append(r)

	# a branch is "connected" when somebody in it got a mapped punch in 30 days
	emp_branch = {e.name: e.branch for e in emps}
	connected = set()
	for r in frappe.db.sql(
		"select distinct employee from `tabBiometric Punch` where status='Mapped' and punch_time >= %s limit 1000000", since_30
	):
		connected.add(emp_branch.get(r[0]))

	return emps, stats, mapped_to, enrol, enrol_by_id, connected


def suggest(emps, stats, mapped_to, names):
	"""employee -> (machine id, why). Only machine IDs nobody owns are suggested."""
	held = {e.attendance_device_id for e in emps if e.attendance_device_id}
	free_ids = [i for i, s in stats.items() if i and i not in held and s.unmapped]
	active = [e for e in emps if e.status == "Active"]
	out = {}

	# 1. same ID, different format (VNR00018 typed, machine sends VNR0018)
	by_shape = defaultdict(list)
	for i in free_ids:
		if _id_shape(i):
			by_shape[_id_shape(i)].append(i)
	for e in active:
		shape = _id_shape(e.attendance_device_id)
		if shape and shape[0] and not stats.get(e.attendance_device_id) and len(by_shape.get(shape, [])) == 1:
			out[e.name] = (by_shape[shape][0], "Same number, different format")

	# 2. name in the attendance software
	if names:
		claims = defaultdict(list)  # employee -> [(score, id)]
		for mid in free_ids:
			mname = names.get(mid)
			if not mname:
				continue
			scored = sorted(((_name_score(mname, e.employee_name), e.name) for e in active), reverse=True)
			best = [s for s in scored if s[0] >= 0.8]
			if len(best) == 1 or (len(best) > 1 and best[0][0] - best[1][0] >= 0.1):
				claims[best[0][1]].append((best[0][0], mid))
		for emp, cl in claims.items():
			if emp in out:
				continue
			cl.sort(reverse=True)
			score, mid = cl[0]
			why = f"Name match {score:.0%}"
			if len(cl) > 1:
				why += f"; also {', '.join(m for _s, m in cl[1:4])}"
			out[emp] = (mid, why)
	return out


def classify(e, stats, mapped_to, enrol, connected):
	cid = e.attendance_device_id
	s = stats.get(cid)
	globals_ = [r for r in enrol.get(e.name, []) if not r.device and r.enabled]
	if e.status != "Active":
		if cid and s and s.n30:
			return 1, "Left but ID still punching"
		return (5, "Left - still holds an ID") if cid else (9, "OK")
	if not cid:
		if e.branch in connected:
			return 1, "No ID"
		return 3, "No ID - branch machine not connected"
	if PLACEHOLDER.match(cid):
		return 1, "Placeholder ID"
	if not s:
		return 2, "ID never sent by any machine"
	others = mapped_to.get(cid, set()) - {e.name}
	if others:
		return 2, f"ID's punches go to {', '.join(sorted(others))}"
	if globals_ and all((r.device_user_id or "").strip() != cid for r in globals_):
		return 2, f"Enrolment says {globals_[0].device_user_id}"
	if not s.n30:
		return 4, "No punch in 30 days"
	return 9, "OK"


def build(site, out, name_paths):
	frappe = _connect(site)
	try:
		if not name_paths:
			name_paths = sorted(
				os.path.join("..", "docs", f) for f in os.listdir(os.path.join("..", "docs")) if "backfill-report" in f and f.endswith(".xlsx")
			) if os.path.isdir(os.path.join("..", "docs")) else []
			if name_paths:
				print(f"Machine names from: {', '.join(name_paths)}")
		names = _load_names(name_paths)
		emps, stats, mapped_to, enrol, enrol_by_id, connected = collect(frappe, names)
		sug = suggest(emps, stats, mapped_to, names)
		rows = []
		for e in emps:
			if e.status != "Active" and not e.attendance_device_id:
				continue
			prio, problem = classify(e, stats, mapped_to, enrol, connected)
			s = stats.get(e.attendance_device_id)
			sid, why = sug.get(e.name, (None, None))
			ss = stats.get(sid) if sid else None
			if sid and prio == 9:
				problem, prio = "OK (but see suggestion)", 6
			rows.append(
				dict(
					prio=prio,
					employee=e.name,
					employee_name=e.employee_name,
					branch=e.branch,
					department=e.department,
					designation=e.designation,
					status=e.status,
					current_id=e.attendance_device_id or None,
					problem=problem,
					cur_punches=s.n if s else 0,
					cur_last=s.last if s else None,
					sug_id=sid,
					sug_name=names.get(sid) if sid else None,
					sug_punches=ss.n if ss else None,
					sug_last=ss.last if ss else None,
					sug_why=why,
					action=ACTION_KEEP if prio == 9 else None,
				)
			)
		rows.sort(key=lambda r: (r["prio"], r["branch"] or "~", r["employee"]))

		held = {e.attendance_device_id: e for e in emps if e.attendance_device_id}
		emp_name = {e.name: e.employee_name for e in emps}
		unowned = []
		for mid, s in stats.items():
			if not mid or mid in held or not s.unmapped:
				continue
			owner = next((k for k, v in sug.items() if v[0] == mid), None)
			unowned.append([mid, names.get(mid), s.n, s.unmapped, s.first, s.last, s.devices, owner, emp_name.get(owner)])
		unowned.sort(key=lambda r: -r[2])

		write_workbook(out, site, rows, unowned, emps, connected)
		counts = defaultdict(int)
		for r in rows:
			counts[r["problem"].split(" - ")[0] if not r["problem"].startswith("ID's") else "ID used by someone else"] += 1
		print(f"Workbook: {out}")
		print(f"  employees listed     {len(rows)}")
		for k, v in sorted(counts.items(), key=lambda x: -x[1]):
			print(f"  {k:<32} {v}")
		print(f"  suggestions          {sum(1 for r in rows if r['sug_id'])}")
		print(f"  machine IDs unowned  {len(unowned)}")
		print("Read-only: nothing was written to the site.")
	finally:
		frappe.destroy()


def write_workbook(out, site, rows, unowned, emps, connected):
	from openpyxl import Workbook
	from openpyxl.formatting.rule import FormulaRule
	from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
	from openpyxl.utils import get_column_letter
	from openpyxl.worksheet.datavalidation import DataValidation

	head = PatternFill("solid", fgColor="1F3864")
	inp = PatternFill("solid", fgColor="FFF2CC")
	thin = Side(style="thin", color="BFBFBF")
	wrap = Alignment(wrap_text=True, vertical="top")

	wb = Workbook()

	# ---- Read me
	rm = wb.active
	rm.title = "Read me"
	lines = [
		("Attendance Device ID clean-up", Font(bold=True, size=16)),
		(f"Site: {site}    Built: {datetime.now():%d %b %Y %H:%M}", Font(italic=True, color="666666")),
		("", None),
		("Why this matters", Font(bold=True, size=12)),
		("The fingerprint machine sends only its own user number (e.g. 1530). GroHR gives that punch to the employee whose", None),
		("Attendance Device ID is exactly that number. Empty or wrong ID = punch belongs to nobody = employee marked Absent.", None),
		("", None),
		("What to do (sheet 'Fix list')", Font(bold=True, size=12)),
		("1. Work from the top: rows are sorted most urgent first. Use the filter on 'Problem' and 'Branch'.", None),
		("2. For each row pick an ACTION (yellow column P):", None),
		(f"     • {ACTION_SUGGESTED} — the machine ID in column K is right for this person (check the name in column L).", None),
		(f"     • {ACTION_NEW} — type the correct machine user number in column Q (as shown in the attendance software).", None),
		(f"     • {ACTION_KEEP} — current ID is right; nothing changes.", None),
		(f"     • {ACTION_CLEAR} — remove the ID (e.g. a Left employee whose number was given to someone else).", None),
		(f"     • {ACTION_LATER} — branch machine is not connected to GroHR yet; nothing changes now.", None),
		("3. Leave ACTION empty if you are not sure — empty rows are skipped.", None),
		("4. Do not change columns A–O, do not sort away the header, do not rename the sheet.", None),
		("5. Send the file back. It is checked first (dry run) and you get a result file before anything changes.", None),
		("", None),
		("Rules the check enforces", Font(bold=True, size=12)),
		("• One machine number belongs to one employee only (unless the other employee is cleared/changed in the same file).", None),
		("• Placeholder values (DGL, Test-209, letters only) are refused.", None),
		("• A number that no machine has ever sent is allowed but flagged — double-check it.", None),
		("• Suggestions are evidence, not proof: name matches come from the attendance software and can be wrong for namesakes.", None),
		("", None),
		("What happens when it is applied", Font(bold=True, size=12)),
		("• Employee Attendance Device ID is updated (with a comment on the Employee saying who/when/why).", None),
		("• The biometric mapping (Biometric Enrolment) is moved to the new number.", None),
		("• All punches already received under the new number are re-processed and become check-ins.", None),
		("• Attendance for the last 18 days is recalculated by the hourly job. Older Absent days are NOT changed automatically.", None),
		("", None),
		("Other sheets", Font(bold=True, size=12)),
		("• 'Machine IDs with no employee' — numbers the machines send that belong to nobody. Use it to find a person's number.", None),
		("• 'Branch summary' — counts per branch, and whether the branch machine is connected.", None),
	]
	for i, (text, font) in enumerate(lines, 1):
		c = rm.cell(row=i, column=1, value=text)
		if font:
			c.font = font
	rm.column_dimensions["A"].width = 130

	# ---- Fix list
	ws = wb.create_sheet(SHEET)
	ws["A1"] = "Attendance Device ID — Fix list"
	ws["A1"].font = Font(bold=True, size=14)
	ws["A2"] = "Fill only the yellow columns: ACTION (P), NEW Device ID (Q), HR remarks (R). See 'Read me'."
	ws["A2"].font = Font(italic=True, color="C00000")
	hr = FIRST_ROW - 1
	for i, (h, key, w) in enumerate(COLUMNS, 1):
		c = ws.cell(row=hr, column=i, value=h)
		c.font = Font(bold=True, color="FFFFFF")
		c.fill = inp if key in ("action", "new_id", "remarks") else head
		if key in ("action", "new_id", "remarks"):
			c.font = Font(bold=True, color="000000")
		c.alignment = Alignment(wrap_text=True, vertical="center")
		ws.column_dimensions[get_column_letter(i)].width = w
	ws.row_dimensions[hr].height = 42

	for r_i, r in enumerate(rows, FIRST_ROW):
		for i, (_h, key, _w) in enumerate(COLUMNS, 1):
			v = r.get(key)
			c = ws.cell(row=r_i, column=i, value=v)
			c.border = Border(bottom=thin)
			if key in ("cur_last", "sug_last") and v:
				c.number_format = "dd-mmm-yy hh:mm"
			if key in ("current_id", "sug_id", "new_id"):
				c.number_format = "@"  # keep 00123 / VNR0018 as text
			if key in ("action", "new_id", "remarks"):
				c.fill = inp
	last = FIRST_ROW + max(len(rows), 1) - 1

	dv = DataValidation(type="list", formula1='"' + ",".join(ACTIONS) + '"', allow_blank=True)
	dv.error, dv.errorTitle = "Pick an action from the list.", "Action"
	ws.add_data_validation(dv)
	dv.add(f"P{FIRST_ROW}:P{last}")

	rng = f"A{FIRST_ROW}:R{last}"
	red = PatternFill("solid", fgColor="F8CBAD")
	amber = PatternFill("solid", fgColor="FFE699")
	grey = PatternFill("solid", fgColor="EDEDED")
	green = PatternFill("solid", fgColor="C6EFCE")
	ws.conditional_formatting.add(f"H{FIRST_ROW}:H{last}", FormulaRule(formula=[f'LEFT($H{FIRST_ROW},2)="OK"'], fill=green))
	ws.conditional_formatting.add(f"H{FIRST_ROW}:H{last}", FormulaRule(formula=[f'ISNUMBER(SEARCH("not connected",$H{FIRST_ROW}))'], fill=grey))
	ws.conditional_formatting.add(
		f"H{FIRST_ROW}:H{last}",
		FormulaRule(formula=[f'OR($H{FIRST_ROW}="No ID",$H{FIRST_ROW}="Placeholder ID",LEFT($H{FIRST_ROW},4)="Left")'], fill=red),
	)
	ws.conditional_formatting.add(f"H{FIRST_ROW}:H{last}", FormulaRule(formula=[f'$H{FIRST_ROW}<>""'], fill=amber))
	# action needs a New ID but none typed
	ws.conditional_formatting.add(
		f"Q{FIRST_ROW}:Q{last}", FormulaRule(formula=[f'AND($P{FIRST_ROW}="{ACTION_NEW}",$Q{FIRST_ROW}="")'], fill=red)
	)
	# suggested chosen but there is no suggestion
	ws.conditional_formatting.add(
		f"P{FIRST_ROW}:P{last}", FormulaRule(formula=[f'AND($P{FIRST_ROW}="{ACTION_SUGGESTED}",$K{FIRST_ROW}="")'], fill=red)
	)
	# same new ID typed twice
	ws.conditional_formatting.add(
		f"Q{FIRST_ROW}:Q{last}", FormulaRule(formula=[f'AND($Q{FIRST_ROW}<>"",COUNTIF($Q${FIRST_ROW}:$Q${last},$Q{FIRST_ROW})>1)'], fill=red)
	)
	ws.freeze_panes = ws.cell(row=FIRST_ROW, column=3)
	ws.auto_filter.ref = f"A{hr}:R{last}"

	# ---- Machine IDs with no employee
	mu = wb.create_sheet("Machine IDs with no employee")
	hdr = ["Machine user ID", "Name in machine", "Punches", "Unmapped punches", "First punch", "Last punch", "Machines", "Suggested employee", "Employee name"]
	mu.append(hdr)
	for c in mu[1]:
		c.font, c.fill = Font(bold=True, color="FFFFFF"), head
	for r in unowned:
		mu.append(r)
	for row in mu.iter_rows(min_row=2, min_col=5, max_col=6):
		for c in row:
			c.number_format = "dd-mmm-yy hh:mm"
	for i, w in enumerate([14, 26, 10, 10, 16, 16, 40, 16, 26], 1):
		mu.column_dimensions[get_column_letter(i)].width = w
	mu.freeze_panes = "B2"
	mu.auto_filter.ref = f"A1:I{max(len(unowned) + 1, 2)}"

	# ---- Branch summary
	bs = wb.create_sheet("Branch summary")
	bs.append(["Branch", "Active staff", "With Device ID", "Without", "On Fix list (not OK)", "Machine connected?"])
	for c in bs[1]:
		c.font, c.fill = Font(bold=True, color="FFFFFF"), head
	agg = defaultdict(lambda: [0, 0, 0])
	for e in emps:
		if e.status == "Active":
			a = agg[e.branch or "(blank)"]
			a[0] += 1
			a[1 if e.attendance_device_id else 2] += 1
	todo = defaultdict(int)
	for r in rows:
		if not r["problem"].startswith("OK") and r["status"] == "Active":
			todo[r["branch"] or "(blank)"] += 1
	for b, (n, w, wo) in sorted(agg.items(), key=lambda x: -x[1][0]):
		bs.append([b, n, w, wo, todo.get(b, 0), "Yes" if (None if b == "(blank)" else b) in connected else "No"])
	for i, w in enumerate([14, 12, 14, 10, 18, 18], 1):
		bs.column_dimensions[get_column_letter(i)].width = w

	wb.active = 1
	wb.save(out)


# ----------------------------------------------------------------------------- apply


def read_filled(path):
	from openpyxl import load_workbook

	wb = load_workbook(path, data_only=True)
	if SHEET not in wb.sheetnames:
		sys.exit(f"Sheet '{SHEET}' not found in {path}.")
	ws = wb[SHEET]
	header = [str(c.value or "").strip() for c in ws[FIRST_ROW - 1]]
	expected = [h for h, _k, _w in COLUMNS]
	if header[: len(expected)] != expected:
		sys.exit("The Fix list header was changed; use the workbook exactly as it was built.")
	out = []
	for row in ws.iter_rows(min_row=FIRST_ROW, values_only=True):
		r = {key: row[i] for i, (_h, key, _w) in enumerate(COLUMNS)}
		if not r["employee"]:
			continue
		for k in ("current_id", "sug_id", "new_id", "action"):
			v = r[k]
			if isinstance(v, float) and v.is_integer():
				v = int(v)
			r[k] = str(v).strip() if v not in (None, "") else ""
		out.append(r)
	return out


def plan(frappe, rows):
	"""-> list of dicts with verdict OK/ERROR/WARN/SKIP and the change to make."""
	emps = {
		e.name: e
		for e in frappe.db.sql("select name, employee_name, status, attendance_device_id from `tabEmployee` limit 1000000", as_dict=True)
	}
	holder = {(e.attendance_device_id or "").strip(): n for n, e in emps.items() if (e.attendance_device_id or "").strip()}
	seen = {
		r[0]: r[1]
		for r in frappe.db.sql("select device_user_id, count(*) from `tabBiometric Punch` group by device_user_id limit 1000000")
	}
	glob_enrol = defaultdict(list)  # id -> enabled global enrolments
	enrol_of = defaultdict(set)  # employee -> ids of their enabled global enrolments
	for r in frappe.db.sql(
		"select name, employee, device_user_id from `tabBiometric Enrolment` where ifnull(device,'')='' and enabled=1 limit 1000000",
		as_dict=True,
	):
		glob_enrol[(r.device_user_id or "").strip()].append(r)
		enrol_of[r.employee].add((r.device_user_id or "").strip())

	result = []
	for r in rows:
		res = dict(employee=r["employee"], name=r["employee_name"], action=r["action"], old=None, new=None, verdict="SKIP", note="")
		result.append(res)
		emp = emps.get(r["employee"])
		if not r["action"] or r["action"] in (ACTION_KEEP, ACTION_LATER):
			res["note"] = r["action"] or "no action picked"
			continue
		if r["action"] not in ACTIONS:
			res.update(verdict="ERROR", note=f"unknown action '{r['action']}'")
			continue
		if not emp:
			res.update(verdict="ERROR", note="employee not found")
			continue
		res["old"] = (emp.attendance_device_id or "").strip()
		if res["old"] != r["current_id"]:
			res.update(verdict="ERROR", note=f"ID changed since the workbook was built (now '{res['old'] or 'empty'}'); rebuild")
			continue
		if r["action"] == ACTION_CLEAR:
			if not res["old"]:
				res.update(note="already empty")
				continue
			res.update(verdict="OK", new="")
			continue
		new = r["sug_id"] if r["action"] == ACTION_SUGGESTED else r["new_id"]
		if not new:
			res.update(verdict="ERROR", note="no ID to use (" + ("no suggestion" if r["action"] == ACTION_SUGGESTED else "column Q empty") + ")")
			continue
		if PLACEHOLDER.match(new):
			res.update(verdict="ERROR", note=f"'{new}' looks like a placeholder, not a machine number")
			continue
		if new == res["old"]:
			res.update(note="same as current")
			continue
		res.update(verdict="OK", new=new)
		if not seen.get(new):
			res.update(verdict="WARN", note="no machine has ever sent this ID - check it")

	# uniqueness: inside the file, and against IDs that stay where they are
	# Every changed employee gives up their old ID and every global enrolment they
	# hold, so those IDs are free for somebody else in the same file.
	freed = set()
	for r in result:
		if r["verdict"] in ("OK", "WARN"):
			freed |= ({r["old"]} if r["old"] else set()) | (enrol_of.get(r["employee"], set()) - {r["new"]})
	taking = defaultdict(list)
	for r in result:
		if r["verdict"] in ("OK", "WARN") and r["new"]:
			taking[r["new"]].append(r)
	for new, rs in taking.items():
		if len(rs) > 1:
			for r in rs:
				r.update(verdict="ERROR", note=f"{new} is given to {len(rs)} employees in this file")
			continue
		r = rs[0]
		h = holder.get(new)
		if h and h != r["employee"] and new not in freed:
			r.update(verdict="ERROR", note=f"{new} is already on {h} ({emps[h].employee_name}); clear or change that one in the same file")
			continue
		other = [g for g in glob_enrol.get(new, []) if g.employee != r["employee"]]
		if other and new not in freed and not (h and h != r["employee"]):
			r.update(verdict="ERROR", note=f"{new} is mapped to {other[0].employee} in Biometric Enrolment {other[0].name}")
	return result


def _set_employee_id(frappe, employee, value):
	frappe.db.set_value("Employee", employee, "attendance_device_id", value or None)


def execute(frappe, result, user_label):
	"""Two passes so swaps (A<->B) work: free every old ID, then take the new ones."""
	todo = [r for r in result if r["verdict"] in ("OK", "WARN")]
	stamp = f"{datetime.now():%d-%m-%Y %H:%M}"

	# Pass 1: one employee = one global ID, so every other global enrolment they
	# hold is retired too — otherwise a stale one keeps outranking the new ID.
	# Device-specific enrolments are left alone.
	for r in todo:
		for g in frappe.get_all(
			"Biometric Enrolment",
			filters={"employee": r["employee"], "enabled": 1, "device": ["is", "not set"], "device_user_id": ["!=", r["new"] or ""]},
			pluck="name",
		):
			frappe.db.set_value("Biometric Enrolment", g, {"enabled": 0, "notes": f"Disabled by device ID workbook {stamp}"})
		_set_employee_id(frappe, r["employee"], None)

	from biometric_hub.biometric_hub.doctype.biometric_enrolment.biometric_enrolment import enrolment_key

	for r in todo:
		if r["new"]:
			_set_employee_id(frappe, r["employee"], r["new"])
			key = enrolment_key(r["new"])
			existing = frappe.db.get_value("Biometric Enrolment", {"enrolment_key": key}, "name")
			if existing:
				frappe.db.set_value(
					"Biometric Enrolment",
					existing,
					{"employee": r["employee"], "employee_name": r["name"], "enabled": 1, "notes": f"Set by device ID workbook {stamp}"},
				)
			else:
				frappe.get_doc(
					{
						"doctype": "Biometric Enrolment",
						"employee": r["employee"],
						"device_user_id": r["new"],
						"enabled": 1,
						"notes": f"Set by device ID workbook {stamp}",
					}
				).insert(ignore_permissions=True)
		text = f"Attendance Device ID changed from '{r['old'] or 'empty'}' to '{r['new'] or 'empty'}' via the Device ID clean-up workbook ({user_label}, {stamp})."
		frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Info", "reference_doctype": "Employee", "reference_name": r["employee"], "content": text}
		).insert(ignore_permissions=True)
	frappe.db.commit()

	from biometric_hub.engine.ingest import replay_unmapped

	for r in todo:
		if r["new"]:
			out = replay_unmapped(r["new"])
			frappe.db.commit()
			r["replayed"] = out
			r["note"] = (r["note"] + "; " if r["note"] else "") + f"{out['mapped']} old punches now mapped"


def write_result(path, result):
	from openpyxl import Workbook
	from openpyxl.styles import Font, PatternFill

	wb = Workbook()
	ws = wb.active
	ws.title = "Result"
	ws.append(["Employee", "Name", "Action", "Old ID", "New ID", "Verdict", "Note"])
	for c in ws[1]:
		c.font = Font(bold=True)
	fills = {"ERROR": "F8CBAD", "WARN": "FFE699", "OK": "C6EFCE", "SKIP": "EDEDED"}
	order = {"ERROR": 0, "WARN": 1, "OK": 2, "SKIP": 3}
	for r in sorted(result, key=lambda r: (order[r["verdict"]], r["employee"])):
		ws.append([r["employee"], r["name"], r["action"], r["old"], r["new"], r["verdict"], r["note"]])
		ws.cell(row=ws.max_row, column=6).fill = PatternFill("solid", fgColor=fills[r["verdict"]])
	for col, w in zip("ABCDEFG", (12, 26, 26, 12, 12, 9, 70)):
		ws.column_dimensions[col].width = w
	ws.auto_filter.ref = f"A1:G{ws.max_row}"
	wb.save(path)


def apply(site, path, do_apply, yes, out, allow_warn):
	rows = read_filled(path)
	frappe = _connect(site)
	try:
		result = plan(frappe, rows)
		n = defaultdict(int)
		for r in result:
			n[r["verdict"]] += 1
		print(f"{len(rows)} rows read from {path}: " + ", ".join(f"{k} {v}" for k, v in sorted(n.items())))
		for r in result:
			if r["verdict"] in ("ERROR", "WARN"):
				print(f"  {r['verdict']:<5} {r['employee']:<10} {r['note']}")
		if not do_apply:
			write_result(out, result)
			print(f"Dry run: nothing written. Result: {out}")
			return
		if n["ERROR"]:
			sys.exit("Errors above: fix them in the workbook (or blank the ACTION) and run again. Nothing written.")
		if n["WARN"] and not allow_warn:
			sys.exit("Warnings above: re-run with --allow-warn once HR has confirmed those IDs. Nothing written.")
		count = n["OK"] + n["WARN"]
		if not count:
			print("Nothing to change.")
			return
		if not yes and input(f"Change {count} employees on {site}? Type YES: ").strip() != "YES":
			print("Cancelled.")
			return
		execute(frappe, result, os.path.basename(path))
		write_result(out, result)
		print(f"Done: {count} employees changed. Result: {out}")
	finally:
		frappe.destroy()


def main():
	p = argparse.ArgumentParser(prog="device_id_workbook")
	sub = p.add_subparsers(dest="cmd", required=True)
	b = sub.add_parser("build", help="read-only: write the Fix-list workbook")
	b.add_argument("--site", required=True)
	b.add_argument("--out", default="device-id-fix-list.xlsx")
	b.add_argument("--names", nargs="*", help="people CSV / backfill report xlsx with names from the attendance software")
	a = sub.add_parser("apply", help="check (default) or apply a filled workbook")
	a.add_argument("--site", required=True)
	a.add_argument("--in", dest="path", required=True)
	a.add_argument("--out", default=None)
	a.add_argument("--apply", action="store_true")
	a.add_argument("--allow-warn", action="store_true", help="also apply rows whose ID no machine has sent yet")
	a.add_argument("--yes", action="store_true")
	x = p.parse_args()
	if x.cmd == "build":
		build(x.site, x.out, x.names)
	else:
		out = x.out or re.sub(r"\.xlsx$", "", x.path) + ("-APPLIED" if x.apply else "-CHECK") + ".xlsx"
		apply(x.site, x.path, x.apply, x.yes, out, x.allow_warn)


if __name__ == "__main__":
	main()
