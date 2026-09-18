#!/usr/bin/env python3
"""Per-bin profiles of a QCTools report.

Analysis-period selection has historically been driven by a single measure —
the number (and summed severity) of frames whose BRNG exceeded a fixed
threshold. That finds out-of-legal-range excursions and nothing else: a tape
whose problem is dropouts, head-switching noise or tracking instability scores
zero on BRNG and falls through to an evenly distributed sample.

This module collects the raw material for a broader decision. As
`QCToolsParser.parse_for_violations_streaming()` walks the report it hands each
frame to a `BinProfiler`, which accumulates a `BinProfile` per 10-second bin:
counts, and per-metric summary statistics for every QCTools measure that is
actually present in the report. Nothing here scores or ranks anything — that is
deliberately left to the caller, so the collection can be tested against real
reports on its own.

**Metric availability varies between reports.** Some sidecars carry only
signalstats/psnr/astats (no cropdetect, entropy, idet, ssim or deflicker),
depending on which qcli produced them. Every metric field is therefore
Optional and is None when its tag never appeared; `BinProfile.metrics` names
the families that did, and `BinProfiler.metrics_present` aggregates that over
the whole report. Consumers must renormalize over what is present rather than
assume a fixed feature set.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

# Bin width for the violation histogram and for these profiles. The two are
# kept equal on purpose: the histogram is derivable from the profiles
# (violation_frames / violation_score_sum), so period selection can move over
# to the richer data without re-binning.
PROFILE_BIN_SIZE = 10.0

# PSNR of two identical frames is infinite. Clipping keeps the bin mean finite
# so a frozen stretch reads as "very high" rather than poisoning the average.
PSNR_CLIP = 100.0

# QCTools tag key -> (field name used internally, metric family). The family is
# what gets reported in BinProfile.metrics, so a consumer can ask "does this
# report have impulsive-damage measures at all" without probing field by field.
NUMERIC_TAG_KEYS: Dict[str, Tuple[str, str]] = {
    'lavfi.signalstats.BRNG': ('brng', 'signalstats'),
    'lavfi.signalstats.TOUT': ('tout', 'signalstats'),
    'lavfi.signalstats.VREP': ('vrep', 'signalstats'),
    'lavfi.signalstats.YDIF': ('ydif', 'signalstats'),
    'lavfi.signalstats.YAVG': ('yavg', 'signalstats'),
    'lavfi.signalstats.SATMAX': ('satmax', 'signalstats'),
    'lavfi.signalstats.SATAVG': ('satavg', 'signalstats'),
    'lavfi.entropy.normalized_entropy.normal.Y': ('entropy', 'entropy'),
    'lavfi.ssim.All': ('ssim', 'ssim'),
    'lavfi.psnr.psnr_avg': ('psnr', 'psnr'),
    'lavfi.deflicker.relative_change': ('deflicker', 'deflicker'),
    'lavfi.cropdetect.x1': ('crop_x1', 'cropdetect'),
    'lavfi.cropdetect.x2': ('crop_x2', 'cropdetect'),
    'lavfi.cropdetect.y1': ('crop_y1', 'cropdetect'),
    'lavfi.cropdetect.y2': ('crop_y2', 'cropdetect'),
}

# Categorical: 'neither' | 'top' | 'bottom'. Anything but 'neither' is a
# repeated field, the idet-side view of frame repetition.
IDET_REPEATED_KEY = 'lavfi.idet.repeated.current_frame'


@dataclass
class BinProfile:
    """Summary of one `PROFILE_BIN_SIZE`-second span of a QCTools report.

    Counts describe every frame that fell in the bin. The metric statistics
    describe only the **non-black** frames: analog tape black carries enough
    sub-black noise to dominate any average it is included in (the same reason
    `QCToolsParser._is_black_frame` gates the violation list), so a bin that is
    half black reports statistics for its picture half and says so through
    `black_frames`.

    A metric field is None when its tag never appeared on a profiled frame —
    either the report does not carry that filter, or the bin held no non-black
    frames. `metrics` distinguishes the two at bin level: a family listed there
    was measured here.
    """
    bin_start: float
    # Counts
    frames: int = 0             # frames profiled (i.e. reaching the analysis)
    black_frames: int = 0       # subset of `frames` classified all-black
    excluded_frames: int = 0    # frames the caller skipped (bars / pre-bars)
    violation_frames: int = 0   # non-black frames over the BRNG threshold
    violation_score_sum: float = 0.0
    repeated_frames: Optional[int] = None   # idet repeated.current_frame != 'neither'

    # Range / legality
    brng_mean: Optional[float] = None
    brng_max: Optional[float] = None
    satmax_max: Optional[float] = None
    satavg_mean: Optional[float] = None

    # Impulsive damage (dropouts, concealment)
    tout_mean: Optional[float] = None
    tout_p95: Optional[float] = None
    vrep_mean: Optional[float] = None
    vrep_max: Optional[float] = None

    # Temporal behaviour (motion, freeze, flicker, tearing)
    ydif_mean: Optional[float] = None
    ydif_p95: Optional[float] = None
    deflicker_absmax: Optional[float] = None
    ssim_mean: Optional[float] = None
    ssim_min: Optional[float] = None
    psnr_mean: Optional[float] = None       # per-frame values clipped at PSNR_CLIP

    # Exposure / flatness
    yavg_mean: Optional[float] = None
    entropy_mean: Optional[float] = None

    # Geometry (borders moving = tracking instability)
    crop_x1_median: Optional[float] = None
    crop_x2_median: Optional[float] = None
    crop_y1_median: Optional[float] = None
    crop_y2_median: Optional[float] = None
    crop_edge_iqr_max: Optional[float] = None

    # Metric families measured in this bin
    metrics: Tuple[str, ...] = ()

    @property
    def bin_end(self) -> float:
        return self.bin_start + PROFILE_BIN_SIZE

    @property
    def picture_frames(self) -> int:
        """Non-black profiled frames — the sample the statistics describe."""
        return self.frames - self.black_frames

    @property
    def black_fraction(self) -> float:
        """Share of profiled frames classified all-black (0.0 when empty)."""
        return (self.black_frames / self.frames) if self.frames else 0.0


class _BinAccumulator:
    """Mutable per-bin sample store, summarized once the bin closes."""

    __slots__ = ('bin_start', 'frames', 'black_frames', 'excluded_frames',
                 'violation_frames', 'violation_score_sum', 'repeated_frames',
                 'saw_idet', 'samples')

    def __init__(self, bin_start: float):
        self.bin_start = bin_start
        self.frames = 0
        self.black_frames = 0
        self.excluded_frames = 0
        self.violation_frames = 0
        self.violation_score_sum = 0.0
        self.repeated_frames = 0
        self.saw_idet = False
        self.samples: Dict[str, List[float]] = {}

    def add_sample(self, name: str, value: float):
        self.samples.setdefault(name, []).append(value)


class BinProfiler:
    """Streaming accumulator turning QCTools frames into `BinProfile`s.

    Frames are expected in presentation order, which is how QCTools writes
    them: each bin is summarized and released as soon as a later bin opens, so
    only one bin's samples are ever held in memory regardless of tape length.
    A frame that arrives for an already-closed bin is counted in
    `out_of_order_frames` and otherwise ignored, rather than silently
    overwriting a finished profile.
    """

    def __init__(self, bin_size: float = PROFILE_BIN_SIZE):
        self.bin_size = bin_size
        self.profiles: Dict[float, BinProfile] = {}
        self.metrics_present: set = set()
        self.out_of_order_frames = 0
        self._open: Optional[_BinAccumulator] = None

    # -- collection ---------------------------------------------------------

    def note_excluded(self, timestamp: float):
        """Record a frame the caller skipped (color bars, mid-file bars).

        Excluded frames contribute no measurements, but their count is what
        tells a later stage that a bin was only partly examined.
        """
        acc = self._accumulator_for(timestamp)
        if acc is not None:
            acc.excluded_frames += 1

    def add_frame(self, timestamp: float, elem, is_black: bool = False,
                  violation_score: Optional[float] = None):
        """Profile one analyzed frame.

        elem: the QCTools <frame> element, read once for every known tag.
        is_black: result of the caller's black-frame classifier; black frames
            are counted but contribute no measurements.
        violation_score: the frame's BRNG value when it counted as a violation
            (so the legacy histogram stays derivable), else None.
        """
        acc = self._accumulator_for(timestamp)
        if acc is None:
            return

        acc.frames += 1
        if violation_score is not None:
            acc.violation_frames += 1
            acc.violation_score_sum += violation_score
        if is_black:
            acc.black_frames += 1
            return

        crop = {}
        for tag in elem.iter('tag'):
            key = tag.get('key')
            if key is None:
                continue
            if key == IDET_REPEATED_KEY:
                value = tag.get('value') or tag.text
                if value:
                    acc.saw_idet = True
                    self.metrics_present.add('idet')
                    if value.strip() != 'neither':
                        acc.repeated_frames += 1
                continue
            mapping = NUMERIC_TAG_KEYS.get(key)
            if mapping is None:
                continue
            name, family = mapping
            raw = tag.get('value') or tag.text
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if not np.isfinite(value):
                # Only PSNR legitimately goes infinite (identical frames).
                if name != 'psnr':
                    continue
                value = PSNR_CLIP
            elif name == 'psnr':
                value = min(value, PSNR_CLIP)
            if family == 'cropdetect':
                # The four edges only mean anything together, so they are held
                # back and validated as a box below.
                crop[name] = value
                continue
            acc.add_sample(name, value)
            self.metrics_present.add(family)

        # cropdetect reports the bounding box of non-black content, and when
        # there is none to find it emits a degenerate box — x1 beyond x2, or y1
        # beyond y2 (seen on 1.2% to 32.6% of frames across the sample
        # reports). A box with negative width is not a measurement, so it is
        # dropped rather than averaged into an edge position.
        if len(crop) == 4 and crop['crop_x2'] > crop['crop_x1'] \
                and crop['crop_y2'] > crop['crop_y1']:
            for name, value in crop.items():
                acc.add_sample(name, value)
            self.metrics_present.add('cropdetect')

    # -- finalization -------------------------------------------------------

    def finalize(self) -> Dict[float, BinProfile]:
        """Close the open bin and return every profile, keyed by bin start."""
        self._close_open()
        return self.profiles

    def _accumulator_for(self, timestamp: float) -> Optional[_BinAccumulator]:
        bin_start = float(int(timestamp // self.bin_size) * self.bin_size)
        if self._open is not None and self._open.bin_start == bin_start:
            return self._open
        if bin_start in self.profiles:
            self.out_of_order_frames += 1
            return None
        self._close_open()
        self._open = _BinAccumulator(bin_start)
        return self._open

    def _close_open(self):
        if self._open is None:
            return
        self.profiles[self._open.bin_start] = _summarize(self._open)
        self._open = None


def _summarize(acc: _BinAccumulator) -> BinProfile:
    """Turn one bin's samples into a BinProfile."""
    s = acc.samples

    def arr(name):
        values = s.get(name)
        return np.asarray(values, dtype=float) if values else None

    def mean(name):
        a = arr(name)
        return float(np.mean(a)) if a is not None else None

    def amax(name):
        a = arr(name)
        return float(np.max(a)) if a is not None else None

    def amin(name):
        a = arr(name)
        return float(np.min(a)) if a is not None else None

    def pct(name, q):
        a = arr(name)
        return float(np.percentile(a, q)) if a is not None else None

    def median(name):
        a = arr(name)
        return float(np.median(a)) if a is not None else None

    def iqr(name):
        a = arr(name)
        if a is None:
            return None
        return float(np.percentile(a, 75) - np.percentile(a, 25))

    deflicker = arr('deflicker')
    deflicker_absmax = float(np.max(np.abs(deflicker))) if deflicker is not None else None

    # One number for "did the frame geometry move in this bin": the widest
    # spread of any single edge. Individual medians stay available alongside.
    edge_iqrs = [iqr(name) for name in ('crop_x1', 'crop_x2', 'crop_y1', 'crop_y2')]
    edge_iqrs = [v for v in edge_iqrs if v is not None]
    crop_edge_iqr_max = max(edge_iqrs) if edge_iqrs else None

    families = {family for name, family in NUMERIC_TAG_KEYS.values() if name in s}
    if acc.saw_idet:
        families.add('idet')

    return BinProfile(
        bin_start=acc.bin_start,
        frames=acc.frames,
        black_frames=acc.black_frames,
        excluded_frames=acc.excluded_frames,
        violation_frames=acc.violation_frames,
        violation_score_sum=acc.violation_score_sum,
        repeated_frames=acc.repeated_frames if acc.saw_idet else None,
        brng_mean=mean('brng'),
        brng_max=amax('brng'),
        satmax_max=amax('satmax'),
        satavg_mean=mean('satavg'),
        tout_mean=mean('tout'),
        tout_p95=pct('tout', 95),
        vrep_mean=mean('vrep'),
        vrep_max=amax('vrep'),
        ydif_mean=mean('ydif'),
        ydif_p95=pct('ydif', 95),
        deflicker_absmax=deflicker_absmax,
        ssim_mean=mean('ssim'),
        ssim_min=amin('ssim'),
        psnr_mean=mean('psnr'),
        yavg_mean=mean('yavg'),
        entropy_mean=mean('entropy'),
        crop_x1_median=median('crop_x1'),
        crop_x2_median=median('crop_x2'),
        crop_y1_median=median('crop_y1'),
        crop_y2_median=median('crop_y2'),
        crop_edge_iqr_max=crop_edge_iqr_max,
        metrics=tuple(sorted(families)),
    )


def violation_histogram_from_profiles(profiles: Dict[float, BinProfile]
                                      ) -> Tuple[Dict[float, int], Dict[float, float]]:
    """Rebuild the legacy (histogram, severity) pair from bin profiles.

    The two dicts period selection currently ranks on are a projection of the
    profiles, and this is the bridge: a caller can adopt the profiles without
    the old inputs changing meaning. Bins with no violations are omitted, as
    they are in the streaming parser.
    """
    histogram = {}
    severity = {}
    for bin_start, profile in profiles.items():
        if profile.violation_frames:
            histogram[bin_start] = profile.violation_frames
            severity[bin_start] = profile.violation_score_sum
    return histogram, severity
