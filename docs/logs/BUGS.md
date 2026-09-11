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

### Round 3 — the QA Auditor reconciling claims against the repo

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-028 | S1 | P1 | fixed | The falsified 600/hr figure survived, unqualified, in five agent charters -- and BUG-014's own test could not see them |

**Third time for this number.** BUG-003 found it. BUG-014 removed it from the
three files the operator reads. The QA Auditor found it still stated as fact in
**five agent charters** — the instructions the agents act on — including
`data-engineer.md`, where it was headed *"the constraint that shapes everything
you build"*, in the charter of the agent that owns the REST governor.

The reason nobody looked: BUG-014's own ledger entry claimed its test *"scans
every operator-facing file… so this class cannot recur silently."* It scanned a
hardcoded list of seven. **The false claim of coverage is what did the damage** —
it turned an unexamined gap into a settled question. The scan is repo-wide now.

### Round 4 — the full hardening pass

Spencer chose to close every remaining open bug before starting the ingestion
clock, rather than record with known-wrong flags in an append-only store. All
five are closed, each with a regression test **demonstrated to fail against the
unfixed code** (10 assertions failed on the reverted tree).

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-012 | S1 | P1 | fixed | One 429 permanently bricks the REST governor; server_reset_at is parsed and never read |
| BUG-20260909-013 | S1 | P1 | fixed | Every gap is recorded backfillable=True even for event classes REQ-D-09a says are permanently unrecoverable |
| BUG-20260909-016 | S3 | P3 | fixed | Governor priority queue is dead code; ordering is emergent from reserve floors |
| BUG-20260909-017 | S1 | P1 | fixed | An event with no parseable event_timestamp lands silently and drops out of manifest-driven range queries |
| BUG-20260909-018 | S3 | P2 | fixed | A landing-zone write failure (disk full) is misrecorded as a stream gap and retried forever |

**BUG-013 was the one that justified the decision.** The landing zone is
append-only, so every gap written before the fix would have permanently claimed
it was backfillable — including windows where cancellations and order
invalidations are gone for good. Not editable later, only annotated.

**BUG-012 had a second half nobody had read.** `observe_success` cleared the
new expiry but left `server_remaining = 0`, so the block expired and the ceiling
did not. The regression test found it; reading the code had not.

### Round 5 — the Tech Lead on the hardening pass

Blocked a third time. All three findings were **introduced or left standing by
the hardening pass itself**.

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-029 | S2 | P0 | fixed | config/base.yaml said the governor reads x-ratelimit-limit and adapts; nothing read it |
| BUG-20260909-030 | S2 | P0 | fixed | BUG-012's fix discarded a truthful "remaining 0" reported on a successful response |
| BUG-20260909-031 | S2 | P0 | fixed | BUG-013 redefined backfillable and made the backfill worklist permanently empty |

**BUG-030 was caught twice.** The Tech Lead found that `observe_success`
discarded a truthful `remaining: 0`. The first attempt at fixing it made the
block *expiry* do the same thing — and the pre-existing assertion *"server
header caps local optimism"* failed the moment it was written. The old suite
caught the new fix's flaw.

### Round 5 — CI runs the real compressor for the first time

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-032 | S1 | P0 | fixed | First real-zstandard CI run turned the codec gate red — the gate's "last frame" heuristic assumed gzip's writer behaviour, and the frame walk still guessed boundaries by magic scanning |

**The first time the production codec was executed by anything.** Neither
development environment can install `zstandard`, so every assertion about it
had been reasoning. Two of those assertions were wrong: real zstd appends an
empty frame on `close()` (gzip does not), and `decompressobj()` reports frame
boundaries directly — no magic scanning needed. The gate refused in the safe
direction, but for the wrong reason. Fixed at the root: the walk now trusts
the decoder's `eof`/`unused_data`, exactly as the gzip path always has, and
the gate has its own regression test proving it refuses three lying codecs.

### Round 6 — CI reaches steps that had never run

CI passed the codec contract on real `zstandard` — **the first executed proof
the production compressor works** — and then reached two steps that had never
executed before.

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-033 | S3 | P0 | fixed | The REQ-N-11 credential gate fired on a 14-character test placeholder the first time it ever ran |
| BUG-20260909-034 | S3 | P1 | fixed | CI actions target Node 20, which GitHub removes from runners on 2026-09-23 — two weeks out |

A security gate with no precision has no authority: one that fires on
`fake_key_value` will be clicked past on the day it fires on a real key. The
grep is now a checker that knows what a key looks like, with its own test —
and `tests/` is not exempt from it.

### Round 7 — the first double-click

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-035 | S3 | P0 | fixed | First live run failed CERTIFICATE_VERIFY_FAILED seven times — python.org macOS Python ships no root certificates, and the consumer blamed the stream |

Everything the code did was right by its own rules: one gap, correct
backoff, correct class labelling. The rules were aimed at the wrong diagnosis.
Same family as BUG-018 — a local problem in an upstream error's clothes. The
consumer now says so once, in words that name the fix, and `certifi` makes the
fix unnecessary on a fresh install.

