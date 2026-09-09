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

### Still open

**None.** All 54 logged bugs are fixed.

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
