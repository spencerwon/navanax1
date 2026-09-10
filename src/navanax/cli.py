"""Phase 0 entrypoint: start the stream, land every frame, record every gap.

    python -m navanax.cli ingest            # run the consumer
    python -m navanax.cli ingest --supervised   # ...under launchd (see docs/04 §8)
    python -m navanax.cli status            # ingestion health
    python -m navanax.cli verify            # re-verify landing-zone checksums
    python -m navanax.cli normalize         # one landing-zone -> store pass
    python -m navanax.cli dashboard         # localhost UI; normalizes continuously
    python -m navanax.cli import-traits F   # load an Explorer tokens.json cache; zero REST

Every day this is not running is a day of history that cannot be bought back:
OpenSea publishes no historical floor series, so the record exists only because
we recorded it.
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .codec import get_codec, verify_codec_roundtrip
from .dotenv import DotenvError, require
from .landing import LandingZoneWriter, is_integrity_failure, verify_manifest
from .opstore import OperationalStore
from .stream import StreamConsumer, new_run_id

log = logging.getLogger("navanax")


def _load_yaml(p: Path) -> dict:
    import yaml
    with p.open() as fh:
        return yaml.safe_load(fh)


def _config(root: Path) -> tuple[dict, list[str]]:
    cfg = _load_yaml(root / "config" / "base.yaml")
    env = os.environ.get("NAVANAX_ENV", cfg.get("environment", "local"))
    envp = root / "config" / f"{env}.yaml"
    if envp.exists():
        def merge(a, b):
            for k, v in b.items():
                a[k] = merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
            return a
        cfg = merge(cfg, _load_yaml(envp))
    wl = _load_yaml(root / "config" / "watchlist.yaml")
    slugs = [c["slug"] for c in wl.get("collections", [])]
    return cfg, slugs


class SingleInstanceError(RuntimeError):
    """Another ingest process already holds the landing-zone lock.

    A distinct type, not a bare RuntimeError. `cmd_ingest` used to catch
    RuntimeError around the whole run, so ANY RuntimeError raised from deep in
    the stream -- hours into a session -- was reported to the operator as
    "another navanax ingest is already running" and told him to stop a process
    that does not exist. The exit code was right (non-zero) and the message was
    a lie, which is the worse half.
    """


@contextmanager
def _single_instance(lock_path: Path):
    """Refuse to start a second ingest process against the same landing zone.

    V3 (validator, second review). Two writers on one root silently lose
    manifest records -- measured at 295 of 600 gap records, with the audit
    reporting clean. A lost gap record is undetectable forever. The cheapest
    correct answer is to make the second process refuse to start.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+")
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            print("WARNING: this platform has no flock; a second ingest process "
                  "against this landing zone cannot be detected.", file=sys.stderr)
        except OSError as exc:
            # Tech-lead finding #4. The errno matters. EWOULDBLOCK/EAGAIN means
            # another process really holds the lock -- refuse. EOPNOTSUPP and
            # friends mean THIS FILESYSTEM does not implement flock at all,
            # which is the reply an SMB/AFP share and some NFS mounts give.
            # Treating those as contention stopped ingestion entirely and told
            # the operator to stop a process that does not exist -- and a day
            # not recorded is a day that cannot be bought back, so failing
            # closed here is the wrong direction.
            unsupported = {
                errno.EOPNOTSUPP, errno.ENOLCK, errno.EINVAL, errno.ENOSYS,
                getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
            }
            if exc.errno in unsupported:
                print(
                    f"WARNING: {lock_path.parent} does not support file locking "
                    f"({errno.errorcode.get(exc.errno, exc.errno)}). This is normal on a "
                    f"network share or an external volume.\n"
                    f"         Ingestion will proceed, but a SECOND ingest process "
                    f"against this landing zone cannot be detected, and two writers "
                    f"silently lose manifest records.\n"
                    f"         Make sure only one is running.",
                    file=sys.stderr,
                )
            else:
                fh.seek(0)
                holder = fh.read().strip() or "an unknown process"
                raise SingleInstanceError(
                    f"another navanax ingest is already running against this "
                    f"landing zone ({holder}).\n"
                    f"  Two writers on one landing zone silently LOSE manifest "
                    f"records, including gap records, which nothing can detect "
                    f"afterwards.\n"
                    f"  Stop the other process, or point this one at a different "
                    f"--root.\n"
                    f"  Lock file: {lock_path}"
                ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")
        fh.flush()
        yield
    finally:
        fh.close()


def _install_shutdown_handlers(loop, task, consumer) -> None:
    """Make closing the Terminal window a clean stop, not a kill.

    BUG-20260909-037. Ctrl-C was handled (KeyboardInterrupt); closing the
    window was not. macOS sends SIGHUP when a Terminal window closes, and an
    unhandled SIGHUP ends the process without running any `finally`: the last
    frame (up to ~7.5 s of events) is lost, no checkpoint is written, and the
    file stays `open` in the manifest. The first live run ended exactly this
    way. Recovery handled it -- 4 frames were flushed and readable -- but a
    clean stop is cheap and loses nothing. SIGTERM is included for the same
    reason (a `kill`, a launchd stop, a sleep-triggered shutdown).
    """
    import signal

    def _stop(signame: str) -> None:
        print(f"\n{signame} received; stopping cleanly and flushing the final frame...",
              file=sys.stderr, flush=True)
        consumer.stop()
        task.cancel()

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _stop, name)
        except (NotImplementedError, RuntimeError):
            pass  # Windows, or not the main thread: Ctrl-C still works


