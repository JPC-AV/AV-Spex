"""
study.py: what the tag-weighing study compares. The candidate tags label_sheet.py picks bins
for, and the scoring configurations tag_weights.py replays, kept in one module that both import
(label_sheet.py picks bins for some configurations, and tag_weights.py scores the candidates).

Edit this file to set up a new round: choose candidates from `bin_table.py --screen`, and add
configurations to CONFIGS. Commit it alongside the round's results so they can be reproduced.
"""
from _selection import NEG_PREFIX

from AV_Spex.checks import bin_scoring
from AV_Spex.checks.bin_scoring import MetricSpec

# (column in bin_table's output, direction): '+' means high is suspicious, '-' low.
# New tags chosen from the `bin_table.py --screen` of period-study-2026-10 (present in every
# report, not flat, not redundant with a scored metric, not a luma/motion proxy):
NEW_CANDIDATES = (
    ("udif_max", "+"),          # chroma temporal spike, U   (rho vs YDIF 0.44)
    ("vdif_max", "+"),          # chroma temporal spike, V   (0.45)
    ("ydif_max", "+"),          # single-frame luma jump: sync loss / roll, but also hard cuts
    ("psnr_psnr_y_min", "-"),   # worst inter-field PSNR: fields disagreeing
    ("satlow_mean", "+"),       # raised chroma floor (noise/chroma error); ~independent of luma/motion
    ("tout_max", "+"),          # TOUT spike, beside the scored mean
    ("brng_min", "+"),          # illegal levels sustained through the whole bin
)
# Today's scored metrics, so their weights can be judged on the same labels.
SCORED_CANDIDATES = (
    ("bp_brng_mean", "+"), ("bp_satmax_max", "+"), ("bp_tout_mean", "+"),
    ("bp_vrep_mean", "+"), ("bp_deflicker_absmax", "+"),
)
CANDIDATES = NEW_CANDIDATES + SCORED_CANDIDATES

# Round 2 (--round legality): the legal-floor luma columns, with BRNG as the yardstick.
LEGALITY_CANDIDATES = (("under_black", "+"), ("over_white", "+"), ("bp_brng_mean", "+"))
LEGALITY_CONFIGS = ("+under_black(leg)", "+over_white(leg)", "+legal_floor(leg)")

BASE_METRICS = bin_scoring.METRICS
BASE_WEIGHTS = dict(bin_scoring.FAMILY_WEIGHTS)


def _with(*extra, weights=None):
    return (BASE_METRICS + tuple(extra), dict(BASE_WEIGHTS, **(weights or {})))


# name -> (metrics, family weights). Edit freely; 'baseline' must stay first and unchanged.
CONFIGS = {
    "baseline": (BASE_METRICS, BASE_WEIGHTS),
    "no_instability": (tuple(m for m in BASE_METRICS if m.family != "instability"),
                       {"legality": 0.5, "impulsive": 0.5}),
    "legality_heavy": (BASE_METRICS, {"legality": 0.6, "impulsive": 0.3, "instability": 0.1}),
    "impulsive_heavy": (BASE_METRICS, {"legality": 0.3, "impulsive": 0.6, "instability": 0.1}),
    "+udif_max(imp)": _with(MetricSpec("udif_max", "impulsive")),
    "+vdif_max(imp)": _with(MetricSpec("vdif_max", "impulsive")),
    "+uvdif_max(chroma)": _with(MetricSpec("udif_max", "chroma"), MetricSpec("vdif_max", "chroma"),
                                weights={"chroma": 0.2}),
    "+ydif_max(inst)": _with(MetricSpec("ydif_max", "instability")),
    "+psnr_y_min-(imp)": _with(MetricSpec(NEG_PREFIX + "psnr_psnr_y_min", "impulsive")),
    "+satlow_mean(chroma)": _with(MetricSpec("satlow_mean", "chroma"), weights={"chroma": 0.2}),
    "+tout_max(imp)": _with(MetricSpec("tout_max", "impulsive")),
    "+brng_min(leg)": _with(MetricSpec("brng_min", "legality")),
    # Legal-floor luma (_selection.add_legal_floor_columns): only the part past legal black or
    # white counts, as bin_scoring already floors SATMAX.
    "+under_black(leg)": _with(MetricSpec("under_black", "legality")),
    "+over_white(leg)": _with(MetricSpec("over_white", "legality")),
    "+legal_floor(leg)": _with(MetricSpec("under_black", "legality"), MetricSpec("over_white", "legality")),
}
