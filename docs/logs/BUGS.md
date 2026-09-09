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

## Validator review of PR #1 — 2026-09-09 · MERGE WAS BLOCKED, NOW RESOLVED

Independent adversarial review by the `validator` agent (opus, not the author).
15 findings, 5 blocking. **Every blocker was in code that was 66/66 green and
had passed a live smoke test against the OpenSea API.**

All five are now fixed, plus the must-settle zstd question (BUG-010). Each fix
carries a regression test that was **demonstrated to fail against the unfixed
code** before being accepted — see `docs/logs/bugs.yaml` for the per-bug detail
and `docs/logs/BUGS.xlsx` for the sortable view.

> Reverted-code run of the new tests: **14 assertions failed**, including
> `clean close: backoff is applied` — which reported **5,965 reconnect attempts
> in 0.4 seconds** on the old clean-close path.

| ID | Sev | Summary | Regression test |
|---|---|---|---|
| BUG-20260909-005 | S1 | A rejected `phx_join` was invisible | `test_join_reply_is_read` |
| BUG-20260909-006 | S1 | REQ-D-26a's time-based flush did not exist | `test_idle_flush_cadence` |
| BUG-20260909-007 | S2 | `verify_manifest` could not see orphaned files | `test_verify_sees_orphans_and_open_files` |
| BUG-20260909-008 | S1 | Clean close: no gap, no backoff | `test_clean_close_records_gap_and_backs_off` |
| BUG-20260909-009 | S1 | No gap recorded across a restart | `test_restart_records_downtime_gap` |
| BUG-20260909-010 | S1 | zstd could silently return only the first frame | `test_codec_multiframe_contract` |

### BUG-20260909-010 · S1 · ING · fixed

| | |
|---|---|
| **Summary** | `ZstdCodec` could silently return only the FIRST FRAME of a multi-frame file, depending on the installed `python-zstandard` version |
| **Detected by** | `validator`, raised as must-settle-before-merge |
| **Location** | `src/navanax/codec.py:160`, `pyproject.toml:12` |
| **Data impact** | `stream_reader`'s `read_across_frames` defaulted to **False** before python-zstandard 0.23.0, and `pyproject.toml` pinned `>=0.22.0`. On an older install, `read_file` on a healthy 64 MB production `.zst` would return roughly the first five seconds of it, raise nothing, and verify clean — the manifest checksum is over the **compressed** bytes and would still match |
| **Status** | fixed |
| **Resolution** | The dependency on the flag is gone: the codec walks frames itself, validating each candidate boundary rather than trusting a magic-byte search. `verify_codec_roundtrip()` writes a multi-frame file, reads it back, truncates it and recovers it — **run at `navanax ingest` startup**, so the process refuses to start rather than recording data it cannot read back. `pyproject.toml` also pinned to `>=0.23.0`, as a second line rather than the only one |
| **Regression test** | `tests/selftest.py::test_codec_multiframe_contract` |
| **Monitor gap** | The self-test used gzip exclusively; the production codec had **zero** coverage. The sandbox where the code is written cannot install `zstandard`, so no amount of local testing could have caught it. The startup self-check exists because the test suite structurally cannot |

### Round 2 — the Validator and the Tech Lead reviewing the FIXES

