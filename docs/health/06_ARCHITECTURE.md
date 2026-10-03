# Architecture — Health

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft for Operator review
**Companion to:** `00_REQUIREMENTS.md`, `04_ENVIRONMENTS_AND_UNDO.md`, `07_MODULE_REGISTRY.md`
**Diagram sources:** `docs/health/diagrams/` (Mermaid `.mmd` files render on GitHub; `architecture.svg` is the static overview)

Every figure here is also a file under `diagrams/`, so a diagram can be changed in one
place and the change reviewed as a diff. The text beside each figure says what the
figure cannot: *why* the boundary is where it is and what it costs.

---

## 1. System context

```mermaid
flowchart LR
  Op(("Spencer<br/>Operator · L4<br/>approves every PR"))
  subgraph SRC["Outside the repository"]
    Lit["Literature<br/>DOI · PMID · textbooks"]
    Onto["Ontologies and registries<br/>UBERON · CL · GO · KEGG · Reactome · UniProt · ChEBI · HGNC"]
  end
  subgraph HP["Health platform (this repository)"]
    direction TB
    KB[("Knowledge base<br/>entities · relations · evidence · verification log")]
    PR[("Parameter table<br/>params.json — value · unit · range · grade · source")]
    EN["Engine<br/>solver · model · scenarios · Monte Carlo · validate"]
    VH["Validation<br/>golden equivalence · expectations · kb-check · gates"]
    SF["Surfaces<br/>reference app · health.cli · (future) dashboard"]
  end
  Lit -->|"curator verifies, grades"| KB
  Onto -->|"ids resolved, labels compared"| KB
  KB -->|"quantities mirror rows"| PR
  PR --> EN
  EN --> VH
  VH -->|"results with their band, labels and disclaimer; expectation status shown beside them, not used as a filter"| SF
  SF --> Op
  Op -.->|"direction · decisions · PR approval"| HP
```

**Why the knowledge base and the parameter table are two things.** The knowledge base
describes the world (what the kidney is, what a source says). The parameter table
describes the *model's* use of it (which number, in which equation, with which grade).
A quantity in the knowledge base can `engineParam`-mirror a parameter row, and
`kb-check` fails if they disagree, but the two are owned by different agents
(`02 §3`) so a tuned value cannot quietly inherit a grade.

---

## 2. Layers and files

```mermaid
flowchart TB
  subgraph L6["PROCESS"]
    A1[".claude/agents/health-*.md"] --- A2["docs/logs/bugs.yaml (area: health)"] --- A3["docs/health/logs/PROCESS_LOG.md"] --- A4["docs/health/decisions/ADR-*.md"] --- A5["tools/gates.py · .github/workflows/ci.yml"]
  end
  subgraph L5["SURFACES"]
    S1["reference/metabolic-map-v1/index.html + app/ (V1, verbatim)"] --- S2["src/health/cli.py<br/>status · kb-check · kb-summary"]
  end
  subgraph L4["VALIDATION"]
    V1["tests/health_selftest.py"] --- V2["tests/health_kb_selftest.py"] --- V3["tests/fixtures/health/golden_v1.json<br/>← tools/health_golden.mjs (Node, JS reference)"] --- V4["src/health/engine/validate.py<br/>evaluate_expectations"]
  end
  subgraph L3["ENGINE  src/health/engine/"]
    E1["solver.py<br/>RK4 · breakpoints · max_step"] --- E2["model.py<br/>13 states · M0–M10 · strain index"] --- E3["scenarios.py<br/>events · overrides · expects"] --- E4["mc.py<br/>mulberry32 · sampling · quantiles"] --- E5["params.py"]
  end
  subgraph L2["PARAMETERS"]
    P1["src/health/engine/params.json — 54 rows"]
  end
  subgraph L1["KNOWLEDGE  src/health/kb/"]
    K1["data/entities.json (107)"] --- K2["data/relations.json (203)"] --- K3["data/evidence.json (57)"] --- K4["data/VERIFICATION_LOG.json (154)"] --- K5["data/schema.json"] --- K6["check.py · report.py"]
  end
  L1 --> L2 --> L3 --> L4 --> L5
  L6 -.->|"gates everything"| L4
```

Counts are from the vendored V1 files (`reference/metabolic-map-v1/kb/*.json`,
`engine/params.json`) at model 1.0.1. `health.cli status` recomputes them from the data on
every run; the figure is a snapshot of that output, not a second source.

**Cost of the split.** The JavaScript reference stays authoritative for the browser and
the Python engine for the gates, so a model change is made twice. That is deliberate
(`decisions/ADR-0002`): the second implementation is the cheapest independent check of
the first, and the golden fixture makes the agreement a test rather than a belief.

---

