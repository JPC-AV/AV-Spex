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
studies/<date>_<slug>/                a study's plan, manifest and one-off scripts
```

- **Version control.** The corpus is its own git repository. Its `.gitignore` tracks the
  labels (CSV and keys), `studies/`, `notes/` and each run's `run.json` / `package.json`, and
  leaves out what can be rebuilt: `cache/`, the package sidecars and scan caches, and the event
  workbooks with their images. The workbooks embed their pictures, so after each labeling
  session run `python3 dev_tools/event_labels.py` to export the verdicts to
  `labels/events_<NAME>.csv`, then commit the corpus.

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

A tape can also differ because the code changed on purpose since the run (a gate fix that
moves a period). `replays/harness.py` tells the two apart:

```bash
python3 dev_tools/replays/harness.py <run>                       # status per tape
python3 dev_tools/replays/harness.py <run> --accept TAPE... --reason "what moved it and why"
python3 dev_tools/replays/harness.py <run> --forget TAPE...      # drop an entry
```

`--accept` records the tape's current periods, the reason, the date and the code commit in
`runs/<run>/expected_drift.json`. Every replay then reports each tape as `match` (the run's own
periods), `expected` (the recorded periods) or `UNEXPECTED`. Only the last means the replay
can't be trusted. A recorded tape that moves again is `UNEXPECTED`, and an entry for a tape
that matches again is flagged as no longer needed. Accept only drift you have looked at and can
explain. `period-study-2026-10` records four: 01823, 02225 and 02328 (the 2026-10-10
short-tape overlap fix) and 02211 (the tail-static gate, 22a2c20).

- `replays/period_grid.py <run> [--counts 3,6,8] [--durations 30,60] [--out FILE]` scores
  every period count × duration against the ground truth. Its harness check (`harness`, and
  `harness_match` = 1 for a plain match) compares against the run's own periods. It needs a run collected with `-g`. This is the
  replay behind the October 2026 change to 6 × 30. That study's outputs were imported as the
  run `period-study-2026-10` (`corpus.py import`), and replaying it reproduces the original
  grid cell for cell.
- `replays/bin_table.py <run> [--screen] [--out FILE]` writes one row per 10 s bin with every
  QCTools tag's per-bin statistics, the bin's suitability, score and families, and whether
  today's selection covers it. `--screen` ranks every tag statistic without labels
  (availability, spread, redundancy with scored metrics, correlation with luma and with motion,
  and where today's score puts the tag's peak bin) to decide which tags are worth labeling for.
- `replays/tag_weights.py <run> [--damage MODE] [--stake [CONFIG...]] [--out-dir DIR]` scores
  tags and scoring configurations against operator labels (see below). It needs labels, so
  run it after a labeling round.

All three replays, and `label_sheet.py`, share `replays/_selection.py`, which rebuilds stage 0
from the corpus and runs stages 1–3. Loading a package walks its QCTools report three times, so
the result is cached in `$AVSPEX_CORPUS/cache/` (~0.5 MB per package). The cache key covers the
report (size and modification time), what stage 0 reads from the package's
`_enhanced_frame_analysis.json` (head-bars end, bars regions, border hints) and the source of
`bin_scoring`, `bin_suitability`, `frame_analysis`, `qctools_bin_profile` and `_selection.py`
itself, so changing any of them rebuilds it. Each package directory keeps one entry; loading
under a new key deletes the old one.
`bin_table.py --no-cache` bypasses it. With a warm cache, a replay of the whole study run takes
seconds.

## Weighing tags (labels)

The corpus ground truth (active-area BRNG/TOUT/VREP) is what period selection already scores,
so it can catch a regression but it can't give credit to a tag that isn't scored yet. That
takes an operator.

1. `python3 dev_tools/replays/bin_table.py <run> --screen`: pick candidate tags and update
   `NEW_CANDIDATES` in `replays/study.py`. That module holds everything a round asks: the
   candidate tags (`NEW_CANDIDATES`, `LEGALITY_CANDIDATES`) and the scoring configurations
   compared (`CONFIGS`, `LEGALITY_CONFIGS`). It is code, not corpus data, so commit it alongside
   the round's results.
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

   **One row per finding.** When a bin's problems differ in severity or origin, copy the row
   and keep its `blind_id`, one row each: e.g. `illegal_levels`, 2, `source` and `dropout`, 1,
   `tape`. A bin counts as damaged when any of its findings does, so the tape dropout counts in
   `tape` mode at its own severity. Joining origins (`source+tape`) still works, but then one
   severity covers both.

   Then run `python3 dev_tools/labels.py` to check the sheets (see below).
4. `python3 dev_tools/replays/tag_weights.py <run> --damage MODE` reports:
   - per tag: hit rate of its top bins against the control base rate, and AUC overall and by
     defect type
   - per configuration (`CONFIGS`, which are `bin_scoring` metric and weight variants): damage
     recall, estimated precision of the bins it targets, the ground-truth guard and a hold-out
     across collections and tapes
   `MODE` decides what counts as damage: `labeled` (any defect at severity >= 1), `severe`
   (severity 2), `tape` (origin `tape`; for rows without an origin, i.e. round 1, `dropout`,
   `sync_loss_roll` or `signal_loss_freeze`, so a round-1 still photo must not be logged as
   `signal_loss_freeze`) or `illegal` (any `illegal_levels`). Round 1 needs this: on worn VHS
   63% of random bins carry some defect at severity 1, so `labeled` leaves nothing for a tag
   to beat. Outputs are `tag_weights_<MODE>_{tags,configs,per_tape}.csv`, written to
   `--out-dir` (default: the current directory). `--tags` scores extra tag columns, e.g.
   `ydif_mean+,blur_max+`.

   **Uncertainty.** Every headline number carries a 95 % interval from resampling whole tapes
   (`--bootstrap N`, default 2000; `0` turns it off). Bins on one tape aren't independent, so
   tapes are the unit. Tags get `p@1_lo/hi`, the base rate's interval and `P(>base)`.
   Configurations get `obj_lo/hi`, and `delta` (objective minus the baseline's on the same
   resample) with `delta_lo/hi` and `P(>0)`. A delta interval that includes 0 hasn't shown the
   configuration is better, whatever its point estimate. On the October 2026 labels,
   `impulsive_heavy` in `tape` mode is +0.036 (−0.001 to +0.076, `P(>0)` 0.97).
5. Bins a configuration targets that nobody has labeled are valued at the control base rate
   and listed in `tag_weights_<MODE>_per_tape.csv`. Any later sheet skips bins already labeled.

   **What another round could change (`--stake`).** Before labeling more, run
   `tag_weights.py <run> --damage MODE --stake [CONFIG ...]`. For each named configuration
   (default: all), it sets that configuration's and the baseline's unlabeled targets all
   damaged or all clean, by group (shared, config only, baseline only), and reports the range
   of objective delta labeling them could produce:
   - `settled-worse`: the delta stays below `--margin` (default +0.02) in every case
   - `settled-better`: it stays above the margin in every case
   - `open`: labeling could go either way

   Settled questions need no more labels. The open configurations' bins go to
   `tag_weights_<MODE>_stake_bins.csv`. Build a sheet of just those with
   `label_sheet.py <run> --bins FILE --name NAME`: the listed bins plus `--controls` (default
   1) per tape, blind IDs prefixed `S`. The bound is a worst case, so a configuration that moves
   many tapes onto unlabeled bins is always open: name the configurations the decision is
   between. On the round-2 labels, the legal-floor configurations come out `settled-worse`,
   and `--stake impulsive_heavy` in `tape` mode lists 65 bins.
6. Focused rounds: `label_sheet.py <run> --round legality --name round2 --regions FILE` builds a
   ~6-bin-per-tape sheet for the legal-floor luma measures (`under_black`, `over_white`, added to
   every bin by `_selection.add_legal_floor_columns`): 1 control, each measure's top bins, the
   bins the `LEGALITY_CONFIGS` would target, and bins from `FILE` (`video_id,tc_in,tc_out`
   regions an operator flagged as missed; a DF tape's timecodes may be typed with `:`).
   Blind IDs carry an `L` prefix (`--id-prefix`).

`labels/seed_labels.csv` holds earlier operator spot checks (same columns plus `bin_start` in
seconds). Labels are keyed by `(video_id, bin_start)`, so they carry over to future runs.

### Checking labels

`labels.py` holds the label schema and the one loader both tools use, so they agree on what is
labeled: a row with both `defect` and `severity` filled. It reads every `labels/sheet_*.csv`
through the `key_*.json` of the same name, plus `seed_labels.csv`, and checks them:

```bash
python3 dev_tools/labels.py     # every problem, then damaged/clean counts and base rate per mode
```

- **Errors** stop `label_sheet.py` and `tag_weights.py` until fixed:
  - a defect or origin that isn't on the list (this catches notes typed into `origin`)
  - a severity other than 0, 1 or 2
  - a defect without a severity, or the other way round
  - `none` joined with a defect
  - a `blind_id` missing from the key, or a `video_id` that disagrees with the key (rows
    reordered in Excel)
- **Warnings** are reported and the tools go on:
  - a sheet with no key (skipped)
  - `none` at severity 1 or 2
  - a damage finding with no origin on a sheet that has an origin column
  - the same finding twice for one bin
  - a bin labeled differently in two files. The later file wins: the seed file first, then
    sheets in name order.

Backups only count if they match `sheet_*.csv` and have a matching key, so
`sheet_round1_blank.csv.bak` and `.xlsx` copies are ignored.

## Video dropout (detector study, November 2026)

Tape dropout (short white or black horizontal lines from signal loss) can't be found in a
QCTools report. Within a tape, frame-wide signalstats ranked labeled dropout bins no better than
chance, and the top TOUT frames were dissolves. So the detector reads the picture
(`src/AV_Spex/checks/video_dropout_detection.py`), and this study labels **events**, not bins.

1. **Scan.** `dropout_scan.py <run> VIDEO...` scans whole tapes at loose thresholds, about 0.2×
   real time per tape, run in parallel with `-j`. Every run it finds is cached under
   `<package>/dropout_scan/` (`scan.json`, `streaks.csv.gz`, `frames.csv.gz`). The scan matches
   videos to packages by file name, so local copies in `sample_files/jpc` work.
2. **Sheet.** `dropout_sheet.py <run> --name NAME --tapes ID...` writes
   `labels/events_<NAME>.xlsx`, `events_key_<NAME>.json` and `events_<NAME>_images/`.
   - **Rows, `--mode bands`** (pilot1). Candidates are sampled across three score bands and both
     polarities, plus random controls, and every row looks the same.
   - **Rows, `--mode filtered`** (pilot2 on). Per tape, `--passed` frames are drawn uniformly
     from those with a run passing `video_dropout_detection.passes_filter()`. That samples what
     the detector would report, so the verdicts estimate its real precision. Two more pools show
     what the filter throws away: `--near-miss` frames (pilot3 on) pass symmetry and recurrence
     but fail the opposite-field band, and `--rejected` frames fail the rest. Frames on an
     earlier `events_key_*.json` aren't asked about again. The key records each run's
     `symmetry`, `recurrence` and `field_ratio`, so the thresholds can be retuned from the
     verdicts, and each row's `flag_density`: how many other frames the filter flags within 1, 5
     and 30 seconds. That tests whether true dropout sits in denser stretches of flags than
     false alarms do, which reporting passages instead of frames would depend on.
   - **Image.** Previous, this and next frame, cropped around the box and enlarged without
     smoothing, above the whole frame.
   - **Columns.** `verdict`, `polarity`, `visibility`, `location` (new from pilot2), `cause`,
     `dropout_elsewhere` and `reviewer` are dropdowns. The `instructions` tab defines them.
   - **Seek check.** The tool re-decodes and re-scans each candidate frame. A candidate it can't
     find again is reported as a bad seek.
3. **Tune.** Tuning is offline, from the cached runs against the verdicts.
   - **Pilot1** (70 candidates, 11 dropout) gave the event filter in `video_dropout_detection.py`:
     - **Symmetry.** Spatial and temporal contrast within 10% of each other.
     - **No recurrence.** No run nearby in frames 2–15 either side.
   - **Pilot2** measured that filter's precision on the frames it would actually report: 10 of
     78 (0.13). The labels pointed to a third test, now in the filter:
     - **Field ratio.** Dropout sits on one field's line, so its contrast against the
       opposite-field lines is about equal to its other contrasts (ratio 0.9–1.2). Content is
       in both fields. The band was chosen after seeing the labels.
   - **Pilot3** (60 passed, 20 near-miss, 10 controls) measured those three tests out of
     sample: 33 of 55 (0.60), and no dropout among the near-misses. Most false reports were short,
     faint motion edges, which gave the fourth test:
     - **Score.** Length × the weaker contrast, at least 400. Chosen after seeing the labels.
4. **Confirm.** The operating point is frozen (`EventFilter` defaults, pinned by a test) and
   confirmed on tapes the study has not seen. `studies/2026-11_video_dropout/plan.md` in the
   corpus states the decision and the adopt bar before that sheet is built.
   - **Intake.** `corpus.py import --run-id gb-42162-heldout-2026-11 <vendor folder>` takes the
     vendor's existing AV Spex outputs as a run, so nothing is re-run and the saved config is
     untouched. `dropout_scan.py` then reads the videos where they are.
   - **Sheet.** `--low-score` adds frames that fail only the score cut, to see what it loses.
   - **Result.** 69 of 126 reported frames were dropout (0.55): a NO-GO for the four thresholds.
5. **Round 2: weigh the measurements together.** The judged runs now double as training data.
   A logistic model over ten run and frame measurements (frozen in
   `video_dropout_detection.MODEL_*`, fitted by the study's `scripts/fit_event_model.py`) scores
   each run the filter passes; `--passed` samples frames it reports and `--model-rejected` the
   ones it drops. It is confirmed on tapes neither the filter nor the model has seen.

`labels.py` reads only `sheet_*.csv` (bin labels), so the `events_*` files don't affect it.
