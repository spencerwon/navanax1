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

### Round 23 — the tech-lead blocks PR-10 (the redundant stream)

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260911-073 | S1 | P0 | fixed | The dedup rule was **max over connections**, which doubles on a B rejoin-replay: A × 1 / B × 2 → 2, and one real event is recorded as two |
| BUG-20260911-074 | S1 | P0 | fixed | `covered_by` was derived from B's landing-file intervals alone, so a **correlated outage** annotated A's gap "covered by b" while B's own register said B was blind for the same minutes |
| BUG-20260911-075 | S1 | P0 | fixed | Every raw `COUNT` over `events` in `metrics.py` doubles under a second connection — series, makers, wallets, event mix, ledger total and chart |

**073 is the fifth project rule catching its own implementation.** "A duplicate is
one copy per connection" sounds careful. It is the maximum, over connections, of
that connection's copy count — and the commonest redundant shape there is is B
dropping, rejoining, and being **replayed** an event A already had. `max(1, 2) = 2`.
One cancellation becomes two, the cancel rate doubles, and the market looks busier
than it is. A surprisingly good result is evidence of a bug.

The premise came from a **fixture, not from the code**. `_lives_store.put` in the
self-test clones one real frame, pins `valid_at` by column override, and never
recomputed the dedup key — so its "two cancels on one order" were two rows sharing
one key, and `terminations_seen == 2` was reading an ambiguity rather than asserting
a rule. Worse: **max, min and one-row-per-key all passed every one of the 74 checks
written for PR-10.** The rule that shipped was indistinguishable, under test, from
the two rules that did not. The new table drives A × 1 / B × 2, A × 2 / B × 1,
A × 2 / B × 2 and A × 2 / B × 0 — the last two separate one-per-key from *both*
neighbours — and the fixture now recomputes its key so its two cancels are two
genuinely distinct events.

The rule is now **one row per dedup key**, and what that discards is written down
rather than implied: `deliveries_a`, `deliveries_b` and `multiplicity_disagreements`
on `order_lives`, the same counts in the sync stats and on `/api/health.dedup`, and
a monitor that warns above a configured threshold. The cost is stated in the
docstring: a genuine repeat delivery down **one** socket is now recorded as one
event. That is an undercount, it is the safe direction, and it is unavoidable — two
rows agreeing on every dedup field are what a duplicate *is*.

**074 is "we were not watching" turning into "the other one was".** Coverage was
read off `opened_at` / `closed_at` per landing file. But `close()` runs on a *clean*
stop — not when the machine sleeps, not when the process is killed, not when launchd
force-restarts it, which are the events that produce gaps in the first place. So the
manifest's "still open" interval is widest exactly when the connection was least able
to record, and on one laptop the outage that blinds A blinds B: each one's unclosed
file vouches for the other. Coverage is now the manifest intervals **minus that
connection's own rows in the gap register**, and only a *closed* gap contained end to
end by one covering interval is annotated. The old test staged the failure out of
existence by calling `close()`; the new one flushes and walks away, which is what a
sleeping laptop does.

**075 is a caveat mistaken for a fix.** PR-10 resolved duplicates inside
`order_lives` — correctly, because the lifecycle queries carry `INDEXED BY` and
SQLite will not accept that against a view — and then stopped, on the reasoning that
the Health tab's `dedup` block carried the warning. A caveat on one tab does not stop
a number being read off another. `MetricEngine.dedup_where()` now returns the **empty
string** on a single-connection store, so every SQL statement is character-for-character
what it was and no query plan moves, and a one-row-per-key filter otherwise — appended
at every counting, listing and aggregating site over `events`. Every response that
prints an `n` carries `dedup_applied` and `n_undedupable`.

Two smaller findings rode along, both S3: `autostart-uninstall.command` and
`autostart-status.command` carried hardcoded three-label lists, so neither could stop
or even *show* a stranded `com.navanax.recorder-b`; and turning the flag back off left
that job loaded and crash-looping on `EXIT_CONFIG` every ten seconds forever. All three
launchers now take the label list from `tools/launchd.py`, the installer boots out and
deletes jobs this configuration no longer wants (printing each one), and a
`com.navanax.*` job this version cannot *name* is reported and left alone — it is
likelier to belong to a newer version than to be rubbish.

### Round 24 — the tech-lead blocks PR-10 again, on the upgrade path

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260911-076 | S1 | P0 | fixed | `ORDER_LIVES_METHOD` was written on every row and **read by nothing**; the full re-fold fired only when `order_lives` was empty, so an existing store upgraded into a mixture of method 2 and 3 rows |
| BUG-20260911-077 | S1 | P0 | fixed | A `covered_by` written while B was dead but had **not yet recorded its gap** was permanent — `WHERE covered_by IS NULL` made a claim from incomplete evidence unretractable |

**Both are upgrade-path defects, and neither is visible from a fresh store.** Every
PR-10 test built its store from scratch, so `order_lives` was always empty at open
and the emptiness guard always fired; and the 074 fixture staged B's gap as
*already recorded*, which is the tidy way to write it and is exactly the state the
race has not reached yet. A suite that only ever creates fresh stores cannot see an
upgrade-path defect, and every schema or rule change has one.

**076 is a version stamp with no reader.** `ORDER_LIVES_METHOD` was added "so a
stored row says which rules made it" and nothing ever asked. The one full re-fold
was guarded on `COUNT(*) == 0`, which answers *has this table ever been built* —
not *was it built by the rules this code implements*. Those are different questions
and only the second survives a rule change. So the fix for BUG-20260911-073 would
have corrected only orders that happened to receive another event: `sync()`
refreshes what a pass **touched**, and a bid cancelled yesterday is never touched
again. It would have kept `terminations_seen = 2` for one cancellation for as long
as the store lived, with the flag off and nothing anywhere saying the store was
half one rule set and half another.

The folding **writer** now re-folds every row on open when the stamp is stale, inside
the writer lock, logging the count and the elapsed time — and writes nothing when the
stamp is current, so this is not a re-fold on every start. A **reader** must not and
does not: a second writer on one SQLite store is BUG-20260910-067. It reports instead
— `/api/health.lives_method` carries `mixed` with the min and max versions, the
top-level `status` goes to `warn`, and the payload names the fix in a sentence.
**Measured: 6.6–6.7 s for a full re-fold of 200,000 lives**, against a 30 s budget,
so the open path is the right place for it and it does not need to move behind the
bind.

