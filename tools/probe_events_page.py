#!/usr/bin/env python3
"""PR-0.1 — what page size does the OpenSea events endpoint ACTUALLY return?

    PYTHONPATH=src python3 tools/probe_events_page.py

One governed GET of `/events/collection/<slug>?limit=200`, and nothing else.
It settles E-U1 in `docs/proposals/TECHLEAD_2026-09-09_factcheck.md`: REQ-D-13
(`docs/00_REQUIREMENTS.md:124`) and `docs/07 §3.2` both state "up to 200 per
page", both are internal documents, and neither carries a verification note.
The whole backfill arithmetic — how many hours of history one REST read buys —
is scaled off that number, so it is worth one token to stop assuming it.

WHAT THIS SPENDS: exactly one REST read, at INTERACTIVE priority, with
`retries=0` so that a 429 or a 500 comes back as the answer rather than turning
one read into four. The call goes through `RestClient`, so the spend lands in
`data/ops.db`'s `rest_ledger` like every other read in the system. If the
governor reports fewer than five tokens available the probe REFUSES and spends
nothing: a measurement is never worth starving the recorder's backfill.

WHAT THIS NEVER TOUCHES: the landing zone (`data/landing/`) and the analytical
store (`data/analytics.sqlite`). It opens `data/ops.db` for the ledger write and
for no other reason.

The answer is appended to `docs/measurements/2026-09-11_events_page_size.md`,
dated, with the raw headers, because a measurement nobody can find later is an
assumption again within a week.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from navanax.governor import Priority  # noqa: E402

# The floor below which this probe will not spend. Five rather than one so that
# the probe cannot be the read that tips an already-drained bucket into the
# recorder's backfill reserve.
MIN_TOKENS = 5.0

MEASUREMENT_PATH = Path("docs/measurements/2026-09-11_events_page_size.md")

HEADER_KEYS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
               "retry-after", "cf-cache-status")

DOC_HEADER = """\
# Measurement — the events endpoint's page size at `limit=200`

*Settles E-U1 (`docs/proposals/TECHLEAD_2026-09-09_factcheck.md` §2 and §3 PR-0.1).
Recorded here because `REQ-D-13` (`docs/00_REQUIREMENTS.md`) and
`docs/07_STORAGE_AND_RECORDING.md` §3.2 both assert "up to 200 per page" and
neither carries a verification note. Written by `tools/probe_events_page.py`;
each run appends one dated entry and edits none of the ones above it.*

`cf-cache-status: HIT` on the reply means the rate-limit headers may be a cached
reading rather than a live one — the same caveat that attaches to the measured
120 reads/hour (BUG-20260909-003). It does **not** affect the item count, which
is the number this file exists to record.

"""


class BudgetRefusal(RuntimeError):
    """Not enough REST budget to spend on a measurement. Nothing was called."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def extract_items(body: Any) -> tuple[str | None, list | None]:
    """Find the array of events in the response and say which key held it.

    `asset_events` is the documented key. The fallback exists because the point
    of this probe is that the documentation has not been checked: if OpenSea
    renamed the key, a probe that returned "0 items" would be a wrong answer
    wearing a right answer's clothes.
    """
    if not isinstance(body, dict):
        return None, None
    if isinstance(body.get("asset_events"), list):
        return "asset_events", body["asset_events"]
    for k, v in body.items():
        if isinstance(v, list):
            return k, v
    return None, None


async def probe(rest, *, slug: str = "argonauts", limit: int = 200,
                minimum_tokens: float = MIN_TOKENS) -> dict[str, Any]:
    """One governed GET. Raises BudgetRefusal — before calling anything — if poor."""
    available = float(rest.gov.bucket.available())
    if available < minimum_tokens:
        raise BudgetRefusal(
            f"the REST governor reports {available:.1f} tokens available and this probe "
            f"refuses below {minimum_tokens:.0f}. Nothing was requested and nothing was "
            f"spent. The bucket refills at {rest.gov.bucket.state.refill_per_second * 3600:.0f} "
            f"reads/hour, so waiting is the whole fix."
        )
    path = f"/events/collection/{slug}"
    params = {"limit": limit}
    status, body = await rest.get(path, params, priority=Priority.INTERACTIVE, retries=0)
    headers = dict(getattr(rest, "last_headers", {}) or {})
    key, items = extract_items(body)
    nxt = body.get("next") if isinstance(body, dict) else None
    return {
        "at": _now_iso(),
        "path": path,
        "requested_limit": limit,
        "status": status,
        "items_key": key,
        "items_returned": None if items is None else len(items),
        "next_present": bool(nxt),
        "next_sample": (nxt[:24] + "...") if isinstance(nxt, str) and len(nxt) > 24 else nxt,
        "headers": {k: headers[k] for k in HEADER_KEYS if k in headers},
        "all_header_names": sorted(headers),
        "requests_made": getattr(rest, "requests_made", None),
        "body_keys": sorted(body) if isinstance(body, dict) else None,
        "error_body": None if status == 200 else json.dumps(body)[:400],
    }


