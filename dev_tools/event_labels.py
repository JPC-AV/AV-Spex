#!/usr/bin/env python3
"""
event_labels.py: export the verdicts in every labels/events_<NAME>.xlsx to events_<NAME>.csv.

The workbooks embed their images (15-35 MB each), so they are a poor thing to keep under
version control: every saved verdict rewrites the whole file. The CSV holds everything the
operator typed and is what the corpus repository tracks. The workbook stays the form to fill
in. Run this after each labeling session, before committing the corpus:

    python3 dev_tools/event_labels.py            # every workbook
    python3 dev_tools/event_labels.py confirm1   # just one

Rows are written as they are, blank ones included, so the CSV diff shows exactly what a
session changed. A verdict that is not on the list is reported, not dropped.
"""
import csv
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import CORPUS  # noqa: E402

LABELS = CORPUS / "labels"
VERDICTS = ("dropout", "not_dropout", "unsure")   # as dropout_sheet.VERDICTS
SKIP = ("image",)                                 # the embedded picture's (empty) cell


def export(xlsx):
    ws = openpyxl.load_workbook(xlsx, read_only=True)["labels"]
    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    keep = [i for i, h in enumerate(header) if h and h not in SKIP]
    cols = [header[i] for i in keep]
    out, judged, odd = [], 0, []
    for r in rows:
        row = {c: ("" if r[i] is None else str(r[i]).strip()) for c, i in zip(cols, keep) if i < len(r)}
        if not row.get("blind_id"):
            continue
        v = row.get("verdict", "")
        if v in VERDICTS:
            judged += 1
        elif v:
            odd.append(f"{row['blind_id']}: verdict {v!r}")
        out.append(row)
    dest = xlsx.with_suffix(".csv")
    with open(dest, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    print(f"{xlsx.name}: {len(out)} rows, {judged} judged -> {dest.name}")
    for line in odd:
        print(f"    ! {line}")


def main():
    names = sys.argv[1:]
    books = ([LABELS / f"events_{n}.xlsx" for n in names] if names
             else sorted(p for p in LABELS.glob("events_*.xlsx") if not p.name.startswith("~$")))
    missing = [p.name for p in books if not p.exists()]
    if missing or not books:
        sys.exit(f"no such workbook: {', '.join(missing)}" if missing else f"no events_*.xlsx in {LABELS}")
    for book in books:
        export(book)


if __name__ == "__main__":
    main()