### Round 8 — the first two minutes of real data

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-036 | S1 | P0 | fixed | Storage sizing assumed 2,000 events/day for Argonauts; measured steady-state rate is ~48/second |
| BUG-20260909-037 | S3 | P1 | fixed | Closing the Terminal window killed ingestion without a clean stop |
| BUG-20260909-038 | S1 | P0 | fixed | The self-test ran from a hand-maintained list; three tests were written, reviewed, and never executed |

**BUG-036 is BUG-003 again, three orders of magnitude bigger.** An unsourced
estimate, repeated until it read as fact, falsified by the first real
measurement — 7,240 events in 152 seconds, 51% bids and 46% cancellations,
from ten makers of which three placed 88%. The stream-first architecture is
vindicated harder than anyone argued for: nearly half this market's activity
cannot be fetched by any REST call at any budget.

**BUG-038 is the uncomfortable one.** The codec gate's self-test was named in
the ledger as BUG-032's regression test, the ledger check confirmed it existed,
and it had never run. The ledger's own evidence had the claim-versus-reality
gap the ledger exists to catch. Test discovery replaces the list.

### Round 9 — the dashboard, validated against real data before shipping

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-039 | S1 | P0 | fixed | `payment_token.eth_price` is the order's value on bids/listings but the token's rate on sales — a trusting parser records a 1.43 ETH sale as 1.00 |
| BUG-20260909-040 | S3 | P0 | fixed | Order-lifecycle joins used the wrong index; 2,000 bid lifetimes took 29 s and the dashboard hung |

Both caught by running the new code over **every real frame on the
operator's machine** (74,286) before it shipped — the check that BUG-002
taught and that is now how a parser gets accepted. First real numbers from
the same run: bids on Argonauts stand for a **median of 9 seconds** (p10 3.2 s,
p90 234 s, n = 38,286).

### Round 10 — the first time a human looked at the page

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-041 | S3 | P1 | fixed | Native `<select>` painted white with light text on macOS — `color-scheme: dark` was never declared |
| BUG-20260909-042 | S3 | P1 | fixed | Every on-screen time was UTC; docs/06 sets America/Chicago and the page never read it |
| BUG-20260909-043 | S4 | P1 | fixed | Hover cards illegible (light on light); USD rounded to whole dollars |

All three were found by Spencer in his first minute with the page, none by
an agent — the page was built and tested in a Linux container and never
rendered in the browser it was for. The fix beyond the code: a **design-lead**
role that owns the look, screenshots the page on the target machine before
handover, and asks the operator design questions (colour, units, hover,
chart style) as their own thread rather than defaulting them.

### Round 11 — the tech-lead gate on the trait/screener PR

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-044 | S1 | P0 | fixed | Series omitted empty buckets instead of returning null — charts bridged unobserved hours while the footer claimed holes |
| BUG-20260909-045 | S1 | P0 | fixed | Trait offers passed every trait filter; a filtered spread mixed a filtered ask with a collection-wide bid without saying so |
| BUG-20260909-046 | S1 | P0 | fixed | Trait loader fetched `metadata_url` with no host check — an OpenSea-hosted collection would have bypassed the governor 9,212 times |
| BUG-20260909-047 | S1 | P1 | fixed | "REST reads spent" counted calls, not attempts; retries under-reported the budget up to 4× |
| BUG-20260909-048 | S2 | P0 | fixed | Third-party strings (trait values, names, token ids, wallets) rendered as raw HTML |

All five found by the **tech-lead** reviewing the branch before the PR was
opened — the gate doing what it was added for in Round 3. Three of the five
fail *plausibly*: nothing crashes, the chart looks right, and the error is in
the optimistic direction. That is the shape the project rules call a
surprisingly good result. The fixes came with 30 new assertions, including
the zero-match filter, the retried 429, and a screener-vs-live-book
agreement check on one store with cancelled, expired and filled orders.

### Round 12 — the standing book (PR-2 / PR-4)

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-049 | S1 | P0 | fixed | `bid_lifetimes` cross-produced bids against cancels — `n = 38,286` was a count of *pairs*, not of bids |
| BUG-20260909-050 | S1 | P0 | fixed | The `dead` predicate ignored `order_revalidate` and `quantity`, and never matched a NULL-`valid_ts` terminator |
| BUG-20260909-051 | S1 | P0 | fixed | Trait-offer criteria were parsed by nothing, so every trait offer was blanket-excluded under every filter |

All three were found by **reading**, not by running: quant-research raised 049
and 050 as T1 and T3, the data-engineer proposal raised 051, and the tech-lead
fact-check confirmed each against the source before any code was written. None
of them could have been found by running the suite, because the suite was green
and the fixtures contained no duplicate terminator, no revalidate (1 frame in
85,775), no untimed cancel and no quantity above 1. **Rarity is what made them
invisible**, and rarity is not the same as harmlessness: a revalidate that is
never seen is depth missing from the book, and an untimed cancel is a price on
the screen that cannot be hit.

