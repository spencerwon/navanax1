# Agentic Hierarchy and Workflows

**Version:** 1.0
**Date:** 2026-09-09
**Companion to:** `00_REQUIREMENTS.md`, `01_METHODOLOGY.md`, `03_VALIDATION_AND_TESTING.md`

---

## 1. Purpose and Design Principles

This document defines the agents — both AI agents working on the codebase and automated software agents running inside the platform — their authority, their boundaries, and the workflows they execute.

### 1.1 Two distinct agent populations

These are frequently conflated and must not be:

| | **Development agents** | **Runtime agents** |
|---|---|---|
| What | Claude sessions building and maintaining the platform | Software processes running inside the platform |
| When | During development work | Continuously, in production |
| Authority | Write code, subject to review gates | Execute defined logic, no code authority |
| Failure mode | Bad code merged | Bad signal emitted, or data corrupted |
| Governed by | §3–§5 of this document | §6–§7 |

### 1.2 Design principles

1. **Least authority.** Each agent gets the narrowest scope that lets it do its job. An agent that can write to the historical store can corrupt an irreplaceable asset.
2. **The human decides.** No agent commits capital. No agent submits a transaction. The Operator is the sole decision authority for anything with money attached.
3. **Separation of proposer and validator.** The agent that builds a signal never validates it. This is the single most important structural rule here, because self-validation is how overfitting becomes invisible.
4. **Explicit context boundaries.** Each agent's inputs are enumerated. An agent that can see the test partition will leak it, whatever its instructions say.
5. **Everything is logged.** Every agent action produces an audit record sufficient to reconstruct why it happened.
6. **Fail loud.** An agent that cannot complete its task reports failure. It never returns a plausible guess.

---

## 2. Authority Model

Five levels. Every agent is assigned exactly one.

| Level | Name | Can | Cannot |
|---|---|---|---|
| **L0** | Observer | Read data, produce reports and analysis | Modify anything |
| **L1** | Analyst | L0 + write to scratch/derived stores, run experiments, and (where §3.15 grants it) write analytical code in a branch | Modify raw data, change config, merge, write non-analytical code |
| **L2** | Builder | L1 + write code, open PRs, modify config in a branch | Merge, write to production data, modify raw/landing zone |
| **L3** | Integrator | L2 + merge approved PRs, deploy, migrate schemas | Bypass validation gates, act without approval, alter historical records |
| **L4** | Operator (human) | Everything, including all capital decisions | — |

**Spencer approves every pull request.** No agent merges anything without his explicit approval on the PR itself — not the Orchestrator at L3, not after a Validator sign-off, not for a one-line documentation fix. The Validator's sign-off is a *precondition* for asking, never a substitute for asking. There is no auto-merge in this project, and any workflow or setting that would enable one is a defect to be removed.

**Immutable across all levels:** no agent at any level may delete or modify records in the landing zone or the bitemporal history. That data is append-only and is the platform's most valuable and least replaceable asset. Corrections are new records that supersede; they are never edits.

---

## 3. Development Agent Hierarchy

### 3.0 Team structure and escalation

The population is organised as **two teams with two different escalation
paths**, so that a fix is made at the lowest level that can make it and only
genuinely operator-level decisions reach Spencer.

```
                      ┌──────────────────────────────┐
                      │        OPERATOR  (L4)        │
                      │  Spencer — product owner,     │
                      │  lead strategist, SOLE merge  │
                      │  and capital authority        │
                      └───────────────┬──────────────┘
                                      │ direction · PR approval · capital
                      ┌───────────────┴──────────────┐
                      │   ORCHESTRATOR  (L2 / L3)    │  ← lead program manager
                      │  plans, decomposes, routes,   │    reports to Spencer
                      │  integrates, closes the loop   │
                      └───────┬──────────────┬───────┘
                              │              │
        ┌─────────────────────┘              └──────────────────────┐
        │  DEV TEAM                                     OPS TEAM    │
        │  escalates to TECH LEAD                       escalates   │
        │                                               to the      │
┌───────┴──────────┐                                    ORCHESTRATOR│
│   TECH LEAD (L3) │  gate before every PR                          │
│   may BLOCK,     │                                    ┌───────────┴────────┐
│   may not merge  │                                    │  PLATFORM ENGINEER │
└───────┬──────────┘                                    │  BUILD REPORTER    │
        │                                               │  DOCS STEWARD      │
┌───────┴───────┬──────────────┬─────────────┐          │  BUG TRIAGE        │
│ DATA ENGINEER │ QUANT RESEARCH│ UI DESIGNER │          │  RESEARCH          │
│     (L2)      │     (L1)      │    (L1)     │          └────────────────────┘
└───────────────┴──────────────┴─────────────┘

        VALIDATOR (L1) ── independent. Never reports to the agent whose work
                          it reviews. Reports to the Operator.
        QA AUDITOR (L2) ─ independent. Routes findings DOWN to the level that
                          can fix them; up to the Operator only when needed.
        DESIGN LEAD (L2) ─ sees every user-facing change RENDERED before the
                          Operator does; asks him design questions as their
                          own thread. Reports to the Orchestrator.
        MARKET ANALYST (L2) ─ on-chain due diligence on the ADDRESSES that
                          make the market; never people. Reports to the
                          Orchestrator; detectors validated by the Validator.
```

