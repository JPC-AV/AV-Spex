# AV Spex — Analysis Period Selection

How frame analysis decides **which stretches of the tape to look at**. Everything here lives in
`src/AV_Spex/checks/frame_analysis.py` unless noted.

Signalstats and BRNG analysis do not examine every frame — decoding a full tape twice (highlighted
and original) for differential BRNG detection is prohibitively slow. Instead they sample a small
number of fixed-length **analysis periods**. Period *placement* is therefore the single decision
that determines whether the report describes the tape's real problems or a random slice of it.

The guiding principle: **periods should land where the QCTools report says the out-of-range pixels
actually are**, and never on content that can't be meaningfully analyzed (color bars, all-black
segments, end-of-tape static).

---

## Configuration

| Field (`FrameAnalysisConfig`, `utils/config_setup.py`) | Default | Meaning |
|---|---|---|
| `analysis_period_count` | `3` | How many periods to select |
| `analysis_period_duration` | `60` | Length of each period, in seconds |

Both are settable from the GUI Complex tab (`gui/gui_complex_window.py`, the "Analysis Periods" row)
and the checks-profile dialog, but **not** from the CLI — edit the saved config or use the GUI.

Two gotchas:

- Older configs carry the **old** key names (`signalstats_duration`, `signalstats_periods`).
  `ConfigManager._migrate_config_data()` renames them on load (for the checks config and for saved
  checks profiles); the shipped `config/checks_config.json` uses the `analysis_period_*` names, and
  new code must too.
- The removed `signalstats_start_time` key may still appear in old saved configs; it is ignored on
  load. The content start is derived from the color-bars end time (`content_start_after_bars()`).

---

## Inputs to the decision

Gathered in `EnhancedFrameAnalysis.analyze()` before any period is placed:

| Input | Source | Role |
|---|---|---|
| `color_bars_end_time` | qct-parse/CLAMS head-bars consensus, passed in from `processing_mgmt` | Everything before it + 10s of margin is off-limits (only when `brng_skip_color_bars` is on) |
| `bars_regions` | merged head + mid-file bars spans | Excluded like black segments (only when `brng_skip_color_bars` is on) |
| `black_segments` | `QCToolsParser.detect_black_segments()` | Excluded like bars |
| `parser.violation_histogram` | `parse_for_violations_streaming()` side effect | The distribution periods are placed against |
| `parser.violation_severity` | same | Tie-break when counts saturate |
| `violations` (top-100 list) | same | Fallback histogram only; also feeds border detection/thumbnails |
| `parser.bin_profiles` | same | Per-bin summary of every QCTools measure; drives the suitability gate below |

Period selection is only run when it is needed —
`needs_period_selection = border_detection or signalstats or brng_analysis`. Dropped-sample
detection is audio-only and skips it; duplicate-frame detection needs the black segments but not
the periods.

### The violation histogram (the part that matters)

`QCToolsParser.parse_for_violations_streaming()` streams the QCTools XML once and, per frame:

1. skips frames before `color_bars_end_time` and frames inside any `exclude_regions` (bars spans) —
   `analyze()` passes no bars here when `brng_skip_color_bars` is off;
2. skips all-black frames via `_is_black_frame()` — analog tape black carries sub-black noise that
   would otherwise dominate every violation list (thresholds are 10-bit `YMAX < 300`,
   `YHIGH < 115`, `YLOW < 97`, scaled ×0.25 for 8-bit; there is deliberately **no YMIN gate**);
3. counts a violation when `lavfi.signalstats.BRNG > 0.01`;
4. accumulates two 10-second-binned maps:
   - `violation_histogram[bin] += 1`
   - `violation_severity[bin] += violation_score` (the raw BRNG fraction).

The **returned list is capped** at `max_frames` (100) sorted by severity. That cap is why the
histogram exists: on a noisy tape the top-100 list collapses onto the two or three worst bursts, so
using it as "the distribution" put every period in the same place. The histogram is the faithful
picture; the capped list is only the fallback when no histogram is available.

`violation_severity` exists for the opposite failure: on a very noisy tape **counts saturate** —
every frame in many bins violates, so bins tie at ~300 and ranking by count degenerates into
"whatever `sorted` happened to order first". Summed severity separates saturated bins by how bad
they are.