The root fix is one relation. `order_lives` — one row per `order_hash`, with
`t_place`, `t_term`, `exit_reason`, `quantity`, `placement_seen` — replaces the
**four** different notions of "ended" that were live in `metrics.py`
(`cancel_count` counted two event types, `bid_lifetimes` one, and the `dead`
subquery listed three, in two copies). `standing_sql()` is now the only
predicate, and the live book, the screener and the bid-lifetime panel all read
it.

Two numbers in this log are now known to be wrong and are kept for the record:
**median bid life 9 s, n = 38,286** (Round 9) came from the defective estimator
and is biased in both directions at once. When the panel is rebuilt on real
data the median may move **either** way. That is the expected consequence of two
known defects — not a discovery, and not a new bug.

### Round 13 — the gate on PR-1 (autostart) and PR-2/4 (order lives, criteria)

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-052 | S1 | P0 | fixed | A gap left open by a dead run was never closed — one stale record masked every chart to infinity; launchd restarts made it routine |
| BUG-20260909-053 | S1 | P0 | fixed | `order_lives` inferred expiry at a time that had not arrived — standing orders recorded as ended, lifetimes biased long |
| BUG-20260909-054 | S3 | P1 | fixed | The collection list endpoint carries `traits`; the list pass discarded them and left 9,161 tokens to a ~76-hour per-token fallback |

Both found by the tech-lead reproducing the change by running it, both in the
flattering direction (a quiet market; a durable book), both fixed the same hour
with a test that failed first.

Found by reading a second tool's scraper against ours. `SpencerTinker/scrape.py`
reads `nft["traits"]` from the SAME `GET /collection/{slug}/nfts` pages that
`traits.py` walked on 2026-09-09, and got traits for all 8,798 indexed
Argonauts in 44 reads. Our docstring said the field was not there; the code
believed the docstring; the fixture was written from the docstring. The
47 reads were spent and the field thrown away. Fix: store list-carried
traits immediately (`opensea_nft_list`), keep the metadata_url / fallback
paths only for entries without them, and add `navanax import-traits` so the
friend's cache loads with zero reads. Docstring now states what is verified
and for which collection; other collections must be re-verified.

### Round 14 — the gate on the trait-cache import

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260909-055 | S1 | P0 | fixed | The trait-cache import silently overwrote — duplicate ids, counts of writes that never happened, reconciliation on the wrong key, and any float accepted as the observation time |
| BUG-20260910-056 | S3 | P1 | fixed | `config/assumptions.yaml`, the assumptions registry, was deleted by an unrelated change and no gate noticed |

Four defects in one import path, all the same habit: trusting an identifier or a
number without resolving it first. Two cache entries resolving to one token id
both wrote, second wins, `imported` counting both — the cache's own internal
contradiction becoming our record with no trace. The list pass counted
`traits_from_list` for every entry that *carried* traits, including the ones
`_store` refused to write, so the number reported was the response's size and
not the store's gain; a conflicting OpenSea value was dropped uncounted in the
same line. Reconciliation compared the cache's *keys* against rows written under
`entry["id"]`. And `--generated` took any float: a millisecond epoch — which is
what the JavaScript tool that produced the cache emits by default — crashed with
a raw `ValueError`, and a future one was written straight into `traits_at`,
where nothing downstream can tell it from a real observation.

None of it had run against the real cache yet. The import's own fixture was
written from the same assumptions as the code — keys that *were* the ids, all
distinct, a plausible second-epoch integer — so it could not falsify any of the
four. The BUG-002 shape for the fifth time.

`config/assumptions.yaml` is the smaller finding with the more uncomfortable
cause: a file no code imports and no test asserts on is invisible to every gate
this project has, so deleting it passed all four green.

### Round 15 — the gate on PR-3 (the standing-book spread)

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260910-057 | S1 | P0 | fixed | `immediacy_cost` was an interval extremum, not the standing-book quantity docs/01 §3.2 defines — biased narrow, and able to render negative on the front page |

The metric was built from its name rather than from its definition. docs/01 §3.2
says *"what you pay to force time-to-clear to zero by hitting the **standing**
collection offer"*. The code aggregated `MIN` over the ask leg and `MAX` over the
bid leg **within a bucket** and subtracted them, so the number on the front page
was the distance between the cheapest ask seen at some point in the interval and
the richest offer seen at some other point — two prices that need never have
coexisted, and on this collection usually did not.

The bias has a known sign — too **narrow** — and it grows with the interval, so
the 1-day bars were worse than the 5-minute bars in the way nobody would notice.
And because the legs are unrelated in time it could go **negative**, which the
KPI card would have rendered as a free arbitrage. On the fixture that reproduces
it the old path returns **−0.10** and the new one returns **null with an alarm**.

