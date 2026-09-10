# Environments, Branching, and Promotion

**Version:** 1.0 · **Date:** 2026-09-09
**Companion to:** `00_REQUIREMENTS.md`, `02_AGENT_HIERARCHY.md`, `03_VALIDATION_AND_TESTING.md`

---

## 1. The thing most people miss

You asked for STAGE, TEST, and PROD and whether you were missing any. You were missing one obvious environment and one non-obvious idea.

**The obvious one: LOCAL.** The machine where an agent or a human edits code and runs it before anyone else sees it. Without it, TEST becomes the place where broken code goes, which makes TEST results meaningless.

**The non-obvious one — and it matters much more here: code environments and data environments are two separate axes, and this project needs both.**

In a normal web application, "the STAGE environment" means one thing: a copy of the app with a copy of the database. Here that framing breaks, because **the production data is irreplaceable and cannot be copied cheaply.** OpenSea gives 120 REST reads an hour (measured). If you corrupt six months of accumulated floor history, you cannot re-download it. There is no backup at the API — the history exists only because you recorded it.

So the question "which environment am I in?" has two answers that must be tracked separately:

- **Which code is running** — LOCAL, TEST, STAGE, PROD
- **Which data is it touching** — FIXTURE, REPLAY, SHADOW, PROD

And the rule connecting them is the important part:

> **Only PROD code may write to PROD data. Every other combination is read-only against PROD data, or writes to its own isolated store.**

That single rule is the main thing standing between a routine coding mistake and permanently losing the asset the whole project is built on.

---

## 2. Code environments

| Environment | What it is | Who runs it | Writes to |
|---|---|---|---|
| **LOCAL** | A developer's or agent's working copy on a branch | Any agent that writes code — L2 agents, plus `quant-research` and `validator` within their L1 carve-outs — and Spencer | LOCAL data store only |
| **TEST** | Automated test run — CI. Every commit, every PR | Automated | FIXTURE + REPLAY data only |
| **STAGE** | A full running copy of the system, live stream connected, but writing to a **separate store** | Orchestrator deploys | SHADOW data store |
| **PROD** | The real thing. The historical record lives here | Orchestrator deploys, after gates | PROD data store |

### 2.1 Why STAGE exists here specifically

STAGE is not a formality. It is where you answer: *does the new pipeline produce the same numbers as the old one?*

Because STAGE consumes the same live stream as PROD but writes to its own store, you can run both versions of a pipeline side by side for a week and diff the outputs. That is the **shadow run**, and `docs/03_VALIDATION_AND_TESTING.md` requires it for any change to data semantics, with every difference explained before cutover.

This catches the worst bug class in the project: a change that alters what a number *means* without altering whether the code runs. Nothing crashes. Every chart still renders. The numbers are just quietly different from last month's, and every backtest spanning the change is now comparing two different things. A shadow run is the only reliable way to see that before it is baked into history.

### 2.2 The stream constraint on STAGE

The Stream API is unmetered, so running STAGE and PROD stream consumers simultaneously costs nothing in rate limit. **REST is not free**, and both environments draw from the same account-wide token bucket. Therefore:

- STAGE gets a hard, small REST budget allocation (default: 10% of the hourly bucket).
- STAGE's governor runs at the lowest priority class and is starved first.
- If possible, use a **separate OpenSea account** for STAGE so the buckets are genuinely independent. This is the cleanest fix and worth the setup effort.

---

## 3. Data environments

| Data env | Contents | Mutable? | Purpose |
|---|---|---|---|
| **FIXTURE** | Small, synthetic, hand-verified datasets committed to the repo | Yes — it is source code | Unit and property tests. Fast, deterministic, free |
| **REPLAY** | A date range of the landing zone, resolved via the manifest — not a separate copy | Read-only view | Integration tests against real message shapes at **zero API cost** |
| **SHADOW** | A parallel store written by STAGE from the live stream | Yes, disposable | Pipeline change comparison |
| **PROD** | The historical record. The asset | **Append-only, forever** | The real system |

### 3.1 REPLAY is the landing zone, not a copy of it

There is no separate replay-recording path. **REPLAY is a date range of the landing zone**, resolved through the daily manifest (REQ-D-27, `07_STORAGE_AND_RECORDING.md` §2.5). PROD ingestion writes the landing zone; a test run points at some past window of it and replays those bytes.