**The escalation rules, stated plainly:**

| Situation | Goes to | Not to |
|---|---|---|
| A defect in code, tests, schema, or docs describing code | **Tech Lead**, who assigns it | Spencer |
| Tech Lead and a specialist disagree on a fix | **Tech Lead decides.** It is the gate | Spencer |
| A fix would change scope, architecture, or a requirement | Tech Lead → **Orchestrator** | Spencer directly |
| Environment, CI, secrets, budget, repo settings, Slack, agent definitions | **Orchestrator** | Tech Lead |
| A requirement is ambiguous or contradicts another | Orchestrator → **Spencer** | guessing |
| A design choice — colour, units, hover, chart style, what a card shows | **Design Lead** asks Spencer, as a separate short thread with options | a developer's default |
| A trade-off between cost, scope, and risk | Orchestrator → **Spencer**, with a recommendation | deciding it internally |
| Anything touching capital, keys, or a transaction | **Spencer, always** | anyone |

**Cycle down before escalating up.** A finding that a lower level can close is
closed there. Escalation is for decisions, not for work.

**Close the loop.** Whenever something reaches Spencer, the Orchestrator states:
what happened, what it recommends, what it needs from him, and what is blocked
until he answers. He is never left holding an open question with no context.

### 3.1 Orchestrator (L2)

**Purpose.** Decomposes Operator intent into tasks, routes them, integrates results, maintains the plan.

**Responsibilities**
- Translate a request into a task graph with dependencies.
- Select the right specialist for each task and brief it with a scoped context.
- Detect when a task requires an Operator decision and escalate rather than guessing.
- Maintain project state: what is done, in flight, blocked.
- Enforce that work passes through the correct gates.

**Does not**
- Write substantial production code (delegates to Platform/Data Engineer).
- Make analytical judgments (delegates to Quant Research).
- Approve its own work through a gate.

**Escalates when:** a requirement is ambiguous; a decision has irreversible consequences; a task would consume significant REST budget; scope has grown beyond what was agreed; two specialists disagree on an interface.

### 3.2 Data Engineer (L2)

**Purpose.** Everything from the API to the normalized store.

**Owns:** stream consumer, REST governor and budget allocator, chain indexer, reconciliation, gap detection and backfill, landing zone, normalization, schema and migrations, data-quality checks.

**Critical constraints**
- Must implement the REST budget allocator before any component that consumes REST. The budget is a shared, exhaustible resource and an unregulated consumer will starve everything else.
- Must treat the landing zone as append-only.
- Must make ingestion idempotent; replay must not change state.
- Must never forward-fill a gap. Gaps are represented as gaps.

**Escalates when:** an API contract changes; rate limits are structurally insufficient for the requested universe; a schema change would require reprocessing history; reconciliation drift exceeds threshold persistently.

### 3.3 Quant Research (L1)

**Purpose.** Metric definitions, statistical models, signal hypotheses, backtest design.

**Owns:** implementation of metrics per methodology §3, hedonic and hierarchical models, wash detection, signal hypotheses and their registration, backtest strategy definitions, research reports.

**Hard constraints — this is where the money is lost if these fail**
- **May only access the training partition.** The validation and test partitions are not in this agent's context. This is enforced by the data-access layer, not by instruction (methodology §7.4).
- Must register a hypothesis with a stated mechanism *before* examining data for it (methodology §7.1).
- Must report uncertainty on every estimate (REQ-F-19).
- **May not validate its own signals.** Hands off to Validator at the `SPECIFIED` stage.
- May not retest a failed hypothesis with adjusted parameters. It goes to `RETIRED` with a post-mortem.

**Escalates when:** a signal looks unusually strong (this is a warning sign, not good news, and warrants scrutiny); sample sizes fall below §8.2 minima; a mechanism cannot be articulated; a result contradicts a previously validated finding.

### 3.4 Platform Engineer (L2)

**Purpose.** Everything the Operator touches, plus the runtime scaffolding.

