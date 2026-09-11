"""Collection onboarding, part one: every token and its traits (REQ-F-01, REQ-F-07a).

Why this exists: the stream says what HAPPENED to token #7531. It never says
what #7531 IS. Trait filters, trait-tier pricing, the hedonic model -- all of
Phase 1's analysis -- need a table that says "#7531: Background=Blue, ...".

Where traits come from, and what it costs (docs/07 §3.1, measured budget 120/hr):

  1. `GET /collection/{slug}/nfts?limit=200`      ~47 governed reads for 9,212
     items. Gives each token's name, image, `metadata_url` -- AND, for
     Argonauts, a `traits: [{trait_type, value}]` list on every item.
     VERIFIED 2026-09-09 from a raw pull of that endpoint by a second tool
     (SpencerTinker/scrape.py, 44 reads, 8,798 tokens all with traits). An
     earlier version of this docstring asserted the opposite and the list
     pass threw the field away (BUG-20260909-054); it is now stored at once
     as `traits_source='opensea_nft_list'`. Other collections must be
     RE-VERIFIED -- the field may be absent or empty where OpenSea has not
     indexed metadata, and the paths below remain for those tokens.
  2. Each token's `metadata_url`, fetched DIRECTLY from wherever it is hosted
     (IPFS gateway, the project's own server). Not OpenSea, not metered by the
     governor. Carries `attributes: [{trait_type, value}]`. For Argonauts
     `metadata_url` is NULL on every token, so this path never applies there.
  3. Fallback, budget-limited: OpenSea's per-token endpoint, for tokens whose
     metadata cannot be fetched. Never used for the whole collection -- that
     would be 9,212 reads, 77 hours of budget.
  4. Offline: `navanax import-traits <tokens.json>` (or a double-click on
     import-traits.command) loads a cache another tool already pulled.
     `traits_source='explorer_cache:<sha256[:12]>'` names the exact file;
     `traits_at` is the cache's own generated time, never now. Zero REST
     reads. See `import_explorer_cache`.

Progress is written to the operational store's `onboarding` row for the
collection as it goes (state, items_done, requests_spent, last_cursor), so a
collection mid-onboarding is distinguishable from a broken one, and the job
is resumable from where it stopped.

Structure only. A trait is recorded as the metadata states it. Rarity,
tiering and pricing are Phase 1 assumptions and live above this layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .governor import Priority
from .normalize import BUSY_TIMEOUT_MS
from .rest import RestClient
from .tls import ssl_context

# Hosts the governor exists to protect. A metadata_url pointing here is NOT
# fetched directly: that would be a metered call outside the budget (REQ-D-01,
# tech-lead F4). Such tokens go through the governed fallback instead.
OPENSEA_HOSTS = ("opensea.io", "seadn.io", "openseauserdata.com")
MAX_METADATA_BYTES = 2_000_000


def is_opensea_host(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in OPENSEA_HOSTS)

log = logging.getLogger("navanax.traits")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    collection    TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    contract      TEXT,
    name          TEXT,
    image_url     TEXT,
    metadata_url  TEXT,
    listed_at     TEXT NOT NULL,          -- when the token list pass saw it
    traits_at     TEXT,                   -- when traits were fetched; NULL = not yet
    traits_source TEXT,                   -- metadata_url | opensea_nft | opensea_nft_list |
                                          -- explorer_cache[:<sha256[:12]> of the cache file] | none
    traits_error  TEXT,
    PRIMARY KEY (collection, token_id)
);
CREATE TABLE IF NOT EXISTS traits (
    collection  TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    trait_type  TEXT NOT NULL,
    value       TEXT NOT NULL,
    PRIMARY KEY (collection, token_id, trait_type, value)   -- a token may carry two values of one type; keep both
);
CREATE INDEX IF NOT EXISTS ix_traits_lookup ON traits(collection, trait_type, value, token_id);
-- One row per `navanax import-traits` run. Additive: nothing reads it to
-- compute a number, and dropping it would lose provenance, not data. It exists
-- because `traits_source='explorer_cache'` names no PARTICULAR cache -- two
-- caches that disagree are indistinguishable after the fact without the digest.
CREATE TABLE IF NOT EXISTS trait_imports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collection    TEXT NOT NULL,
    imported_at   TEXT NOT NULL,          -- when THIS table saw the cache
    generated_at  TEXT NOT NULL,          -- when the cache's traits were OBSERVED
    cache_file    TEXT,
    cache_bytes   INTEGER,
    cache_sha256  TEXT,
    traits_source TEXT NOT NULL,          -- what went into tokens.traits_source
    counts_json   TEXT NOT NULL           -- the run's exact counts, verbatim
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_attributes(meta: dict[str, Any]) -> list[tuple[str, str]]:
    """Token metadata JSON -> [(trait_type, value)]. Tolerant of the common shapes.

    ERC-721 metadata conventionally carries `attributes: [{trait_type, value}]`;
    some collections use `traits`, some use a dict, some put numbers in
    `value`. Everything becomes a string, verbatim -- this is structure, not
    judgement, and "Blue" and "blue" are two values until a human says otherwise.
    """
    out: list[tuple[str, str]] = []
    attrs = meta.get("attributes")
    if attrs is None:
        attrs = meta.get("traits")
    if isinstance(attrs, dict):
        attrs = [{"trait_type": k, "value": v} for k, v in attrs.items()]
    if not isinstance(attrs, list):
        return out
    for a in attrs:
        if not isinstance(a, dict):
            continue
        t = a.get("trait_type") or a.get("type") or a.get("name")
        v = a.get("value")
        if t is None or v is None:
            continue
        out.append((str(t).strip(), str(v).strip()))
    return out


class TraitsJob:
    def __init__(self, conn: sqlite3.Connection, rest: RestClient, opstore, *, slug: str,
                 ipfs_gateway: str = "https://ipfs.io/ipfs/", concurrency: int = 6,
                 timeout: float = 20.0, opensea_fallback_budget: int = 50, max_pages: int = 500) -> None:
        self.conn = conn
        self.rest = rest
        self.opstore = opstore
        self.slug = slug
        self.gateway = ipfs_gateway.rstrip("/") + "/"
        self.concurrency = concurrency
        self.timeout = timeout
        self.fallback_budget = opensea_fallback_budget
        self.max_pages = max_pages           # 500 x 200 = 100k tokens; larger collections raise it in config
        self.conn.executescript(SCHEMA)
        self._ctx = ssl_context()

    # -- 1. token list, through the governor -----------------------------------
    async def list_tokens(self) -> dict[str, Any]:
        row = next((r for r in self.opstore.onboarding_status() if r["collection_slug"] == self.slug), None)
        cursor = row["last_cursor"] if row and row.get("state") == "tokens" else None
        if row and row.get("state") in ("traits", "complete"):
            return {"pages": 0, "tokens": 0, "skipped": "token list already complete"}
        self.opstore.upsert_onboarding(self.slug, state="tokens")
        spent0 = int(row["requests_spent"]) if row else 0
        made0 = self.rest.requests_made
        spent = spent0
        pages = tokens = traits_from_list = 0
        skipped_same = skipped_conflict = 0
        disagreements: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["next"] = cursor
            status, body = await self.rest.get(f"/collection/{self.slug}/nfts", params, priority=Priority.BACKFILL)
            spent = spent0 + (self.rest.requests_made - made0)     # attempts, retries included
            if status != 200:
                self.opstore.upsert_onboarding(self.slug, state="failed", error=f"{status}: {json.dumps(body)[:200]}",
                                               requests_spent=spent)
                raise RuntimeError(f"token list failed: {status} {json.dumps(body)[:200]}")
            batch = body.get("nfts") or []
            with self.conn:
                self.conn.executemany(
                    """INSERT INTO tokens (collection, token_id, contract, name, image_url, metadata_url, listed_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(collection, token_id) DO UPDATE SET
                         name=excluded.name, image_url=excluded.image_url, metadata_url=excluded.metadata_url""",
                    [(self.slug, str(n.get("identifier")), n.get("contract"), n.get("name"),
                      n.get("display_image_url") or n.get("image_url"), n.get("metadata_url"), _now())
                     for n in batch if n.get("identifier") is not None])
            # BUG-20260909-054: the list response carries `traits` (verified for
            # Argonauts). Store them now, with the observation time, so the
            # per-token paths below only run for items whose entry had none.
            # A token that already has traits from another source is left alone.
            for n in batch:
                if n.get("identifier") is None:
                    continue
                pairs = parse_attributes({"traits": n.get("traits")}) if isinstance(n.get("traits"), list) else []
                if not pairs:
                    continue
                tid = str(n["identifier"])
                if self._store(tid, pairs, "opensea_nft_list", None, only_if_missing=True):
                    traits_from_list += 1
                    continue
                # BUG-20260909-055: the write was refused because this token
                # already has traits from another source. That is either
                # agreement (nothing to say) or a DISAGREEMENT between OpenSea's
                # list response and whatever we recorded first -- which used to
                # be dropped here uncounted, and is evidence about one of the two
                # sources. It is reported exactly as an import disagreement is.
                mine = self._stored_traits(tid)
                theirs: dict[str, set[str]] = {}
                for t, v in pairs:
                    theirs.setdefault(t, set()).add(v)
                if mine == theirs:
                    skipped_same += 1
                    continue
                skipped_conflict += 1
                for t in sorted(set(mine) | set(theirs)):
                    if mine.get(t) != theirs.get(t):
                        disagreements.append({"token_id": tid, "trait_type": t,
                                              "stored": sorted(mine.get(t, ())),
                                              "list": sorted(theirs.get(t, ()))})
            pages += 1
            tokens += len(batch)
            cursor = body.get("next")
            total = self.conn.execute("SELECT COUNT(*) FROM tokens WHERE collection=?", (self.slug,)).fetchone()[0]
            self.opstore.upsert_onboarding(self.slug, state="tokens", last_cursor=cursor,
                                           items_done=total, requests_spent=spent)
            log.info("token list: page %d, %d tokens so far (%d reads)", pages, total, spent)
            if not cursor or not batch:
                break
            if cursor in seen_cursors or pages >= self.max_pages:
                # A server handing back the same cursor forever would otherwise
                # spend the whole budget one governed read at a time (tech-lead F14).
                self.opstore.upsert_onboarding(self.slug, state="failed", requests_spent=spent,
                                               error=f"token list stopped: repeated cursor or > {self.max_pages} pages")
                raise RuntimeError(f"token list stopped after {pages} pages: repeated cursor or page cap")
            seen_cursors.add(cursor)
        self.opstore.upsert_onboarding(self.slug, state="traits", last_cursor=None, items_total=total)
        return {"pages": pages, "tokens": tokens, "requests_spent": spent,
                "traits_from_list": traits_from_list,           # WRITES, not entries carrying traits
                "traits_skipped_same": skipped_same,
                "traits_skipped_conflict": skipped_conflict,
                "traits_disagreements": disagreements}

    # -- 2. metadata, direct -----------------------------------------------------
    def _resolve_url(self, url: str) -> str:
        if url.startswith("ipfs://"):
            return self.gateway + url[len("ipfs://"):].lstrip("/").replace("ipfs/", "", 1)
        if url.startswith("ar://"):
            return "https://arweave.net/" + url[len("ar://"):]
        return url

    def _fetch_json(self, url: str) -> dict[str, Any]:
        target = self._resolve_url(url)
        if is_opensea_host(target):
            # Never fetched here. The caller routes it through the governed fallback.
            raise PermissionError(f"metadata_url on an OpenSea host is a metered call: {target[:80]}")
        req = urllib.request.Request(target, headers={"User-Agent": "navanax/0.1", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
            data = r.read(MAX_METADATA_BYTES + 1)
        if len(data) > MAX_METADATA_BYTES:
            raise ValueError(f"metadata larger than {MAX_METADATA_BYTES} bytes")
        return json.loads(data)

    def _stored_traits(self, token_id: str) -> dict[str, set[str]]:
        """{trait_type: {value, ...}} as the store currently holds it, for diffing."""
        out: dict[str, set[str]] = {}
        for t, v in self.conn.execute(
                "SELECT trait_type, value FROM traits WHERE collection=? AND token_id=?",
                (self.slug, token_id)):
            out.setdefault(t, set()).add(v)
        return out

    def _store(self, token_id: str, pairs: list[tuple[str, str]], source: str, error: str | None,
               *, only_if_missing: bool = False, observed_at: str | None = None) -> bool:
        """Write one token's traits; return whether traits were actually WRITTEN.

        `only_if_missing` leaves a token that already has traits untouched (two
        sources never silently overwrite each other); `observed_at` is the time
        the traits were OBSERVED by the source, which defaults to now for a live
        fetch.

        The return value is not decoration. BUG-20260909-055: the list pass
        counted every entry that CARRIED traits, including the ones this method
        refused to write, so the number reported to the Operator was the
        response's size rather than the store's gain. A caller that wants to
        count writes has to be told which calls wrote.
        """
        with self.conn:
            if only_if_missing and self.conn.execute(
                    "SELECT 1 FROM tokens WHERE collection=? AND token_id=? AND traits_at IS NOT NULL",
                    (self.slug, token_id)).fetchone():
                return False
            if pairs:
                self.conn.execute("DELETE FROM traits WHERE collection=? AND token_id=?", (self.slug, token_id))
                self.conn.executemany("INSERT OR REPLACE INTO traits (collection, token_id, trait_type, value) VALUES (?,?,?,?)",
                                      [(self.slug, token_id, t, v) for t, v in pairs])
            # traits_at is set only on success: a token that failed keeps
            # traits_at NULL and is retried on the next run (tech-lead F12).
            self.conn.execute("UPDATE tokens SET traits_at=?, traits_source=?, traits_error=? WHERE collection=? AND token_id=?",
                              ((observed_at or _now()) if pairs else None, source, error, self.slug, token_id))
        return bool(pairs)

    async def fetch_traits(self, *, limit: int | None = None) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT token_id, metadata_url FROM tokens WHERE collection=? AND traits_at IS NULL ORDER BY CAST(token_id AS INTEGER)",
            (self.slug,)).fetchall()
        if limit:
            rows = rows[:limit]
        loop = asyncio.get_running_loop()
        sem = asyncio.Semaphore(self.concurrency)
        done = ok = failed = 0
        fallback_left = self.fallback_budget
        row = next((r for r in self.opstore.onboarding_status() if r["collection_slug"] == self.slug), None)
        spent0 = int(row["requests_spent"]) if row else 0
        made0 = self.rest.requests_made

        async def one(token_id: str, url: str | None) -> None:
            nonlocal done, ok, failed, fallback_left
            async with sem:
                pairs: list[tuple[str, str]] = []
                err: str | None = None
                source = "none"
                if url:
                    try:
                        meta = await loop.run_in_executor(None, self._fetch_json, url)
                        pairs = parse_attributes(meta)
                        source = "metadata_url"
                    except Exception as exc:  # noqa: BLE001 - one bad URL is a row, not a crash
                        err = f"{type(exc).__name__}: {str(exc)[:120]}"
                if not pairs and fallback_left > 0:
                    # Budgeted fallback through the governor: the OpenSea per-token
                    # endpoint carries traits. Capped so a broken metadata host
                    # cannot quietly spend the whole hour's budget.
                    contract = self.conn.execute("SELECT contract FROM tokens WHERE collection=? AND token_id=?",
                                                 (self.slug, token_id)).fetchone()[0]
                    if contract:
                        # The slot is spent only where a read is actually made.
                        # It used to be decremented above this check, so a token
                        # with no known contract -- exactly what an imported cache
                        # leaves behind for a slug absent from KNOWN_CONTRACTS --
                        # burned a budgeted slot on a request that never happened,
                        # and a token that could have used it went without.
                        fallback_left -= 1
                        status, body = await self.rest.get(f"/chain/ethereum/contract/{contract}/nfts/{token_id}",
                                                           priority=Priority.MAINTENANCE)
                        if status == 200:
                            pairs = [(str(t.get("trait_type")), str(t.get("value")))
                                     for t in (body.get("nft") or {}).get("traits") or []
                                     if t.get("trait_type") is not None and t.get("value") is not None]
                            source = "opensea_nft"
                            err = None
                        else:
                            err = (err or "") + f" | opensea {status}"
                self._store(token_id, pairs, source, err if not pairs else None)
                done += 1
                if pairs:
                    ok += 1
                else:
                    failed += 1
                if done % 100 == 0 or done == len(rows):
                    total = self.conn.execute(
                        "SELECT COUNT(*) FROM tokens WHERE collection=? AND traits_at IS NOT NULL AND traits_error IS NULL",
                        (self.slug,)).fetchone()[0]
                    self.opstore.upsert_onboarding(self.slug, state="traits", items_done=total,
                                                   requests_spent=spent0 + (self.rest.requests_made - made0))
                    log.info("traits: %d/%d this run (%d ok, %d failed); %d tokens have traits", done, len(rows), ok, failed, total)

        await asyncio.gather(*(one(t, u) for t, u in rows))
        remaining = self.conn.execute("SELECT COUNT(*) FROM tokens WHERE collection=? AND traits_at IS NULL", (self.slug,)).fetchone()[0]
        if remaining == 0:
            self.opstore.upsert_onboarding(self.slug, state="complete")
        return {"attempted": done, "ok": ok, "failed": failed, "remaining": remaining}

    # -- summary for the UI ------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        return trait_coverage(self.conn, self.slug)


def trait_coverage(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """"N of M tokens now have traits", with the trait-type census. Exact, not sampled.

    `TraitsJob.summary()` is this function; it is a module function so that
    anything holding only a connection -- the import path, a one-off audit --
    can print the same coverage without constructing a REST client it will
    never use.
    """
    n_tokens = conn.execute("SELECT COUNT(*) FROM tokens WHERE collection=?", (slug,)).fetchone()[0]
    n_traited = conn.execute("SELECT COUNT(DISTINCT token_id) FROM traits WHERE collection=?", (slug,)).fetchone()[0]
    n_failed = conn.execute("SELECT COUNT(*) FROM tokens WHERE collection=? AND traits_error IS NOT NULL",
                            (slug,)).fetchone()[0]
    types = conn.execute("SELECT trait_type, COUNT(DISTINCT value) FROM traits WHERE collection=? "
                         "GROUP BY trait_type ORDER BY trait_type", (slug,)).fetchall()
    return {"collection": slug, "tokens": n_tokens, "with_traits": n_traited, "failed": n_failed,
            "trait_types": [{"trait_type": t, "distinct_values": n} for t, n in types]}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def criteria_trait_coverage(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """Every distinct string criterion in `order_criteria` that matches NO trait value.

    The same question `MetricEngine.criteria_coverage()` answers for the
    dashboard, asked here so `import-traits` can answer it the moment the traits
    land -- which is when a casing or spelling mismatch is cheapest to see and
    when the Operator is actually looking. A criterion with no matching value can
    never match a token: the filter returns nothing, and nothing looks exactly
    like a quiet market.

    `order_criteria` is written by the normalizer. A store that has only ever had
    traits imported into it does not have that table, and the honest answer then
    is "not checked", never "0 missing".
    """
    if not (_table_exists(conn, "order_criteria") and _table_exists(conn, "events")):
        return {"collection": slug, "available": False,
                "note": "no order_criteria/events table in this store yet -- run the normalizer, "
                        "then the dashboard reports this continuously (docs/08 §4a)"}
    pairs = conn.execute(
        """SELECT DISTINCT c.trait_type, c.value FROM order_criteria c
           JOIN events e ON e.run = c.run AND e.seq = c.seq
           WHERE e.collection = ? AND c.kind = 'string'""", (slug,)).fetchall()
    missing = [(t, v) for t, v in pairs if not conn.execute(
        "SELECT EXISTS(SELECT 1 FROM traits WHERE collection=? AND trait_type=? AND value=?)",
        (slug, t, v)).fetchone()[0]]
    return {"collection": slug, "available": True, "distinct_criteria": len(pairs),
            "matched": len(pairs) - len(missing), "missing": len(missing),
            "missing_pairs": [{"trait_type": t, "value": v} for t, v in missing[:50]],
            "alert": bool(missing)}


def case_near_misses(conn: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """Trait types and values that differ ONLY by case or surrounding whitespace.

    Values are stored verbatim -- "Blue" and "blue" are two values until a human
    says otherwise (docs/08 §4a). That rule is right, and it is exactly why this
    check has to exist: a filter on "Cloak" silently matches none of the tokens
    recorded under "cloak", and a silently empty filter reads as an illiquid
    market rather than as a bug. This reports the collisions; it never merges
    them. Merging is the Operator's call.
    """
    types: dict[str, set[str]] = {}
    for (t,) in conn.execute("SELECT DISTINCT trait_type FROM traits WHERE collection=?", (slug,)):
        types.setdefault(t.strip().casefold(), set()).add(t)
    values: dict[tuple[str, str], set[str]] = {}
    for t, v in conn.execute("SELECT DISTINCT trait_type, value FROM traits WHERE collection=?", (slug,)):
        values.setdefault((t.strip().casefold(), v.strip().casefold()), set()).add(v)
    type_hits = [{"casefolded": k, "variants": sorted(v)} for k, v in sorted(types.items()) if len(v) > 1]
    value_hits = [{"trait_type": k[0], "casefolded": k[1], "variants": sorted(v)}
                  for k, v in sorted(values.items()) if len(v) > 1]
    return {"collection": slug, "trait_type_near_misses": type_hits, "value_near_misses": value_hits,
            "alert": bool(type_hits or value_hits),
            "note": "reported, never merged -- two spellings are two values until the Operator says otherwise"}


def trait_values(conn: sqlite3.Connection, slug: str) -> dict[str, list[dict[str, Any]]]:
    """{trait_type: [{value, n}, ...]} -- the filter panel's contents."""
    out: dict[str, list[dict[str, Any]]] = {}
    for t, v, n in conn.execute(
            "SELECT trait_type, value, COUNT(*) FROM traits WHERE collection=? GROUP BY trait_type, value ORDER BY trait_type, 3 DESC, value",
            (slug,)):
        out.setdefault(t, []).append({"value": v, "n": n})
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Offline import of a trait cache pulled by another tool (zero REST reads)
# ---------------------------------------------------------------------------
# Contracts we know without a REST read. The watchlist carries no contract
# field; when it grows one, prefer it over this table.
KNOWN_CONTRACTS = {"argonauts": "0x387c41b0b2f1128de44db1bcf8baad085f26392c"}


SCALAR = (str, int, float, bool)


def cache_traits(entry: dict[str, Any]) -> tuple[list[tuple[str, str]], int]:
    """One Explorer cache entry -> ([(trait_type, value)], malformed_count).

    Only `traits` is read; rank, rarity_*, price, listing, deal... are that
    tool's ASSUMPTIONS and market snapshots, not structure, and never enter this
    store.

    A trait whose type or value is not a scalar -- a nested object, a list -- is
    skipped and COUNTED, never coerced. `str({'a': 1})` would write the Python
    repr `{'a': 1}` into `traits.value` as though a token really carried that
    trait: a plausible substitute for a value we could not read, which is the
    one thing this layer must never write (data-engineer, "fail loud"). A
    `traits` field that is neither a mapping nor a list is one malformed unit.
    """
    t = entry.get("traits")
    if t is None:
        return [], 0
    if not isinstance(t, (dict, list)):
        return [], 1
    items = list(t.items()) if isinstance(t, dict) else \
        [(a.get("trait_type") or a.get("type") or a.get("name"), a.get("value"))
         for a in t if isinstance(a, dict)]
    malformed = sum(1 for a in t if not isinstance(a, dict)) if isinstance(t, list) else 0
    pairs: list[tuple[str, str]] = []
    for tt, v in items:
        if tt is None or v is None:
            continue                      # absent, not malformed: nothing was claimed
        if not isinstance(tt, SCALAR) or not isinstance(v, SCALAR):
            malformed += 1
            continue
        pairs.append((str(tt).strip(), str(v).strip()))
    return pairs, malformed


def _as_map(pairs: list[tuple[str, str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for t, v in pairs:
        out.setdefault(t, set()).add(v)
    return out


def import_explorer_cache(conn: sqlite3.Connection, cache: dict[str, Any], *, slug: str,
                          generated_at: str, contract: str | None = None,
                          cache_file: str | None = None, cache_bytes: int | None = None,
                          cache_sha256: str | None = None) -> dict[str, Any]:
    """Load `tokens.json` from the Explorer tool into `tokens` / `traits`. Idempotent.

    `cache` is {key: {id, name, image, traits: {trait_type: value}, ...}};
    `generated_at` is the ISO-UTC time the cache was generated -- that is when
    the traits were OBSERVED, and it is what `traits_at` records. Claiming
    "now" would be a bitemporal lie (docs/05 TMP).

    The key and the entry's `id` are two different things and only the `id`
    identifies the token. Everything below is keyed on the RESOLVED id.

      * a token absent from `tokens` is inserted (listed_at = now: that is when
        THIS table first saw it; image from `image`; metadata_url NULL);
      * a token with no traits yet gets the cache's, source `explorer_cache`
        (or `explorer_cache:<sha256[:12]>` when the file's digest is given, so
        the row names the particular cache it came from);
      * a token that ALREADY has traits from any source is NOT overwritten. It
        is diffed against the cache per trait_type, and every disagreement is
        returned -- it is evidence about one of the two sources, and the
        Operator decides which. The caller exits non-zero on any disagreement;
      * TWO entries resolving to ONE id are collapsed. Identical traits are a
        duplicate and counted as one. DIFFERENT traits are a disagreement inside
        the cache itself, and NOTHING is written for that token -- not the
        traits, not the row. Before BUG-20260909-055 the second entry silently
        overwrote the first and `imported` counted both, so the cache's own
        internal contradiction became our record with no trace;
      * an entry that is not an object, or a trait whose value is not a scalar,
        is skipped and counted as `malformed`. Never coerced -- see `cache_traits`.

    Every count in the result is exact; nothing is sampled.
    """
    ensure_schema(conn)
    contract = contract or KNOWN_CONTRACTS.get(slug)
    now = _now()
    source = f"explorer_cache:{cache_sha256[:12]}" if cache_sha256 else "explorer_cache"
    table_ids = {r[0] for r in conn.execute("SELECT token_id FROM tokens WHERE collection=?", (slug,))}
    known_contracts = {r[0]: r[1] for r in conn.execute(
        "SELECT token_id, contract FROM tokens WHERE collection=? AND contract IS NOT NULL", (slug,))}
    traited = {r[0] for r in conn.execute(
        "SELECT token_id FROM tokens WHERE collection=? AND traits_at IS NOT NULL", (slug,))}
    stored: dict[str, dict[str, set[str]]] = {}
    for tid, tt, v in conn.execute("SELECT token_id, trait_type, value FROM traits WHERE collection=?", (slug,)):
        if tid in traited:
            stored.setdefault(tid, {}).setdefault(tt, set()).add(v)

    out: dict[str, Any] = {
        "collection": slug, "generated_at": generated_at, "source": source, "traits_source": source,
        "cache_sha256": cache_sha256, "cache_file": cache_file, "cache_bytes": cache_bytes,
        "cache_entries": len(cache),
        "tokens_inserted": 0, "imported": 0, "skipped_already_had": 0,
        "diffed_agree": 0, "diffed_disagree": 0, "disagreements": [],
        "duplicate_ids_same": 0, "duplicate_ids_conflicting": 0, "duplicate_id_disagreements": [],
        "malformed": 0, "malformed_examples": [],
        "contract_mismatches": 0, "contract_mismatch_examples": [],
        "in_cache_not_in_table": 0, "in_table_not_in_cache": 0,
        "cache_tokens_without_traits": 0,
    }

    # -- pass 1: resolve every entry to a token id and settle the duplicates ---
    # Nothing is written in this pass. A contradiction found here must stop the
    # write for that token, and that cannot be decided one row at a time.
    first_key: dict[str, str] = {}
    resolved: dict[str, dict[str, Any]] = {}
    resolved_pairs: dict[str, list[tuple[str, str]]] = {}
    conflicting: set[str] = set()
    for key, entry in cache.items():
        if not isinstance(entry, dict):
            out["malformed"] += 1
            out["malformed_examples"].append(
                {"key": str(key), "reason": f"entry is {type(entry).__name__}, not an object"})
            continue
        tid = str(entry["id"]) if entry.get("id") is not None else str(key)
        pairs, bad = cache_traits(entry)
        if bad:
            out["malformed"] += bad
            out["malformed_examples"].append(
                {"key": str(key), "token_id": tid,
                 "reason": f"{bad} trait(s) whose type or value is not a scalar -- skipped, never coerced"})
        if tid in resolved:
            if _as_map(pairs) == _as_map(resolved_pairs[tid]):
                out["duplicate_ids_same"] += 1
            else:
                conflicting.add(tid)
                out["duplicate_ids_conflicting"] += 1
                out["duplicate_id_disagreements"].append({
                    "token_id": tid, "keys": [first_key[tid], str(key)],
                    "first": sorted((t, v) for t, v in resolved_pairs[tid]),
                    "second": sorted((t, v) for t, v in pairs)})
            continue
        first_key[tid] = str(key)
        resolved[tid] = entry
        resolved_pairs[tid] = pairs

    # Reconciliation is between the ids the cache CLAIMS and the ids the table
    # HOLDS -- built from the resolved ids, before any insert moves the goalposts.
    cache_ids = set(resolved)
    out["in_cache_not_in_table"] = len(cache_ids - table_ids)
    out["in_table_not_in_cache"] = len(table_ids - cache_ids)

    # -- pass 2: write ---------------------------------------------------------
    with conn:
        for tid, entry in resolved.items():
            if tid in conflicting:
                continue                    # the cache contradicts itself here; write nothing
            pairs = resolved_pairs[tid]
            if contract and known_contracts.get(tid) and known_contracts[tid] != contract:
                # COALESCE below keeps what is stored, which is right -- but it
                # also HIDES this. One of the two contracts is wrong, and a wrong
                # contract sends every fallback read to the wrong collection.
                out["contract_mismatches"] += 1
                if len(out["contract_mismatch_examples"]) < 50:
                    out["contract_mismatch_examples"].append(
                        {"token_id": tid, "stored": known_contracts[tid], "importing": contract})
            if tid not in table_ids:
                conn.execute(
                    """INSERT INTO tokens (collection, token_id, contract, name, image_url, metadata_url, listed_at)
                       VALUES (?,?,?,?,?,NULL,?)""",
                    (slug, tid, contract, entry.get("name"), entry.get("image"), now))
                table_ids.add(tid)
                out["tokens_inserted"] += 1
            else:
                # Fill blanks only. A value another source recorded is never replaced.
                conn.execute(
                    """UPDATE tokens SET contract=COALESCE(contract, ?), name=COALESCE(name, ?),
                                         image_url=COALESCE(image_url, ?)
                       WHERE collection=? AND token_id=?""",
                    (contract, entry.get("name"), entry.get("image"), slug, tid))
            if not pairs:
                out["cache_tokens_without_traits"] += 1
                continue
            if tid in traited:
                out["skipped_already_had"] += 1
                mine = stored.get(tid, {})
                theirs = _as_map(pairs)
                for t in sorted(set(mine) | set(theirs)):
                    if mine.get(t) == theirs.get(t):
                        out["diffed_agree"] += 1
                    else:
                        out["diffed_disagree"] += 1
                        out["disagreements"].append({
                            "token_id": tid, "trait_type": t,
                            "stored": sorted(mine.get(t, ())), "cache": sorted(theirs.get(t, ()))})
                continue
            conn.execute("DELETE FROM traits WHERE collection=? AND token_id=?", (slug, tid))
            conn.executemany("INSERT OR REPLACE INTO traits (collection, token_id, trait_type, value) VALUES (?,?,?,?)",
                             [(slug, tid, t, v) for t, v in pairs])
            conn.execute("UPDATE tokens SET traits_at=?, traits_source=?, traits_error=NULL "
                         "WHERE collection=? AND token_id=?", (generated_at, source, slug, tid))
            out["imported"] += 1
        conn.execute(
            """INSERT INTO trait_imports (collection, imported_at, generated_at, cache_file,
                                          cache_bytes, cache_sha256, traits_source, counts_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (slug, now, generated_at, cache_file, cache_bytes, cache_sha256, source,
             json.dumps({k: v for k, v in out.items() if not isinstance(v, list)}, sort_keys=True)))
    return out


def open_store(path: str | Path) -> sqlite3.Connection:
    """The traits job's connection: `tokens` and `traits` only, and NO writer lock.

    BUG-20260910-067 put an exclusive flock on `<store>.lock` around the FOLD --
    the landing-zone-to-events writer. This job is not that: it writes `tokens`
    and `traits`, which come from metered REST reads, never from the landing
    zone, and it folds no events. Taking the fold lock here would mean the daily
    traits job and the dashboard could not run at the same time, which is a
    worse outcome than the contention it would prevent.

    What it needs instead is patience. An explicit `busy_timeout` (SQLite's
    "wait this long for the other writer to finish" knob) means a fold holding
    the write lock for the length of one batch insert makes this job WAIT rather
    than fail with "database is locked" -- a spurious failure that reads exactly
    like a real one.
    """
    conn = sqlite3.connect(str(path), check_same_thread=False,
                           timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    ensure_schema(conn)
    return conn
