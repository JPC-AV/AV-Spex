#!/usr/bin/env python3
"""Which stretches of a tape can be meaningfully analyzed at all.

Period selection has always excluded three things: all-black segments,
detected color bars, and the last 30 seconds of the file. Those cover the
obvious cases, but not the ones that *look* like severe damage to a
BRNG-ranked search and therefore attract periods:

* **Signal loss / flat field** — the deck or TBC emits a synthetic frame while
  the tape gives it nothing. Average luma sits below broadcast black, so BRNG
  reads ~1.0 (every pixel out of range) and the bin outranks every real
  problem on the tape.
* **Lead-in or end-of-tape static** — frames uncorrelated with their
  predecessors. High BRNG, high TOUT, and nothing a signalstats or border
  measurement can say anything useful about.
* **Concealment repetition** — long runs of repeated fields where the deck
  papered over dropouts.

Measuring those and reporting the numbers as if they described the picture is
worse than not sampling them: the report ends up characterizing the tape by
its dead spots. This module decides, per 10-second bin, whether there is
picture there to analyze. It does not rank anything — how *interesting* a
suitable bin is belongs to scoring.

**Structure of the decision.** Two hard reasons stand alone, because they are
definitional rather than tuned: a bin with no picture frames, and a bin whose
average luma is below broadcast black (there is no picture, only sub-black
noise). Everything else is a *soft* reason and needs corroboration — two
independent measures must agree before a bin is gated. That is the same
screen-then-confirm shape used elsewhere in this codebase, and it is what
keeps legitimately difficult content (a cut-heavy passage, a noisy stretch,
low-motion talking heads) from being thrown away.

**Metric availability varies between reports** (see `qctools_bin_profile`), so
the soft rules are evaluated only over the measures a report actually carries.
When fewer than two are available the soft gate is not applied at all: with one
measure there is nothing to corroborate with, and a single-signal gate on this
data is exactly the failure mode being avoided. Reports with only signalstats
still get both hard rules.

Thresholds below were measured across nine sample reports (eight healthy JPC
tapes, one LC tape with known signal loss and static). No soft rule fires on
any bin of the healthy tapes; on the damaged tape the gate marks the lead-in
static, the sub-black signal loss, and the end-of-tape hash.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from AV_Spex.checks.qctools_bin_profile import PROFILE_BIN_SIZE, BinProfile

# --- Hard reasons ---------------------------------------------------------

# Broadcast black, in the report's own scale. A bin whose *average* luma is
# below it has more of the frame under black than over it — the deck's
# flat-field output during signal loss measures YMIN=YAVG=YMAX=64 exactly, and
# the LC tape's loss regions average 53. The closest healthy bin measured is
# 65 (JPC_AV_01056's noisy analog black, which black-segment detection already
# excludes), so the margin against real picture is comfortable: legal picture
# cannot average below black without most of it being out of range.
BLACK_LEVEL_10BIT = 64.0
BLACK_LEVEL_8BIT = 16.0

# --- Soft reasons (need two) ----------------------------------------------

# Below this many picture frames a bin's statistics describe too small a
# sample to gate on — a bin that is 299/300 black yields one frame's numbers.
# Those bins are already handled by the black rules.
MIN_PICTURE_FRAMES = 30

# Frame decorrelated from its predecessor. Healthy tapes bottom out at 0.80
# (1st percentile across six reports carrying ssim); static measures 0.18-0.33.
SSIM_DECORRELATED = 0.35

# Share of frames idet flags as repeated fields. Healthy tapes reach 0.32 in
# genuinely low-motion passages; concealment and static run 0.35-0.94.
REPEATED_FIELD_FRACTION = 0.35

# Normalized luma entropy — a flat field carries almost none. Healthy bins
# reach down to 0.40, so this is deliberately well below that: it is a
# corroborating signal for content that is nearly featureless, not a
# darkness detector.
ENTROPY_FLAT = 0.25

# Mean absolute frame-to-frame luma difference, as a share of full scale.
# Healthy tapes peak at 0.23 of full scale in their busiest bins (and those
# are end-of-tape bins the tail rule already drops); hash measures 0.40+.
YDIF_DECORRELATED_FRACTION = 0.20

# Vertical line repetition — the deck repeating lines to conceal dropouts.
# Flat-field signal loss measures ~0.99.
VREP_CONCEALMENT = 0.25

# Number of soft reasons that must agree before a bin is gated.
SOFT_REASONS_REQUIRED = 2

# The guard below compares gated bins against the whole tape, so it needs
# enough bins for that share to mean anything: on a 30-second file two gated
# bins are most of it without saying anything about the tape's character.
# Under this many content bins the soft gate is applied as measured, and the
# usual last-resort-keep protects period selection if everything is gated.
MIN_BINS_FOR_GUARD = 10

# If the soft rules would gate more than this share of the content bins, the
# tape's "abnormal" is its normal — a tape that is mostly static has no better
# place to sample, and gating it all would leave selection with nothing. The
# soft gate is dropped wholesale in that case and the reason recorded, rather
# than silently sampling whatever survived.
MAX_SOFT_GATED_FRACTION = 0.6


@dataclass(frozen=True)
class BinVerdict:
    """Whether one bin holds analyzable picture, and why not when it doesn't."""
    bin_start: float
    suitable: bool
    reasons: Tuple[str, ...] = ()       # empty when suitable
    soft_metrics_available: int = 0
    # True when a rule that needs no corroboration gated this bin. Kept
    # separate from the reason text so callers can tell a definitional
    # exclusion from a measured one without parsing strings.
    hard: bool = False


