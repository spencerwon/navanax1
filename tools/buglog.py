#!/usr/bin/env python3
"""Render the bug ledger to a spreadsheet, and check it against reality.

    python3 tools/buglog.py            # regenerate docs/logs/BUGS.xlsx
    python3 tools/buglog.py --check    # CI gate, writes nothing
    python3 tools/buglog.py --mark-removed <module-id>   # a health removal recipe step

`docs/logs/bugs.yaml` is the source of truth. Nobody types into the
spreadsheet: a hand-maintained log drifts from the code within a week, and this
project has already shipped four bugs whose entire shape was "every artifact
agrees except the code".

--check enforces three things a human reviewer would otherwise have to hold in
their head:

  1. every bug id in the ledger appears in docs/logs/BUGS.md, and vice versa
  2. every `locations` file actually exists in the repo
  3. every fixed bug names a regression test, and that test function exists
  4. every BUG-id mentioned anywhere in the repo exists in the ledger

(3) is the one that matters. "Fixed" without a regression test is a claim, and
`tech-lead` blocks on exactly that. For a fixed entry every `regression_test` item is
either `file::function` (the file exists and defines the function) or exactly the literal
`docs-only (no mechanical guard)` -- and then every one of the entry's locations must be
documentation: under docs/, a .md/.mmd/.svg file, or a comment line of a .yaml/.yml file.
Anything else (prose, a bare file, "none -- re-verified by hand") fails, naming the entry:
the gate cannot tell a placeholder from a test. Four entries of 2026-09-09 whose prose
names a CI step predate the rule and are listed, by id, in PRE_STRICT_PROSE_TESTS.

(4) reads git-tracked files only (`git ls-files -z` from the repository root), and walks
the directory tree only when git is unavailable or the root is not a checkout: an ignored
or untracked path -- a builder's worktree under .claude/worktrees/, a local scratch file --
is not part of the repository, and an id in it must never fail (or pass) the gate.

`removed_with_module: <module id>` (or a list of ids) is the one exception to (2) and to
the existence half of (3). A health module's removal recipe (docs/health/07) deletes its
files on purpose, and the ledger is append-only, so the entries that cite those files are
kept and marked instead: `--mark-removed <id>` adds the field to every entry whose
locations or regression tests fall under that module's `paths` or `tests` in
config/health/modules.yaml (run it before the entry is removed), and `--check` then skips
the existence checks for that entry -- and only for it. Everything else still holds: the
entry must be in BUGS.md, a fixed one must still name its test and its resolved_at, and the
module it names must no longer be registered (a field that hides a live module's broken
reference is itself a failure). The field is a column of the spreadsheet.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs" / "logs" / "bugs.yaml"
MARKDOWN = ROOT / "docs" / "logs" / "BUGS.md"
SHEET = ROOT / "docs" / "logs" / "BUGS.xlsx"

# docs/05_BUG_TAXONOMY.md 2. Kept here as data so the sheet and the gate agree.
SEVERITY = {
    "S0a": ("Data corruption", "HALT INGESTION", "C00000"),
    "S0b": ("Backtest integrity", "HALT BACKTESTS", "C55A11"),
    "S1":  ("Silent wrongness", "Alert, non-suppressible", "E36C0A"),
    "S2":  ("Data loss / availability", "Record the gap, never fill it", "BF8F00"),
    "S3":  ("Functional break", "Fix in this phase", "7F7F7F"),
    "S4":  ("Cosmetic", "Backlog", "A6A6A6"),
}
PRIORITY = {
    "P0": "Drop everything -- blocks merge",
    "P1": "Before the next merge",
    "P2": "This phase",
    "P3": "Backlog",
}
ERROR_CLASS = {
    "CFG": "Configuration / environment",
    "ING": "Ingestion",
    "NRM": "Normalisation",
    "STA": "Statistics",
    "RTL": "Rate limiting",
    "TMP": "Temporal / bitemporal",
    "SEC": "Security",
    "INF": "Infrastructure",
    "PRS": "Presentation",
    "BTI": "Backtest integrity",
    # Health subsystem classes (docs/health/05_BUG_TAXONOMY.md §3, ADR-0005)
    "MDL": "Model (health)",
    "PRM": "Parameter (health)",
    "EVD": "Evidence (health)",
    "NUM": "Numerics (health)",
    "KBI": "Knowledge base integrity (health)",
    "VAL": "Validation harness (health)",
    "ETH": "Ethics / framing (health)",
}

COLUMNS = [
    ("ID", "id", 20),
    ("Date", "_date", 12),
    ("Time (CT)", "_time", 11),
    ("Severity", "severity", 10),
    ("Severity means", "_sev_meaning", 26),
    ("Response", "_sev_response", 30),
    ("Priority", "priority", 9),
    ("Priority means", "_pri_meaning", 27),
    ("Class", "error_class", 8),
    ("Class means", "_cls_meaning", 26),
    ("Status", "status", 10),
    ("Summary", "summary", 60),
    ("Root cause", "root_cause", 60),
    ("Data impact", "data_impact", 60),
    ("Resolution", "resolution", 60),
    ("Monitor gap (why it was not caught)", "monitor_gap", 55),
    ("Requirement", "requirement", 14),
    ("Branch", "branch", 24),
    ("Code index", "_code_index", 34),
    ("Open in GitHub", "_code_link", 40),
    ("Regression test", "regression_test", 44),
    ("Removed with module", "removed_with_module", 16),
    ("Detected by", "detected_by", 34),
    ("Channel", "detection_channel", 12),
    ("Occurred", "occurred_at", 22),
    ("Logged", "logged_at", 22),
    ("Resolved", "resolved_at", 22),
    ("PR", "_pr_link", 10),
]


def load() -> dict:
    import yaml
    with LEDGER.open() as fh:
        return yaml.safe_load(fh)


def derive(bug: dict, repo: str) -> dict:
    b = dict(bug)
    logged = str(b.get("logged_at", ""))
    b["_date"] = logged[:10]
    b["_time"] = logged[11:19]
    sev = SEVERITY.get(b.get("severity", ""), ("", "", "808080"))
    b["_sev_meaning"], b["_sev_response"], b["_sev_colour"] = sev
    b["_pri_meaning"] = PRIORITY.get(b.get("priority", ""), "")
    b["_cls_meaning"] = ERROR_CLASS.get(b.get("error_class", ""), "")
    locs = b.get("locations") or []
    b["_code_index"] = "  ·  ".join(f"{loc['file']}:L{loc['line']}" for loc in locs)
    branch = b.get("branch", "main")
    b["_code_link"] = "\n".join(
        f"https://github.com/{repo}/blob/{branch}/{loc['file']}#L{loc['line']}"
        for loc in locs
    )
    b["_pr_link"] = f"#{b['pr']}" if b.get("pr") else ""
    # A fix may be backed by more than one test, and naming only one of them
    # hides the others from the gate. The ledger accepts a string or a list.
    rt = b.get("regression_test")
    if isinstance(rt, list):
        b["regression_test"] = "\n".join(rt)
    rm = b.get("removed_with_module")
    if isinstance(rm, list):
        b["removed_with_module"] = ", ".join(map(str, rm))
    for k, v in list(b.items()):
        if isinstance(v, str):
            b[k] = " ".join(v.split()) if "\n" in v else v
    return b


# ---------------------------------------------------------------------------
def build(data: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    repo = data["repo"]
    bugs = [derive(b, repo) for b in data["bugs"]]

    wb = Workbook()
    ws = wb.active
    ws.title = "Bug log"

    head_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F3864")
    body_font = Font(name="Arial", size=10)
    mono_font = Font(name="Arial", size=9)
    link_font = Font(name="Arial", size=9, color="0563C1", underline="single")
    top = Alignment(vertical="top", wrap_text=True)
    thin = Side(style="thin", color="D9D9D9")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    for c, (title, _key, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=c, value=title)
        cell.font, cell.fill, cell.border = head_font, head_fill, box
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.row_dimensions[1].height = 34

    for r, bug in enumerate(bugs, start=2):
        for c, (_title, key, _w) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=r, column=c, value=bug.get(key, ""))
            cell.font = body_font
            cell.alignment = top
            cell.border = box
            if key == "id":
                cell.font = Font(name="Arial", size=10, bold=True)
            elif key == "severity":
                cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor=bug["_sev_colour"])
                cell.alignment = Alignment(vertical="top", horizontal="center")
            elif key == "status":
                cell.fill = PatternFill(
                    "solid",
                    fgColor="C6EFCE" if bug.get("status") == "fixed" else "FFC7CE",
                )
            elif key in ("_code_index", "regression_test", "requirement"):
                cell.font = mono_font
            elif key == "_code_link":
                cell.font = link_font
                first = str(bug.get("_code_link", "")).split("\n")[0]
                if first:
                    cell.hyperlink = first
            elif key == "_pr_link" and bug.get("pr"):
                cell.font = link_font
                cell.hyperlink = f"https://github.com/{repo}/pull/{bug['pr']}"
        ws.row_dimensions[r].height = 120

    last = get_column_letter(len(COLUMNS))
    table = Table(displayName="BugLog", ref=f"A1:{last}{len(bugs) + 1}")
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleLight1", showRowStripes=True, showColumnStripes=False
    )
    ws.add_table(table)
    ws.freeze_panes = "C2"

    # -- Severity hierarchy sheet ------------------------------------------
    hs = wb.create_sheet("Severity hierarchy")
    hs["A1"] = "Error hierarchy -- docs/05_BUG_TAXONOMY.md §2"
    hs["A1"].font = Font(name="Arial", size=12, bold=True)
    hs["A2"] = ("Severity is about CONSEQUENCE, not effort. It decides what stops. "
                "Priority is derived from severity and from whether data is "
                "actively being lost while the bug is open.")
    hs["A2"].font = Font(name="Arial", size=10, italic=True)
    hs.merge_cells("A2:E2")
    hs["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    hs.row_dimensions[2].height = 34

    row = 4
    headers = (("Severity", 12), ("Means", 28), ("Response", 34),
               ("Open now", 12), ("Total", 10))
    for col, (h, w) in enumerate(headers, start=1):
        cell = hs.cell(row=row, column=col, value=h)
        cell.font, cell.fill = head_font, head_fill
        hs.column_dimensions[get_column_letter(col)].width = w
    for i, (sev, (means, response, colour)) in enumerate(SEVERITY.items()):
        r = row + 1 + i
        hs.cell(row=r, column=1, value=sev).font = Font(
            name="Arial", size=10, bold=True, color="FFFFFF")
        hs.cell(row=r, column=1).fill = PatternFill("solid", fgColor=colour)
        hs.cell(row=r, column=2, value=means).font = body_font
        hs.cell(row=r, column=3, value=response).font = body_font
        n = len(bugs) + 1
        hs.cell(row=r, column=4,
                value=f'=COUNTIFS(\'Bug log\'!$D$2:$D${n},$A{r},'
                      f'\'Bug log\'!$K$2:$K${n},"<>fixed")').font = body_font
        hs.cell(row=r, column=5,
                value=f"=COUNTIF('Bug log'!$D$2:$D${n},$A{r})").font = body_font

    note = row + 2 + len(SEVERITY)
    hs.cell(row=note, column=1,
            value="Priority").font = Font(name="Arial", size=11, bold=True)
    for i, (p, meaning) in enumerate(PRIORITY.items()):
        hs.cell(row=note + 1 + i, column=1, value=p).font = Font(
            name="Arial", size=10, bold=True)
        hs.cell(row=note + 1 + i, column=2, value=meaning).font = body_font

    cls = note + 2 + len(PRIORITY)
    hs.cell(row=cls, column=1,
            value="Error class").font = Font(name="Arial", size=11, bold=True)
    for i, (k, meaning) in enumerate(ERROR_CLASS.items()):
        hs.cell(row=cls + 1 + i, column=1, value=k).font = Font(
            name="Arial", size=10, bold=True)
        hs.cell(row=cls + 1 + i, column=2, value=meaning).font = body_font

    src = wb.create_sheet("How this file is made")
    for i, line in enumerate([
        "This spreadsheet is GENERATED. Do not edit it -- edits are overwritten.",
        "",
        "Source of truth   docs/logs/bugs.yaml",
        "Generator         tools/buglog.py",
        "Narrative log     docs/logs/BUGS.md  (the long-form detail per bug)",
        "Owner             the `bug-triage` agent",
        "",
        "Regenerate:   python3 tools/buglog.py",
        "CI gate:      python3 tools/buglog.py --check",
        "",
        "--check fails the build if:",
        "  · a bug is in the ledger but not in BUGS.md, or the reverse",
        "  · a `locations` file does not exist in the repo",
        "  · a bug is marked fixed but names no regression test",
        "  · a named regression test function does not exist in the test file",
        "  · a fixed bug's test is neither file::function nor the literal",
        "    'docs-only (no mechanical guard)' with only documentation locations",
        "  (the two existence checks are skipped for an entry carrying",
        "   removed_with_module: its files went with a removed health module)",
        "",
        "That last one is the point. 'Fixed' without a test that fails against",
        "the old code is a claim, not a fix -- and five of the bugs in this log",
        "were found in code that was 66/66 green.",
    ]):
        c = src.cell(row=i + 1, column=1, value=line)
        c.font = Font(name="Arial", size=10,
                      bold=line.startswith("This spreadsheet"))
    src.column_dimensions["A"].width = 78

    SHEET.parent.mkdir(parents=True, exist_ok=True)
    wb.save(SHEET)


# ---------------------------------------------------------------------------
REGISTRY = ROOT / "config" / "health" / "modules.yaml"
_MODULE_ID = re.compile(r"^[a-z][a-z0-9-]*$")

#: The one regression_test value that is not a test: a fix to documentation only, with
#: every location in documentation (see `doc_only_location`).
DOCS_ONLY = "docs-only (no mechanical guard)"
#: Fixed entries logged before rule (3) was strict, whose regression_test is prose naming
#: a CI step. Closed: an entry joins only by editing this gate, in review. Rewriting one to
#: `file::function` removes it from here.
PRE_STRICT_PROSE_TESTS = frozenset({
    "BUG-20260909-004",   # "CI itself (ruff check src tests tools)"
    "BUG-20260909-019",   # "CI itself" (the pytest step)
    "BUG-20260909-034",   # "CI itself -- the deprecation annotation disappears"
    "BUG-20260909-038",   # "tests/selftest.py itself -- the summary line ..."
})
_DOC_SUFFIXES = frozenset({".md", ".mmd", ".svg"})


def doc_only_location(loc: dict) -> bool:
    """A location that is documentation: under docs/, a .md/.mmd/.svg file, or a comment
    line (`#`) of a .yaml/.yml file -- the line read from the file, so a missing file or a
    line of configuration is not documentation."""
    file = str(loc.get("file", ""))
    suffix = pathlib.PurePosixPath(file).suffix.lower()
    if file.startswith("docs/") or suffix in _DOC_SUFFIXES:
        return True
    if suffix not in (".yaml", ".yml"):
        return False
    try:
        lines = (ROOT / file).read_text(encoding="utf-8").splitlines()
        line = int(loc.get("line", 0))
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        return False
    return 1 <= line <= len(lines) and lines[line - 1].lstrip().startswith("#")


def registered_modules() -> set[str]:
    """The ids in the health module registry; empty when there is none (the health
    subsystem's `process` module removed)."""
    return {str(m.get("id")) for m in _registry_entries()}


def _registry_entries() -> list[dict]:
    if not REGISTRY.exists():
        return []
    import yaml
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    return [m for m in data.get("modules") or [] if isinstance(m, dict)]


def _removed_with(bug: dict) -> list:
    v = bug.get("removed_with_module")
    if v is None:
        return []
    return list(v) if isinstance(v, list) else [v]


def _refs(bug: dict) -> list[str]:
    """Every file and `file::function` an entry cites (locations, then regression tests)."""
    rt = bug.get("regression_test")
    tests = [rt] if isinstance(rt, str) else list(rt or [])
    return ([str(loc.get("file", "")) for loc in bug.get("locations") or []]
            + [str(t) for t in tests])


def _in_scope(ref: str, paths: list[str], tests: list[str]) -> bool:
    """`ref` (a file or `file::function`) is one of a module's tests or under its paths."""
    if ref in tests:
        return True
    file = ref.split("::", 1)[0]
    return any(file == p.rstrip("/") or file.startswith(p.rstrip("/") + "/") for p in paths)


def mark_removed(text: str, bugs: list[dict], module_id: str, paths: list[str],
                 tests: list[str]) -> tuple[str, list[str]]:
    """`text` (bugs.yaml) with `removed_with_module: <module_id>` added to every entry that
    cites a file under `paths` or a test in `tests` and carries no such field yet, and the
    ids it marked. The line goes right after the entry's `status:` line; nothing else in
    the file changes (the ledger is append-only)."""
    marked = [b["id"] for b in bugs if not _removed_with(b)
              and any(_in_scope(r, paths, tests) for r in _refs(b))]
    out: list[str] = []
    done: list[str] = []
    pending = False
    for line in text.splitlines(keepends=True):
        m = re.match(r"^- id: (BUG-\d{8}-\d{3})\s*$", line)
        if m:
            pending = m.group(1) in marked and m.group(1) not in done
            current = m.group(1)
        out.append(line)
        if pending and re.match(r"^  status:", line):
            out.append(f"  removed_with_module: {module_id}\n")
            done.append(current)
            pending = False
    if sorted(done) != sorted(marked):
        raise SystemExit(f"--mark-removed: no `status:` line found for "
                         f"{sorted(set(marked) - set(done))}")
    return "".join(out), marked


def repo_files() -> list[pathlib.Path]:
    """The files rule (4) reads: git-tracked files under ROOT (`git ls-files -z`). Only
    when git is missing or ROOT is not a checkout, every file under ROOT (the old walk).
    Ignored and untracked paths -- builders' worktrees under .claude/worktrees/, scratch
    files -- are not the repository and never count."""
    import subprocess
    try:
        p = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                           capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        p = None
    if p is not None and p.returncode == 0:
        names = p.stdout.decode("utf-8", "surrogateescape").split("\0")
        return [ROOT / n for n in names if n]
    return list(ROOT.rglob("*"))


def check(data: dict, registered: set[str] | None = None) -> list[str]:
    """Every rule in the module docstring. `registered` is the set of module ids in the
    health registry (read from config/health/modules.yaml when None)."""
    if registered is None:
        registered = registered_modules()
    problems: list[str] = []
    md = MARKDOWN.read_text() if MARKDOWN.exists() else ""
    # (0) A DUPLICATE ID is the one defect that makes every other entry unciteable:
    # a bug id is a name, and two entries under one name means "BUG-20260910-059"
    # no longer identifies anything. This used to be a set comprehension, so two
    # entries collapsed into one and the gate stayed green -- which is exactly what
    # happens when two branches allocate the same number, and two branches in this
    # repo are doing that right now with 059-061.
    all_ids = [b["id"] for b in data["bugs"]]
    ledger_ids = set(all_ids)
    dupes = sorted({i for i in all_ids if all_ids.count(i) > 1})
    for dupe in dupes:
        problems.append(
            f"{dupe}: appears {all_ids.count(dupe)} times in bugs.yaml -- a duplicate bug id "
            f"means the id names two different defects, so every reference to it is ambiguous. "
            f"Renumber the one that landed second (the branch that merges second renumbers).")
    md_ids = set(re.findall(r"BUG-\d{8}-\d{3}", md))

    for missing in sorted(ledger_ids - md_ids):
        problems.append(f"{missing}: in bugs.yaml, absent from BUGS.md")
    for missing in sorted(md_ids - ledger_ids):
        problems.append(f"{missing}: in BUGS.md, absent from bugs.yaml (the ledger is "
                        f"the source of truth -- add it there)")

    for bug in data["bugs"]:
        bid = bug["id"]
        removed = _removed_with(bug)
        for mid in removed:
            if not (isinstance(mid, str) and _MODULE_ID.match(mid)):
                problems.append(f"{bid}: removed_with_module {mid!r} is not a module id")
            elif mid in registered:
                problems.append(
                    f"{bid}: removed_with_module {mid} names a module that is still in "
                    f"config/health/modules.yaml -- the field is for files a removal deleted, "
                    f"never a way to hide a live module's broken reference")
        if "removed_with_module" in bug and not removed:
            problems.append(f"{bid}: removed_with_module is empty")
        for loc in bug.get("locations") or []:
            if not removed and not (ROOT / loc["file"]).exists():
                problems.append(f"{bid}: location {loc['file']} does not exist")
        rt = bug.get("regression_test")
        tests = [rt] if isinstance(rt, str) else list(rt or [])
        if bug.get("status") == "fixed" and not tests:
            problems.append(f"{bid}: marked fixed with NO regression test named")
            continue
        fixed = bug.get("status") == "fixed"
        if fixed and not bug.get("resolved_at"):
            problems.append(f"{bid}: marked fixed with no resolved_at")
        docs_only = False
        for one in tests:
            if one == DOCS_ONLY:
                docs_only = True
                continue
            if not isinstance(one, str) or "::" not in one:
                if fixed and bid not in PRE_STRICT_PROSE_TESTS:
                    problems.append(
                        f"{bid}: regression test {one!r} is neither `file::function` nor the "
                        f"literal {DOCS_ONLY!r} -- the gate cannot tell a placeholder from a "
                        f"test")
                continue
            if removed:
                continue      # its files went with the module (the shape rule above holds)
            path, fn = one.split("::", 1)
            p = ROOT / path
            if not p.exists():
                problems.append(f"{bid}: regression test file {path} does not exist")
            elif f"def {fn}(" not in p.read_text():
                problems.append(
                    f"{bid}: regression test {one} is named but `def {fn}(` is not in "
                    f"{path} -- a fix whose test does not exist is not a fix")
        if fixed and docs_only:
            code = [str(loc.get("file")) for loc in bug.get("locations") or []
                    if not doc_only_location(loc)]
            if code or not bug.get("locations"):
                problems.append(
                    f"{bid}: regression test is {DOCS_ONLY!r} but "
                    + (f"{', '.join(code)} {'is' if len(code) == 1 else 'are'} not "
                       f"documentation" if code else "it names no location")
                    + " -- a fix to code or configuration needs a test")
    # (4) V15: pyproject.toml cited BUG-20260909-011, which did not exist.
    # A reference to a bug id nobody can look up is worse than no reference.
    referenced: dict[str, set[str]] = {}
    skip = {".git", "__pycache__", ".venv", "node_modules"}
    for f in repo_files():
        if not f.is_file() or f.suffix.lower() in {".xlsx", ".zst", ".gz", ".db", ".pyc"}:
            continue
        if any(part in skip for part in f.relative_to(ROOT).parts):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in set(re.findall(r"BUG-\d{8}-\d{3}", text)):
            referenced.setdefault(m, set()).add(str(f.relative_to(ROOT)))
    for ref, where in sorted(referenced.items()):
        if ref not in ledger_ids:
            problems.append(
                f"{ref}: referenced in {', '.join(sorted(where)[:3])} but not in "
                f"bugs.yaml -- a bug id nobody can look up is worse than none")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="validate the ledger against the repo; write nothing")
    ap.add_argument("--mark-removed", metavar="MODULE_ID",
                    help="add removed_with_module: MODULE_ID to every entry citing that "
                         "health module's paths or tests (a removal recipe step; run it "
                         "while the module is still in config/health/modules.yaml)")
    a = ap.parse_args()
    data = load()
    if a.mark_removed:
        entry = next((m for m in _registry_entries() if m.get("id") == a.mark_removed), None)
        if entry is None:
            print(f"--mark-removed: {a.mark_removed!r} is not in config/health/modules.yaml "
                  "(mark the ledger before removing the entry)", file=sys.stderr)
            return 1
        text, marked = mark_removed(LEDGER.read_text(encoding="utf-8"), data["bugs"],
                                    a.mark_removed, list(entry.get("paths") or []),
                                    list(entry.get("tests") or []))
        LEDGER.write_text(text, encoding="utf-8")
        print(f"removed_with_module: {a.mark_removed} added to {len(marked)} entr"
              f"{'y' if len(marked) == 1 else 'ies'}: {', '.join(marked) or '(none)'}")
        return 0

    problems = check(data)
    if problems:
        print("BUG LEDGER CHECK FAILED", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        if a.check:
            return 1
    else:
        print(f"ledger check clean: {len(data['bugs'])} bugs, "
              f"every fixed one names a regression test that exists")

    if a.check:
        return 0
    build(data)
    print(f"wrote {SHEET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