## 3. From a citation to a chart

```mermaid
flowchart LR
  E["Evidence record<br/>ev:robertson-athar-1976<br/>DOI · PMID · quote · accessed"]
  VR["Verification<br/>method · checkedOn · retraction · errata"]
  Q["Quantity<br/>value 281.0 · unit mOsm/kg<br/>range [278, 285.5] · grade A-primary"]
  P["params.json row<br/>adh_threshold<br/>engineParam mirror checked by kb-check"]
  M["model.py block M4<br/>ADH_target = max(0, slope · (osm − threshold))"]
  S["simulate()<br/>default params · scenario · dt · seed"]
  MC["simulate_mc()<br/>n samples · q05 · q50 · q95"]
  H["evaluate_expectations()<br/>one row per registered expectation<br/>pass · fail · not_checked"]
  C["Chart<br/>band + median + reference lines<br/>disclaimer · evidence chip · grade"]
  E --> VR --> Q --> P --> M --> S --> MC --> H --> C
  P -.->|"paramsFor(key): influence screen"| C
```

Every arrow is a place a number can go wrong, and each has a check: verification
(`02 §2.2`), the engineParam mirror (`kb-check`), the M-block audit against the JS
reference (golden test), the band (`HREQ-U`; a requirement with no result-schema gate yet,
`01` Appendix A), the harness row count (`ExpectationSkippedError`), and the surface
checklist (safety review, by hand).

---

## 4. The validation pipeline

```mermaid
flowchart LR
  subgraph CI["CI: push to main, pull request to main"]
    direction LR
    c1["selftest.py<br/>(stdlib, market)"] --> c2["health_selftest.py<br/>(stdlib)"] --> c3["health_kb_selftest.py<br/>(stdlib)"] --> c4["pip install"] --> c5["all three, --no-skips"] --> c6["health.cli kb-check"] --> c7["zstd probe · codec contract<br/>(market)"] --> c8["buglog.py --check"] --> c9["ruff"] --> c10["pytest"] --> c11["secrets_check.py"] --> c12["signing grep · error-swallow checks"]
  end
  subgraph REF["Model change, or new fixture sections"]
    r1["node tools/health_golden.mjs"] --> r2["golden_v1.json regenerated<br/>(old sections byte-identical when only sections are added)"] --> r3["diff explained line by line"]
  end
  subgraph REV["Review"]
    v1["health-rigor-lead<br/>re-derive · perturb · plant violations"] --> v2["tech-lead<br/>traceability · reality · PR description"] --> v3["health-safety-reviewer<br/>disclaimer · labels · framing"] --> v4(("⛔ Spencer"))
  end
  CI --> REF --> REV
```

The triggers and the step order are those of `.github/workflows/ci.yml`: the job runs on
a push to `main` and on a pull request to `main` (each push to its branch re-runs it), and
a failed step stops the job, so every step after it is skipped. A push to a branch with no
pull request open against `main` runs no CI; `tools/gates.py`, run by the agent before
claiming green, is then the only gate. The
stdlib-only steps run **before** `pip install` on purpose (`docs/03 §4.6`): a suite that
cannot run until the environment is built cannot tell you the environment is broken.
This round adds `tests/health_errors_selftest.py` and a golden `--check` step in
`tools/gates.py` that runs where a Node binary exists and prints a loud SKIPPED otherwise;
the figure shows the workflow before any CI step for either, and is redrawn if the merge
adds one. [VERIFY-AFTER-MERGE]

---

## 5. How work flows through the agents

```mermaid
sequenceDiagram
  participant Op as Spencer (L4)
  participant Or as orchestrator
  participant Cu as literature-curator
  participant Mo as physiology-modeler
  participant Ri as rigor-lead (independent)
  participant Sa as safety-reviewer (independent)
  participant TL as tech-lead
  Op->>Or: "add potassium dynamics"
  Or->>Cu: register expectations BEFORE any run (WF-H-02)
  Cu-->>Or: expects[] with ranges, kinds, evidence
  Or->>Mo: implement behind module flag (enabled: false)
  Mo-->>Mo: MODEL_VERSION bump, golden regenerated, tests green
  Mo-->>Ri: hand off (modeler's involvement in validation ends)
  Ri-->>Ri: re-derive params, ±20 % perturbation, planted KB violations
  Ri-->>TL: PASS / BLOCK with findings
  TL-->>Sa: user-facing text and surfaces
  Sa-->>TL: HREQ-S checklist result
  TL->>Op: PR with verified claims, deferred items, risk paragraph
  Op-->>Or: approve → merge (module still off)
  Or->>Op: enable/<module> PR (one line) → approve
```

---

## 6. Modules and how they come apart

