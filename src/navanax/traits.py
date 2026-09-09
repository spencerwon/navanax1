"""Collection onboarding, part one: every token and its traits (REQ-F-01, REQ-F-07a).

Why this exists: the stream says what HAPPENED to token #7531. It never says
what #7531 IS. Trait filters, trait-tier pricing, the hedonic model -- all of
Phase 1's analysis -- need a table that says "#7531: Background=Blue, ...".

Where traits come from, and what it costs (docs/07 §3.1, measured budget 120/hr):

  1. `GET /collection/{slug}/nfts?limit=200`      ~47 governed reads for 9,212
     items. Gives each token's name, image and `metadata_url`. NOT its traits.
  2. Each token's `metadata_url`, fetched DIRECTLY from wherever it is hosted
     (IPFS gateway, the project's own server). Not OpenSea, not metered by the
     governor. This is where the traits are (`attributes: [{trait_type, value}]`).
  3. Fallback, budget-limited: OpenSea's per-token endpoint, for tokens whose
     metadata cannot be fetched. Never used for the whole collection -- that
     would be 9,212 reads, 77 hours of budget.

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
    traits_source TEXT,                   -- metadata_url | opensea_nft | none
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
    async def list_tokens(self) -> dict[str, int]:
        row = next((r for r in self.opstore.onboarding_status() if r["collection_slug"] == self.slug), None)
        cursor = row["last_cursor"] if row and row.get("state") == "tokens" else None
        if row and row.get("state") in ("traits", "complete"):
            return {"pages": 0, "tokens": 0, "skipped": "token list already complete"}
        self.opstore.upsert_onboarding(self.slug, state="tokens")
        spent0 = int(row["requests_spent"]) if row else 0
        made0 = self.rest.requests_made
        spent = spent0
        pages = tokens = 0
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
        return {"pages": pages, "tokens": tokens, "requests_spent": spent}

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

    def _store(self, token_id: str, pairs: list[tuple[str, str]], source: str, error: str | None) -> None:
        with self.conn:
            if pairs:
                self.conn.execute("DELETE FROM traits WHERE collection=? AND token_id=?", (self.slug, token_id))
                self.conn.executemany("INSERT OR REPLACE INTO traits (collection, token_id, trait_type, value) VALUES (?,?,?,?)",
                                      [(self.slug, token_id, t, v) for t, v in pairs])
            # traits_at is set only on success: a token that failed keeps
            # traits_at NULL and is retried on the next run (tech-lead F12).
            self.conn.execute("UPDATE tokens SET traits_at=?, traits_source=?, traits_error=? WHERE collection=? AND token_id=?",
                              (_now() if pairs else None, source, error, self.slug, token_id))

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
                    fallback_left -= 1
                    contract = self.conn.execute("SELECT contract FROM tokens WHERE collection=? AND token_id=?",
                                                 (self.slug, token_id)).fetchone()[0]
                    if contract:
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
        c = self.conn
        n_tokens = c.execute("SELECT COUNT(*) FROM tokens WHERE collection=?", (self.slug,)).fetchone()[0]
        n_traited = c.execute("SELECT COUNT(DISTINCT token_id) FROM traits WHERE collection=?", (self.slug,)).fetchone()[0]
        n_failed = c.execute("SELECT COUNT(*) FROM tokens WHERE collection=? AND traits_error IS NOT NULL", (self.slug,)).fetchone()[0]
        types = c.execute("SELECT trait_type, COUNT(DISTINCT value) FROM traits WHERE collection=? GROUP BY trait_type ORDER BY trait_type",
                          (self.slug,)).fetchall()
        return {"collection": self.slug, "tokens": n_tokens, "with_traits": n_traited, "failed": n_failed,
                "trait_types": [{"trait_type": t, "distinct_values": n} for t, n in types]}


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


def open_store(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)
    return conn