**077 is 074 arriving through a narrower door.** 074 fixed the correlated outage by
subtracting B's own recorded gaps from its coverage. But the subtraction can only
use gaps that have been **recorded**, and `record_downtime_gap` runs at the start of
`run()` — a process killed without warning writes nothing until it is alive again.
For the seconds or hours in between, the register shows no gap for B and B's last
landing file is still "open", so a fold annotates A's gap "covered by b", honestly,
on the evidence it has. Then B restarts, records its downtime, and the claim is
false — and `WHERE covered_by IS NULL` meant nothing could take it back.

The ruling that unblocks it: `gap_register` lives in the **operational** store,
which `docs/07 §1` calls disposable and reconstructible and which `close_gap` already
updates in place. It is not the landing zone and not the bitemporal record, so
correcting a derived annotation there is allowed. The invariant that is *not*
negotiable — a gap is never closed, shortened or removed by coverage logic — is
unchanged, and is now asserted by snapshotting the whole row before and after.
Coverage is recomputed every fold in both directions, each transition logged once
(INFO to set, WARNING to revoke) with the gap id and the reason, and the revocation
line says the gap is **UNCHANGED** so nobody reads a revocation as the gap being
altered. Health carries `covered_by_n` and `covered_by_revoked_since_start_n`.

### Round 25 — the gates had not run on any branch since 2026-09-09

| ID | Sev | Pri | Status | Summary |
|---|---|---|---|---|
| BUG-20260911-078 | S2 | P0 | fixed | CI's first step runs `tests/selftest.py` **before `pip install`**, and 81 of 187 tests need PyYAML — 79 in-process, 2 in a spawned child. The job died on step one with `ModuleNotFoundError` and every later step was skipped — on every branch, `main` included. **Shipped red twice**: the first fix was verified with an in-process import blocker, which a child process does not inherit |

**No data was lost; two days of verification were.** The steps that never ran
include the bug-ledger consistency gate, the secret scan (REQ-N-11), the
transaction-capability check (REQ-N-14), the `DataIntegrityError` re-raise check,
lint, and the *only* place python-zstandard's real multi-frame behaviour is ever
exercised — which is the entire point of BUG-20260909-010. Every merge in that
window was made against a pipeline that had proven nothing.

**"Standard library only" was documentation, never a test.** It was true when the
file was written and nothing enforced it afterwards.
`test_metric_engine_contract` calls `metrics.load_intervals(config/intervals.yaml)`,
which does `import yaml`; 78 more reach PyYAML the same way — through
`load_intervals`, through `navanax.cli`, or by reading `config/assumptions.yaml`
directly. Locally the suite passes, because PyYAML is installed. The claim could
therefore decay silently for as long as it liked, and the one environment that
would have noticed — a machine with no dependencies — is the one that only CI ever
runs. A bare `ModuleNotFoundError` out of a test aborts the runner, so the failure
was total rather than partial, and GitHub Actions skips every subsequent step of a
failed job: one missing module silenced eleven gates.

**A skip is a first-class outcome now, and it is loud.** `@needs("yaml")` at the
definition site declares what a test cannot run without (a list kept anywhere else
is the drift `BUG-20260909-038` removed from discovery); `missing_modules` answers
by actually importing it. A test whose module is absent is printed as `SKIP` where
it would have run, **listed by name** above the summary, and counted in its own
column — never added to `PASS`. The summary line reads
`191 test functions, 829 passed, 0 failed, 81 skipped (needs: yaml)`.

**The guard that keeps the skips honest is the part that matters.** A skip is still
a test that did not run, so `--no-skips` refuses to exit 0 if anything was skipped
at all. `tools/gates.py` runs the suite that way — this machine has the
dependencies, so a skip here means a test stopped running and the developer would
otherwise never learn it — and a **new CI step after `Install`** runs it that way
too, so those 79 tests are genuinely executed on every push and cannot quietly
disappear. The stdlib-only step is unchanged and now passes, with skips. No
existing test was weakened, deleted, or had an assertion changed.

Both halves are mutation-checked by the regression tests: making a skip count as a
pass fails them, and making strict mode ignore skips fails them.

**It shipped red a second time, and the second cause is the interesting one.** The
first fix was verified by making `yaml` unimportable with an in-process
`sys.meta_path` finder. **A child process does not inherit that.**
`test_ingest_supervised_exit_code_contract` and
`test_ingest_supervised_sigterm_is_a_clean_stop` spawn
`python -m navanax.cli ingest --supervised`; the child imported the real PyYAML out
of the developer's environment, both tests passed under the blocker, and neither was
declared. In CI the child dies at `src/navanax/cli.py:40` and the **parent** reports
an ordinary failed check — `ingest --supervised: SIGTERM exits 0 ... exit=1` — with
the `ModuleNotFoundError` buried in a captured traceback and nothing in the summary
naming a missing module. The fix shipped, CI stayed red, and the second failure
looked nothing like the first.

**An in-process import blocker is not a valid check of the stdlib-only floor.** The
only valid one is a bare interpreter, because that is what CI has:

```bash
rm -rf /tmp/bare && python3 -m venv /tmp/bare
/tmp/bare/bin/python tests/selftest.py              # must exit 0, "0 failed"
/tmp/bare/bin/python tests/selftest.py --no-skips   # must exit non-zero, listing skips
```

A child spawned with `sys.executable` inherits that interpreter, so the subprocess
class is covered. One hole the bare venv does **not** close by itself: tests that run
a `*.command` shell script spawn `bash`, and every one of those scripts picks its
interpreter by searching `PATH` (`for c in python3.14 … python3`) rather than using
`sys.executable` — so on a developer machine that search finds the *system* PyYAML.
Re-running behind a `PATH` of wrappers pointing at the bare interpreter closes it; as
of 2026-09-14 it changes nothing, the same failures and no others, but a new
script-spawning test could change that silently. Both the rule and the hole are in
`docs/03 §4.6.1`.

A test that shells out to `navanax.cli` needs `yaml` exactly as surely as one that
imports it, because the **child** does.
`test_every_test_that_spawns_navanax_cli_declares_that_it_needs_yaml` is the tripwire
between bare-venv runs: it walks the argv of every `subprocess.run`/`Popen` in the
suite and fails if a spawner is missing its declaration. It scans source rather than
running, because proving this dynamically needs an interpreter with no PyYAML — which
is precisely what the machine running the gate does not have.