```mermaid
flowchart TB
  ref["reference-v1<br/>reference/metabolic-map-v1/<br/>(verbatim, read-only)"]
  eng["engine-v1<br/>src/health/engine/ · tests/health_selftest.py<br/>golden_v1.json · tools/health_golden.mjs"]
  kb["kb-v1<br/>src/health/kb/ · tests/health_kb_selftest.py"]
  cli["cli<br/>src/health/cli.py"]
  proc["process<br/>docs/health/ · .claude/agents/health-*.md<br/>config/health/ · gates and CI steps"]
  ref --> eng
  ref --> kb
  eng --> cli
  kb --> cli
  proc -.-> eng
  proc -.-> kb
  proc -.-> cli
  classDef off fill:#eee,stroke:#999,color:#333
```

Arrows point from a dependency to what needs it. Removal goes against the arrows: `cli`
first, then `engine-v1` or `kb-v1`, then `reference-v1`; `process` can be removed at any
time because nothing imports it. Every entry in `config/health/modules.yaml` carries the
recipe (`04 §6.5`), and the registry is the only place the dependency edges are recorded.

---

## 7. The model, as a feedback diagram

```mermaid
flowchart LR
  subgraph IN["Inputs (piecewise constant, breakpoints)"]
    W["water in"]; NA["Na in"]; K["K in"]
  end
  subgraph GUT["M2 gut"]
    VG["V_gut_water"]; NG["Na_gut"]
  end
  subgraph FLUID["M1 · M3 fluid compartments"]
    VE["V_ecf"]; VI["V_icf"]; NE["Na_ecf"]; KI["K_icf"]; SI["osm_icf_solute"]
  end
  subgraph HORM["M4 · M5 · M6 hormones, drive, pressure"]
    ADH["ADH"]; TH["Thirst"]; AL["Aldo"]; AN["ANP"]; MAP["MAP"]; RA["R_auto (slow)"]
  end
  subgraph KID["M7 kidney"]
    GFR["GFR"]; FE["FE_Na"]; UO["U_osm"]; UF["urine flow"]
  end
  W --> VG --> VE
  NA --> NG --> NE
  K --> KI --> SI
  VE -- "osm_e = 2[Na] + other" --> ADH
  VE -- "volume shifts threshold" --> ADH
  ADH --> UO --> UF --> VE
  VE -- "osm gradient" --> VI
  VE -- "vr^-aldo_vol_exp" --> AL
  VE -- "vr^anp_vol_exp" --> AN
  VE -- "fast share" --> MAP
  RA -- "slow share, τ ≈ 10 d" --> MAP
  VE --> RA
  MAP -- "pressure natriuresis" --> FE
  AL -- "retains Na" --> FE
  AN -- "natriuretic" --> FE
  MAP --> GFR
  VE --> GFR
  GFR --> FE
  FE -- "Na_ur" --> NE
  NE --> VE
  VE --> TH
  SI[" "] -.-> VI
```

Thirteen states, as in `model.js` `STATE_KEYS`; the block labels (M0–M8 and M10; neither
implementation has an M9, BUG-20261003-099) match the code comments in both
implementations so the audit path is: diagram → block → two
implementations → golden test. The kidney strain index (M10) is a derived quantity, not
a state, and is deliberately absent from this figure: it summarises four of these loads,
it is not a loop.

---

## 8. Scientific roadmap as modules

```mermaid
flowchart LR
  P0["Phase 0<br/>V1 slice: water · Na · ADH · Aldo · ANP · MAP · kidney<br/>(this PR)"]
  P1["Phase 1<br/>body-size scaling · plasma K · Na storage compartment"]
  P2["Phase 2<br/>chronic pressure and kidney function (research)<br/>outcome layer population-only"]
  P3["Phase 3<br/>glucose–insulin · obesity–hypertension coupling"]
  P4["Phase 4<br/>population layer · intervention comparison with bands"]
  P5["Phase 5<br/>Python surface from an approved design spec"]
  P0 --> P1 --> P2 --> P4
  P1 --> P3 --> P4
  P4 --> P5
```

Each phase is one or more modules under `07_MODULE_REGISTRY.md`, each landing with its
flag off, its expectations registered first, and its own ADR.

---

## 9. What is deliberately not here

- **No database.** JSON files with a schema and code-enforced cross-file rules are diffable, reviewable and append-only friendly. The day a query needs a database, that is an ADR.
- **No service.** Nothing listens on a port. The reference app is static; the CLI is a process that exits.
- **No personal data.** Not a field, not a column, not a plan. A module that needs it is a separate amendment (`00 §1.4`).
- **No adaptive solver.** Fixed-step RK4 bounded by the fastest time constant is what V1 validated; a stiff module later gets its own ADR and its own golden protocol.