# The exit-code contract `ingest` owes a supervisor (launchd KeepAlive, or any
# other). Tested in tests/selftest.py; documented in docs/04_ENVIRONMENTS.md §8.
#
#   0  clean stop -- SIGTERM, SIGHUP or Ctrl-C. Final frame flushed.
#   2  configuration: empty watchlist, or no usable OPENSEA_API_KEY in .env
#   3  refused: the compressor failed its round-trip check on this machine
#   4  refused: another ingest process holds this landing zone's lock
#   5  fatal error during the run (e.g. the landing zone became unwritable)
#
# Every non-zero code means "did not record". Under KeepAlive launchd restarts
# after ThrottleInterval regardless of which one it was, and the restart records
# the downtime as a gap (StreamConsumer.record_downtime_gap, BUG-20260909-009),
# so a restart loop is visible in the gap register rather than invisible.
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_CODEC = 3
EXIT_ALREADY_RUNNING = 4
EXIT_FATAL = 5


def _supervised_setup() -> None:
    """Minimal adjustments for running under a supervisor rather than a Terminal.

    Two things, both about the log file being readable:

    1. Line buffering. stdout to a FILE is block-buffered by default, so the
       log stays empty for minutes and `tail` on it shows nothing -- which
       looks exactly like a job that never started.
    2. A run banner. launchd appends to one log file across every restart. With
       no boundary marker you cannot tell one 3-second crash loop from one
       healthy 12-hour run, and the restart count is the number that matters.

    There is no colour to strip: the CLI has never emitted any.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):  # pragma: no cover - non-TextIO stream
            pass
    print("=" * 70)
    print(f"navanax ingest (supervised)  pid={os.getpid()}  "
          f"started={datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print("=" * 70)


def cmd_ingest(args) -> int:
    if getattr(args, "supervised", False):
        _supervised_setup()
    root = Path(args.root)
    cfg, slugs = _config(root)
    if not slugs:
        print("watchlist is empty -- nothing to subscribe to", file=sys.stderr)
        return EXIT_CONFIG
    # BUG-20260909-001: read .env, which is what every message tells the user to fill in.
    try:
        key = require("OPENSEA_API_KEY", path=root / ".env")
    except DotenvError as exc:
        print(f"{exc}\n\n  The stream is unmetered but still needs a key.\n"
              f"  Get one: https://docs.opensea.io/reference/api-keys",
              file=sys.stderr)
        return EXIT_CONFIG

    run_id = new_run_id()
    lz = cfg["landing"]

    # Prove the crash-safety property on THIS machine before recording anything
    # that cannot be re-fetched. Costs microseconds; the alternative is trusting
    # that whichever `zstandard` version pip installed reads multi-frame files.
    try:
        verify_codec_roundtrip(get_codec(lz.get("codec", "zstd"), lz.get("codec_level")))
    except Exception as exc:  # noqa: BLE001 - any failure here means do not ingest
        print(f"REFUSING TO INGEST\n\n{exc}", file=sys.stderr)
        return EXIT_CODEC

    writer = LandingZoneWriter(
        root / lz["root"], run_id,
        codec=lz.get("codec", "zstd"), codec_level=lz.get("codec_level"),
        roll_bytes=lz.get("roll_bytes", 64 << 20),
        flush_seconds=lz.get("flush_seconds", 5),
        flush_events=lz.get("flush_events", 1000))
    store = OperationalStore(root / cfg["opstore"]["path"])
    consumer = StreamConsumer(
        key, slugs, writer, store,
        url=cfg["opensea"]["stream_url"],
        heartbeat_seconds=cfg["opensea"].get("heartbeat_seconds", 30),
        max_backoff=cfg["opensea"].get("max_backoff_seconds", 60),
        run_id=run_id)

    print(f"run_id      {run_id}")
    print(f"collections {', '.join(slugs)}")
    print(f"landing     {root / lz['root']}  codec={lz.get('codec')}")
    print("ctrl-c to stop cleanly (the current frame is flushed on exit)\n")

    async def main() -> None:
        task = asyncio.create_task(consumer.run())
        _install_shutdown_handlers(asyncio.get_running_loop(), task, consumer)
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        with _single_instance(root / lz["root"] / ".ingest.lock"):
            try:
                asyncio.run(main())
            except KeyboardInterrupt:
                print("\nstopping; flushing final frame...")
            finally:
                consumer.stop()
                writer.close()
                print(json.dumps(consumer.stats.as_dict(), indent=2))
    except SingleInstanceError as exc:
        print(f"REFUSING TO INGEST\n\n{exc}", file=sys.stderr)
        writer.close()
        return EXIT_ALREADY_RUNNING
    except Exception as exc:  # noqa: BLE001 - a supervisor needs a code, not a traceback alone
        # Reached when the run itself dies -- most plausibly LandingZoneWriteError,
        # which stream.run() re-raises deliberately because reconnecting cannot
        # fix a local disk. Print the traceback (the log file is the only place
        # the operator will ever see it) AND return a code, so KeepAlive
        # restarts and the next start records the downtime as a gap.
        import traceback
        print(f"FATAL: ingestion stopped -- {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        try:
            writer.close()
        except Exception as close_exc:  # noqa: BLE001 - never mask the original cause
            # A close failure here is usually the SAME fault (disk, unmounted
            # volume). Say both, and let the first one stand as the reason.
            print(f"  (also: closing the landing-zone writer failed -- "
                  f"{type(close_exc).__name__}: {close_exc})", file=sys.stderr)
        return EXIT_FATAL
    return EXIT_OK


def cmd_status(args) -> int:
    root = Path(args.root)
    cfg, slugs = _config(root)
    store = OperationalStore(root / cfg["opstore"]["path"])
    print("watchlist       ", ", ".join(slugs) or "(empty)")
    print("open gaps       ", len(store.open_gaps()))
    print("awaiting backfill", len(store.unbackfilled_gaps()))
    for row in store.onboarding_status():
        done, total = row["items_done"], row["items_total"] or 0
        pct = f"{100*done/total:.0f}%" if total else "?"
        print(f"onboarding      {row['collection_slug']}: {row['state']} {pct} "
              f"({row['requests_spent']} reads spent)")
    return 0


def cmd_verify(args) -> int:
    root = Path(args.root)
    cfg, _ = _config(root)
    problems = verify_manifest(root / cfg["landing"]["root"],
                               deep=not getattr(args, "shallow", False))
    if getattr(args, "shallow", False):
        print("NOTE  --shallow: checksums only. A file whose bytes are intact but "
              "whose DECODER returns a prefix (BUG-20260909-010) is NOT detected "
              "in this mode. Run without --shallow for the real integrity audit.")
    failures = [p for p in problems if is_integrity_failure(p)]
    notes = [p for p in problems if not is_integrity_failure(p)]

    for n in notes:
        print("NOTE  " + n)
    if not failures:
        print("landing zone verified clean"
              + (f" ({len(notes)} note(s) above)" if notes else ""))
        return 0
    print("S0a -- LANDING ZONE INTEGRITY FAILURE", file=sys.stderr)
    for p in failures:
        print("  " + p, file=sys.stderr)
    print("\nThe append-only guarantee is what the reprocessing path rests on.\n"
          "Halt ingestion and investigate before writing anything further.", file=sys.stderr)
    return 1


def cmd_normalize(args) -> int:
    """One pass of landing zone -> analytical store. The dashboard does this continuously."""
    from .normalize import Normalizer
    root = Path(args.root)
    cfg, _ = _config(root)
    n = Normalizer(root / cfg["landing"]["root"], root / cfg["analytical"]["path"])
    stats = n.sync()
    print(json.dumps(stats, indent=2))
    n.close()
    return 0


def cmd_traits(args) -> int:
    """Onboard a collection's tokens and traits (REQ-F-01 / REQ-F-07a). Resumable."""
    from .governor import governor_from_config
    from .opstore import OperationalStore
    from .rest import RestClient
    from .traits import TraitsJob, open_store
    root = Path(args.root)
    cfg, slugs = _config(root)
    slug = args.slug or (slugs[0] if slugs else None)
    if not slug:
        print("no collection on the watchlist", file=sys.stderr)
        return 2
    try:
        key = require("OPENSEA_API_KEY", path=root / ".env")
    except DotenvError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    tcfg = cfg.get("traits") or {}
    store = OperationalStore(root / cfg["opstore"]["path"])
    gov = governor_from_config(cfg)
    rest = RestClient(key, gov, run_id="traits", ledger=store.log_rest)
    conn = open_store(root / cfg["analytical"]["path"])
    job = TraitsJob(conn, rest, store, slug=slug,
                    ipfs_gateway=tcfg.get("ipfs_gateway", "https://ipfs.io/ipfs/"),
                    concurrency=int(tcfg.get("concurrency", 6)),
                    timeout=float(tcfg.get("timeout_seconds", 20)),
                    opensea_fallback_budget=int(tcfg.get("opensea_fallback_budget", 50)))
    print(f"collection   {slug}")
    print(f"budget       {gov.bucket.state.capacity:.0f} reads/hr; the token list costs ~1 read per 200 tokens")
    print("metadata     fetched directly from each token's metadata_url (not metered by OpenSea)")
    print()

    async def run() -> None:
        r1 = await job.list_tokens()
        # The list response carries `traits` for Argonauts (BUG-20260909-054), so
        # most of the work happens in pass 1. `traits_from_list` counts WRITES;
        # an entry the store already had is counted separately, and one whose
        # value DISAGREES with what we recorded first is reported, not dropped
        # (BUG-20260909-055).
        dis = r1.pop("traits_disagreements", [])
        print("token list  ", json.dumps(r1))
        if dis:
            print(f"\n*** {r1['traits_skipped_conflict']} token(s) where OpenSea's list response "
                  f"DISAGREES with what this store already recorded -- nothing overwritten ***",
                  file=sys.stderr)
            for d in dis[:20]:
                print(f"  token {d['token_id']:>6}  {d['trait_type']}: store={d['stored']}  list={d['list']}",
                      file=sys.stderr)
            if len(dis) > 20:
                print(f"  ... and {len(dis) - 20} more", file=sys.stderr)
            print("One of the two sources is wrong for these tokens. Decide which before trusting either.\n",
                  file=sys.stderr)
        r2 = await job.fetch_traits(limit=args.limit)
        print("traits      ", json.dumps(r2))
        s = job.summary()
        print("summary     ", json.dumps(s))
        print(f"coverage     {s['with_traits']} of {s['tokens']} tokens now have traits")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped; progress is saved -- run again to resume")
    finally:
        conn.close()
    return 0


