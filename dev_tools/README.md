# dev_tools: batch runs, results corpus, offline replays

Developer-only tooling for running AV Spex over many test packages and learning from the
results. It is not part of the app. The package only includes `src/`, the PyInstaller build
only bundles what the launcher imports, and pytest only collects `tests/`.

The workflow has three stages:

1. **Collect** (slow, needs the drives): run AV Spex once per package and keep the small
   sidecars in a local corpus, tagged with the code and config that produced them.
2. **Aggregate** (seconds): flatten a run into one table, or diff two runs.
3. **Replay** (seconds per package): rerun a decision that AV Spex makes from the QCTools report
   alone (for example, period placement) under settings or code it never ran with, and score
   it against ground truth.

Collect once, then ask the corpus as many questions as you like. Go back to the drives only
when a question needs a new measurement from the video.

## Corpus

`$AVSPEX_CORPUS` (default `~/git/JPC_AV/avspex_corpus`):

```
runs/<run_id>/                        one directory per run
    run.json, checks_config.json, spex_config.json, src.diff
    packages/<FOLDER>/
        package.json                  video properties, timings, exit code, file list
        <ID>_qc_metadata/...          the sidecars this run wrote, the QCTools report it read,
        <ID>_report_csvs/...          and the processing log trimmed to this run
        ground_truth/active_signalstats.txt.gz    only with collect.sh -g
cache/                                replay cache (see Replays)
labels/                               label sheets and keys, seed labels, timecode.json
notes/                                study write-ups (by hand; no tool reads or writes it)
```

- **Run ID.** It is `<commit>[-d<hash of uncommitted src/ changes>]-c<hash of checks+spex config>`,
  so the same code and config always land in the same directory.
- **Resuming.** Re-running a batch resumes it, and any change starts a new run.
- **Excluded files.** Media, HTML reports and images are left out (pass `-i` to keep the
  images). A package costs ~5–40 MB, mostly the QCTools report.
- **Listing runs.** `python3 dev_tools/corpus.py list` shows each run's ID, date, package
  count and label. `corpus.py run-id` prints the ID the current code and config would get,
  without creating anything. The other subcommands (`init-run`, `check-run`, `done`,
  `collect-package`) are what `collect.sh` calls; `import` is covered under Replays.
- **Output location.** `aggregate.py`, `period_grid.py`, `bin_table.py` and `tag_weights.py`
  write their CSVs to the current directory unless given `--out` / `--out-dir`, so run them
  from somewhere other than the repo root or pass a path. Only `label_sheet.py` writes into the
  corpus.

## Collect

```bash
dev_tools/collect.sh -l "baseline" /Volumes/EXT2_EXF/george_blood /Volumes/EXT2_EXF/media_burn
dev_tools/collect.sh -l "baseline" /Volumes/EXT2_APFS/jpc   # separate call: names overlap with EXT2_EXF
dev_tools/collect.sh -c study_config.json -g -l "period study" /Volumes/EXT2_APFS/jpc
```

- **What it does.** `collect.sh` drives `cycle_avspex_batches.sh`, which copies packages to
  local disk in batches that fit, runs `collect_one.sh` on each, copies the new outputs back to
  the drive and frees the space.
- **Config.** The run uses the config AV Spex currently has saved. `-c FILE` imports one
  first, and AV Spex arguments after `--` are refused, because they would change the config
  behind the run ID.
- **Ground truth.** `-g` adds a whole-file active-area signalstats pass per package. The
  period replay needs it. It takes roughly 5–15 % of the tape's length (0.08–0.11× measured
  in the October 2026 run).
- **Mid-batch edits.** If you edit `src/` or the config mid-batch, the remaining packages fail
  fast (exit 3) instead of being filed under the wrong run ID. Finish the batch or restart it.
- **Failed packages.** A package whose av-spex run failed is still collected for inspection,
  but it is not counted as done. The next invocation retries it.

Overnight gotchas, all hit during the October 2026 study:

- **Time Machine local snapshots.** They keep deleted staged files on disk, so free space
  stops growing. Run `tmutil deletelocalsnapshots /`, or turn Time Machine off for the run.
