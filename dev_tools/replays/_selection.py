"""
_selection.py: the period-selection replay shared by the scripts in replays/.

`load_package()` rebuilds everything AV Spex's period selection reads from one corpus package
(stage 0: black segments, bars, the violation scan, bin profiles, suitability), plus two things
only replays need: the full-frame BRNG of every frame (stage 3's comparison input) and per-bin
statistics of **every** numeric QCTools tag on video frames (`tag_bins`), so tags the app does
not score yet can be tried. `select_periods()` then runs stages 1-3 at any count x duration,
optionally under different scoring (`scoring()`), exactly as period_grid.py always did.

Loading walks the QCTools report three times and is most of a replay's cost, so the result is
cached under $AVSPEX_CORPUS/cache, keyed by the report and by the source of the modules whose
behaviour it captures. Editing any of those modules invalidates it.
"""
import bisect
import contextlib
import gzip
import hashlib
import json
import math
import os
import pickle
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional

import numpy as np

from AV_Spex.checks import bin_scoring, bin_suitability, frame_analysis, qctools_bin_profile
from AV_Spex.checks.frame_analysis import (
    EnhancedFrameAnalysis, IntegratedSignalstatsAnalyzer, QCToolsParser,
    content_start_after_bars, merge_avoid_segments,
)
from AV_Spex.checks.qctools_bin_profile import PROFILE_BIN_SIZE, PSNR_CLIP

GT_FILE = "ground_truth/active_signalstats.txt.gz"
CACHE_ROOT = Path(os.environ.get("AVSPEX_CORPUS", "~/git/JPC_AV/avspex_corpus")).expanduser() / "cache"
CACHE_VERSION = 1   # bump when load_package() changes what it stores

# Per-bin statistics kept for every tag in tag_bins.
TAG_STATS = ("mean", "p95", "max", "min")
# Tags that are not per-frame measurements: bit depth is constant, and idet's counters are
# cumulative over the file (BinProfile already turns repeated.current_frame into a count).
SKIP_TAG_PARTS = ("BITDEPTH", ".idet.")

# Legal luma range, 10-bit (divided by 4 for an 8-bit report). The derived legality columns
# measure only the part of a bin past these limits, the way bin_scoring floors SATMAX, so they
# are zero on a legal bin instead of tracking how bright or dark the picture is.
LEGAL_BLACK_10BIT = 64.0
LEGAL_WHITE_10BIT = 940.0


def tag_name(key):
    """Short column name for a QCTools tag key: signalstats keys lose their filter prefix."""
    name = key[len("lavfi."):] if key.startswith("lavfi.") else key
    if name.startswith("signalstats."):
        name = name[len("signalstats."):]
    return name.lower().replace(".", "_")


# --- ground truth and frame lists -----------------------------------------------------------

class Rows(list):
    def __init__(self, rows):
        super().__init__(rows)
        self.times = [r[0] for r in rows]


def window(rows, start, end, inclusive_end=False):
    times = rows.times if hasattr(rows, "times") else [r[0] for r in rows]
    lo = bisect.bisect_left(times, start)
    hi = bisect.bisect_right(times, end) if inclusive_end else bisect.bisect_left(times, end)
    return rows[lo:hi]


def load_ground_truth(path):
    """Per-frame active-area signalstats (time, BRNG, TOUT, VREP), sorted by time."""
    rows = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            rec = {}
            for part in line.strip().split("|"):
                key, _, val = part.partition("=")
                rec[key.rsplit(".", 1)[-1]] = val
            try:
                rows.append((float(rec["pts_time"]), float(rec.get("BRNG", 0) or 0),
                             float(rec.get("TOUT", 0) or 0), float(rec.get("VREP", 0) or 0)))
            except (KeyError, ValueError):
                continue
    rows.sort()
    return rows


