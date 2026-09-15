# Frame Analysis — Step-by-Step Process

A point-by-point account of everything AV Spex does in **analysis period selection**, **border
detection**, **signalstats**, and **BRNG analysis**, in the order it happens. It is written from
the code (`src/AV_Spex/checks/frame_analysis.py`, `frame_geometry.py`, and the call site in
`processing/processing_mgmt.py`), not from the existing docs. Where the code and the existing docs
disagree, the code is described here and the disagreement is listed at the end.

Values are the code defaults. "Config" means `FrameAnalysisConfig` in `utils/config_setup.py`.

---

## 0. Before anything runs

1. Frame analysis runs inside `process_video_outputs()`, after QCTools and qct-parse/CLAMS.
2. It is skipped entirely if every frame-analysis sub-step is disabled.
3. Processing passes in two bars inputs from the qct-parse + CLAMS consensus:
   - `color_bars_end_time` — end of the head bars (or `None`).
   - `bars_regions` — every detected bars span (head plus any mid-file bars).
4. Config values read at the entry point (`analyze_frame_quality`):
   - `border_detection_mode` → `simple` or `sophisticated`
   - `brng_skip_color_bars` (True)
   - `max_border_retries` (3)
5. Video geometry and timing are read with OpenCV; if OpenCV can't open the file, geometry comes
   from ffprobe instead and frame-reading steps know they can't decode frames.
   An unknown duration is recorded as 0 and treated as "unknown", never computed with.
6. The QCTools report is located (`.qctools.xml.gz` next to the video, in `_qc_metadata/`, or in
   `_vrecord_metadata/`).

### Overall order

| Step | What | Runs when |
|---|---|---|
| 0 | Bitplane check | enabled (not covered here) |
| 1 | Resolve head color-bars end time | `brng_skip_color_bars` is on |
| 2a | Detect black segments from QCTools | any of border / signalstats / BRNG / duplicate-frame is on, and a QCTools report exists |
| 2b | Scan QCTools for BRNG violations + build candidate periods | any of border / signalstats / BRNG is on, and a QCTools report exists |
| 3 | Border detection (+ visualization) | enabled |
| 4 | Signalstats (final period selection happens here) | enabled |
| 4b | Refine periods from signalstats findings | signalstats and border results exist, and there are QCTools candidates |
| 5 | BRNG differential analysis | enabled |
| 6 | Border refinement loop | border + BRNG enabled, mode is `sophisticated`, BRNG says borders need adjusting, `auto_retry_borders` on |
| 7–8 | Dropped-sample, duplicate-frame detection | enabled (not covered here) |
| 9 | Summary + write `{video_id}_enhanced_frame_analysis.json` | always |

Every step checks for cancellation before it starts.

---

## 1. Shared groundwork: reading the QCTools report

These pieces are used by period selection, signalstats and BRNG.

### 1.1 Head color-bars end time
1. If `brng_skip_color_bars` is on and no end time was passed in, fall back to reading
   `{video_id}_report_csvs/qct-parse_colorbars_durations.csv` (next to the video) and parse its end
   timestamp. If that file isn't there, bars end = 0 (no bars).
2. If `brng_skip_color_bars` is off, the passed-in value is used as is.
3. An empty value (no bars detected) is treated as 0 from here on.

### 1.2 Bit-depth detection
1. Read the first frames of the QCTools report.
2. If a `UAVG`/`VAVG` chroma average is present: above 300 → stats are on a 10-bit scale; otherwise
   8-bit. (Chroma midpoint is ~512 at 10-bit, ~128 at 8-bit, regardless of picture content.)
3. If no chroma tags: any frame in the first 100 with `YMAX` > 250 → 10-bit; otherwise 8-bit.

### 1.3 Black-frame rule
A frame is **black** when all three hold (10-bit values; divided by 4 for 8-bit reports):
- `YMAX` < 300
- `YHIGH` < 115
- `YLOW` < 97

There is deliberately no `YMIN` condition — analog tape black carries sub-black noise that keeps
`YMIN` above zero.

### 1.4 Black-segment detection
1. Walk every frame in the report, applying the black-frame rule.
2. Consecutive black frames form a segment; a non-black frame only closes the segment if it arrives
   more than **0.5 s** after the last black frame (short interruptions are bridged).
3. Keep segments at least **2.0 s** long.
4. Black segments + all `bars_regions` together form the **avoid list** used everywhere below.

