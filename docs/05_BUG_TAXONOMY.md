# Bug Taxonomy, Error Hierarchy, and Logging

**Version:** 1.0 · **Date:** 2026-09-09
**Companion to:** `03_VALIDATION_AND_TESTING.md`, `04_ENVIRONMENTS.md`

---

## 1. The principle that sets severity here

In most software, severity tracks how loudly something breaks. A crash is worse than a wrong number.

**In this system it is the reverse.** A crash stops work and gets fixed the same day. A wrong number gets charted, analyzed, backtested, and eventually traded on — and nothing downstream will catch it, because everything downstream is designed to trust the data layer.

So the first question in triage is not "how badly did it break?" It is:

> **Did this write wrong data, or produce a wrong number that looked right?**

If yes, severity goes up, not down. A silent one-percent error in a price series is worse than a stack trace, because the stack trace is free to detect and the silent error costs six months of invalid backtests.

---

## 2. Severity hierarchy

| Level | Name | Definition | First action | Target response |
|---|---|---|---|---|
| **S0a** | **Data corruption** | Wrong data is being written to the PROD store right now, or already has been | **HALT INGESTION IMMEDIATELY**, then diagnose | Immediate, drop everything |
| **S0b** | **Backtest integrity failure** | The leakage trap passed, or look-ahead was detected. Every backtest since the last passing trap run is invalid | **HALT BACKTESTS**, mark affected results invalid. Do **not** halt ingestion — the fault is in the backtest accessor, which writes nothing, and halting would create a real S2 gap for no benefit | Immediate, drop everything |
| **S1** | **Silent wrongness** | A number is wrong but plausible. Nothing crashes, nothing alerts, and a human would not notice. Includes any monitor that failed to fire when it should have | Determine blast radius: which records, which window, which analyses used it | Same day |
| **S2** | **Data loss** | Recoverable data was lost or not captured — an ingestion gap, a missed backfill window, a dropped event class | Record the gap honestly. Backfill via REST where the events endpoint allows (REQ-D-09); **never interpolate or forward-fill** the remainder | Same day if the recoverable window is closing, else next day |
| **S3** | **Functional break** | Something is visibly broken. A view errors, a job crashes, a query fails. Loud, obvious, contained | Normal debugging | Within days |
| **S4** | **Degradation / cosmetic** | Slow query, awkward layout, unclear label, missing convenience | Backlog | When convenient |

### 2.1 Escalation rules

Severity is not fixed at intake. Raise it when:

- An S3 turns out to have been writing bad records before it crashed → **S0a or S1**.
- An S4 "cosmetic" issue turns out to be a chart hiding a gap or omitting an uncertainty interval → **S1**. Those look cosmetic and are not; they cause wrong decisions.
- An S2 gap turns out to be unrecoverable and spans a period used in a completed backtest → **S1**, because a conclusion was drawn from incomplete data.

**Never lower a severity to make a queue look better.** If a severity was assigned wrongly, correct it with a logged reason.

### 2.2 The S0a standing rule

**Halt first, diagnose second.** Stopping ingestion creates a gap, and a gap is recoverable — you record it, backfill what the API allows, and mark the rest as irrecoverable. Continuing to write corrupt records into an append-only store creates a problem that may be permanent, because you cannot delete them and you may not be able to tell later which ones were wrong.

Losing an hour of data is an inconvenience. Not knowing which of your records are trustworthy is a project-ending problem.

---

## 3. Error class taxonomy

Severity says how urgent. Class says what kind, which is what tells you where to look and which monitor should have caught it.

| Class | Code | Examples | Which monitor should catch it |
|---|---|---|---|
| **Ingestion** | `ING` | Stream disconnect, malformed payload, missed event, backfill failure | Stream alive, event rate band, gap detector |
| **Rate limit** | `RTL` | Budget exhausted, 429 storm, governor bypassed, key expired | Budget burn rate, key expiry check |
| **Normalization** | `NRM` | Wrong field mapping, unit error, currency mishandled, precision loss | Schema conformance, cross-source agreement |
| **Temporal** | `TMP` | Ordering by arrival not `event_timestamp`, bitemporal violation, timezone error, **look-ahead leakage** | Bitemporal invariant audit, leakage trap |
| **Statistical** | `STA` | Wrong estimator, assumption violated unreported, missing uncertainty, sample-size gate bypassed | Assumption diagnostics, validation gates |
| **Backtest integrity** | `BTI` | Look-ahead, unrealistic fill, understated cost, instant-exit assumption | Leakage trap, cost audit, fill audit |
| **Presentation** | `PRS` | Chart interpolates a gap, staleness not shown, provenance missing, interval dropped from display | Manual review, `platform-engineer` checklist |
| **Configuration** | `CFG` | Threshold in code not config, unlogged config change, wrong environment config loaded | Config change log audit |
| **Infrastructure** | `INF` | Process died, disk full, dependency broke, migration failed | Supervisor health check |
| **Security** | `SEC` | Credential in repo, listener bound beyond localhost, any transaction capability appearing | Secret scan, port scan, capability check |

