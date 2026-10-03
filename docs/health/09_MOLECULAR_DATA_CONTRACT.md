# Molecular Data Contract — Genes, Transcripts, Proteins

**Version:** 0.1 · **Date:** 2026-10-03 · **Status:** Draft for Operator review
**Companion to:** `00_REQUIREMENTS.md`, `01_METHODOLOGY.md` §3–§4, `02_AGENT_HIERARCHY.md` §2.5–2.7, `03_VALIDATION_AND_TESTING.md` §6, `04_ENVIRONMENTS_AND_UNDO.md` §6

---

## 1. What this document is

The Operator asked for three agents: one responsible for protein information, one for
DNA, one for RNA — gathering, storage, upkeep and validation, "completely accurate and
appendable as data structures". This document is the contract those agents work to and
the structure the data side (built by Claude Code on the Operator's machine) implements.
It says what a record is, what "accurate at every level" means mechanically, how a
record is appended and superseded but never edited, which sources are authoritative,
and which checks must exist before any molecular record is cited by the model or shown
on a surface.

The existing knowledge base (`src/health/kb/data/`) already holds molecule, protein,
enzyme, gene and hormone **entities** with ontology ids (ChEBI, KEGG, UBERON) and
graded evidence. This contract does not replace them. It adds, beside them, the
**molecular layer**: sequence-level, structure-level and annotation-level records that
an entity can point at, with their own verification, releases and checks. The existing
`entity` record gains one optional field, `molecular`, that names the gene, transcript
and protein records it is backed by. Nothing in the engine reads the molecular layer in
Phase 1; the first consumer is the viewer's scale ladder (HREQ-P-10), where a rung with
no entity behind it is not shown.

## 2. The three domains and their records

One domain per agent; one record type per biological object; every record immutable.

| Domain | Agent | Record types | Primary source of truth | Secondary sources |
|---|---|---|---|---|
| **DNA** (genes and genomic spans) | `health-dna-curator` | `Gene`, `GenomicSpan`, `Variant` (Phase 2) | HGNC (symbol, id, locus type); Ensembl (GRCh38, gene model); NCBI Gene | RefSeq, GENCODE, ClinVar (variants, Phase 2) |
| **RNA** (transcripts, non-coding RNA) | `health-rna-curator` | `Transcript`, `Exon`, `NcRna`, `ExpressionSummary` | Ensembl / GENCODE (transcript models, MANE status); RefSeq (NM_/NR_/XM_) | MANE, miRBase, Rfam, GTEx (expression, graded) |
| **Protein** | `health-protein-curator` | `Protein`, `Isoform`, `Feature` (domains, sites, PTMs), `Structure`, `Interaction` (Phase 2) | UniProtKB/Swiss-Prot (reviewed entries only for the canonical record) | InterPro/Pfam, PDB (RCSB), Gene Ontology, Enzyme Commission, Reactome |

The three domains meet on the **central-dogma links**: a `Protein` names the
`Transcript` that encodes it; a `Transcript` names its `Gene`; a `Gene` lists its
transcripts. A link is a claim with evidence like any other; a link that the sources
disagree on is a `conflict`, not a choice.

## 3. Identity: every record has a stable id and resolves at its source

- Our ids: `gene:<HGNC symbol>` (e.g. `gene:AQP2`), `tx:<Ensembl transcript id without version>` (e.g. `tx:ENST00000199280`), `protein:<UniProt accession>` (e.g. `protein:P41181`), `isoform:<accession>-<n>`, `span:<gene>:<assembly>:<n>`.
- Every record carries `externalIds`, one entry per source database, each with the identifier **and the version** the source gives it (`ENSG00000167580.10`, `NM_000486.6`, `P41181` with `sequenceVersion: 1`, `entryVersion: 2xx`). An identifier without a version is an error for the sources that version (Ensembl, RefSeq, UniProt); HGNC and NCBI Gene ids are unversioned by design and carry the release date instead.
- An identifier is recorded only after it resolves at its source (HTTP 200, record not obsolete, not merged, not demoted from reviewed), with the label the source returns stored as `sourceLabel` and compared with the record's `name`/`symbol` — the same rule as HREQ-D-02 for literature.

## 4. Accuracy at every level — the six levels, each with a mechanical check

"Completely accurate" is not a feeling. A record is accurate at a level when the check
for that level passes and is recorded; a record that has not been checked at a level
says so (`verified: { L2: null }`), and a surface never shows an unchecked level as fact.

| Level | What it means | The check (offline unless stated) |
|---|---|---|
| **L0 Identity** | Ids resolve, labels match, release pinned | `mol-check` identity rules; online re-verification on a schedule (§8) |
| **L1 Sequence** | The stored sequence is exactly the source's at the pinned release | SHA-256 of the uppercase sequence (and UniProt's CRC64 where the source gives one) stored in the record; recomputed from the stored sequence file; compared with the source on re-verification |
| **L2 Internal consistency** | A record agrees with itself and its links | CDS length = 3 × (protein length + 1); exon lengths sum to transcript length; CDS inside the transcript; translation of the CDS (standard code; selenocysteine and non-AUG starts flagged, never silently accepted) equals the protein sequence; the protein's gene equals the transcript's gene; the span lies on the stated assembly and strand |
| **L3 Cross-source agreement** | Independent sources agree on identity and sequence | Ensembl ↔ RefSeq ↔ UniProt cross-references as the sources publish them; MANE Select agreement; a disagreement is stored as a `conflict` record with both values and never resolved by deletion |
| **L4 Claims and quantities** | Every number or annotation has evidence and a grade | Same rules as `01 §3`: value, unit, range, resolvable evidence, grade; GO terms carry their evidence code; expression summaries carry dataset, release, unit and n |
| **L5 Currency** | The record is pinned to a source release and drift is detected | `releases.json` names the release of every source used (`UniProt 2026_0x`, `Ensembl 11x`, `RefSeq annotation release`, `GRCh38.p14`, `HGNC <date>`); a scheduled re-verification (§8) compares live source values with the stored ones and opens a superseding record on any change |

