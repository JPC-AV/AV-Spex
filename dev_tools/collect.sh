#!/usr/bin/env bash
#
# collect.sh: run AV Spex over many packages and keep the results in the developer corpus.
#
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
#   dev_tools/collect.sh [collect options] [cycle options] [SOURCE_DIR ...]
#
# Collect options:
#   -c FILE  Import this AV Spex config first (av-spex --import-config FILE). Otherwise the
#            currently saved config is used, exactly as the GUI/CLI left it.
#   -l TEXT  Label for the run, shown by `corpus.py list` (e.g. "6x30 defaults baseline")
#   -g       Also make a whole-file active-area signalstats pass per package (ground truth
#            for period replays; roughly 1-2x the tape's real time per package)
#   -i       Keep images (thumbnails, border/spectrogram pictures) as well
#
# Cycle options (passed through): -s DIR, -b GB, -r GB, -n. See cycle_avspex_batches.sh -h.
# AV Spex arguments after "--" are refused: they change the saved config behind the run ID's
# back. Set the config first (-c, or av-spex -dr ...).
#
# Examples:
#   dev_tools/collect.sh -l "baseline" /Volumes/EXT2_EXF/george_blood /Volumes/EXT2_EXF/media_burn
#   dev_tools/collect.sh -c ~/configs/study.json -g -l "period study" /Volumes/EXT2_APFS/jpc
#
# Packages with the same folder name in two sources must go in separate invocations (the cycle
# script refuses duplicates); they share the run, so the second invocation skips them.

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${COLLECT_PYTHON:-python3}"
STAGING_DIR="$HOME/git/JPC_AV/sample_files/jpc"
LABEL=""
CONFIG=""
PASS=()

while [ $# -gt 0 ]; do
    case "$1" in
        -c) CONFIG="$2"; shift 2 ;;
        -l) LABEL="$2"; shift 2 ;;
        -g) export COLLECT_GROUND_TRUTH=1; shift ;;
        -i) export COLLECT_IMAGES=1; shift ;;
        -s) STAGING_DIR="$2"; PASS+=("$1" "$2"); shift 2 ;;
        -b|-r) PASS+=("$1" "$2"); shift 2 ;;
        -h|--help) sed -n '3,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --) echo "collect.sh: av-spex arguments are not allowed; set the config first (-c FILE)" >&2; exit 1 ;;
        *) PASS+=("$1"); shift ;;
    esac
done

if [ -n "$CONFIG" ]; then
    av-spex --import-config "$CONFIG" >/dev/null || { echo "could not import $CONFIG" >&2; exit 1; }
fi

RUN_ID=$("$PY" "$HERE/corpus.py" init-run ${LABEL:+--label "$LABEL"}) || exit 1
export COLLECT_RUN_ID="$RUN_ID"
echo "Run: $RUN_ID  (corpus: ${AVSPEX_CORPUS:-$HOME/git/JPC_AV/avspex_corpus})"

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
echo "Aggregate with: python3 $HERE/aggregate.py $RUN_ID"
exit $rc
