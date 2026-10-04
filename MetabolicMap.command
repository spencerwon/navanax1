#!/bin/bash
# Navanax METABOLIC MAP -- double-click to open the Metabolic Map viewer as a desktop app.
#
# The program desktop/MetabolicMap.app runs (module desktop-app, docs/health/07), in a
# Terminal window: it serves reference/metabolic-map-v1/ to this computer only
# (127.0.0.1), opens it in a native window when the optional pywebview is installed and in
# your browser otherwise, and prints the address, the disclaimer and the validation
# status. Ctrl-C, or closing this window, stops it. While the desktop-app flag in
# config/health/modules.yaml is off it refuses to start and says why.
# Educational model -- not medical advice. Not clinically validated.
#
# METABOLIC_MAP_PYTHON=/path/to/python3 picks the interpreter (for example a virtual
# environment with pywebview installed).

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
usable() { "$1" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' >/dev/null 2>&1; }
PY=""
if [ -n "${METABOLIC_MAP_PYTHON:-}" ]; then
  usable "$METABOLIC_MAP_PYTHON" && PY="$METABOLIC_MAP_PYTHON"
else
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$c" >/dev/null 2>&1 && usable "$c" && { PY="$c"; break; }
  done
fi
[ -z "$PY" ] && { echo "No Python 3.10+ found. Install it from python.org."; read -r -p "Press Enter to close. "; exit 1; }
PYTHONPATH=src "$PY" -m health.app "$@"
code=$?
[ "$code" -ne 0 ] && read -r -p "Press Enter to close. "
exit "$code"
