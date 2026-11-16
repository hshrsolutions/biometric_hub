# Attendance Device ID clean-up workbook

Tool: `biometric_hub/tools/device_id_workbook.py` (added 2026-10-02). Fixes wrong /
missing `Employee.attendance_device_id` in bulk through one Excel file that HR fills.

## Why not just edit the Employee form or use Data Import?
- A global `Biometric Enrolment` outranks `Employee.attendance_device_id`; changing the
  Employee field alone leaves the old enrolment winning.
- Old punches parked as *Unmapped* under the correct ID are only re-processed if the
  enrolment changes and `replay_unmapped` runs.
- Employee `save()` re-runs every validation (mandatory fields, other apps' hooks); one
  unrelated broken rule blocks the whole clean-up.

## Steps (run from the bench's `sites` folder as the bench user)
1. **Build (read-only)**
   `../env/bin/python <apps>/biometric_hub/biometric_hub/tools/device_id_workbook.py build --site SITE --out ../docs/device-id-fix-list.xlsx [--names people.csv|backfill-report.xlsx]`
   With no `--names`, any `../docs/*backfill-report*.xlsx` is used for machine names.
2. **HR fills** sheet *Fix list*: ACTION (P) and, if needed, NEW Device ID (Q). Sheets:
   *Read me*, *Fix list* (most urgent first), *Machine IDs with no employee*, *Branch summary*.
3. **Check (dry run)**: `... apply --site SITE --in filled.xlsx` → `filled-CHECK.xlsx`
   (ERROR / WARN / OK / SKIP per row). Nothing is written.
4. **Apply**: `... apply --site SITE --in filled.xlsx --apply [--allow-warn]` → refuses on
   any ERROR; WARN (ID never sent by a machine) needs `--allow-warn`.

## What apply does
Pass 1 for every changed employee: disables all their enabled *global* enrolments
(device-specific ones untouched) and empties the field — so swaps A↔B work. Pass 2:
sets the new ID (`db.set_value`), enables/creates the global enrolment, adds an Info
Comment on the Employee, commits, then `replay_unmapped(new_id)`.

Checks: action known; ID unchanged since build; no placeholder (letters only / "test");
one ID per employee in the file; ID not held by an employee or enrolment that stays.

Not done: attendance older than the `cmenu` 18-day recalculation window stays as it is.

## Tests
Seeded end-to-end test (build → fill → check → apply → verify, rolled back) 13/13 on
demo 2026-10-02: name suggestion, wrong-ID suggestion, placeholder and duplicate refusal,
A↔B swap, old enrolment disabled, old punches mapped, audit comment.
