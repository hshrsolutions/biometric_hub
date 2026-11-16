# Biometric Hub — Specification

App-scoped contract, seeded from `biometric-hub-prd.md` at Phase 0 and owned by
this repo from here on. The bench-level PRD holds the phase plan and the
rationale; this file holds what the app *is*.

App: `biometric_hub` · Module: `Biometric Hub` · Prefix: `Biometric `
Requires: `frappe`, `hrms` · Licence: MIT

## 1. Scope and isolation (binding)

Biometric Hub moves punches from attendance hardware into HRMS
`Employee Checkin`, and owns everything in between: staging, mapping,
idempotency, health and audit.

- **The `engine/` package is the only place that may write to an HRMS
  document**, and there are exactly three such writes: `ingest.py` creates an
  `Employee Checkin` and advances `Shift Type.last_sync_of_checkin`;
  `enrolment.py` mirrors a global enrolment onto
  `Employee.attendance_device_id`. Not the API layer, not a driver, not a hook.
- HRMS and ERPNext are touched **only** via (1) a Link field on a `Biometric `
  doctype, (2) a `Custom Field` shipped as a fixture, (3) a `doc_events` hook,
  (4) `permission_query_conditions` / `has_permission`. Never patch an HRMS
  controller, edit core doctype JSON, or monkeypatch.
- **A biometric problem must never block an HR document.** Park the row, write a
  `Biometric Sync Log`, carry on.
- **No punch is ever silently dropped.** Every punch is persisted as a
  `Biometric Punch` before mapping is attempted.
- **A permanent rejection is never retried on the wire.** Unmapped, duplicate and
  unknown-device are terminal states resolved by a human.
- The one HRMS `doc_events` validation hook, `Employee Checkin.before_insert`
  (`engine/access.py`), only refuses an employee's own self check-in when HR
  has turned the mobile app off for them. It reads; it never writes.
- **Least privilege.** `Biometric Integration` holds `Employee Checkin: create`
  and `Biometric Punch: create` and nothing else — in particular **not**
  `Shift Type: write`.

## 2. Layout

```
biometric_hub/
├── biometric_hub/doctype/      # the seven doctypes
├── drivers/                    # BaseDriver + per-vendor drivers
├── engine/
│   ├── ingest.py               # the only Employee Checkin writer
│   ├── access.py               # employee device access rules (no HRMS writes)
│   └── health.py               # heartbeat, silent-device alerts, reconcile
├── api/
│   ├── ingest.py               # push / heartbeat / bootstrap
│   └── admin.py                # desk actions
├── setup/scaffold.py           # idempotent fresh-install path
└── tests/{selftest,seed}.py
```

## 3. Doctypes

| Doctype | Role |
|---|---|
| `Biometric Driver` | Registry of vendor drivers, synced from the `biometric_drivers` hook on migrate. Not hand-edited. |
| `Biometric Device` | One physical reader: driver, connectivity mode, connection, punch handling, geolocation, health. |
| `Biometric Connector` | One registered on-prem agent: its API user, version and last check-in. |
| `Biometric Enrolment` | `device_user_id` ↔ `Employee`, optionally per device, with validity dates. Supersedes `Employee.attendance_device_id` while keeping it in sync. |
| `Biometric Device Shift` | Child table on the device: which Shift Types it feeds, so the server can advance `last_sync_of_checkin` once every device in a group has reported. |
| `Biometric Punch` | Raw staging + audit trail. `punch_key` — a derived hash of (`device_serial`, `device_user_id`, `punch_time`) with a `UNIQUE` index — is the idempotency key. |
| `Biometric Sync Log` | One row per batch or run, with counts. |
| `Biometric Settings` | Single. Global switches, all conservative by default. |
| `Biometric Employee Access` | One per employee (named by employee), owned by HR: `access_mode` (*Any Device* / *Allowed Devices Only*), `allow_mobile_checkin`, and the allowed-devices table. No record = unrestricted. See `docs/DEVICE-ACCESS.md`. |
| `Biometric Access Device` | Child of the above: one allowed device, with an optional per-device ID that is kept in step as a device-specific `Biometric Enrolment`. |
| `Biometric Device User` | One user of the attendance software (named by device user ID) with photo, areas and HR's match decision. Confirm creates the enrolment and replays Unmapped punches. See `docs/DEVICE-USERS.md`. |
| `Biometric Employee ID` | Child table on Employee (custom field `bh_biometric_ids`): every biometric ID for the employee, unique across employees; kept in step with `Biometric Enrolment`. See `docs/EMPLOYEE-IDS.md`. |
| `Biometric User Sync` | One request to create/update or remove an employee in eSSL Easy Time Pro via the agent, with its outcome (Queued → Sent → Done / Dry Run / Conflict / Skipped / Error). Written only by `engine/user_push.py`. See `docs/EASYTIME-PUSH.md`. |
| `Biometric Area Rule` | Child of Biometric Settings: GroHR Branch/Department/Company value → Easy Time Pro area code(s) + department code. |

`Biometric Punch.status`: `Pending` → `Mapped` | `Unmapped` | `Blocked` |
`Duplicate` | `Ignored` | `Error`. `Unmapped`, `Blocked` and `Error` are the
work queue.

## 4. Resolution rules

1. Stage the punch. Idempotency key hit → `Duplicate`, return success.
2. Resolve the device by `serial_no`. Unknown or disabled → `Error`/`Ignored`,
   surfaced as an unrecognised device in the desk.
3. Normalise time to site timezone. Beyond `max_future_skew_minutes` → `Error`.
   Before `ignore_punches_before` → `Ignored`.
4. Map the employee: device-specific `Biometric Enrolment` first, then the
   global one, honouring `valid_from`/`valid_upto`; fall back to
   `Employee.attendance_device_id`. No match → `Unmapped`, **terminal**.
