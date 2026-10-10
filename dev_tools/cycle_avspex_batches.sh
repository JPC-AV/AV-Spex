#!/usr/bin/env bash
#
# cycle_avspex_batches.sh
#
# Cycles JPC_AV_##### package folders from a slow external drive onto the local
# disk in batches, runs AV Spex on each one, then copies the new/updated outputs
# back to the package's original location on the external and frees the local
# space for the next batch.
#
# The external copy is never removed: packages are COPIED to local disk, and
# only files that AV Spex created or modified are copied back. That way the big
# .mkv never has to be re-written to the spinning disk, and an interrupted run
# can't leave a package half-moved.
#
# Usage:
#   ./cycle_avspex_batches.sh [options] [SOURCE_DIR ...] [-- AV_SPEX_ARGS ...]
#
# Options:
#   -s DIR   Local staging dir   (default: $HOME/git/JPC_AV/sample_files/jpc)
#   -b GB    Max batch size, GB  (default: 40)
#   -r GB    Free space to keep in reserve on the local disk, GB (default: 10)
#   -k KEY   Keep progress under <staging>/.avspex_cycle/KEY/ (collect.sh passes the run ID,
#            so each code + config gets its own done list)
#   -n       Dry run: list the batches and exit
#   -h       Help
#
# SOURCE_DIRs are searched recursively for folders named JPC_AV_<digits>.
# Default sources: /Volumes/EXT2_EXF/george_blood /Volumes/EXT2_EXF/media_burn
#
# Anything after "--" is passed to av-spex, e.g.
#   ./cycle_avspex_batches.sh -- --profile step1
#   ./cycle_avspex_batches.sh /Volumes/EXT2_APFS/jpc -- --off mediatrace
#
# Re-running is safe: completed packages are listed in
# <staging>/.avspex_cycle[/KEY]/done.txt and skipped. Delete a line there to redo one.
#
# AVSPEX_CMD (default av-spex) is called as: $AVSPEX_CMD <local package> [AV_SPEX_ARGS],
# with CYCLE_SOURCE set to the package's original path.

# ---------------------------------------------------------------- settings ---
STAGING_DIR="$HOME/git/JPC_AV/sample_files/jpc"
BATCH_GB=40
RESERVE_GB=10
DRY_RUN=0
STATE_KEY=""
AVSPEX_CMD="${AVSPEX_CMD:-av-spex}"
DEFAULT_SOURCES=(/Volumes/EXT2_EXF/george_blood /Volumes/EXT2_EXF/media_burn)

usage() { sed -n '3,39p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# ------------------------------------------------------------- arguments ---
ORIG_ARGS=("$@")
SOURCES=()
AVSPEX_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -s) STAGING_DIR="$2"; shift 2 ;;
        -b) BATCH_GB="$2"; shift 2 ;;
        -r) RESERVE_GB="$2"; shift 2 ;;
        -k) STATE_KEY="$2"; shift 2 ;;
        -n) DRY_RUN=1; shift ;;
        -h|--help) usage 0 ;;
        --) shift; AVSPEX_ARGS=("$@"); break ;;
        -*) echo "Unknown option: $1" >&2; usage 1 ;;
        *)  SOURCES+=("${1%/}"); shift ;;
    esac
