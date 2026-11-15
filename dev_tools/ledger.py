#!/usr/bin/env python3
"""
ledger.py: which tape has been used for what, so a held-out test is run on tapes nobody tuned on.

A detector tuned and tested on the same tapes looks better than it is. The ledger records every
use of a tape, per topic (a detector or a study: bars, audio_dropout, video_dropout,
period_selection, ...), as one of two roles:

    dev       someone looked at this tape's results for the topic: labeled it, reviewed its
              detections, tuned a threshold against it. It can never again be a fair test of it.
    heldout   set aside as a test for the topic, to be scored once against a frozen detector.
              Until a later `dev` row says the test is over, the tape is reserved: do not look.

It lives in $AVSPEX_CORPUS/ledger.csv, one row per use:

    video_id,topic,role,date,source,note

Tapes with rows in truth/<detector>.csv count as `dev` for that detector without a ledger row:
an operator reviewed that detector's output on them.

    ledger.py                           summary: per topic, how many tapes are dev / held out / fresh
    ledger.py tapes [--topic T]         one line per tape with its roles
    ledger.py fresh TOPIC [--run RUN]   tapes never used for TOPIC (--untouched: never used at all)
    ledger.py draw TOPIC N [--run RUN]  pick N fresh tapes at random and record them as heldout
    ledger.py add TOPIC ROLE ID...      record a use by hand (--date, --source, --note)
    ledger.py check                     validate; report tapes that were dev before being held out

`draw` exists so the test set is chosen before anyone has seen it, by a recorded seed, not by
picking tapes that look interesting. Draw first, freeze the detector, then score.

A tape is known to the ledger once any run in the corpus holds it (or a ledger row names it).
While a tape is reserved for a detector, `scorecard.py --unjudged / --sheet` leaves it out.
"""
import argparse
import csv
import datetime
import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import CORPUS, resolve_run  # noqa: E402

LEDGER = CORPUS / "ledger.csv"
TRUTH = CORPUS / "truth"
COLUMNS = ("video_id", "topic", "role", "date", "source", "note")
ROLES = ("dev", "heldout")


def tape_id(text):
    """'2213', '02213' or 'JPC_AV_02213' -> 'JPC_AV_02213'."""
    m = re.fullmatch(r"(?:JPC_AV_)?(\d+)", text.strip())
    if not m:
        sys.exit(f"not a tape ID: {text!r}")
    return f"JPC_AV_{int(m.group(1)):05d}"


def run_tapes(run_dir):
    out = set()
    for p in (Path(run_dir) / "packages").glob("*/package.json"):
        try:
            out.add(json.loads(p.read_text())["video_id"])
        except (ValueError, KeyError):
            pass
    return out


def known_tapes():
    out = set()
    for run in (CORPUS / "runs").glob("*"):
        out |= run_tapes(run)
    return out


def read_rows():
    """Ledger rows plus the dev uses implied by the truth files."""
    rows = []
    if LEDGER.exists():
        with open(LEDGER, newline="") as fh:
            rows = [{k: (r.get(k) or "").strip() for k in COLUMNS} for r in csv.DictReader(fh)]
    for path in sorted(TRUTH.glob("*.csv")) if TRUTH.is_dir() else []:
        seen = {}
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                vid = (r.get("video_id") or "").strip()
                if not vid:
                    continue
                m = re.search(r"\d{4}-\d{2}-\d{2}", r.get("source") or "")
                day = m.group(0) if m else ""
                # the earliest look is the one that matters; an undated one counts as earliest
                if vid not in seen or (seen[vid] and (not day or day < seen[vid])):
                    seen[vid] = day
        rows += [{"video_id": v, "topic": path.stem, "role": "dev", "date": d,
                  "source": f"truth/{path.name}", "note": "", "_derived": True}
                 for v, d in sorted(seen.items())]
    return rows


def by_tape(rows):
    out = defaultdict(lambda: defaultdict(list))
    for r in rows:
        out[r["video_id"]][r["topic"]].append(r)
    return out


def status(uses):
    """A tape's standing for one topic, from its rows for that topic."""
    if not uses:
        return "fresh"
    held = [u["date"] for u in uses if u["role"] == "heldout"]
    dev = [u["date"] for u in uses if u["role"] == "dev"]
    if held and not any(d > max(held) for d in dev):
        return "reserved"
    return "spent" if held else "dev"


def reserved(topic, rows=None):
    """Tapes held out for a topic whose test is not over: nobody should look at them yet."""
    tapes = by_tape(read_rows() if rows is None else rows)
    return {vid for vid, topics in tapes.items() if status(topics.get(topic)) == "reserved"}


def problems(rows):
    out = []
    for i, r in enumerate(rows, start=2):
        if r.get("_derived"):
            continue
        where = f"ledger.csv line {i}"
        if not re.fullmatch(r"JPC_AV_\d{5}", r["video_id"]):
            out.append(f"{where}: video_id {r['video_id']!r}")
        if r["role"] not in ROLES:
            out.append(f"{where}: role {r['role']!r} (want {' / '.join(ROLES)})")
        if not r["topic"]:
            out.append(f"{where}: no topic")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r["date"]):
            out.append(f"{where}: date {r['date']!r} (want YYYY-MM-DD)")
    for vid, topics in sorted(by_tape(rows).items()):
        for topic, uses in sorted(topics.items()):
            for h in (u for u in uses if u["role"] == "heldout"):
                # an undated dev use (a truth row with no date in its source) counts as earlier
                before = [u for u in uses if u["role"] == "dev" and u["date"] <= h["date"]]
                if before:
                    out.append(f"{vid} was dev for {topic} ({before[0]['source'] or before[0]['date']}) "
                               f"before it was held out on {h['date']}: not a fair test")
    return out


