# ADR-0004 — Origin context: the vendored artifact, not the shared chat

**Status:** Accepted · **Date:** 2026-10-03 · **Deciders:** orchestrator session; Operator informed
**Supersedes:** — · **Superseded by:** —

## Context

The Operator pointed this build at a shared Claude conversation
(`https://claude.ai/share/2af558dd-efed-4ec8-bb8f-349c7a7417e0`) as the narrower-scope
beginning of the project. From the cloud container the page could not be read:

- the page is client-rendered and its script bundle is served from `assets-proxy.anthropic.com`, which the environment's network policy denies (`CONNECT` 403 at the proxy);
- the data endpoint (`/api/chat_snapshots/<id>`) sits behind a Cloudflare JavaScript challenge whose script host `challenges.cloudflare.com` is also denied;
- a headless Chromium could reach the same-origin challenge script but does not trust the proxy's certificate, and installing that certificate into the browser trust store was blocked by the session's safety classifier;
- no browser or computer-use tool from the Operator's devices was exposed to the session, so the Operator's own signed-in browser could not be used.

The Operator's artifact gallery contained **Metabolic Map** (published 2026-10-01), an
educational water/sodium/kidney model with a 3D viewer, Monte Carlo bands, a graded
knowledge base and a verification log — evidently the narrower-scope beginning.

## Decision

Use the Metabolic Map artifact as the origin context. Vendor it verbatim
(`reference/metabolic-map-v1/`, with its URL and version in its README) and treat its
files — model, parameters, scenarios, expectations, knowledge base, verification log,
audit items — as the authoritative statement of what V1 is.

If the shared chat contains decisions that the artifact does not (scope, priorities,
constraints), the Operator supplies them by pasting the text or allowing the two hosts
above in the environment's network settings, and this ADR is superseded by one that
records what changed.

## Consequences

- Everything built here traces to files the Operator published, not to a paraphrase of a conversation.
- Any intent that lived only in the chat is, for now, unknown; `docs/health/00 §11` lists the open questions this leaves.

## How to undo

Supersede with an ADR that records the chat's content and what it changes.