**Owns:** dashboard and all views, charting, screener, interaction and linked selection, alert delivery, backtest and paper-trading UI, the runtime agent scaffolding, performance.

**Constraints**
- Must surface data provenance in every view (REQ-N-02, REQ-F-15) and staleness whenever a source is degraded (REQ-N-03). A chart that silently hides a gap is a correctness bug, not a cosmetic one.
- Must meet the performance budgets in REQ-N-05..08.
- Must never compute analytics in the presentation layer. Presentation renders what the analysis layer produced; a metric computed two different ways in two places is a guaranteed future inconsistency.

### 3.5 Validator (L1) — independent

**Purpose.** Adversarially attempt to break every claim before it reaches production or capital.

**Structural independence is the point.** The Validator reports to the Operator, never to the agent whose work it is checking, and is never the same session that produced the work. Its incentive is to find problems.

**Owns:** execution of the validation protocol in `03_VALIDATION_AND_TESTING.md`, leakage testing, robustness testing, backtest integrity audits, code review of analytical logic, data-quality audits, the multiple-testing register.

**Specific mandate — actively try to break things**
- Attempt to reproduce results independently.
- Search for look-ahead and leakage; plant deliberate leakage and confirm detection.
- Perturb parameters and confirm stability.
- Re-derive costs independently and check they were not understated.
- Verify the claimed sample size is the *effective* sample size.
- Check the hypothesis register for undisclosed multiple testing.

**Authority:** the Validator can **block** promotion of a signal or a merge. Only the Operator can override, and an override is recorded with a written rationale.

**Access:** the Validator may access the validation partition. Only the Validator may trigger a test-partition evaluation, once per signal (methodology §7.4).

### 3.6 Tech Lead (L3) — `tech-lead`, opus

**Purpose.** The gate between finished work and a pull request. Everything the
dev team produces passes through here before Spencer sees it, so that his review
is about *judgment* rather than about catching what should have been caught
upstream.

**Exists because of a specific failure.** Phase 0 reached a live smoke test with
66 passing assertions and a `status=ok` from the API, and an independent
Validator then found **five separate ways it silently lost irreplaceable data**.
Every one was in code that looked finished. The gap was not effort — it was that
nobody checked the whole against the requirements before it moved.

**Checks what neither the specialist nor the Validator does.** The Validator
hunts defects in the code as written. The Tech Lead asks whether the change makes
sense as part of the system:

- **Requirement traceability, and requirement *reality*.** A requirement marked
  satisfied must trace to running code, not to a docstring claiming it.
  *Grep for the mechanism; do not trust the comment.*
- **Claim-versus-code drift**, especially in what the *operator* reads —
  README, `setup.command`, error messages, CLI help.
- **Config that is actually parsed.** A config block nothing reads is worse than
  no config: it looks like a knob and turns nothing.
- **Dead code that implies capability.**
- **Test coverage of the claim, not of the function.**
- **Whether a Validator finding was fixed or dodged** by rewording a docstring,
  loosening a test, or adding a `noqa`.
- **The PR's own title, summary and description** — complete, accurate, honest
  about what was deferred, and explicit about what Spencer needs to decide. The
  description is the interface between the work and the only person with merge
  authority; overstating it is the same claim-versus-reality failure as a wrong
  docstring, aimed at the person who can least afford it.

**May:** block a PR. Assign work back to any dev-team specialist. Require a
regression test that demonstrably fails against the unfixed code.

**May not:** merge. Approve its own work. Weaken a Validator finding.

**Reports to** Spencer — never to the agent whose work it is reviewing.

### 3.7 Integrator (L3)

**Purpose.** The merge and deploy authority. Not a separate specialist — this is a **role the Orchestrator assumes**, and only after a Validator sign-off exists for the change in question.

**May:** merge an approved PR, deploy, run a schema migration, cut over a shadow-run.

**May not:** merge without the required Validator sign-off; merge its own analytical work; bypass a gate; alter historical records. An Operator override of a Validator block is the only path around a gate, and it is recorded with a written rationale.

Separating this from ordinary L2 work is the point: the agent that wrote the change is never the authority that admits it.

### 3.8 UI Designer (L1) — `ui-designer`, sonnet

**Purpose.** Spencer's conversational design partner. He describes what he wants in plain language; this agent proposes, iterates, and — once he approves — writes a Design Spec that `platform-engineer` builds from.

**Owns:** `docs/design/`. Layout, information density, chart selection, interaction, empty and degraded states.

**Does not:** write production code, implement anything, or hand work directly to `platform-engineer`. Routing goes through the Orchestrator.