The fix is not a chart fix. `immediacy_cost`, `floor_ask` and `collection_bid`
are now read off `order_lives` by a sweep line over placements and terminations,
and reported per bucket as the **time-weighted median with [p10, p90]**, with
`coverage` (seconds the legs stood ÷ observable bucket seconds) and `n` beside
every point. Both legs are sampled at the same τ. The Operator's decision of
2026-09-10 — *floors are built from standing asks only; a bucket with no live ask
is a hole* — is what the default now implements; the old behaviour stays
reachable as `book='observed'` and says in its own basis what it is not.

Shipped with it: percentiles are **withheld** below `min_n_for_percentiles`
rather than printed with a warning beside them (REQ-F-19, docs/00:199). All
three of `bid_lifetimes`' quantiles go together — a median is a percentile too.
The standing series' median does not, and the distinction is deliberate: it is
the level of a continuously observed step function, a fact at n = 1, and
withholding it would blank the chart wherever the book is genuinely one listing
deep.

Every existing test asserted the metric against its own implementation — the
contract test restated the extremum definition, and the 5-minute case asserted
that two legs in different buckets are UNDEFINED, which is the defect written
down as intended behaviour. **A test written from the code cannot find a
requirement violation.**

### Round 16 — the gate that passed on zero evidence

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260910-058 | S1 | P0 | fixed | `sync()` reported an undecodable landing file as read-with-zero-rows; a corpus fold with no codec printed "115 files read, 0 rows, ALL GATES GREEN" |

Found by the orchestrator running the new `tools/gates.py --corpus` in the
device VM. Fix: failures and short files are counted in the sync stats and fail
the gate; `gates.command` runs the fold on the Mac where the codec lives.

### Round 17 — a bid line for a trait group with no members (PR-5)

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260910-059 | S1 | P1 | fixed | the trait chart's union bid leg drew the collection offer for a filter selecting **zero** tokens — a confident bid line for a trait group with no members |
| BUG-20260910-060 | S1 | P1 | fixed | the same shape in `_bucketed`: under a trait filter that selects zero tokens, `bid_count` / `event_count` still counted collection offers and COVERing trait offers |

Found by the data engineer smoke-testing PR-6's page against real PR-5 payloads
before opening the PR, on the filter case nobody draws on purpose: `Print: Nope`,
a clause with no matching token.

The ask lines went null on their own — no tokens, so no listings. The **bid** did
not. A collection offer is not token-scoped and `S(F) ⊆ S(C)` is vacuously true
when `S(F) = ∅`, so the panel drew **0.348 Ξ** as "the highest standing bid for
this trait" for a trait group with **zero** members. Every leg count was correct;
only the line was a lie, and it was a lie in the flattering direction — a bid
with no ask above it reads as a trait nobody has listed and somebody wants.

**It is reachable today on every filter.** `traits` has 0 rows in this working
copy and on the Operator's machine until the Explorer import lands, so *every*
filter has `S(F) = ∅`. The first thing the trait chart would have drawn, on its
first run, is this number.

Fixed in `trait_set_series`: when the filter is non-empty and `|S(F)| = 0`, the
bid series and `winning_leg` are null in every bucket, `basis.empty_token_set` is
true, and both the panel and the basis print *"this filter selects no token, so
there is no trait group … if that is unexpected, check whether the `traits` table
is populated at all."* The per-leg counts still report what **was** standing, so
the withholding is visible as a withholding rather than as an empty book.

