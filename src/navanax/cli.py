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
from datetime import datetime, timedelta, timezone
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


def cmd_audit_token_ids(args) -> int:
    """READ-ONLY audit for BUG-20260909-057. Opens both stores and changes neither.

    Exit 0: no stored row took its `token_id` from a Seaport criteria item.
    Exit 1: at least one did -- the recipe to repair them is printed.
    """
    import sqlite3

    from .normalize import audit_criteria_token_ids
    root = Path(args.root)
    cfg, _ = _config(root)
    db = root / cfg["analytical"]["path"]
    landing = root / cfg["landing"]["root"]

    print("BUG-20260909-057 audit -- token_id taken from a Seaport criteria item")
    print("read-only: this command opens the landing zone and the analytical store")
    print("           for reading only. It writes, renames and deletes nothing.")
    print()
    print("Seaport itemType 4 and 5 are ERC721/ERC1155_WITH_CRITERIA. Their")
    print("`identifierOrCriteria` is a MERKLE ROOT over a SET of token ids, not a")
    print("token id -- and a root of 0 means \"any token in the collection\". A row")
    print("that took its token_id from one is a collection-wide bid recorded")
    print("against a single token: it stops counting as collection-wide in")
    print("immediacy_cost (REQ-F-13a) and shows up as a phantom bid on that token.")
    print()

    # -- 1. the store, as it stands ----------------------------------------
    store_rows: list[tuple] = []
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            store_rows = conn.execute(
                "SELECT event_type, COUNT(*), SUM(token_id IS NOT NULL) FROM events "
                "GROUP BY event_type ORDER BY event_type").fetchall()
            suspect = conn.execute(
                "SELECT event_type, COUNT(*) FROM events WHERE token_id IS NOT NULL "
                "AND event_type IN ('collection_offer','trait_offer') "
                "GROUP BY event_type ORDER BY event_type").fetchall()
            suspect_examples = conn.execute(
                "SELECT run, seq, event_type, collection, token_id, order_hash FROM events "
                "WHERE token_id IS NOT NULL AND event_type IN ('collection_offer','trait_offer') "
                "ORDER BY seq LIMIT 10").fetchall()
        finally:
            conn.close()
    else:
        print(f"NOTE  no analytical store at {db} -- nothing has been folded yet.")
        print()
        suspect, suspect_examples = [], []

    n_suspect = sum(n for _, n in suspect)
    if store_rows:
        print("ANALYTICAL STORE -- rows with a non-NULL token_id, by event type")
        print(f"  {'event_type':<22}{'rows':>10}{'token_id set':>14}")
        for et, rows, with_tid in store_rows:
            print(f"  {et:<22}{rows:>10}{(with_tid or 0):>14}")
        print()
        print("  A collection_offer or trait_offer is an order over a SET of tokens.")
        print("  Its token_id must be NULL. The expected count in that column for")
        print("  those two rows is 0.")
        print(f"  criteria-bearing offers carrying a token_id: {n_suspect}"
              + ("  <-- NOT CLEAN" if n_suspect else "  (clean)"))
        for r in suspect_examples:
            print(f"    run={r[0]} seq={r[1]} {r[2]} {r[3]} token_id={r[4]!r} order={r[5]}")
        print()

    # -- 2. the record, which is what settles it ---------------------------
    if getattr(args, "shallow", False):
        print("--shallow: the landing-zone sweep was skipped, so this run cannot say")
        print("where a token_id came from -- only that the two criteria-bearing offer")
        print("types do or do not carry one. An item_received_bid folded from a")
        print("criteria item looks identical to a legitimate one from here. Run without")
        print("--shallow for the real answer.")
        return 1 if n_suspect else 0

    if not landing.exists():
        print(f"no landing zone at {landing} -- cannot sweep the record.", file=sys.stderr)
        return 2
    print(f"LANDING ZONE SWEEP  {landing}")
    a = audit_criteria_token_ids(landing)
    print(f"  {a['files_read']} file(s) read, {a['frames']} event frame(s)"
          + (f", {a['files_unreadable']} UNREADABLE" if a["files_unreadable"] else ""))
    print()
    print(f"  {'event_type':<22}{'frames':>9}{'protocol_data':>15}{'criteria item':>15}{'AT RISK':>10}")
    for et in sorted(a["by_event_type"]):
        b = a["by_event_type"][et]
        print(f"  {et:<22}{b['frames']:>9}{b['with_protocol_data']:>15}"
              f"{b['with_criteria_item']:>15}{b['token_id_from_criteria']:>10}")
    print()
    print("  `protocol_data` is how many frames of that type carry the Seaport order")
    print("  at all -- the open question this audit exists to settle. If it is 0 for")
    print("  collection_offer and trait_offer, OpenSea never sends it on an offer and")
    print("  the defect could not have fired on anything recorded.")
    print("  `AT RISK` is how many frames the PRE-FIX parser WOULD have folded with a")
    print("  token_id taken from a criteria item. It is a property of the frame, not")
    print("  of the store: a frame folded since the fix is at risk and still correct.")
    print()

    # -- 3. what is actually IN the store, frame by frame -------------------
    corrupt: list[tuple] = []
    checked = 0
    if db.exists() and a["affected_keys"]:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            keys = a["affected_keys"]
            for i in range(0, len(keys), 200):          # SQLite's parameter limit
                chunk = keys[i:i + 200]
                q = " OR ".join(["(run=? AND seq=?)"] * len(chunk))
                flat = [v for k in chunk for v in k]
                checked += len(chunk)
                corrupt.extend(conn.execute(
                    "SELECT run, seq, event_type, collection, token_id, order_hash FROM events "
                    f"WHERE token_id IS NOT NULL AND ({q})", flat).fetchall())
        finally:
            conn.close()

    at_risk = a["affected_total"]
    print(f"RESULT: {at_risk} recorded frame(s) at risk;")
    print(f"        {len(corrupt)} stored row(s) actually carry a token_id taken from a "
          f"criteria item."
          + ("  (the at-risk key list was truncated;"
             " treat this as a lower bound)" if a["affected_keys_truncated"] else ""))
    if not at_risk:
        print("        No recorded frame could have hit the defect, so no stored row is")
        print("        wrong and no re-fold is needed for this bug. The fix is a guard")
        print("        against frames not yet received. Nothing to do.")
        return 1 if n_suspect else 0
    if not db.exists():
        print(f"        There is no analytical store at {db} to check them against, so")
        print("        this run cannot say whether anything was folded wrong. Fold first")
        print("        (`navanax normalize`), then run this again.")
        return 1
    if not corrupt:
        print("        Every at-risk frame was folded by a FIXED parser: no stored row is")
        print("        wrong and no re-fold is needed. Re-run this after any fold done by")
        print("        an older build.")
        return 1 if n_suspect else 0

    for r in corrupt[:10]:
        print(f"    run={r[0]} seq={r[1]} {r[2]} {r[3]} token_id={r[4]!r} order={r[5]}")
    if len(corrupt) > 10:
        print(f"    ... and {len(corrupt) - 10} more")
    print()
    print("WHAT TO DO: token_id is DERIVED, so the record is intact and the repair is")
    print("        a RE-FOLD, not an edit -- nothing here or in the repair path ever")
    print("        UPDATEs a row or touches the landing zone. Stop the dashboard and")
    print("        any ingest fold, then:")
    print()
    print("          python3 -c \"from navanax.normalize import Normalizer; \\")
    print("            n=Normalizer('<landing-root>','<analytical.sqlite>'); \\")
    print("            print(n.reset_for_refold()); print(n.sync()); n.close()\"")
    print()
    print("        That clears the derived tables (events, order_criteria, order_lives,")
    print("        unparsed, watermarks -- not tokens/traits, which cost REST reads) and")
    print("        re-reads every frame from the landing zone through the fixed parser.")
    print("        Then run this audit again: the second number must be 0.")
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
        print("token list  ", json.dumps(r1))
        r2 = await job.fetch_traits(limit=args.limit)
        print("traits      ", json.dumps(r2))
        print("summary     ", json.dumps(job.summary()))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped; progress is saved -- run again to resume")
    finally:
        conn.close()
    return 0


