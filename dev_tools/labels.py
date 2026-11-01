#!/usr/bin/env python3
"""
labels.py: operator labels, their schema, loading and checks.

label_sheet.py writes sheets and tag_weights.py scores against them; both read labels through
this module, so they agree on what is labeled. Run it on its own after editing a sheet:

    python3 dev_tools/labels.py

It prints every problem, then each damage mode's counts, and exits 1 on an error. The tools run
the same check when they load labels and stop at an error.

Files, in $AVSPEX_CORPUS/labels/:
  sheet_<name>.csv   read through key_<name>.json (blind_id -> video_id, bin_start, selectors)
  seed_labels.csv    earlier spot checks, with video_id and bin_start in place of blind_id
Labels are keyed by (video_id, bin_start), so they carry across runs.

A filled row is one finding:
  defect    one of DEFECTS; join several with '+' when they share a severity and origin
  severity  0 (nothing worth a period), 1 (visible) or 2 (obvious)
  origin    one of ORIGINS, joined with '+' when they can't be told apart; blank for none
A bin whose findings differ in severity or origin gets one row per finding: copy the row and
keep its blind_id (e.g. obvious damage baked into the source, plus a light tape dropout).
A blank row is unlabeled.

Errors (the tools stop):
  - a defect or origin token not in DEFECTS / ORIGINS (also catches notes in the wrong column)
  - a severity other than 0, 1 or 2
  - a defect without a severity, or a severity or origin without a defect
  - 'none' joined with a defect
  - a blind_id missing from the key, or a video_id that disagrees with it (rows moved)
Warnings:
  - a sheet without its key (skipped)
  - 'none' at severity 1 or 2
  - a damage finding with no origin, on a sheet that has an origin column
  - the same finding twice for one bin in one file
  - a bin labeled differently in two files: the later file wins (the seed file first, then
    sheets by name)
"""
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from corpus import CORPUS

LABELS = CORPUS / "labels"
SEED_FILE = "seed_labels.csv"

DEFECTS = ("none", "dropout", "head_switching_tracking", "sync_loss_roll", "illegal_levels",
           "chroma_error", "noise", "signal_loss_freeze", "graphics_slate_only", "other")
# tape: this transfer/playback (dropout, tracking, sync). content: in the program as made (sub-black
# clothing, a still photo, a graphic with illegal levels). source: baked into dubbed or archival
# material (generation loss, head switching recorded in).
ORIGINS = ("tape", "content", "source")
SHEET_COLUMNS = ("video_id", "tc_in", "tc_out", "blind_id", "defect", "severity", "origin", "notes")

CLEAN_DEFECTS = {"none", "graphics_slate_only"}
# Round 1 had no origin column: there, these defects stand in for origin 'tape'
TAPE_DEFECTS = {"dropout", "sync_loss_roll", "signal_loss_freeze"}
DAMAGE_MODES = ("labeled", "severe", "tape", "illegal")


@dataclass(frozen=True)
class Finding:
    defects: frozenset
    severity: int
    origin: frozenset = frozenset()     # empty when not given

    def damaged(self, mode):
        """Whether this finding counts as damage under a --damage mode (see tag_weights.py)."""
        if mode == "illegal":
            return self.severity >= 1 and "illegal_levels" in self.defects
        if mode == "tape":
            if self.severity < 1:
                return False
            if self.origin:
                return "tape" in self.origin
            return bool(self.defects & TAPE_DEFECTS)
        floor = 2 if mode == "severe" else 1
        return self.severity >= floor and not self.defects <= CLEAN_DEFECTS

    def __str__(self):
        origin = f" ({'+'.join(sorted(self.origin))})" if self.origin else ""
        return f"{'+'.join(sorted(self.defects))}@{self.severity}{origin}"


@dataclass
class Label:
    findings: list = field(default_factory=list)
    control: bool = False
    source: str = ""        # file the label was read from

    @property
    def defects(self):
        return set().union(*(f.defects for f in self.findings))

    @property
    def severity(self):
        return max(f.severity for f in self.findings)

    def damaged(self, mode):
        return any(f.damaged(mode) for f in self.findings)

    def damaged_defects(self, mode):
        """Defect types of the findings that count as damage under `mode`."""
        return set().union(*(f.defects for f in self.findings if f.damaged(mode))) - CLEAN_DEFECTS

    def __str__(self):
        return ", ".join(str(f) for f in self.findings)


@dataclass
class Problem:
    level: str      # 'error' or 'warning'
    where: str      # file[:line]
    message: str

    def __str__(self):
        return f"{self.level}: {self.where}: {self.message}"


def _tokens(text):
    return frozenset(t.strip().lower() for t in text.split("+") if t.strip())


def _finding(row, where, has_origin, problems):
    """The row's Finding, None for a blank row or one with an error (recorded in problems)."""
    defect = (row.get("defect") or "").strip()
    severity = (row.get("severity") or "").strip()
    origin = (row.get("origin") or "").strip()
    if not (defect or severity or origin):
        return None
    errors = []
    if not defect or not severity:
        errors.append("incomplete: defect and severity go together"
                      + ("" if defect or severity else " (origin given alone)"))
    defects, origins = _tokens(defect), _tokens(origin)
    bad = sorted(defects - set(DEFECTS))
    if bad:
        errors.append(f"unknown defect {', '.join(map(repr, bad))}")
    if "none" in defects and len(defects) > 1:
        errors.append(f"'none' joined with a defect ({defect!r})")
    bad = sorted(origins - set(ORIGINS))
    if bad:
        shown = origin if len(origin) <= 40 else origin[:37] + "..."
        errors.append(f"unknown origin {shown!r} (expected {', '.join(ORIGINS)}; notes go in notes)")
    sev = None
    if severity:
        try:
            sev = float(severity)
        except ValueError:
            pass
        if sev not in (0, 1, 2):
            errors.append(f"severity {severity!r} is not 0, 1 or 2")
    if errors:
        problems.extend(Problem("error", where, e) for e in errors)
        return None
    finding = Finding(defects, int(sev), origins)
    if defects == {"none"} and finding.severity >= 1:
        problems.append(Problem("warning", where, f"'none' at severity {finding.severity}"))
    if has_origin and not origins and not defects <= CLEAN_DEFECTS:
        problems.append(Problem("warning", where, f"{defect} has no origin; tape mode falls back "
                                                   f"to the defect type"))
    return finding