**The handoff is the point.** Design converges in conversation, where iteration is cheap; implementation starts only from an approved written spec, where iteration is expensive. A spec is not approved until its correctness checklist is filled in — uncertainty shown, gaps rendered as gaps, staleness visible, provenance reachable, sample sizes present, both denominations available. Those look cosmetic and each one causes a wrong trading decision.

### 3.9 Build Reporter (L0) — `build-reporter`, sonnet

**Purpose.** Spencer should not have to ask what changed. This agent produces the running record: doc change digests, changelog entries, bug and error summaries, ingestion health, and what needs his decision.

**Owns:** `docs/reports/`. Posts to `#opensea-dev`, and to `#opensea-alerts` for any S0a/S0b/S1 immediately rather than waiting for a digest.

**Read-only over everything else.** It never fixes anything; it names the problem and the owning agent.

**Cadence:** daily digest (skipped entirely on a quiet day — a digest that arrives regardless teaches him to ignore it), weekly summary against the phase plan, a short note per merge, and immediate incident reports.

**Its most valuable output is drift detection**: documents claiming one thing while code does another. That is reported as a defect with an owner.

### 3.10 Research (L0)

**Purpose.** Read-only investigation — market context, API changes, methodology literature, collection background.

**Owns:** external research, API documentation monitoring, competitive/tooling landscape, collection due diligence, post-mortems on retired signals.

**Constraints:** read-only; must cite sources; must distinguish fact from inference explicitly.

### 3.11 Docs Steward (L0) — `docs-steward`, haiku

**Purpose.** Keeps the document set current and posts a change feed to
`#opensea-dev` the moment anything changes — file, section, what changed, and
**why**, so a decision can be backtracked without reading commit history.

**Rules:** say why, not only what · one post per logical change · flag a
reversal loudly · never post a change that is merely proposed.

**Escalates to** the Orchestrator (ops team).

### 3.12 Bug Triage (L0) — `bug-triage`, haiku

**Purpose.** Owns `docs/logs/bugs.yaml`, the machine-readable ledger that
`docs/logs/BUGS.xlsx` and the narrative `BUGS.md` are both reconciled against.
Assigns severity and error class from `docs/05_BUG_TAXONOMY.md`, and records the
**monitor gap** for every bug — the reason it was not caught — which is the field
that has taught this project the most.

Nobody types into the spreadsheet. `python3 tools/buglog.py` regenerates it;
`--check` fails CI if a bug is marked fixed while the regression test it names
does not exist.

**Escalates to** the Orchestrator (ops team).

### 3.13 Docs Explainer (L0) — `docs-explainer`, sonnet

**Purpose.** Answers Spencer's questions about the document set and the system
in plain language, and explains engineering concepts he has not met before.
This project's Operator is a domain expert in the market and new to software
engineering norms; that combination is an asset only if the explaining
actually happens.

**Rules:** define jargon inline the first time · use a concrete example with
real numbers wherever one is possible · never invent a number to make an
example concrete, and never present an assumption as a measurement.

**May not:** change code, docs, or config. It explains what exists; the
`docs-steward` writes.

**Escalates to** the Orchestrator (ops team).

### 3.14 QA Auditor (L2) — `qa-auditor`, sonnet

**Purpose.** Reconciles what was **said** against what is **in the repo**.

Work here happens in long conversations. Things get decided, described, and
declared done in chat. Some land in the repo, some land partially, some land in
the code and never reach the document that specifies them. Nobody notices,
because the conversation moved on and the conversation is where the claim lives.

**Exists because four of this project's bugs share one shape** — a claim and an
implementation that disagreed, surviving because *everything except the code*
agreed:

| Bug | Every artifact said | The code did |
|---|---|---|
| BUG-001 | `.env` is read | read only `os.environ` |
| BUG-003 | 600 REST reads/hour | the real limit is 120 |
| BUG-006 | a frame closes every 5 seconds | flushed only when the next event arrived |
| BUG-010 | landing files are multi-frame and recoverable | could return only the first frame |

**Checks:** claims made in conversation against artifacts on disk · docs against
code, especially every number · docs against each other · the bug ledger against
reality (`tools/buglog.py --check`, plus whether a named regression test actually
asserts the failure mode) · agent definitions against the workflows that cite
them.

**Routes rather than fixes.** Dev findings to the Tech Lead; ops findings to the
Orchestrator; only ambiguity, trade-offs, and "something you were relying on is
not done" to Spencer.

**Says "unverifiable from the repo"** when a claim cannot be checked, and says
what would settle it. Never marks it verified.

**Runs continuously**, not only before a PR.

### 3.15 Design Lead (L2) — `design-lead`, opus

**Purpose.** Owns how the product **looks and reads**: the dashboard, chart
labels, number formats, timestamps, launcher output. Screenshots every
user-facing change in the browser it is for **before** the Operator sees it,
and asks the Operator design questions as their own short thread instead of
letting a developer default them.