@dataclass
class SuitabilityAssessment:
    """Per-bin verdicts plus the merged spans they imply."""
    verdicts: Dict[float, BinVerdict] = field(default_factory=dict)
    # Contiguous unsuitable bins, merged into (start, end) spans. These join
    # black segments and bars regions as places periods are kept away from.
    unsuitable_regions: List[Tuple[float, float]] = field(default_factory=list)
    # False when the soft gate was skipped — either too few metrics to
    # corroborate with, or it would have gated most of the tape.
    soft_gate_applied: bool = True
    note: Optional[str] = None

    def reasons_for(self, bin_start: float) -> Tuple[str, ...]:
        verdict = self.verdicts.get(bin_start)
        return verdict.reasons if verdict else ()


def _black_level(bit_depth_10: bool) -> float:
    return BLACK_LEVEL_10BIT if bit_depth_10 else BLACK_LEVEL_8BIT


def _full_scale(bit_depth_10: bool) -> float:
    return 1023.0 if bit_depth_10 else 255.0


def _hard_reasons(profile: BinProfile, bit_depth_10: bool) -> List[str]:
    """Reasons that stand on their own — no corroboration needed."""
    reasons = []
    if profile.picture_frames <= 0:
        reasons.append('no picture frames (all black or excluded)')
        return reasons
    if profile.yavg_mean is not None and profile.yavg_mean < _black_level(bit_depth_10):
        reasons.append(f'average luma below broadcast black '
                       f'({profile.yavg_mean:.0f}) — no picture, only sub-black noise')
    return reasons


def _soft_reasons(profile: BinProfile, bit_depth_10: bool) -> Tuple[List[str], int]:
    """Corroborating reasons, plus how many of them could be measured here."""
    reasons = []
    available = 0

    if profile.ssim_mean is not None:
        available += 1
        if profile.ssim_mean < SSIM_DECORRELATED:
            reasons.append(f'frames decorrelated from their predecessors '
                           f'(SSIM {profile.ssim_mean:.2f})')

    if profile.repeated_frames is not None and profile.picture_frames > 0:
        available += 1
        fraction = profile.repeated_frames / profile.picture_frames
        if fraction > REPEATED_FIELD_FRACTION:
            reasons.append(f'{fraction * 100:.0f}% of frames are repeated fields')

    if profile.entropy_mean is not None:
        available += 1
        if profile.entropy_mean < ENTROPY_FLAT:
            reasons.append(f'almost no picture detail (entropy {profile.entropy_mean:.2f})')

    if profile.ydif_mean is not None:
        available += 1
        if profile.ydif_mean > YDIF_DECORRELATED_FRACTION * _full_scale(bit_depth_10):
            reasons.append(f'luma uncorrelated frame to frame (YDIF {profile.ydif_mean:.0f})')

    if profile.vrep_mean is not None:
        available += 1
        if profile.vrep_mean > VREP_CONCEALMENT:
            reasons.append(f'sustained line repetition (VREP {profile.vrep_mean:.2f})')

    return reasons, available