def fresh(topic, rows, pool, untouched=False):
    tapes = by_tape(rows)
    if untouched:
        return sorted(v for v in pool if v not in tapes)
    return sorted(v for v in pool if not tapes.get(v, {}).get(topic))


def append(new):
    exists = LEDGER.exists()
    if exists and not LEDGER.read_text().endswith("\n"):
        with open(LEDGER, "a") as fh:
            fh.write("\n")
    with open(LEDGER, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if not exists:
            w.writeheader()
        w.writerows(new)


def cmd_summary(rows, pool):
    tapes = by_tape(rows)
    topics = sorted({r["topic"] for r in rows})
    everyone = pool | set(tapes)
    print(f"{len(everyone)} tape(s) known; {len([v for v in everyone if v not in tapes])} never used for anything\n")
    print(f"{'topic':<20} {'dev':>5} {'spent':>6} {'reserved':>9} {'fresh':>6}")
    for t in topics:
        c = defaultdict(int)
        for v in everyone:
            c[status(tapes.get(v, {}).get(t))] += 1
        print(f"{t:<20} {c['dev']:>5} {c['spent']:>6} {c['reserved']:>9} {c['fresh']:>6}")
    print("\ndev = looked at; spent = was a held-out test, now looked at; reserved = held out, "
          "test not over (do not look); fresh = never used for the topic")


def cmd_tapes(rows, pool, topic):
    tapes = by_tape(rows)
    for vid in sorted(pool | set(tapes)):
        parts = [f"{t}={status(u)}" for t, u in sorted(tapes.get(vid, {}).items())
                 if not topic or t == topic]
        print(f"{vid}  {'  '.join(parts) if parts else ('fresh' if topic else 'never used')}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("tapes"); p.add_argument("--topic")
    p = sub.add_parser("fresh"); p.add_argument("topic"); p.add_argument("--run")
    p.add_argument("--untouched", action="store_true", help="only tapes never used for any topic")
    p = sub.add_parser("draw"); p.add_argument("topic"); p.add_argument("n", type=int)
    p.add_argument("--run"); p.add_argument("--untouched", action="store_true")
    p.add_argument("--seed", type=int, help="default: today's date as YYYYMMDD")
    p.add_argument("--note", default="")
    p = sub.add_parser("add"); p.add_argument("topic"); p.add_argument("role", choices=ROLES)
    p.add_argument("ids", nargs="+"); p.add_argument("--date"); p.add_argument("--source", default="")
    p.add_argument("--note", default="")
    sub.add_parser("check")
    args = ap.parse_args()

    rows = read_rows()
    today = datetime.date.today().isoformat()
    if args.cmd == "check":
        bad = problems(rows)
        for line in bad:
            print(f"! {line}")
        print(f"{len(bad)} problem(s)" if bad else f"ledger ok: {sum(1 for r in rows if not r.get('_derived'))} "
              f"row(s), plus {sum(1 for r in rows if r.get('_derived'))} dev use(s) implied by truth/")
        sys.exit(1 if bad else 0)
    if args.cmd == "add":
        new = [{"video_id": tape_id(i), "topic": args.topic, "role": args.role,
                "date": args.date or today, "source": args.source, "note": args.note} for i in args.ids]
        append(new)
        print(f"{len(new)} row(s) added to {LEDGER}")
        bad = [b for b in problems(read_rows()) if any(n["video_id"] in b for n in new)]
        for line in bad:
            print(f"! {line}")
        return

    pool = run_tapes(resolve_run(args.run)) if getattr(args, "run", None) else known_tapes()
    if args.cmd in ("fresh", "draw"):
        free = fresh(args.topic, rows, pool, args.untouched)
        if args.cmd == "fresh":
            print("\n".join(free))
            print(f"{len(free)} of {len(pool)} tape(s) "
                  f"{'never used at all' if args.untouched else 'never used for ' + args.topic}", file=sys.stderr)
            return
        if args.n > len(free):
            sys.exit(f"only {len(free)} fresh tape(s) for {args.topic}; asked for {args.n}")
        seed = args.seed if args.seed is not None else int(today.replace("-", ""))
        picked = sorted(random.Random(seed).sample(free, args.n))
        src = f"ledger.py draw, seed {seed}, {args.n} of {len(free)} fresh" + (f" in {args.run}" if args.run else "")
        append([{"video_id": v, "topic": args.topic, "role": "heldout", "date": today,
                 "source": src, "note": args.note} for v in picked])
        print("\n".join(picked))
        print(f"{len(picked)} tape(s) held out for {args.topic} ({src})", file=sys.stderr)
        return
    if args.cmd == "tapes":
        cmd_tapes(rows, pool, args.topic)
        return
    cmd_summary(rows, pool)


if __name__ == "__main__":
    main()