This matters beyond saving work: test data captured by a second, separate code path is not guaranteed to resemble what production actually receives. Here they are the same bytes by construction, so a replay test exercises exactly the payloads the live system saw.

The reason: you cannot afford to integration-test against the live API. At 120 reads an hour (measured), a suite that makes even 60 calls costs 10% of the budget *per run* — and a suite is run many times a day, on every commit, by every agent. That competes directly with production ingestion for the same account-wide bucket. Recorded sessions give you realistic test data — real message shapes, real edge cases, real out-of-order arrivals — for free, forever, and reproducibly.

Twelve months from now, a recorded session from Phase 0 will still be catching regressions. It costs almost nothing to start and cannot be created retroactively.

### 3.2 PROD data is append-only, for everyone

No agent at any authority level may modify or delete a record in the landing zone or the bitemporal history. Not to fix a bug, not to clean up, not to correct a typo.

**Corrections are new records that supersede old ones.** The old record stays, marked superseded, with the reason. This costs disk space, which is cheap, and buys the ability to reconstruct exactly what the system believed at any past moment — which is what makes point-in-time backtesting honest and is required by REQ-F-28.

---

## 4. Branching

Deliberately simple. One person and a set of agents do not need a complex branching model, and complexity here creates merge mistakes rather than preventing them.

```
main ─────────────────────────────────●──────────●────────────▶  (always deployable to PROD)
        │                             ▲          ▲
        ├── feat/rest-governor ───────┘          │
        ├── feat/trait-hedonic ──────────────────┘
        └── fix/BUG-YYYYMMDD-NNN ────────────────┘
```

| Branch | Rule |
|---|---|
| `main` | Always deployable. Protected. No direct commits, ever |
| `feat/<short-name>` | New work. One logical change per branch |
| `fix/<BUG-ID>` | Bug fix, named for its log entry so the link is automatic |
| `exp/<name>` | Research exploration. **Never merged to main** — findings graduate into a `feat/` branch, raw exploration does not |

### 4.1 Commit messages

```
<type>(<scope>): <summary>

<what changed and why>

Requirement: REQ-D-03
Bug: BUG-YYYYMMDD-NNN        (if applicable)
Validated-by: validator       (if it passed a gate)
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `config`, `data`.

The `Requirement:` line is not bureaucracy — it is what lets you ask "why does this code exist?" in six months and get an answer. Code that traces to no requirement is a signal that either the code is unnecessary or a requirement is missing. Both are worth knowing.

---

## 5. Promotion gates

Nothing moves right without passing the gate.

```
LOCAL ──▶ TEST ──▶ STAGE ──▶ PROD
      │        │         │
      │        │         └─ Shadow-run diff fully explained
      │        │            Validator sign-off recorded
      │        │            Rollback path exists and is tested
      │        │            Spencer approves anything irreversible
      │        │
      │        └─ All unit + property + integration tests pass
      │           Leakage trap still rejects the planted strategy
      │           No new data-quality alerts
      │
      └─ Tests written alongside the code, not after
         Self-review done
```

### 5.1 The leakage trap runs on every promotion

`docs/03_VALIDATION_AND_TESTING.md` §6.2 defines a fixture strategy containing deliberate look-ahead. The backtester must reject it. This runs at every TEST gate.

If it ever reports a profit, the backtester's integrity guarantee is broken, and **every backtest result produced since the last passing run is invalid.** That is not a normal test failure — it retroactively invalidates work. Treat a leakage-trap failure as **S0b** (`docs/05_BUG_TAXONOMY.md` §2): halt backtests and mark results invalid, but do **not** halt ingestion — the backtest accessor writes nothing to the store.

### 5.2 Rollback

Every PROD deploy has a tested rollback path before it ships. For code this is straightforward. For anything that changed data semantics, rollback means the old pipeline path is still present and switchable, which is why §2.1's shadow run keeps both paths alive through cutover rather than deleting the old one on merge.

---

## 6. Configuration and secrets

### 6.1 Configuration

All thresholds, weights, universe criteria, cost assumptions, and interval and display definitions live in version-controlled config files — **never in code** (REQ-N-09). Per-environment overrides layer on a shared base:

```
config/
  base.yaml           # every default
  local.yaml          # overrides for LOCAL
  test.yaml
  stage.yaml
  prod.yaml
  universe.yaml       # the watchlist
  costs.yaml          # fee, royalty, gas, slippage assumptions
  assumptions.yaml    # every registered judgment — see 06_TIME_UNITS_AND_LAYERING.md §4.4
  intervals.yaml      # chart intervals and anchored ranges — see 06 §1.2
