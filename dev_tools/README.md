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

`$AVSPEX_CORPUS` (default `~/git/JPC_AV/avspex_corpus`), one directory per run:

```
runs/<run_id>/
    run.json, checks_config.json, spex_config.json, src.diff
    packages/<FOLDER>/
        package.json                  video properties, timings, exit code, file list
        <ID>_qc_metadata/...          the sidecars this run wrote, the QCTools report it read,
        <ID>_report_csvs/...          and the processing log trimmed to this run
        ground_truth/active_signalstats.txt.gz    only with collect.sh -g
```

- **Run ID.** It is `<commit>[-d<hash of uncommitted src/ changes>]-c<hash of checks+spex config>`,
  so the same code and config always land in the same directory.
- **Resuming.** Re-running a batch resumes it, and any change starts a new run.
- **Excluded files.** Media, HTML reports and images are left out (pass `-i` to keep the
  images). A package costs ~5–40 MB, mostly the QCTools report.
- **Listing runs.** `python3 dev_tools/corpus.py list` shows each run's ID, date, package
  count and label.

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
  period replay needs it. It takes roughly 5–15 % of the tape's length.
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

- `replays/period_grid.py <run> [--counts 3,6,8] [--durations 30,60]` scores every period
  count × duration against the ground truth. Its harness check (`harness_match`) compares
  against the run's own periods. It needs a run collected with `-g`. This is the replay
  behind the October 2026 change to 6 × 30. That study's outputs were imported as the run
  `period-study-2026-10` (`corpus.py import`), and replaying it reproduces the original grid
  cell for cell.