# The window a trait cache's `generated` time may fall in. Outside it, the value
# is not a timestamp we can believe, and `traits_at` is the column the whole
# bitemporal record rests on (docs/06 §2).
#
#   * BEFORE 2020-01-01 -- OpenSea did not exist in a form this tool could have
#     read; 0 is what an absent field turns into, and 1.7e6 is what a
#     hand-typed number turns into.
#   * AFTER now + 1 h -- a cache cannot have been generated in the future. An
#     hour of slack absorbs a clock skew between the machine that pulled the
#     cache and this one; more than that is a wrong number, not a skew.
#
# BUG-20260909-055: neither bound was checked. A future epoch was written
# straight into `traits_at`, where nothing downstream can tell it from an
# observation, and a MILLISECOND epoch -- which is what every JavaScript tool
# emits by default -- reached `datetime.fromtimestamp` and came back as a raw
# `ValueError: year 58650 is out of range` with no indication of what to do.
GENERATED_MIN = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
GENERATED_SLACK_SECONDS = 3600


def generated_at_iso(value, *, where: str = "--generated") -> str:
    """Epoch seconds -> ISO-UTC, or raise ValueError with a plain refusal.

    Never returns a time it cannot defend. The caller prints the message and
    exits 2 -- refusing is always cheaper than a wrong `traits_at`, because the
    wrong one is indistinguishable from a real observation a week later.
    """
    try:
        g = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where} must be a number of seconds since 1970-01-01 UTC; "
                         f"got {value!r}, which is not a number") from None
    if g != g or g in (float("inf"), float("-inf")):
        raise ValueError(f"{where} is {value!r}, which is not a usable time") from None
    hi = datetime.now(timezone.utc).timestamp() + GENERATED_SLACK_SECONDS
    if not (GENERATED_MIN <= g <= hi):
        hint = ""
        if g > hi:
            hint = ("\n  That is in the FUTURE. `traits_at` records when the traits were OBSERVED; "
                    "a future observation is not a thing that can have happened.")
            if g / 1000.0 >= GENERATED_MIN and g / 1000.0 <= hi:
                hint += (f"\n  It looks like MILLISECONDS (the JavaScript default). "
                         f"In seconds it would be {g / 1000.0:.0f} "
                         f"({datetime.fromtimestamp(g / 1000.0, tz=timezone.utc).isoformat()}). "
                         f"Pass that, or divide the cache's field by 1000.")
        else:
            hint = ("\n  That is before 2020-01-01, which no real cache can have been generated at. "
                    "0 is what an absent or null field becomes.")
        raise ValueError(
            f"refusing: {where} is {g:.0f}, outside the believable window "
            f"[{datetime.fromtimestamp(GENERATED_MIN, tz=timezone.utc).date()}, now + 1 h].{hint}\n"
            f"  Nothing was written. Fix the timestamp and run again -- a wrong `traits_at` cannot "
            f"be told from a real observation afterwards.") from None
    try:
        return datetime.fromtimestamp(g, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"refusing: {where} is {g!r}, which this machine cannot convert to a "
                         f"UTC time ({type(exc).__name__}: {exc}). Nothing was written.") from None