### Per-bin profiles

The same streaming pass also builds `parser.bin_profiles` — a `BinProfile` per 10-second bin
(`checks/qctools_bin_profile.py`) summarizing **every** QCTools measure the report carries, not just
BRNG: TOUT/VREP (impulsive damage), SATMAX/SATAVG (chroma legality), YDIF/SSIM/PSNR/deflicker
(temporal behaviour), entropy and YAVG (flatness/exposure), cropdetect edge medians plus the widest
edge IQR (geometry drift), and idet repeated-field counts. Counts (`frames`, `black_frames`,
`excluded_frames`, `violation_frames`, `violation_score_sum`) cover every frame in the bin; the
statistics describe only the **non-black** frames, for the same reason the violation list skips them.

Ranking still runs off `violation_histogram`/`violation_severity`
(`violation_histogram_from_profiles()` reproduces both from the profiles exactly, which is the bridge
for moving that over too). What the profiles already drive is the suitability gate in Stage 0.

Three things to know before building on it:

- **Metric availability varies between reports.** Some sidecars carry only signalstats/psnr/astats
  (no cropdetect, entropy, idet, ssim or deflicker) — `JPC_AV_01772` is one, `JPC_AV_01581` has the
  full set. Every metric field is `Optional` and `None` when its tag never appeared;
  `BinProfile.metrics` / `parser.bin_profile_metrics` name the families that did. Consumers must
  renormalize over what is present rather than assume a fixed feature set.
- **Audio frames are interleaved with video frames on the same timeline.** They carry no
  signalstats, so `_is_black_frame` and the violation extractor both read them as ordinary
  not-black picture. Both report scans — `parse_for_violations_streaming()` and
  `detect_black_segments()` — now skip any frame whose `media_type` is present and not `video`
  (a missing attribute is still treated as video). In the violation scan they inflated per-bin
  frame counts and read as out-of-order video; in the black scan an audio frame landing more than
  `gap_tolerance` (0.5s) after the last black video frame closes a segment early, or splits one in
  two that `min_duration` then discards. Neither shows on the sample reports, whose audio frames
  arrive about every 0.1s, well inside the tolerance — the guard is for reports with sparser audio.
  `_detect_bit_depth()` still scans all frames, but returns on the first frame carrying UAVG/VAVG,
  which real video frames always do.
- **One bin's samples are held at a time.** Bins are summarized and released as the timeline
  advances, so memory does not scale with tape length. A frame arriving for an already-closed bin is
  counted in `out_of_order_frames` and dropped rather than merged into a finished profile.

Collection costs roughly 15% of the XML walk (5.5s → 6.3s on a 31-minute 10-bit tape with every
filter present) and no measurable memory. It happens in this pass because the alternative is a
second full walk of the report.

---

## Stage 0 — Suitability gate

`checks/bin_suitability.py`, called from `analyze()` right after the violation parse. It answers one
question per bin: **is there picture here to analyze at all?** Bins that fail are merged into
`unsuitable_regions` and joined to `avoid_segments`, so every later stage — candidate placement,
period validation, the shift/refit repair, the count top-up and the even-distribution fallback —
keeps away from them, exactly as they do from black and bars.

The gate exists because the three things it catches all *look like severe damage* to a BRNG-ranked
search and reliably outrank real problems:

| What | How it measures | Why BRNG loves it |
|---|---|---|
| Signal loss / flat field | average luma below broadcast black | whole frame out of range → BRNG ≈ 1.0, the highest score a bin can have |
| Lead-in / end-of-tape static | frames uncorrelated with their predecessors | BRNG 0.2–0.6 sustained over minutes |
| Concealment repetition | long runs of repeated fields | high VREP and BRNG together |

### The two kinds of reason

**Hard reasons stand alone** — they are definitional, not tuned:

- the bin has no picture frames (all black, or all excluded as bars);
- `yavg_mean` is below broadcast black (64 at 10-bit scale, 16 at 8-bit). Legal picture cannot
  average below black without most of it being out of range. The closest healthy measurement across
  the sample set is 65 (`JPC_AV_01056`'s noisy analog black, which black-segment detection already
  excludes), and the LC tape's loss regions average 53–54.