done
[ ${#SOURCES[@]} -eq 0 ] && SOURCES=("${DEFAULT_SOURCES[@]}")

# Keep the Mac awake for the whole run (macOS only).
if [ "$DRY_RUN" -eq 0 ] && [ -z "$CYCLE_CAFFEINATED" ] && command -v caffeinate >/dev/null 2>&1; then
    export CYCLE_CAFFEINATED=1
    exec caffeinate -ims "$0" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
fi

STAGING_DIR="${STAGING_DIR%/}"
STATE_DIR="$STAGING_DIR/.avspex_cycle${STATE_KEY:+/$STATE_KEY}"
# Markers track local copies, which are shared whatever the key
MARKERS="$STAGING_DIR/.avspex_cycle/markers"
DONE_FILE="$STATE_DIR/done.txt"
FAILED_FILE="$STATE_DIR/failed.txt"
LOG_FILE="$STATE_DIR/cycle_$(date +%Y%m%d_%H%M%S).log"
MANIFEST="$STATE_DIR/manifest.tsv"

mkdir -p "$MARKERS" "$STATE_DIR" || { echo "Cannot create $STATE_DIR" >&2; exit 1; }
touch "$DONE_FILE" "$FAILED_FILE"

# Don't drag macOS extended attributes (._ files) onto the exFAT drive.
CP_NOXATTR=""
[ "$(uname)" = "Darwin" ] && CP_NOXATTR="-X"

# --------------------------------------------------------------- helpers ---
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# Total bytes of regular files under a dir (ignores AppleDouble/.DS_Store).
tree_bytes() {
    find "$1" -type f ! -name '._*' ! -name '.DS_Store' -print0 2>/dev/null |
        xargs -0 stat -f %z 2>/dev/null | awk '{s+=$1} END{printf "%.0f\n", s+0}'
}
# Linux fallback for testing (BSD stat uses -f %z, GNU stat uses -c %s)
if [ "$(uname)" != "Darwin" ]; then
    tree_bytes() {
        find "$1" -type f ! -name '._*' ! -name '.DS_Store' -print0 2>/dev/null |
            xargs -0 stat -c %s 2>/dev/null | awk '{s+=$1} END{printf "%.0f\n", s+0}'
    }
fi

free_bytes() { df -Pk "$STAGING_DIR" | awk 'NR==2{printf "%.0f\n", $4*1024}'; }
gb() { awk -v b="$1" 'BEGIN{printf "%.1f GB", b/1073741824}'; }
is_done() { grep -qx "$1" "$DONE_FILE"; }

# Safety: only ever rm -rf a JPC_AV_##### folder directly inside the staging dir.
remove_local() {
    local name="$1"
    if echo "$name" | grep -Eq '^JPC_AV_[0-9]+$' && [ -d "$STAGING_DIR/$name" ]; then
        rm -rf "${STAGING_DIR:?}/$name"
    fi
    rm -f "$MARKERS/$name"
}

# Copy package from external -> local. Verifies total bytes afterwards.
copy_in() {
    local name="$1" src="$2" dest="$STAGING_DIR/$1"
    log "  copying in  $name  ($src)"
    rm -f "$MARKERS/$name"
    cp -Rp $CP_NOXATTR "$src" "$STAGING_DIR/" 2>>"$LOG_FILE" || { log "  ! copy failed: $name"; return 1; }
    local a b
    a=$(tree_bytes "$src"); b=$(tree_bytes "$dest")
    if [ "$a" != "$b" ]; then
        log "  ! size mismatch after copy ($a vs $b): $name"; return 1
    fi
    # Marker: anything newer than this afterwards was written by AV Spex.
    touch "$MARKERS/$name"
}

# Copy files created/modified since the marker back to the external.
copy_back() {
    local name="$1" src_dir="$2" local_dir="$STAGING_DIR/$1" n=0 rc=0 rel f
    log "  copying back new/updated files for $name"
    while IFS= read -r -d '' f; do
        rel="${f#$local_dir/}"
        mkdir -p "$src_dir/$(dirname "$rel")" &&
            cp -p $CP_NOXATTR "$f" "$src_dir/$rel" 2>>"$LOG_FILE" || { log "  ! failed to copy back: $rel"; rc=1; }
        n=$((n+1))
    done < <(find "$local_dir" -type f -newer "$MARKERS/$name" ! -name '._*' ! -name '.DS_Store' -print0)
    log "  copied back $n file(s)"
    return $rc
}

# --------------------------------------------------------- find packages ---
: > "$MANIFEST"
for root in "${SOURCES[@]}"; do
    if [ ! -d "$root" ]; then echo "Source not found (drive mounted?): $root" >&2; exit 1; fi
    find "$root" -type d -name 'JPC_AV_*' \
        ! -path '*/$RECYCLE.BIN/*' ! -path '*/.Trashes/*' 2>/dev/null |
        grep -E '/JPC_AV_[0-9]+$' | while IFS= read -r d; do
            printf '%s\t%s\n' "$(basename "$d")" "$d"
        done >> "$MANIFEST"
done
sort -o "$MANIFEST" "$MANIFEST"

dupes=$(cut -f1 "$MANIFEST" | uniq -d)
if [ -n "$dupes" ]; then
    echo "Duplicate package names found in sources, refusing to continue:" >&2
    echo "$dupes" >&2; exit 1
fi

total=$(wc -l < "$MANIFEST" | tr -d ' ')
[ "$total" -eq 0 ] && { echo "No JPC_AV_##### folders found in: ${SOURCES[*]}"; exit 0; }

log "Sources: ${SOURCES[*]}"
log "Staging: $STAGING_DIR   batch cap: ${BATCH_GB} GB   reserve: ${RESERVE_GB} GB"
log "AV Spex: $AVSPEX_CMD <package> ${AVSPEX_ARGS[*]}"
log "Found $total package(s); $(grep -c . "$DONE_FILE") already done."

if [ "$DRY_RUN" -eq 0 ] && ! command -v "$AVSPEX_CMD" >/dev/null 2>&1; then
    log "av-spex not found on PATH (set AVSPEX_CMD=/path/to/av-spex)"; exit 1
fi

# --------------------------------------------------- resume leftovers ---
# A local copy with a marker finished copying in -> reuse it.
# A local copy without a marker was a partial copy -> discard it.
LEFTOVER=()
while IFS=$'\t' read -r name src; do
    [ -d "$STAGING_DIR/$name" ] || continue
    if is_done "$name"; then
        remove_local "$name"
    elif [ -f "$MARKERS/$name" ]; then
        log "Resuming leftover local copy: $name"; LEFTOVER+=("$name")
    else
        log "Discarding partial local copy: $name"; [ "$DRY_RUN" -eq 0 ] && remove_local "$name"
    fi
done < "$MANIFEST"

# -------------------------------------------------------------- main loop ---
BATCH_CAP=$(awk -v g="$BATCH_GB" 'BEGIN{printf "%.0f", g*1073741824}')
RESERVE=$(awk -v g="$RESERVE_GB" 'BEGIN{printf "%.0f", g*1073741824}')
src_of() { awk -F'\t' -v n="$1" '$1==n{print $2; exit}' "$MANIFEST"; }

PENDING=()
while IFS=$'\t' read -r name src; do
    is_done "$name" && continue
    skip=0; for l in ${LEFTOVER[@]+"${LEFTOVER[@]}"}; do [ "$l" = "$name" ] && skip=1; done
    [ $skip -eq 0 ] && PENDING+=("$name")
done < "$MANIFEST"

batch_no=0
idx=0
ok_count=0; fail_count=0

while [ $idx -lt ${#PENDING[@]} ] || [ ${#LEFTOVER[@]} -gt 0 ]; do
    batch_no=$((batch_no+1))
    BATCH=(${LEFTOVER[@]+"${LEFTOVER[@]}"}); LEFTOVER=()

    # Fill the batch up to the cap, without exceeding free space minus reserve.
    avail=$(( $(free_bytes) - RESERVE ))
    [ "$DRY_RUN" -eq 1 ] && avail=$BATCH_CAP
    used=0
    while [ $idx -lt ${#PENDING[@]} ]; do
        name="${PENDING[$idx]}"
        size=$(tree_bytes "$(src_of "$name")")
        if [ ${#BATCH[@]} -gt 0 ] && { [ $((used+size)) -gt $BATCH_CAP ] || [ $((used+size)) -gt $avail ]; }; then
            break
        fi
        if [ $size -gt $avail ]; then
            log "! $name is $(gb $size) but only $(gb $avail) is available locally (after reserve). Free up space or lower -r."
            exit 1
        fi
        BATCH+=("$name"); used=$((used+size)); idx=$((idx+1))
    done

    log "=== Batch $batch_no: ${BATCH[*]}  ($(gb $used) to copy)"
    [ "$DRY_RUN" -eq 1 ] && continue

    # 1. Copy in
    READY=()
    for name in "${BATCH[@]}"; do
        if [ -f "$MARKERS/$name" ] && [ -d "$STAGING_DIR/$name" ]; then
            READY+=("$name"); continue   # leftover from a previous run
        fi
        if copy_in "$name" "$(src_of "$name")"; then
            READY+=("$name")
        else
            remove_local "$name"; echo "$name	copy_in	$(date)" >> "$FAILED_FILE"; fail_count=$((fail_count+1))
        fi
    done

    # 2. Process, copy back, clean up — one package at a time
    for name in ${READY[@]+"${READY[@]}"}; do
        src=$(src_of "$name")
        log "  running AV Spex on $name"
        start=$(date +%s)
        CYCLE_SOURCE="$src" "$AVSPEX_CMD" "$STAGING_DIR/$name" ${AVSPEX_ARGS[@]+"${AVSPEX_ARGS[@]}"} 2>&1 | tee -a "$LOG_FILE"
        rc=${PIPESTATUS[0]}
        log "  AV Spex exit code $rc for $name ($(( ($(date +%s)-start)/60 )) min)"

        if copy_back "$name" "$src" && [ $rc -eq 0 ]; then
            echo "$name" >> "$DONE_FILE"; ok_count=$((ok_count+1))
            remove_local "$name"
            log "  done: $name"
        elif [ $rc -ne 0 ]; then
            # Outputs were still copied back for inspection; free the space and move on.
            echo "$name	avspex_exit_$rc	$(date)" >> "$FAILED_FILE"; fail_count=$((fail_count+1))
            remove_local "$name"
            log "  ! AV Spex failed on $name (outputs copied back; will retry on next run)"
        else
            echo "$name	copy_back	$(date)" >> "$FAILED_FILE"; fail_count=$((fail_count+1))
            log "  ! copy back failed for $name; leaving local copy at $STAGING_DIR/$name"
        fi
    done
done

log "Finished. $ok_count succeeded, $fail_count failed this run. Log: $LOG_FILE"
if [ $fail_count -gt 0 ]; then
    log "Failures listed in $FAILED_FILE"
    exit 1
fi
exit 0
