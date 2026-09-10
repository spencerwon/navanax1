#!/usr/bin/env python3
"""The gate a branch must pass before it is allowed to reach the remote.

    python3 tools/pushgate.py                 # check the current branch
    python3 tools/pushgate.py --branch X      # check a named branch (must be HEAD)
    python3 tools/pushgate.py --json          # same result, machine-readable
    python3 tools/pushgate.py --allow-tester  # permit the shared `tester` branch

Exit 0 only if EVERY check passes. Exit 1 otherwise, with a numbered report
saying what failed and what to do about it.

WHY THIS TOOL DOES NOT PUSH
---------------------------
It cannot, and that is deliberate. Two separate facts force the split:

  1. The Linux VM the agents run in on Spencer's Mac cannot reach GitHub --
     the corporate proxy refuses the tunnel (`403 from proxy after CONNECT`).
     No agent in this project can push, with or without a credential.
  2. Spencer approves every pull request (`docs/02_AGENT_HIERARCHY.md` §2).
     There is no auto-merge here and any mechanism that would create one is a
     defect to be removed.

So the work is split at the only place it can be split: **the agent prepares
and verifies, the Operator's click performs.** This file is the preparing and
verifying half. `push.command` -- which runs on macOS, where GitHub Desktop
already holds his credentials -- is the performing half, and it will not run
until this exits 0 and he types PUSH.

The click is a control, not an inconvenience. Do not "fix" this by adding a
token or the `gh` CLI. There is no token in this project and there must not be.

WHAT IT CHECKS, cheapest and most structural first
--------------------------------------------------
  1. HEAD is a pushable branch (not `main`, not detached, not `tester`).
  2. The working tree is clean.
  3. Nothing forbidden is tracked (data/, *.tgz, *.zip, *.patch, .sync/).
  4. docs/gates/<branch>.yaml records the required sign-offs, bound to this
     exact commit sha. That file is gitignored on purpose: it names the HEAD
     sha it applies to, and committing it would itself move HEAD, so a tracked
     record could never satisfy its own binding.
  5. tools/secrets_check.py is clean.
  6. tools/buglog.py --check is clean.
  7. ruff check src tests tools (SKIPPED with a warning if ruff is absent --
     the Mac may not have it; CI runs it as a hard failure).
  8. tests/selftest.py and tests/validator_probe.py are green.

Standard library only. `push.command` runs this, and a launcher must never
depend on a package the Operator might not have installed -- which is also why
check 4 reads YAML with the small subset parser below instead of PyYAML.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]

PROTECTED_BRANCHES = {"main", "master"}
SHARED_BRANCHES = {"tester"}

#: Shipped default. `config/base.yaml` -> `gates.required_roles` overrides it.
DEFAULT_REQUIRED_ROLES = ["tech-lead", "pm", "validator"]

VERDICTS = {"APPROVE", "APPROVE-WITH-FIXES", "REJECT"}
APPROVED = "APPROVE"

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# ===========================================================================
# A very small YAML subset reader.
#
# Deliberately not PyYAML. `push.command` is a double-clicked launcher and a
# launcher that dies on `ModuleNotFoundError` teaches the Operator that the
# gate is flaky, which is how a gate stops being run at all. The repo does have
# PyYAML for the application; this path must work without it.
#
# The subset is exactly what a sign-off file needs: nested mappings by
# indentation, lists of mappings, quoted or bare scalars, `#` comments.
# Everything outside that subset is a hard error rather than a guess -- a
# sign-off file that parses to something other than what it looks like is the
# worst possible failure mode for this particular file.
# ===========================================================================
class GateYamlError(ValueError):
    """The sign-off file is not in the subset the gate can read."""


def _strip_comment(line: str) -> str:
    """Remove a trailing `#` comment, respecting quotes. Raise on an open quote."""
    out: list[str] = []
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        else:
            out.append(ch)
    if quote:
        raise GateYamlError("unterminated quoted string")
    return "".join(out).rstrip()


def _tokenize(text: str) -> list[tuple[int, str, int]]:
    """-> [(indent, body, line_number)], comments and blank lines dropped."""
    toks: list[tuple[int, str, int]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        lead = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in lead:
            raise GateYamlError(f"line {lineno}: tab used for indentation, use spaces")
        try:
            body = _strip_comment(raw)
        except GateYamlError as exc:
            raise GateYamlError(f"line {lineno}: {exc}") from None
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip(" "))
        toks.append((indent, body.strip(), lineno))
    return toks


def _scalar(value: str, lineno: int):
    if value in ("~", "null"):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if value[0] in "\"'":
        raise GateYamlError(f"line {lineno}: unterminated quoted string")
    if value[0] in "[{":
        raise GateYamlError(
            f"line {lineno}: flow style ({value[0]}...) is not supported; "
            "write the list one `- item` per line"
        )
    return value


def _parse_map(toks, i: int, indent: int):
    out: dict = {}
    while i < len(toks) and toks[i][0] == indent:
        _, body, lineno = toks[i]
        if body.startswith("-"):
            raise GateYamlError(f"line {lineno}: list item where a `key: value` was expected")
        if ":" not in body:
            raise GateYamlError(f"line {lineno}: expected `key: value`, got {body!r}")
        key, _, value = body.partition(":")
        key, value = key.strip(), value.strip()
        if not key:
            raise GateYamlError(f"line {lineno}: empty key")
        if key in out:
            raise GateYamlError(f"line {lineno}: duplicate key {key!r}")
        i += 1
        if value:
            out[key] = _scalar(value, lineno)
        elif i < len(toks) and toks[i][0] > indent:
            out[key], i = _parse_block(toks, i, toks[i][0])
        else:
            out[key] = None
    if i < len(toks) and toks[i][0] > indent:
        raise GateYamlError(f"line {toks[i][2]}: unexpected indentation")
    return out, i


def _parse_list(toks, i: int, indent: int):
    out: list = []
    while i < len(toks) and toks[i][0] == indent and toks[i][1].startswith("-"):
        _, body, lineno = toks[i]
        if body != "-" and not body.startswith("- "):
            raise GateYamlError(f"line {lineno}: expected `- item`, got {body!r}")
        rest = "" if body == "-" else body[2:].strip()
        i += 1
        if not rest:
            if i < len(toks) and toks[i][0] > indent:
                value, i = _parse_block(toks, i, toks[i][0])
                out.append(value)
            else:
                out.append(None)
            continue
        if ":" in rest and rest[0] not in "\"'":
            # `- key: value`, possibly with sibling keys on the lines below,
            # aligned under the key rather than under the dash.
            key_col = indent + (len(body) - len(body[2:].lstrip())) if body != "-" else indent + 2
            j = i
            while j < len(toks) and toks[j][0] >= key_col:
                j += 1
            sub = [(key_col, rest, lineno)] + toks[i:j]
            value, _ = _parse_map(sub, 0, key_col)
            out.append(value)
            i = j
        else:
            out.append(_scalar(rest, lineno))
    if i < len(toks) and toks[i][0] > indent:
        raise GateYamlError(f"line {toks[i][2]}: unexpected indentation")
    return out, i


def _parse_block(toks, i: int, indent: int):
    if toks[i][1].startswith("-"):
        return _parse_list(toks, i, indent)
    return _parse_map(toks, i, indent)


def parse_yaml(text: str):
    """Parse the YAML subset. Raise GateYamlError on anything outside it."""
    toks = _tokenize(text)
    if not toks:
        return {}
    if toks[0][0] != 0:
        raise GateYamlError(f"line {toks[0][2]}: document must start at column 0")
    value, i = _parse_block(toks, 0, 0)
    if i != len(toks):
        raise GateYamlError(f"line {toks[i][2]}: unexpected content {toks[i][1]!r}")
    return value


# ===========================================================================
# Sign-off records
# ===========================================================================
def gate_file_for(branch: str) -> pathlib.Path:
    """docs/gates/<branch with slashes as dashes>.yaml"""
    return ROOT / "docs" / "gates" / (branch.replace("/", "-") + ".yaml")


def required_roles(config_path: pathlib.Path | None = None) -> list[str]:
    """Read `gates.required_roles` out of config/base.yaml.

    Only the `gates:` block is sliced out and parsed, so an unrelated addition
    elsewhere in base.yaml (a flow-style list, say) cannot break the gate.
    """
    path = config_path if config_path is not None else ROOT / "config" / "base.yaml"
    if not path.exists():
        return list(DEFAULT_REQUIRED_ROLES)
    lines = path.read_text(encoding="utf-8").splitlines()
    block: list[str] = []
    inside = False
    for line in lines:
        if inside:
            if line.strip() and not line.startswith((" ", "\t")):
                break
            block.append(line)
        elif line.startswith("gates:"):
            inside = True
            block.append(line)
    if not block:
        return list(DEFAULT_REQUIRED_ROLES)
    doc = parse_yaml("\n".join(block))
    roles = (doc.get("gates") or {}).get("required_roles")
    if not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles):
        raise GateYamlError(
            "config/base.yaml: gates.required_roles must be a non-empty list of role names"
        )
    return roles


def _parse_at(value, role: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GateYamlError(f"sign-off from {role!r}: `at` is missing")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise GateYamlError(
            f"sign-off from {role!r}: `at` is not an ISO-8601 timestamp: {value!r}"
        ) from None
    if stamp.tzinfo is None:
        raise GateYamlError(
            f"sign-off from {role!r}: `at` has no timezone offset ({value!r}); "
            "write it as 2026-09-10T09:12:00-05:00"
        )
    return stamp


def validate_signoffs(
    doc,
    branch: str,
    head_sha: str,
    roles_required,
    role_defined=None,
    now: datetime | None = None,
) -> list[str]:
    """-> a list of problems. Empty means the record is present and current."""
    if role_defined is None:
        def role_defined(role: str) -> bool:  # noqa: E306
            return (ROOT / ".claude" / "agents" / f"{role}.md").exists()
    now = now or datetime.now(timezone.utc)
    problems: list[str] = []

    if not isinstance(doc, dict):
        return ["the sign-off file is not a mapping (`branch:`/`commit:`/`signoffs:`)"]

    recorded_branch = doc.get("branch")
    if recorded_branch != branch:
        problems.append(
            f"`branch:` says {recorded_branch!r} but HEAD is on {branch!r}"
        )

    commit = doc.get("commit")
    if not isinstance(commit, str) or not SHA_RE.match(commit.strip().lower()):
        problems.append(f"`commit:` is not a full 40-character sha: {commit!r}")
    elif commit.strip().lower() != head_sha.lower():
        problems.append(
            f"`commit:` is {commit.strip()[:12]} but HEAD is {head_sha[:12]} -- the "
            "sign-offs were given for a different commit. A new commit needs new "
            "sign-offs; that binding is the whole mechanism."
        )

    signoffs = doc.get("signoffs")
    if not isinstance(signoffs, list) or not signoffs:
        problems.append("`signoffs:` is missing or empty")
        return problems

    seen: dict[str, int] = {}
    approved: set[str] = set()
    for n, entry in enumerate(signoffs, 1):
        if not isinstance(entry, dict):
            problems.append(f"sign-off #{n} is not a mapping")
            continue
        role = entry.get("role")
        if not isinstance(role, str) or not role.strip():
            problems.append(f"sign-off #{n} has no `role:`")
            continue
        role = role.strip()
        if role in seen:
            problems.append(
                f"role {role!r} appears twice (sign-offs #{seen[role]} and #{n}) -- "
                "one role, one verdict"
            )
            continue
        seen[role] = n
        if not role_defined(role):
            problems.append(
                f"role {role!r} has no charter at .claude/agents/{role}.md -- a "
                "sign-off from a role the repo does not define is not a sign-off"
            )
        verdict = entry.get("verdict")
        if verdict not in VERDICTS:
            problems.append(
                f"sign-off from {role!r}: verdict {verdict!r} is not one of "
                + ", ".join(sorted(VERDICTS))
            )
        try:
            stamp = _parse_at(entry.get("at"), role)
        except GateYamlError as exc:
            problems.append(str(exc))
        else:
            if stamp > now:
                problems.append(
                    f"sign-off from {role!r} is dated {entry.get('at')}, which is in "
                    "the future -- check the clock, or the record was written ahead of "
                    "the review"
                )
        if verdict == APPROVED:
            approved.add(role)
        elif verdict == "APPROVE-WITH-FIXES":
            problems.append(
                f"sign-off from {role!r} is APPROVE-WITH-FIXES, which is not approval. "
                "Apply the fixes, commit, and get the role to sign the new commit."
            )
        elif verdict == "REJECT":
            problems.append(f"sign-off from {role!r} is REJECT")

    for role in roles_required:
        if role not in approved:
            problems.append(
                f"no APPROVE recorded from required role {role!r}"
            )
    return problems


# ===========================================================================
# Forbidden tracked paths
# ===========================================================================
def forbidden_reason(path: str) -> str:
    """-> why this path must never be tracked, or "" if it is fine."""
    lower = path.lower()
    if path == "data" or path.startswith("data/"):
        return "the landing zone and the stores are irreplaceable data, not source code"
    if lower.endswith((".tgz", ".zip")):
        return "an archive in git bloats the repo and hides its own contents from review"
    if lower.endswith(".patch"):
        return "a patch file is a copy of a diff git already has"
    if path == ".sync" or path.startswith(".sync/"):
        return ".sync/ is a transfer scratch directory, never history"
    return ""


def forbidden_tracked(paths) -> list[tuple[str, str]]:
    """-> [(path, reason)] for every tracked path that must not be tracked."""
    hits = []
    for path in paths:
        reason = forbidden_reason(path)
        if reason:
            hits.append((path, reason))
    return hits


# ===========================================================================
# git and subprocess helpers
# ===========================================================================
def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True
    )


def git_out(*args: str) -> str:
    proc = git(*args)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def run_step(cmd: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output


def _tail(text: str, n: int = 25) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


# ===========================================================================
# The gate
# ===========================================================================
class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> None:
        self.checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if c["status"] == "FAIL"]

    def render(self) -> str:
        out = []
        for n, c in enumerate(self.checks, 1):
            out.append(f"  {n}. {c['status']:<7} {c['name']}")
            if c["detail"]:
                for line in c["detail"].splitlines():
                    out.append(f"          {line}")
            if c["status"] in ("FAIL", "SKIPPED") and c["fix"]:
                out.append(f"          -> {c['fix']}")
        return "\n".join(out)


def push_range(branch: str) -> tuple[str, str]:
    """-> (rev-range, one-line explanation of why that range)."""
    if git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}").returncode == 0:
        return f"origin/{branch}..HEAD", f"commits not yet on origin/{branch}"
    upstream = git_out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream:
        return f"{upstream}..HEAD", (
            f"the branch is new on the remote; these are the commits it adds to {upstream}"
        )
    base = git_out("merge-base", "origin/main", "HEAD")
    if base:
        return f"{base}..HEAD", "the branch is new on the remote and has no upstream; measured from origin/main"
    return "HEAD~10..HEAD", "no upstream and no origin/main; showing the last 10 commits"


def collect(branch_arg: str | None, allow_tester: bool) -> dict:
    report = Report()
    result: dict = {"ok": False, "branch": None, "head": None, "checks": report.checks}

    # --- 1. a pushable branch -------------------------------------------
    branch = git_out("rev-parse", "--abbrev-ref", "HEAD")
    head = git_out("rev-parse", "HEAD")
    result["branch"], result["head"] = branch or None, head or None
    if not branch or branch == "HEAD":
        report.add(
            "on a pushable branch", "FAIL",
            "HEAD is detached -- it is not on a branch at all.",
            "git switch -c feat/<short-name> to put this work on a branch.",
        )
    elif branch in PROTECTED_BRANCHES:
        report.add(
            "on a pushable branch", "FAIL",
            f"HEAD is on {branch!r}, which is protected. No direct commits, ever "
            "(docs/04_ENVIRONMENTS.md §4).",
            "Move the work to a feat/ or fix/ branch and re-run.",
        )
    elif branch in SHARED_BRANCHES and not allow_tester:
        report.add(
            "on a pushable branch", "FAIL",
            f"{branch!r} is the shared integration branch. Pushing to it is possible "
            "but should be a decision, not a default.",
            "Re-run with --allow-tester if that is genuinely what is intended.",
        )
    elif branch_arg and branch_arg != branch:
        report.add(
            "on a pushable branch", "FAIL",
            f"--branch says {branch_arg!r} but HEAD is on {branch!r}.",
            "Check out the branch you mean to push, then re-run.",
        )
    else:
        note = " (shared branch, allowed by --allow-tester)" if branch in SHARED_BRANCHES else ""
        report.add("on a pushable branch", "PASS", f"{branch} @ {head[:12]}{note}")

    # --- 2. clean working tree ------------------------------------------
    status = git_out("status", "--porcelain")
    if status:
        report.add(
            "working tree clean", "FAIL",
            "uncommitted changes:\n" + _tail(status, 20),
            "Commit them (docs/04_ENVIRONMENTS.md §4.1 for the message shape) or "
            "stash them. What is not committed does not get pushed, so a dirty tree "
            "means the sign-offs describe something other than what would land.",
        )
    else:
        report.add("working tree clean", "PASS", "nothing modified, staged, or untracked")

    # --- 3. nothing forbidden is tracked --------------------------------
    tracked = [p for p in git_out("ls-files").splitlines() if p]
    hits = forbidden_tracked(tracked)
    if hits:
        lines = [f"{p}  -- {why}" for p, why in hits[:40]]
        if len(hits) > 40:
            lines.append(f"... and {len(hits) - 40} more")
        report.add(
            "nothing forbidden is tracked", "FAIL",
            f"{len(hits)} tracked path(s) that must never be in git:\n" + "\n".join(lines),
            "git rm --cached <path> for each, add it to .gitignore, and commit. "
            "(Commit 9932f9f committed .sync/*.tgz and a 2,125-line .patch; this "
            "check exists so that cannot recur.)",
        )
    else:
        report.add(
            "nothing forbidden is tracked", "PASS",
            f"{len(tracked)} tracked files, none under data/ or .sync/, no archives or patches",
        )

    # --- 4. sign-off record present and current -------------------------
    gate_path = gate_file_for(branch or "HEAD")
    rel = gate_path.relative_to(ROOT).as_posix()
    try:
        roles = required_roles()
    except GateYamlError as exc:
        roles = list(DEFAULT_REQUIRED_ROLES)
        report.add(
            "sign-off record present and current", "FAIL", str(exc),
            "Fix config/base.yaml's gates.required_roles block.",
        )
    else:
        result["required_roles"] = roles
        if not gate_path.exists():
            report.add(
                "sign-off record present and current", "FAIL",
                f"{rel} does not exist. Required roles: {', '.join(roles)}.",
                f"Ask each role to review. When one gives a verdict, record it in {rel} "
                "with the sha it applies to. See docs/gates/README.md for the format. "
                "Never write a sign-off a role did not give.",
            )
        else:
            try:
                doc = parse_yaml(gate_path.read_text(encoding="utf-8"))
            except GateYamlError as exc:
                report.add(
                    "sign-off record present and current", "FAIL",
                    f"{rel} does not parse: {exc}",
                    "See docs/gates/README.md for the exact shape the gate reads.",
                )
            else:
                problems = validate_signoffs(doc, branch, head, roles)
                if problems:
                    report.add(
                        "sign-off record present and current", "FAIL",
                        f"{rel}:\n" + "\n".join("- " + p for p in problems),
                        "Every one of these must be resolved by a real review, not by "
                        "editing the file.",
                    )
                else:
                    got = ", ".join(
                        str(s.get("role")) for s in doc["signoffs"]
                    )
                    report.add(
                        "sign-off record present and current", "PASS",
                        f"{rel}: APPROVE from {got}, bound to {head[:12]}",
                    )

    # --- 5. secrets ------------------------------------------------------
    ok, out = run_step([sys.executable, "tools/secrets_check.py"])
    report.add(
        "no credential in the repository", "PASS" if ok else "FAIL",
        _tail(out, 15),
        "REQ-N-11. Remove the credential, rotate it, and rewrite the commit that "
        "introduced it before anything reaches the remote.",
    )

    # --- 6. bug ledger ---------------------------------------------------
    ok, out = run_step([sys.executable, "tools/buglog.py", "--check"])
    report.add(
        "bug ledger consistent", "PASS" if ok else "FAIL",
        _tail(out, 15),
        "Every fixed bug must name a regression test that exists. "
        "Fix docs/logs/bugs.yaml or add the missing test.",
    )

    # --- 7. ruff ---------------------------------------------------------
    if shutil.which("ruff") is None:
        report.add(
            "ruff check src tests tools", "SKIPPED",
            "ruff is not installed on this machine.",
            "Not a failure here, but CI runs ruff as a hard gate -- if it is unhappy "
            "the PR will go red after the push. `pip install ruff` to see it first.",
        )
    else:
        ok, out = run_step(["ruff", "check", "src", "tests", "tools"])
        report.add(
            "ruff check src tests tools", "PASS" if ok else "FAIL",
            _tail(out, 25), "Fix the lint findings; CI fails on these too.",
        )

    # --- 8. the test suites ---------------------------------------------
    for rel_test in ("tests/selftest.py", "tests/validator_probe.py"):
        path = ROOT / rel_test
        if not path.exists():
            report.add(
                f"python3 {rel_test}", "SKIPPED",
                f"{rel_test} does not exist on this branch.",
                "Expected on a branch that predates the file. If this branch should "
                "have it, the branch is missing a merge from main.",
            )
            continue
        ok, out = run_step([sys.executable, rel_test])
        report.add(
            f"python3 {rel_test}", "PASS" if ok else "FAIL",
            _tail(out, 20), "The suite must be green before anything is pushed.",
        )

    result["ok"] = not report.failed

    # --- the summary the Operator reads before typing PUSH ---------------
    if result["ok"] and branch:
        rev_range, why = push_range(branch)
        commits = git_out("log", "--oneline", rev_range)
        files = git_out("diff", "--name-only", rev_range)
        upstream = git_out("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        remote_url = git_out("remote", "get-url", "origin")
        result["summary"] = {
            "branch": branch,
            "head": head,
            "upstream": upstream or None,
            "remote": "origin",
            "remote_url": remote_url or None,
            "remote_ref": f"refs/heads/{branch}",
            "range": rev_range,
            "range_reason": why,
            "commits": [ln for ln in commits.splitlines() if ln],
            "files": [ln for ln in files.splitlines() if ln],
        }
    return result


def render_summary(summary: dict) -> str:
    lines = [
        "  Branch        " + summary["branch"],
        "  HEAD          " + summary["head"],
        "  Upstream      " + (summary["upstream"] or "(none yet -- the push will set it)"),
        "  Remote        " + (summary["remote_url"] or "origin"),
        "  Remote ref    origin " + summary["remote_ref"],
        "",
        f"  Commits that would be pushed ({summary['range_reason']}):",
    ]
    lines += ["    " + c for c in summary["commits"]] or ["    (none -- nothing to push)"]
    lines += ["", f"  Files they touch ({len(summary['files'])}):"]
    lines += ["    " + f for f in summary["files"][:60]]
    if len(summary["files"]) > 60:
        lines.append(f"    ... and {len(summary['files']) - 60} more")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Check whether this branch is allowed to go to the remote. "
                    "It never pushes -- push.command does that, under the Operator's click."
    )
    ap.add_argument("--branch", help="the branch you believe you are on; checked against HEAD")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--allow-tester", action="store_true",
                    help="permit the shared `tester` branch")
    args = ap.parse_args(argv)

    result = collect(args.branch, args.allow_tester)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    report = Report()
    report.checks = result["checks"]
    print("=" * 72)
    print("  PUSH GATE -- " + (result["branch"] or "no branch"))
    print("=" * 72)
    print(report.render())
    print("-" * 72)
    if result["ok"]:
        print("  ALL CHECKS PASSED. This branch may go to the remote.")
        print("-" * 72)
        print(render_summary(result["summary"]))
        print("-" * 72)
        print("  Nothing has been pushed. push.command performs the push, and only")
        print("  after you type PUSH. Merging stays yours: you approve the PR.")
    else:
        n = len(report.failed)
        print(f"  {n} CHECK{'S' if n != 1 else ''} FAILED. Nothing will be pushed.")
        print("  Fix what is listed above and run this again.")
    print("=" * 72)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
