"""The one REST client. Every OpenSea REST call in the system goes through here,
and here goes through the governor (REQ-D-01..04).

Stdlib only (urllib), run in a worker thread so the governor's async gate can
sequence callers. Response headers are fed back to the governor every time --
`observe_response` is not optional; without it the local budget model drifts
from the server's, and a drifted model is how a 429 storm starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .governor import Priority, RestGovernor
from .tls import ssl_context

log = logging.getLogger("navanax.rest")
BASE = "https://api.opensea.io/api/v2"


class RestClient:
    def __init__(self, api_key: str, governor: RestGovernor, *, base: str = BASE,
                 timeout: float = 30.0, run_id: str | None = None, ledger=None) -> None:
        self.api_key = api_key
        self.gov = governor
        self.base = base
        self.timeout = timeout
        self.run_id = run_id
        self.ledger = ledger          # OperationalStore.log_rest, when present
        self._ctx = ssl_context()

    def _do(self, path: str, params: dict[str, Any] | None) -> tuple[int, dict[str, str], Any]:
        url = self.base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, headers={"x-api-key": self.api_key, "Accept": "application/json",
                                                   "User-Agent": "navanax/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as r:
                body = r.read()
                return r.status, {k.lower(): v for k, v in r.headers.items()}, json.loads(body or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"raw": raw[:300].decode(errors="replace")}
            return e.code, {k.lower(): v for k, v in e.headers.items()}, parsed

    async def get(self, path: str, params: dict[str, Any] | None = None, *,
                  priority: Priority = Priority.BACKFILL, retries: int = 3) -> tuple[int, Any]:
        """One governed GET. Retries 429/5xx through the governor's backoff."""
        loop = asyncio.get_running_loop()
        for attempt in range(retries + 1):
            async with self.gov.slot(priority):
                status, headers, body = await loop.run_in_executor(None, self._do, path, params)
                self.gov.observe_response(status, headers)
            if self.ledger is not None:
                try:
                    self.ledger(priority.name, path, status,
                                remaining=_int(headers.get("x-ratelimit-remaining")), run_id=self.run_id)
                except Exception:  # noqa: BLE001 - bookkeeping must never break a call
                    log.debug("rest ledger write failed", exc_info=True)
            if status == 429 or 500 <= status < 600:
                if attempt < retries:
                    log.warning("REST %s -> %s; the governor will back off before retry %d", path, status, attempt + 1)
                    continue
            return status, body
        return status, body  # pragma: no cover


def _int(x: Any) -> int | None:
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None
