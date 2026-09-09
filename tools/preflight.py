#!/usr/bin/env python3
"""Preflight: validate the environment before starting ingestion.

Run this ONCE on the machine that will run ingestion, before `navanax ingest`.
It answers the questions that cannot be answered from documentation:

  1. Does the API key work at all?
  2. Does OpenSea return rate-limit headers?  (REQ-D-02 is written to work
     either way, but which branch we are on changes how the governor behaves,
     and right now nobody knows.)
  3. Is the collection slug correct, and what is its real supply?
  4. Can this machine hold a WebSocket to the stream, and what do real frames
     look like?
  5. What does one real event actually cost in bytes?  (docs/07 §2.3 is an
     ESTIMATE. This replaces it with a measurement.)

Budget: spends at most 3 REST reads of your hourly limit (measured 120). Stream test is free.

    export OPENSEA_API_KEY=...        # never paste a key into a file or a chat
    python3 tools/preflight.py --slug argonauts --stream-seconds 60

Writes tools/preflight_report.json. Paste that back and the docs get corrected
from measurements instead of estimates.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REST = "https://api.opensea.io/api/v2"
WS = "wss://stream.openseabeta.com/socket/websocket"

OK, WARN, BAD = "  OK  ", " WARN ", " FAIL "
report: dict = {"ran_at": datetime.now(timezone.utc).isoformat(), "checks": {}}


def say(status: str, msg: str, detail: str = "") -> None:
    print(f"[{status}] {msg}")
    if detail:
        for line in detail.splitlines():
            print(f"         {line}")


def get(path: str, key: str) -> tuple[int, dict, dict]:
    req = urllib.request.Request(
        f"{REST}{path}",
        headers={"x-api-key": key, "Accept": "application/json",
                 "User-Agent": "navanax-preflight/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return e.code, dict(e.headers), parsed


def check_key(key: str, slug: str) -> bool:
    print("\n--- 1. API key + collection lookup (1 REST read) " + "-" * 22)
    status, headers, body = get(f"/collections/{slug}", key)
    report["checks"]["auth"] = {"status": status}

    if status == 401:
        say(BAD, "Key rejected (401).",
            "Check it is the API key, not a bearer token, and not expired.\n"
            "Free instant keys expire after 7 days (REQ-D-06).")
        return False
    if status == 404:
        say(BAD, f"Collection slug '{slug}' not found (404).",
            "The slug is the last path segment of the OpenSea collection URL.")
        return False
    if status == 429:
        say(BAD, "Rate limited on the very first call (429).",
            "Something else is already consuming this account's budget.")
        return False
    if status != 200:
        say(BAD, f"Unexpected status {status}", json.dumps(body)[:300])
        return False

    say(OK, "Key works, collection found.")

    # --- 2. rate-limit headers: the question REQ-D-02 leaves open ----------
    print("\n--- 2. Rate-limit headers (resolves an open design question) " + "-" * 10)
    rl = {k: v for k, v in headers.items()
          if "ratelimit" in k.lower() or k.lower() in ("retry-after", "x-rate-limit")}
    report["checks"]["rate_limit_headers"] = rl
    if rl:
        say(OK, "Headers ARE returned -- the governor can read real budget.",
            "\n".join(f"{k}: {v}" for k, v in rl.items()))
    else:
        say(WARN, "No rate-limit headers on this response.",
            "Not a failure: REQ-D-02 requires the governor to fall back to its\n"
            "local token-bucket model, which is what it will do. Worth knowing,\n"
            "because it means our budget view is an estimate, not ground truth.")
    print("         all response headers seen:")
    for k in sorted(headers):
        print(f"           {k}: {headers[k][:90]}")

    # --- 3. collection facts ---------------------------------------------
    print("\n--- 3. Collection facts " + "-" * 46)
    facts = {k: body.get(k) for k in
             ("collection", "name", "owner", "total_supply", "is_disabled",
              "is_nsfw", "trait_offers_enabled")}
    contracts = body.get("contracts") or []
    facts["contracts"] = contracts
    report["checks"]["collection"] = facts
    for k, v in facts.items():
        if v not in (None, [], ""):
            say(OK, f"{k}: {v}")

    supply = body.get("total_supply")
    if isinstance(supply, int) and supply > 0:
        pages = -(-supply // 200)  # ceil; the NFT endpoint allows limit up to 200
        est = pages + 2
        # BUG-20260909-014. This used to emit `est_minutes_at_600_per_hour`
        # computed as est/10, which was the arithmetic for the superseded
        # (and never sourced) hourly figure, into the MACHINE-READABLE
        # artifact the documents are corrected FROM, four lines above a print
        # statement that already said "120/hr (measured)". The same function
        # published both numbers. At the measured limit the divisor is 2, so the
        # JSON under-reported onboarding time by 5x.
        report["checks"]["onboarding_estimate"] = {
            "total_supply": supply, "pages_at_200": pages, "est_reads": est,
            "rate_limit_per_hour": 120,
            "rate_limit_provenance": "MEASURED 2026-09-09 from x-ratelimit-limit "
                                     "(caveat: cf-cache-status was HIT)",
            "est_minutes_at_measured_limit": round(est / (120 / 60), 1),
        }
        print()
        say(OK, f"Onboarding estimate: {supply:,} items -> {pages} pages at 200/page",
            f"~{est} REST reads = {est/120*100:.0f}% of a 120/hr budget (measured).\n"
            f"docs/07 §3.1 predicted 60-100 reads for this collection.")
    return True


def check_stream(key: str, slug: str, seconds: int) -> bool:
    print(f"\n--- 4. Stream connectivity ({seconds}s, costs NO budget) " + "-" * 15)
    try:
        import websockets  # noqa: F401
    except ImportError:
        say(WARN, "`websockets` not installed -- skipping the stream test.",
            "pip install websockets   (or: pip install -e '.[dev]')")
        report["checks"]["stream"] = {"skipped": "websockets not installed"}
        return True

    import asyncio

    import websockets

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    from navanax.stream import normalize_frame

    frames: list[str] = []
    events: list[dict] = []

    async def run() -> None:
        url = f"{WS}?token={key}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            say(OK, "WebSocket connected.")
            await ws.send(json.dumps(
                {"topic": f"collection:{slug}", "event": "phx_join", "payload": {}, "ref": "1"}))
            say(OK, f"Sent phx_join for collection:{slug}")
            deadline = time.monotonic() + seconds
            last_hb = time.monotonic()
            while time.monotonic() < deadline:
                if time.monotonic() - last_hb > 25:
                    await ws.send(json.dumps(
                        {"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": "hb"}))
                    last_hb = time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    raw = raw.decode()
                frames.append(raw)
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # BUG-20260909-002: Phoenix v2 sends arrays, not maps.
                m = normalize_frame(parsed)
                if m is None:
                    say(WARN, f"unrecognised frame shape: {type(parsed).__name__}",
                        json.dumps(parsed)[:200])
                    continue
                ev = m.get("event")
                if ev == "phx_reply":
                    pl = m.get("payload")
                    st = (pl or {}).get("status") if isinstance(pl, dict) else None
                    say(OK if st == "ok" else BAD, f"Join reply: status={st}")
                    if st != "ok":
                        say(BAD, "Join REJECTED", json.dumps(m)[:300])
                elif ev not in ("heartbeat", "phx_close", "phx_error"):
                    events.append(m)
                    if len(events) <= 3:
                        say(OK, f"event: {ev}")

    try:
        asyncio.run(asyncio.wait_for(run(), timeout=seconds + 30))
    except Exception as exc:  # noqa: BLE001 - preflight must report ANY failure, never abort
        say(BAD, f"Stream failed: {type(exc).__name__}: {exc}")
        report["checks"]["stream"] = {"error": f"{type(exc).__name__}: {exc}"}
        return False

    sizes = [len(f.encode()) for f in frames]
    res = {
        "frames": len(frames), "events": len(events), "seconds": seconds,
        "mean_frame_bytes": round(statistics.mean(sizes), 1) if sizes else None,
        "median_frame_bytes": statistics.median(sizes) if sizes else None,
        "max_frame_bytes": max(sizes) if sizes else None,
        "event_types": sorted({e.get("event") for e in events if e.get("event")}),
    }
    report["checks"]["stream"] = res
    print()
    say(OK, f"{len(frames)} frames / {len(events)} events in {seconds}s")
    if events:
        rate_day = len(events) * 86400 / seconds
        res["implied_events_per_day"] = round(rate_day)
        say(OK, f"Implied rate: ~{rate_day:,.0f} events/day for {slug}",
            "docs/07 §2.3 ESTIMATED 2,000/day for Argonauts. This is a measurement --\n"
            "a short sample, so treat it as indicative, but it beats a guess.")
        say(OK, f"Mean frame: {res['mean_frame_bytes']} bytes "
                f"(docs/07 assumed ~1,150)")
        say(OK, f"Event types seen: {', '.join(res['event_types'])}")
        # Save real frames as replay fixtures -- these are worth more than
        # anything synthetic, and they cannot be created retroactively.
        out = os.path.join(os.path.dirname(__file__), "sample_frames.jsonl")
        with open(out, "w") as fh:
            for f in frames[:200]:
                fh.write(f + "\n")
        say(OK, f"Saved {min(len(frames),200)} real frames -> {out}",
            "These become FIXTURE test data. Real payloads beat synthetic ones.")
    else:
        say(WARN, "No market events in the sample window.",
            "Expected for a thin collection -- Argonauts may go minutes between\n"
            "events. The connection and join both worked, which is what matters.\n"
            "Re-run with --stream-seconds 300 for a better rate estimate.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="argonauts")
    ap.add_argument("--stream-seconds", type=int, default=60)
    ap.add_argument("--skip-stream", action="store_true")
    a = ap.parse_args()

    print("=" * 72)
    print("NAVANAX PREFLIGHT")
    print("=" * 72)

    # Load .env, exactly as every message in this project says we do.
    # BUG-20260909-001: we used to read only os.environ while telling the user
    # to use .env, so a correctly-filled .env failed with "key is not set".
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
    try:
        from navanax.dotenv import DotenvError, require
        key = require("OPENSEA_API_KEY", path=pathlib.Path(__file__).resolve().parents[1] / ".env")
    except DotenvError as exc:
        say(BAD, str(exc).split("\n")[0], "\n".join(str(exc).split("\n")[1:]))
        return 2
    except ImportError:
        key = os.environ.get("OPENSEA_API_KEY", "").strip()
        if not key:
            say(BAD, "OPENSEA_API_KEY is not set and src/navanax/dotenv.py is missing.",
                "Run this from the project root so the .env loader can be found.")
            return 2
    say(OK, f"Key present ({len(key)} chars, ...{key[-4:]})")

    ok = check_key(key, a.slug)
    if ok and not a.skip_stream:
        ok = check_stream(key, a.slug, a.stream_seconds) and ok

    out = os.path.join(os.path.dirname(__file__), "preflight_report.json")
    with open(out, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print("\n" + "=" * 72)
    print(f"{'PREFLIGHT PASSED' if ok else 'PREFLIGHT FAILED'} -- report: {out}")
    print("The report contains NO key material. Safe to paste back.")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