**Exists because** BUG-041/042/043 — a white control box, UTC on every clock,
illegible hover cards — were found by Spencer in his first minute with the
page and by no agent. All three were invisible in the Linux container the page
was built in.

**Reference points** are the Operator's words, kept verbatim in the charter:
OpenSea/Coinbase layout with Austin FC Verde `#00B140`, Tableau-grade charts,
≥ 4.5:1 contrast everywhere, USD to the cent, hover cards readable, Central
time shown with its zone, a labelled moving-average overlay once there is
≥ 24 h of data (never smoothing the raw series in place).

**May** edit CSS and formatting directly. **Files** logic defects to the Tech
Lead as `PRS` (or `TMP` for timezone) bugs with a screenshot. **Never** changes
what a number means to make it prettier, hides an undefined value or a small-n
warning, or signs off a page it has not seen rendered.

**Reports to** the orchestrator. Design questions for Spencer go through the
orchestrator as a separate thread with two or three concrete options.

### 3.16 Market Analyst (L2) — `market-analyst`, opus

**Purpose.** On-chain market-structure due diligence on the wallets that make
this market: behaviour, holdings, flows, counterparties, clustering, and
manipulation-pattern detection, per **address**. Three makers produce 88% of
Argonauts events; the Operator needs to know what kind of participant is
behind a quote before trading against it.

**Sources.** The landing zone first; public chain data (RPC / block-explorer,
own key, never the OpenSea REST budget) second; OpenSea REST only for a public
profile lookup, through `RestClient`, counted.

**Boundary, stated plainly.** Addresses and clusters, never people. No linking
an address to a legal name, company, employer, or personal finances; nothing
recorded that the owner did not attach to the address themselves; profiles
stay local and gitignored; findings are structure ("this address did X"), not
motive. The charter is an exact description of the role, not a euphemism.

**Reports to** the orchestrator. Pattern detectors go to the validator, never
self-validated.

### 3.17 Context boundaries — summary

| Agent | Landing zone | Normalized | Train | Validation | Test | Config | Code | Live capital |
|---|---|---|---|---|---|---|---|---|
| Orchestrator | R | R | — | — | — | R | R | — |
| Data Engineer | R/W-append | R/W | — | — | — | R/W-branch | R/W-branch | — |
| Quant Research | R¹ | R¹ | **R/W** | — | — | R/W-branch³ | R/W-branch² | — |
| Platform Eng | — | R | — | — | — | R/W-branch | R/W-branch | — |
| Validator | R | R | R | **R** | **R (once)** | R | R/W-branch⁴ | — |
| Tech Lead | R | R | — | — | — | R | R | — |
| QA Auditor | R | R | — | — | — | R | R | — |
| Docs Explainer | R | R | — | — | — | R | R | — |
| UI Designer | — | R | — | — | — | R | — | — |
| Build Reporter | R | R | — | — | — | R | R | — |
| Research | — | R | — | — | — | R | R | — |
| Operator | R/W | R/W | R/W | R/W | R/W | R/W | R/W | **Sole** |

¹ **Training-window only.** Quant Research's landing-zone and normalized-store access is filtered at the data-access layer to records with `observed_at` inside the training partition. Unrestricted read access would silently void the partition control, since both stores contain the validation and test periods in full.
² **Analytical modules only** — models, metrics, signal definitions, notebooks. Quant Research may not write ingestion, platform, or governor code.
³ **`config/assumptions.yaml` only.** Quant Research owns the registry of judgments (`docs/06_TIME_UNITS_AND_LAYERING.md` §4.4) and may edit that one file in a branch. Every other config file is read-only to it, and it cannot merge its own change.
⁴ **Test and audit code only** — regression tests, leakage traps, reproduction scripts, audit tooling. The Validator must be able to write the test that demonstrates a defect; it may not modify the production code under review, and it may not merge.

Footnotes 2–4 are the only authorities L1 agents hold beyond the §2 baseline. They are granted narrowly, named explicitly, and none of them includes merge.

---

## 4. Development Workflows

### WF-D-01 · Feature Development

