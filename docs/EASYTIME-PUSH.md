# Pushing employees into eSSL Easy Time Pro

Added 2026-10-03 (demo only). GroHR creates, updates and removes employees in Easy
Time Pro (eSSL's build of ZKTeco BioTime) through the office-PC agent, so a new
joiner reaches the machines without being typed in twice. Easy Time Pro then sends
the user to every machine in their **area**, as it normally does.

**What it does not do:** fingerprints and faces still have to be enrolled at a
machine. We create the person (ID, name, area); nobody can make a fingerprint
remotely. Once someone has enrolled at one machine, Easy Time Pro can copy their
fingerprint or face to the other machines in that area.

## How it flows

```
HR: new biometric ID / employee leaves / name or branch changes / "Send" button
  → Biometric User Sync row (Queued)
  → next agent check-in: the heartbeat reply carries it (Sent)
  → agent calls Easy Time Pro's REST API on the office PC
  → result back to GroHR: Done / Dry Run / Conflict / Error
```

The agent only calls out, so nothing waits on the PC being reachable. A request
with no answer after 30 minutes is sent again (the calls are safe to repeat). After
5 attempts it becomes an Error.

## What gets pushed

| Event | Request |
|---|---|
| An **active** employee gets a biometric ID that counts on all machines | Add / Update |
| Name, branch, department or company changes, and GroHR created that user before | Add / Update (overwrite) |
| Employee status goes from Active to Left / Inactive / Suspended | Remove from Machines (Easy Time Pro *resignation*: attendance disabled, user removed from the machines). Switch: *Remove from Machines When Employee Leaves* |
| Employee form → Attendance → **Send to Easy Time Pro** | Add / Update for each of their IDs |
| Easy Time Pro Sync list → **Push all active employees** | Preview first, then queues every ID never pushed before |

IDs that count on **one machine only** are never pushed. Easy Time Pro cannot
represent "this number is this person on that machine only", so those stay HR's to
set on the machine.

## The safety rule: never overwrite somebody else

If Easy Time Pro already has a user with the same ID:

* the name looks like the same person → updated automatically (area, name);
* anyone else → **Conflict**. Nothing is changed. The row shows who Easy Time Pro
  has. Fix the ID on the employee, or, if it really is the same person under
  another name, press **Overwrite** on the row.

This is deliberate. At a customer, 13 machine IDs are shared between branches. Renaming
a machine user without checking would send another person's punches to this
employee.

## Setting it up

1. **Office PC (agent 0.4.0+):** make an Easy Time Pro user for the agent, add the
   `easytime` block to `config.json` with `"dry_run": true`, and run
   `python -m grohr_agent --probe-easytime`. It lists the area and department
   codes. See the agent README.
2. **Biometric Settings → Push Employees to Easy Time Pro:**
   * **Connector**: the agent on that PC;
   * **Area From**: Branch (or Department / Company);
   * **Area Rules**: one row per GroHR branch → Easy Time Pro area code(s),
     comma separated, with an optional department code;
   * **Default Area Code(s)**: leave blank unless every unmatched employee should
     go to that area. Blank means an employee with no rule is **Skipped**, not
     pushed everywhere;
   * **Default Department Code**: `1` is Easy Time Pro's built-in department.
3. Tick **Push Employees to Easy Time Pro**. Use **Push all active employees**
   (Preview first). With dry run on, rows come back **Dry Run** with a message
   such as "would create 1234 RAMESH in 1 area(s)". Check a few.
4. Set `"dry_run": false` on the PC, restart the service, then press **Resend all
   Dry Run**. Settle every **Conflict** by hand.

## Statuses

| Status | Meaning | What to do |
|---|---|---|
| Queued | Waiting for the agent's next check-in | — |
| Sent | Handed to the agent, no answer yet | Resent after 30 min |
| Done | Created / Updated / Resigned / Not Found (removing someone who was not there) | — |
| Dry Run | The agent is in dry run; the message shows what it would do | Resend all Dry Run after switching it off |
| Conflict | Easy Time Pro has someone else under this ID | Fix the ID, or Overwrite |
| Skipped | No area rule for the employee's branch, the employee no longer exists, or a newer request replaced this one | Add the rule, then Retry |
| Error | Easy Time Pro refused, is unreachable, the agent has no `easytime` block, or no answer after 5 tries | Read the message, then Retry |

## For developers

* `engine/user_push.py` makes every decision: who, which area, overwrite or not,
  same person or conflict. It writes only `Biometric User Sync`.
* Hooks: `Biometric Enrolment.on_update` (new global ID) and `Employee.on_update`
  (leaver, name or placement change). Neither can block a save.
* API: the `heartbeat` reply has `user_pushes` (only for the configured push
  connector); `api.ingest.user_push_results(connector, results)` takes the answers.
* Agent: `grohr_agent/easytime.py`. BioTime-style endpoints
  (`/jwt-api-token-auth/`, `/personnel/api/employees|departments|areas|resigns/`),
  which can be overridden under `easytime.paths`. **These have not yet been
  checked against a real Easy Time Pro install.** The tests use an in-memory fake
  (`tests/fake_easytime.py` in the agent repo). Run `--probe-easytime` on the real
  PC before trusting them.
* Tests: `tests/test_user_push.py` (30 checks: the real agent against the fake,
  over HTTP), plus 15 offline checks in the agent.