Every finding below was **introduced or left unbuilt by the fixes for**
**BUG-005…010**. That is the reason they carry IDs rather than code comments:
the tech lead's process finding was that V16 was raised because findings lived
as untracked prose, the fix gave IDs to those eight, and the round-2 findings
then became a *new* set of untracked findings. `tools/buglog.py --check` now
fails on any `BUG-` id referenced anywhere in the repo that is absent here —
which is literally how these entries came to be written.

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-020 | S1 | P0 | fixed | verify_manifest still could not detect a decoder returning a prefix of an intact file |
| BUG-20260909-021 | S1 | P0 | fixed | BUG-005's fix made control frames count as market events in the manifest |
| BUG-20260909-022 | S1 | P0 | fixed | Manifest read-modify-write had no cross-process lock; concurrent writers silently lost gap records |
| BUG-20260909-023 | S1 | P0 | fixed | A live gap was filed under one day and never recorded when it ended; only the restart path was fixed |
| BUG-20260909-024 | S1 | P0 | fixed | A chance frame-magic inside compressed data could silently truncate a file if the zstd binding returned partial data |
| BUG-20260909-025 | S2 | P1 | fixed | Join rejections re-opened a gap on every reconnect; measured 4,000 open gaps and 89.6s of fsync inside the event loop |
| BUG-20260909-026 | S3 | P1 | fixed | stats.heartbeats was always 0 in production; the test fixture asserted the frame we SEND, not the one the server sends |
| BUG-20260909-027 | S2 | P1 | fixed | Ingest refused to start on any filesystem without flock, reporting a second instance that did not exist |

**BUG-023 is the one worth reading.** The Tech Lead caught what both the
Validator and I missed: my multi-day-gap test called `record_gap` with *both*
endpoints known, which production never does. It proved the helper, not the
path. Driving the real consumer showed a Friday-to-Monday disconnect still
producing one day's manifest and a gap saying `ended_at: null` forever — a
reader could not tell a three-second reconnect from a lost weekend. That is
the tech-lead charter's own blocking criterion — *"a test whose assertion
would pass against a broken implementation"* — applied to its author.

### Fixed this round, no longer open

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-011 | S3 | P1 | fixed | pyproject declared requires-python >=3.12 while the operator's actual environment is Python 3.10 |
| BUG-20260909-014 | S1 | P1 | fixed | BUG-003's 5x rate-limit error survived in three operator-facing artifacts |
| BUG-20260909-015 | S3 | P2 | fixed | config/base.yaml rest_budget was parsed by nothing; the governor hardcoded capacity |
| BUG-20260909-019 | S3 | P0 | fixed | CI's pytest step collects zero tests and exits 5, so the pipeline cannot go green |

### Still open

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-012 | S1 | P1 | open | One 429 permanently bricks the REST governor; server_reset_at is parsed and never read |
| BUG-20260909-013 | S1 | P1 | open | Every gap is recorded backfillable=True even for event classes REQ-D-09a says are permanently unrecoverable |
| BUG-20260909-016 | S3 | P3 | open | Governor priority queue is dead code; ordering is emergent from reserve floors |
| BUG-20260909-017 | S1 | P1 | open | An event with no parseable event_timestamp lands silently and drops out of manifest-driven range queries |
| BUG-20260909-018 | S3 | P2 | open | A landing-zone write failure (disk full) is misrecorded as a stream gap and retried forever |

All six remaining open items are **inactive**: nothing in Phase 0 makes a REST
call, so BUG-012 and BUG-016 cannot bite yet. BUG-013 is the least comfortable
— the landing zone is append-only, so every gap written before it is fixed
carries a known-wrong `backfillable` flag permanently.

### The lesson

> The 66 assertions cover the gzip writer's happy path and constants. They do not
> cover the zstd path at all, any restart, any idle period, any orphan file, any
> join outcome, or any control frame — which is why all five blocking findings are
> in code that is 66/66 green.

**Process changes made in response:**

1. `tech-lead` (L3, opus) — a second gate. Validator hunts defects; Tech Lead
   checks coherence, requirement traceability, and whether a finding was really
   fixed or dodged. `docs/02_AGENT_HIERARCHY.md` §3.6, WF-D-01.
2. `qa-auditor` (L2, sonnet) — reconciles what was *said* against what is *in
   the repo*, continuously. §3.7.
3. This log is now generated from `docs/logs/bugs.yaml`, and
   `tools/buglog.py --check` fails CI if a bug is marked fixed while the
   regression test it names does not exist.
