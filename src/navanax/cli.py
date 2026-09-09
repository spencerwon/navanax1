"""Phase 0 entrypoint: start the stream, land every frame, record every gap.

    python -m navanax.cli ingest            # run the consumer
    python -m navanax.cli status            # ingestion health
    python -m navanax.cli verify            # re-verify landing-zone checksums

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
                raise RuntimeError(
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


def cmd_ingest(args) -> int:
    root = Path(args.root)
    cfg, slugs = _config(root)
    if not slugs:
        print("watchlist is empty -- nothing to subscribe to", file=sys.stderr)
        return 2
    # BUG-20260909-001: read .env, which is what every message tells the user to fill in.
    try:
        key = require("OPENSEA_API_KEY", path=root / ".env")
    except DotenvError as exc:
        print(f"{exc}\n\n  The stream is unmetered but still needs a key.\n"
              f"  Get one: https://docs.opensea.io/reference/api-keys",
              file=sys.stderr)
        return 2

    run_id = new_run_id()
    lz = cfg["landing"]

    # Prove the crash-safety property on THIS machine before recording anything
    # that cannot be re-fetched. Costs microseconds; the alternative is trusting
    # that whichever `zstandard` version pip installed reads multi-frame files.
    try:
        verify_codec_roundtrip(get_codec(lz.get("codec", "zstd"), lz.get("codec_level")))
    except Exception as exc:  # noqa: BLE001 - any failure here means do not ingest
        print(f"REFUSING TO INGEST\n\n{exc}", file=sys.stderr)
        return 3

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
    except RuntimeError as exc:
        print(f"REFUSING TO INGEST\n\n{exc}", file=sys.stderr)
        writer.close()
        return 4
    return 0


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="navanax")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest").set_defaults(fn=cmd_ingest)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    v = sub.add_parser("verify")
    v.add_argument("--shallow", action="store_true",
                   help="checksums only; skip decompressing every file "
                        "(fast, but cannot detect a short decode)")
    v.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