**BUG-060 was the same defect one function away, and is fixed in the same pass.**
`_bucketed`'s filter clause is `((token_id IS NULL AND event_type='collection_offer')
OR (event_type='trait_offer' AND <COVERS>) OR (<token matches every clause>))`. Each
disjunct is right on its own; the *set* of them had no `|S(F)| > 0` condition, so
under a filter that selects no token the two token-less branches kept matching.
Measured on the fixture, before the fix, in the bucket holding the book:

| metric, filter selects 0 tokens | before | after |
|---|---|---|
| `bid_count` | **1.0** — the collection offer | `None` |
| `event_count` | **1.0** | `None` |
| `sales_count` · `cancel_count` · `listing_count` · `volume` | **0.0** | `None` |
| `top_item_bid` · `floor_ask` · `immediacy_cost` | `None` (already) | `None`, now with a reason |
| `basis.empty_token_set` | **absent** — nothing on the page could say why | `true` + a printed note |

The `0.0` cases matter as much as the `1.0` ones: a zero says *"nothing happened
to this trait group in this hour"*, and the truth is that there is no trait group.
And while `traits` is empty **every** filter selects zero tokens, so the `1.0` was
100 % of the filtered bid count — the Activity chart's *bids* bars under any trait
filter were counting bids on tokens the filter does not select.

The fix is one predicate, `filter_narrows(spec)`, used in the two places that were
about to disagree: `_bucketed` returns nothing, and `series()` fills the grid with
`None` rather than the usual `0.0` for COUNT/SUM. `collection_bid` is the
deliberate carve-out and is unchanged — it is not narrowed by a trait filter (leg
discipline, quant §1 metric 1) — but its basis now says the number is
collection-wide and is not a statement about the filter. The note prints under
**every** chart, not only the trait panel.

### Round 18 — the tech-lead blocks PR-5/PR-6

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260910-061 | S1 | P0 | fixed | the empty-token-set guard was on **one** of `series()`' four branches; the standing-book branches drew a line while the basis in the same response said every bucket was undefined |
| BUG-20260910-062 | S1 | P1 | fixed | `trait_offer_verdicts` re-queried `traits` once per (offer, criterion) — 27,694 queries for 48 distinct pairs, 8.6 s at 200k lives |
| BUG-20260910-063 | S2 | P2 | fixed | every PARTIAL offer travelled as full detail — 4.1 MB of JSON |

**061 is an incomplete fix to 060, and that is the interesting part.** The guard
was written where the defect was *found* — the `_bucketed` branch — rather than
around the value the guard is *about*. `series()` has four branches; three
ignored it, so `floor_ask` and `immediacy_cost` on the standing book drew a line
while `basis.empty_token_set_note`, in the same response, asserted that every
bucket was undefined. **A basis that contradicts its own arrays is worse than no
basis**, because the basis is the thing a reader falls back on.

It is reachable only when `tokens` and `traits` disagree, which is why it was not
obvious — and why the BUG-060 regression test could not see it. That test filtered
on a trait value **no token has**, so the standing series was null for lack of
data and the guard was never what made it null. The new fixture populates `traits`
and leaves `tokens` empty — a real intermediate state of trait onboarding — so the
book matches the filter while `|S(F)| = 0`. Before the fix, on that fixture:
`floor_ask` **1.20 Ξ**, `immediacy_cost` **0.852 Ξ**, and the trait chart's ask
line drawn, all with `empty_token_set: true` printed beside them.

> **A guard needs a fixture in which the thing it guards against is actually
> present.** When two tables can disagree, the test for a rule that spans them
> must make them disagree.

The same review corrected the note's wording (**F7**): it told the Operator to
check the `traits` table, and the guard counts `tokens`. On the very fixture that
exposed 061, `traits` was the populated half — so the note named the one table
that was fine. It now says the universe is `tokens` and that both are filled by
the same onboarding run.

**062** is cost, not correctness: the reach of a criterion `(trait_type, value)`
is a property of the trait table, not of the offer that names it. Memoised per
call, plus one grouped query for the criteria rows: fewer than 200 queries and
0.03 s on a 2,000-offer synthetic, against ~6,000 queries before. The regression
test asserts the **query count** with `sqlite3`'s trace callback rather than a
wall clock — the count is what was wrong, and a stopwatch on shared CI hardware is
a flaky test.

**063**: `partial_n` is now counted separately from the detail list and is never
capped; `partial` is a sample of at most 50, flagged with `partial_truncated`. A
cap that silently becomes the answer is BUG-047's shape one module over.

Also in this round, no ledger id: `/api/trait_series` indexed the intervals dict
directly, so an unknown interval reached the page as a bare **500**
(`KeyError: '5min'`). It now refuses with a `ValueError` naming the valid ids,
which the handler maps to **400** — REQ-N-09, an interval not in
`intervals.yaml` is refused, not improvised.

### Round 19 — the tech-lead blocks PR-8 (survival estimator + bid-lifetime panel)

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260910-064 | S2 | P1 | fixed | `/api/survival` shipped one array entry per event time on seven arrays — **2.29 MB** at 40,000 lives — and held the dashboard's writer lock across the cluster bootstrap |
| BUG-20260910-065 | S3 | P3 | **open** | `bid_lifetimes` has no `as_of`: it reads terminations as of the **fold**, so a window ending in the past is answered with hindsight and its censored lives sit on a different horizon from its ended ones |

**064 is BUG-063's shape, one module over, written after 063 was fixed.** An
uncapped list became an untrimmed set of arrays. The curve is now thinned onto its
own log-spaced grid **for transport only** — indices are *selected*, never
averaged; `t = 0` and the final step are always kept; and the percentiles, RMST,
residual survivals and the bootstrap are all computed from the **full** curve
before the thinning runs, so no estimate is ever read off the thinned one.
Measured on a 40,000-life synthetic: **2,289,345 → 88,017 bytes**, 17,985 → 384
points. `km.points`, `km.event_times` and `km.downsampled` travel in the response,
because a reduction nobody can see is a reduction nobody can check.

The lock half is the worse half and had no size at all: the bootstrap is `B × n`
arithmetic over a list already in memory, and holding the analytical store's
writer lock across seconds of it stops every other panel *and* the background
normalizer. `survival_prepare()` is now exactly the part that touches sqlite; the
endpoint holds the lock across that and drops it before the bootstrap.

> **Every survival fixture had six lives**, so seven arrays of six entries weighed
> nothing and no test ever looked at the response as an object with a size. The
> same blind spot produced 063. A panel's fixture has to be big enough for its
> failure mode to exist.

**065 is left open on purpose, with a settling note in the ledger.** It is not
reachable from the page — every range the UI can ask for ends at *now*, which is
also the fold — and PR-8's `survival()` takes an explicit `as_of` and is what the
page now draws. `bid_lifetimes` is off screen and kept only as the naive
comparison the PR description contrasts against; changing its numbers now would
alter the one baseline a reviewer uses to judge how far PR-8 moved the median.
The recommendation recorded is to delete it once PR-8 has been run over the real
corpus and that comparison has been made. Until then the defect is stated in the
response itself: `bid_lifetimes`' own `censoring` string names this id and says
what the number is not.

Also in this round, no ledger id — three findings that were wrong-before-shipping
rather than defects in shipped code. **The PR-8 exit palette was chosen by eye and
failed the validator hard**: `#F0A202` / `#7E8F87` / `#5C7C8A` measured worst-CVD
**2.6** and worst-normal **7.7** on the five-colour exit stack — two greys no
reader could separate. Re-picked to `#007711` / `#5544FF` / `#EEAA00` and
re-measured at **16.8 / 27.1** (§4b.1 carries the table and the scopes). **The
`@data` row in docs/08 still said `--trait-offer` `#C792EA`** a day after PR-6
re-picked it, and nothing compared the table to `:root`;
`test_docs_palette_table_matches_the_root_block` now parses both and fails on any
disagreement in either direction. And **two UI assertions were passing on the
wrong string** — the step-shape check was satisfied by the band's invisible edge
traces, so switching the visible curve to a straight interpolation left it green;
both are now scoped to the exact trace and the exact header template, and each was
proven by mutation.

