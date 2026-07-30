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

Both are settable from the GUI Complex tab (`gui/gui_complex_window.py`, "Periods" / "duration"
inputs) but **not** from the CLI — edit the saved config or use the GUI.

Two gotchas:

- The shipped `config/checks_config.json` still carries the **old** key names
  (`signalstats_duration`, `signalstats_periods`). `ConfigManager._migrate_config_data()`
  (`utils/config_manager.py:217-225`) renames them on load, so the dataclass defaults and the JSON
  agree; new code must use the `analysis_period_*` names.
- `signalstats_start_time` (also in the JSON) is **vestigial**. The content start is derived from
  the color-bars end time at call time (`analyze()` passes
  `content_start_time = color_bars_end_time + 10`), not from this field.

---

## Inputs to the decision

Gathered in `EnhancedFrameAnalysis.analyze()` before any period is placed
(`frame_analysis.py:5076-5153`):

| Input | Source | Role |
|---|---|---|
| `color_bars_end_time` | qct-parse/CLAMS head-bars consensus, passed in from `processing_mgmt` | Everything before it + 10s of margin is off-limits |
| `bars_regions` | merged head + mid-file bars spans | Excluded like black segments |
| `black_segments` | `QCToolsParser.detect_black_segments()` | Excluded like bars |
| `parser.violation_histogram` | `parse_for_violations_streaming()` side effect | The distribution periods are placed against |
| `parser.violation_severity` | same | Tie-break when counts saturate |
| `violations` (top-100 list) | same | Fallback histogram only; also feeds border detection/thumbnails |

Period selection is only run when it is needed —
`needs_period_selection = border_detection or signalstats or brng_analysis`. Dropped-sample
detection is audio-only and skips it; duplicate-frame detection needs the black segments but not
the periods.

### The violation histogram (the part that matters)

`QCToolsParser.parse_for_violations_streaming()` (`frame_analysis.py:348-484`) streams the QCTools
XML once and, per frame:

1. skips frames before `color_bars_end_time` and frames inside any `exclude_regions` (mid-file bars);
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

---

## Stage 1 — Candidate periods from the violation distribution

`_analyze_qctools_violation_distribution()` (`frame_analysis.py:6413-6536`).

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

`SignalstatsAnalyzer._find_analysis_periods()` (`frame_analysis.py:3291-3365`). Called from
`analyze_with_signalstats()`; this is where the periods that actually get analyzed are fixed.

`effective_start = max(content_start, color_bars_end or 0) + 10` — the 10s is safety margin past
the bars.

Three prioritized sources:

1. **QCTools violation clusters** — the stage-1 candidates, used as-is if present.
2. **Border-detection quality hints** — `border_data.quality_frame_hints`, timestamps border
   detection found visually clean. Each hint becomes a period centered on it; only used if enough
   hints land after `effective_start` and leave 30s of tail, otherwise it falls through.
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

`_validate_periods_against_black_segments()` (`frame_analysis.py:3413-3483`), applied to every
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

---

## Stage 3 — Refinement after signalstats

Signalstats runs on the stage-2 periods first, and its per-period findings can then *change* the
periods that BRNG analysis uses.

**Per-period diagnosis** (`analyze_with_signalstats`, `frame_analysis.py:3100-3113`): each period is
measured twice — QCTools BRNG over the **full frame** and an ffprobe `signalstats` pass over the
**active area only** — and labeled:

| Diagnosis | Condition |
|---|---|
| `border_violations` | full-frame % exceeds active-area % by > 5, and active < 30% |
| `content_violations` | active-area violations > 10% |
| `minimal_violations` | neither |

**Refinement** (`_refine_periods_from_signalstats`, `frame_analysis.py:6296-6372`): each current
period is scored `content_violations` (100 + active%) > `border_violations` (50 + active%) >
`minimal` (active% alone). Periods scoring **< 5** — essentially no active-area signal, nothing for
the differential detector to find — are replaced by unused stage-1 QCTools candidates that don't
overlap a current period. Replacements are re-validated against the avoid-segments. If there are no
low-value periods or no spare candidates, the list is returned unchanged.

**Downstream effects of the diagnosis** (period placement aside):

- *Sampling density* in BRNG analysis (`analyze_with_differential_detection`,
  `frame_analysis.py:1585-1620`): a `minimal_violations` period with < 1% active-area violations is
  sampled lightly (30 frames); a `content_violations` period with > 10% is sampled densely (200
  frames); sensitivity is set `strict` for content violations and for near-clean periods, `normal`
  for border-dominated ones.
- *Thumbnail choice* (`_select_diverse_violations_for_thumbnails`, `frame_analysis.py:2173-2227`):
  violations from `content_violations` periods are offered first, border-dominated ones last —
  border problems are already documented by the border-detection section.

---

## Fallback when signalstats is disabled

BRNG analysis can run without signalstats (`frame_analysis.py:5307-5334`). In that case:

1. Use the stage-1 QCTools candidate periods directly if there are any;
2. else build evenly distributed periods over
   `[color_bars_end + 10, video_duration - 10]`, spaced `content_duration / (count + 1)`;
3. either way, run the result through `_validate_periods_against_black_segments()`.

This path skips the count guarantee and the refinement pass — it has no signalstats findings to
refine against.

---

## Where the periods surface

- **JSON**: `{video_id}_enhanced_frame_analysis.json` → `signalstats.analysis_periods` and
  `brng_analysis.analysis_periods`, each a list of `[start_seconds, duration]`. Detected black
  segments are alongside under `black_segments`.
- **HTML report**: `get_frame_analysis_periods()` (`utils/generate_report.py:3558`) reads them
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
  (head bars); the `bars_regions` list is merged into `avoid_segments` (mid-file bars). Code that
  only honors one of them will place periods on test patterns. Reference SMPTE bars measure BRNG
  ≈ 0.0118, above the `> 0.01` violation threshold, so un-excluded bars read as a dense violation
  cluster and attract periods.