**The monitor gap is the uncomfortable one.** Every gate in this repo checks the
code; nothing checked that the thing running the gates was still running. A red
pipeline on `main` for two days produced no alert, because the only consumer of
that signal was a human opening the Actions tab. The second gap is narrower and
sharper: nothing said which verification method was *valid*, so a reasonable method
that happened to be wrong was used and its clean result was believed.

## Round 26 — the deploy, 2026-09-14

**BUG-20260914-079 (S1/P0).** The first real deploy of this work onto the
Operator's own machine. The dashboard came up, noticed `order_lives` had been
folded by older rules, and started rewriting all 3.7 million rows — in one
unbounded gulp, holding the writer lock and the whole page. Ten minutes later it
had written nothing measurable to a 14 GB store and the dashboard was dead. It
was stopped by hand.

The fold materialises every qualifying `events` row into a Python dict before
writing anything. That is bounded on the incremental path (400 hashes at a time)
and unbounded on the full one. It was measured at 6.6 s on 200,000 synthetic
lives and shipped on that number — and the tech-lead's own review said, in
writing, that 6.6 s was not the number for the Operator's machine. Nobody acted
on the caveat.

The fix is deliberately small: the re-fold no longer happens at startup at all.
It runs when asked — `rebuild-lives.command`, or `NAVANAX_REFOLD_LIVES=1` — and
the deferral is loud rather than quiet: Health reports `lives_method` as mixed,
the whole response goes to `warn`, and the log says the lifetime counts are
still on the older rules. Nothing is presented as corrected when it is not.
Making the fold itself streaming is separate work; an unbounded operation does
not belong in a startup path whether or not it is fast.

**The standing correction:** a performance claim about the analytical store is
not verified until it is verified against a copy of the real one. Every gate
that passed here ran on fixtures two orders of magnitude too small.

**BUG-20260914-080 (S1/P0).** With the startup rebuild deferred the page still
did not answer. Measured on the real store: `COUNT(*)` over `events` 14.8 s, over
`order_lives` 20.8 s — and Health ran seven of those on every call, while status
and the dedup block each scanned `events` in full. Worse, every request took the
fold's lock and the fold's connection, so after any downtime a click waited behind
a fold that runs for minutes. Requests now read through their own read-only
connection under their own lock (a WAL reader never waits for the writer), and
table counts are cached with `counts_as_of` beside them — instant `MAX(rowid)`
for the append-only events table, background recounts at most every ten minutes
for the rest. A regression test makes the fold artificially slow and asserts the
page still answers.

**BUG-20260914-081 (S1/P0).** Redeployed with BUG-080's fix, `/api/health` still
hung. Health ran `PRAGMA quick_check` — which reads every page — on its first
call and every ten minutes, on the request path, under the page's lock. The
comment beside the constant said "on the Operator's 2.8 GB store that is
seconds"; the store is 14 GB. Above 512 MB it now runs in the background on a
throwaway connection, Health answers at once with `state: checking`, and the TTL
is six hours.

**BUG-20260914-082 (S2/P0).** BUG-080's background counts ran on the page's own
connection. A sqlite3 connection serialises its statements, so the 21-second
`COUNT(*)` on `order_lives` blocked every request — for the first minutes after
every start and again every ten minutes. Background maintenance now has its own
connection and lock, one job at a time.

**BUG-20260914-083 (S1/P0).** Timed every metric the page calls against a
snapshot of the real 14.5 GB store, natively. Every chart series: 0.0–1.7 s at
every range, ETH and USD. The main tab's `live_book`: **459.8 s** — it scanned
every placement event ever recorded with two subqueries per row to find 25
standing orders, when `order_lives` already holds one row per order. It now
reads the lifetime table through a partial index over open orders: 0.0 s. Status
lost two full-table scans (26.2 s and 15.3 s) to a partial index and a
per-collection maximum. The page no longer starts a refresh while one is still
waiting. Measured but not fixed here: `survival_prepare` over 7 d at 128.9 s and
`wallets` over 24 h at 19.2 s — the Flow and Wallets tabs — are next.

**BUG-20260914-084 (S1/P0).** Deployed, and the main tab still did not load.
`/api/meta` answered in 24 ms; `/api/status` had not answered after 25 s. Every
page request went through one shared read-only connection under one lock —
BUG-080's fix — so a single slow request held every other one for as long as it
ran, and the Operator's open tab asks for a full refresh every 10 s. The log
could not say which request it was: it carried no request timings, only 150
broken-pipe tracebacks from browsers that had given up. Requests now each take a
pooled read-only connection, so a slow panel is slow alone, and any request over
2 s is logged with its path and query so the next stall names itself.

**BUG-20260914-085 (S1/P0).** The one that explains the others' numbers. Every
BUG-083 timing was taken on a snapshot made with SQLite's backup API, which
writes one clean file. The live store had a 1.13 GB write-ahead log beside it
that had not restarted in days, because SQLite only restarts the log when a
writer finds no reader using it, and a page that refreshes every 10 s never
leaves that window. Readers consult one hash table per 4096 log frames on
every page lookup — about 68 tables per page here — so a query that took 0.5 s
on the snapshot took ten minutes live. The dashboard now checkpoints and
truncates the log when it opens the store and after any fold that finds it
past 64 MB, waiting at most 3 s for readers and otherwise trying again next
fold. The lesson is written into docs/03: a timing on a copy is a timing of
the data, not of the file the Operator's page reads.

**BUG-20260914-086 (S2/P1).** With the log truncated, the request log named the
two panels still slow on the main tab: the event mix over a day (38–44 s) and
the makers table (17–19 s). Both fetched every one of the day's 2.8 million
rows from the table — one to check its collection, the other for its maker and
type — when a covering index could answer without touching a row. The mix now
reads `ix_events_coll_type`; a new `ix_events_coll_maker_type` (collection,
maker, valid_ts, event_type) serves the makers query alone. The first cut
widened the ledger's own maker index instead and the ledger's plan test caught
the TEMP B-TREE that put back — the ledger sorts by that index's trailing
(valid_ts, rowid). An index whose columns no longer match its definition is
now rebuilt on open, with a log line saying so.

