#!/usr/bin/env python3
"""Render the bug ledger to a spreadsheet, and check it against reality.

    python3 tools/buglog.py            # regenerate docs/logs/BUGS.xlsx
    python3 tools/buglog.py --check    # CI gate, writes nothing

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
`tech-lead` blocks on exactly that.
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
def check(data: dict) -> list[str]:
    problems: list[str] = []
    md = MARKDOWN.read_text() if MARKDOWN.exists() else ""
    ledger_ids = {b["id"] for b in data["bugs"]}
    md_ids = set(re.findall(r"BUG-\d{8}-\d{3}", md))

    for missing in sorted(ledger_ids - md_ids):
        problems.append(f"{missing}: in bugs.yaml, absent from BUGS.md")
    for missing in sorted(md_ids - ledger_ids):
        problems.append(f"{missing}: in BUGS.md, absent from bugs.yaml (the ledger is "
                        f"the source of truth -- add it there)")

    for bug in data["bugs"]:
        bid = bug["id"]
        for loc in bug.get("locations") or []:
            if not (ROOT / loc["file"]).exists():
                problems.append(f"{bid}: location {loc['file']} does not exist")
        rt = bug.get("regression_test")
        if bug.get("status") == "fixed" and not rt:
            problems.append(f"{bid}: marked fixed with NO regression test named")
            continue
        if bug.get("status") == "fixed" and not bug.get("resolved_at"):
            problems.append(f"{bid}: marked fixed with no resolved_at")
        if rt and "::" in rt:
            path, fn = rt.split("::", 1)
            p = ROOT / path
            if not p.exists():
                problems.append(f"{bid}: regression test file {path} does not exist")
            elif f"def {fn}(" not in p.read_text():
                problems.append(
                    f"{bid}: regression test {rt} is named but `def {fn}(` is not in "
                    f"{path} -- a fix whose test does not exist is not a fix")
    # (4) V15: pyproject.toml cited BUG-20260909-011, which did not exist.
    # A reference to a bug id nobody can look up is worse than no reference.
    referenced: dict[str, set[str]] = {}
    skip = {".git", "__pycache__", ".venv", "node_modules"}
    for f in ROOT.rglob("*"):
        if not f.is_file() or f.suffix.lower() in {".xlsx", ".zst", ".gz", ".db", ".pyc"}:
            continue
        if any(part in skip for part in f.parts):
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
    a = ap.parse_args()
    data = load()

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