```

Every config change is logged with timestamp and rationale (REQ-N-10). When a result changes, you must be able to tell whether the world changed or you did. Without that log, you cannot.

### 6.2 Secrets

API keys and RPC credentials load from environment variables or a local secret store, **never from the repository** (REQ-N-11). CI runs an automated secret scan; a hit fails the build.

**No private keys or seed phrases in any environment** (REQ-N-12), and **no transaction-signing or order-submission capability reachable from any runtime agent** (REQ-N-14). `docs/03_VALIDATION_AND_TESTING.md` §9.4 makes verifying the absence of signing capability a *standing* test rather than a one-time review. Note REQ-N-12 is scoped to v1 and anticipates that execution, if it ever arrives, is manual or via an explicitly-scoped session under the safety preconditions in `docs/00_REQUIREMENTS.md` §7.4 — so "ever" is a v1 statement, not a permanent architectural claim.

### 6.3 Key expiry

Free instant OpenSea keys expire after 7 days. Each environment tracks its key's expiry and alerts before it lapses (REQ-D-06). A silently expired key looks exactly like a quiet API outage, which is a bad afternoon.

---

## 7. What runs where, in practice

| Component | LOCAL | TEST | STAGE | PROD |
|---|---|---|---|---|
| Stream consumer | Replay files | Replay files | Live → SHADOW | Live → PROD |
| REST governor | Mocked | Mocked | 10% budget, lowest priority | Full budget |
| Chain indexer | Fixture | Fixture | Live, separate store | Live |
| Metric engine | ✓ | ✓ | ✓ | ✓ |
| Signal engine | ✓ | ✓ | ✓ (no alerts sent) | ✓ |
| ETH/USD price feed | Fixture rates | Fixture rates | Live | Live |
| Alert delivery | Disabled | Disabled | **Routed to a test channel** | Live to Spencer |
| Paper trading | Disposable | Fixture | Disposable | Real record |
| Backtester | ✓ | ✓ + leakage trap | ✓ | ✓ |

Note the alert row. STAGE alerts must go somewhere visible but separate — a dedicated Slack channel — so you can see whether the alert logic is sane before it starts interrupting you. An alerting system tested only in production teaches you to ignore alerts, which is the failure mode REQ-F-25 exists to prevent.

---

## 8. Running unattended

*Added 2026-09-09 (PR-1, `docs/proposals/TECHLEAD_2026-09-09_factcheck.md` §3). Written for a reader who has not administered a Unix machine before; every term is defined where it first appears.*

### 8.1 The problem this solves

Until now, recording happened only while a Terminal window opened by `start.command` stayed open. Close the window, log out, or let the process crash at 2 AM, and recording stops until somebody notices.

That matters more here than in most systems, because **the hours you do not record cannot be bought back.** OpenSea publishes no historical floor series and no historical order book. The record exists only because we recorded it (`07_STORAGE_AND_RECORDING.md` §4). An hour of downtime is an hour that is permanently absent from every backtest this project will ever run — and cancellations and order invalidations inside that window are not backfillable from anywhere, at any price (REQ-D-09a).

Every other piece of work on the PR list is recoverable later. This one is not, which is why it went first.

### 8.2 What launchd is

**launchd** is the piece of macOS that starts and supervises background programs. It is Apple's own; it has been the thing that starts everything on a Mac since 2005. It is already running on the Operator's machine right now, managing dozens of Apple's own jobs. We are adding three of ours to the list.

A job is described by a **property list** (a "plist") — a small XML file. A per-user job lives in `~/Library/LaunchAgents/`, needs no administrator password, and can be deleted by dragging it to the Trash. It is not a login item, it is not a kernel extension, it is not a system daemon, and it cannot affect any other user account on the machine.

We install three:

| Label | What it runs | When |
|---|---|---|
| `com.navanax.recorder` | `python3 -m navanax.cli ingest --supervised` | At login, and again ~10 s after any exit |
| `com.navanax.dashboard` | `python3 -m navanax.cli dashboard --no-browser --port 8765` | At login, and again ~30 s after any exit (see `ThrottleInterval` below) |
| `com.navanax.traits` | `python3 -m navanax.cli traits` | At login, then daily at 03:30 local |

Plus one **opt-in** job that `autostart-install.command` deliberately does *not* install: `com.navanax.keepawake` (§8.6).

The plists are generated by `tools/launchd.py`, never by a shell heredoc. A plist is XML, and a malformed one does not fail loudly — launchd simply never runs the job, and `launchctl print` reports "could not find service". A recorder that silently never started is indistinguishable from a quiet market, which is the single most dangerous confusion available in this system. `plistlib` cannot emit invalid XML, and `tests/selftest.py` parses every plist the generator produces.

### 8.3 The keys that matter, in plain terms

- **`KeepAlive`** — "restart this whenever it exits, for any reason." Crash, clean exit, `kill`, anything. This is the single key that makes the recorder unattended. The recorder and the dashboard have it. **The traits job deliberately does not**: it is a job that is *supposed* to finish, and restarting a finished job in a loop would drain the metered 120-reads-per-hour REST bucket permanently.
- **`ThrottleInterval: 10`** — launchd will not restart a job more often than every 10 seconds. Without it, a job that fails instantly is respawned in a hot loop. This is the recorder's and the traits job's value.
- **`ThrottleInterval: 30` on the dashboard** — BUG-20260910-067. The failure mode a throttle has to survive on the dashboard is a **port conflict**: a second dashboard is already listening on 8765, so every start fails to bind and exits 2. At 10 s that is 360 starts an hour, and on 2026-09-10 it was 177 of them — each one opening the 2.8 GB analytical store and folding new frames into it *before* the bind failed, because the store was opened first. Two writers alternating on one SQLite store, with processes killed mid-write, left it `database disk image is malformed`. `dashboard.serve()` now binds **before** it opens the store, so a doomed retry writes nothing at all; 30 s is the second layer, which makes the loop slow enough to read in `dashboard.log`. `KeepAlive` stays on — the page is how the Operator sees the record.
- **`RunAtLoad`** — start it once immediately when the job is loaded, which happens at install and at every login.
- **`ExitTimeOut: 30`** — how long launchd waits after `SIGTERM` before it force-kills. The recorder flushes its final frame on `SIGTERM` (`cli._install_shutdown_handlers`, BUG-20260909-037); this gives that flush room.
- **`StartCalendarInterval`** — a wall-clock schedule, in **local** time, not UTC. If the Mac is asleep at 03:30, launchd runs the job when it next wakes; it does not wake the Mac.
- **`EnvironmentVariables: PATH`** — a launchd job does **not** read `.zshrc` or `.zprofile`. Whatever PATH the plist names is the entire PATH. Ours covers the python.org framework location, both Homebrew prefixes, and the system directories. The installer also writes the *full absolute path* of the interpreter into `ProgramArguments`, so the job does not depend on PATH resolution at all.
- **No API key appears in any plist.** The CLI reads `.env` itself (`dotenv.require`), which keeps §6.2's rule intact: secrets stay in one file, outside the repository, and are not copied into `~/Library/`.

### 8.4 The exit-code contract

A supervisor is only as good as the process's willingness to die honestly. `ingest` owes launchd these codes, and `tests/selftest.py` holds it to them:

| Code | Meaning |
|---|---|
| `0` | Clean stop — `SIGTERM`, `SIGHUP` or Ctrl-C. Final frame flushed. |
| `2` | Configuration: empty watchlist, or no usable `OPENSEA_API_KEY` in `.env`. |
| `3` | Refused: the compressor failed its round-trip check on this machine. |
| `4` | Refused: another ingest process holds this landing zone's lock. |
| `5` | Fatal error during the run (most plausibly the landing zone becoming unwritable, which `stream.run()` re-raises deliberately because reconnecting cannot fix a local disk). |

Every non-zero code means "did not record". Under `KeepAlive` launchd restarts regardless of which one it was, and **the restart records the downtime as a gap** (`StreamConsumer.record_downtime_gap`, BUG-20260909-009) — so a restart loop shows up in the gap register as visible holes rather than as an unexplained quiet period. `autostart-status.command` prints this table.

`dashboard` owes launchd the same shape (BUG-20260910-067):

| Code | Meaning |
|---|---|
| `0` | Clean stop — Ctrl-C or `SIGTERM`. |
| `2` | **Refused before the store was opened**: the host is not loopback (REQ-N-13), or the port is already held — almost always by a second dashboard. `serve()` binds before it constructs anything that opens `data/analytics.sqlite`, so this exit costs nothing and folds nothing. |
| `4` | Refused: another process holds the analytical store's fold-writer lock (`<store>.lock`). The message names its pid. |
| `5` | Fatal error during the run. |

The dashboard's non-zero codes mean "did not serve", not "did not record" — the recorder is a separate job and keeps landing frames throughout. What made a dashboard restart loop dangerous was never the loop itself but what each iteration *did* on the way down; see `ThrottleInterval: 30` in §8.3.

One important non-case: **a rejected or expired API key does not exit.** The stream loop treats an upstream failure as reconnectable, backs off, and records a gap for each attempt. That is deliberate — a key rotation should not kill the recorder — but it means "the job is running" is not the same claim as "the job is recording". The gap count in `autostart-status.command`, not the process state, is the number to read.

### 8.5 The single-instance guarantee, and why the installer stops a manual recorder

Two ingest processes writing to one landing zone silently lose manifest records — measured at 295 of 600 gap records lost, with the integrity audit reporting clean. A lost gap record is undetectable forever.

The guard is already in the code: `cli._single_instance` takes an exclusive `flock` on `data/landing/.ingest.lock`, and the second process **refuses to start** (exit 4) rather than racing. So the worst outcome of installing while a manual recorder is open is a background job that refuses and retries every 10 seconds — never a corrupted record.

`autostart-install.command` still stops the manual recorder first, by reading the pid out of the lock file, sending `SIGTERM`, and waiting up to 30 seconds for the lock to be released. The reason is not safety but completeness: a clean stop flushes the final frame and closes the manifest entry, and an abrupt one does not. If the process does not exit within 30 seconds the installer **refuses and installs nothing**.

### 8.6 Sleep: the thing this cannot fix

**When the Mac sleeps, recording stops.** launchd cannot prevent sleep, and no job scheduler on macOS can. A closed lid, or the display sleeping on battery, stops the recorder as surely as quitting it. The gap is recorded honestly on wake; the events inside it are gone.

The opt-in mitigation is `keepawake-install.command`, which installs `com.navanax.keepawake` running Apple's own `/usr/bin/caffeinate -i -s`:

- `-i` prevents idle sleep;
- `-s` restricts that to when the Mac is **on AC power**, so a laptop on battery still sleeps normally rather than flattening overnight.

Closing the lid still sleeps the machine either way. `caffeinate` cannot override the lid and neither can anything else in this project. Overnight recording means lid open, charger in.

This is deliberately not installed by `autostart-install.command`: changing when the machine sleeps is the Operator's decision about his own hardware, not an installer's default. `keepawake-uninstall.command` reverses it immediately, with no restart.

### 8.7 Logs

Each job appends to its own plain-text file, openable in TextEdit:

```
data/logs/recorder.log
data/logs/dashboard.log
data/logs/traits.log
data/logs/keepawake.log
```

launchd does not create these directories; the installer does. `--supervised` line-buffers stdout and prints a banner with the pid and start time at every start, because launchd appends every restart to the same file — without a boundary marker you cannot tell one healthy 12-hour run from a loop of 3-second crashes, and the restart count is the number that matters.

**These logs are not rotated yet.** At the observed event rate that is not urgent, but it is an open item: an unbounded log file is a slow disk-full, and a full disk is what `LandingZoneWriteError` and exit code 5 exist for.

### 8.8 Uninstalling

Double-click `autostart-uninstall.command`. It stops the three jobs (via `launchctl bootout`, which sends `SIGTERM` first, so the recorder flushes its final frame) and deletes exactly the three plists it created. **It deletes no data** — landing zone, databases and logs are untouched — and `start.command` works normally again afterwards. The opt-in keep-awake job has its own uninstaller and is deliberately left alone.

`autostart-status.command` is read-only: it prints each job's state, pid and last exit code, the last five lines of each log, and the fast gap count from `navanax.cli status`. It starts nothing, stops nothing, and spends no REST budget.

### 8.9 Where this sits in §2's model

These LaunchAgents run **PROD code against PROD data** on the Operator's machine. That is the one combination §2 permits to write to the historical record. Nothing here changes ingestion semantics: `--supervised` only affects output buffering and a banner line. A STAGE shadow run, when one exists, needs its own labels, its own landing root, and its own lock file — not a second copy of these plists.