### Round 20 — PR-9 (view split): one defect found while moving the survival panel

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260910-066 | S4 | P3 | fixed | The survival panel escaped three server strings with `esc()` and then assigned them to `.textContent`, so the Operator was shown the literal characters `&lt;` where the withheld-percentiles reason says `n_eff = 0 ... < 30` |

**Found by looking at the page, not at the code.** The PR-9 screenshot of the Flow
view rendered *"percentiles WITHHELD — n_eff = 0 maker-episode cluster(s) `&lt;` 30
(REQ-F-19)"*. `esc()` is the page's HTML escaper and every other call site sends its
result to `innerHTML`, where escaping is the whole point. `#s-head` and `#b-surv` are
set with **`.textContent`**, which never interprets markup — so escaping there was
not a safety measure at all, it was a double encoding, and the one string on the
page that contains a `<` is the one that explains why a number is missing. The
sentence a reader most needs to trust was the sentence that looked broken.

Nothing is unescaped as a result: the fix removes `esc()` **only** on the three
`textContent` targets, and `test_ui_contract`'s escaping property test — which
covers every `${…}` reaching `innerHTML` — is unchanged and still green. The code
now carries a comment saying not to "restore" it.

### Round 21 — PR-10: the production incident. Two writers on one derived store

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260910-067 | S3 | P0 | fixed | `serve()` opened the analytical store and started the folding writer **before** binding 127.0.0.1:8765, so with a second dashboard already listening every launchd retry folded into `data/analytics.sqlite` and then died on "Address already in use" — 177 times in half an hour, until the 2.8 GB store became `database disk image is malformed` |

**What happened, in order.** A second dashboard was running on port 8765. The
launchd job kept trying to start its own. Each retry did this:

```
Dashboard(...)          → opens data/analytics.sqlite read-write
     └─ start()         → the normalizer thread folds new frames INTO it
ThreadingHTTPServer(..) → OSError: [Errno 48] Address already in use
     └─ process exits mid-fold; launchd waits ThrottleInterval (10 s); repeat
```

177 times. Two writers alternating on one SQLite file, one of them killed while
writing, and the store ended unopenable.

**Severity is S3; the root cause is S1-class, and the distinction is the point.**
The consequence was availability — the dashboard was down and the Health page with
it — because the analytical store is *derived*. The corrupt file was **renamed
aside, never deleted and never edited**, and the store was rebuilt from the landing
zone; every event came back, because the raw frames are the record and the store is
a fold of them. **No landing-zone byte was touched in any code path.** What makes
this worth a P0 is that it was *silent*: no error, no warning, and no number
anywhere on the page said a second process was folding. The first symptom was a
store that would not open. Point the same two writers at the landing zone instead
of at a derived file and this entry is an S0a with permanent loss.

**The fix is three layers, and the first one is just ordering.**

1. **Bind first.** `serve()` binds the listening socket *before* it constructs
   `Dashboard`. A process that cannot get its port now exits without having opened
   the store at all — the doomed retry costs nothing, whatever else is in place.
   `navanax dashboard` exits **2** on a bind failure and says the store was not
   opened.
