# Bug Log

Append-only. Format and rules: `docs/05_BUG_TAXONOMY.md` §4.
New entries are appended by `bug-triage`. Lifecycle fields are updated by the agent that merges the fix.

---

### BUG-20260909-001 · S1 · CFG · fixed

| | |
|---|---|
| **Summary** | Entry points read only `os.environ`; never loaded `.env` they told users to fill |
| **Logged** | 2026-09-09T01:05:00-05:00 |
| **Occurred** | 2026-09-09T00:38:00-05:00 (when `tools/preflight.py` was written) |
| **Detected by** | Spencer, running `setup.command` on his own machine |
| **Branch** | `feat/phase0-ingestion` |
| **Location** | `tools/preflight.py:L236`, `src/navanax/cli.py:L50` |
| **Data impact** | **None.** No data had been ingested yet; both affected paths are read-only and abort before writing. No records exist to be wrong |
| **Status** | fixed |
| **Resolution** | Added `src/navanax/dotenv.py` and wired both entry points to it |
| **Regression test** | `tests/selftest.py::test_dotenv_loading` |
| **Monitor gap** | **Yes, and it is the interesting part.** No test ever exercised a real entry point end to end. 49 assertions covered the internals; zero covered "does a first-time operator get past step one." A first-run smoke test now exists |

**Detail.**

`tools/preflight.py` and `src/navanax/cli.py` both obtained the API key with
`os.environ.get("OPENSEA_API_KEY")` and nothing else. Neither ever opened `.env`.

Meanwhile the failure message printed `"(or put it in .env)"`, `.env.example`
existed for the purpose, `.gitignore` line 2 excluded it, `04_ENVIRONMENTS.md`
§6.2 specified it, and `setup.command` told the operator on screen: *"Reads your
key from .env — you don't type it."*

Every artifact in the project agreed on a behaviour the code did not have. The
operator filled in `.env` correctly — 32 characters, plain text, valid — and was
told his key was not set, with the remedy being the thing he had already done.

This is the failure mode `05_BUG_TAXONOMY.md` §1 describes: nothing crashed and
nothing was visibly broken. The code did exactly what it said in its own source.
The defect was that the source disagreed with every instruction wrapped around
it, and only a human running it for real could see that.

Rated S1 rather than S3 because the misleading error text was the harmful part:
it sent the operator to re-check a correct file, which costs more than a blank
failure would have.

**Fix.** `src/navanax/dotenv.py` — a 40-line loader, no third-party dependency
(one fewer install on a first run that has to work). Handles the macOS-specific
traps: **TextEdit silently saving Rich Text**, which looks correct on screen and
is undetectable without checking the bytes; plus BOM, CRLF, quoted values,
`export ` prefixes, and inline comments. A real environment variable still wins
over the file unless `override=True`.

---

### BUG-20260909-002 · S3 · ING · fixed

| | |
|---|---|
| **Summary** | Stream consumer assumed Phoenix v1 map frames; OpenSea sends v2 arrays |
| **Logged** | 2026-09-09T02:20:00-05:00 |
| **Detected by** | Spencer, first live WebSocket connection |
| **Location** | `src/navanax/stream.py` `handle_frame`, `tools/preflight.py` `check_stream` |
| **Data impact** | **None.** No ingestion had run. The consumer would have crashed on its first frame, loudly, before writing anything |
| **Status** | fixed |
| **Resolution** | Added `normalize_frame()` handling both wire formats; both call sites route through it |
| **Regression test** | `tests/selftest.py::test_phoenix_v2_arrays` (8 assertions) |
| **Monitor gap** | No test used a real frame. The synthetic fixtures were all v1 maps because that is the format I wrote them in — the test data inherited the bug from the code it was testing |

**Detail.** `AttributeError: 'list' object has no attribute 'get'`, thrown on the
very first frame the server sent (the join reply).