```
Operator states intent
  → Orchestrator: clarify scope, identify affected requirements, decompose
  → [ESCALATE if ambiguous — do not guess]
  → Specialist agent: design → interface review with Orchestrator
  → Specialist: implement with tests written alongside
  → Validator: independent review (correctness, leakage, provenance, performance)
  → [BLOCK if failed → return to specialist with findings]
  → tech-lead: coherence + requirement traceability + were findings really
       fixed + is the PR description complete, accurate, and honest about
       what was deferred
  → [BLOCK if failed → back to the specialist with specifics]
  → qa-auditor: does the repo match everything that was CLAIMED about it
  → design-lead: has a human-shaped eye SEEN it rendered where the Operator will see it (UI changes only)
  → [route findings down: dev → tech-lead, ops → orchestrator]
  → Open PR. Post to #opensea-dev.
  → ⛔ SPENCER APPROVES THE PR — no exceptions, no auto-merge
  → Integrator: merge
  → build-reporter: on-merge note
  → Post-merge verification against acceptance criteria
```

**Gate:** no merge without Validator sign-off on anything touching data ingestion, analytics, or backtesting. UI-only changes may use a lighter review.

### WF-D-02 · Signal Development (the highest-risk workflow)

```
Quant Research: articulate mechanism — who is on the other side, and why are they wrong?
  → [REJECT if no mechanism can be stated]
  → Register hypothesis: prediction, test design, kill criteria, prior
  → Register entry is APPEND-ONLY and timestamped BEFORE any data is examined
  → Explore on TRAINING partition only
  → [FAIL → RETIRED + post-mortem. NOT a second attempt with new parameters]
  → SPECIFIED: freeze parameters and code, document fully
  → HANDOFF to Validator (Quant Research's involvement ends here)
  → Validator: backtest with full costs, robustness suite, leakage audit
  → Validator: validation partition evaluation
  → [FAIL → RETIRED + post-mortem]
  → Validator: test partition — ONE evaluation, logged, never repeated
  → [FAIL → RETIRED. Signal is dead. No revival.]
  → VALIDATED → Operator reviews and approves paper trading
  → 30+ days paper trading with predefined success criteria
  → Operator decision: LIVE / extend / retire
```

**The handoff at `SPECIFIED` is the structural core of this workflow.** It is what makes the validation meaningful. An agent that both builds and validates will, without any intent to deceive, make a hundred small choices that favour its own result.

### WF-D-03 · Data Pipeline Change

```
Data Engineer: assess impact — does this change historical interpretation?
  → [ESCALATE if yes: historical reinterpretation invalidates prior backtests]
  → Implement behind a feature flag
  → Shadow-run: new and old paths in parallel, compare outputs
  → Validator: audit the diff, confirm every difference is explained
  → [BLOCK on unexplained differences]
  → Backfill plan reviewed against REST budget
  → Cut over; keep old path available for rollback
  → Record change in the data-provenance log
```

Requirement: no pipeline change ships without a shadow-run comparison. Silent changes in data semantics are the hardest class of bug to detect after the fact, because everything continues to look normal.

### WF-D-04 · Incident Response

```
Alert fires (data quality, ingestion failure, anomaly)
  → Triage: is data being corrupted right now?
  → [IF YES: halt ingestion immediately. A gap is recoverable; corruption may not be]
  → Diagnose against the audit log
  → Assess blast radius: which stored data is affected?
  → Fix, then remediate historical data via superseding records (never edits)
  → Post-mortem: root cause, detection gap, prevention
  → Add a regression test and a detection check for this failure mode
```

**Halt-on-corruption is the standing rule.** Stopping ingestion loses time that can be backfilled. Writing corrupt records into an append-only store creates a permanent problem.

### WF-D-05 · Weekly Research Cycle

```
Monday    Research: market context, API changes, collections of note
Tuesday   Validator: data quality audit; Quant: live signal performance vs. expectation
Wednesday Quant: hypothesis generation — mechanisms first
Thursday  Development work on the highest-priority validated need
Friday    Orchestrator: state review, register review, Operator briefing
```

---

## 5. Agent Interaction Protocol

### 5.1 Task briefs

Every delegated task carries: objective, scope boundaries (explicitly including what *not* to do), inputs and permitted data access, definition of done, escalation triggers, and relevant requirement IDs.

### 5.2 Result reports

Every completed task returns: what was done, what was **not** done and why, assumptions made, confidence and its basis, known limitations, and what should be verified independently.

**The "what was not done" and "known limitations" fields are mandatory and may not be empty.** An agent that reports unqualified success has not looked hard enough.

### 5.3 Escalation

Escalate immediately, do not proceed on assumption, when: requirements are ambiguous; an action is irreversible; capital is implicated; REST budget would be materially consumed; a result contradicts a prior validated finding; scope has grown; a result is surprisingly good.

**"Surprisingly good" is an escalation trigger.** In this domain, a result that looks too good is evidence of a bug or a leak far more often than it is evidence of an edge.

### 5.4 Disagreement

Agents disagreeing on an analytical question do not average their answers or defer to seniority. The disagreement is documented, both positions are stated with their evidence, and it escalates to the Operator. Where the disagreement is empirically resolvable, the resolving test is designed and run.

