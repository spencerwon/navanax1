# ADR-0006 — Supersedes ADR-0004: the origin chat is recovered; what it adds

**Status:** Accepted · **Date:** 2026-10-03 · **Deciders:** Spencer (pasted the chat), orchestrator session
**Supersedes:** ADR-0004 · **Superseded by:** —

## Context

ADR-0004 recorded that the shared conversation could not be read from the container and
adopted the Metabolic Map artifact as the origin. The Operator then pasted the
conversation's content into this session. It confirms the artifact is the V1 of the same
project and adds what the artifact alone could not say.

## What the chat adds (recorded verbatim where it is the Operator's intent)

**The vision, in the Operator's words.** "I want this to be a 3D intractable model.
Goals are we can visualize at various scale a full technical repository of metabolic and
molecular biology effects … an accurate and digestable model of the human body — better
than any one system that has been created so far. We want to educate, test and have our
own accurate virtual lab. What doe drinking water do to blood electrolyte levels, how
much salt does it take to strain the kidney, how can we model accute affects generally.
How can we see chronic risks grow in the body in real time. Let's create an educational
resource for the ages here that could lead to research insights and disease cure as well
as better trained doctors, nurses, and scientists … heavily relying upon published
papers and showing all uncertainty. Extreme data rigor, efficient system design, use any
resource you need this new laptop of mine can process and store. Test its very limits."

**Operator decisions already taken in that session.** Code lands in
`/Users/speence/Desktop/The Human Body` on the Mac; the designer decides look and feel;
unverified assumptions (grade E) are shown as a **loud red pill plus a banner on
affected charts**; D-2 (slow blood-pressure state) and D-3 (GFR two ways, named
reference person) applied; the Operator "conceptualizes data and designs visual-heavy"
and asked to be shown choices visually.

**Prior state.** V1.0.1 shipped with audit verdict "SHIP WITH NOTES": a git repo with
one commit on `main` in `The Human Body` on the Mac (not yet published to GitHub; a
second local repo "Human Body Model" is empty); tests 52 pass, 1 todo on the Mac;
launchers `tools/Start.command`, `Stop`, `Status`, `Run_Tests`, `Setup`,
`Stress_Test`; documents `docs/SPEC_V1.md`, `docs/AUDIT_V1.md`, a 42-row bug log
auto-fed by the test runner; a Project plan, Tracker and Agents charter as project
documents. Five wrong-from-memory DOIs were caught and replaced during verification;
zero retractions.

**Decisions still open from that session.** D-1 strain colours (A/B/C, shown in
`app/design-options.html`); D-4 bladder display; D-6 repository visibility and licence;
D-7 dark-always vs follow-system; publishing the repository so future work goes
through pull requests. The intended GitHub name was `spencerwon/metabolic-map`.

## Decision

- Keep the vendored artifact as the authoritative *code* origin (it is the published build of that work); treat the pasted chat as the authoritative *intent* origin.
- Widen `docs/health/00_REQUIREMENTS.md`: the 3D multi-scale viewer and the virtual lab are first-class product surfaces, not a later phase; users include learners, clinicians in training and researchers; grade-E loudness is a requirement (HREQ-S-06); compute on the Operator's machine is a requirement with a stress test (HREQ-N-07).
- Carry the open decisions into `docs/health/logs/TRACKER.md` and `00 §11`.
- Record the project plan the Operator asked for as `docs/health/08_PROJECT_PLAN.md`, with the orchestrator accountable for delivery and the Operator the sole approver.
- The Mac repository's documents (`SPEC_V1.md`, `AUDIT_V1.md`, the bug log, the test suite) are not in this container. They are referenced, not reproduced; when the Operator publishes that repository, its history is attached here or migrated per `00 §11` Q1.

## Consequences

- `00 §1.2` now states the Operator's goals in the Operator's words; everything else in the document set traces to them.
- The roadmap re-orders: the viewer evolves every phase rather than arriving at the end.

## How to undo

Supersede with an ADR that records a different reading of the Operator's intent.
