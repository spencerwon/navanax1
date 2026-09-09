#!/usr/bin/env python3
"""REQ-N-11: no credential may be committed to the repository. Ever.

    python3 tools/secrets_check.py            # scan tracked files, exit 1 on a hit
    python3 tools/secrets_check.py PATH...    # scan specific files (tests use this)

BUG-20260909-033. The first version of this gate was a one-line grep in CI
that matched `NAME=<any alphanumeric>`. The first time it ever ran, it fired on
the dotenv regression test writing `OPENSEA_API_KEY=fake_key_value` into a
temp file. A gate that cannot tell a 14-character placeholder from a
32-character key trains people to click past it, which is worse than no gate.

What counts as credential-shaped here, and why:

  * A known credential NAME assigned a value that is LONG ENOUGH to be real.
    OpenSea API keys are 32 characters; GitHub PATs are longer. A value under
    20 characters of key-alphabet is not a key. `fake_key_value` is 14.
  * Any GitHub token prefix (`ghp_`, `github_pat_`, `gho_`, `ghs_`) followed
    by token-shaped characters, regardless of what it is assigned to.
  * Any 12-word-or-longer run of lowercase words next to the word "mnemonic"
    or "seed" -- a wallet seed phrase. No agent may hold one (docs/02 §6.3),
    so one in the repo is an S0 event, not a lint warning.

What is deliberately NOT excluded: tests/. A real key in a test file is a
leak in a test file. The check is made precise instead of made blind.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

CRED_NAMES = r"(?:OPENSEA_API_KEY|API_KEY|PRIVATE_KEY|SECRET_KEY|SEED_PHRASE|MNEMONIC|GITHUB_TOKEN|GH_TOKEN|ACCESS_TOKEN)"
PATTERNS = [
    ("credential assignment",
     re.compile(CRED_NAMES + r"\s*[=:]\s*['\"]?([A-Za-z0-9_\-]{20,})['\"]?")),
    ("github token",
     re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("seed phrase",
     re.compile(r"(?i)(?:mnemonic|seed)[^\n]{0,40}?(?:\b[a-z]{3,8}\b[ ,]+){11,}\b[a-z]{3,8}\b")),
]
SKIP_NAMES = {".env.example"}
SKIP_SUFFIXES = {".xlsx", ".gz", ".zst", ".db", ".pyc", ".png", ".jpg"}


def tracked_files() -> list[pathlib.Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, timeout=30, check=True).stdout.split("\n")
        return [ROOT / f for f in out if f]
    except (OSError, subprocess.SubprocessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def scan(paths: list[pathlib.Path]) -> list[str]:
    hits: list[str] = []
    for f in paths:
        if not f.is_file() or f.name in SKIP_NAMES or f.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for label, rx in PATTERNS:
                if rx.search(line):
                    try:
                        rel = f.relative_to(ROOT)
                    except ValueError:
                        rel = f
                    hits.append(f"{rel}:{i}: {label}")
    return hits


def main(argv: list[str]) -> int:
    paths = [pathlib.Path(a) for a in argv] if argv else tracked_files()
    hits = scan(paths)
    if hits:
        print("REQ-N-11 VIOLATION -- credential-shaped content in tracked files:",
              file=sys.stderr)
        for h in hits:
            print("  " + h, file=sys.stderr)
        print("\nRotate the credential FIRST (a leaked key is compromised the moment "
              "it is pushed, and git history keeps it), then remove it.", file=sys.stderr)
        return 1
    print(f"secrets check clean: {len(paths)} files, nothing credential-shaped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
