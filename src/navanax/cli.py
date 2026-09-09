"""Phase 0 entrypoint: start the stream, land every frame, record every gap.

    python -m navanax.cli ingest            # run the consumer
    python -m navanax.cli status            # ingestion health
    python -m navanax.cli verify            # re-verify landing-zone checksums

Every day this is not running is a day of history that cannot be bought back:
OpenSea publishes no historical floor series, so the record exists only because
we recorded it.
"""
from __future__ import annotations

import argparse, asyncio, json, logging, os, sys
from pathlib import Path

from .landing import LandingZoneWriter, verify_manifest
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


def cmd_ingest(args) -> int:
    root = Path(args.root)
    cfg, slugs = _config(root)
    if not slugs:
        print("watchlist is empty -- nothing to subscribe to", file=sys.stderr)
        return 2
    key = os.environ.get("OPENSEA_API_KEY", "")
    if not key:
        print(
            "OPENSEA_API_KEY is not set.\n"
            "  The stream is unmetered but still needs a key.\n"
            "  Get one: https://docs.opensea.io/reference/api-keys\n"
            "  Put it in .env (which is gitignored). Never in the repo.",
            file=sys.stderr)
        return 2

    run_id = new_run_id()
    lz = cfg["landing"]
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
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopping; flushing final frame...")
    finally:
        consumer.stop()
        writer.close()
        print(json.dumps(consumer.stats.as_dict(), indent=2))
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
    problems = verify_manifest(root / cfg["landing"]["root"])
    if not problems:
        print("landing zone verified clean")
        return 0
    print("S0a -- LANDING ZONE INTEGRITY FAILURE", file=sys.stderr)
    for p in problems:
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
    sub.add_parser("verify").set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