**Soft reasons need corroboration** — two must agree before a bin is gated:

| Reason | Threshold | Healthy range measured | Bad range measured |
|---|---|---|---|
| frames decorrelated from predecessor | `ssim_mean < 0.35` | 0.80–0.95 (p1 across 6 reports) | 0.18–0.33 |
| repeated fields | `> 35%` of picture frames | up to 32% in low-motion passages | 35–94% |
| flat field | `entropy_mean < 0.25` | down to 0.40 | 0.30 (and caught by the hard rule anyway) |
| luma uncorrelated frame to frame | `ydif_mean > 0.20 × full scale` | peaks at 0.23 in end-of-tape bins the tail rule drops | 0.40+ |
| line repetition | `vrep_mean > 0.25` | ~0 | ~0.99 on flat-field loss |

Single-signal gating is exactly the failure mode being avoided, so one reason is never enough: a
cut-heavy passage has low SSIM, a locked-off interview has repeated fields, a noisy transfer has low
entropy. `JPC_AV_01663` @ 1860s (SSIM 0.753, YDIF 124) and `JPC_AV_01710` @ 1250s (28% repeated
fields) are both healthy content that a single rule would have thrown away.

### Guards

- **Availability.** Soft rules are evaluated only over the measures the report carries. With fewer
  than two available the soft gate is skipped entirely — there is nothing to corroborate with. A
  signalstats-only report still gets both hard rules, and can still pair YDIF with VREP.
- **Sample size.** A bin needs `MIN_PICTURE_FRAMES` (30) non-black frames before soft rules apply;
  below that its statistics come from a handful of frames (`JPC_AV_01710` @ 220s is 299/300 black).
- **Whole-tape guard.** If the soft rules would gate more than 60% of the content bins, the tape's
  abnormal *is* its normal and the soft gate is dropped wholesale, with `note` recording why.
  Hard reasons are never dropped. The guard needs `MIN_BINS_FOR_GUARD` (10) content bins before a
  percentage means anything.

### Measured effect

Replaying stage 1 + stage 2 over ten sample reports: **placement is byte-identical on all nine
healthy tapes** (including the 02041/02212 ground truth from the earlier redesign) and moves only on
the LC tape with known damage:

```
21459403   before [95, 2065, 2865]   after [135, 2025, 2235]
           unanalyzable share of each period:
           before  75% /  33% / 0%      after  8% / 8% / 0%
```

The gated spans there are 0–40s (black), 80–140s (lead-in static), 2080–2100s (signal loss) and
2940–3010s (end-of-tape). Note 2940–2960 is *not* covered by the last-30s rule on a 3000s file.

---

## Stage 0b — Composite scoring

`checks/bin_scoring.py`, called right after the suitability gate. It scores every
*analyzable* bin, and that score — not BRNG density — is what Stage 1 ranks on.

### Why BRNG alone stopped being enough

Measured per-bin p90/median across the sample reports:

| Metric | p90 / median |
|---|---|
| **BRNG** (what ranking used) | **1.16 – 1.95** |
| TOUT | 1.41 – 2.80 |
| SATAVG | 1.29 – 3.08 |
| YDIF | 1.80 – 9.49 |
| deflicker | 4.11 – 131.1 |
| cropdetect edge IQR | median 0, p90 0–5 (rare spikes) |

BRNG is the flattest signal available, and on a noisy tape it is flat *and*
saturated: all 187 content bins of `JPC_AV_01772` have >95% of frames violating, so
ranking separated them by ~25% differences in mean out-of-range share. Worse, a bin
whose problem is a dropout burst can carry no BRNG violation at all, and was
therefore invisible — the histogram only ever contained bins that tripped
`BRNG > 0.01`. With scores, **every analyzable bin is a candidate**.

### Families