Phoenix Channels ships two serializers. v1 sends maps; **v2 sends arrays** —
`[join_ref, ref, topic, event, payload]` — because arrays are smaller on the
wire. OpenSea uses v2. Our code called `.get()` on a list.

Rated S3, not higher: it is a loud crash that writes nothing. The `TMP`/`NRM`
classes are the dangerous ones; this is the harmless kind of wrong.

Worth noting what it cost us anyway: **the join was never confirmed**. The crash
happened before parsing the reply status, so we still do not know whether OpenSea
accepted the subscription. That is the next thing to establish.

---

### BUG-20260909-003 · S1 · CFG · fixed

| | |
|---|---|
| **Summary** | Documented REST budget of 600/hr was an unverified assumption; measured limit is 120/hr |
| **Logged** | 2026-09-09T02:25:00-05:00 |
| **Occurred** | 2026-09-09T00:15:00-05:00 (when 600 first entered the requirements) |
| **Detected by** | Spencer, first live API response — `x-ratelimit-limit: 120` |
| **Location** | `config/base.yaml`, `src/navanax/governor.py`, and every doc citing 600 |
| **Data impact** | **None** — no ingestion had run. But every budget calculation in the document set was wrong by 5× |
| **Status** | fixed |
| **Resolution** | Config default 120, reserve floors rescaled 0/4/12/24, docs corrected |
| **Regression test** | `tests/selftest.py::test_governor_budget` rescaled to 120 |
| **Monitor gap** | **The number was never measured.** It was asserted early, repeated in seven documents, and cited so often it read as established. Nothing in the process required a figure to have a source |

**Detail.** Every document stated 600 reads/hour. The first live response said:

```
x-ratelimit-limit: 120
x-ratelimit-remaining: 119
```

**Five times tighter than planned.** Consequences:

| Operation | at assumed 600/hr | at measured 120/hr |
|---|---|---|
| Onboard Argonauts (49 reads) | 8% of an hour | **41% of an hour** |
| Hourly reconcile, 200 collections | 33% | **167% — impossible** |
| Daily rotation, 200 collections | 1.3% | 6.7% |

The stream-first architecture is not merely validated by this — it is the only
thing that makes the project viable at 120/hr. Had we designed around REST
polling, this measurement would have ended the project.

**Two caveats, both material.**

1. That response carried `cf-cache-status: HIT`. It came from Cloudflare's
   cache, so the headers may be a **cached** reading rather than a live one.
   `x-ratelimit-reset` was 22 seconds *before* the request's own `Date`, which is
   consistent with a stale header. 120 is therefore a *provisional* measurement.
2. The governor reads `x-ratelimit-limit` on every response and adapts, so the
   config default only matters until the first uncached reply. Setting it low is
   the safe direction to be wrong in.

**The real lesson is the monitor gap.** The 600 figure was never sourced. It
appeared once, was repeated across seven documents, and its ubiquity became its
credibility. One real request falsified it. Numbers in these documents now carry
their provenance — `MEASURED <date>`, `ASSUMED`, or `FROM DOCS <url>` — so an
unsourced figure is visible as unsourced.

---

### BUG-20260909-004 · S4 · CFG · fixed

| | |
|---|---|
| **Summary** | CI lint failed on first PR: 43 ruff errors; ruleset was unpinned and `tools/` unlinted |
| **Logged** | 2026-09-09T02:45:00-05:00 |
| **Detected by** | GitHub Actions CI on PR #1 — working exactly as designed |
| **Location** | `pyproject.toml` `[tool.ruff]`, `.github/workflows/ci.yml` |
| **Data impact** | **None.** Style only; no behavior change and nothing had ingested |
| **Status** | fixed |
| **Resolution** | Removed 4 unused imports, wrapped 8 long lines, narrowed 5 broad excepts, pinned the ruleset, extended lint to `tools/` |
| **Regression test** | CI itself. `tests/selftest.py` still 66/66 |
| **Monitor gap** | Two: the ruleset was unpinned (`[tool.ruff]` with no `select`, so behavior tracks whatever ruff version CI installs), and `tools/` was never linted at all |

