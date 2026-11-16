# Employee → Biometric IDs

Added 2026-10-03. One employee can have **several** attendance-machine IDs; every one
of them counts toward that employee's attendance; **no ID can belong to two employees**.

## For HR

Employee form → **Biometric IDs** table (just below Attendance Device ID).

| Column | Meaning |
|---|---|
| Biometric ID | The user number the machine has for this person |
| Only on Device | Blank = counts on every machine (normal). Set only when this number means this person on that one machine |
| Enrolment | Filled in by GroHR |

- Add a row, save → punches on that ID count for this person, including punches
  already received and parked as unmapped (they are repaired automatically).
- Remove a row, save → that ID no longer counts for this person.
- An ID that already belongs to someone else is refused on save, naming who has it.
- The **Attendance Device ID** field is the first all-machines ID in the table.
  Typing a new one there adds it to the table.
- IDs linked anywhere else (workbench, photo confirm) appear here automatically.
  Per-machine IDs set in **Device Access** stay on that screen but still count for uniqueness.

## Technical

- Custom field `Employee.bh_biometric_ids` (Table → `Biometric Employee ID`); shipped by
  `setup/scaffold.py` (fresh install) and `fixtures/custom_field.json` (migrate).
- `engine/employee_ids.py`: `validate` (Employee.validate — normalise, primary-ID rule,
  uniqueness), `sync` (Employee.on_update — enrolments match the table, replay
  Unmapped), `on_enrolment_insert/trash` (enrolments made elsewhere ↔ rows), `backfill`.
- Writes only our child rows and `Biometric Enrolment`; `attendance_device_id` is set on
  HR's own save (validate), not in the background.
- Tests: `tests/test_employee_ids.py` (19).
