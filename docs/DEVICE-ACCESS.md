# Employee device access

Added 2026-10-02. Lets HR decide, per employee, **which attendance machines they
may punch on** and **whether they may check in from the mobile app** — and lets
one employee use several machines, each with its own ID if needed.

## What it does

| Channel | Rule HR sets | What happens to a punch outside the rule |
|---|---|---|
| Biometric machines | **Any Device** (default) or **Allowed Devices Only** + a list | Kept as a **Blocked** punch — no checkin, nothing lost. Adding the machine later turns it into a checkin automatically |
| Mobile app (`/hrms`) or desk self check-in | **Allow Mobile App Check-in** on/off | The employee sees *"Mobile check-in is turned off for you…"* and no checkin is saved |

An employee with **no** access record is unrestricted, exactly as before. So
installing this changes nothing until HR starts using it.

HR recording a checkin **for** someone (desk, data import) is never blocked —
only a person checking *themselves* in is governed by the mobile switch.

## How HR uses it

1. Open the employee → **Attendance → Device Access** (or *Biometric Hub →
   Employee Device Access → New*).
2. **Biometric Devices**:
   - *Any Device* — punch anywhere (default).
   - *Allowed Devices Only* — only on the machines listed below.
3. **Allowed Devices** table — one row per machine:
   - **Device** — the machine.
   - **ID on This Device** — leave blank if the machine knows them by their
     usual ID (shown at the top as *Usual Device ID*). Fill it only when that
     machine has them under a different number — e.g. branch machine uses
     `VNR0012` while head office uses `1434`. The app creates the matching
     device-specific enrolment for you and removes it if you clear the row.
4. **Allow Mobile App Check-in** — untick to force machine-only attendance.
5. Save. Any punches already held as Blocked that the new rules allow are
   released into checkins within a minute (background job).

The form also shows **All IDs on File** (every enrolment for the person) and
**Punches, last 30 days** by machine and outcome, so HR can see what the person
actually uses before restricting them.

Common set-ups:

| Need | Set |
|---|---|
| Works at one branch only | Allowed Devices Only + that branch's machine(s) |
| Moves between head office and branches | Allowed Devices Only + every machine they use (add an ID per machine if they differ) |
| Field staff, mobile only | Allowed Devices Only, empty table, mobile **on** |
| Machine only, no phone check-in | Any Device, mobile **off** |

**Bulk:** *Employee Device Access* supports Data Import (parent + Allowed
Devices child rows) for setting up many people at once.

## Seeing what was blocked

- **Workbench → Last 24 hours** has a *Blocked* tile.
- *Biometric Punch* list, filter **Status = Blocked** — each punch says who,
  which machine, and which machines they are allowed on.
- **Blocked Punches** button on the access form.
- *Biometric Punch Outcomes* report has a *Blocked* column.

Blocked punches are never auto-purged (only Mapped/Duplicate are).

## Technical notes

- `engine/access.py` holds every rule. `engine/ingest.py` calls
  `check_device()` after the employee is mapped (step 4b); a refusal finishes
  the punch as `Blocked` with the employee recorded.
- `ingest.replay_blocked(employee)` re-runs that employee's Blocked punches; it
  is queued after every save and delete of an access record.
  `ingest.reprocess()` now locks the punch row and skips a punch already
  `Mapped`, so two concurrent replays never write two checkins.
- Mobile switch: `doc_events` `Employee Checkin.before_insert` →
  `access.check_self_service_checkin`. Applies only when the session user is the
  employee's own `user_id`; skipped for the engine (`flags.biometric_hub`),
  imports, install/migrate/patch, Administrator, and anyone acting for someone
  else. It reads; it never writes the checkin.
- Per-device IDs become `Biometric Enrolment` rows (device set, notes
  *"Managed by Employee Device Access …"*). Only those managed rows are ever
  removed by the access record; the global enrolment and workbench-made ones are
  untouched. An ID already held by another employee on that machine is refused.
- Permissions: HR Manager has full access (incl. import); Biometric Manager and
  System Manager too. The `Biometric Integration` agent account has none.
- Schema ships both ways: `setup/scaffold.py` (fresh install, incl. the new
  `Blocked` status option via `ensure_select_options`) and exported doctype JSON
  (migrate).
- Tests: `tests/test_access.py` (31 checks), part of `selftest`.