2. **A writer lock.** `Normalizer(..., writer=True)` takes an exclusive `flock` on
   `<store>.lock` for its lifetime; a second folding writer is refused with
   `StoreWriterBusyError` naming the holder's pid and the two ways out, and
   `close()` releases it. Readers pass `writer=False` — `mode=ro`, enforced by
   SQLite rather than by intention, no lock, and `sync()` / `reset_for_refold()` /
   `refold_criteria()` refuse. docs/07 §1's "one writer, readers attach read-only"
   was a documented pattern with nothing behind it; it is now enforced. The
   flock errno rules are **one** set shared with `cli._single_instance`, so a share
   that cannot lock warns and proceeds instead of stopping the fold. The traits job
   is deliberately *outside* the lock — it writes `tokens`/`traits` and folds no
   events — and opens with an explicit `busy_timeout` so a concurrent fold makes it
   wait rather than fail spuriously.
3. **`ThrottleInterval: 30`** on the dashboard plist, so a port conflict cannot
   retry every 10 s.

**Second round — the tech-lead blocked the first fix, correctly.** Everything above
prevents the store *becoming* corrupt. None of it addressed the incident's **end
state**: with `analytics.sqlite` already malformed, `sqlite3.connect()` succeeds
(it is lazy) and the first `PRAGMA journal_mode=WAL` raises
`DatabaseError: database disk image is malformed` straight out of
`Dashboard.__init__` — caught by nothing in `cmd_dashboard`, so: a raw traceback,
an **undocumented exit 1**, and `KeepAlive` repeating that every 30 s. The page
that explains the fault was the one thing the fault took away.

So the dashboard now **degrades instead of dying**:

| | while degraded |
|---|---|
| `/api/health` | **200**, with a `degraded` block (fault, store, recipe) and `quick_check.ok` false |
| `/api/meta`, `/api/gaps` | **200** — neither reads the analytical store |
| every other API route | **503** `{"error": "analytical store is malformed", "rebuild": …}` |
| `/` | **200** — there has to be somewhere to read all of the above |
| store-derived counts | `null`, never `0`. `0` is a claim about a store nobody could read |
| the recorder | untouched, still landing frames |

`_loop` retries the open every 60 s, so recovery is **one step**:
`rebuild-store.command` renames the store to `analytics.sqlite.corrupt-<date>` —
a move, never a delete — and the running dashboard folds a fresh one within a
minute. It asks the **lock**, not the lock file, before moving anything, and names
the holding pid if it is held: "is that pid still alive?" gives false refusals in
both directions, and a false refusal here means the Operator cannot recover at all.
Verified end to end against a deliberately malformed store: 503 → rename → 200 with
`events` back, in 35 s, no restart. `cmd_dashboard` catches
`sqlite3.DatabaseError` anyway and maps it to a documented
`DASH_EXIT_STORE_MALFORMED = 5` with the recipe rather than a stack trace. And
`serve()`'s `except BaseException` path now closes `norm` as well as the socket
(**B2**) — a failure *after* the store opened left the writer lock held by an
object nobody would ever close, so a retry in the same process was refused by its
own stale lock.

**Why nothing caught it.** Nothing looked at the analytical store's integrity, and
nothing named the process folding into it. `/api/health` reported the store's *size*
and the last fold time but never asked whether the file was still readable — so
corruption was found by a crash loop instead of by a check. Both new Health fields
(`store_writer`, and a `PRAGMA quick_check` cached for ten minutes because it reads
every page of a 2.8 GB file) exist because of that. And the suite had no test that
started **two** of anything: every dashboard test built one `Dashboard`, and one
writer never contends with itself.

The second round's version of the same gap: **no test had ever pointed the
dashboard at a store that was already broken.** Every fixture built its store by
folding a fresh landing zone, so `Dashboard.__init__` was only ever exercised on a
healthy file, and the exit code the real incident produced was one no test had ever
seen. The new fixture corrupts **page 1 of a real SQLite file**, because the three
ways to break a store fail differently and only one of them is this bug: junk
behind a valid magic gives *"file is not a database"*; scribbled interior pages
open fine and are caught by `quick_check`; a corrupted page 1 gives *"database disk
image is malformed"* on the first PRAGMA. Only the third reproduces the incident.

### Round 21 — the tech-lead blocks PR-9 (view split, ledger, wallets, health)

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260910-068 | S1 | P1 | fixed | `/api/ledger` never used the index its own basis named — with an unconditional time window the planner skip-scanned and then sorted the whole window into a TEMP B-TREE, 864 ms per page at 200,000 rows |
| BUG-20260910-069 | S1 | P1 | fixed | The "chart this selection" caption stated a **capped** count as exact, and counted rows without the price predicate the chart draws from |
| BUG-20260910-070 | S3 | P2 | fixed | `tools/buglog.py --check` collected bug ids into a **set**, so a duplicate id collapsed into one and the gate stayed green |
| BUG-20260910-071 | S2 | P2 | fixed | The degraded-store reopen probe ran on the **fold** tick, so `refresh_seconds: 3600` meant a rebuilt store went unnoticed for up to an hour |

