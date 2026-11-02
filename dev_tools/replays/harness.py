#!/usr/bin/env python3
"""
harness.py: check that the replay still reproduces a run, and record drift that is expected.

Every replay first replays each tape at the run's own settings and compares with the periods
the run chose. A tape can differ for two reasons: the code changed since the run, and the
change is wanted (a gate fix that moves a period on purpose), or the replay no longer mirrors
the app. Only the second is a problem, so wanted drift is recorded in the run's
expected_drift.json with the periods the tape gets now. A recorded tape counts as `expected`
while it keeps those periods; if it moves again it is UNEXPECTED, like any other mismatch.

    python3 dev_tools/replays/harness.py RUN                                # status per tape
    python3 dev_tools/replays/harness.py RUN --accept JPC_AV_02211 --reason "tail-static gate (22a2c20)"
    python3 dev_tools/replays/harness.py RUN --forget JPC_AV_02211          # drop an entry

--accept records a tape's current periods with the reason, the date and the code it was
replayed under. Accept only drift you have looked at and can explain. An entry for a tape that
matches again is reported as no longer needed.
"""
import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from corpus import code_state, find_packages  # noqa: E402
from _selection import DRIFT_FILE, harness_replay, harness_status, harness_summary, load_package, run_dir  # noqa: E402

from AV_Spex.utils.log_setup import logger


def _fmt(periods):
    return ";".join(f"{s}+{d}" for s, d in periods) if periods else "-"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", help="run ID (or unique prefix) or run directory")
    ap.add_argument("--accept", nargs="+", default=[], metavar="TAPE",
                    help="record these tapes' current periods as expected drift")
    ap.add_argument("--reason", help="why the accepted tapes drift (required with --accept)")
    ap.add_argument("--forget", nargs="+", default=[], metavar="TAPE", help="remove these entries")
    args = ap.parse_args()
    if args.accept and not args.reason:
        sys.exit("--accept needs --reason: say what change moved these tapes and why it is wanted")
    logger.setLevel(logging.ERROR)

    statuses, replayed, ctxs = {}, {}, {}
    for pkg in find_packages([args.run]):
        try:
            ctx = load_package(pkg)
        except Exception as e:
            print(f"{pkg.name}: skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        ctxs[ctx.name] = ctx
        replayed[ctx.name] = harness_replay(ctx)
        statuses[ctx.name] = harness_status(ctx, replayed[ctx.name])
    if not ctxs:
        sys.exit("no packages")

    path = run_dir(next(iter(ctxs.values()))) / DRIFT_FILE
    data = json.loads(path.read_text()) if path.exists() else {"tapes": {}}
    tapes = data.setdefault("tapes", {})

    for name in sorted(ctxs):
        status, entry = statuses[name], tapes.get(name)
        line = f"{name:<14} {status or 'no periods':<10}"
        if status not in (None, "match"):
            line += f" run {_fmt(ctxs[name].actual)} -> replay {_fmt(replayed[name])}"
        if entry:
            line += f"  [{entry.get('reason', '')}]"
            if status == "match":
                line += " (entry no longer needed: --forget it)"
        print(line)
    print("\n" + harness_summary(statuses))

    unknown = [t for t in args.accept + args.forget if t not in ctxs]
    if unknown:
        sys.exit(f"not in this run: {', '.join(unknown)}")
    if not (args.accept or args.forget):
        return
    code = code_state()
    commit = code["commit"][:7] + (f"-d{code['src_diff_hash']}" if code["src_dirty"] else "")
    for name in args.accept:
        if statuses[name] == "match":
            print(f"{name}: matches the run; nothing to accept", file=sys.stderr)
            continue
        tapes[name] = {"periods": [list(p) for p in replayed[name]], "run_periods": [list(p) for p in ctxs[name].actual],
                       "reason": args.reason, "accepted": date.today().isoformat(), "code": commit}
        print(f"{name}: accepted {_fmt(replayed[name])}")
    for name in args.forget:
        if tapes.pop(name, None) is not None:
            print(f"{name}: entry removed")
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