**`TMP` and `BTI` deserve standing paranoia.** They are the classes that produce confident, profitable-looking, completely wrong results, and they are nearly invisible without the specific tests built to catch them.

**Any `SEC` finding is minimum S1**, regardless of exploitability. This is a single-operator local system with no network exposure, so the realistic risk is low — but the cost of being wrong about that is total, and the checks are cheap.

---

## 4. Bug log

Location: `docs/logs/BUGS.md`.

**The log is append-only for entries; the lifecycle fields of an existing entry are updated in place.** Precisely:

- A new entry is appended. Entries are **never deleted or reordered**, and `id`, `logged_at`, `occurred_at`, `severity`, `class`, `summary`, `detail`, `branch`, `commit`, `location`, `link`, `data_impact`, and `detected_by` are **immutable once written** — they record what was true at discovery.
- `status`, `resolution`, `regression_test`, and `monitor_gap` are **mutable** and expected to be filled in later.
- `severity` is the one exception to immutability: it may be **raised** (§2.1) with a logged reason appended to `detail`. It is never lowered without Spencer's explicit approval.

**Ownership of the update:** `bug-triage` writes the initial entry. **The agent that merges the fix updates `status`, `resolution`, and `regression_test` in the same change as the fix**, so the log cannot drift from the code. `docs-steward` audits for entries left `in_progress` past their severity's target response and reports them; it does not close them.

### 4.1 Entry format

```markdown
### BUG-YYYYMMDD-NNN · S1 · NRM · fixed

| | |
|---|---|
| **Summary** | Collection stats volume read as USD when API returned ETH |
| **Logged** | 2026-09-10T14:22:05-05:00 |
| **Occurred** | 2026-09-08T09:00:00-05:00 (first affected record) |
| **Detected by** | Cross-source agreement monitor |
| **Branch** | `feat/collection-stats` |
| **Commit** | `a3f9c21` |
| **Location** | `src/ingest/normalize/collection_stats.py:L84-L91` |
| **Link** | [collection_stats.py:L84-L91](https://github.com/ORG/REPO/blob/a3f9c21/src/ingest/normalize/collection_stats.py#L84-L91) |
| **Data impact** | 2026-09-08T09:00 → 2026-09-10T14:22. 412 CollectionStateSnapshot records. Superseding records written 2026-09-10T16:40. No backtest ran over this window |
| **Status** | fixed |
| **Resolution** | Read `volume_symbol` and branch on it rather than assuming USD. Fixed in `e71b0aa` |
| **Regression test** | `tests/ingest/test_collection_stats.py::test_eth_denominated_volume` |
| **Monitor gap** | None — cross-source agreement caught it in 2 days. Consider tightening to same-day |

**Detail.** The v2 collection stats endpoint reports volume in the currency named by
`volume_symbol`. The normalizer assumed USD unconditionally. For ETH-denominated
collections this understated volume by roughly the ETH price...
```

### 4.2 Field notes

**`link` — pin the commit SHA, not a branch name.** A link to `blob/main/file.py#L84` points at whatever is on line 84 today, which after a few edits is a different line of code and quietly misleads whoever reads the entry later. `blob/a3f9c21/file.py#L84` points at what was actually broken, forever.

**`data_impact` is never blank.** Every entry answers: did this write wrong data, which records, over what window, and has it been superseded? "None" is a valid answer and must be stated explicitly, because a blank field is indistinguishable from an unasked question.

**`monitor_gap` is where the compounding value is.** For every bug, ask: which monitor should have caught this, and why didn't it? A bug found by Spencer rather than by a check is a gap in the detection layer, and closing that gap is usually worth more than the fix itself.

**`regression_test` is mandatory to close a bug.** No test, no closure — the status stays `in_progress`. This is the mechanism by which the test suite becomes a record of every mistake the project has actually made, which is far more valuable than a suite written from imagination.

### 4.3 Index

`docs/logs/BUGS_INDEX.md` maintains a sortable table of all entries: ID, severity, class, status, summary, date. `docs-steward` regenerates it from the log.

---

## 5. Triage flow

