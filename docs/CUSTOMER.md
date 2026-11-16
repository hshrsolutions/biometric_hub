# Biometric attendance — setting it up

For whoever administers a GroHR site. It assumes no knowledge of the app.

## What this does

Attendance readers produce punches. This turns those punches into **Employee
Checkins** in GroHR, which HRMS then uses to mark attendance against shifts.

The one thing worth understanding before you start: a reader does not know who
anybody is. It knows a **device user ID** — a number, typed into the reader when
the person was enrolled on it. Somewhere, that number has to be matched to an
employee. That matching is the whole job, and almost every attendance problem
traces back to it.

## 1. Decide how punches will reach the site

| Your situation | Mode |
|---|---|
| Readers on the office LAN, GroHR is in the cloud | **Via Connector** — a small agent on an office PC |
| Reader can reach the internet and supports push | **Push** |
| GroHR is on the same network as the readers | **Direct Poll** |
| Punches only exist in eSSL Time Pro's database | **Via Connector**, driver `WDMS` |

Via Connector is the common answer.

## 2. Add the devices

**Biometric Hub → Biometric Device**, one per reader.

- **Serial Number** — as printed on the device. This is how it identifies itself;
  get it wrong and its punches are refused.
- **Location** — where it is. You will thank yourself when one goes quiet.
- **Fixed Log Type** — set this **only** on a reader that is physically entry-only
  or exit-only. It overrides whatever the device reports, which is useful because
  some readers report direction unreliably.
- **Ignore Punches Before** — set it to today when adding an existing reader,
  otherwise its stored history floods in on first connection.
- **Latitude / Longitude** — required if **Allow Geolocation Tracking** is on in
  HR Settings. Without them, that reader's punches will all be rejected.
- **Heartbeat Grace** — how long silence is acceptable. Default 60 minutes.
- **Alert Recipients** — who gets told when it goes quiet. Fill this in.

## 3. Install the agent (Via Connector only)

Create a **Biometric Connector** for the office, and a user holding only the
**Biometric Integration** role. Generate an API key and secret for that user.

That user can create attendance punches and nothing else. If anyone suggests
using an Administrator key instead, the answer is no — that credential lives on
a PC in an office, and it should be able to do exactly one thing.

Then follow `INSTALL-WINDOWS.md` in the agent package.

## 4. Map people to device IDs

**Biometric Hub → Biometric Enrolment.** One row per person per ID.

- Leave **Device** blank in almost all cases — the ID then works on every reader.
- Set it only when the same number means different people on different readers.
- **Valid From / Valid Upto** matter when an ID is re-issued to a new joiner.
  Without them, that person inherits the previous holder's history.

If your employees already have **Attendance Device ID** filled in, installing the
app created these rows for you.

## 4b. Several IDs for one person

**Employee → Biometric IDs.** List every machine number the person uses; all of them
count toward their attendance. An ID already used by someone else is refused.
Full guide: `EMPLOYEE-IDS.md`.

## 4a. Choose which machines each person may use (optional)

**Employee → Attendance → Device Access.** Per employee, HR can:

- restrict them to named machines (*Allowed Devices Only*) — a punch anywhere
  else is held as **Blocked**, not lost, and goes through as soon as you add
  that machine;
- give them a different ID on a particular machine;
- switch **mobile app check-in** off, so they must use a machine.

No record means no restriction. Full guide: `DEVICE-ACCESS.md`.

## 5. Watch it

**Biometric Hub → Workbench** is the screen to check.

- **Devices** — anything not `Online` needs attention. `Silent` means it has not
  been heard from within its grace window.
- **Unmapped punches** — real people, punching, who match no employee. Each row
  has a **Map to employee** button; using it also repairs every punch already
  parked behind that ID, so past days are corrected too.
- **Last 24 hours** — mapped, unmapped, blocked, duplicates, errors.

Three reports: *Employees Without Biometric Enrolment* (people no reader can
identify), *Biometric Punch Outcomes*, *Biometric Device Uptime*.

## 6. Things that go wrong, and what they mean

| Symptom | Cause |
|---|---|
| Someone is never marked present | Their device ID is unmapped. It will be in the workbench queue |
| A whole reader stops | It shows as `Silent`. Check power, network, and whether the agent is running |
| Punches arrive, attendance does not | Attendance also needs a **Shift Assignment**. That is HRMS, not this app |
| Everyone shows only one punch a day | The reader is not reporting direction. Set **Fixed Log Type**, or use two readers |
| A device's punches are all errors | Usually missing coordinates with geolocation tracking on. The error message says so |
| A punch is in the future | The reader's clock is wrong |
| Punches show as **Blocked** | HR restricted that person to other machines. Add the machine in their Device Access |
| "Mobile check-in is turned off for you" | HR switched the mobile app off in that person's Device Access |

Nothing is ever thrown away. A punch that cannot be matched is kept and shown,
not discarded — so no attendance problem here is ever unrecoverable, and no
question about "what happened on the 14th" is unanswerable.

## 7. Demonstration data

```bash
bench --site <site> execute biometric_hub.tests.seed.run
bench --site <site> execute biometric_hub.tests.seed.clear
```
