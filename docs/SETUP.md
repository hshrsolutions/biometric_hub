# Biometric Hub — setting up a site from scratch

The ordered procedure, start to finish. Follow it top to bottom for a new
customer; skip to the section you need for a change to a running site.

This is the *deployment* document. [`CUSTOMER.md`](CUSTOMER.md) is the one to
hand to whoever will administer the site day to day, and
[`SPEC.md`](SPEC.md) is the contract for anyone changing the code.

Every command below is run from the bench directory as the `frappe` user.

---

## 0. Before you start

Have these answers first. Two of them decide everything that follows, and
getting them wrong means redoing sections 4 to 7.

| Question | Why it matters |
|---|---|
| **What hardware is on site?** Make, model, and whether the readers are on the office LAN | Decides the driver in §2 |
| **Can the GroHR server reach the readers?** Almost always no, if GroHR is hosted and the readers are in an office | Decides the connectivity mode in §2 |
| Is there a PC in the office that stays on? | Needed for the connector agent |
| Does HRMS have **Allow Geolocation Tracking** on? | If yes, every device needs coordinates or all its punches fail |
| Which shifts does each reader feed? | Needed for auto-attendance to advance (§5) |

The site needs `hrms` installed. `erpnext` comes with it.

---

## 1. Install

```bash
bench get-app <repo-url>
bench --site <site> install-app biometric_hub
```

That is the whole installation. It creates the doctypes, both roles, the four
drivers, the settings with safe defaults, the workspace, the workbench page and
the three reports — and, if employees already have **Attendance Device ID**
filled in, a `Biometric Enrolment` for each of them, so existing mappings carry
straight over.

Verify:

```bash
bench --site <site> execute biometric_hub.tests.selftest.run
```

On a site with no employees yet it checks the schema and says plainly that the
rest cannot run. That is the expected output at this stage.

---

## 2. Decide how punches will arrive

This is the decision the rest of the setup hangs on.

| Situation | Connectivity Mode | Driver |
|---|---|---|
| Readers on the office LAN, GroHR hosted elsewhere | **Via Connector** | `ZKTeco` |
| Punches only exist inside the attendance software's **MySQL / MariaDB** or **SQL Server** database (eTimeTrack, BioTime, ZKTime, …) | **Via Connector** | `SQL Database` |
| Punches only exist inside eSSL Time Pro's database (SQL Server) | **Via Connector** | `WDMS` (same reader, SQL Server by default) |
| Reader can reach the internet and supports ADMS push | **Push** | `ZKTeco Push (ADMS)` |
| GroHR is on the same network as the readers | **Direct Poll** | `ZKTeco` |
| Proving the pipe before hardware arrives | any | `Fake` |

**Via Connector is the usual answer.** Direct Poll is rare and only works when
the server can genuinely open port 4370 to the reader.

`Direct Poll` also needs `pyzk` in the bench environment:

```bash
./env/bin/pip install pyzk
```

Not needed for Via Connector — the agent carries its own copy.

**Database sources (`SQL Database` / `WDMS`).** The server never holds the
database login. Register one `Biometric Device` per reader serial (the agent's
`--probe` lists every serial found in the table), and put the host, database,
login and table/column names in the agent's own `config.json` under `sources`
— see the agent's README. Give the agent a login with **SELECT on that one
table only**; it never writes, and on MySQL the session is also set READ ONLY.

**Device IDs typed on the Employee form count.** Setting
`Employee.attendance_device_id` creates the matching global `Biometric
Enrolment` and replays every punch parked as Unmapped for that ID — the same
effect as mapping it in the workbench.

---

## 3. Biometric Settings

**Biometric Hub → Biometric Settings.** The defaults are deliberately
conservative and most sites change nothing. Two matter during setup:

- **Enable Ingestion** — on. The master switch.
- **Create Employee Checkin** — **turn this OFF** if you are replacing an
  existing integration. Punches will stage and be matched, but nothing is
  written into HRMS, so both paths can run side by side. Turn it on at cutover.
  For a site with no existing integration, leave it on.

The rest — log type policy, clock skew, retention, reconcile, alerts — can be
left alone until something calls for them.

---

## 4. Add the devices

**Biometric Hub → Biometric Device**, one per physical reader.

| Field | Notes |
|---|---|
| **Device Name** | How it appears everywhere. "Factory Gate", not "Device 1" |
| **Serial Number** | Exactly as the device reports it. In Push mode this is the *only* way it identifies itself, and a wrong one means every punch is refused |
| **Driver** / **Connectivity Mode** | From §2 |
| **IP Address / Port / Communication Key** | Direct Poll and Via Connector only |
| **Location** | Fill it in. You will want it the day one goes quiet |
| **Fixed Log Type** | Set **only** on a reader that is physically entry-only or exit-only. It overrides whatever the device reports, which is useful because many readers report direction unreliably |
| **Ignore Punches Before** | Set to today when adding an existing reader. Without it, its entire stored history floods in on first connection |
| **Latitude / Longitude** | Required if HRMS has **Allow Geolocation Tracking** on. Without them every punch from this reader becomes an error — the error message says so, but it is easier to set them now |
| **Heartbeat Grace (minutes)** | Default 60. How long silence is acceptable before it is reported. 0 disables alerting for this device |
| **Alert Recipients** | Comma-separated. Fill this in — it is the difference between noticing a dead reader today and noticing it at payroll |