def walk_report(xml_path, parser):
    """One walk of the QCTools report, returning:

    ff_brng: (time, brng) for every non-black frame carrying BRNG, which is what
        parse_brng_period reads per period.
    tag_bins: {bin_start: {column: value}} over non-black video frames, with
        `<tag>_{mean,p95,max,min}` for every numeric tag and `frames` (the count used).
    """
    ff_brng = []
    samples: Dict[float, Dict[str, list]] = {}
    counts: Dict[float, int] = {}
    opener = gzip.open if str(xml_path).endswith(".gz") else open
    with opener(xml_path, "rt") as fh:
        it = iter(ET.iterparse(fh, events=["start", "end"]))
        _, root = next(it)
        for event, elem in it:
            if event != "end" or elem.tag != "frame":
                continue
            ts = elem.get("pkt_pts_time")
            media_type = elem.get("media_type")
            if ts and (media_type is None or media_type == "video") and not parser._is_black_frame(elem):
                t = float(ts)
                tag = elem.find('.//tag[@key="lavfi.signalstats.BRNG"]')
                if tag is not None and tag.get("value"):
                    ff_brng.append((t, float(tag.get("value"))))
                b = float(int(t // PROFILE_BIN_SIZE) * PROFILE_BIN_SIZE)
                bucket = samples.setdefault(b, {})
                counts[b] = counts.get(b, 0) + 1
                for tag in elem.iter("tag"):
                    key = tag.get("key") or ""
                    if not key.startswith("lavfi.") or any(p in key for p in SKIP_TAG_PARTS):
                        continue
                    try:
                        value = float(tag.get("value") or tag.text)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(value):
                        if "psnr" not in key:
                            continue
                        value = PSNR_CLIP      # identical frames; as BinProfile clips it
                    elif "psnr" in key:
                        value = min(value, PSNR_CLIP)
                    bucket.setdefault(tag_name(key), []).append(value)
            elem.clear()
            root.clear()
    ff_brng.sort()

    tag_bins = {}
    for b, bucket in samples.items():
        row = {"frames": counts[b]}
        for name, values in bucket.items():
            a = np.asarray(values, dtype=float)
            row[f"{name}_mean"] = float(a.mean())
            row[f"{name}_p95"] = float(np.percentile(a, 95))
            row[f"{name}_max"] = float(a.max())
            row[f"{name}_min"] = float(a.min())
        tag_bins[b] = row
    return ff_brng, tag_bins


# --- package context -------------------------------------------------------------------------

@dataclass
class Package:
    """Everything a replay needs from one corpus package. Built by load_package()."""
    pkg: Path
    name: str
    info: dict
    fa: dict
    meta: dict
    xml: Path
    duration: float
    bars_end: float
    bars_regions: list
    hints: list
    black: list
    avoid: list                 # black + bars + unanalyzable, merged
    violations: list
    histogram: Optional[dict]
    severity: Optional[dict]
    profiles: dict              # QCToolsParser.bin_profiles
    bit_depth_10: bool
    suitability: object         # bin_suitability.SuitabilityAssessment
    ff_rows: Rows
    tag_bins: dict
    gt_rows: Optional[Rows] = None
    actual: list = field(default_factory=list)   # the run's own periods, rounded

    @property
    def effective_start(self):
        return content_start_after_bars(self.bars_end)

    @property
    def scores(self):
        """Composite scores under the scoring currently in force (see scoring())."""
        return score(self)


def _code_hash():
    h = hashlib.sha256(str(CACHE_VERSION).encode())
    for mod in (bin_scoring, bin_suitability, frame_analysis, qctools_bin_profile):
        h.update(Path(mod.__file__).read_bytes())
    h.update(Path(__file__).read_bytes())
    return h.hexdigest()[:16]


def _cache_prefix(pkg):
    """Per package directory, so the same tape in two runs keeps two entries."""
    return f"{pkg.name}-{hashlib.sha256(str(pkg.resolve()).encode()).hexdigest()[:8]}-"


def _cache_path(pkg, xml):
    st = xml.stat()
    key = hashlib.sha256(f"{st.st_size}|{st.st_mtime_ns}|{_code_hash()}".encode())
    return CACHE_ROOT / f"{_cache_prefix(pkg)}{key.hexdigest()[:16]}.pkl.gz"


def load_package(pkg, use_cache=True) -> Package:
    pkg = Path(pkg)
    info = json.loads((pkg / "package.json").read_text())
    name = info["video_id"]
    if not info.get("qctools_report"):
        raise FileNotFoundError("no QCTools report")
    xml = pkg / info["qctools_report"]
    fa = json.load(open(pkg / f"{name}_qc_metadata" / f"{name}_enhanced_frame_analysis.json"))
    meta = dict(info.get("video") or {}, frame_analysis_seconds=info.get("frame_analysis_seconds"))

    cache = _cache_path(pkg, xml) if use_cache else None
    ctx = None
    if cache and cache.exists():
        try:
            with gzip.open(cache, "rb") as fh:
                ctx = pickle.load(fh)
        except Exception:
            ctx = None
    if ctx is None:
        bars_end = fa.get("color_bars_end_time") or 0
        bars_regions = [(r["start"], r["end"]) for r in fa.get("bars_regions") or []]
        # Stage 0, exactly as EnhancedFrameAnalysis.analyze() builds it
        black = QCToolsParser(str(xml)).detect_black_segments(min_duration=2.0)
        avoid = merge_avoid_segments(black, bars_regions)
        parser = QCToolsParser(str(xml))
        violations = parser.parse_for_violations_streaming(
            max_frames=100, skip_color_bars=True, color_bars_end_time=bars_end,
            exclude_regions=bars_regions)
        profiles = getattr(parser, "bin_profiles", {}) or {}
        suitability = bin_suitability.assess_bins(profiles, bit_depth_10=parser.bit_depth_10)
        if suitability.unsuitable_regions:
            avoid = merge_avoid_segments(avoid, suitability.unsuitable_regions)
        ff_brng, tag_bins = walk_report(xml, parser)
        ctx = Package(
            pkg=pkg, name=name, info=info, fa=fa, meta=meta, xml=xml,
            duration=meta["duration"], bars_end=bars_end, bars_regions=bars_regions,
            hints=[tuple(h) for h in (fa.get("initial_borders") or {}).get("quality_frame_hints") or []],
            black=black, avoid=avoid, violations=violations,
            histogram=getattr(parser, "violation_histogram", None),
            severity=getattr(parser, "violation_severity", None),
            profiles=profiles, bit_depth_10=parser.bit_depth_10, suitability=suitability,
            ff_rows=Rows(ff_brng), tag_bins=tag_bins)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".partial")
            with gzip.open(tmp, "wb") as fh:
                pickle.dump(ctx, fh)
            tmp.replace(cache)
            for stale in CACHE_ROOT.glob(f"{_cache_prefix(pkg)}*.pkl.gz"):
                if stale != cache:
                    stale.unlink()

    # Cheap, or liable to change without the report changing: always fresh
    ctx.pkg, ctx.info, ctx.fa, ctx.meta = pkg, info, fa, meta
    ctx.actual = [(round(s), round(d))
                  for s, d in (fa.get("brng_analysis") or {}).get("analysis_periods") or []]
    ctx.gt_rows = Rows(load_ground_truth(pkg / GT_FILE)) if (pkg / GT_FILE).exists() else None
    add_legal_floor_columns(ctx)
    return ctx


def add_legal_floor_columns(ctx):
    """Add `under_black` (how far the bin's worst 10th-percentile luma, YLOW min, falls below
    legal black) and `over_white` (how far its worst 90th-percentile luma, YHIGH max, rises
    above legal white) to every bin of ctx.tag_bins. Both are 0 for a legal bin."""
    scale = 1.0 if ctx.bit_depth_10 else 0.25
    black, white = LEGAL_BLACK_10BIT * scale, LEGAL_WHITE_10BIT * scale
    for row in ctx.tag_bins.values():
        if row.get("ylow_min") is not None:
            row["under_black"] = max(0.0, black - row["ylow_min"])
        if row.get("yhigh_max") is not None:
            row["over_white"] = max(0.0, row["yhigh_max"] - white)


# --- scoring ---------------------------------------------------------------------------------

NEG_PREFIX = "neg_"     # MetricSpec(field="neg_<column>") scores a tag low-is-bad
NEG_OFFSET = 1e9
_PROFILE_FIELDS = set(qctools_bin_profile.BinProfile.__dataclass_fields__)


class _AugmentedProfile:
    """A BinProfile that also answers for every column of the generic tag table, so a
    MetricSpec can name a tag BinProfile does not collect (e.g. 'ymin_min', 'psnr_psnr_avg_mean')."""
    __slots__ = ("_profile", "_tags")

    def __init__(self, profile, tags):
        self._profile, self._tags = profile, tags or {}

    def __getattr__(self, name):
        # A BinProfile field always answers for itself, None included: the tag table also
        # carries e.g. brng_mean, over frames the violation scan excluded (bars), and must not
        # fill in where production saw nothing.
        if name in _PROFILE_FIELDS:
            return getattr(self._profile, name)
        if name.startswith(NEG_PREFIX):
            # Low-is-bad tags (e.g. inter-field PSNR): score_bins ranks the excess over a floor,
            # clamped at zero, so the flip has to stay positive. An offset keeps rank order.
            value = self.__getattr__(name[len(NEG_PREFIX):])
            return None if value is None else NEG_OFFSET - value
        return self._tags.get(name)


@contextlib.contextmanager
def scoring(metrics=None, weights=None):
    """Temporarily replace bin_scoring.METRICS / FAMILY_WEIGHTS (module globals that
    score_bins reads at call time). Families named in metrics but missing from weights get 0."""
    saved = bin_scoring.METRICS, bin_scoring.FAMILY_WEIGHTS
    try:
        if metrics is not None:
            bin_scoring.METRICS = tuple(metrics)
        if weights is not None or metrics is not None:
            w = dict(weights if weights is not None else saved[1])
            for spec in bin_scoring.METRICS:
                w.setdefault(spec.family, 0.0)
            bin_scoring.FAMILY_WEIGHTS = w
        yield
    finally:
        bin_scoring.METRICS, bin_scoring.FAMILY_WEIGHTS = saved


def score(ctx: Package):
    """score_bins() over the package's profiles, augmented with the generic tag table."""
    profiles = {b: _AugmentedProfile(p, ctx.tag_bins.get(b)) for b, p in ctx.profiles.items()}
    return bin_scoring.score_bins(profiles, ctx.suitability.verdicts, bit_depth_10=ctx.bit_depth_10)


# --- stages 1-3 ------------------------------------------------------------------------------

def simulate_comparison(periods, ff_rows, gt_rows):
    """comparison_results as analyze_with_signalstats builds them, from the two frame lists."""
    comps = []
    for start, dur in periods:
        comp = {}
        ff = window(ff_rows, start, start + dur, inclusive_end=True)
        gt = window(gt_rows, start, start + dur)
        if ff and gt:
            full = sum(1 for _, b in ff if b > 0) / len(ff) * 100
            active = sum(1 for r in gt if r[1] > 0) / len(gt) * 100
            comp["ffprobe_active_area"] = {"violations_pct": active}
            if full > active + 5 and active < 30:
                comp["diagnosis"] = "border_violations"
            elif active > 10:
                comp["diagnosis"] = "content_violations"
            else:
                comp["diagnosis"] = "minimal_violations"
        comps.append(comp)
    return SimpleNamespace(comparison_results=comps)


def select_periods(ctx: Package, count, dur, scores=None) -> List[tuple]:
    """Stages 1-3 at count x dur, returning the final periods sorted by start.

    scores: precomputed bin scores; by default the current scoring (see scoring()).
    Stage 3 needs the ground truth as its active-area measurement and is skipped without it.
    """
    if scores is None:
        scores = score(ctx)
    ea = EnhancedFrameAnalysis.__new__(EnhancedFrameAnalysis)
    ss = IntegratedSignalstatsAnalyzer.__new__(IntegratedSignalstatsAnalyzer)
    ss.duration, ss.fps, ss.qctools_report = ctx.duration, ctx.meta.get("fps"), str(ctx.xml)
    ss.last_resort_period_note = None
    ea.signalstats_analyzer = ss

    cands = []
    if ctx.violations or scores:
        cands = ea._analyze_qctools_violation_distribution(
            ctx.violations, num_periods=count, period_duration=dur, video_duration=ctx.duration,
            black_segments=ctx.avoid, histogram=ctx.histogram, severity=ctx.severity,
            bin_scores=scores, content_start=ctx.effective_start)
    stage2 = ss._find_analysis_periods(0, ctx.bars_end, dur, count, ctx.hints or None,
                                       qctools_periods=cands, black_segments=ctx.avoid)
    final = stage2
    if stage2 and cands and ctx.gt_rows is not None:
        final = ea._refine_periods_from_signalstats(
            stage2, simulate_comparison(stage2, ctx.ff_rows, ctx.gt_rows), cands, ctx.avoid, dur,
            ctx.bars_end)
    return sorted(final)


def covered(bin_start, periods, bin_size=PROFILE_BIN_SIZE):
    """A bin counts as sampled when a period covers at least half of it."""
    return any(min(bin_start + bin_size, s + d) - max(bin_start, s) >= bin_size / 2
               for s, d in periods)


def in_spans(t, spans):
    return any(s <= t < e for s, e in spans)
