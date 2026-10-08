# ADR-0005 — One bug ledger for the repository, with an `area` field

**Status:** Accepted · **Date:** 2026-10-03 · **Deciders:** orchestrator session
**Supersedes:** — · **Superseded by:** —

## Context

`docs/logs/bugs.yaml` is the repository's bug ledger, with a CI gate
(`tools/buglog.py --check`) that enforces: every id appears in `BUGS.md` and vice versa;
every `locations` file exists; every fixed bug names a regression test that exists;
every `BUG-` id referenced anywhere in the repository exists in the ledger. A second
ledger for health would mean a second gate or none.

The V1 artifact carries its own audit identifiers (`BUG-0002`, `BUG-0049`…`BUG-0054`,
audit items `F-01`…`F-12`, decisions `D-1`…`D-3`, tracker `T-103`, `T-120`). Their
format (`BUG-NNNN`) does not match the ledger's (`BUG-YYYYMMDD-NNN`), so the ledger gate
does not see them.

## Decision

- Health defects go in the shared ledger with `area: health`, the health error classes of `docs/health/05 §3`, and `reader_impact` in place of `data_impact`'s meaning.
- The V1 audit identifiers are **not** ledger ids. They are catalogued, with current status, in `docs/health/logs/V1_AUDIT_ITEMS.md`, and referred to in prose as "V1 audit item F-04" or "V1 bug BUG-0052".
- `tools/buglog.py` is extended only if a health-specific check is needed; the first candidate is "every `ETH`-class bug has a safety-reviewer sign-off before `fixed`".

## Consequences

- One gate, one spreadsheet, one place to look.
- The V1 items must be migrated into the ledger only when they are worked on here; until then the catalogue is the record.

## How to undo

Split the ledger: move `area: health` entries to `docs/health/logs/bugs.yaml`, add a
second `--check` invocation to gates and CI, and supersede this ADR.
