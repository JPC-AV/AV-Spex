#!/usr/bin/env bash
#
# collect_one.sh: the AVSPEX_CMD that collect.sh hands to cycle_avspex_batches.sh.
#
# For one package (already copied to local disk), it:
#   1. refuses to run if the code or config changed since the run started,
#   2. runs av-spex on it,
#   3. optionally (COLLECT_GROUND_TRUTH=1) makes one whole-file active-area signalstats pass
#      over the crop frame analysis used, while the video is still local,
#   4. copies the sidecars the run wrote into the corpus run directory (corpus.py).
#
# Expects COLLECT_RUN_ID (set by collect.sh). CYCLE_SOURCE (set by the cycle script) is
# recorded as the package's original location. Exits with av-spex's exit code.

PKG="${1%/}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${COLLECT_PYTHON:-python3}"
RUN_ID="${COLLECT_RUN_ID:?COLLECT_RUN_ID is not set; run this through collect.sh}"

"$PY" "$HERE/corpus.py" check-run "$RUN_ID" || exit 3

NAME="$(basename "$PKG")"
if [ ! -f "$PKG/$NAME.mkv" ]; then
    # The files' ID is usually the folder name, but not always (JPC_AV_20241/ holds JPC_AV_02041_*)
    vid=$(find "$PKG" -maxdepth 1 -type f -name 'JPC_AV_*.mkv' ! -name '._*' ! -name '*_access*' | head -1)
    [ -n "$vid" ] && NAME="$(basename "$vid" .mkv)"
fi
LOG="$PKG/${NAME}_qc_metadata/${NAME}_avspex_processing.log"
log_offset=0
[ -f "$LOG" ] && log_offset=$(stat -f %z "$LOG")

started=$(date +%s)
av-spex "$PKG"
rc=$?
avspex_s=$(( $(date +%s) - started ))

GT_JSON=""
if [ $rc -eq 0 ] && [ "${COLLECT_GROUND_TRUTH:-0}" = "1" ]; then
    corpus_root="${AVSPEX_CORPUS:-$HOME/git/JPC_AV/avspex_corpus}"
    gt_dir="$corpus_root/runs/$RUN_ID/packages/$(basename "$PKG")/ground_truth"
    mkdir -p "$gt_dir"
    JSON="$PKG/${NAME}_qc_metadata/${NAME}_enhanced_frame_analysis.json"
    VIDEO="$PKG/$NAME.mkv"
    # Crop as frame analysis measured periods (frame_geometry.build_crop_filter form);
    # the simple-mode 25 px inset when border detection left nothing usable.
    CROP=$("$PY" - "$JSON" "$VIDEO" <<'EOF'
import json, subprocess, sys
try:
    x, y, w, h = (int(v) for v in json.load(open(sys.argv[1]))["initial_borders"]["active_area"])
    if w > 0 and h > 0:
        print(f"crop={w}:{h}:{x}:{y}"); sys.exit()
except Exception:
    pass
try:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "json", sys.argv[2]],
                         capture_output=True, text=True).stdout
    s = json.loads(out)["streams"][0]
    print(f"crop={s['width'] - 50}:{s['height'] - 50}:25:25")
except Exception:
    print("")
EOF
)
    echo "[collect] $NAME: whole-file active-area signalstats (${CROP:-full frame})"
    gt_start=$(date +%s)
    ffprobe -v error -f lavfi \
        -i "movie=${VIDEO}${CROP:+,$CROP},signalstats=stat=brng+tout+vrep" \
        -show_entries "frame=pts_time:frame_tags=lavfi.signalstats.BRNG,lavfi.signalstats.TOUT,lavfi.signalstats.VREP,lavfi.signalstats.YAVG,lavfi.signalstats.YMIN,lavfi.signalstats.YMAX,lavfi.signalstats.SATMAX" \
        -of compact=p=0 | gzip -c > "$gt_dir/active_signalstats.txt.gz.partial"
    if [ "${PIPESTATUS[0]}" -eq 0 ]; then
        mv "$gt_dir/active_signalstats.txt.gz.partial" "$gt_dir/active_signalstats.txt.gz"
        frames=$(gzip -dc "$gt_dir/active_signalstats.txt.gz" | wc -l | tr -d ' ')
        GT_JSON="{\"crop\": \"$CROP\", \"seconds\": $(( $(date +%s) - gt_start )), \"frames\": $frames}"
        echo "[collect] $NAME: $frames frames"
    else
        rm -f "$gt_dir/active_signalstats.txt.gz.partial"
        echo "[collect] ffprobe failed for $NAME; no ground truth written"
    fi
fi

args=(collect-package "$RUN_ID" "$PKG" --started "$started" --log-offset "$log_offset"
      --exit-code "$rc" --avspex-seconds "$avspex_s")
[ -n "${CYCLE_SOURCE:-}" ] && args+=(--source "$CYCLE_SOURCE")
[ "${COLLECT_IMAGES:-0}" = "1" ] && args+=(--images)
[ -n "$GT_JSON" ] && args+=(--ground-truth-json "$GT_JSON")
"$PY" "$HERE/corpus.py" "${args[@]}" >/dev/null || echo "[collect] ! could not collect $NAME"
echo "[collect] $NAME collected into run $RUN_ID (av-spex exit $rc)"
exit $rc
