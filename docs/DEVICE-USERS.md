# Device users and photos — "who is this machine ID?"

Added 2026-10-03. Every user the attendance software (eSSL Easy Time Pro / BioTime)
knows, with their photo, so HR can confirm which GroHR employee each machine ID is.

## Getting the data (office PC, read-only)

Run on the PC where the agent is installed (it has the database settings):

```
python tools\export_photos.py --probe
python tools\export_photos.py --photo-root "C:\<attendance software install folder>" --out C:\GroHR\export
```

`--probe` first: it shows where photos are kept in that version (a column with a
file path, or the image itself). The export writes `photos-<date>.zip`. Also export
the employee list from the attendance software (Employee → Export → Excel).
Photos are personal data: hand the zip over directly, do not e-mail it around.

## Importing (GroHR desk)

**Biometric Device User → Import from attendance software**: attach the employee
export and, optionally, the photo zip → **Check** (dry run, shows counts) → confirm.
Re-importing updates; it never undoes an HR decision. Photos are stored as private
files attached to each record.

Each user gets a status:

| Status | Meaning |
|---|---|
| Confirmed | Linked in GroHR already, or HR confirmed |
| Suggested | A name match — HR must look at the photo and confirm |
| Unchecked | No match — HR picks the employee |
| Not This Person | HR rejected the suggestion |

## Confirming (HR)

Open a user: machine photo and GroHR photo side by side. Choose the **Employee**,
click **Confirm: this is the employee**. GroHR links the ID (global enrolment) and
replays every punch parked as Unmapped behind it — past attendance is corrected.
**Not this person** clears a wrong suggestion. Image view (`/app/biometric-device-user/view/image`)
shows everyone as photo cards.

If the ID is already linked to someone else, Confirm refuses: when two people
really use the same number on different machines, link it per machine in
**Employee Device Access** instead.

## Technical

- `engine/device_users.py` — read export (header row auto-detected; xlsx/csv),
  read photo zip (manifest or file names), suggest (name ≥ 85 % and clearly ahead
  of the next), import (dry run by default), confirm, reject.
- Doctype `Biometric Device User` (named by device user ID, `image_field = photo`);
  HR Manager / Biometric Manager / System Manager.
- Agent: `tools/export_photos.py` (SELECT only; reads blob, base64 or file path).
- Tests: `tests/test_device_users.py` (22), part of `selftest`.

## Automatic: change an ID → the office PC sends the profile (agent 0.3.0+)

Switch on **Biometric Settings → Fetch Profiles from Connector**. From then on,
whenever a device ID is set or changed (Employee form, Biometric IDs table,
enrolment, Device Access, workbench), GroHR marks that ID **Requested**. The
agent's next check-in reply lists it; the agent reads the name and photo from the
attendance software (read-only) and posts them to `biometric_hub.api.ingest.profiles`.
The Employee form shows them under **On the attendance machine**
(Waiting for office PC → From machine / Not in attendance software). Buttons:
**Attendance → Fetch machine photo** (Employee) and **Fetch from office PC**
(Device User). Older agents ignore the request; nothing breaks.
Agent settings: optional `profile` block in `config.json` (table/columns/photo_roots).
Test: `tests/test_agent_profiles_e2e.py` (real agent process, 10 checks).
