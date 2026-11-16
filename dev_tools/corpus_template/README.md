# AV Spex results corpus

What AV Spex reported about each tape, and what people have confirmed about those reports.
The tools that fill and read it are in the AV Spex repository under `dev_tools/`; start with
`dev_tools/README.md` there.

## What is here

```
runs/<run_id>/        one directory per batch run of AV Spex (one code version + one config)
    run.json          the commit, config hash and tool versions behind the run
    packages/<TAPE>/
        package.json  the record of one tape: video properties, timings, which machine
        ...           the sidecars AV Spex wrote (not in git, see below)
truth/                confirmed cases per detector, one CSV each, plus review workbooks
ledger.csv            which tape has been used for what (development or held-out test)
labels/               label sheets and their keys for the studies
configs/              AV Spex configs used for runs; baseline_checks.json is the shared one
notes/, studies/      write-ups and one-off study material
```

## What git tracks, and what it does not

Git tracks the small things people make: `truth/`, `ledger.csv`, `labels/`, `configs/`,
`notes/`, `studies/`, and each run's `run.json` and `package.json` records.

It does not track the sidecars inside `runs/<run_id>/packages/<TAPE>/` (5 to 40 MB per tape).
They stay on the machine that collected them. So after cloning you can see every run that
exists (`avspex-dev runs` marks them `records only`), but you can only score or build review
sheets from runs collected on your own machine, or copied over by hand:

```bash
rsync -a other-mac:path/to/avspex_corpus/runs/<run_id>/packages/ runs/<run_id>/packages/
```

## The routine

```bash
git pull                              # before you start
avspex-dev collect ... / sheet / import-sheet      # the work
avspex-dev dropout export             # only if you filled in a dropout events workbook
avspex-dev doctor                     # validates truth files and the ledger
git add -A && git commit -m "what you reviewed" && git push
```

`truth/*.csv` and `ledger.csv` merge by keeping both sides' new rows (`.gitattributes`), so
two people adding verdicts at the same time do not conflict. Edit existing rows rarely, and
say so in the commit message when you do.