### 1.5 Violation scan (`parse_for_violations_streaming`)
1. Walk every frame in the report.
2. Skip frames before the head bars end (only if `brng_skip_color_bars` is on).
3. Skip frames inside any `bars_regions` span.
4. Skip black frames.
5. A frame is a **violation** when its QCTools `BRNG` value (share of out-of-range pixels, 0–1) is
   **> 0.01**.
6. For every violation, add to 10-second bins:
   - `violation_histogram[bin]` += 1
   - `violation_severity[bin]` += the BRNG value
7. Return only the **top 100** violations by BRNG value (this capped list feeds border detection
   frame choice and BRNG frame targeting). The histogram and severity maps cover **all** violations.

---

## 2. Analysis period selection

Signalstats and BRNG both sample a few fixed-length windows rather than the whole file.
Config: `analysis_period_count` (3), `analysis_period_duration` (60 s).

### 2.1 Stage 1 — Candidate periods from QCTools (`_analyze_qctools_violation_distribution`)
Runs right after the violation scan, only if violations were found.
1. Start from the full 10 s histogram (falls back to binning the top-100 list if no histogram).
2. Drop a bin if:
   - it ends within the **last 30 s** of the file, or
   - more than half of it (> 5 s) overlaps a single avoid-list segment.
3. If the period length is longer than the video, shorten it to the video length.
4. Rank bins by summed severity (by count if no severity map).
5. For each bin in rank order, the candidate period is **centered on the bin**
   (`bin_start + 5 − duration/2`), clamped to start ≥ 0 and to end at or before the end of the file.
6. **Pass 1**: accept a candidate only if its start is at least **2 × period length** from every
   accepted start. Stop at the requested count.
7. **Pass 2** (only if pass 1 came up short): same, but the required gap relaxes to **1 × period
   length** (simple non-overlap).
8. Sort by start time. These are the **QCTools candidates**.

### 2.2 Stage 2 — Final selection (`_find_analysis_periods`, called from signalstats)
1. **Effective start** = `max(content_start, bars_end) + 10`. On the first signalstats pass,
   `content_start` is `bars_end + 10` (or 10 with no bars), so periods may start no earlier than
   **bars end + 20 s** (or 20 s).
2. If duration is unknown **and** there are no QCTools candidates → no periods (signalstats reports
   "could not run").
3. Choose a placement strategy, first that applies:
   1. **QCTools candidates**, if any → use them as-is.
   2. **Border-detection quality hints** (sophisticated mode only; top 10 quality frames). Only hints
      at or after the effective start are used, and there must be at least as many as the requested
      count. Each period is centered on a hint (not before effective start) and must end 30 s before
      the file end. Used only if that yields the full requested count.
   3. **Even distribution** across `effective start → (duration − 30 s)`:
      - enough room for all periods → spread evenly, first at effective start, last ending at the
        window end;
      - room for at least one → as many as fit, spread evenly;
      - less than one period of room → one shortened period at effective start.
4. **Validate against the avoid list** (section 2.3).
5. **Top up to the requested count** if short (section 2.4).
6. Sort by start time.

### 2.3 Validation and repair against black/bars (`_validate_periods_against_black_segments`)
For each period:
1. Sum its overlap with all avoid-list segments.
2. Overlap **≤ 25 %** of its length → keep it unchanged.
3. Overlap > 25 % → **shift**:
   - try positions 5 s, 10 s, 15 s … away, forward first then backward, up to half the video duration;
   - a position is acceptable if it starts at/after effective start, ends ≥ 10 s before file end,
     overlaps the avoid list by **≤ 10 %**, and is at least one period length from every
     already-kept period's start.
4. No shift works → **shrink to fit**:
   - build the non-black gaps between effective start and 10 s before file end;
   - try the largest gap first; skip gaps shorter than **10 s**;
   - keep the original start if it lies in the gap with ≥ 10 s left, otherwise use the gap start;
   - length = min(period length, room left in the gap);
   - reject if it overlaps an already-kept period.
5. Neither works → drop the period.
6. If **every** period was dropped, keep the one with the least avoid-list overlap anyway and set a
   **last-resort note** (sticky for the rest of the file) — this becomes BRNG's "low confidence".

### 2.4 Top-up (`_fill_periods_to_count`)
1. Only runs if fewer periods than requested, and the window (effective start → file end − 30 s)
   is at least one period long.
