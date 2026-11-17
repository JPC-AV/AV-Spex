# Replays

Commands are shown as `python3 dev_tools/<script>.py`; each is also an `avspex-dev` command (`avspex-dev -h` lists them, and the table in [the README](../README.md#commands) maps one to the other).

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

## Detector replay

`avspex-dev replay detector` answers "what would this change to a detector do?" without a
collect. It re-runs the detectors that read the QCTools report, under the code as it is now,
from the sidecars a run kept, then scores the result against the confirmed cases. The whole
106-tape baseline run takes about two minutes on eight cores; a collect of it takes most of a
day.

```bash
avspex-dev replay detector                                  # latest run, all four detectors
avspex-dev replay detector --detector bars --truth-only     # the fast loop while tuning
avspex-dev replay detector --tapes JPC_AV_02678 -v          # one tape, listing what changed
```

- **What is replayed.** `bars`, `chroma_phase`, `audio_dropout`, `audible_timecode` by default. The replay
  calls the real `run_qctparse()` on the stored report and the real
  `merge_head_bars_consensus()`, so a change anywhere in those is picked up, wiring included.
- **What is frozen.** CLAMS reads the video, so its bars and tone spans and its SSIM scores are
  read back from the run. **A change to the CLAMS detectors, or to how
  `process_qctools_output()` runs them, is not replayed**: collect again for that. The bars
  arbiter uses the recorded SSIM scores; if a change makes it ask about a span they do not
  cover, it scans the video when the drive is mounted, and otherwise says so and keeps the span
  (no evidence is not a rejection).
- **What cannot be replayed.** `tone_leak` and `dropped_samples` read the video itself.
- **`duplicate_frames` can be, with the drive mounted.** Ask for it by name
  (`--detector duplicate_frames`); it is not in the default set. Its candidates come from the
  stored QCTools report and each is confirmed against the frames, as in the app, at 5 to 10 s a
  tape. A tape whose video is not on a mounted drive scores as not run. The black segments it
  avoids are read back from the run; the bars are the replayed consensus.
- **Trying a setting.** `--set KEY=VALUE` (repeatable) lays a checks setting over the run's
  config, e.g. `--set outputs.frame_analysis.duplicate_min_run_length=2`. The replay is kept
  under a name ending `-set<hash>`, and `run.json` in it records the settings. This is also how
  to replay a *new bundled default*: the run's own saved value otherwise wins.
- **Config.** The run's saved config, not the one AV Spex has saved now. Nothing is saved.
- **The check that makes it believable.** "Reproduces the run?" counts the tapes whose events
  come back identical. Under the code the run was collected with it must be all of them (it is,
  for `bd5014b-cb355033`: chroma phase, audio dropout and audible timecode reproduce on every
  tape). After a change it is the list of tapes the change moved.
- **Reading the result.** Truth rows are the measurement: `better` and `WORSE` rows are what the
  change did to confirmed cases. An *unjudged* event that is no longer reported may be a fix or
  a new miss; `-v` lists those so they can be reviewed.
- **Held-out tapes** are replayed (computing is not looking) but left out of everything printed
  for the detector they are reserved for.
- **Fresh tapes** (never looked at for the detector) are counted, but `-v` does not list their
  events unless `--list-fresh` is given. Reading that list uses the tapes up as a test, so draw
  the held-out set first (`avspex-dev ledger draw`).
- **Where it goes.** `out/replays/replay-<run>-<code>/` in the corpus, untracked. `<code>` is
  the commit plus a hash of uncommitted `src/` changes, so each state of the code gets its own
  replay and re-running skips tapes already done (`--force` redoes them; use it after editing
  the replay itself, which is outside `src/`). The directory is run-like, so the other tools
  take its name wherever they take a run:

  ```bash
  avspex-dev score replay-bd5014b-cb355033-e26018a --detector bars
  avspex-dev sheet replay-bd5014b-cb355033-e26018a --heldout --detector bars   # held-out test form
  ```

A confirmed fix still ends with a held-out test on tapes nobody tuned against. The replay makes
the tuning loop fast; it does not replace that test.