A record's `verified` block records, per level, the date, the method, the agent and the
result; `mol-check` fails a record that claims a level it has no verification for.

## 5. Append-only: how a record changes

As `04 §6`: nothing is edited in place.

- Records are **immutable**. A correction, a source update or a new release produces a **new record** with `supersedes: <old id>@<recordedAt>`; the old record gains nothing (the index derives `supersededBy`). Readers resolve "current" through the index, never by the file's last line.
- Every record carries `recordedAt`, `recordedBy` (the agent name), `release` (the source release it was taken from) and `verification` (the §4 block).
- `conflicts` are records too, with both values, both sources and a note; a conflict is closed by a superseding record that cites the resolution, never by removing one side.
- A **snapshot manifest** per data release lists every record file with its SHA-256; the deferred knowledge-base rule `append-only` (`03 §6`, W-18) is enforced against it: a record present in the previous snapshot must be present, byte-identical, in the next.
- Storage format: one record per line (JSON Lines) per record type, so an append is one line and a diff is one line; the data side may choose otherwise only if appends stay single-record and diffs stay readable, and says why in an ADR.

## 6. Storage layout (to be built by the data side; the names are the contract)

```
src/health/kb/data/molecular/
  schema.molecular.json      definitions: Gene, GenomicSpan, Transcript, Exon, NcRna,
                             Protein, Isoform, Feature, Structure, ExpressionSummary,
                             Conflict, Verification, Xref, Release
  genes.jsonl                one Gene record per line, append-only
  transcripts.jsonl
  proteins.jsonl
  features.jsonl             domains, sites, PTMs (protein); exons (transcript)
  expression.jsonl           graded expression summaries (RNA); abundance (protein)
  conflicts.jsonl
  releases.json              the pinned source releases, with dates and URLs
  verification_log.jsonl     one verification record per check per record per level
  snapshots/<date>.manifest  SHA-256 of every file above at that release
  sequences/<type>/<id>.fa   transcript (mRNA), CDS and protein sequences; genomic
                             spans are coordinates plus checksum, re-fetchable from the
                             assembly, not stored (size); a variant stores its own
```

The existing `entity` record gains an optional `molecular` field:
`{ "gene": "gene:AQP2", "transcripts": ["tx:ENST00000199280"], "proteins": ["protein:P41181"] }`.

## 7. Checks the data side implements (`health.cli mol-check`)

Mirrors `kb-check`: every rule has an error code, a planted violation in
`tests/health_mol_selftest.py`, and a row in the table below; the self-test parses this
table (as it parses `03 §6`) and fails when a rule has neither an implementing code nor
a deferral by name. Offline mode is the default and runs in the gates; online mode runs
on the schedule of §8 and needs network.

| Rule | Statement | Level | Mode |
|---|---|---|---|
| `mol-schema` | Every record validates against `schema.molecular.json` | L0 | offline |
| `mol-id-format` | Our ids and every external id match the source's pattern; versioned sources carry a version | L0 | offline |
| `mol-label-match` | `sourceLabel` equals the source's label at `release` | L0 | online |
| `mol-resolves` | Every external id resolves and is current (not obsolete, merged or demoted) | L0 | online |
| `mol-sequence-hash` | The stored checksum equals the checksum of the stored sequence file; the file exists for every sequence-bearing record | L1 | offline |
| `mol-sequence-source` | The stored checksum equals the source's sequence at `release` | L1 | online |
| `mol-cds-protein` | CDS length and translation agree with the protein record (standard code; flagged exceptions carry evidence) | L2 | offline |
| `mol-exon-sum` | Exon lengths sum to transcript length; CDS lies inside the transcript | L2 | offline |
| `mol-central-dogma` | Protein → transcript → gene links exist, are mutual, and name the same gene | L2 | offline |
| `mol-xref-agree` | Cross-references published by each source agree with our links, or a `conflict` record exists | L3 | online |
| `mol-mane` | A protein-coding gene with a MANE Select transcript names it as canonical, or a conflict says why not | L3 | offline (from the stored MANE release) |
| `mol-claim-evidence` | Every quantity, feature and annotation carries resolvable evidence and a grade; GO terms carry an evidence code | L4 | offline |
| `mol-release-pinned` | Every record names a release present in `releases.json` | L5 | offline |
| `mol-drift` | A live source value differs from the stored one → an open drift finding that only a superseding record closes | L5 | online |
| `mol-append-only` | Every record of the previous snapshot is present, byte-identical | L5 | offline |
| `mol-verified-claim` | A record claims no level without a verification record for it | — | offline |
| `mol-entity-link` | Every `entity.molecular` reference names an existing current record | — | offline |

