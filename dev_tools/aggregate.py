#!/usr/bin/env python3
"""
aggregate.py: flatten collected runs into one table, or diff two runs package by package.

    aggregate.py RUN [RUN ...] [--out table.csv]   one row per (run, package)
    aggregate.py --diff RUN_A RUN_B [--only REGEX] columns that changed, per package
    aggregate.py --columns RUN                     list the columns with an example value

RUN is a run ID, a unique prefix of one, or a run directory (see `corpus.py list`).

Each column comes from one small extractor function below, reading the package's sidecars.
To track something new, add an extractor and append it to EXTRACTORS; old runs simply show
the column empty where the sidecar is missing. Columns are prefixed by area (fa_, bars_,
qct_, ...) so a --only regex can pick an area out.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import resolve_run  # noqa: E402


class Package:
    def __init__(self, path):
        self.path = Path(path)
        self.info = json.loads((self.path / "package.json").read_text())
        self.vid = self.info["video_id"]
        self.qc = self.path / f"{self.vid}_qc_metadata"
        self.csvs = self.path / f"{self.vid}_report_csvs"
        self._fa = None

    @property
    def fa(self):
        """The frame-analysis JSON, or {} when frame analysis did not run."""
        if self._fa is None:
            p = self.qc / f"{self.vid}_enhanced_frame_analysis.json"
            self._fa = json.loads(p.read_text()) if p.exists() else {}
        return self._fa

    def csv_rows(self, name):
        p = self.csvs / name
        if not p.exists():
            return None
        with open(p, newline="", errors="replace") as fh:
            return list(csv.DictReader(fh))


def _spans_total(spans):
    return round(sum((s.get("end") or 0) - (s.get("start") or 0) for s in spans or []), 1)


def _periods(periods):
    return ";".join(f"{s:.0f}+{d:.0f}" for s, d in periods or []) or None


# ----------------------------------------------------------------- extractors ---

def ex_package(p):
    v = p.info.get("video") or {}
    gt = p.info.get("ground_truth") or {}
    return {
        "video_id": p.vid,
        "tape_min": round(v["duration"] / 60, 1) if v.get("duration") else None,
        "frame_size": f"{v.get('width')}x{v.get('height')}" if v.get("width") else None,
        "fps": round(v["fps"], 3) if v.get("fps") else None,
        "avspex_exit": p.info.get("avspex_exit"),
        "avspex_s": p.info.get("avspex_seconds"),
        "fa_s": p.info.get("frame_analysis_seconds"),
        "ground_truth": bool(gt) or (p.path / "ground_truth/active_signalstats.txt.gz").exists(),
    }


def ex_steps(p):
    return {f"fa_step_{k}": v for k, v in (p.fa.get("steps_enabled") or {}).items()}


def ex_bars(p):
    out = {
        "bars_end_s": _r(p.fa.get("color_bars_end_time")),
        "bars_regions": len(p.fa.get("bars_regions") or []) if p.fa else None,
    }
    for name, col in (("qct-parse_colorbars_durations.csv", "bars_qct"),
                      ("clams_bars_colorbars_durations.csv", "bars_clams"),
                      ("clams_tone_detection_durations.csv", "tone_clams")):
        out[col] = _durations(p.csvs / name)
    return out


def _durations(path):
    """The durations sidecars are a sentence ("... found:" / "... found no ...") followed by
    `label,start,end` lines. Returns "label start-end; ...", "none", or None when absent."""
    if not path.exists():
        return None
    spans = []
    for line in path.read_text(errors="replace").splitlines()[1:]:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 3:
            spans.append(f"{parts[0]} {parts[1]}-{parts[2]}")
    return "; ".join(spans) or "none"


def ex_selection(p):
    fa = p.fa
    if not fa:
        return {}
    scoring = fa.get("bin_scoring") or {}
    evidence = fa.get("period_evidence") or []
    return {
        "fa_black_segments": len(fa.get("black_segments") or []),
        "fa_black_s": _spans_total(fa.get("black_segments")),
        "fa_unanalyzable": len(fa.get("unanalyzable_regions") or []),
        "fa_unanalyzable_s": _spans_total(fa.get("unanalyzable_regions")),
        "fa_score_metrics": ",".join(scoring.get("metrics") or []) or None,
        "fa_qctools_violations": fa.get("qctools_violations_found"),
        "fa_periods": _periods((fa.get("brng_analysis") or {}).get("analysis_periods")
                               or (fa.get("signalstats") or {}).get("analysis_periods")),
        "fa_periods_without_evidence": sum(1 for e in evidence if not e.get("evidence")),
        "fa_period_confidence": (fa.get("brng_analysis") or {}).get("period_confidence"),
    }


def ex_borders(p):
    b = p.fa.get("final_borders") or p.fa.get("initial_borders") or {}
    aa = b.get("active_area")
    return {
        "fa_border_method": b.get("detection_method"),
        "fa_active_area": "x".join(map(str, aa)) if aa else None,
        "fa_border_refinements": p.fa.get("refinement_iterations"),
    }


def ex_signalstats(p):
    s = p.fa.get("final_signalstats") or p.fa.get("signalstats") or {}
    if not s:
        return {}
    return {
        "ss_violation_pct": _r(s.get("violation_percentage")),
        "ss_max_brng": _r(s.get("max_brng")),
        "ss_avg_brng": _r(s.get("avg_brng")),
        "ss_region": s.get("analyzed_region"),
        "ss_severity": s.get("severity"),
        "ss_periods_measured": s.get("periods_measured"),
        "ss_diagnoses": ",".join(sorted({c.get("diagnosis") or "-" for c in s.get("comparison_results") or []})),
        "ss_worst_frame_s": _r(s.get("worst_frame_time")),
    }


def ex_brng(p):
    b = p.fa.get("final_brng_analysis") or p.fa.get("brng_analysis") or {}
    if not b:
        return {}
    stats = (b.get("actionable_report") or {}).get("summary_statistics") or {}
    return {
        "brng_violations": stats.get("total_violations", len(b.get("violations") or [])),
        "brng_max_pct": _r(stats.get("max_violation_percentage")),
        "brng_avg_pct": _r(stats.get("average_violation_percentage")),
        "brng_edge_pct": _r(stats.get("edge_violation_percentage")),
        "brng_needs_border_adjust": b.get("requires_border_adjustment"),
    }


def ex_detectors(p):
    out = {}
    for key, col in (("bitplane_check", "bitplane"), ("dropped_sample_detection", "dropped"),
                     ("duplicate_frame_detection", "dupes")):
        d = p.fa.get(key)
        if isinstance(d, dict):
            out[f"{col}_status"] = d.get("status")
    d = p.fa.get("dropped_sample_detection")
    if isinstance(d, dict):
        out.update(dropped_spikes=d.get("spike_count"), dropped_av_diff_ms=_r(d.get("duration_diff_ms")))
    d = p.fa.get("duplicate_frame_detection")
    if isinstance(d, dict):
        out.update(dupes_runs=d.get("total_runs"), dupes_frames=d.get("total_duplicate_frames"),
                   dupes_verified=d.get("verification_available"))
    return out


def ex_qct_parse(p):
    """Row counts of every qct-parse / CLAMS sidecar, so a detector going quiet or noisy
    between runs shows up without a dedicated extractor."""
    out = {}
    if not p.csvs.is_dir():
        return out
    for f in sorted(p.csvs.glob("*.csv")):
        name = f.name.replace(f"{p.vid}_", "")
        if name.startswith(("qct-parse_", "clams_")):
            with open(f, errors="replace") as fh:
                out[f"qct_rows_{name[:-4]}"] = max(sum(1 for _ in fh) - 1, 0)
    return out


EXTRACTORS = [ex_package, ex_steps, ex_bars, ex_selection, ex_borders, ex_signalstats, ex_brng,
              ex_detectors, ex_qct_parse]


def _r(v, nd=3):
    return round(v, nd) if isinstance(v, float) else v


# ---------------------------------------------------------------------- runs ---

def table(run_dir):
    rows = []
    for pj in sorted((run_dir / "packages").glob("*/package.json")):
        pkg = Package(pj.parent)
        row = {"run": run_dir.name, "package": pkg.info["folder"]}
        for ex in EXTRACTORS:
            try:
                row.update(ex(pkg))
            except Exception as e:  # one odd sidecar should not lose the whole table
                row[f"error_{ex.__name__}"] = f"{type(e).__name__}: {e}"
        rows.append(row)
    return rows


def write_csv(rows, out):
    lead = ["run", "package", "video_id", "tape_min"]
    fields = lead + [k for k in dict.fromkeys(k for r in rows for k in r) if k not in lead]
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def diff(run_a, run_b, only):
    a = {r["package"]: r for r in table(run_a)}
    b = {r["package"]: r for r in table(run_b)}
    skip = {"run", "avspex_s", "fa_s"}
    pat = re.compile(only) if only else None
    print(f"A = {run_a.name}\nB = {run_b.name}\n")
    for name in sorted(set(a) | set(b)):
        if name not in a or name not in b:
            print(f"{name}: only in {'A' if name in a else 'B'}")
            continue
        cols = [k for k in dict.fromkeys([*a[name], *b[name]])
                if k not in skip and (not pat or pat.search(k)) and a[name].get(k) != b[name].get(k)]
        if cols:
            print(f"{name}:")
            for k in cols:
                print(f"    {k:<36} {a[name].get(k)!s:<28} -> {b[name].get(k)}")
    common = set(a) & set(b)
    timed = [(a[n].get("fa_s"), b[n].get("fa_s")) for n in common if a[n].get("fa_s") and b[n].get("fa_s")]
    if timed:
        ta, tb = sum(x for x, _ in timed), sum(y for _, y in timed)
        print(f"\nFrame analysis time over {len(timed)} packages: {ta}s -> {tb}s ({(tb - ta) / ta:+.0%})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", help="CSV path (default: <first run>.csv in the current directory)")
    ap.add_argument("--diff", action="store_true", help="compare exactly two runs")
    ap.add_argument("--only", help="with --diff, only columns matching this regex")
    ap.add_argument("--columns", action="store_true", help="list columns with an example value")
    args = ap.parse_args()
    runs = [resolve_run(r) for r in args.runs]

    if args.diff:
        if len(runs) != 2:
            sys.exit("--diff takes exactly two runs")
        diff(*runs, args.only)
        return
    rows = [r for run in runs for r in table(run)]
    if not rows:
        sys.exit("no collected packages in " + ", ".join(r.name for r in runs))
    if args.columns:
        for k in dict.fromkeys(k for r in rows for k in r):
            example = next((r[k] for r in rows if r.get(k) not in (None, "")), None)
            print(f"{k:<40} {str(example)[:60]}")
        return
    out = args.out or f"{runs[0].name}.csv"
    write_csv(rows, out)
    print(f"{len(rows)} package rows from {len(runs)} run(s) -> {out}")


if __name__ == "__main__":
    main()
