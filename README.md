## Biometric Hub

Brand-agnostic biometric attendance integration for Frappe HR.

Biometric Hub owns everything between an attendance reader and an HRMS
`Employee Checkin`: staging, employee mapping, idempotency, device health and
audit. HRMS keeps everything after the checkin — shifts, attendance marking,
policy — untouched.

It exists because the alternative is what most sites actually run: a bespoke,
unversioned script on someone's Windows box, authenticating as an over-privileged
API user, posting one punch per HTTP call, with no back-off and no way to see
that it died three weeks ago.

### What it does

- **Nothing is dropped.** Every punch is persisted as a `Biometric Punch` before
  any mapping is attempted. A punch from an unknown device user becomes a row in
  a work queue, not a 417 into the void.
- **Nothing is duplicated.** A DB-level unique key on
  (device serial, device user id, punch time) makes replay free, which is what
  lets the on-prem agent be dumb and crash safely.
- **Mapping is a first-class doctype.** `Biometric Enrolment` supports
  per-device IDs and validity windows, and keeps `Employee.attendance_device_id`
  in sync so existing sites work from minute one.
- **Devices are visible.** Heartbeats, last-seen, silent-device alerts, and one
  screen showing every unmapped device ID with its punch count.
- **Least privilege.** The integration role can create checkins and nothing else
  — notably not write `Shift Type`, which the server advances itself.
- **Vendor-agnostic.** A driver converts a protocol into `RawPunch` objects and
  never touches the database.

### Documentation

| Document | What |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | The contract — scope, doctypes, resolution rules, API, install, verify |
| `biometric-hub-prd.md` (bench root) | The full PRD, phase plan and rationale |
| `biometric-hub-architecture.md` (bench root) | Living status and decisions log |

### The connector agent

Most readers are LAN-only, so something inside the customer's network must reach
out. That is `grohr-biometric-agent` — a **separate GPL-3.0 repository**, a fork
of [`frappe/biometric-attendance-sync-tool`](https://github.com/frappe/biometric-attendance-sync-tool),
running as a Windows service and posting batches outbound over HTTPS. It holds
no business logic: every rule lives here, on the server, where it can be fixed
without visiting a customer's office. See PRD §7.

### Installation

```bash
bench get-app $URL_OF_THIS_REPO
bench --site $SITE install-app biometric_hub
```

Requires `hrms`. No manual SQL, no hand-edited config.

### Verify

```bash
bench --site $SITE execute biometric_hub.tests.selftest.run
bench --site $SITE execute biometric_hub.tests.seed.run   # demo data, no hardware needed
```

### Contributing

This app uses `pre-commit` (ruff, eslint, prettier, pyupgrade):

```bash
cd apps/biometric_hub && pre-commit install
```

### License

MIT. The connector agent is a separate program under GPL-3.0 — see PRD §7.1.