## 8. Upkeep: the scheduled re-verification

A scheduled job (the same shape as the nightly robustness run, W-21) runs `mol-check`
in online mode against the live sources, with a fixed request budget and polite
rate limits, and writes a drift report. A drift is never auto-applied: the responsible
curator writes the superseding record, with the release that changed it, through
WF-H-08. Sources release on known cadences (UniProt every eight weeks, Ensembl several
times a year, RefSeq continuously): the report says which release each drift came from.

## 9. Who does what

- The three curators (`02 §2.5–2.7`) gather, record and verify their own domain, and
  never validate their own records at L3 (cross-source) or across the central-dogma
  links — the Rigor Lead does, adversarially, each sweep (`02 §2.3`).
- The data side (Claude Code on the Operator's machine) builds §6 and §7: schema,
  storage, the checker with planted violations, the index that resolves "current", the
  snapshot manifest, and the CLI; it is a module (`molecular-kb`) in
  `config/health/modules.yaml` with a removal recipe, flag off until its first data
  release passes `mol-check` offline (the two-PR rule, `04 §6.5`).
- The Safety Reviewer reads any surface that shows molecular information as a
  clinician would: a gene or protein fact is educational, never a diagnosis; a variant
  (Phase 2) is never shown with a clinical interpretation (`00 §1.4`, HREQ-S-04).
- The Operator approves every PR; the first data release is a decision (D-12).

## 10. Phasing

| Phase | Scope |
|---|---|
| **1a** (this slice) | Contract, three agents, the structure (§6–§7) with planted tests, the `molecular-kb` module flag off |
| **1b** | The first data release: every molecule, protein, enzyme and hormone entity the V1 knowledge base holds (about 20) with Gene, Transcript (MANE Select), Protein records verified at L0–L3; the `entity.molecular` links; the scale ladder shows them |
| **2** | Variants (ClinVar, dbSNP) with the safety rule above; interactions; structures beyond ids; expression atlases with grades |
| **3** | Pathways and reactions linked to the engine's fluxes, so a flux names the enzymes and transporters that carry it |

## 11. Requirements minted here

| ID | Requirement (short form) | § | Enforced by |
|---|---|---|---|
| HREQ-B-01 | Every molecular record is immutable; a change is a superseding record naming what it replaces | 5 | `mol-append-only`, snapshot manifest |
| HREQ-B-02 | Every external identifier carries its source version or release, resolves at the source, and its label matches | 3 | `mol-id-format`, `mol-resolves`, `mol-label-match` |
| HREQ-B-03 | Every stored sequence carries a SHA-256 (and the source's own checksum where published) verified against the stored file and the source | 4 | `mol-sequence-hash`, `mol-sequence-source` |
| HREQ-B-04 | Protein, transcript and gene records agree with each other (CDS, translation, exon sums, mutual links) | 4 | `mol-cds-protein`, `mol-exon-sum`, `mol-central-dogma` |
| HREQ-B-05 | A disagreement between sources is a `conflict` record; it is never resolved by choosing silently | 4, 5 | `mol-xref-agree`, review |
| HREQ-B-06 | Every quantity, feature and annotation carries resolvable evidence and a grade, as `01 §3` | 4 | `mol-claim-evidence` |
| HREQ-B-07 | Every record names the source release it was taken from; drift is detected on a schedule and closed only by a superseding record | 4, 8 | `mol-release-pinned`, `mol-drift` |
| HREQ-B-08 | A record claims an accuracy level only with a verification record for that level; surfaces show unchecked levels as unchecked | 4 | `mol-verified-claim`, viewer |
| HREQ-B-09 | A curator never validates its own records at L3 or across the central-dogma links; the Rigor Lead does | 9 | process, `02 §2.3` |
| HREQ-B-10 | Molecular facts on a surface are educational; a variant is never shown with a clinical interpretation | 9 | Safety review, HREQ-S-04 |
| HREQ-B-11 | The molecular layer is a registered module with a rehearsed removal recipe; the engine does not import it in Phase 1 | 9 | `health.registry --check` |
| HREQ-B-12 | Every rule in §7 has an error code with a planted violation, or a deferral by name; the self-test parses §7 | 7 | `tests/health_mol_selftest.py` |

## 12. Open questions for the Operator

- **D-12** The first data release (Phase 1b) is cited by the viewer only; should any
  engine parameter (e.g. an enzyme's Km) be allowed to cite a molecular record before
  Phase 3? Default: no.
- **D-13** Genomic sequences are not stored (coordinates plus checksum, re-fetchable);
  transcript, CDS and protein sequences are. Agree, or store everything under Git LFS?
- **D-14** Expression data (GTEx) is a summary with a grade, not raw samples. Agree?
