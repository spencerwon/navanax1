#!/bin/bash
# Navanax TRAITS — double-click to load every Argonaut's traits into the store.
#
# PASS 1 -- the collection's token list from OpenSea, ~47 governed reads for
# 9,212 tokens. This is where the traits actually come from now: the list
# response carries each token's `traits`, and they are stored as they arrive
# (traits_source `opensea_nft_list`). Verified for Argonauts on 2026-09-09
# against a second tool's raw pull; BUG-20260909-054, when this field was
# being thrown away and 9,161 per-token reads planned instead.
#
# PASS 2 -- each token's metadata from wherever it is hosted (IPFS/Arweave/HTTP),
# NOT metered by OpenSea. FOR ARGONAUTS THIS PASS DOES NOTHING: every Argonaut
# token's metadata_url is NULL, so there is no URL to fetch. It is here for collections
# that do publish one. Expect it to report 0 attempted on a run where pass 1
# already covered the collection -- that is the pass working, not failing.
#
# FALLBACK -- tokens that neither pass got traits for go to OpenSea's per-token
# endpoint, capped at 50 per run (config traits.opensea_fallback_budget), and
# only where the token's contract is known.
#
# Budget: ~47 reads for the list + up to 50 fallback = up to 97 of the 120/hour,
# more if calls are retried after a 429. It shares the budget with the recorder's
# backfill, so run it when the recorder shows no gaps awaiting backfill.
# Stop any time with Ctrl + C; run again to resume -- failed tokens are retried,
# finished ones are not refetched. It ends by printing how many of the
# collection's tokens now have traits.
#
# Where OpenSea's list value DISAGREES with a trait this store already recorded
# from another source, nothing is overwritten: both are printed and you decide.
#
# ALREADY HAVE A CACHE from the Explorer tool? Double-click import-traits.command
# instead. It loads the same data for ZERO reads.
# The dashboard's trait filters light up as the data arrives.

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null && { PY="$c"; break; }
done
[ -z "$PY" ] && { echo "No Python 3.10+ found."; read -r -p "Enter to close. "; exit 1; }
echo "============================================================"
echo "  NAVANAX -- traits onboarding"
echo "============================================================"
"$PY" -m navanax.cli traits
echo; read -r -p "Done. Press Enter to close. "