- **Folder name vs file ID.** They can differ (`JPC_AV_20241/` holds `JPC_AV_02041_*`). The
  tools read the ID from the files.
- **Sleep.** `cycle_avspex_batches.sh` wraps itself in `caffeinate`. It must be executable
  and called by path.

## Aggregate

```bash
python3 dev_tools/aggregate.py <run> --out table.csv          # one row per package
python3 dev_tools/aggregate.py <run_a> <run_b> --out both.csv # stacked
python3 dev_tools/aggregate.py --diff <run_a> <run_b>         # what changed, per package
python3 dev_tools/aggregate.py --diff <a> <b> --only '^fa_'   # just frame-analysis columns
python3 dev_tools/aggregate.py --columns <run>                # what columns exist
```

Columns come from small extractor functions in `aggregate.py`, prefixed by area:

| Prefix | Area |
|---|---|
| `fa_` | period selection, borders, steps |
| `ss_` | signalstats |
| `brng_` | BRNG |
| `bars_`, `tone_` | bars and tone detection |
| `dropped_`, `dupes_`, `bitplane_` | the independent detectors |
| `qct_rows_` | row count of every qct-parse/CLAMS CSV |

To track something new, write an extractor and add it to `EXTRACTORS`. Older runs show the
column empty.

## Replays

Each replay lives in `replays/` and calls the real selection code with inputs rebuilt from
the corpus. **Every replay must first reproduce what the run actually did**, and report how
often it does. That check is what makes its other numbers believable. It is also what flags
the replay as stale once the code it mirrors changes, since these scripts call private
methods.

- `replays/period_grid.py <run> [--counts 3,6,8] [--durations 30,60] [--out FILE]` scores
  every period count × duration against the ground truth. Its harness check (`harness_match`)
  compares against the run's own periods. It needs a run collected with `-g`. This is the
  replay behind the October 2026 change to 6 × 30. That study's outputs were imported as the
  run `period-study-2026-10` (`corpus.py import`), and replaying it reproduces the original
  grid cell for cell.
- `replays/bin_table.py <run> [--screen] [--out FILE]` writes one row per 10 s bin with every
  QCTools tag's per-bin statistics, the bin's suitability, score and families, and whether
  today's selection covers it. `--screen` ranks every tag statistic without labels (availability, spread,
  redundancy with scored metrics, correlation with luma and with motion, and where today's
  score puts the tag's peak bin) to decide which tags are worth labeling for.
- `replays/tag_weights.py <run> [--damage MODE] [--out-dir DIR]` scores tags and scoring
  configurations against operator labels (see below). It needs labels, so run it after a
  labeling round.

All three replays, and `label_sheet.py`, share `replays/_selection.py`, which rebuilds stage 0
from the corpus and runs stages 1–3. Loading a package walks its QCTools report three times, so
the result is cached in `$AVSPEX_CORPUS/cache/` (~0.5 MB per package). The cache key covers the
report (size and modification time) and the source of `bin_scoring`, `bin_suitability`,
`frame_analysis`, `qctools_bin_profile` and `_selection.py` itself, so editing any of them
rebuilds it. It does **not** cover the package's `_enhanced_frame_analysis.json` (the bars
regions stage 0 reads), so delete the package's cache entry if you change that file. Each
package directory keeps one entry; loading under a new key deletes the old one.
`bin_table.py --no-cache` bypasses it. With a warm cache, a replay of the whole study run takes
seconds.

## Weighing tags (labels)

The corpus ground truth (active-area BRNG/TOUT/VREP) is what period selection already scores,
so it can catch a regression but it can't give credit to a tag that isn't scored yet. That
takes an operator.

1. `python3 dev_tools/replays/bin_table.py <run> --screen`: pick candidate tags and update
   `NEW_CANDIDATES` in `label_sheet.py`. This is a code edit: the candidate list (like
   `LEGALITY_CANDIDATES` and `tag_weights.CONFIGS`) lives in the committed scripts, not in the
   corpus, so commit it alongside the sheet's results.