def assess_bins(profiles: Dict[float, BinProfile],
                bit_depth_10: bool = True,
                bin_size: float = PROFILE_BIN_SIZE) -> SuitabilityAssessment:
    """Decide which bins hold analyzable picture.

    profiles: `QCToolsParser.bin_profiles`.
    bit_depth_10: `QCToolsParser.bit_depth_10` — the luma thresholds are in
        the report's own scale.

    Returns an assessment whose `unsuitable_regions` can be added to the
    black/bars spans period selection already avoids.
    """
    if not profiles:
        return SuitabilityAssessment()

    hard_gated: Dict[float, List[str]] = {}
    soft_gated: Dict[float, List[str]] = {}
    available_by_bin: Dict[float, int] = {}
    content_bins = 0

    for bin_start in sorted(profiles):
        profile = profiles[bin_start]
        hard = _hard_reasons(profile, bit_depth_10)
        if hard:
            hard_gated[bin_start] = hard
            available_by_bin[bin_start] = 0
            if profile.picture_frames > 0:
                content_bins += 1
            continue

        content_bins += 1
        if profile.picture_frames < MIN_PICTURE_FRAMES:
            available_by_bin[bin_start] = 0
            continue

        soft, available = _soft_reasons(profile, bit_depth_10)
        available_by_bin[bin_start] = available
        if available >= SOFT_REASONS_REQUIRED and len(soft) >= SOFT_REASONS_REQUIRED:
            soft_gated[bin_start] = soft

    note = None
    soft_gate_applied = True
    if soft_gated and content_bins >= MIN_BINS_FOR_GUARD:
        gated_fraction = len(soft_gated) / content_bins
        if gated_fraction > MAX_SOFT_GATED_FRACTION:
            note = (f'{gated_fraction * 100:.0f}% of the tape measured as '
                    f'unanalyzable (static, signal loss or concealment), so that '
                    f'is this tape\'s normal — periods were placed without the '
                    f'suitability gate')
            soft_gated = {}
            soft_gate_applied = False

    verdicts = {}
    for bin_start in sorted(profiles):
        reasons = tuple(hard_gated.get(bin_start, ())) + tuple(soft_gated.get(bin_start, ()))
        verdicts[bin_start] = BinVerdict(
            bin_start=bin_start,
            suitable=not reasons,
            reasons=reasons,
            soft_metrics_available=available_by_bin.get(bin_start, 0),
            hard=bin_start in hard_gated,
        )

    return SuitabilityAssessment(
        verdicts=verdicts,
        unsuitable_regions=merge_unsuitable_regions(verdicts, bin_size),
        soft_gate_applied=soft_gate_applied,
        note=note,
    )


def merge_unsuitable_regions(verdicts: Dict[float, BinVerdict],
                             bin_size: float = PROFILE_BIN_SIZE
                             ) -> List[Tuple[float, float]]:
    """Merge contiguous unsuitable bins into (start, end) spans."""
    regions: List[Tuple[float, float]] = []
    for bin_start in sorted(verdicts):
        if verdicts[bin_start].suitable:
            continue
        bin_end = bin_start + bin_size
        if regions and abs(regions[-1][1] - bin_start) < 1e-6:
            regions[-1] = (regions[-1][0], bin_end)
        else:
            regions.append((bin_start, bin_end))
    return regions


def describe_assessment(assessment: SuitabilityAssessment,
                        max_regions: int = 6) -> List[str]:
    """Human-readable lines for the log, one per unsuitable span."""
    lines = []
    for start, end in assessment.unsuitable_regions[:max_regions]:
        reasons = assessment.reasons_for(start)
        detail = f" — {reasons[0]}" if reasons else ""
        lines.append(f"    {start:.1f}s - {end:.1f}s{detail}")
    remaining = len(assessment.unsuitable_regions) - max_regions
    if remaining > 0:
        lines.append(f"    ... {remaining} more")
    return lines
