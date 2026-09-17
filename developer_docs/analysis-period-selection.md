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

### Per-bin profiles (collected, not yet consumed)

The same streaming pass also builds `parser.bin_profiles` — a `BinProfile` per 10-second bin
(`checks/qctools_bin_profile.py`) summarizing **every** QCTools measure the report carries, not just
BRNG: TOUT/VREP (impulsive damage), SATMAX/SATAVG (chroma legality), YDIF/SSIM/PSNR/deflicker
(temporal behaviour), entropy and YAVG (flatness/exposure), cropdetect edge medians plus the widest
edge IQR (geometry drift), and idet repeated-field counts. Counts (`frames`, `black_frames`,
`excluded_frames`, `violation_frames`, `violation_score_sum`) cover every frame in the bin; the
statistics describe only the **non-black** frames, for the same reason the violation list skips them.

Nothing selects periods from this yet — `violation_histogram`/`violation_severity` remain the ranking
inputs, and `violation_histogram_from_profiles()` reproduces both from the profiles exactly, which is
the bridge for moving selection over.

Three things to know before building on it:

- **Metric availability varies between reports.** Some sidecars carry only signalstats/psnr/astats
  (no cropdetect, entropy, idet, ssim or deflicker) — `JPC_AV_01772` is one, `JPC_AV_01581` has the
  full set. Every metric field is `Optional` and `None` when its tag never appeared;
  `BinProfile.metrics` / `parser.bin_profile_metrics` name the families that did. Consumers must
  renormalize over what is present rather than assume a fixed feature set.
- **Audio frames are interleaved with video frames on the same timeline.** They carry no
  signalstats, so they were always no-ops for the violation list, but they would inflate per-bin
  frame counts and read as out-of-order video. The parse loop now skips any frame whose
  `media_type` is present and not `video` (a missing attribute is still treated as video).
- **One bin's samples are held at a time.** Bins are summarized and released as the timeline
  advances, so memory does not scale with tape length. A frame arriving for an already-closed bin is
  counted in `out_of_order_frames` and dropped rather than merged into a finished profile.

Collection costs roughly 15% of the XML walk (5.5s → 6.3s on a 31-minute 10-bit tape with every
filter present) and no measurable memory. It happens in this pass because the alternative is a
second full walk of the report.

---

## Stage 1 — Candidate periods from the violation distribution

`EnhancedFrameAnalysis._analyze_qctools_violation_distribution()`.

1. **Bin the violations** — prefer `histogram`; else bin the capped `violations` list at 10s.
2. **Exclude bins** (`_bin_excluded`): a bin whose overlap with any black segment or bars region
   exceeds half the bin (5s), or that ends within the **last 30 seconds** of the file
   (end-of-tape static). Noise spikes that escape the per-frame black classifier get caught here.
3. **Clamp the period duration** to the video duration if the configured duration is longer.
4. **Rank bins** by summed severity when available, else by count (`_bin_rank`). The top 10 are
   logged.
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
- **Period placement is not free.** Every period costs two full decodes of its duration in the BRNG
  differential step, so `analysis_period_count × analysis_period_duration` is the real runtime knob.
- **Bars are avoided in two different ways.** The scalar `color_bars_end_time` sets `effective_start`
  (head bars); the `bars_regions` list is merged into `avoid_segments` (head and mid-file bars). Code
  that only honors one of them will place periods on test patterns. Both are gated together by
  `brng_skip_color_bars` in `analyze()` (`brng_bars_end` / `brng_bars_regions`); duplicate-frame
  detection gets the ungated values. Reference SMPTE bars measure BRNG
  ≈ 0.0118, above the `> 0.01` violation threshold, so un-excluded bars read as a dense violation
  cluster and attract periods.