2. `python3 dev_tools/label_sheet.py <run> --probe --name round1` (drives mounted the first time,
   for start timecodes) writes to `$AVSPEX_CORPUS/labels/`:
   - `sheet_round1.csv`: ~12 bins per tape (`--per-tape`, `--controls`). Each tape gets 2
     random controls, the peak bin of every candidate tag, today's evidence bins and runner-up
     bins, sorted by tape and timecode.
     `tc_in`/`tc_out` are the file's own NDF/DF timecode, as the NLE shows it. The empty
     columns to fill in are `defect`, `severity`, `origin` and `notes`. `--name` refuses a name
     whose sheet already exists.
   - `key_round1.json`: why each bin was picked. Don't open it while labeling.
   - `timecode.json`: start timecodes cached by `--probe`. Without one, a tape's rows start
     at 00:00:00:00 and say so in `notes`.
3. Fill in `defect` (`none, dropout, head_switching_tracking, sync_loss_roll, illegal_levels,
   chroma_error, noise, signal_loss_freeze, graphics_slate_only, other`; join several with
   `+`), `severity` (0 nothing worth a period, 1 visible, 2 obvious) and `origin` (`tape` for
   this transfer's playback damage, `content` for the program as made, `source` for damage
   baked into dubbed or archival material; blank for none). Leave a row blank to skip it.
   Illegal levels in a graphic are `illegal_levels` (origin `content`), not
   `graphics_slate_only`: they are still what BRNG analysis measures.
4. `python3 dev_tools/replays/tag_weights.py <run> --damage MODE` reports:
   - per tag: hit rate of its top bins against the control base rate, and AUC overall and by
     defect type
   - per configuration (`CONFIGS`, which are `bin_scoring` metric and weight variants): damage
     recall, estimated precision of the bins it targets, the ground-truth guard and a hold-out
     across collections and tapes
   `MODE` decides what counts as damage: `labeled` (any defect at severity >= 1), `severe`
   (severity 2), `tape` (origin `tape`; for rows without an origin, i.e. round 1, `dropout`,
   `sync_loss_roll` or `signal_loss_freeze`, which also counts round 1's still photos logged as
   `signal_loss_freeze`) or `illegal` (any `illegal_levels`). Round 1 needs this: on worn VHS
   63% of random bins carry some defect at severity 1, so `labeled` leaves nothing for a tag
   to beat. Outputs are `tag_weights_<MODE>_{tags,configs,per_tape}.csv`, written to `--out-dir` (default: the
   current directory). `--tags` scores extra tag columns, e.g. `ydif_mean+,blur_max+`.
5. Bins a configuration targets that nobody has labeled are valued at the control base rate
   and listed in `tag_weights_<MODE>_per_tape.csv`. Any later sheet skips bins already labeled.
6. Focused rounds: `label_sheet.py <run> --round legality --name round2 --regions FILE` builds a
   ~6-bin-per-tape sheet for the legal-floor luma measures (`under_black`, `over_white`, added to
   every bin by `_selection.add_legal_floor_columns`): 1 control, each measure's top bins, the
   bins the legality `CONFIGS` would target, and bins from `FILE` (`video_id,tc_in,tc_out`
   regions an operator flagged as missed; a DF tape's timecodes may be typed with `:`).
   Blind IDs carry an `L` prefix (`--id-prefix`).

`labels/seed_labels.csv` holds earlier operator spot checks (same columns plus `bin_start` in
seconds). Labels are keyed by `(video_id, bin_start)`, so they carry over to future runs.

Both `label_sheet.py` and `tag_weights.py` load labels by globbing `labels/sheet_*.csv`, each
read through the `key_*.json` of the same name (a sheet with no matching key is skipped
silently), plus `seed_labels.csv`. Things to know:

- Sheets are not validated on load. A misspelled defect or origin is read as given, so check
  the tokens before running.
- A row counts as labeled for `tag_weights.py` only when both `defect` and `severity` are
  filled; `label_sheet.py` treats any row with a `defect` as labeled and won't ask about it
  again.
- `tape` mode matches `tape` anywhere in a `+`-joined origin, so `source+tape` counts.
- If a bin appears in more than one file, the last one read wins without a warning: the seed
  file first, then sheets in name order.