**BUG-20260914-087 (S1/P0).** The one behind "the historical data is not
there". A bucket was blanked if *any* ingestion gap overlapped it. The stream
now closes with "Service restarting" every half hour and reconnects in 1–3 s;
each blip is rightly a gap in the register, and each one blanked its whole
hour. Eighteen of the last twenty-four hours were blank with 99.9 % of each
observed. ASM-031: a bucket is masked only when blind time exceeds
max(60 s, 10 % of the bucket); touched buckets keep their value and the basis
lists their blind seconds. Masking never repaired what a blip loses (a missed
cancel leaves its order standing in every later bucket regardless); it only
hid the record.

**BUG-20260914-088 (S1/P0).** Every fold that added a row ran a full ANALYZE
over the 15 GB store — every index, end to end, every five seconds. A fold of
17,512 rows took 354 s and the header read "EVENTS / MIN 0 · LAST EVENT 6.1m
ago" forever. ANALYZE now runs once on a store without statistics and again
only after 10 % growth, sampled.

**BUG-20260914-089 (S2/P1).** `top_item_bid` over a day took 18 s and a
trait-filtered series 14–24 s. The observed-book path pulled one row per event
into Python to bucket it — 1.35 million item bids a day — when a sub-day bucket
is arithmetic and MAX/MIN/SUM/COUNT can be grouped in SQL over a covering
index; the SQL path is asserted equal to the row path bucket by bucket. A
trait-filtered standing leg now seeks the filter's tokens through a new
`ix_lives_token` instead of walking every life of the kind. What remains, and
is a design item rather than a query fix: the unfiltered standing item-bid leg
still sweeps every bid life in the window in Python — bucket extremes should
be maintained by the fold.

**BUG-20260914-090 (S2/P1).** The Wallets tab over a day: 17–19 s, because the
ranked list took MIN/MAX of the *text* timestamp, which no index carries, so
every row of the day was fetched. The same instants as numbers sit in the new
maker index; the list is now index-only and the strings are made from the
numbers.

**BUG-20260914-091 (S1/P0).** The Traits tab's token table never loaded: the
screener took 518 s, because it still took each token's best ask and bid over
every placement event with the standing predicate's per-row subqueries — the
exact shape BUG-083 removed from the live book one PR earlier. Nobody grepped
for the second copy. It now reads the open lives the way the live book does.

**BUG-20260914-092 (S2/P1).** Every chart's x axis ran from Sept 10 to now
whatever range was chosen: gap shading drew all 276 gaps in the register and the
axis grew to fit them. Shapes are clipped to the series' window and the axis is
pinned to it. Two design calls taken with the Operator in the same pass: the top
item bid (a bid on a rare token, ten times the floor) draws on its own right-hand
axis; the live book shows one row per token with an ×N badge.

**BUG-20260914-093 (S2/P1).** With ANALYZE gone, a fold of 844 rows still took
18–22 s: it decoded the whole open landing file every pass and threw away the
frames it had already folded, so each pass cost as much as the file's age.
Frames are flushed whole and the file is append-only; the reader now resumes at
the last complete-frame boundary and decodes only what is new. The fold's stats
carry a phase breakdown so the next slow fold names its phase.

**BUG-20260914-094 (S2/P1).** The phase breakdown's first catch, the same
evening: `lives 22.4s` of a 22.6 s fold. The sweep that re-folds standing
orders whose expiry has passed scanned all 5.9 million lives to find the
fourteen thousand standing ones, five times a minute. A partial index over the
standing lives makes it a seek.

## Health subsystem — 2026-10-03

The health subsystem (`docs/health/`) shares this ledger; its entries carry
`area: health` and the classes of `docs/health/05_BUG_TAXONOMY.md` §3. Its first five
entries were found by the methodology builder measuring the vendored V1 reference under
Node, not by a reader.

**BUG-20261003-095 (S2/P2).** The kidney strain index's glomerular term averages a
pressure load and a hyperfiltration load with `0.5`/`0.5` and scales the GFR rise by
`0.1`, all three inline in `model.js` M10 (and mirrored in `model.py`), while every
sibling weight and scale is a graded `params.json` row. Three constants the evidence
drawer cannot list. Open: three E-assumption rows at M1, with the version bump.

**BUG-20261003-096 (S1/P1).** The influence screen behind every evidence chip runs one
reference scenario (10 g salt, 24 h, +10 %). Run on every scenario at its own horizon in
both directions, MAP over 30 days has 15 feeders, not 12 — and one of the six missing is
`pn_gain`, calibrated to the very band that chart is compared with. The lists look
complete and are not. Open: HREQ-U-12 (union over scenarios and directions) at M1.

**BUG-20261003-097 (S1/P1).** The Python harness had no `role`. A calibration target —
the He 2013 blood-pressure band that `map_vol_exp`, `pn_gain` and `aldo_vol_exp` were
tuned to hit — would have been counted as a literature pass, and so would the day-30
sodium-balance row, which holds for any parameter set that reaches balance. Fixed in
this PR on the Python side (role, calibrates, counted); the reference mirrors at M1.

**BUG-20261003-098 (S3/P1).** The golden fixture covered four short scenarios and not
`chronic_high_salt_30d` (744 h, 41,664 steps), the stiff parameter corner, or step
checkpoints — so a Python/JavaScript divergence that grows with run length or stiffness
had nowhere to show. Fixed in this PR: the fixture is regenerated with all of them and
the source hashes in its header.

**BUG-20261003-099 (S4/P3).** `index.js` still says "all 52" parameters and "~53
simulations"; the table has had 54 rows since D-2. No M9 block exists between M8 and
M10. Cosmetic; logged because a stale count in a comment is the shape this project
keeps catching.

