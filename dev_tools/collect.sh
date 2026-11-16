#!/usr/bin/env bash
#
# collect.sh: run AV Spex over many packages and keep the results in the developer corpus.
#
# This is the staged form, for packages on a slow or read-only drive: `avspex-dev collect`
# without --stage runs AV Spex on the packages where they are and needs none of this.
# Wraps cycle_avspex_batches.sh (which shuttles packages from the external drives to local
# disk in batches) and, for each package, runs av-spex and copies the sidecars it wrote into
# $AVSPEX_CORPUS/runs/<run_id>/packages/<folder>/ (see corpus.py). The run ID comes from the
# current commit, any uncommitted src/ changes and the effective AV Spex config. So:
#   - re-running with the same code and config resumes, skipping collected packages;
#   - a code or config change starts a new run directory, and the old one is left alone;
#   - editing src/ or the config mid-batch makes the remaining packages fail fast rather
#     than mixing two setups under one ID.
#
# Usage:
#   dev_tools/collect.sh [collect options] [cycle options] SOURCE_DIR ...
#
# Collect options:
#   -c FILE  Import this AV Spex config first (av-spex --import-config FILE). Otherwise the
#            currently saved config is used, exactly as the GUI/CLI left it.
#   -l TEXT  Label for the run, shown by `corpus.py list` (e.g. "6x30 defaults baseline")
#   -g       Also make a whole-file active-area signalstats pass per package (ground truth
#            for period replays; roughly 5-15% of the tape's real time per package)
#   -i       Keep images (thumbnails, border/spectrogram pictures) as well
#
# Cycle options (passed through): -s DIR, -b GB, -r GB, -w, -n. See cycle_avspex_batches.sh -h.
# The staging directory is -s DIR, else $AVSPEX_STAGING, else the one saved by avspex-dev setup.
# -w leaves the original packages untouched (nothing is written back); the corpus still gets
# every sidecar. With -s on the same APFS volume as the sources, staging is a clone: instant
# and free, so a whole drive can be collected without copying it.
# AV Spex arguments after "--" are refused: they change the saved config behind the run ID's
# back. Set the config first (-c, or av-spex -dr ...).
#
# Examples:
#   dev_tools/collect.sh -s ~/staging -l "baseline" /Volumes/DRIVE/vendor_a /Volumes/DRIVE/vendor_b
#   dev_tools/collect.sh -c ~/configs/study.json -g -l "period study" /Volumes/DRIVE/jpc
#
# Packages with the same folder name in two sources must go in separate invocations (the cycle
# script refuses duplicates); they share the run, so the second invocation skips them.

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${COLLECT_PYTHON:-python3}"
# Corpus and staging paths come from one place (settings.py); the environment wins.
eval "$("$PY" "$HERE/corpus.py" env)" || exit 1
STAGING_DIR="${AVSPEX_STAGING:-}"
LABEL=""
CONFIG=""
PASS=()

while [ $# -gt 0 ]; do
    case "$1" in
        -c) CONFIG="$2"; shift 2 ;;
        -l) LABEL="$2"; shift 2 ;;
        -g) export COLLECT_GROUND_TRUTH=1; shift ;;
        -i) export COLLECT_IMAGES=1; shift ;;
        -s) STAGING_DIR="$2"; shift 2 ;;
        -b|-r) PASS+=("$1" "$2"); shift 2 ;;
        -h|--help) sed -n '3,41p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --) echo "collect.sh: av-spex arguments are not allowed; set the config first (-c FILE)" >&2; exit 1 ;;
        *) PASS+=("$1"); shift ;;
    esac
done

if [ -z "$STAGING_DIR" ]; then
    echo "collect.sh: no staging directory. Pass -s DIR, or use 'avspex-dev collect SOURCE...'" >&2
    echo "            to run AV Spex on the packages where they are." >&2
    exit 1
fi
PASS=(-s "$STAGING_DIR" ${PASS[@]+"${PASS[@]}"})

if [ -n "$CONFIG" ]; then
    av-spex --import-config "$CONFIG" >/dev/null || { echo "could not import $CONFIG" >&2; exit 1; }
fi

RUN_ID=$("$PY" "$HERE/corpus.py" init-run ${LABEL:+--label "$LABEL"}) || exit 1
export COLLECT_RUN_ID="$RUN_ID"
echo "Run: $RUN_ID  (corpus: $AVSPEX_CORPUS)"

# The corpus is the record of what is done; make the cycle script's done list agree with it.
STATE="${STAGING_DIR%/}/.avspex_cycle/$RUN_ID"
mkdir -p "$STATE"
touch "$STATE/done.txt"
"$PY" "$HERE/corpus.py" done "$RUN_ID" | while read -r name; do
    [ -n "$name" ] && ! grep -qx "$name" "$STATE/done.txt" && echo "$name" >> "$STATE/done.txt"
done

AVSPEX_CMD="$HERE/collect_one.sh" "$HERE/cycle_avspex_batches.sh" -k "$RUN_ID" ${PASS[@]+"${PASS[@]}"}
rc=$?
echo "Run $RUN_ID: $("$PY" "$HERE/corpus.py" done "$RUN_ID" | grep -c .) package(s) collected"
echo "Next: avspex-dev sheet $RUN_ID   (review workbook)   or   avspex-dev table $RUN_ID"
exit $rc