| Family | Weight | Metrics | Why |
|---|---|---|---|
| `legality` | 0.4 | `brng_mean` (floor 0.01), `satmax_max` (floor = illegal chroma) | what BRNG/signalstats analysis exists to characterize |
| `impulsive` | 0.4 | `tout_mean` (floor 0.012), `vrep_mean` | dropouts and the deck's concealment of them — the gap this closes |
| `instability` | 0.2 | `deflicker_absmax` | brightness deviating from the frame's temporal neighbours; weighted lower because its one metric is a per-bin maximum, so a single hard cut can set it |

Within a family the **strongest** metric speaks for it (two weak signals must not add
up to one strong one); across families the weighted sum, with weights renormalized
over the families the report can actually measure, so a signalstats-only sidecar
scores on the same 0–1 scale as a rich one.

**cropdetect is deliberately not scored**, though the profiles still collect its edge medians.
It reports the bounding box of non-black content, so the box moves whenever the content changes
shape or brightness rather than when the picture is unstable. Measured across the sample set: absent
from 3 of the 9 JPC reports (including both ground-truth files); where present, degenerate boxes
(`x2 <= x1` or `y2 <= y1`, emitted when there is no non-black content to bound) run from 1.2% to
32.6% of frames; and rejecting those degenerate boxes reproduced the *same* selection on every JPC
file, which is what ruled the metric out — its influence was never detector failure, it was content.
The bins it promoted were a dark scene on one tape (YAVG 218 against a file median of 355) and a
bright one on another. `BinProfiler` now drops degenerate boxes at collection time regardless, so
the edge medians are trustworthy for any future use.

### TOUT: the mean, over a floor

Two calibration decisions here came from operator spot-checks of four verified bins — three
regions with confirmed dropout/head-switching damage, and one confirmed to have illegal pixels
from graphics but *no* dropout or instability.

**The mean, not the 95th percentile.** A bin full of cuts and fades has a high TOUT p95 (a handful
of outlier frames) and a low mean; sustained dropout raises both. On the verified bins p95 gave
0.0219–0.0407 for the true positives against 0.0190 for the false positive — overlapping — while
the mean gave 0.0168–0.0214 against 0.0086, a clean 2× gap.

**An absolute floor of 0.012.** Rank normalization guarantees that *some* bin tops the impulsive
family on every tape, including tapes with no dropouts at all. The floor is what lets the family
score zero across a clean transfer and leave selection to legality — `JPC_AV_01056` has no bin
above it; `JPC_AV_02212` has 20 of 141.

**A motion residual was tried and rejected.** TOUT rank correlates with YDIF rank (+0.44 to +0.88
across the sample reports), so subtracting motion looked like the fix. It is not: on the verified
bins no subtraction coefficient separated true from false positives, and at full strength the false
positive *beat* a true positive (0.76 against 0.00). Damaged passages are often the high-motion ones
— dubbed material is both a generation down and cut quickly — so removing motion removes real
damage with it. The statistic, not the confound, was the problem.

**YDIF is deliberately not scored.** It measures motion, which is content rather than
damage — the busiest bin of a healthy tape is a fast cut. Stage 0 uses it only as an
extreme-value rule, where it means something different.

### Normalization: within-file rank of the excess over a floor

Not a z-score. Two properties of the data rule that out:

- VREP is exactly 0 in 89–100% of content bins, so its median *and* MAD are both 0 — every
  z-score divides by zero.
- Rank handles the zero-heavy case natively. Ties resolve **downward** (rank = share
  of bins strictly below), so the quiet bins all score 0 and a lone spike lands near 1.
  An average-rank convention would instead hand every quiet bin half a point for being
  quiet.

A metric with no spread contributes nothing, with no special case: every bin ties, so
every bin ranks 0. Unmeasured (`None`) ranks 0 too — never evidence of a problem.
Where a metric has a meaningful absolute floor (BRNG's existing violation threshold,
the illegal-chroma level for SATMAX) the **excess over it** is what gets ranked, so
entirely legal bins are not ranked against one another.

Unsuitable bins are excluded from the ranking population as well as from the result:
they are not places a period can go, and including them would shift every other bin's
rank.

**The score is a targeting score, not a severity measure.** It says where the worst of
*this* tape is; the top bin of a pristine transfer scores the same 1.0 as the top bin
of a ruined one. Period selection always takes the top N, so that is what it needs —
but the number must never be shown to a user as a quality figure.