Then **Test Connection** on the device form. For Direct Poll it reaches the
reader; for Via Connector it tells you whether the agent has checked in; for
Push it tells you whether the device has ever connected.

---

## 5. Tell each device which shifts it feeds

On the device, under **Shifts Fed by This Device**, fill in **Shift Types**.

This is what lets the server advance `Shift Type.last_sync_of_checkin`, which is
what HRMS uses as its watermark for auto-attendance. A shift only moves forward
once **every** enabled device feeding it has reported, so a half-synced day is
never marked.

Leave it empty and punches still become checkins — but auto-attendance will not
advance on its own, and someone will eventually ask why.

---

## 6. Create the integration user and the connector

Skip this section for Direct Poll.

**a. The user.** Create a new User, e.g. `biometric@customer.example`.
Give it **exactly one role: `Biometric Integration`**. Nothing else.

That account can create attendance punches and checkins and do nothing else — it
cannot read Employee, cannot write Shift Type, cannot see a salary slip. This is
the point. If anyone proposes using an Administrator key instead, the answer is
no: this credential is going to sit in a file on a PC in an office.

**b. The keys.** Open the user → **API Access** → **Generate Keys**. Copy the
secret now; it is shown once.

**c. The connector.** **Biometric Hub → Biometric Connector**, one per
*premises*, not per device:

- **Agent Name** — e.g. "Head Office Agent"
- **API User** — the user above
- **Site / Customer Location** — where it runs

Then go back to each device from §4 and set its **Connector**.

---

## 7. Install the agent

Only for Via Connector. The agent is a separate program with its own repository
and its own licence — see `INSTALL-WINDOWS.md` in the agent package for the full
runbook. In short, on the office PC:

```
copy config.example.json config.json
```

Fill in `site_url`, `api_key`, `api_secret` and `connector`, lock the file down,
then prove it before installing anything:

```
python -m grohr_agent --once --verbose
```

You want to see the bootstrap listing your devices, punches read, and the server
accepting them. Then install it as a service so it survives logout and reboots:

```
python agent_win.py install
net start GroHRBiometricAgent
```

Confirm from the GroHR side, not just the PC: the `Biometric Connector` should
now show a recent **Last Check-in** and an **Agent Version**.

---

## 8. Map people to device IDs

**Biometric Hub → Biometric Enrolment.** This is the part that takes real time,
and it is the part every attendance complaint eventually traces back to.

A reader does not know who anyone is. It knows a **device user ID** — the number
typed into the reader when the person was enrolled on it. That number has to be
matched to an employee.

- Leave **Device** blank in almost every case. The ID then works on every reader.
- Set it only when the same number means different people on different readers.
- **Valid From / Valid Upto** matter when an ID is re-issued to a new joiner —
  without them, that person inherits the previous holder's punch history.

You do not have to do this up front. Section 9 gives you the list, in priority
order, of the people who are actually punching.

---

## 9. First punches, and checking them

Let punches arrive for an hour, then open **Biometric Hub → Workbench**.

- **Devices** — everything should be `Online`. `Silent` means it has not been
  heard from inside its grace window.
- **Unmapped punches** — every device ID with no employee behind it, with counts.
  **This list is your mapping work, sorted by how much it matters.** Press
  **Map to employee** on a row and every punch already parked behind that ID is
  replayed, so past days are repaired, not just future ones.
- **Last 24 hours** — mapped, unmapped, duplicates, errors.

Then the reports:

- **Employees Without Biometric Enrolment** — people no reader can identify
- **Biometric Punch Outcomes** — by day and device
- **Biometric Device Uptime** — last contact per reader

Work the unmapped queue to empty. When it is, spot-check that a real employee's
checkins look right in HRMS for a full day.

---

## 10. Push-mode devices only: the nginx rule

ADMS readers connect to fixed `/iclock/*` paths and cannot be told to use a
different URL shape, so they need an nginx rule.

`nginx/biometric-hub-iclock.conf` in the app is that rule. It is **additive** and
belongs in `/etc/nginx/conf.d/`.

Three things, in order:

1. Fill in the `allow` / `deny` block with the customer's network. The ADMS
   protocol authenticates with nothing but a serial number in a query string, so
   this is the only real access control there is.
2. Point `proxy_pass` at this bench's `webserver_port`.
3. `nginx -t`, then reload — **never `bench setup nginx`**, which regenerates the
   whole configuration and would destroy the existing SSL blocks for every other
   site on the server.

