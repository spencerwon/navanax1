# ADR-0003 — Package layout, the module registry, and the two-PR enable rule

**Status:** Accepted · **Date:** 2026-10-03 · **Deciders:** orchestrator session; Operator review pending
**Supersedes:** — · **Superseded by:** —

## Context

The Operator asked that the system be easy to undo and easy to add onto. Undo is only
cheap when the unit of change is explicit, its edges are recorded, and turning a thing
on is separable from adding it.

## Decision

1. **Layout.** One package, `src/health/`, with sub-packages by layer: `engine/`
   (solver, model, scenarios, mc, params, validate), `kb/` (data as package data, check,
   report), `cli.py`, `errors.py`. Knowledge-base JSON lives inside the package
   (`src/health/kb/data/`) so `pip install -e .` ships it and the checker never guesses a
   path. The V1 origin lives outside the package, read-only, at `reference/metabolic-map-v1/`.
2. **Registry.** `config/health/modules.yaml` is the source of truth for modules: id,
   enabled, owner, phase, paths, depends_on, tests, adr, removal recipe.
   `docs/health/07_MODULE_REGISTRY.md` renders it; `health.cli status` prints every
   module and its state so a disabled module is visible.
3. **Two-PR enable.** A module lands with `enabled: false` in its own pull request
   (branch `module/<id>`); a second one-line pull request (`enable/<id>`) turns it on.
   `git revert` of either merge is a complete, independent undo.
4. **Removal is rehearsed.** Before a module's pull request is opened, its removal
   recipe is run on a scratch branch and the gates confirmed green; the result is stated
   in the pull request. A recipe that needs an extra step is a tech-lead blocker.

## Consequences

- Two pull requests per module instead of one. Accepted: the second is one line and buys independent revertibility.
- A flag that nothing reads is a `CFG` defect; `status` reading the registry is the minimum consumer.
- Phase 0 modules shipped in a single pull request (this one) because they are the foundation the rule depends on; the registry already lists them with recipes, and the rule applies from Phase 1.

## How to undo

Supersede this ADR with one that chooses a different unit of change; the registry file
and the `07` page are deleted or regenerated accordingly.