### Measured effect

Replaying stage 1 + stage 2 over ten sample reports, placement changes on nine of ten
(`JPC_AV_01581` is unchanged). Notably `JPC_AV_02041` moves off `[465, 995, 1475]` to
`[225, 465, 985]`, giving up the 24:35 period cited as ground truth in the earlier
redesign — an accepted trade, decided deliberately: its new top bins score impulsive
1.00 / legality 0.97, evidence the old ranking could not see. Chosen bins' dominant
families across the set run roughly two-thirds `impulsive`, one-third `legality`.

---

## Stage 1 — Candidate periods from the violation distribution

`EnhancedFrameAnalysis._analyze_qctools_violation_distribution()`.

1. **Choose the candidate bins** — every scored bin when Stage 0b ran; else the `histogram`;
   else the capped `violations` list binned at 10s.
2. **Exclude bins** (`_bin_excluded`): a bin whose overlap with any black segment or bars region
   exceeds half the bin (5s), or that ends within the **last 30 seconds** of the file
   (end-of-tape static). Noise spikes that escape the per-frame black classifier get caught here.
3. **Clamp the period duration** to the video duration if the configured duration is longer.
4. **Rank bins** by composite score when available (Stage 0b), else by summed severity, else by
   count (`_bin_rank`). The top 10 are logged with the families that drove them.
5. **Place periods**, densest bin first, centering the period on the bin and clamping it inside the
   file (`_candidate_start`). Placement runs in **two passes**:
   - Pass 1 requires each new period to start at least `2 × period_duration` from every period
     already chosen — so periods cover *distinct* problem regions instead of stacking on one burst.
   - Pass 2 (only if pass 1 came up short) relaxes the separation to `1 × period_duration`, i.e.
     simple non-overlap, for tapes whose violations really are concentrated in one place.

Output: up to `num_periods` `(start, duration)` tuples, sorted by start. These are *candidates* —
they still have to survive stage 2.

---

## Stage 2 — Final selection

`IntegratedSignalstatsAnalyzer._find_analysis_periods()`. Called from `analyze_with_signalstats()`;
this is where the periods that actually get analyzed are fixed.

`effective_start = max(content_start, content_start_after_bars(color_bars_end))` — head bars end
plus `BARS_SAFETY_MARGIN_SECONDS` (10s). The margin is added once, here; callers pass
`content_start_time=0` (the first signalstats pass used to pre-add it, giving 20s). The BRNG
fallback and post-refinement validation use `content_start_after_bars()` directly. With
`brng_skip_color_bars` off, `analyze()` passes a bars end of 0, so the effective start is 10s.

Three prioritized sources:

1. **QCTools violation clusters** — the stage-1 candidates, used as-is if present.
2. **Border-detection quality hints** — `border_data.quality_frame_hints`, the timestamps of the
   10 best-exposed frames sophisticated border detection measured (simple mode produces none). Each
   hint becomes a period centered on it; only used if at least `count` hints land after
   `effective_start` and every resulting period leaves 30s of tail, otherwise it falls through.
3. **Even distribution** — the last resort when the tape has no BRNG violations at all. Periods are
   spread evenly across `[effective_start, duration - 30]`. If that window can't fit
   `count × duration`, as many whole periods as fit are used; if it can't fit even one, a single
   truncated period covers what's available.

Then two correction passes:

- **Black-segment validation** (`_validate_periods_against_black_segments`) — see below.
- **Count guarantee** (`_fill_periods_to_count`) — if clusters or validation left fewer periods than
  requested, top up with evenly spaced ones. Candidate starts are drawn from a grid of
  `max(2 × count, 4)` slots across the content window (denser than needed, so rejections still leave
  alternatives); a candidate is skipped if it overlaps an existing period or overlaps black segments
  by more than 25% of its duration. This exists because violation-cluster selection legitimately
  returns fewer than `count` periods on a clean tape, and the report should still sample the
  requested number of places.

The final list is returned sorted by start time.

### Black-segment validation and repair

`_validate_periods_against_black_segments()`, applied to every
candidate regardless of which source produced it. Note that "black segments" here always means the
merged `avoid_segments` list — detected black **plus** detected bars regions.