**BUG-20261003-100 (S0a/P0)**, **-101**, **-102**, **-103**, **-104 (S1/P1).** The
clinical-safety review of the published V1 surface, from source: the strain index is
framed as a dose answer without its label on the dose panel and the HUD gauge (100,
S0a, the Operator's artifact to patch — tracker A-2); 135 mmol/L drawn as a red danger
line labelled "hyponatremia threshold" (101); imperative scenario titles with no
reference person (102); the chronic result shows neither its known divergence nor that
its band is a calibration target (103); only the first sentence of the disclaimer is
rendered — "Not clinically validated." lives in a comment (104; the Python side now
carries `meta.validation_status`). The vendored copy is never edited (HREQ-P-09); each
lands in the artifact and in every new surface at M1.

**BUG-20261003-105, BUG-20261003-106, BUG-20261003-107, BUG-20261003-108, BUG-20261003-109, BUG-20261003-110, BUG-20261003-111, BUG-20261003-112, BUG-20261003-113.** The rigor-lead's attack on the knowledge-base checker:
`report.py` tracebacks on a list-typed field and `status` then prints no disclaimer
(105); the loader accepts `NaN`/`Infinity` the browser rejects (106); `re.ASCII` lets a
DOI with a Unicode space through (107); any `resolved: true` record verifies an id
forever, so the append-only log cannot retire one (108); `status` prints
`E-assumption: 0` when params are unusable instead of "unavailable" (109); the
registry scanner disagrees with PyYAML in 17 of 20 crafted cases (110); a schema
tightening the data violates is only a warning (111); `03 §6` names sixteen rules and
seven are unimplemented, four warn-only (112); checker hygiene (113). All fixed in this PR
with regression tests (47 blocking rules now), except what needs data fields that do not
exist yet, which `DEFERRED_RULES` names and a meta-test enforces.

**BUG-20261003-114 (S4/P3).** `k_excr_gain` says "Log-uniform sampling"; its range is
exactly 5-fold and the rule is `hi/lo > 5`, so it is uniform. Inert in every scenario.

**BUG-20261003-115 (S1/P1).** The first real literature fail: after 1 L of water the
modelled sodium takes **7.05 h** to recover within 0.5 mmol/L, outside Crowe 1987's
6 h, in both implementations (Monte Carlo n = 256: q05 2.83 h, q50 8.73 h, q95 +∞,
in-range share 0.32). V1's own tests never scored this row. It stays a counted fail; the
range is not widened (01 §7.4). The Operator decides: supersede as a known divergence,
or recalibrate through WF-H-01 (tracker D-9).

**BUG-20261003-116 (S4/P3).** Three latent quirks in the reference engine found while
porting; none reachable with shipped data.

**BUG-20261003-117 (S2/P1).** The self-test pinned only `status == "pass"` for the counted
rows, so seven extractor mutants that read the wrong window, unit or baseline survived
(peak time from t = 0, salt window from t = 0, water fraction without baseline urine, ΔMAP
not per 100 mmol, osmolality rise at its peak, urine peak from mL/min, nadir at 1.5 h).
Fixed: every extractor's value is pinned at 1e-9 and recomputed independently from the
result's own series (trapezoid integrals, never the ledger); all seven are now killed.

**BUG-20261003-118 (S2/P1).** A Monte Carlo set for another scenario, run length, dt or any
n was attached as a row's band. Fixed: the set must match the scored result's scenario,
t_end and dt and hold n runs, and n ≥ 256 unless `allow_small_n=True` (HREQ-U-08).

**BUG-20261003-119 (S1/P1).** Rows without Monte Carlo carried no band fields and nothing
said the statuses were unbanded. Fixed: the six band fields are always present
(`band_reason` "no Monte Carlo supplied"); `summarize()` counts `unbanded_counted` and the
status line prints "bands 0 of 10 counted" with the reason (HREQ-V-15).

**BUG-20261003-120 (S2/P1).** An unregistered or mis-cased kind, a countable row without a
range, or an extractor unavailable for any reason became a quiet not_checked row; a kind
typo on the recovery row turned the summary into 9 pass / 0 fail. Fixed: registry defects
are a `ConfigurationError`; only a run too short may give not_checked, and
`summarize()` lists every countable not_checked row with its reason.

**BUG-20261003-121 (S2/P1).** Truncated runs were scored with substitute values. Fixed: a
run shorter than the scenario is not scored (every row not_checked, "run too short"), and
whole-run extractors check coverage; on a full run a never-recovered +inf stays a
counted fail.

**BUG-20261003-122 (S2/P1).** Results from non-default parameters or another dt were
scored without trace. Fixed: refused unless `params_override=True`; every row's meta
stamps the params digest, the override flag, dt and t_end.

**BUG-20261003-123 (S2/P1).** The duplicate-id guard saw only explicit ids, and none
existed. Fixed: duplicates are checked over the final ids, and all 24 shipped
expectations carry explicit `<scenario_id>/<NN>` ids (a Python-only key).

**BUG-20261003-124 (S3/P2).** A reversed or NaN range was scored as a counted model fail.
Fixed: `ConfigurationError` unless lo ≤ hi and neither bound is NaN (±inf allowed).

**BUG-20261003-125 (S3/P2).** No plant covered the bounds or ±inf; the strict-comparison
and inf-passes mutants survived and a string "0.5" scored a pass. Fixed: boundary, ±inf
and non-number plants; both mutants killed.

**BUG-20261003-126 (S3/P2).** The known-divergence and unverified rows gave generic
reasons. Fixed: the row's note (else its target) is on the reason.

**BUG-20261003-127 (S1/P1), open.** The counted row "minimum urine osmolality"
(drink_water_1L/05) shares its only evidence, Baylis 1986, with `U_osm_min`, which floors
the metric at the row's lower bound: only the upper bound is a test (harness value
62.76 mOsm/kg). The row is unchanged; the curator decides between an independent source
(dropping Baylis from `U_osm_min`) and marking the lower bound structural.

**BUG-20261003-128 (S2/P1), open.** The recovery row's "< 6 h" bound has no traceable
Crowe 1987 value (the only quote is the 2 h excreted share) and the row has no note. The
fail itself is honest under every reading (7.05, 6.88, 6.47 h). Extends BUG-20261003-115;
the range is not widened; the curator traces or regrades the bound.

**BUG-20261003-129 (S1/P1).** The D-2 "MAP time course" design target had no role, so the
harness reported 3 calibrated parameters where 03 §12 lists 4. Fixed: role calibration of
`map_auto_tau_h`; the status line now says "calibrated params 4".

**BUG-20261003-130 (S1/P1).** HREQ-U-08, M-12 and V-15 were listed as enforced by
`validate.py` and were not. Fixed: the n gate (118), `independent_vs_calibrated` on the
summary and the status line, and a `registry_version` (sha256 of every registered
expectation record) on every row and the summary.

**BUG-20261003-131 (S1/P1).** The 33-object disclaimer test never checked
`meta.modelVersion`; dropping it from `result_meta` left the suite green. Fixed: the
predicate requires it.

**BUG-20261003-132 (S1/P1).** No golden or invariant test ever moved potassium or made
the body sweat: every registered scenario holds K intake constant and sweat at zero, so
eight port mutants of `model.py` (both K sign flips, the `k_excr_gain` sign, the `K_ur`
exponential removed, both sweat signs, sweat sodium forced to 0, the sweat input ignored)
passed the whole suite, and the potassium and cell-solute invariants "passed" because
every K flux was identically zero. The fixture gains `solverCoverage` (a 24 h baseline
from K_icf × 1.02, and a sweat/potassium run built in the test, not registered); all eight
mutants are now killed. Fixed in this PR.