---

## 6. Runtime Agents

Software processes inside the platform. These are not AI agents; they are deterministic services with defined contracts. They are documented here because their authority boundaries matter as much as the development agents'.

```
┌─────────────────────────────────────────────────────────────┐
│  SUPERVISOR — lifecycle, health, restart, alerting on death  │
└───┬─────────────────────────────────────────────────────────┘
    │
    ├── STREAM CONSUMER      ← WebSocket, always-on, writes landing zone
    ├── REST GOVERNOR        ← budget allocator, priority queue, backoff
    ├── CHAIN INDEXER        ← Transfer events, holder ledger
    ├── RECONCILER           ← periodic stream-vs-REST drift check
    ├── GAP DETECTOR         ← finds holes, enqueues backfill
    ├── NORMALIZER           ← landing zone → normalized store
    ├── METRIC ENGINE        ← derived metrics, snapshot writer
    ├── WASH DETECTOR        ← scores sales
    ├── SIGNAL ENGINE        ← evaluates registered signals
    ├── IDEA GENERATOR       ← constructs Trade Ideas with full context
    ├── ALERT MANAGER        ← dedupe, prioritize, rate-limit, deliver
    ├── OUTCOME TRACKER      ← tracks every idea's result, acted on or not
    ├── SCRAPER (optional)   ← disabled by default; ToS-gated; output never feeds signals
    └── QUALITY MONITOR      ← continuous data-quality checks
```

### 6.1 Runtime agent constraints

| Agent | Hard constraints |
|---|---|
| Stream Consumer | Never drops an event silently. Connection loss records a gap window. Orders by `event_timestamp` |
| REST Governor | **Sole** REST caller. Enforces priority classes. Reads live rate-limit headers. Surfaces exhaustion |
| Reconciler | Records drift as a metric; never silently "fixes" by overwriting |
| Metric Engine | Never writes a metric without recording sample size and provenance |
| Signal Engine | Only evaluates signals in `VALIDATED` (paper) or `LIVE` (promoted) state. Never evaluates `EXPLORATORY` or `SPECIFIED` ones against live data |
| Idea Generator | Every idea reproducible from its recorded input snapshot (REQ-F-23). No idea without a position-size ceiling |
| Alert Manager | Rate-limited per collection. Tiered. Never suppresses a data-quality alert |
| Outcome Tracker | Tracks **all** ideas including unacted ones. Cannot be disabled — this is the platform's honesty mechanism |
| Scraper | Owned by Data Engineer. Disabled unless the REQ-D-21 ToS review has passed and is recorded. Human-scale request rates. Tags output `source=scrape`, which the Signal Engine and backtester refuse as input (REQ-D-19). Disables itself on layout change rather than returning a plausible wrong value (REQ-D-20) |

### 6.2 The Outcome Tracker deserves special mention

It records what happened to every idea the platform emitted, whether or not the Operator acted on it. This is deliberately not optional and deliberately not disableable.

Without it, evaluation of the platform becomes a memory exercise, and memory is systematically biased toward remembering the ideas that worked. The Outcome Tracker is the mechanism by which the question "is this thing actually working?" has a real answer rather than a feeling.

### 6.3 What no runtime agent may do

- Submit a transaction.
- Access a private key.
- Modify the landing zone or historical records.
- Change its own configuration.
- Suppress a data-quality alert.
- Promote a signal between lifecycle stages.

---

## 7. Runtime Workflows

### WF-R-01 · Continuous Ingestion (always on)

```
Stream event arrives
  → validate schema → [FAIL: quarantine + alert, never discard]
  → append to landing zone with observed_at
  → normalize → upsert (idempotent, ordered by event_timestamp)
  → trigger dependent metric recomputation
  → feed signal engine
```

### WF-R-02 · Scheduled Reconciliation

```
Every N hours (budget permitting)
  → Governor allocates BACKFILL-priority budget
  → REST snapshot for a rotating subset of collections
  → compare against stream-derived state
  → record drift metric per collection
  → [drift > threshold: quality alert + full resync for that collection]
```

Rotating subset, not all collections — a full sweep would exhaust the hourly budget.

### WF-R-03 · Signal Evaluation → Trade Idea

```
Data updated for collection C
  → Signal Engine evaluates registered VALIDATED signals for C
  → [no fire: log the evaluation anyway — non-firing is data]
  → Idea Generator assembles: thesis, fair value + interval, costs,
    time-to-exit estimate, position ceiling, confidence, input snapshot
  → economic significance check → [FAIL: log, suppress]
  → uncertainty check: fair_value_lower_bound clears costs? → [FAIL: suppress]
  → Alert Manager: dedupe, prioritize, rate-limit
  → deliver to Operator with a link to full evidence
  → Outcome Tracker begins tracking regardless of Operator action
```