def verdict(r: dict[str, Any]) -> str:
    """One sentence a reader can act on, including when the answer is 'no answer'."""
    if r["status"] != 200:
        return (f"**No answer. The request returned HTTP {r['status']}, so the page size is "
                f"still unverified and REQ-D-13's `up to 200 per page` remains an assumption.** "
                f"One read was spent.")
    n = r["items_returned"]
    if n is None:
        return ("**No answer. The reply was HTTP 200 but carried no array of events, so the "
                "page size is still unverified.** One read was spent.")
    req = r["requested_limit"]
    if n == req:
        tail = (f"The endpoint honoured `limit={req}` exactly, which is what REQ-D-13 assumed. "
                f"Backfill arithmetic scaled on 200 per page stands.")
    elif n < req and r["next_present"]:
        tail = (f"The endpoint CAPPED the page below the requested `limit={req}` and handed back "
                f"a `next` cursor, so a full page is {n}, not {req}. Every backfill estimate "
                f"scaled on {req} per page is optimistic by {req / n:.1f}x and must be rescaled "
                f"before PR-10 is sized.")
    elif n < req:
        tail = (f"Fewer than `limit={req}` came back and there is no `next` cursor, so this is "
                f"the end of the available history rather than a page cap — the ceiling is "
                f"still unmeasured. Re-run against a busier collection or a wider window.")
    else:
        tail = (f"The endpoint returned MORE than the requested `limit={req}`, which no reading "
                f"of the documentation predicts. Treat this as a bug in the probe until a "
                f"second run reproduces it.")
    return f"**Answer: {n} items for `limit={req}`.** {tail}"


def render_entry(r: dict[str, Any]) -> str:
    """The dated markdown block appended to the measurement file."""
    hdrs = r["headers"]
    rows = [
        ("Requested `limit`", f"`{r['requested_limit']}`"),
        ("HTTP status", str(r["status"])),
        ("Items returned", "(no array in the reply)" if r["items_returned"] is None
         else str(r["items_returned"])),
        ("Array key", f"`{r['items_key']}`" if r["items_key"] else "(none)"),
        ("`next` cursor present", "yes" if r["next_present"] else "no"),
    ]
    for k in HEADER_KEYS:
        rows.append((f"`{k}`", f"`{hdrs[k]}`" if k in hdrs else "(absent)"))
    rows.append(("REST reads spent",
                 f"{r['requests_made']} (INTERACTIVE, recorded in `data/ops.db` `rest_ledger`)"))
    rows.append(("Probe", "`tools/probe_events_page.py`"))
    body = "\n".join(f"| {a} | {b} |" for a, b in rows)
    out = [f"## {r['at']} — `GET {r['path']}?limit={r['requested_limit']}`",
           "",
           "| Field | Value |",
           "|---|---|",
           body,
           "",
           verdict(r),
           ""]
    if r["error_body"]:
        out += ["", "Response body (truncated):", "", "```", r["error_body"], "```", ""]
    out += ["Header names the reply actually carried: "
            + (", ".join(f"`{h}`" for h in r["all_header_names"]) or "(none)") + ".",
            ""]
    return "\n".join(out)


def append_entry(path: Path, entry: str) -> None:
    """Append, never rewrite. Earlier measurements are evidence, not drafts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DOC_HEADER + entry)
    else:
        old = path.read_text()
        path.write_text(old + ("" if old.endswith("\n") else "\n") + "\n---\n\n" + entry)


def _print_result(r: dict[str, Any]) -> None:
    print(f"  status               {r['status']}")
    print(f"  items returned       {r['items_returned']}"
          f"{'' if r['items_key'] is None else '   (key: ' + r['items_key'] + ')'}")
    print(f"  `next` present       {'yes' if r['next_present'] else 'no'}"
          f"{'' if not r['next_present'] else '   ' + str(r['next_sample'])}")
    print("  rate-limit headers:")
    for k in HEADER_KEYS:
        print(f"    {k:<24} {r['headers'].get(k, '(absent)')}")
    print(f"  reads spent          {r['requests_made']}")
    print()
    print("  " + verdict(r).replace("**", ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--slug", default=None, help="default: the first watchlist collection")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--base", default=None,
                    help="REST base URL; default: config/base.yaml opensea.rest_base")
    ap.add_argument("--out", default=str(MEASUREMENT_PATH))
    args = ap.parse_args(argv)

    from navanax.cli import _config
    from navanax.dotenv import DotenvError, require
    from navanax.governor import governor_from_config
    from navanax.opstore import OperationalStore
    from navanax.rest import BASE, RestClient

    root = Path(args.root)
    cfg, slugs = _config(root)
    slug = args.slug or (slugs[0] if slugs else "argonauts")
    try:
        key = require("OPENSEA_API_KEY", path=root / ".env")
    except DotenvError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # The base URL comes from config, not from a literal here (REQ-N-09).
    base = args.base or (cfg.get("opensea") or {}).get("rest_base") or BASE
    store = OperationalStore(root / cfg["opstore"]["path"])
    gov = governor_from_config(cfg)
    rest = RestClient(key, gov, base=base, run_id="probe-events-page", ledger=store.log_rest)

    print(f"collection           {slug}")
    print(f"request              GET /events/collection/{slug}?limit={args.limit}")
    print(f"budget model         {gov.bucket.state.capacity:.0f} reads/hour "
          f"({gov.bucket.state.capacity_source}); {gov.bucket.available():.1f} available now")
    print(f"cost                 1 read, INTERACTIVE, no retries. Refuses below {MIN_TOKENS:.0f}.")
    print("writes               docs/measurements/ and data/ops.db's ledger. "
          "Never the landing zone, never the analytical store.")
    print()

    try:
        r = asyncio.run(probe(rest, slug=slug, limit=args.limit))
    except BudgetRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 6
    except Exception as exc:  # noqa: BLE001 - the operator needs the reason, not a traceback
        print(f"the request failed before it produced an answer: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 5

    _print_result(r)
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    append_entry(out, render_entry(r))
    print()
    print(f"recorded in          {out}")
    return 0 if r["status"] == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
