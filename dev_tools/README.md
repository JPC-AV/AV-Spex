# dev_tools: run AV Spex over many tapes and check what it reports

Developer and admin tooling. It is not part of the app: the package only includes `src/`, the
PyInstaller build only bundles what the launcher imports, and pytest only collects `tests/`.

The idea in three lines:

1. **Collect.** Run AV Spex over a set of tapes and keep the small sidecars it writes as a *run*.
2. **Review.** Get a spreadsheet of everything the detectors reported, check each against the
   video, and mark it real or not.
3. **Score.** Those verdicts are kept for good, so every later version of a detector is checked
   against them automatically.

Everything goes through one command, `avspex-dev`, and everything is kept in one place, the
**corpus**: a separate git repository the group shares.

- [Setup](#setup)
- [Your first review](#your-first-review)
- [Rules that keep the results honest](#rules-that-keep-the-results-honest)
- [Commands](#commands)
- [Collecting: options and storage](#collecting-options-and-storage)
- [Scorecard](#scorecard-known-cases-per-detector), [Tape ledger](#tape-ledger-dev-vs-held-out),
  [Tables](#tables-aggregate)
- Further reading in `docs/`: [corpus layout](docs/corpus_layout.md), [replays](docs/replays.md),
  [fault injection](docs/fault_injection.md), [weighing tags](docs/tag_weighing.md),
  [video dropout study](docs/video_dropout_study.md)

## Setup

You need the AV Spex repository installed from source, with its external tools (FFmpeg,
MediaInfo, ExifTool, MediaConch, QCTools; see the main README).

```bash
git clone https://github.com/JPC-AV/JPC_AV_videoQC.git
cd JPC_AV_videoQC
pip install -e ".[dev]"                       # editable: runs are tied to this code
dev_tools/avspex-dev setup --clone <URL of the shared corpus repository> --link
```

`setup` asks where to put the corpus and for your name (it goes on the verdicts you record),
then runs `doctor`. `--link` puts `avspex-dev` on your PATH, next to `python`; without it, call
`dev_tools/avspex-dev`. With no `--clone`, `setup` creates an empty corpus instead, or adopts
the directory you point `--corpus` at. It is safe to run again.

```
$ avspex-dev doctor
Install
  ok   AV Spex runs from this repository: /Users/you/JPC_AV_videoQC
  ok   openpyxl (review workbooks)
  ok   external tools: av-spex, ffmpeg, ffprobe, qcli, mediainfo, exiftool, mediaconch
  ok   avspex-dev is on PATH
Settings
  ok   corpus: /Users/you/avspex_corpus (settings file)
  ok   reviewer: Your Name
Corpus
  ok   up to date with the remote
  ok   runs: 3: 0 with sidecars here, 3 records only
  warn no run can be scored on this machine yet
       -> avspex-dev collect --baseline /path/to/tapes
  ...
AV Spex config
  warn the saved config differs from the baseline: 4 setting(s)
       -> avspex-dev collect --baseline imports the baseline (your config is backed up first)
```

Every line that is not `ok` says what to do. Run `doctor` whenever something seems off.

**Settings** are three values, saved by `setup` in `dev_tools.json` in the AV Spex config
directory. An environment variable overrides each: `AVSPEX_CORPUS`, `AVSPEX_REVIEWER`,
`AVSPEX_STAGING` (only for [staged collection](#staged-collection-slow-or-read-only-drives)).

## Your first review

Tapes are *packages*: a folder named `JPC_AV_<digits>` holding the `.mkv`. Point the tools at a
package or at any directory with packages somewhere under it.

**1. Get up to date.**

```bash
cd <corpus> && git pull
```

**2. Collect.** This runs AV Spex on each tape, which takes roughly a fifth of the tape's
running time with every detector on. Leave it overnight for a shelf of tapes.

```bash
avspex-dev collect --baseline -l "Ada's first batch" /Volumes/TAPES/batch_12
```

- `--baseline` imports the group's AV Spex config (`configs/baseline_checks.json` in the
  corpus) so that everyone's results are comparable. Your current config is backed up first
  and the command to restore it is printed.
- AV Spex writes its usual outputs into each package folder. The sidecars are copied into the
  corpus as a run.
- Stop it any time. The same command again picks up where it left off.
- `-n` lists what would be collected and stops.

```
Run: bd5014b-cb355033   corpus: /Users/you/avspex_corpus   baseline config
12 package(s) found, 0 already collected, 12 to do

=== [1/12] JPC_AV_01056  (21:14)
...
Run bd5014b-cb355033: 12 collected, 0 failed
Next: avspex-dev sheet bd5014b-cb355033
```

The run ID is `<code commit>-c<config hash>`. You rarely need to type it: commands that take a
run default to `latest`, the most recent run collected on this machine.

**3. Make a review sheet.**

```bash
avspex-dev sheet
```

```
run: bd5014b-cb355033 (latest)

41 unjudged event(s) from bd5014b-cb355033
    -> /Users/you/avspex_corpus/truth/review_bd5014b-cb355033_Ada_2026-11-16.xlsx

    audio_dropout         9
    audible_timecode      0
    tone_leak             2
    chroma_phase         14
    bars                 12
    duplicate_frames      3
    dropped_samples       1

Fill in the verdict column (see the instructions tab), then:
    avspex-dev import-sheet /Users/you/avspex_corpus/truth/review_bd5014b-cb355033_Ada_2026-11-16.xlsx
```

One row per thing a detector reported that nobody has judged yet. `--detector bars` limits it
to one detector (repeatable). `--max-per-tape 10` keeps a tape that reports hundreds of events
from being most of the sheet: it contributes a random 10 (the seed is printed), and the rest
stay unjudged.

**4. Fill it in.** Open the workbook (the `instructions` tab repeats this). For each row, open
the video at `tc_in`. The timecodes are the file's own, as your NLE or player shows them.

| detector | video_id | tc_in | tc_out | seconds | verdict | note |
|---|---|---|---|---|---|---|
| bars | JPC_AV_01056 | 00:00:02:10 | 00:00:58:04 | 55.8 | **present** | |
| audio_dropout | JPC_AV_01056 | 00:14:31:02 | 00:14:31:20 | 0.6 | **absent** | fade to silence between segments |
| chroma_phase | JPC_AV_01056 | 00:21:07:15 | 00:21:09:00 | 1.5 | | |

- **present**: it is really there.
- **absent**: you looked, and it is not. A false alarm.
- **blank**: not judged. It will come up again on a later sheet, and is never counted against
  the detector. Leave blank anything you did not check or cannot call.
- To record something the detector missed, add a row with the detector, tape, span and
  `present`.

**5. Import your verdicts.**

```bash
avspex-dev import-sheet <the .xlsx>
```

```
bars               +11 present  +1 absent -> bars.csv
audio_dropout      +4 present  +5 absent -> audio_dropout.csv

29 row(s) added, 12 left blank (still unjudged)
```

Each verdict becomes a row in `truth/<detector>.csv` with your name and the date. Importing the
same sheet twice adds nothing twice.

**6. See the score.**

```bash
avspex-dev score
```

Per detector: `hit` / `MISS` for the present rows, `clean` / `FALSE_ALARM` for the absent ones.

**7. Share it.**

```bash
cd <corpus>
avspex-dev doctor                 # validates the truth files
git add -A && git commit -m "Reviewed batch 12 (bars, audio dropout)" && git push
```

Git carries your verdicts and the run's records, not its sidecars (5 to 40 MB per tape). Those
stay on your machine. Colleagues see your run in `avspex-dev runs` as `records only`.

## Rules that keep the results honest

- **Do not edit `src/` or the AV Spex config while a collect is running.** The run ID is the
  code plus the config. The remaining tapes stop rather than be filed under the wrong run.
- **Use the baseline config unless you mean not to.** Two people on different configs produce
  two runs that cannot be compared. `doctor` says whether yours matches.
- **Unjudged is not wrong.** An event nobody has checked is neither a hit nor a false alarm.
  Do not guess to empty a sheet.
- **"Did not run" is not "found nothing".** A detector whose sidecar is missing scores
  `not_run`, never `clean`.
- **Looking at a tape's results uses it up as a test.** Before tuning a detector, set aside
  held-out tapes you have not looked at ([Tape ledger](#tape-ledger-dev-vs-held-out)).
- **Pull before you review, push when you are done.** Appended verdicts from two people merge
  without conflict; edits to existing rows may not.

## Commands

`avspex-dev COMMAND -h` shows a command's options. Each command runs the script on the right
with the same arguments, so the script form still works.

| Command | What it does | Script |
|---|---|---|
| `setup` | Clone or create the corpus, save your settings | |
| `doctor` | Check the install, corpus, git state, config and truth files | |
| `collect SOURCE...` | Run AV Spex over tapes and keep the results as a run | `collect_one.sh` per tape; `collect.sh` with `--stage` |
| `runs` | List runs: ID, date, package count, `local` or `records only`, label | `corpus.py list` |
| `sheet [RUN]` | Review workbook of unjudged events | `scorecard.py RUN --sheet` |
| `import-sheet XLSX` | File a filled-in workbook's verdicts | `scorecard.py --import-sheet` |
| `score [RUN]` | Score detectors against confirmed cases; `--compare RUN_B`, `--unjudged DETECTOR`, `--check` | `scorecard.py` |
| `table RUN...` | One row per tape; `--diff A B` for what changed | `aggregate.py` |
| `ledger ...` | Which tape was used for what; held-out draws | `ledger.py` |
| `labels` | Validate the bin label sheets | `labels.py` |
| `label-sheet RUN ...` | Build a bin label sheet | `label_sheet.py` |
| `inject FAULT [RUN]` | Put known faults into clips of real tapes and count what the detector finds: `dropped-samples`, `tone-leak`, `duplicate-frames`. Measures misses, which sheets cannot ([fault injection](docs/fault_injection.md)) | `inject.py` |
| `replay detector [RUN]` | Re-run bars, chroma phase, audio dropout and audible timecode from a run's sidecars under the current code, and score them: minutes, no tapes needed (duplicate frames too, with the drive mounted; `--set` tries a config value) ([replays](docs/replays.md#detector-replay)) | `replays/detector.py` |
| `replay harness\|period-grid\|bin-table\|tag-weights ...` | Offline replays of period selection | `replays/*.py` |
| `dropout scan\|sheet\|export ...` | Video dropout study tools | `dropout_scan.py`, `dropout_sheet.py`, `event_labels.py` |
| `corpus ...` | `run-id`, `import`, and the pieces `collect` calls | `corpus.py` |

`RUN` is a run ID, a unique prefix of one (`bd50`), or `latest`.

## Collecting: options and storage

```bash
avspex-dev collect [-l LABEL] [--baseline | -c FILE] [-g] [-i] [-n] SOURCE...
```

- **Config.** The run uses the config AV Spex currently has saved. `--baseline` or `-c FILE`
  imports one first, after backing up yours. AV Spex flags cannot be passed, because they
  would change the config behind the run ID.
- **Resuming.** Same code and config, same run: collected tapes are skipped. Any change to
  `src/` or the config starts a new run and leaves the old one alone.
- **Failed tapes.** A tape whose AV Spex run failed is still collected for inspection, but is
  not counted as done. The next collect retries it.
- **Ground truth.** `-g` adds a whole-file active-area signalstats pass per tape. Only the
  period replay needs it. It takes roughly 5–15 % of the tape's length.
- **Images.** `-i` keeps thumbnails and pictures. Media and HTML reports are never kept.
- **Disk.** A tape costs 5–40 MB in the corpus, mostly the QCTools report. An existing QCTools
  report in the package is reused, not regenerated.
- **Folder name vs file ID.** They can differ (`JPC_AV_20241/` holds `JPC_AV_02041_*`). The
  tools read the ID from the files.
- **Sleep.** `collect` keeps the Mac awake (`caffeinate`) for as long as it runs.
- **Results already on a drive.** `avspex-dev corpus import --run-id NAME <folder>` takes
  existing AV Spex outputs as a run without re-running anything (see
  [corpus layout](docs/corpus_layout.md)).

### Staged collection (slow or read-only drives)

By default AV Spex runs on the packages where they are. When they sit on a slow spinning disk,
or on a drive you must not write to, add `--stage`:

```bash
avspex-dev collect --stage ~/staging -l "vendor drive" /Volumes/DRIVE/vendor_a
avspex-dev collect --stage ~/staging -w ...       # -w: leave the originals exactly as they are
```

This hands off to `collect.sh` and `cycle_avspex_batches.sh`: packages are copied to the
staging directory in batches that fit (`-b GB` largest batch, `-r GB` free space to keep),
AV Spex runs on the copies, new outputs are copied back to the drive (unless `-w`), and the
space is freed. `avspex-dev setup --staging DIR` saves a default directory, after which a bare
`--stage` is enough. Staging on the same APFS volume as the sources is a clone: instant and
free.

- **Same folder name in two sources.** They must go in separate invocations (the cycle script
  refuses duplicates). They share the run, so the second invocation skips them.
- **Time Machine local snapshots** keep deleted staged files on disk, so free space stops
  growing. Run `tmutil deletelocalsnapshots /`, or turn Time Machine off for the run.
- **Progress** is kept under `<staging>/.avspex_cycle/<run_id>/` (`done.txt`, `failed.txt`,
  logs).

## Scorecard (known cases per detector)

`table --diff` shows that a detector's output changed. The scorecard says whether the change
was right, by checking each run against cases an operator has confirmed.

```bash
avspex-dev score [RUN]                        # every detector with a truth file
avspex-dev score RUN_A --compare RUN_B        # truth rows whose outcome changed
avspex-dev score [RUN] --unjudged bars        # reported events no truth row covers, as CSV rows
avspex-dev score [RUN] --out scorecard.csv    # one row per truth row, with its outcome
avspex-dev score --check                      # validate the truth files
avspex-dev sheet [RUN] [--detector D] [-o XLSX]
avspex-dev import-sheet XLSX
```

- **Truth.** `truth/<detector>.csv` in the corpus, columns
  `video_id,tc_in,tc_out,verdict,source,note`. Times are the file's own timecode as the NLE
  shows it, or plain elapsed seconds. Both blank means the whole tape. You can edit these files
  by hand; `score --check` validates them.
  - `present`: a real event. The detector should report something overlapping the span.
  - `absent`: an operator looked and there is nothing to report, usually a past false alarm.
    The detector should report nothing overlapping the span.
- **Outcomes.** `hit` / `MISS` for present rows, `clean` / `FALSE_ALARM` for absent rows.
  `not_run` means the run has no output from that detector for the tape (sidecar missing, step
  off, or the detector said it could not measure). It is never counted as clean or as a miss.
- **Unjudged events.** Truth is never complete, so an event no row covers is not a false
  alarm. Every row added makes the scorecard stricter for good.
- **Detectors.** `audio_dropout`, `audible_timecode`, `tone_leak`, `chroma_phase`, `bars`,
  `duplicate_frames`, `dropped_samples`. To add one, write a function in `scorecard.py` that
  returns the detector's events as `(start, end)` seconds, or `None` when it did not run, and
  add it to `DETECTORS`.
- **Start timecodes** are recorded per tape when it is collected (`package.json`). An entry in
  `labels/timecode.json` overrides that, which is where to correct one by hand. A tape with
  neither (runs collected before November 2026) is read as starting at zero.
- **Records-only runs.** A run collected on another machine cannot be scored here: the tools
  say so and stop, rather than report every detector as `not_run`.

## Tape ledger (dev vs held-out)

A detector tuned and tested on the same tapes looks better than it is. The ledger records every
use of a tape, per topic (a detector or a study), in `ledger.csv` in the corpus:

```bash
avspex-dev ledger                              # per topic: dev / spent / reserved / fresh
avspex-dev ledger tapes --topic bars           # one line per tape
avspex-dev ledger fresh bars --run RUN         # tapes never used for bars (--untouched: for anything)
avspex-dev ledger draw bars 15 --run RUN       # hold out 15 fresh tapes, picked by a recorded seed
avspex-dev ledger add bars dev 02213 02216 --source "bars retune" --note "..."
avspex-dev ledger check                        # validate; flag tapes that were dev before held out
```

- **dev**: someone looked at the tape's results for the topic (labeled, reviewed, tuned). It can
  never again be a fair test of it. Tapes with rows in `truth/<detector>.csv` are dev for that
  detector automatically; everything else needs a row.
- **heldout**: set aside as a test, to be scored once against a frozen detector. Until a later
  `dev` row says the test is over, the tape is **reserved** and `sheet` / `score --unjudged`
  leave it out. Afterwards it is **spent**: usable for development, never again as a test.
  `sheet --heldout --detector D` builds the form for scoring the test itself.
- **The order that keeps a test fair**: `draw` the held-out tapes before looking at them, tune on
  the rest, freeze, score the held-out tapes once, then `add ... dev` to release them.
- Collecting a run does not use a tape. Looking at its results does: reviewing a new run's
  sheet makes every tape on it dev for that detector, so draw the held-out set first.

## Tables (aggregate)

```bash
avspex-dev table RUN --out table.csv           # one row per tape
avspex-dev table RUN_A RUN_B --out both.csv    # stacked
avspex-dev table --diff RUN_A RUN_B            # what changed, per tape
avspex-dev table --diff A B --only '^fa_'      # just frame-analysis columns
avspex-dev table --columns RUN                 # what columns exist
```

CSVs go to the current directory unless given `--out`, so run from somewhere other than the
repo root or pass a path (`out/` in the corpus is ignored by git).

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

## Sharing sidecars between machines

Only needed when someone has to score or replay a run they did not collect. Copy the run's
packages into the same place in their corpus:

```bash
rsync -a other-mac:<corpus>/runs/<run_id>/packages/ <corpus>/runs/<run_id>/packages/
```

`avspex-dev runs` then shows the run as `local`.