**068 is BUG-040's shape with one extra turn, and the extra turn is the lesson.**
The obvious fix — `INDEXED BY` — is not sufficient. SQLite then *skip-scans* the
named index (`ANY(collection) AND ANY(maker) AND valid_ts>?`) in order to use the
range term, and a skip-scan does not deliver rows in the index's order, so the
TEMP B-TREE survives. The index was named, the plan read plausibly, and the sort
was still there. Three things together fix it: the named index, an `ORDER BY` that
is the index's own column order `(key, valid_ts, rowid)` with the cursor carrying
that same tuple, and `+e.valid_ts` on the five sorts whose index does not lead
with a timestamp — SQLite's documented way to keep a term out of the index
constraint. The trade is deliberate and scoped: on those five the window becomes a
per-row filter (right for a LIMITed page, wrong for a `COUNT`, so the count query
is built from the indexable form), and on the **default** sort `valid_ts` the range
stays an index range, because there the window *is* the order. 864 ms → 0.2 ms for
page 1, 7–9 ms for a page 12,000 rows deep.

**The lasting change is that this repo now reads `EXPLAIN QUERY PLAN` in a test.**
BUG-040 was caught by a human noticing a 29-second wall clock on real data; the
same defect one module over was invisible on a twelve-row fixture, which is every
fixture the ledger tests had. `MetricEngine.ledger_query_plan()` exists so the
claim in `basis.index` is checkable, and the test asserts both halves — the plan
*and* the consequence at 200,000 rows — so neither can stand in for the other.

**070 has a live cause, not a hypothetical one.** IDs **059–061 are allocated on
two branches at once** — this one and `feat/push-steward`. Whichever merges second
must renumber its three entries and update every reference to them (source
comments, test docstrings, `BUGS.md`); the gate will now name the collision
instead of silently keeping one of the two meanings. **068–071 carry the same
risk** and the same rule: they were allocated on this branch while another was
open, so if that branch reached 068+ too, whichever merges second renumbers.

### Round 22 — the tech-lead blocks PR-0.2 (the two-socket probe) and the deploy scripts

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260911-072 | S1 | P1 | fixed | `probe_two_sockets` decided window membership from each socket's **own** first-sight time, so an event A saw before the window and B was **replayed** inside it counted as a unique for B — fake drops, inflating the measured case for PR-10 |

**This is the flattering-number failure, in the one place it does the most damage.**
The per-connection unique count is the *entire* measured justification for PR-10's
redundant stream. The two sockets are opened five seconds apart by design and
OpenSea replays recent events to a joining subscriber, so on any real run B is
handed events A already had — and every one of those was being counted as "an event
a single connection dropped". Nothing crashed. The number was plausible, and it
pointed at the conclusion the PR wanted.

`common_window`'s settle margin existed to handle exactly this and could not,
because membership was still evaluated one socket's clock at a time. Membership in
the common window is not a per-socket property: an event belongs to it only if
**neither** socket had already seen it when the window opened. `window_sets()` now
computes both window sets and then drops every key whose minimum first-sight
**across both sockets** precedes `t0` — returning the dropped keys rather than
discarding them, because how much the server replays after a join is itself a
measurement, and it now appears in the table, the dict and the verdict as
`n_excluded_pre_window`.

`verdict()` could also reach "**a single socket demonstrably drops events**" from
any non-empty union, with no guard that the window existed or had length. It now
returns early unless the window exists, has non-zero length, *and* the union is
non-empty, and the drop sentence carries its numerator and denominator beside the
percentage rather than a bare `40.0%`.

**Why nothing caught it.** The suite *did* have a stagger test, and it passed — but
in its fixture B never saw the early event at all, so the key was absent from B's
set for the trivial reason. The case that matters is the one where B **does** see
it, late. The test read like it covered the ground, which is why nobody wrote the
one that mattered.

**Four smaller findings ride along, and three are the same shape:** a property
asserted against the *text* of a file instead of against what the code does.
"Never reconnects" was a grep of the probe's own docstring — now a counting connect
factory against a peer that closes early, asserting exactly one connect per socket.
"open-dashboard never starts anything" was one string (`navanax.cli`) — now a scan
of every executed line for another `.command`, a file handed to a shell, or any
launchctl verb but `print`. "update touches nothing under data/" was one file —
now a snapshot of the whole subtree (paths, sizes, mtimes) plus a static scan that
`data/` is never in a write position. The fourth is behavioural: `update.command`
called `launchctl bootstrap` a second time just to capture the error text, which
loaded the job twice on the failure path — BUG-20260910-067's two-writers shape —
and printed the *second* call's message as the reason the first one failed. One
call now, captured, branched on, and never an empty `PROBLEM` block. Ctrl+C on the
probe writes its entry marked **partial** with the seconds it actually ran, instead
of throwing the evidence away, and `_import_tool` can no longer grade a stale
`.pyc`.

### Still open

| ID | Sev | Pri | Summary | Why it is open |
|---|---|---|---|---|
| BUG-20260910-065 | S3 | P3 | `bid_lifetimes` reads terminations as of the fold, with no `as_of` | Not reachable from the page; `survival()` supersedes it. Settling recommendation: delete `bid_lifetimes` after PR-8's corpus run, once the median comparison has been made. |

71 of 72 logged bugs are fixed.

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
