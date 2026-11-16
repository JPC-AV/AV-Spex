# The corpus: layout, run IDs, what git tracks

Commands are shown as `python3 dev_tools/<script>.py`; each is also an `avspex-dev` command (`avspex-dev -h` lists them, and the table in [the README](../README.md#commands) maps one to the other).

The corpus is wherever `avspex-dev setup` put it (`avspex-dev doctor` prints the path; `$AVSPEX_CORPUS` overrides it):

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
truth/                                operator-confirmed cases per detector (see Scorecard)
ledger.csv                            which tape was used for what (see Tape ledger)
configs/                              AV Spex configs used for runs (baseline_checks.json)
notes/                                study write-ups (by hand; no tool reads or writes it)
studies/<date>_<slug>/                a study's plan, manifest and one-off scripts
```

- **Sidecars stay where they were collected.** Git carries each run's records (`run.json`,
  `package.json`) but not the sidecars. A run collected on another machine shows as
  `records only`, and the tools refuse to score it rather than report every detector as not
  run. `runs/<run_id>/sessions.log` (who collected when) is per machine and untracked.
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
- **Listing runs.** `python3 dev_tools/corpus.py list` (`avspex-dev runs`) shows each run's ID,
  date, package count, whether its sidecars are on this machine (`local` / `records only`) and
  label. `corpus.py run-id` prints the ID the current code and config would get,
  without creating anything. The other subcommands (`init-run`, `check-run`, `done`,
  `collect-package`) are what `collect.sh` calls; `import` is covered under Replays.
- **Output location.** `aggregate.py`, `period_grid.py`, `bin_table.py` and `tag_weights.py`
  write their CSVs to the current directory unless given `--out` / `--out-dir`, so run them
  from somewhere other than the repo root or pass a path. Only `label_sheet.py` writes into the
  corpus.