def _open_csv(path):
    # utf-8-sig: Excel may add a byte-order mark, which would rename the first column
    return open(path, newline="", encoding="utf-8-sig")


def _sheet_entries(path, problems):
    """(where, video_id, bin_start, row, control) per row of a sheet, and whether it has an
    origin column; None when the sheet can't be read through a key."""
    key_path = path.with_name(path.name.replace("sheet_", "key_", 1)).with_suffix(".json")
    if not key_path.exists():
        problems.append(Problem("warning", path.name, f"no {key_path.name}; sheet skipped"))
        return None
    key = json.loads(key_path.read_text())
    entries = []
    with _open_csv(path) as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        missing = [c for c in ("blind_id", "defect", "severity") if c not in columns]
        if missing:
            problems.append(Problem("error", path.name, f"missing column(s) {', '.join(missing)}"))
            return None
        for n, row in enumerate(reader, 2):
            where = f"{path.name}:{n}"
            filled = any((row.get(c) or "").strip() for c in ("defect", "severity", "origin"))
            blind = (row.get("blind_id") or "").strip()
            k = key.get(blind)
            if k is None:
                if filled:
                    problems.append(Problem("error", where, f"blind_id {blind!r} is not in {key_path.name}"))
                continue
            vid = (row.get("video_id") or "").strip()
            if vid and vid != k["video_id"]:
                problems.append(Problem("error", where, f"video_id {vid} but the key has {blind} on "
                                                        f"{k['video_id']}: rows moved?"))
                continue
            entries.append((where, k["video_id"], float(k["bin_start"]), row,
                            "control" in k["selectors"]))
    return entries, "origin" in columns


def _seed_entries(path, problems):
    entries = []
    with _open_csv(path) as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        for n, row in enumerate(reader, 2):
            try:
                b = float(row["bin_start"])
            except (KeyError, TypeError, ValueError):
                problems.append(Problem("error", f"{path.name}:{n}", "no numeric bin_start"))
                continue
            entries.append((f"{path.name}:{n}", (row.get("video_id") or "").strip(), b, row, False))
    return entries, "origin" in columns


def read(directory=None):
    """({(video_id, bin_start): Label}, [Problem]) from every label file, without stopping at
    errors (a row with one is left out)."""
    directory = Path(directory) if directory else LABELS
    problems, labels = [], {}
    seed = directory / SEED_FILE
    files = ([seed] if seed.exists() else []) + sorted(directory.glob("sheet_*.csv"))
    for path in files:
        first = len(problems)
        _read_file(path, path == seed, labels, problems)
        # in line order within a file; file-wide problems after the lines
        problems[first:] = sorted(problems[first:], key=_line_of)
    return labels, problems


def _line_of(problem):
    _, _, line = problem.where.partition(":")
    return int(line) if line else float("inf")


def _read_file(path, is_seed, labels, problems):
    """Add one file's labels to `labels`, over any earlier file's."""
    got = (_seed_entries if is_seed else _sheet_entries)(path, problems)
    if got is None:
        return
    entries, has_origin = got
    found = {}
    for where, vid, b, row, control in entries:
        finding = _finding(row, where, has_origin, problems)
        if finding is None:
            continue
        label = found.setdefault((vid, b), Label(control=control, source=path.name))
        if finding in label.findings:
            problems.append(Problem("warning", where, f"repeats an earlier row's finding ({finding})"))
            continue
        label.findings.append(finding)
    for k, label in found.items():
        prev = labels.get(k)
        if prev is not None and set(prev.findings) != set(label.findings):
            problems.append(Problem("warning", path.name,
                                    f"{k[0]} @ {k[1]:.0f}s: {label} replaces {prev} from {prev.source}"))
        labels[k] = label


def load(directory=None):
    """The labels, as the tools use them: problems go to stderr, and any error stops the run."""
    labels, problems = read(directory)
    for p in problems:
        print(p, file=sys.stderr)
    errors = sum(p.level == "error" for p in problems)
    if errors:
        sys.exit(f"{errors} error(s) in the label sheets; fix them and rerun "
                 f"(python3 dev_tools/labels.py lists them)")
    return labels


def labeled_bins(directory=None):
    """(video_id, bin_start) of every labeled bin: the ones a new sheet skips."""
    return set(load(directory))


def main():
    labels, problems = read()
    for p in problems:
        print(p)
    errors = sum(p.level == "error" for p in problems)
    controls = [l for l in labels.values() if l.control]
    findings = sum(len(l.findings) for l in labels.values())
    print(f"\n{len(labels)} labeled bins ({findings} findings, {len(controls)} controls) from {LABELS}; "
          f"{errors} error(s), {len(problems) - errors} warning(s)")
    for mode in DAMAGE_MODES:
        dmg = sum(l.damaged(mode) for l in labels.values())
        base = sum(l.damaged(mode) for l in controls) / len(controls) if controls else float("nan")
        print(f"  {mode:<8} {dmg:>4} damaged, {len(labels) - dmg:>4} clean; control base rate {base:.3f}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