def cmd_import_traits(args) -> int:
    """Load an Explorer `tokens.json` trait cache into the store. Zero REST reads.

    Exit 0: imported cleanly. Exit 1: at least one token whose stored traits
    DISAGREE with the cache -- printed in full; the store was not changed for
    those tokens and the Operator must decide which source is right.
    """
    from .traits import import_explorer_cache, open_store
    root = Path(args.root)
    cfg, slugs = _config(root)
    slug = args.collection or (slugs[0] if slugs else None)
    if not slug:
        print("no collection: pass --collection or fill the watchlist", file=sys.stderr)
        return 2
    src = Path(args.tokens_json)
    try:
        cache = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read {src}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(cache, dict):
        print(f"{src} is not a dict keyed by token id", file=sys.stderr)
        return 2
    generated: float | None = args.generated
    if generated is None:
        # The cache's own timestamp lives in summary.json next to it.
        sp = src.with_name("summary.json")
        try:
            generated = float(json.loads(sp.read_text(encoding="utf-8"))["generated"])
        except (OSError, ValueError, KeyError, TypeError):
            print(f"no --generated given and {sp} has no `generated` epoch; refusing to guess when the "
                  f"traits were observed", file=sys.stderr)
            return 2
    # BUG-20260909-058. The refusal above covers the ABSENCE of a timestamp. A
    # present one still has to be plausible: `traits_at` is an OBSERVATION time,
    # and a wrong one is a bitemporal lie no later read can detect (docs/05 TMP).
    # 0 stamps 1970; an epoch pasted in MILLISECONDS stamps the year 58680 (or
    # dies in the C library, depending on the platform).
    floor = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ceiling = datetime.now(timezone.utc) + timedelta(days=1)
    try:
        generated_dt = datetime.fromtimestamp(generated, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        print(f"--generated {generated:.0f} is not a usable epoch ({type(exc).__name__}: {exc}).\n"
              f"  It must be epoch SECONDS between {floor.date()} and now + 1 day "
              f"(a timestamp in milliseconds is 1000x too large).", file=sys.stderr)
        return 2
    if not (floor <= generated_dt <= ceiling):
        why = ("that is in milliseconds, not seconds" if generated > 1e12
               else "that is before this project existed" if generated_dt < floor
               else "that is in the future")
        print(f"--generated {generated:.0f} is {generated_dt.isoformat().replace('+00:00', 'Z')} -- "
              f"{why}.\n"
              f"  traits_at records WHEN THE TRAITS WERE OBSERVED; an implausible one "
              f"silently corrupts every as-of query.\n"
              f"  Refusing. Give epoch SECONDS between {floor.date()} and "
              f"{ceiling.date()}, or drop --generated and let summary.json say.",
              file=sys.stderr)
        return 2
    generated_at = generated_dt.isoformat().replace("+00:00", "Z")
    db = root / cfg["analytical"]["path"]
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_store(db)
    try:
        res = import_explorer_cache(conn, cache, slug=slug, generated_at=generated_at, contract=args.contract)
    finally:
        conn.close()
    dis = res.pop("disagreements")
    print(json.dumps(res, indent=2))
    if dis:
        print(f"\n*** {len(dis)} TRAIT DISAGREEMENT(S) between the store and {src.name} -- nothing overwritten ***",
              file=sys.stderr)
        for d in dis:
            print(f"  token {d['token_id']:>6}  {d['trait_type']}: store={d['stored']}  cache={d['cache']}",
                  file=sys.stderr)
        print("One of the two sources is wrong for these tokens. Decide which before trusting either.",
              file=sys.stderr)
        return 1
    return 0


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
    at = sub.add_parser("audit-token-ids",
                        help="read-only: did any stored row take its token_id from a "
                             "Seaport criteria item? (BUG-20260909-057)")
    at.add_argument("--shallow", action="store_true",
                    help="analytical store only; skip the landing-zone sweep "
                         "(fast, but cannot establish where a token_id came from)")
    at.set_defaults(fn=cmd_audit_token_ids)
    t = sub.add_parser("traits")
    t.add_argument("--slug", default=None)
    t.add_argument("--limit", type=int, default=None, help="fetch traits for at most N tokens this run")
    t.set_defaults(fn=cmd_traits)
    it = sub.add_parser("import-traits", help="load an Explorer tokens.json trait cache; zero REST reads")
    it.add_argument("tokens_json")
    it.add_argument("--collection", default=None)
    it.add_argument("--contract", default=None, help="token contract; defaults to the known one for the slug")
    it.add_argument("--generated", type=float, default=None,
                    help="epoch seconds the cache was generated; defaults to summary.json `generated` next to it")
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