Then point each reader at `https://<site>/iclock/cdata` in its own admin
interface. An unregistered serial is refused with a 401 and recorded in
**Biometric Sync Log**, so a reader nobody added shows up rather than silently
doing nothing.

---

## 11. Scheduler and alerts

The hourly job marks quiet devices `Silent` and emails their alert recipients —
once, on the transition, not every hour. The daily job reconciles staged punches
and purges old ones.

Neither runs if the site's scheduler is off:

```bash
bench --site <site> enable-scheduler
bench --site <site> doctor
```

Also check outgoing email actually works on the site, or the alerts are theatre.

---

## 12. Go-live checklist

- [ ] `selftest` passes
- [ ] Every reader is a `Biometric Device`, with the right serial
- [ ] Every device has **Location** and **Alert Recipients**
- [ ] Coordinates set, if geolocation tracking is on
- [ ] **Ignore Punches Before** set on any pre-existing reader
- [ ] **Shift Types** filled in on each device
- [ ] The integration user holds **only** `Biometric Integration`
- [ ] The agent runs as a service, and **Last Check-in** is updating
- [ ] **Unmapped punches** is empty, or every remaining row is explained
- [ ] **Employees Without Biometric Enrolment** reviewed — the rest genuinely do not use a reader
- [ ] Scheduler on, email working
- [ ] **Create Employee Checkin** is ON
- [ ] A full day of attendance checked against reality
- [ ] The customer has `CUSTOMER.md`, and knows the workbench is the screen to check

---

## Seeing it filled in first

To look at a working setup before touching a customer's site — no hardware
needed:

```bash
bench --site <site> execute biometric_hub.tests.seed.run
# open /app/biometric-workbench
bench --site <site> execute biometric_hub.tests.seed.clear
```

That creates a device, a connector, mapped punches, an unmapped queue and a
punch from an unregistered reader, so every part of the workbench has something
real in it.

---

## The five-minute version

For a site with LAN readers and a hosted GroHR:

1. `bench --site <site> install-app biometric_hub`
2. **Biometric Device** per reader — serial, location, driver `ZKTeco`, mode
   **Via Connector**, coordinates, **Ignore Punches Before** = today
3. A user with only `Biometric Integration`; generate API keys
4. **Biometric Connector** for the office; set it on each device
5. Install the agent on an office PC; `--once` to prove it; install the service
6. Watch the workbench and map the unmapped IDs
7. `enable-scheduler`

---

## Backfill or audit from an export (one-off)

For a first cutover, or a gap left by an old feed. Two scripts, both one-off:

1. **Customer PC, read-only on the attendance database** (agent repo):
   `python tools/export_punches.py --from 2026-03-01 --to 2026-09-29 --out C:\GroHR\export`
   uses the agent's own `config.json` `sources` and writes `punches-*.csv`,
   `summary-*.csv` and `people.csv` (identity columns only: code + name).
2. **Server** (from `<bench>/sites`, by path — works even if the installed app is older):
   `../env/bin/python <path>/biometric_hub/tools/backfill_csv.py --site SITE --csv punches.csv --people people.csv --out report.xlsx`
   Read-only. Every punch lands in one bucket: `in_grohr` (a checkin already exists,
   exact or within `--near` seconds — e.g. the old feed's copy), `staged` (already a
   Biometric Punch), `to_import`, `to_import_unmapped`, `before_cutoff`,
   `unknown_device`; plus `only_in_grohr` checkins with no punch in the software.
   Sheets: By day, Unmapped IDs (with the software's names), Attendance already
   marked, Only in GroHR.
3. Add `--apply` (optionally `--from/--to`) to import only the two `to_import`
   buckets through `engine.ingest` (source `Manual`). Re-running is safe (punch key);
   device `last_punch_time` is put back so the live agent's cursor is not rewound.
   Unmapped rows turn into checkins when HR sets the device ID.

After an import, attendance already submitted for those days does not change by
itself — see the "Attendance already marked" sheet.

## Maintenance

```bash
bench --site <site> execute biometric_hub.tests.selftest.run        # everything
bench --site <site> execute biometric_hub.engine.health.reconcile   # retry staged punches
bench --site <site> execute biometric_hub.engine.health.purge       # retention
bench --site <site> execute biometric_hub.drivers.registry.sync     # after adding a driver app
bench --site <site> execute biometric_hub.setup.scaffold.run        # idempotent repair
```

`scaffold.run` is safe to re-run at any time: it adds anything missing, and
leaves settings and existing records alone.

## Removing it

```bash
bench --site <site> uninstall-app biometric_hub
```

`Employee Checkin` records already created stay — they belong to HRMS. So does
`Employee.attendance_device_id`, which this app kept in step precisely so that
removing it leaves the site working the way it was found.