**BUG-20261003-133 (S3/P2).** Output thinning at `outEvery` 7 and 2.5 had no golden, so
dropping the final-point rule (103 points against JavaScript's 104) or using Python's
banker's `round()` (361 against 241) survived. Both are now golden runs. Fixed in this PR.

**BUG-20261003-134 (S3/P2).** The JavaScript NaN semantics the port emulates (NaN sorted
last in quantiles, `Math.max(0, NaN)` = NaN, `Math.pow(1, NaN)` = NaN) had no golden; all
three emulations could be deleted unseen. The fixture gains `nonFinite`, compared NaN for
NaN by position. Fixed in this PR.

**BUG-20261003-135 (S4/P3).** At dt = 1/60 h every shipped breakpoint sits on the output
grid, so dropping all breakpoints passed the trajectory test and unsorted breakpoints were
never exercised. Golden runs at dt = 0.1 h and an unsorted-breakpoint scenario close it.
Fixed in this PR.

**BUG-20261003-136 (S1/P1).** HREQ-V-07 was not enforced: the instability trap (step
factor 3.5 on the 30-day scenario) returned tens of thousands of NaN values and no error.
`simulate()` now raises `NonFiniteTrajectoryError` (S3/NUM) naming the first time and key;
the JavaScript reference still returns the NaN trajectory, a documented port deviation.
Fixed in this PR.

**BUG-20261003-137 (S1/P1).** `03 §3.3–§3.4` read as enforced gates, but no test ran
convergence, bolus exactness, the breakpoint trap or the instability trap. All four are now
gates that fail: halving the step twice (1e-5 of peak, 1e-3 for ADH and Thirst), the
off-grid bolus to 1e-12 (measured 2.4e-15), the trap without breakpoints misplacing 9.59 %,
the instability trap raising; every scenario and the stiff corner under `--robust`. Fixed
in this PR.

**BUG-20261003-138 (S1/P1).** The n = 256 steady-state drift gate of `03 §3.1` was claimed
while the test ran 3 samples for 6 h. The default suite keeps 3 × 6 h; n = 256 × 24 h runs
under `--robust` (worst 4.1e-14), and the summary line says whether it ran. Fixed in this PR.

**BUG-20261003-139 (S1/P1).** The `03 §9` fixtures (water 1.3 L/day: required urine
1,228.57 mOsm/kg, infeasible; `adh_threshold` 296: sodium 145.61 mmol/L, feasible but
rejected by Monte Carlo) were in no test. Both now are. Fixed in this PR.

**BUG-20261003-140 (S4/P3).** The perturbation test exempted `k_excr_gain` and
`sweat_na_mmolL` as inert only because no run let them act. After 132 the exempt set is the
two `na_normal` thresholds, which the drawSamples golden catches. Fixed in this PR.

The adversarial review of the health subsystem read the documents against the code. The
twelve entries below are the document side of it. Each figure in the corrected text was
re-measured on the port, and every line that depends on code landing in the same round is
marked for re-verification after the merge (tracker W-22).

**BUG-20261003-142 (S1/P1).** `03 §12`, the table that sets the validation record against
the tuning, quoted about 8,900 compared golden values with a worst difference of 4e-14. The
suite compares 12,822 values, and the worst difference is 1.24e-12 relative. The table also
called the harness counts "identical on the reference", which has no harness. Fixed: model
1.0.1 and default parameters stated, the counts as the suite prints them (24 rows; counted
9 pass, 1 fail; 12 not_checked; calibration 1; structural 1), and a separate row for the
counts once the MAP time-course row becomes a calibration row.

**BUG-20261003-143 (S1/P1).** `01 §6.3` presented a two-sided calibration link as built: id
`chronic_high_salt_30d:0`, `calibratedAgainst` on the parameters, and a knowledge-base check
that the two agree. `params.json` has no `calibratedAgainst`, the check is deferred, and the
harness id is `<scenario>/<NN>`. Fixed: the record as shipped, the id rule, and the
parameter side and the JavaScript mirror named as planned (W-18, W-13).

**BUG-20261003-144 (S4/P3).** `03 §4.2` listed four golden trajectories and asked for
additions the fixture already held: all seven scenarios, the stiff corner, checkpoints,
rejection margins and findings. Fixed: every section as built, this round's additions
marked, and the regeneration rule stated (a `MODEL_VERSION` bump, or new sections with every
old one byte-identical).

**BUG-20261003-145 (S4/P3).** `03 §5.1` described harness rows with 8 fields and called
`id`, `role`, `registered` and `counted` "required additions". The rows carry all 17 fields.
Fixed.

**BUG-20261003-146 (S4/P3).** `03 §2` gave Node timings as the cost of every layer. The Python
port that runs the gates is 8 to 13 times slower per run, and about 10 minutes for the
literature layer at n = 256, not about a minute. Fixed: a Node column and a Python column,
both measured, with the method.

**BUG-20261003-147 (S4/P3).** `03 §4.3` said a 1e-6 change to any parameter fails the golden
comparison; the test exempts four. Fixed: the two classification thresholds stay exempt, with
the reason, and the potassium and sweat goldens of this round cover the other two.

**BUG-20261003-148 (S4/P3).** `03 §11` and the HREQ-V-25 row put the planted knowledge-base
fixtures in `tests/health_selftest.py`, and §9 called them files. They are in-memory plants
in `tests/health_kb_selftest.py`. Fixed.

**BUG-20261003-149 (S4/P3).** `config/health/base.yaml` said no threshold lives in code. It
is a checked mirror of code constants. Its comments also placed `MODEL_VERSION` in the wrong
file and named two of the four invariants, and 13 of its keys have no reader. Fixed in
comments only: the header says what the agreement test enforces, and each unread key is
marked reserved. No key or value changed.

**BUG-20261003-150 (S4/P3).** ADR-0002 and HREQ-P-01 said the port uses "the same
identifiers", reads its tolerances from the configuration, and raises
`ReferenceDivergenceError` on a divergence. Function names are snake_case, the tolerances are
test constants mirrored in the configuration, and nothing raises the error. Fixed by an
appended errata section (the accepted text stands) and a reworded HREQ-P-01.

**BUG-20261003-151 (S4/P3).** `06 §4` drew CI as running on every push. It runs on a push or
pull request to `main` only, and its order is kb-check, ledger, lint, pytest, secrets. A push
to a branch with no pull request open against `main` runs no CI and is gated only by
`tools/gates.py`. Fixed in the figure and its `.mmd` source.

**BUG-20261003-152 (S4/P3).** Tracker W-1 still read "in progress" after b810ed6 delivered
the port. Closed with the measured equivalence figures.

**BUG-20261003-153 (S4/P3).** Three documents said six registered scenarios. Seven carry
expectations: 1 + 6 + 2 + 5 + 2 + 4 + 4 = 24 rows. Fixed. The same pass corrected 08's "40
contract rules" to the measured 54 checker codes (47 blocking).

**The undo model, rehearsed (BUG-20261003-154 … BUG-20261003-165).** The adversarial
review ran the removal recipes on scratch copies for the first time, and none of them
worked as written. Removing `engine-v1` exactly as its recipe said turned the gates red,
because the ledger requires every cited file to exist and seven entries cite the engine
(BUG-20261003-154); the knowledge-base self-test typed the Phase 0 module set
(BUG-20261003-155); the `cli` recipe's first edit left `import os` unused, so ruff failed
(BUG-20261003-156); the `errors` recipe never set the flag or ran the gates
(BUG-20261003-157); `depends_on` said `kb-v1` and `cli` import `health.errors`, which
only the engine does, and 07 disagreed with the yaml (BUG-20261003-158); `reference-v1`
and `errors` kept listing a test file the engine recipe deleted, and nothing checked a
`tests` path (BUG-20261003-159); the flag was a label — a disabled engine was imported
and `status` printed `model 1.0.1` beside `modules DISABLED: engine-v1`
(BUG-20261003-160); the engine recipe left stale references in five files
(BUG-20261003-161); "`process` can go at any time; nothing imports it" was false — the
engine and knowledge-base tests read its configuration and documents (BUG-20261003-162,
S1); no gate ran `health_golden.mjs --check` (BUG-20261003-163); the engine recipe was
imprecise against a shared CI step (BUG-20261003-164); and the health README said
`gates.py` runs `status` (BUG-20261003-165). All fixed in this PR: `removed_with_module`
and `buglog.py --mark-removed`; a gate, `python -m health.registry --check`, that holds
every entry to the disk, the imports, the live files naming its paths and the generated
07; `status` obeys the flag; one CI step per module and mode; a golden gate that says
SKIPPED rather than green when Node is absent; `errors` has its own test file; every
module depends on `process`, which goes last. Each recipe was then run literally on a
scratch copy with `tools/gates.py` green after it (`cli` then `engine-v1`; `errors`;
`kb-v1`; `reference-v1`; `process`). `engine-v1` and `cli` are recorded in 07 as accepted
exceptions to HREQ-X-02, with the reason.

**BUG-20261003-166 (S3/P2).** `buglog.py --check` rule 4 (every id mentioned is in the
ledger) walked the whole directory tree, so in the main checkout it failed on ids that
exist only in the builders' git-ignored worktrees under `.claude/worktrees/`. It reads
`git ls-files` now, and walks the tree only where git cannot answer. Fixed in this PR.

**BUG-20261003-167 (S3/P2).** Rule 3 skipped any `regression_test` without `::`, so a
fixed entry whose test was the sentence "none — doc-only; re-verified by the qa-auditor
sweep" passed. A fixed entry's test is now `file::function` or exactly
`docs-only (no mechanical guard)`, and the literal holds only when every location is
documentation; `removed_with_module` does not bypass it. The four 2026-09-09 entries whose
prose names a CI step are a closed exception list in the gate. Fixed in this PR.

**BUG-20261003-168 and BUG-20261003-169 (S2/P2).** Both "never swallowed" CI steps were regular
expressions that read a handler's body as running to the next column-0 line, so a `raise`
later in the same function hid a swallowing handler; on a planted swallow they flagged
nothing. Replaced by `tools/swallow_check.py`, a syntax-tree check that also sees subclasses,
tuples and qualified names; the health step derives every `SurfaceIntegrityError` subclass by
import, and the errors self-test proves the tool fires and that CI calls it.

**BUG-20261003-170 (S3/P2).** `health.registry --check` walked the working tree, so an
ignored build artefact (`src/navanax.egg-info/SOURCES.txt`) counted as a live reference to
module paths and the gate failed in the main checkout. The scan reads `git ls-files` now and
walks only outside a checkout, the rule the ledger gate learned the same day (BUG-166).

**BUG-20261003-171 (S3/P1).** CI's first run on the pull request went red at the golden
check: the fixture header records the Node version that wrote it and `--check` compared
whole files, so the runner's Node 22.23.3 read a value-identical fixture as stale. Node
20.20, 22.22 and 22.23 regenerate every value byte-identically; the check now treats the
recorded version as provenance, names the first differing line when something real
changed, and a test plants both cases on a copy.

**Model 1.1.0, the reference change planned for M1 (BUG-20261003-095, BUG-20261003-099,
BUG-20261003-104, BUG-20261003-116, BUG-20261003-172, BUG-20261003-173; W-13, W-14, W-16,
W-20).** The JavaScript reference and the port moved
together, with the golden fixture regenerated and a section-by-section comparison against
the 1.0.1 fixture: 16 of 26 top-level sections byte-identical (every trajectory, the stiff
corner, the solver coverage, the Monte Carlo bands, the sweep, the steady state and the
findings), and the rest changed only as intended. The three strain-index constants that M10
hard-coded are parameter rows (BUG-095): 57 parameters, 33 E-assumption, 24 of 57 graded at
least B, every strain value unchanged. The expectation records in `scenarios.js` carry the
same `id`, `role` and `calibrates` as the port's, so no key is Python-only (W-13). The
reference exports `VALIDATION_STATUS` and puts it beside the disclaimer on every result
(BUG-104); doing so showed that six of its seven results also lacked the model version, the
disclaimer or both, which HREQ-M-01 requires (BUG-172, S1), now carried by one `resultMeta()` in
both implementations. M9 now labels the outputs that do not feed back, and the comment
counts are gone (BUG-099). Three latent edge cases are fixed on both sides (BUG-116): a
NaN anywhere in an influence difference counts, scenario inputs read frozen copies, and a
quantile between two equal infinities is that infinity; four non-finite fixture values
moved from NaN. The generator then refused the new fixture: the old one had used 98.4 % of
a 400 KB budget (BUG-173, raised to 448 KB, the Operator's to confirm). Every new test was
shown to fail without its change: 17 single-change mutants, 17 killed.
**The curator's M1 slice (BUG-20261003-180 … BUG-20261003-185).** The literature
curator gave the parameter table its two missing structured fields — `fixedReason` on the
twelve `mc: false` rows and `calibratedAgainst` on the four calibrated ones, both in
`schema.json` — and worked the open audit items. `naIn_base_mmold` was graded A-meta from
He 2013 although 150 mmol/day is a scenario condition: superseded to B-textbook with the
old grade and the reason in its notes (BUG-20261003-180, V1 F-12; the Operator may choose
E-assumption). The He 2013 band [0.7, 3.2] mmHg turns out to be exactly the
perfect-correlation limit of the SBP and DBP intervals — [1.06, 2.87] if they were
uncorrelated — a conservative envelope, not a MAP confidence interval; the range is the
Operator's (BUG-20261003-181, V1 F-06). Every literature registry and publisher was refused
by the session's egress policy, so Suckling, Baylis, Crowe and He could not be re-read and
W-9, D-10 and D-11 stay open with each attempt logged (BUG-20261003-182). The six
unused-evidence warnings had no structured exit: Suckling 2012 is now cited where it is true
and the other five carry an `engineOnly` block, and the verification log gained
`curationRecords` for checks that are not external identifiers (BUG-20261003-183). Four
quotes carried an untyped ± with no "SEM/SD unverified" on the record (BUG-20261003-184), and
a metadata-only edit to `params.json` stales the golden fixture, which 03 §4 lets be
regenerated only with a version bump (BUG-20261003-185). All six are open: their regression
tests and the checker change are proposed to the files' owners.

**The influence union (BUG-20261003-096 fixed; BUG-20261003-177 and BUG-20261003-178
open; HREQ-U-12, HREQ-V-19, W-15).** Both implementations now run the influence screen
for every registered scenario at its own horizon, at +10 % and at -10 %, and report the
union: 805 runs, about 24 s under Node. MAP's feeders grow from 12 to 28 and include
`pn_gain`, the parameter calibrated to the 30-day band that chart is compared with; Thirst's
include `thirst_vol_gain`. The default screen's code was factored into one function that
both screens share, and its golden section stayed byte-identical, which is the proof that
it did not change. The fixture gained an `influenceAll` section (feeder lists exactly,
effects to 6 digits), with the budget raised to 720 KB; the default suite holds the port to
it on one scenario and `--robust` on all seven. `tools/health_influence_diff.py` prints the
feeders added and removed against a baseline fixture and is the review record for every
model change. Two findings stay open. The union is not a strict superset of the default
screen: one cumulative ledger key, never displayed, loses a feeder because a longer horizon
widens its range (BUG-177). And the viewer still primes the default screen, so the chip on
the chronic MAP chart still omits `pn_gain` until its worker asks for the union (BUG-178).
**BUG-20261003-198 (S3/P1) and BUG-20261003-199 (S3/P2), open.** Found while adding the
`desktop-app` module (ADR-0007). Its registry entry lands with `enabled: false`, as
`04 §6.5` requires, and four checks in `tests/health_kb_selftest.py` went red with nothing
wrong: written when every module was on, one requires `<id> on` for every registered id and
two compare the whole DISABLED line with a single id (BUG-198). And the registry gate
searches the live files for a module's path as a bare substring, so `desktop/` — the first
one-segment path — is "named" by any file that says desktop, prose or the module's id; one
appended sentence in `docs/README.md` turned the gate red (BUG-199). Both fixes edit files
the desktop-app module does not own and are proposed with its pull request.

### Still open

| ID | Sev | Pri | Summary | Why it is open |
|---|---|---|---|---|
| BUG-20260910-065 | S3 | P3 | `bid_lifetimes` reads terminations as of the fold, with no `as_of` | Not reachable from the page; `survival()` supersedes it. Settling recommendation: delete `bid_lifetimes` after PR-8's corpus run, once the median comparison has been made. |
| BUG-20261003-095 | S2 | P2 | Strain index glomerular constants live in code, not the table | Needs three graded rows and a model-version bump (M1) |
| BUG-20261003-096 | S1 | P1 | Influence screen under-counts feeders; evidence chips omit a calibrated parameter | HREQ-U-12 per-scenario, both-direction screen (M1) |
| BUG-20261003-099 | S4 | P3 | Stale parameter count in a reference comment; no M9 block | With the next reference change (M1) |
| BUG-20261003-100 | S0a | P0 | Published V1 surface frames the strain index as a dose answer without its label | Operator decision A-2: patch the artifact or accept until M1 |
| BUG-20261003-101 | S1 | P1 | 135 mmol/L drawn as a red danger line | M1 viewer review, both implementations |
| BUG-20261003-102 | S1 | P1 | Imperative scenario titles, no reference person | Display-title map at M1 (W-19) |
| BUG-20261003-103 | S1 | P1 | Chronic result hides its divergence and calibration status | M1 viewer review (HREQ-S-07) |
| BUG-20261003-104 | S1 | P1 | Only the first disclaimer sentence is rendered in V1 | Python side fixed in this PR; reference at the next version bump (W-20) |
| BUG-20261003-114 | S4 | P3 | k_excr_gain sampling note wrong | Curator, M1 |
| BUG-20261003-115 | S1 | P1 | 1 L water sodium recovery 7.05 h vs registered 6 h | Operator decision D-9 |
| BUG-20261003-116 | S4 | P3 | Latent reference-engine quirks | Next reference change (M1) |
| BUG-20261003-127 | S1 | P1 | The counted row drink_water_1L/05 (minimum urine osmolality) shares its only evidence (Baylis 1986) with U_os… | Open -- the curator's decision (row not changed; ranges are never widened): cite an independent source for the row and drop Baylis 1986 fro… |
| BUG-20261003-128 | S2 | P1 | The registered "< 6 h" bound of the sodium recovery row has no traceable Crowe 1987 source value, and the row… | Open -- curator (extends BUG-20261003-115): find and quote the source value for 6 h, or record the bound as an assumption with that grade;… |

156 of 170 logged bugs are fixed (the open health entries are Operator or curator decisions, or M1 work, each named in its row above).

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
