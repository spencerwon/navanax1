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