2. Lay out a grid of `max(2 × count, 4)` evenly spaced candidate starts across the window.
3. Accept a candidate if it doesn't overlap an existing period and overlaps the avoid list by
   ≤ 25 %. Stop at the requested count.

### 2.5 Stage 3 — Refinement from signalstats results (`_refine_periods_from_signalstats`)
Runs after signalstats, only if both signalstats and border results exist and there were QCTools
candidates.
1. Score each period from its signalstats diagnosis (section 4.4) and active-area flagged-frame %:
   - content violations → 100 + active %
   - border violations → 50 + active %
   - minimal → active %
2. Periods scoring **< 5** are replaceable (essentially nothing out of range in the active area).
   Periods without a diagnosis (not measured both ways, e.g. border detection off) are never
   replaced.
3. Replacement candidates = QCTools candidates that don't overlap any current period.
4. Replace replaceable periods with candidates, in order.
5. If anything was replaced, re-validate against the avoid list (section 2.3, effective start =
   bars end + 10).
6. Signalstats is **not** re-run on the replaced periods; the BRNG step uses them with whatever
   per-period signalstats data exists at that index.

### 2.6 When signalstats is disabled (BRNG fallback)
1. Use the QCTools candidates if any.
2. Otherwise spread `count` periods evenly across `bars end + 10 → duration − 10`
   (spacing = content length / (count + 1)). Unknown duration → no periods, BRNG reports
   "could not run".
3. Validate against the avoid list (section 2.3). No top-up in this path.

### 2.7 Confidence attached to BRNG results
- **normal** — intended periods were analyzed.
- **partial_coverage** — some BRNG periods failed to produce comparison videos.
- **last_resort** — section 2.3 step 6 fired.
- If both apply, the level is `last_resort`; the note keeps both reasons.

---

## 3. Border detection

Produces the **active area** `(x, y, width, height)` used to crop signalstats and BRNG.

### 3.1 Simple mode
1. Crop `simple_border_pixels` (**25 px** default; `--frame-border-pixels`) from every side.
   A negative value is treated as 0.
2. If that leaves no picture (crop wider than the frame), use the whole frame.
3. No quality hints, no head-switching check.

### 3.2 Sophisticated mode
1. If OpenCV can't open the file → fall back to simple mode (using `simple_border_pixels`, as do
   the other fallbacks below).
   Settings (Complex tab or JSON; values are checked here — a non-number uses the default, a value
   below the minimum is raised to it, each with a warning):

   | Setting | Default | Minimum | Controls |
   |---|---|---|---|
   | `sophisticated_sample_frames` (N) | 30 | 5 | frames measured |
   | `sophisticated_threshold` | 10 | 0 | brightness that counts as picture |
   | `sophisticated_edge_sample_width` | 100 | 1 | left/right search depth |
   | `sophisticated_padding` | 5 | 0 | safety margin per side |

2. **Choose frames to measure**:
   1. Read up to the first N of the top-100 QCTools violation frames.
   2. If fewer than N suitable frames, also read `max(50, N × 5/3)` frames evenly spaced across the
      whole file (including bars and black).
   3. Frame suitability (grayscale, 8-bit): rejected if mean < 15 (too dark), mean > 240 (too
      bright), or standard deviation < 15 (low contrast).
   4. Quality score = average of brightness score `1 − |mean − 120| / 120` and contrast score
      `min(std / 50, 1)`.
   5. Keep the N highest-scoring frames.
3. Fewer than **5** suitable frames → fall back to simple mode.
4. **Measure borders on each frame** (grayscale; "picture" = mean brightness > threshold):
   - left: first column (scanning in from the left, up to edge-width columns) above threshold;
   - right: same from the right edge;
   - top: first row (scanning down, up to **20** rows — not affected by edge width) whose
     middle-third mean is above threshold;
   - bottom: same from the bottom, up to 20 rows.
   - Take the **median** of each side across all frames (0 if never found).
5. **Head switching** (sampled independently of the frames above):
   1. Read 20 frames evenly spaced across the file.
   2. For each, scan the bottom 30 rows upward. A row is an artifact row when its left-half mean is
      > 10 and `|left mean − right mean| / left mean` > 0.5. Scanning stops at the first normal row
      after artifact rows; the artifact height is how far up it reached.
   3. Detected if more than **20 %** of sampled frames have an artifact; severity `high` above 50 %,
      otherwise `moderate`. Records percentage, average height and max height.
