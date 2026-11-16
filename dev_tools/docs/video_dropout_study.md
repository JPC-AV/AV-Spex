# Video dropout (detector study, November 2026)

Commands are shown as `python3 dev_tools/<script>.py`; each is also an `avspex-dev` command (`avspex-dev -h` lists them, and the table in [the README](../README.md#commands) maps one to the other).

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