For each period, total overlap with all avoid-segments is summed:

- **≤ 25% overlap** → keep as-is.
- **> 25%** → try to **shift** it (`_shift_period_away_from_black`): search outward from the original
  start in 5-second steps, alternating forward/backward, up to half the video length. A candidate
  position is accepted if it stays inside `[effective_start, duration - 10]`, overlaps
  avoid-segments by **≤ 10%** (a tighter bar than the 25% that triggered the shift), and is at
  least one period-duration away from every already-validated start.
- **No valid shift** → try to **shrink and refit** (`_fit_period_in_content_gap`): build the list of
  non-black gaps between `effective_start` and `duration - 10`, try the largest first, keep the
  original start if the gap still leaves ≥ 10s, else start at the gap head, and truncate the period
  to fit. Minimum viable period is 10s. This exists for short tapes whose entire non-black content
  is shorter than one configured period.
- **Still nothing** → the period is dropped (and `_fill_periods_to_count` may later replace it).

**Last-resort keep**: `_validate_periods_against_black_segments` never returns empty when it was
given candidates. If every one is dropped, the candidate with the *least* black overlap is kept and
`self.last_resort_period_note` is set (sticky for the run — validation is called from several
places, and any one firing means the sample is compromised). Analyzing a partly-black window and
labeling it beats skipping the file's BRNG analysis silently.

**Confidence signal**: `resolve_period_confidence(last_resort_note, coverage_note)` folds the
signals into `BRNGAnalysisResult.period_confidence` (`PERIOD_CONFIDENCE_LEVELS` = `normal` |
`partial_coverage` | `last_resort`) plus a `period_confidence_note` joining every reason.
`last_resort` outranks `partial_coverage` — black content makes the numbers wrong, partial coverage
only makes them incomplete. `generate_frame_analysis_html` renders anything non-`normal` as a yellow
caveat box above the BRNG figures (`_render_frame_brng_html` in `utils/generate_report.py`).

Note that an empty period list therefore means *no candidates existed at all* (or the video
duration was unknown), not "all were rejected" — `DifferentialBRNGAnalyzer.analyze_with_differential_detection`
returns no result in that case, with a `could_not_run_reason` the report shows, rather than falling
back to a fixed window (removed in d58a800, since that window would have measured the very black
content selection had just rejected).

---

## Stage 3 — Refinement after signalstats

Signalstats runs on the stage-2 periods first, and its per-period findings can then *change* the
periods that BRNG analysis uses.

**Per-period diagnosis** (`analyze_with_signalstats`): each period is measured twice — QCTools BRNG
over the **full frame** (black frames skipped) and an ffprobe `signalstats` pass over the **active
area only** — and labeled. A frame counts as flagged from a single out-of-range pixel (BRNG > 0) on
both sides. With border detection off there is no active area, so periods are measured on the full
frame only and get no diagnosis:

| Diagnosis | Condition |
|---|---|
| `border_violations` | full-frame % exceeds active-area % by > 5, and active < 30% |
| `content_violations` | active-area violations > 10% |
| `minimal_violations` | neither |

**Refinement** (`_refine_periods_from_signalstats`): each current period that has a diagnosis is
scored `content_violations` (100 + active%) > `border_violations` (50 + active%) >
`minimal` (active% alone). Periods scoring **< 5** — essentially no active-area signal, nothing for
the differential detector to find — are replaced by unused stage-1 QCTools candidates that don't
overlap a current period. Replacements are re-validated against the avoid-segments. If there are no
low-value periods or no spare candidates, the list is returned unchanged.

**Downstream effects of the diagnosis** (period placement aside):

- *Sensitivity and sampling density* in BRNG analysis (`analyze_with_differential_detection`,
  via the `UpstreamAnalysisContext` built by `_build_upstream_context`, which only includes periods
  that have a diagnosis):
  - sensitivity is `strict` for `border_violations` and `minimal_violations`, `normal` for
    `content_violations` (and for periods with no diagnosis);
  - a period whose active area has < 1% flagged frames **and** max BRNG < 0.01% is sampled lightly
    (30 frames); one with > 30% flagged **or** max BRNG > 1% is sampled densely (200 frames);
    anything else uses the default (QCTools-targeted frames topped up to 50–200).
