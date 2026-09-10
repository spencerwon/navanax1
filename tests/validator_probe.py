"""Validator probes for feat/explorer-integration (commits 053fafb, eeb9b79).

Stdlib only; `python3 tests/validator_probe.py`. These pin behaviour the
author's suite did not exercise. Each check states what the code DOES; where
that is a policy choice rather than a defect the docstring says so.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from navanax.codec import GzipCodec  # noqa: E402
from navanax.landing import LandingZoneWriter  # noqa: E402
from navanax.metrics import MetricEngine, load_intervals, standing_sql  # noqa: E402
from navanax.normalize import (  # noqa: E402
    COLS,
    Normalizer,
    iso_to_ts,
    parse_event,
    parse_trait_criteria,
)
from navanax.traits import ensure_schema, import_explorer_cache, open_store  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"\n        {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# import-traits
# ---------------------------------------------------------------------------
def probe_import_refuses_overwrite(tmp: Path) -> None:
    conn = open_store(tmp / "a.sqlite")
    conn.execute("INSERT INTO tokens (collection, token_id, listed_at, traits_at, traits_source) "
                 "VALUES ('argonauts','7','2026-09-09T00:00:00Z','2026-09-09T01:00:00Z','opensea_nft')")
    conn.execute("INSERT INTO traits VALUES ('argonauts','7','Cloak','Death')")
    conn.commit()
    cache = {"7": {"id": "7", "traits": {"Cloak": "Clergy", "Relic": "Gold"}}}
    r = import_explorer_cache(conn, cache, slug="argonauts", generated_at="2026-09-08T00:00:00Z")
    rows = sorted(conn.execute("SELECT trait_type, value FROM traits WHERE token_id='7'"))
    src = conn.execute("SELECT traits_source, traits_at FROM tokens WHERE token_id='7'").fetchone()
    check("import: opensea_nft traits are not overwritten, not extended, and the source/time are untouched",
          rows == [("Cloak", "Death")] and src == ("opensea_nft", "2026-09-09T01:00:00Z"), f"{rows} {src} {r}")
    check("import: a trait_type present in the cache but absent from the store is reported as a disagreement, not silently added",
          any(d["trait_type"] == "Relic" and d["stored"] == [] for d in r["disagreements"]), str(r["disagreements"]))
    conn.close()


def probe_import_empty_traits_dict(tmp: Path) -> None:
    """A cache entry with `traits: {}` while the STORE has traits for that token.
    The code counts it under cache_tokens_without_traits and `continue`s BEFORE
    the diff, so store-vs-cache is never compared for it. Empty-as-unknown is a
    defensible policy, but the operator is not told the two sources differ."""
    conn = open_store(tmp / "b.sqlite")
    conn.execute("INSERT INTO tokens (collection, token_id, listed_at, traits_at, traits_source) "
                 "VALUES ('argonauts','8','2026-09-09T00:00:00Z','2026-09-09T01:00:00Z','opensea_nft')")
    conn.execute("INSERT INTO traits VALUES ('argonauts','8','Cloak','Death')")
    conn.commit()
    r = import_explorer_cache(conn, {"8": {"id": "8", "traits": {}}}, slug="argonauts", generated_at="2026-09-08T00:00:00Z")
    check("import: empty traits dict is counted as cache_empty and never stamps traits_at",
          r["cache_empty"] == 1 and r["imported"] == 0)
    check("import (policy, now documented): empty cache traits mean UNKNOWN -- not a disagreement, but counted in cache_empty_vs_stored",
          r["diffed_disagree"] == 0 and r["skipped_already_had"] == 0 and r["cache_empty_vs_stored"] == 1)
    # store side: a token with traits_at set but ZERO trait rows (can happen after a manual DELETE) diffs as "stored=[]"
    conn.execute("INSERT INTO tokens (collection, token_id, listed_at, traits_at, traits_source) "
                 "VALUES ('argonauts','9','2026-09-09T00:00:00Z','2026-09-09T01:00:00Z','opensea_nft')")
    conn.commit()
    r = import_explorer_cache(conn, {"9": {"id": "9", "traits": {"Cloak": "X"}}}, slug="argonauts", generated_at="2026-09-08T00:00:00Z")
    check("import: traits_at set with no trait rows is treated as 'has traits' -> cache is refused and reported",
          r["imported"] == 0 and r["diffed_disagree"] == 1, str(r))
    conn.close()


def probe_import_generated_zero(tmp: Path) -> None:
    """CLI: `--generated 0` is not None, so epoch 0 (1970) would be stamped. Refusal only covers absence."""
    from navanax.cli import main as cli_main
    root = tmp / "root0"
    (root / "config").mkdir(parents=True)
    (root / "config" / "base.yaml").write_text("environment: local\nanalytical:\n  path: data/an.sqlite\n"
                                               "opstore:\n  path: data/ops.db\nlanding:\n  root: data/landing\n")
    (root / "config" / "watchlist.yaml").write_text("collections:\n  - slug: argonauts\n")
    tj = tmp / "tokens.json"
    tj.write_text(json.dumps({"1": {"id": "1", "traits": {"Cloak": "Death"}}}))
    rc = cli_main(["--root", str(root), "import-traits", str(tj), "--collection", "argonauts", "--generated", "0"])
    exists = (root / "data" / "an.sqlite").exists()
    ts = None
    if exists:
        conn = sqlite3.connect(root / "data" / "an.sqlite")
        ts = conn.execute("SELECT traits_at FROM tokens WHERE token_id='1'").fetchone()
        conn.close()
    check("import CLI (fixed): --generated 0 is refused (exit 2) and nothing is stamped",
          rc == 2 and ts is None, f"rc={rc} traits_at={ts}")


# ---------------------------------------------------------------------------
# standing_sql lifecycle
# ---------------------------------------------------------------------------
def _store(tmp: Path, name: str) -> Normalizer:
    n = Normalizer(tmp / f"lz-{name}", tmp / f"{name}.sqlite")
    ensure_schema(n.conn)
    n.conn.execute("INSERT INTO tokens (collection, token_id, listed_at) VALUES ('argonauts','1','2026-09-09T00:00:00Z')")
    n.conn.execute("INSERT INTO traits VALUES ('argonauts','1','Cloak','Death')")
    return n


def _put(n: Normalizer, seq: int, etype: str, ts: str, oh: str, **over) -> None:
    row = {c: None for c in COLS}
    row.update({"run": "r", "seq": seq, "file": "f", "observed_at": ts, "valid_at": ts,
                "observed_ts": iso_to_ts(ts), "valid_ts": iso_to_ts(ts), "event_type": etype,
                "collection": "argonauts", "token_id": "1", "order_hash": oh, "price_eth": 1.0, "quantity": 1})
    row.update(over)
    n.conn.execute(f"INSERT INTO events ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})", tuple(row[c] for c in COLS))


def _asks(n: Normalizer, now_ts: float) -> list[str]:
    return [r[0] for r in n.conn.execute(
        f"SELECT e.order_hash FROM events e WHERE e.collection='argonauts' AND e.event_type='item_listed' "
        f"AND (e.expiration_ts IS NULL OR e.expiration_ts > ?) AND {standing_sql('e')}", (now_ts,))]


def probe_lifecycle(tmp: Path) -> None:
    n = _store(tmp, "life")
    now = iso_to_ts("2026-09-09T12:00:00Z")
    # A: listed, cancelled, invalidated, revalidated -> cancel must remain final
    _put(n, 1, "item_listed", "2026-09-09T10:00:00Z", "A")
    _put(n, 2, "item_cancelled", "2026-09-09T10:01:00Z", "A")
    _put(n, 3, "order_invalidate", "2026-09-09T10:02:00Z", "A")
    _put(n, 4, "order_revalidate", "2026-09-09T10:03:00Z", "A")
    # B: listed, invalidated, revalidated, invalidated again -> dead
    _put(n, 5, "item_listed", "2026-09-09T10:00:00Z", "B")
    _put(n, 6, "order_invalidate", "2026-09-09T10:01:00Z", "B")
    _put(n, 7, "order_revalidate", "2026-09-09T10:02:00Z", "B")
    _put(n, 8, "order_invalidate", "2026-09-09T10:03:00Z", "B")
    # C: listed, sold, revalidated -> sold is final
    _put(n, 9, "item_listed", "2026-09-09T10:00:00Z", "C")
    _put(n, 10, "item_sold", "2026-09-09T10:01:00Z", "C")
    _put(n, 11, "order_revalidate", "2026-09-09T10:02:00Z", "C")
    # D: invalidate and revalidate share the SAME valid_ts (>= makes the tie reopen)
    _put(n, 12, "item_listed", "2026-09-09T10:00:00Z", "D")
    _put(n, 13, "order_invalidate", "2026-09-09T10:01:00Z", "D")
    _put(n, 14, "order_revalidate", "2026-09-09T10:01:00Z", "D")
    # E: revalidate BEFORE invalidate (out-of-order arrival) -> invalidation is last word, dead
    _put(n, 15, "item_listed", "2026-09-09T10:00:00Z", "E")
    _put(n, 16, "order_revalidate", "2026-09-09T10:01:00Z", "E")
    _put(n, 17, "order_invalidate", "2026-09-09T10:02:00Z", "E")
    # F: NULL expiration_ts, nothing else -> standing (explicit in predicate)
    _put(n, 18, "item_listed", "2026-09-09T10:00:00Z", "F", expiration_ts=None, expiration_at=None)
    # G: listing whose cancel arrived with the SAME valid_ts as placement (>= makes it dead)
    _put(n, 19, "item_listed", "2026-09-09T10:00:00Z", "G")
    _put(n, 20, "item_cancelled", "2026-09-09T10:00:00Z", "G")
    n.conn.commit()
    live = set(_asks(n, now))
    check("standing: cancel is final even after invalidate -> revalidate (A dead)", "A" not in live, str(live))
    check("standing: invalidate -> revalidate -> invalidate is dead (B)", "B" not in live, str(live))
    check("standing: sold is final even after a later revalidate (C)", "C" not in live, str(live))
    check("standing (flagged assumption): invalidate and revalidate at the SAME valid_ts reopen the order (D)", "D" in live, str(live))
    check("standing: revalidate before invalidate does not rescue it (E dead)", "E" not in live, str(live))
    check("standing (flagged policy): NULL expiration_ts is treated as never-expiring and standing (F)", "F" in live, str(live))
    check("standing: a cancel at the same valid_ts as placement kills it (G)", "G" not in live, str(live))
    eng = MetricEngine(n.conn, load_intervals(ROOT / "config" / "intervals.yaml"), "America/Chicago")
    b = eng.trait_book("argonauts", now=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc))
    cell = b["traits"]["Cloak"]["Death"]
    check("trait_book agrees with the raw predicate: token 1 has a standing ask (D or F) and ask_n counts tokens not orders",
          cell["ask"] == 1.0 and cell["ask_n"] == 1, str(cell))
    n.close()


# ---------------------------------------------------------------------------
# parse_event / backfill
# ---------------------------------------------------------------------------
def _real_trait_offer_line() -> str:
    for line in (ROOT / "tools" / "sample_frames.jsonl").read_text().splitlines():
        if '"trait_offer"' in line:
            return line
    raise RuntimeError("no trait_offer frame in tools/sample_frames.jsonl")


def probe_parse_trait_criteria() -> None:
    fr = json.loads(_real_trait_offer_line())
    p = fr[4]["payload"]
    p["trait_criteria"] = {}
    p["trait_criteria_list"] = [{"trait_type": "Cloak", "trait_name": "Death"}, {"trait_type": "Relic", "trait_name": "Gold"}]
    env = {"_seq": 1, "_run": "r", "_recv": "2026-09-09T00:00:00Z", "_topic": fr[2], "_ets": p.get("event_timestamp"),
           "raw": json.dumps(fr, separators=(",", ":"))}
    row = parse_event(env)
    check("parse_event: list of 2 with empty trait_criteria -> first pair, n=2, JSON present, token_id None",
          row["trait_type"] == "Cloak" and row["trait_value"] == "Death" and row["trait_criteria_n"] == 2
          and row["trait_criteria_json"] is not None and row["token_id"] is None, str({k: row[k] for k in ("trait_type", "trait_value", "trait_criteria_n", "token_id")}))
    check("parse_event: trait_criteria_json is deterministic (sort_keys) so backfill re-runs write identical bytes",
          json.loads(row["trait_criteria_json"])["traits"][1]["trait_name"] == "Gold")
    t, v, nn, js = parse_trait_criteria({"trait_criteria": {}, "trait_criteria_list": [], "numeric_trait_criteria_list": [{"trait_type": "Level", "min": 1}]})
    check("parse_event: a numeric-only criterion yields n=1 with trait_type NULL -> counted under no single trait; the 'without "
          "criteria' counter keys on trait_criteria_n IS NULL so it is not reported as unparsed",
          (t, v, nn) == (None, None, 1) and js is None, str((t, v, nn, js)))
    # collection_offer with a real Seaport criteria item: identifierOrCriteria "0" as a STRING is truthy
    p2 = {"event_type": "collection_offer", "sent_at": "2026-09-09T10:19:24Z",
          "payload": {"chain": "ethereum", "collection": {"slug": "argonauts"}, "event_timestamp": "2026-09-09T10:19:23Z",
                      "base_price": "1", "order_hash": "0xc0", "quantity": 1,
                      "payment_token": {"decimals": 18, "eth_price": "1", "symbol": "WETH", "usd_price": "1"},
                      "protocol_data": {"parameters": {"offer": [{"itemType": 1, "identifierOrCriteria": "0"}],
                                                       "consideration": [{"itemType": 4, "identifierOrCriteria": "0", "token": "0xabc"}]}}}}
    env2 = {"_seq": 2, "_run": "r", "_recv": "2026-09-09T00:00:00Z", "_topic": "collection:argonauts", "_ets": None,
            "raw": json.dumps(["1", None, "collection:argonauts", "collection_offer", p2])}
    row2 = parse_event(env2)
    check("parse_event: itemType 4 with identifierOrCriteria '0' (string) no longer becomes token_id '0' on a collection_offer",
          row2 is not None and row2["token_id"] is None, str(row2 and row2["token_id"]))


def probe_backfill_idempotent_and_readonly(tmp: Path) -> None:
    lz = tmp / "lz-bf"
    w = LandingZoneWriter(lz, "run-bf", codec=GzipCodec(), auto_flush=False)
    line = _real_trait_offer_line()
    fr = json.loads(line)
    # one trait_offer + one item_received_bid (its token_id must be re-derived, not left as the bogus '0')
    bid = None
    for ln in (ROOT / "tools" / "sample_frames.jsonl").read_text().splitlines():
        if '"item_received_bid"' in ln:
            bid = json.loads(ln)
            break
    for raw in (fr, bid):
        w.write(json.dumps(raw, separators=(",", ":")), topic=raw[2], event_timestamp=raw[4].get("sent_at"))
    w.close()
    digest = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(lz.rglob("*")) if p.is_file())).hexdigest()
    n = Normalizer(lz, tmp / "bf.sqlite")
    n.sync()
    before = sorted(n.conn.execute("SELECT * FROM events"))
    st1 = n.backfill_trait_criteria()
    mid = sorted(n.conn.execute("SELECT * FROM events"))
    st2 = n.backfill_trait_criteria()
    after = sorted(n.conn.execute("SELECT * FROM events"))
    cnt = n.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("backfill on already-filled columns: rows identical before/after, no duplicates, nothing rewritten (rows_updated 0)",
          before == mid == after and cnt == 2 and st1["rows_updated"] == 0 and st2["rows_updated"] == 0, f"{st1} {st2} n={cnt}")
    digest2 = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(lz.rglob("*")) if p.is_file())).hexdigest()
    check("backfill: landing zone bytes unchanged", digest == digest2)
    # a trait_offer row whose token_id was hand-set to the Merkle root AND a non-trait row with a bogus token_id
    n.conn.execute("UPDATE events SET token_id='831572299943853139516' WHERE event_type='trait_offer'")
    n.conn.execute("UPDATE events SET token_id='0' WHERE event_type='item_received_bid'")
    n.conn.commit()
    n.backfill_trait_criteria()
    tid = n.conn.execute("SELECT token_id FROM events WHERE event_type='trait_offer'").fetchone()[0]
    other = n.conn.execute("SELECT token_id FROM events WHERE event_type='item_received_bid'").fetchone()[0]
    check("backfill (fixed): Merkle-root token_id on trait_offer is nulled AND a bogus token_id on another type is re-derived too",
          tid is None and other not in (None, "0") and other.isdigit() and len(other) < 20, f"{tid} {other}")
    n.close()


def probe_no_landing_writes() -> None:
    src = (ROOT / "src" / "navanax" / "normalize.py").read_text() + (ROOT / "src" / "navanax" / "traits.py").read_text()
    import re
    bad = [w for w in ("write_bytes", "write_text", ".rename(", "os.remove", ".unlink(", "shutil.") if w in src]
    bad += re.findall(r"(?<!url)open\(", src)
    check("static: normalize.py / traits.py contain no file-write primitives (landing zone is read-only from these paths)",
          bad == [], str(bad))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="nvx-probe-"))
    for fn in (probe_import_refuses_overwrite, probe_import_empty_traits_dict, probe_import_generated_zero,
               probe_lifecycle, probe_backfill_idempotent_and_readonly):
        fn(tmp)
    probe_parse_trait_criteria()
    probe_no_landing_writes()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAIL", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