```
Something looks wrong
  → bug-triage (haiku — cheap, so nobody hesitates to report)
  → Is PROD data being corrupted RIGHT NOW?
      YES → HALT INGESTION. S0a. Escalate to Spencer immediately.
  → Is it a backtest-integrity failure (leakage detected / trap passed)?
      YES → HALT BACKTESTS, mark results invalid. S0b. Escalate immediately.
            Do NOT halt ingestion.
      NO  → continue
  → Classify: severity + class
  → Determine data impact — which records, which window
  → Write the log entry with every field
  → Route:
      S0a/S0b/S1 → Spencer notified + the owning agent, immediately
      S2/S3 → owning agent, queued by severity
      S4    → backlog
  → Fix on branch fix/<BUG-ID>
  → Regression test added (mandatory)
  → validator reviews anything S0a/S0b/S1 or touching ING/TMP/STA/BTI
  → Merge, update log status and resolution
  → Post-mortem for S0a/S0b/S1: root cause, detection gap, prevention
```

### 5.1 Who fixes what

| Class | Owner |
|---|---|
| `ING`, `RTL`, `NRM`, `TMP` (ingestion side) | `data-engineer` |
| `STA`, `BTI`, `TMP` (backtest side) | `quant-research` builds the fix, `validator` reviews it — **never the same agent** |
| `PRS` | `platform-engineer` — checklist is the "Correctness requirements that look cosmetic but are not" section of `.claude/agents/platform-engineer.md` |
| `CFG`, `INF`, `SEC` | `data-engineer` or `platform-engineer` by location; **`SEC` always gets `validator` review** |

### 5.2 Post-mortems

Required for every S0a, S0b and S1. Four questions, in writing, in the log entry:

1. **What was the root cause?** Not "the code was wrong" — why was it wrong, and why did it look right?
2. **Why did detection take as long as it did?** Which monitor should have caught it?
3. **What else could this cause have broken?** Same pattern, different file.
4. **What prevents recurrence?** A regression test is the minimum. A new monitor is better.

No blame. The purpose is a system that catches this class of error next time, and a post-mortem culture that assigns fault produces post-mortems that hide information.

---

## 6. Runtime error hierarchy (code)

The exception classes the application raises. Mapping error types to severity in code, rather than in a human's head at 2am, is what makes the halt-on-corruption rule actually execute.

```
OpenSeaPlatformError                    (base — never raised directly)
│
├── DataIntegrityError                  → S0. Handler HALTS INGESTION.
│   ├── CorruptRecordError
│   ├── BitemporalViolationError
│   └── ImmutableStoreWriteError        (attempt to modify landing zone)
│
├── BacktestIntegrityError              → S0. Handler HALTS BACKTESTS and
│   │                                     marks every result since the last
│   │                                     passing leakage-trap run INVALID.
│   │                                     Does NOT halt ingestion — the fault
│   │                                     is in the backtest accessor, which
│   │                                     writes nothing to the store, and
│   │                                     halting ingestion would create a
│   │                                     real S2 gap in response to it.
│   ├── LeakageDetectedError            (backtest saw future data)
│   └── LeakageTrapPassedError          (the planted look-ahead strategy
│                                        reported a profit — the guarantee
│                                        itself is broken)
│
├── SilentWrongnessError                → S1. Alert, non-suppressible.
│   ├── UnitMismatchError               (ETH/USD, wei/ether confusion)
│   ├── PrecisionLossError
│   ├── AssumptionViolationError        (model diagnostics failed)
│   └── InsufficientSampleError         (below the §8.2 minimum)
│
├── DataAvailabilityError               → S2. Record the gap. Backfill via
│                                        REST where the events endpoint
│                                        allows (REQ-D-09); NEVER interpolate,
│                                        forward-fill, or synthesize a value.
│   ├── StreamGapError
│   ├── IrrecoverableGapError           (event class REST cannot backfill)
│   └── BackfillFailedError
│
├── RateLimitError                      → S2/S3 by persistence.
│   ├── BudgetExhaustedError
│   ├── GovernorBypassError             → S1 (a component called REST directly)
│   └── KeyExpiredError
│
├── OperationalError                    → S3. Normal failure handling.
│   ├── UpstreamUnavailableError
│   ├── SchemaConformanceError
│   └── PersistenceError
│
└── ConfigurationError                  → S3, or S1 if it silently changed a result.
```

### 6.1 Rules the hierarchy enforces

- **`DataIntegrityError` and its subclasses are never caught and swallowed.** They propagate to the supervisor, which halts ingestion. Any `except DataIntegrityError` that does not re-raise is itself an S1 defect, and CI greps for it.
- **`BacktestIntegrityError` never halts ingestion.** It halts backtests. Conflating the two turns a contained bug into a real data gap.
- **`InsufficientSampleError` is an error, not a warning.** The system refuses to produce a number it cannot support. This is the mechanism that makes "never a point estimate without uncertainty" structural rather than aspirational.
- **`GovernorBypassError` is S1** even though nothing broke, because a component that can call REST directly will eventually exhaust the budget and starve production ingestion, and the failure will look like an unrelated outage.
- **Every raise carries context**: what was expected, what was received, the record identifier, and the `ingestion_run_id`. An error message without the record ID costs an hour of searching.