- *Thumbnail choice* (`_select_diverse_violations_for_thumbnails`):
  violations from `content_violations` periods are offered first, border-dominated ones last —
  border problems are already documented by the border-detection section.

---

## Fallback when signalstats is disabled

BRNG analysis can run without signalstats (in `analyze()`). In that case:

1. Use the stage-1 QCTools candidate periods directly if there are any;
2. else build evenly distributed periods over
   `[content_start_after_bars(bars_end), video_duration - 10]`, spaced `content_duration / (count + 1)`
   (unknown duration → no periods, BRNG reports it could not run);
3. either way, run the result through `_validate_periods_against_black_segments()`.

This path skips the count guarantee and the refinement pass — it has no signalstats findings to
refine against.

---

## Where the periods surface

- **JSON**: `{video_id}_enhanced_frame_analysis.json` → `signalstats.analysis_periods` and
  `brng_analysis.analysis_periods`, each a list of `[start_seconds, duration]`. Detected black
  segments are alongside under `black_segments`.
- **HTML report**: `get_frame_analysis_periods()` (`utils/generate_report.py`) reads them
  (signalstats first, then brng_analysis, then standalone sidecars) and returns
  `(start, end)` tuples; `get_frame_analysis_black_segments()` does the same for black segments.
  Both are drawn on the eval-bars failure timeline (`make_eval_bars_timeline_html`) — periods as
  shaded bands behind the traces, so a reader can see *why* the periods sit where they do.
  The timeline's dashed teal `BRNG` trace is the same measure that drives period placement, so the
  trace peaks and the shaded bands should visibly coincide.

---

## Gotchas

- **Never rank periods off the capped violations list.** `parse_for_violations_streaming()` returns
  the top 100 frames by severity; its temporal spread is an artifact of the cap, not the tape. Use
  `violation_histogram` / `violation_severity`.
- **Counts saturate before severity does.** On noisy tapes every frame in a bin violates. Any new
  ranking heuristic needs the severity tie-break or it silently degenerates.
- **The two overlap thresholds are intentionally different**: 25% triggers a repair, but a repaired
  position must get under 10%. Loosening the second to 25% lets a shifted period settle right back
  against a black segment.
- **A repaired period must clear the periods that come after it, not just the validated ones.**
  `_validate_periods_against_black_segments` walks the list in order; a shift searching outward can
  walk the first period right up to the second, which has not been validated yet. Composite ranking
  packs candidates more tightly (the relaxed separation pass fires more often), which made the
  collision reachable — `JPC_AV_03796` produced two periods 20 seconds apart. The repair now checks
  `validated + periods[index+1:]`, and the function sorts its result, because a repair can move a
  period past its neighbour and two of the three call sites used the order as returned.
- **Avoid segments must be merged before use.** The validators measure a period's overlap with the
  avoid list by *summing* per-segment overlaps, so two spans describing the same seconds count them
  twice and a period that is 10% black reads as 20% and gets shifted off a fine position. Sources
  genuinely overlap — a bars flash inside a black tail, an unanalyzable bin inside a black segment —
  so `merge_avoid_segments()` normalizes them where the list is built. It is also what keeps the
  suitability gate from silently re-weighting the black segments it duplicates.
- **Period placement is not free.** Every period costs two full decodes of its duration in the BRNG
  differential step, so `analysis_period_count × analysis_period_duration` is the real runtime knob.
- **Bars are avoided in two different ways.** The scalar `color_bars_end_time` sets `effective_start`
  (head bars); the `bars_regions` list is merged into `avoid_segments` (head and mid-file bars). Code
  that only honors one of them will place periods on test patterns. Both are gated together by
  `brng_skip_color_bars` in `analyze()` (`brng_bars_end` / `brng_bars_regions`); duplicate-frame
  detection gets the ungated values. Reference SMPTE bars measure BRNG
  ≈ 0.0118, above the `> 0.01` violation threshold, so un-excluded bars read as a dense violation
  cluster and attract periods.