4b. Device access: if the employee has a `Biometric Employee Access` record in
   *Allowed Devices Only* mode and this device is not on it → `Blocked`
   (employee recorded, no checkin). Released by `replay_blocked` when HR changes
   the record.
5. Log type: `device.fixed_log_type` wins, else the device's own value, else
   `Biometric Settings.default_log_type_policy`.
6. Create the `Employee Checkin` with `employee`, `time`, `device_id`,
   `log_type`, `skip_auto_attendance` and coordinates. Link it back, mark
   `Mapped`. If HRMS has **Allow Geolocation Tracking** on and no coordinates
   are available, the punch becomes an `Error` naming the device and the setting
   — so `Biometric Device.latitude`/`longitude` are required on such a site.
7. Advance `Shift Type.last_sync_of_checkin` server-side once every device in
   the group has reported.
8. Write one `Biometric Sync Log` for the batch.

Every step is isolated: one bad punch fails one punch, never the batch.

## 5. API

All under `biometric_hub.api.*`, all requiring `Biometric Integration`.

| Method | Purpose |
|---|---|
| `api.ingest.push(punches, connector=None)` | Batch submission. Per-punch `{key, status}`. Idempotent. |
| `api.ingest.heartbeat(connector, agent_version, devices=None)` | Liveness + version. Reply carries `profile_requests` and, for the push connector, `user_pushes`. |
| `api.ingest.user_push_results(connector, results)` | The agent's Easy Time Pro outcomes (≤100). Writes only `Biometric User Sync`. |
| `api.ingest.bootstrap(connector)` | The agent's device list and poll settings. |
| `api.admin.test_device(device)` | Desk button — driver `test_connection`. |
| `api.admin.sync_now(device, since=None)` | Desk button — one pull run. |
| `api.admin.map_unmapped(device_user_id, employee, device=None)` | Create enrolment, re-run parked punches. |

## 5a. Drivers shipped

| Driver | Mode | Read by |
|---|---|---|
| `ZKTeco` | LAN pull, port 4370 | Server (`Direct Poll`) or the agent |
| `ZKTeco Push (ADMS)` | Device pushes to `/iclock/*` | Server |
| `WDMS` | eSSL Time Pro's SQL Server database, **read-only** | The agent only |
| `Fake` | A local JSON file | Either — for commissioning and support |

## 6. Drivers

Registered by any app through the `biometric_drivers` hook:

```python
biometric_drivers = ["biometric_hub.drivers.zkteco.ZKTecoDriver"]
```

A driver converts a protocol into `RawPunch` objects. **A driver never touches
the database.** Interface: `test_connection`, `fetch_punches`, `parse_push`,
`sync_clock`, `push_enrolment`, plus capability flags.

## 7. The connector agent

Not in this repo. `grohr-biometric-agent` is a separate **GPL-3.0** program (a
fork of `frappe/biometric-attendance-sync-tool`) that runs on customer premises
and talks to `api.ingest.*` over HTTPS. It is deliberately dumb: no business
logic, no durable state that matters, every rule server-side. See PRD §7.

## 8. Install

```bash
bench get-app <repo> && bench --site <site> install-app biometric_hub
```

Dual path, kept in sync: `setup/scaffold.py` is idempotent and builds the schema
on a fresh install; exported doctype JSON carries it through `bench migrate`.
No manual SQL, no hand-edited config.

## 9. Verify

```bash
bench --site <site> execute biometric_hub.tests.selftest.run   # everything, 270 checks (2026-10-03)
bench --site <site> execute biometric_hub.tests.seed.run       # demo data, no hardware
bench --site <site> execute biometric_hub.tests.seed.clear
```

`selftest` asserts the schema, settings, least privilege and the idempotency
index, then runs the ingestion acceptance: a synthetic punch becomes exactly one
`Employee Checkin`, a replay of it creates none, an unknown device user ID parks
as `Unmapped` and is never retried on the wire, clock skew and the device cut-off
park correctly, and one malformed punch never takes its batch down. It creates
its own device and enrolments and removes everything it made.

## 9a. Operations

- **Hourly**: device health. A reader past its grace window becomes `Silent` and
  its alert recipients are emailed — once, on the transition, not every run.
- **Nightly**: reconcile (`Pending` and transient `Error` punches within the
  lookback window) and retention purge (`Mapped` and `Duplicate` only —
  `Unmapped` and `Error` are unfinished work and are never purged).
- **Reports**: *Employees Without Biometric Enrolment*, *Biometric Punch
  Outcomes*, *Biometric Device Uptime*.
- **ADMS**: `nginx/biometric-hub-iclock.conf` is additive and must be installed
  deliberately. Never run `bench setup nginx` on a shared server.

## 9b. Companion documents

| Document | For |
|---|---|
| `docs/SETUP.md` | Deploying a site end to end, in order, with a go-live checklist |
| `docs/CUSTOMER.md` | Whoever administers the site day to day |
| `docs/DEVICE-ACCESS.md` | HR: which machines / mobile app each employee may use |
| `docs/DEVICE-USERS.md` | HR: confirm who each machine ID is, by photo; automatic profile fetch |
| `docs/EMPLOYEE-IDS.md` | HR: several biometric IDs per employee, unique |
| `docs/EASYTIME-PUSH.md` | Admin/HR: pushing employees into eSSL Easy Time Pro |
| This file | Anyone changing the code |

## 10. Status

Phases 0–10 done 2026-08-31. Employee device access added 2026-10-02
(demo only). Phase plan and current status:
`/home/frappe/demo-bench/biometric-hub-prd.md` §10 and
`/home/frappe/demo-bench/biometric-hub-architecture.md`.