Note the two suppression gates before delivery. Most signal firings should not become alerts. An alert stream the Operator learns to ignore is worse than none.

### WF-R-06 · Market Development Alerts (REQ-F-24)

Thesis-independent. These fire on material state change whether or not any signal is registered against the collection, and they are the mechanism by which the Operator learns about a collection *before* a signal exists for it.

```
Metric Engine updates state for collection C
  → evaluate development detectors:
      · regime_state transition (methodology §6.2)
      · changepoint in sale rate or unique buyers (§5.4)
      · smart_money_flow_w crossing threshold
      · holder concentration shift beyond band
      · listing_pressure or listed_ratio step change
      · wash_ratio_w material change
  → attach evidence: what changed, over what window, with what sample size
  → [suppress if the underlying sample is below the §8.2 minimum]
  → Alert Manager: dedupe, prioritize, rate-limit per collection
  → deliver at INFORMATIONAL tier, distinct from Trade Idea alerts
  → logged; no outcome tracking (there is no thesis to score)
```

Development alerts are explicitly **not** trade ideas and are tiered separately, so that the Trade Idea channel's precision is not diluted by them.

### WF-R-04 · Data Quality Monitoring

```
Continuous checks:
  · stream connection alive, event rate within expected band
  · REST budget consumption on track
  · reconciliation drift within threshold
  · no unexplained gaps in the snapshot series
  · metric distributions within historical bounds
  · wash ratio not spiking (may indicate manipulation OR a detector bug)
  · schema conformance
  → any breach: alert (never suppressed) + mark affected views as degraded
```

### WF-R-05 · Paper Trading

```
Operator or engine creates a paper position
  → immutable record: timestamp, thesis, entry, target, invalidation, criteria
  → [thesis and entry cannot be edited afterward — REQ-F-36]
  → continuous mark-to-market against live data
  → invalidation breached: flag for review
  → target hit or review date reached: verdict
  → outcome recorded against the originating signal's statistics
  → report includes comparison to ETH-hold benchmark (REQ-F-37)
```

---

## 8. Escalation Matrix

| Trigger | Escalates to | Urgency |
|---|---|---|
| Data corruption suspected | Operator | Immediate — halt first |
| Ingestion down > 1 hour | Operator | High |
| API contract change | Operator via Orchestrator | High |
| REST budget exhausted repeatedly | Operator | Medium |
| Reconciliation drift persistent | Operator via Validator | Medium |
| Signal result surprisingly strong | Validator, then Operator | Medium — treat as suspicious |
| Validator blocks a promotion | Operator | Medium |
| Requirement ambiguous | Operator | Blocking |
| Live signal degrading | Operator | Medium |
| Wash ratio spike | Operator | Low unless a trade is pending |

---

## 9. Anti-Patterns

Recorded because each is a plausible-seeming shortcut that would quietly destroy the project's validity.

| Anti-pattern | Why it is fatal |
|---|---|
| Same agent builds and validates a signal | Overfitting becomes undetectable. The single most important rule here |
| Agent merges without Spencer's PR approval | He is the only reviewer of record; a Validator sign-off is permission to *ask*, not to merge |
| Agent tweaks a failed hypothesis and retests | This *is* overfitting, performed procedurally |
| Agent accesses the test partition to "check" something | Permanently consumes the only honest evaluation available |
| Agent forward-fills a gap to make a chart look clean | Fabricated data entering the permanent record |
| Agent widens scope without escalating | Requirements stop being a contract |
| Agent reports a point estimate without uncertainty | Every downstream decision is miscalibrated |
| Agent suppresses a data-quality alert as noisy | The alert existed because someone predicted this failure |
| Agent computes a metric in the presentation layer | Guarantees eventual inconsistency between views |
| Agent edits landing-zone data to fix a bug | Destroys the ability to reprocess; data may be unrecoverable given rate limits |
| Runtime agent gains transaction capability "temporarily" | The safety model in Requirements §7.4 exists precisely to prevent this |

---

## 10. Onboarding a New Agent Session

Any new session working on this project reads, in order:

1. `00_REQUIREMENTS.md` — what is being built and what is out of scope
2. `01_METHODOLOGY.md` — how the analysis works and what standard of evidence applies
3. This document — its own role, authority, and boundaries
4. `03_VALIDATION_AND_TESTING.md` — what its work must pass

It then states, before beginning: which role it is operating in, which authority level, which data partitions it may access, and what its escalation triggers are. If it cannot determine its role, that is itself an escalation.