6. **Vertical blanking** (first 10 quality frames): in the outer 30 columns on each side, look for
   columns with mean < 20 and std < 10. If the blanking reaches further in than the measured
   left/right border, move that border to the blanking edge + 2 px.
7. **Bottom crop** = the larger of the measured bottom border and the average head-switching height.
8. Active area = frame minus the four borders, then shrunk by the padding on every side. If that
   leaves no picture, fall back to simple mode.
9. Quality hints for period selection = timestamps of the top 10 quality frames.

### 3.3 Border visualization (both modes)
1. Search the window **90 s → 210 s** (150 s ± 60 s) for the highest-quality frame, checking one
   frame per second with the same suitability test. If none qualifies, use the frame at 150 s (or
   mid-file if the video is shorter).
2. Save `{video_id}_border_detection.jpg`: left panel full frame, right panel active area only.
   - Simple mode: dashed red lines at each crop edge more than 10 px from the frame edge.
   - Sophisticated: red shaded border regions; orange band for head switching at its average height.
   - Caption with L/R/T/B border sizes and head-switching summary.

### 3.4 With border detection disabled
If signalstats or BRNG is enabled, a full-frame placeholder (method `disabled`) is passed downstream
so BRNG still has frame geometry. Signalstats does **not** treat it as a detected active area: it
measures the full frame only (section 4.1).

### 3.5 Border refinement loop (after BRNG)
Only when: border detection and BRNG enabled, mode is **sophisticated**, BRNG's aggregate result says
`requires_border_adjustment` (section 5.6), and `auto_retry_borders` is on.

Each iteration, up to `max_border_retries` (3), while BRNG still says adjustment is needed:
1. **Expand borders**: for each edge BRNG recommends (5 or 10 px — section 5.6), move that edge
   inward by that amount. Width/height never drop below 100 px or past the frame bounds. The method
   becomes `sophisticated_refined`.
2. Save `{video_id}_border_detection_refined_iter{N}.jpg`.
3. If signalstats is enabled, re-run it with the new active area (period selection runs again from
   the QCTools candidates; effective start is now bars end + 10). The stage-3 period refinement
   (section 2.5) is **not** re-applied.
4. Re-run BRNG on the new active area and periods.
5. Record the iteration: area change, BRNG frame count before/after, edge violation %, and whether
   it improved.