**Detail.** First PR, first CI run, red. Good — that is the gate doing its job on
the very first use.

Three real classes underneath the noise:

1. **Unused imports** (`gzip`, `io`, `RawCodec`, `GapRecord` in the test file) —
   leftovers from refactoring. Harmless, but they make a reader think a
   dependency exists where it does not.
2. **Long lines** — cosmetic.
3. **Broad `except Exception`** — the one worth thinking about.

**On the broad excepts.** Copilot suggested replacing
`except (asyncio.CancelledError, Exception): pass` with
`except asyncio.CancelledError: pass`. That fixes the lint and **makes the code
worse**: a heartbeat task that died for a real reason would then propagate from
inside a `finally`, masking the original connection error that we actually want
to diagnose. Taken instead: catch `CancelledError` (expected, silent) and log
anything else. Cancellation is not an error; a real failure is a clue.

Two broad catches were kept deliberately, with `noqa` and a written reason:

- `landing.read_file` — *any* decompression failure means damaged or truncated
  data, and every codec raises a different type. Narrowing it would silently
  lose data the frame-recovery path (REQ-D-26a) exists to save.
- `stream._run_once` — reconnect on anything, by design.

The rest were narrowed to real types (`json.JSONDecodeError`,
`zstandard.ZstdError`, `TimeoutError`, `NavanaxError`).

**The two monitor gaps are the durable lesson.** `[tool.ruff]` set only
`line-length` and `target-version` with no `select`, so the effective ruleset was
whatever ruff's defaults happen to be in the version CI installs — meaning a
green build can go red on a day nobody touched the code. Now pinned explicitly.
And `ruff check src tests` never covered `tools/`, which is where `preflight.py`
lives — the file the operator runs first.

---

## Validator review of PR #1 — 2026-09-09 · MERGE BLOCKED

Independent adversarial review by the `validator` agent (opus, not the author).
15 findings, 5 blocking. Full detail in the review; blockers logged individually
below. **Every blocker is in code that is 66/66 green and passed a live smoke
test against the OpenSea API.**

### BUG-20260909-005 · S1 · ING · open · BLOCKING

| | |
|---|---|
| **Summary** | A rejected `phx_join` is invisible: control frames return before landing, and the reply is never read |
| **Location** | `src/navanax/stream.py:256-257`, `:343-345` |
| **Data impact** | **Potentially total.** Expired key or bad slug → the process runs for days at 0 events with `open gaps 0` and healthy stats. Every event in that window is lost, and the evidence of *why* was never written to disk |
| **Monitor gap** | Nothing asserts that a subscription was accepted. BUG-002's own closing note said confirming the join was "the next thing to establish" — and it still is not established in code |

### BUG-20260909-006 · S2→S1 · ING · open · BLOCKING · **worst of the five**

| | |
|---|---|
| **Summary** | REQ-D-26a's time-based frame flush does not exist |
| **Location** | `src/navanax/landing.py:303-310` |
| **Data impact** | `_maybe_flush_frame` is reachable only from `write()`. Repro: one event, then 7200 simulated idle seconds → **0 bytes on disk**, no manifest, and after a simulated SIGKILL `read_file` recovered **0 events** while `verify_manifest` returned "clean" |
| **Monitor gap** | The self-test drives the flush by writing events. It never tests the *idle* case, which is the normal regime for a thin collection |

**Why this is the most dangerous.** Preflight saw zero events in 60 seconds on
Argonauts. An `item_cancelled` arrives, the laptop sleeps — the event is gone,
and `item_cancelled` is in the IRRECOVERABLE set (REQ-D-09a) that REST can never
return. That is the exact event class observed live today. The docstring's
"bounding crash loss to seconds" is false: loss is bounded by the inter-event
interval, which is unbounded.

Same shape as BUG-001 and BUG-003 — requirement, docstring, and bug log all
agreed on behaviour the code did not have.