def cmd_import_traits(args) -> int:
    """Load an Explorer `tokens.json` trait cache into the store. Zero REST reads.

    Exit 0: imported cleanly. Exit 1: at least one disagreement -- a token whose
    stored traits differ from the cache's, or a token id the cache itself gives
    two different sets of traits for. Both are printed in full; the store was not
    changed for those tokens and the Operator must decide which source is right.
    Exit 2: refused before writing anything (unreadable cache, unbelievable
    timestamp, no collection).
    """
    import hashlib

    from .traits import (
        case_near_misses,
        criteria_trait_coverage,
        import_explorer_cache,
        open_store,
        trait_coverage,
    )
    root = Path(args.root)
    cfg, slugs = _config(root)
    slug = args.collection or (slugs[0] if slugs else None)
    if not slug:
        print("no collection: pass --collection or fill the watchlist", file=sys.stderr)
        return 2
    src = Path(args.tokens_json)
    try:
        blob = src.read_bytes()
        cache = json.loads(blob.decode("utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {src}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(cache, dict):
        print(f"{src} is not a dict keyed by token id", file=sys.stderr)
        return 2
    generated = args.generated
    where = "--generated"
    if generated is None:
        # The cache's own timestamp lives in summary.json next to it.
        sp = src.with_name("summary.json")
        where = f"the `generated` field of {sp.name}"
        try:
            generated = json.loads(sp.read_text(encoding="utf-8"))["generated"]
        except (OSError, ValueError, KeyError, TypeError):
            print(f"no --generated given and {sp} has no `generated` epoch; refusing to guess when the "
                  f"traits were observed", file=sys.stderr)
            return 2
    try:
        generated_at = generated_at_iso(generated, where=where)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sha = hashlib.sha256(blob).hexdigest()
    db = root / cfg["analytical"]["path"]
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_store(db)
    try:
        res = import_explorer_cache(conn, cache, slug=slug, generated_at=generated_at,
                                    contract=args.contract, cache_file=str(src),
                                    cache_bytes=len(blob), cache_sha256=sha)
        cov = trait_coverage(conn, slug)
        crit = criteria_trait_coverage(conn, slug)
        near = case_near_misses(conn, slug)
    finally:
        conn.close()
    dis = res.pop("disagreements")
    dup = res.pop("duplicate_id_disagreements")
    malformed = res.pop("malformed_examples")
    mismatch = res.pop("contract_mismatch_examples")
    print(json.dumps(res, indent=2))

    # -- coverage, in the words the Operator asked the question in -------------
    print(f"\ncoverage    {cov['with_traits']} of {cov['tokens']} tokens now have traits"
          + (f" ({100 * cov['with_traits'] / cov['tokens']:.1f}%)" if cov["tokens"] else "")
          + f"; {len(cov['trait_types'])} trait types")
    for t in cov["trait_types"]:
        print(f"              {t['trait_type']:<24} {t['distinct_values']} distinct values")
    if crit.get("available"):
        print(f"criteria    {crit['matched']} of {crit['distinct_criteria']} standing order criteria "
              f"match a trait value; {crit['missing']} match none")
        for p in crit["missing_pairs"]:
            print(f"              NO MATCH  {p['trait_type']} = {p['value']}")
    else:
        print(f"criteria    not checked -- {crit['note']}")
    if near["alert"]:
        print("\n*** CASE NEAR-MISSES: two spellings that differ only by case are two different "
              "values here, and a filter on one matches none of the other ***", file=sys.stderr)
        for g in near["trait_type_near_misses"]:
            print(f"  trait type  {g['variants']}", file=sys.stderr)
        for g in near["value_near_misses"]:
            print(f"  value       {g['trait_type']}: {g['variants']}", file=sys.stderr)
        print("  Reported, never merged. Deciding they are the same value is the Operator's call.",
              file=sys.stderr)
    else:
        print("near-miss   no trait type or value differs from another only by case")

    if malformed:
        print(f"\n*** {res['malformed']} MALFORMED cache item(s) skipped -- never stored as text ***",
              file=sys.stderr)
        for m in malformed[:20]:
            print(f"  key {m['key']}: {m['reason']}", file=sys.stderr)
        if len(malformed) > 20:
            print(f"  ... and {len(malformed) - 20} more", file=sys.stderr)
    if mismatch:
        print(f"\n*** {res['contract_mismatches']} CONTRACT MISMATCH(ES): the store and this import "
              f"disagree about which contract the token belongs to ***", file=sys.stderr)
        for m in mismatch[:20]:
            print(f"  token {m['token_id']:>6}  stored={m['stored']}  importing={m['importing']}",
                  file=sys.stderr)
        print("  The stored value was kept. One of the two is wrong, and a wrong contract sends every "
              "fallback read to the wrong collection.", file=sys.stderr)
    if dup:
        print(f"\n*** {len(dup)} TOKEN ID(S) THE CACHE GIVES TWO DIFFERENT SETS OF TRAITS -- "
              f"nothing written for them ***", file=sys.stderr)
        for d in dup:
            print(f"  token {d['token_id']:>6}  keys {d['keys']}: {d['first']}  vs  {d['second']}",
                  file=sys.stderr)
        print("The cache contradicts itself here. Neither set is recorded.", file=sys.stderr)
    if dis:
        print(f"\n*** {len(dis)} TRAIT DISAGREEMENT(S) between the store and {src.name} -- nothing overwritten ***",
              file=sys.stderr)
        for d in dis:
            print(f"  token {d['token_id']:>6}  {d['trait_type']}: store={d['stored']}  cache={d['cache']}",
                  file=sys.stderr)
        print("One of the two sources is wrong for these tokens. Decide which before trusting either.",
              file=sys.stderr)
    return 1 if (dis or dup) else 0


def cmd_dashboard(args) -> int:
    from .dashboard import serve
    root = Path(args.root)
    cfg, slugs = _config(root)
    try:
        serve(root, cfg, slugs, port=args.port, open_browser=not args.no_browser)
    except ValueError as exc:
        print(f"REFUSING: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"could not bind the dashboard port: {exc}\n"
              f"  Is another dashboard already running? Open http://127.0.0.1:"
              f"{args.port or (cfg.get('dashboard') or {}).get('port', 8765)}/ instead.",
              file=sys.stderr)
        return 3
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="navanax")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("--supervised", action="store_true",
                   help="running under launchd or another supervisor: line-buffer the "
                        "output so the log file is live, and print a run banner so one "
                        "restart can be told from the next")
    i.set_defaults(fn=cmd_ingest)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    v = sub.add_parser("verify")
    v.add_argument("--shallow", action="store_true",
                   help="checksums only; skip decompressing every file "
                        "(fast, but cannot detect a short decode)")
    v.set_defaults(fn=cmd_verify)
    sub.add_parser("normalize").set_defaults(fn=cmd_normalize)
    t = sub.add_parser("traits")
    t.add_argument("--slug", default=None)
    t.add_argument("--limit", type=int, default=None, help="fetch traits for at most N tokens this run")
    t.set_defaults(fn=cmd_traits)
    it = sub.add_parser("import-traits", help="load an Explorer tokens.json trait cache; zero REST reads")
    it.add_argument("tokens_json")
    it.add_argument("--collection", default=None)
    it.add_argument("--contract", default=None, help="token contract; defaults to the known one for the slug")
    # Deliberately NOT type=float: argparse's own failure is a usage dump and an
    # exit code, where this needs a sentence saying what a believable value is.
    it.add_argument("--generated", default=None,
                    help="epoch SECONDS (not milliseconds) the cache was generated; defaults to "
                         "summary.json `generated` next to it. Must fall in [2020-01-01, now + 1 h]")
    it.set_defaults(fn=cmd_import_traits)
    d = sub.add_parser("dashboard")
    d.add_argument("--port", type=int, default=None)
    d.add_argument("--no-browser", action="store_true")
    d.set_defaults(fn=cmd_dashboard)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
