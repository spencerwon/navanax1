#!/bin/bash
# Navanax IMPORT TRAITS — double-click to load a trait cache another tool
# already pulled. It asks you for nothing and it spends ZERO of the 120
# reads/hour OpenSea budget: everything it needs is already on this disk.
#
# What it is for: the Explorer tool writes a `tokens.json` holding every
# token's traits, plus a `summary.json` next to it saying WHEN it pulled them.
# That generated time is what goes into `traits_at` — the traits were observed
# then, not now, and claiming now would be a lie the store cannot detect later.
#
# What it will NOT do:
#   * overwrite a trait this store already recorded from another source. Where
#     the two disagree it prints both and exits non-zero, and you decide;
#   * import a token id the cache gives two different sets of traits for. A
#     cache that contradicts itself is evidence about the cache, not a record;
#   * believe a generated time in the future or before 2020 — it refuses, and
#     writes nothing at all rather than stamping a plausible substitute.
#
# Afterwards it prints how many of the collection's tokens now have traits,
# which standing order criteria match no trait value, and any two spellings
# that differ only by case ("Cloak" vs "cloak") — those are two different
# values here, so a filter on one silently matches none of the other.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }

echo "============================================================"
echo "  NAVANAX -- import a trait cache (no OpenSea reads at all)"
echo "============================================================"
echo

# Where the cache might be. In order:
#   1. traits.explorer_cache_path in config/base.yaml (or the active env file),
#      which is where it belongs once the Operator has settled on a location;
#   2. a cache/ folder beside this script, and one level inside it;
#   3. data/cache/, for a cache dropped next to the store.
# Every path considered is printed if none of them exists — "cache not found"
# without saying where you looked is a message that cannot be acted on.
CACHE="$("$PY" - <<'PYEOF'
import sys
from pathlib import Path

root = Path.cwd()
looked, found = [], None


def cfg_path():
    try:
        import yaml
    except ImportError:
        return None
    base = root / "config" / "base.yaml"
    if not base.exists():
        return None
    try:
        cfg = yaml.safe_load(base.read_text()) or {}
    except Exception:
        return None
    env = cfg.get("environment", "local")
    envp = root / "config" / f"{env}.yaml"
    if envp.exists():
        try:
            over = (yaml.safe_load(envp.read_text()) or {}).get("traits") or {}
        except Exception:
            over = {}
        if over.get("explorer_cache_path"):
            return over["explorer_cache_path"]
    return (cfg.get("traits") or {}).get("explorer_cache_path")


candidates = []
recorded = cfg_path()
if recorded:
    candidates.append(Path(recorded).expanduser())
candidates += [root / "cache" / "tokens.json", root / "data" / "cache" / "tokens.json"]
for parent in (root / "cache", root / "data" / "cache"):
    if parent.is_dir():
        candidates += sorted(parent.glob("*/tokens.json"))

for c in candidates:
    looked.append(str(c))
    if found is None and c.is_file():
        found = c

if found:
    print(found)
    sys.exit(0)
print("NOT_FOUND")
print(f"config recorded a path: {recorded}" if recorded
      else "config/base.yaml records no traits.explorer_cache_path")
for path in looked:
    print(f"  looked in {path}")
sys.exit(3)
PYEOF
)"

if [ "${CACHE%%$'\n'*}" = "NOT_FOUND" ]; then
  echo "No trait cache found. Here is every place I looked in:"
  echo "${CACHE#NOT_FOUND$'\n'}"
  echo
  echo "Put the Explorer tool's tokens.json (and the summary.json beside it,"
  echo "which carries the time the traits were pulled) in a folder called"
  echo "cache/ next to this script, or record its location in config/base.yaml"
  echo "under:"
  echo
  echo "    traits:"
  echo "      explorer_cache_path: /full/path/to/tokens.json"
  echo
  read -r -p "Press Enter to close. "
  exit 2
fi

if ! "$PY" -c 'import navanax' 2>/dev/null; then
  echo "navanax is not installed for $PY. Double-click setup.command first."
  read -r -p "Press Enter to close. "
  exit 2
fi

echo "cache        $CACHE"
echo "reads spent  0 -- nothing here touches the OpenSea budget"
echo
"$PY" -m navanax.cli import-traits "$CACHE"
RC=$?
echo
case $RC in
  0) echo "Imported cleanly." ;;
  1) echo "Imported, but the lines marked *** above are DISAGREEMENTS: the cache and"
     echo "this store do not tell the same story about those tokens, or the cache"
     echo "contradicts itself. Nothing was overwritten. Read them before trusting either." ;;
  2) echo "Refused before writing anything. The reason is printed above." ;;
  *) echo "Stopped with code $RC." ;;
esac
read -r -p "Press Enter to close. "