### BUG-20260909-007 · S2 · ING · open · BLOCKING

| | |
|---|---|
| **Summary** | `verify_manifest` walks manifest→disk only; complete files with no manifest entry are invisible and reported clean |
| **Location** | `src/navanax/landing.py:402-432`; `FileRecord` written only in `_close_current` (`:358`) |
| **Data impact** | Killed before `close()` → 553 bytes, 6 events readable, `_manifest/` empty, `cli verify` prints "landing zone verified clean." Every ungraceful termination orphans its final file, permanently unreachable via the manifest path `ManifestWriter` documents as mandatory |

### BUG-20260909-008 · S1 · ING · open · BLOCKING

| | |
|---|---|
| **Summary** | A clean WebSocket close records no gap and reconnects with zero backoff |
| **Location** | `src/navanax/stream.py:319-336` — `_open_gap` is called only from `except Exception` |
| **Data impact** | OpenSea drops idle subscriptions gracefully; `async for` then returns normally. Repro: `reconnects` = 0, gap records = **0**, and the loop spun hot enough that a concurrent `asyncio.sleep(0.2)` never resumed. REQ-D-14 is satisfied only on the crash path |

### BUG-20260909-009 · S1 · ING · open · BLOCKING

| | |
|---|---|
| **Summary** | No gap is recorded across a process restart; the checkpoint table is never written by production code |
| **Location** | `opstore.save_checkpoint` (`:149`) and `log_rest` (`:202`) are called only from `tests/selftest.py`; `cli.py:45-96` calls neither |
| **Data impact** | Stop Friday, restart Monday → `open gaps 0`, a 72-hour hole with nothing marking it and no `last_event_ts` to bound it. Downstream reads a continuous record. §2.1 escalation: an unrecoverable gap spanning analysed data is S1 |

### Non-blocking, tracked

- **S3→S2** one 429 permanently bricks the governor: `server_remaining` is set to 0 and only a successful response can raise it, which requires a slot. `server_reset_at` parsed and never read
- **S1** `IRRECOVERABLE` referenced only by a test; every gap recorded `backfillable=True`, contradicting `GapRecord`'s own docstring. `_close_gap` logs that backfill was enqueued — nothing is enqueued
- **S1** `preflight_report.json` still writes `est_minutes_at_600_per_hour` — BUG-003's 5× error survives in the machine-readable artifact the docs are corrected from
- **S1** BUG-003 marked `fixed` while `README.md:33` and `setup.command:48` still say 600 — the two files the operator reads first
- **S3** `config/base.yaml` `rest_budget` block is parsed by nothing; values live in code (REQ-N-09 violated)
- **S3** governor `_Waiter`/`_waiters`/`_seq` are dead; ordering is emergent from reserve floors, so MAINTENANCE can beat an earlier INTERACTIVE
- **S1** an event with no parseable `event_timestamp` lands silently and drops out of every manifest-driven range query
- **S3** a landing-zone write failure (disk full) is misrecorded as a stream gap and retried forever

### Must settle before merge

`ZstdCodec.decompress` uses `stream_reader` without `read_across_frames`, which
defaulted to **False** before python-zstandard 0.23.0. `pyproject.toml` pins
`>=0.22.0`. If an install lands on the old default, `read_file` on a healthy
production `.zst` returns **only the first frame** — no exception, verifies
clean. Not executable in the sandbox. Pin `>=0.23.0`, pass the flag explicitly,
and assert a ≥3-frame round-trip.

### The lesson

> The 66 assertions cover the gzip writer's happy path and constants. They do not
> cover the zstd path at all, any restart, any idle period, any orphan file, any
> join outcome, or any control frame — which is why all five blocking findings are
> in code that is 66/66 green.

Process change: `tech-lead` (L3, opus) added as a second gate. The Validator
hunts defects; the Tech Lead checks coherence and requirement traceability.
`docs/02_AGENT_HIERARCHY.md` §3.6, WF-D-01.