6. **Stop early if the round made no meaningful improvement** (`_is_meaningful_improvement`, comparing
   this round's BRNG result with the previous one). A round counts as improved if any of:
   - violation frames fell by more than 20 %;
   - the worst frame's violation % fell by more than 20 %;
   - edge violation % is still above 50 % **and** the borders actually moved (keep trying);
   - edge violation % fell by more than 30 %.

   Edge violations above 50 % with borders that didn't move always stops the loop. The borders from
   the stopping round are kept as the final borders.
7. After the loop: store final borders/BRNG/signalstats alongside the initial ones, regenerate the
   signalstats example thumbnails against the final borders, and save
   `{video_id}_border_refinement_comparison.jpg` if any iteration ran.
8. Downstream (report, access-file crop) prefers the final borders and final BRNG result.

### 3.6 Use of the active area outside frame analysis
The access-file crop uses the active area only when the method starts with `sophisticated`
(simple mode never crops the access copy), and only when `access_file_crop_borders` is on.

---

## 4. Signalstats analysis

Measures, per period, how much of the picture is out of broadcast range, comparing the whole frame
against the active area.

### 4.1 Setup
1. Select periods (section 2.2).
2. No periods → return a "could not run" result (stats `None`, severity `warning`), naming whether
   the duration was unknown or everything overlapped black.
3. Validate the active area (must be positive width/height, non-negative origin); an invalid one is
   ignored and the pass runs on the full frame.
4. With border detection off (placeholder method `disabled`) there is no active area either: each
   period is measured on the full frame only (QCTools if available, otherwise ffprobe without a
   crop), periods get no border/content diagnosis, and the overall diagnosis uses the full-frame
   wording ("borders were not excluded", section 4.6).

### 4.2 Per period — full frame (QCTools)
1. Read every frame of the QCTools report within the period.
2. Skip black frames (section 1.3); count them.
3. Record each remaining frame's BRNG value.
4. A frame is **flagged** when BRNG **> 0** (a single out-of-range pixel).
5. Full-frame flagged % = flagged / analyzed; full-frame max BRNG = highest value × 100.

### 4.3 Per period — active area (ffprobe)
1. Run
   `ffprobe -f lavfi -i "movie=<file>:seek_point=<start>,crop=w:h:x:y,signalstats=stat=brng,trim=duration=<len>"`
   reading `pts_time` and `lavfi.signalstats.BRNG` per frame.
2. ffmpeg applies broadcast-range limits for the file's own bit depth.
3. **Black frames are not skipped** on this side.
4. Flagged = BRNG > 0. Active-area flagged % and max BRNG computed as above.
5. If ffprobe fails, the full-frame result is used for that period's aggregate instead.

### 4.4 Per-period diagnosis
Only when both sides returned data. Checked in order:
1. **border_violations** — full-frame flagged % > active-area flagged % + 5, **and** active-area
   flagged % < 30.
2. **content_violations** — active-area flagged % > 10.
3. **minimal_violations** — otherwise.

### 4.5 Aggregate across periods
1. Combine the per-period results used for the aggregate (active area where available).
2. **Violation %** = total flagged frames / total analyzed frames.
3. **Max BRNG** = highest single-frame BRNG × 100.
4. **Average BRNG** = mean of all per-frame BRNG values × 100 (the severity measure).
5. **Representative frame** = the frame whose BRNG is closest to the average; **worst frame** = the
   highest-BRNG frame.
6. **Analyzed region** = `active_area`, `full_frame`, or `mixed`.
7. **Coverage**: if some periods returned no data (or the run was cancelled), a coverage note is
   attached.
8. All periods empty → "could not run" result, not zeros.

### 4.6 Overall diagnosis and severity
**With an active area:**
1. More periods diagnosed border than content → "concentrated in border/blanking … picture content
   appears broadcast-safe" — `ok`.
2. Else, any content period:
   - average BRNG ≥ 10 % → "review recommended" — `alert`
   - otherwise → `warning` if violation % > 50, else `info`
   - wording says "Most" if violation % > 50, else "Some".
3. Else → "within broadcast-safe range" — `ok`.

**Without an active area** (full-frame only):
1. Average BRNG ≥ 10 % → `alert`.
2. Violation % < 10 and max BRNG < 0.1 % → `ok`.
3. Violation % < 50 and max BRNG < 1 % → `info`.
4. Otherwise → `warning`.

### 4.7 Example-frame thumbnails
1. Skipped if max BRNG is 0.
2. Clear `signalstats_frames/` from previous runs.
3. For the representative and worst frames, one ffmpeg call extracts the frame, crops to the active
   area, and stacks it side by side with a `signalstats=out=brng:color=magenta` copy.
4. Label the halves "Original" / "BRNG (magenta = out of range)" and save
   `{video_id}_signalstats_{representative|worst}.jpg`.

### 4.8 Hand-off to BRNG (upstream context)
Built when both signalstats and border results exist. Only periods measured both ways (so they
have a diagnosis) are included; BRNG uses its default sensitivity and sampling for the rest:
- per-period diagnosis, active-area flagged % and max BRNG, full-frame flagged % and max BRNG;
- head-switching result from border detection;
- average active-area BRNG, overall diagnosis, border widths, and a border-violation fraction
  (these last four are recorded but not currently used by BRNG).

---

## 5. BRNG analysis (differential detection)

Finds *where* in the picture the out-of-range pixels are, by rendering each period twice and
comparing.

### 5.1 Setup
1. Periods come from section 2 (refined, or fallback).
2. No periods → return nothing, with a "could not run" reason carried to the report.
3. Validate the active area (as in 4.1).

### 5.2 Per period — sensitivity and sampling from signalstats
If signalstats data exists for this period index:
1. **Sensitivity**: `strict` for border or minimal diagnoses; `normal` for content violations.
   Without signalstats data: `normal`.
2. **Sample count**:
   - active-area flagged % < 1 **and** max BRNG < 0.01 % → **30** frames (light);
   - active-area flagged % > 30 **or** max BRNG > 1 % → **200** frames (dense);
   - otherwise default logic (5.4).

### 5.3 Per period — render comparison clips
Two ffmpeg encodes into `temp_brng_period_N/` (libx264, preset fast, CRF 23), both seeking to the
period start for the period length:
1. **Highlighted**: `crop=…,signalstats=out=brng:color=magenta` — out-of-range pixels painted magenta.
2. **Original**: `crop=…` only.
3. If either encode fails, the period is counted as failed and skipped. All periods failing → BRNG
   returns nothing ("could not run"); some failing → partial-coverage note.

### 5.4 Per period — choose frames to compare
1. Map each of the top-100 QCTools violations that falls within the period to its frame position in
   the clip.
2. If that gives fewer than the minimum (50, or the 5.2 sample count), add evenly spaced frames
   (100, or the 5.2 count), then sort and keep the first 200 (or the 5.2 count).
3. If no QCTools violations exist at all: evenly spaced frames, 500 (or the 5.2 count), capped at
   the clip's frame count.

### 5.5 Per frame — detect violation pixels
Compare the highlighted and original frame channel by channel (BGR difference = highlighted − original).

| Parameter | strict | normal |
|---|---|---|
| magenta threshold | 12 | 10 |
| min change | 10 | 8 |
| votes required | 2 of 4 | 2 of 4 |
| min cluster size (px) | 15 | 10 |
| opening iterations | 2 | 2 |
| closing pass | no | yes (1) |

Four detection methods, each marks pixels:
1. **Strict magenta** — blue and red both rose by more than the threshold, green rose by less than
   half the threshold.
2. **Ratio** — blue or red changed by more than min change, both rose, and their increases are
   within ±40 % of each other.
3. **HSV** — highlighted hue is magenta (140–160, or < 10, or > 170 on OpenCV's 0–180 scale),
   saturation rose by more than 15, and brightness didn't drop by more than 10.
4. **Green drop** — for already-bright pixels: green fell by more than 2 × threshold, highlighted
   red and blue both > 150, highlighted green < 100, and red/blue barely changed.

Then:
1. A pixel is a violation when at least 2 methods agree.
2. Morphological opening (3×3 ellipse) to remove specks; closing afterwards in normal mode.
3. Connected components (8-connectivity): keep a cluster if it meets the minimum size **and** is not
   a thin sliver (short side / long side > 0.1) **or** is larger than 50 px.
4. The frame is recorded as a **violation frame** if more than **2** pixels remain.
   - Frame BRNG % = violation pixels / cropped frame pixels × 100.

### 5.6 Per violation frame — classify the pattern
1. **Edge strips**: 15 px on each side. The bottom strip is widened for head switching the crop
   didn't remove (sophisticated mode only):
   - residual = head-switching `max_height_px` − the active area's bottom crop (border detection
     already cropped the *average* height plus padding);
   - if head switching was seen in > 30 % of sampled frames and the residual is > 15 px, the bottom
     strip becomes residual + 5 px, capped at 40 px;
   - all bottom-strip measurements (linear score, blanking depth, adjacent band) use that width.
2. **Interior density** = violation % of the area inside the strips.
3. For each edge:
   - **Violation %** in the strip.
   - **Linear score**: share of rows (left/right) or columns (top/bottom) with violations hugging the
     outer edge (≥ 4 pixels within the outer 3 columns for left/right; ≥ 2 pixels within the outer 4
     rows for top/bottom).
   - **Blanking depth** (if strip violation % > 15): how far in from the edge violations reach.
   - **Confinement**: compare with a band 30 px deep just inside the strip; if that band's density is
     ≥ 50 % of the strip's, violations aren't confined to the edge.
   - **Edge-specific** if linear score > 50 %, **or** (strip violation % > 15 **and** (strip density ≥
     2 × interior **or** ≥ 15 points above interior) **and** confined).
   - Edge-specific and linear score > 70 % → **continuous edge**.
4. **Edge severity**: 3+ continuous edges → high; 2 → medium; 3+ affected edges, or 2 with a linear
   score > 60 → low; otherwise none.
5. **Luma zones** (skipped when edge severity is medium/high): share of violation pixels in
   sub-black (< 64), midtones (64–191) and highlights (≥ 192) on the 8-bit decoded original.
6. **Diagnostic labels**:
   - linear score > 50 on any edge → "Linear blanking patterns on: …"
   - else continuous edges → "Continuous edge artifacts (…)"
   - else any edge → "Edge artifacts (…)"
   - plus "Border adjustment recommended", "Border detection likely missed blanking" (high),
     "Moderate blanking detected" (medium) where applicable;
   - if no edge issue or low severity: highlights > 70 % → "Highlight clipping"; sub-black > 70 % →
     "Sub-black detected";
   - nothing matched → "General broadcast range violations".
7. Also recorded: whether violations concentrate on picture detail (Canny edges, > 60 %).

### 5.7 Aggregate across all violation frames
1. **Edge violation %** = share of violation frames with any edge-specific edge.
2. **Continuous edge %** = share with at least one continuous edge.
3. **Linear pattern %** = share where any edge's linear score > 30.
4. **Average linear score** per edge.
5. **Border expansion recommendation** per edge: affected in > 20 % of violation frames → 5 px;
   > 50 % → 10 px.
6. **Requires border adjustment** if any of:
   - linear pattern % > 20
   - continuous edge % > 15
   - edge violation % > 30 and continuous edge % > 0
   - edge violation % > 60 and continuous edge % = 0
   - continuous edge % > 10 on 2+ edges
   - any edge's average linear score > 40
7. **Summary statistics**: violation frame count, average and max frame BRNG %.
8. **Assessment sentence**: average frame BRNG % (labelled "low-level" below 10 %, "minimal" below
   0.1 %); edge share wording at > 70 %, > 40 %, > 0 %; linear blanking noted above 20 %.

### 5.8 Thumbnails
1. Clear `brng_thumbnails/` from previous runs.
2. Order violation frames: those in content-violation periods first, then undiagnosed, then
   border-violation periods (within each group, by BRNG value).
3. Take the first, then add frames at least **5 s** from every selected frame, up to **5**.
   If still short, fill with the next best regardless of spacing.
4. For each, build a 4-panel image: Original | BRNG Highlighted / Violations Only (magenta pixels
   re-extracted and shown yellow) | Analysis Data (frame, time, BRNG %, pixel count, top 2
   diagnostics). Downscale to ≤ 1600 px wide; save as JPEG.
5. Delete the temporary clips.

---

## 6. Outputs

All written to `{video_id}_qc_metadata/`:

| File | From |
|---|---|
| `{video_id}_enhanced_frame_analysis.json` | everything: steps enabled, bars end, black segments, initial/final borders, signalstats (initial/final), BRNG (initial/final), refinement history |
| `{video_id}_border_detection.jpg` | 3.3 |
| `{video_id}_border_detection_refined_iter{N}.jpg` | 3.5 |
| `{video_id}_border_refinement_comparison.jpg` | 3.5 |
| `signalstats_frames/{video_id}_signalstats_{representative,worst}.jpg` | 4.7 |
| `brng_thumbnails/{stem}_brng_{NNN}_{time}s.jpg` | 5.8 |

---

## 7. Discrepancies and likely issues found while writing this

### Settings that are not used
- **`sophisticated_viz_time` / `sophisticated_search_window`** (bundled JSON only, not in
  `FrameAnalysisConfig`): the border visualization always uses 150 s / 120 s.
- **`skip_start_seconds`** in BRNG analysis: passed, never read.
- **`brng_skip_color_bars` / `--frame-no-colorbar-skip`** affects only the QCTools violation scan
  (1.5) and the CSV fallback (1.1). Periods, signalstats and BRNG still avoid the head bars when an
  end time is passed in.

### Probable bugs
- **Effective start differs between passes**: bars end + 20 s on the first signalstats pass, bars
  end + 10 s on refinement re-runs and in BRNG fallback validation.
- The active-area ffprobe pass uses `-of csv=p=0`, which the project otherwise avoids because
  side data can add a trailing separator. Parsing splits on the last comma, so a trailing
  separator would make every line unparseable. Unverified whether frame side data triggers it here.

### Existing docs that disagree with the code
- **Help window — BRNG**: says three voting methods; there are four (green-drop added).
- **Help / README — Border detection**: "iterative refinement" is not mentioned as
  sophisticated-only; simple mode never refines.
- **Help / report — "Period selection priority"**: lists QCTools clusters → border hints → even
  spacing, which matches the code, but omits that border hints only exist in sophisticated mode and
  need at least `count` usable hints.
- **Help window — Analysis Periods**: "bars plus a 10-second margin" — the first pass uses 20 s.
- **GUI docs — Skip Color Bars**: says bars "detected by qct-parse"; it's the qct-parse + CLAMS
  consensus.
