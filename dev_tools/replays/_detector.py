"""
_detector.py: replay the qct-parse detectors and the bars consensus for one package, from the
sidecars a run kept. Used by detector.py; kept importable so worker processes can load it.

What is re-run, under the code as it is now:

    run_qctparse()               bars detection, chroma phase, audio analysis, read from the
                                 package's QCTools report (and its audio stats sidecar)
    merge_head_bars_consensus()  the qct-parse / CLAMS cross-validation, handed to
    consensus_after_qctparse()   run_qctparse and finished the way process_qctools_output() does

What is frozen from the run, because it reads the video:

    CLAMS bars and tone spans    read back from the run's CLAMS durations CSVs
    CLAMS SSIM scores            the arbiter reads the run's recorded scores; a span they do
                                 not cover needs the video, and is reported if it is not there
    the video's properties       duration, frame rate, start timecode, audio stream count

Thumbnails, the identical-channel sample comparison, bars evaluation, clamped levels and tone
leak are switched off: none is a scored detector here and most need the video.

duplicate_frames is the one detector here that needs the tape's drive mounted. Its candidates
come from the stored QCTools report, but each is confirmed against the frames themselves, and
an unconfirmed candidate is not a finding: without the video the tape scores as not run. The
black segments it avoids are read back from the run; the bars are the replayed consensus.
"""
import csv
import json
import logging
import shutil
import tempfile
import time
import traceback
from pathlib import Path

# detector -> the qct-parse step that has to run for it
REPLAYABLE = {
    "bars": None,                    # bars detection always runs: the other steps read its result
    "chroma_phase": "detect_chroma_phase_errors",
    "audio_dropout": "audio_analysis",
    "audible_timecode": "audio_analysis",
    "duplicate_frames": None,        # not a qct-parse step; needs the video (see above)
}
DEFAULT = ("bars", "chroma_phase", "audio_dropout", "audible_timecode")   # what needs no drive
NEEDS_VIDEO = ("tone_leak", "dropped_samples")
CLAMS_BARS_CSV = "clams_bars_colorbars_durations.csv"
CLAMS_TONE_CSV = "clams_tone_detection_durations.csv"


def _seconds(text):
    h, m, s = text.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _labelled_spans(path):
    """[(label, start_s, end_s), ...] from a CLAMS durations CSV; [] when it found nothing,
    None when the file is not there (the detector did not run)."""
    if not path.exists():
        return None
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    return [(r[0], _seconds(r[1]), _seconds(r[2])) for r in rows[1:] if len(r) >= 3]


def frozen_clams(csvs):
    """(clams_regions, head_start, head_end, clams_ran) as process_qctools_output() had them,
    rebuilt from the run's CLAMS sidecars."""
    from AV_Spex.processing import processing_mgmt as pm
    bars, tones = _labelled_spans(csvs / CLAMS_BARS_CSV), _labelled_spans(csvs / CLAMS_TONE_CSV)
    if bars is None and tones is None:
        return [], None, None, False
    bars, tones = bars or [], tones or []
    regions = sorted([(s, e, "bars") for _, s, e in bars] + [(s, e, "tone") for _, s, e in tones],
                     key=lambda r: r[0])
    primary = [(s, e) for label, s, e in bars if label == "primary"]
    second = [(s, e) for label, s, e in bars if label == "head-second-pass"]
    head_start, head_end = primary[0] if primary else (None, None)
    if second and second[0][0] < pm.HEAD_BARS_START_THRESHOLD:
        head_start, head_end = second[0]
    return regions, head_start, head_end, True


def parse_overrides(pairs):
    """["outputs.frame_analysis.duplicate_min_run_length=1", ...] -> a nested dict to lay over
    the checks config. Values are read as JSON (1, true, "text"), else kept as text."""
    out = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"{pair!r}: expected KEY=VALUE, e.g. outputs.frame_analysis.duplicate_min_run_length=1")
        try:
            value = json.loads(value)
        except ValueError:
            pass
        node = out
        *parents, leaf = key.strip().split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return out


def _check_known(overrides, data, where=""):
    for key, value in overrides.items():
        if key not in data:
            raise ValueError(f"no checks setting named {where}{key}")
        if isinstance(value, dict):
            if not isinstance(data[key], dict):
                raise ValueError(f"{where}{key} is a value, not a group of settings")
            _check_known(value, data[key], f"{where}{key}.")


def run_configs(run_dir, overrides=None):
    """The run's checks and spex configs as dataclasses, over the bundled defaults (so a field
    added since the run takes its default). `overrides` (see parse_overrides) is laid over the
    checks config last: the way to try a setting the run was not collected with."""
    from AV_Spex.utils.config_manager import ConfigManager
    from AV_Spex.utils.config_setup import ChecksConfig, SpexConfig
    mgr, out = ConfigManager(), {}
    for name, cls in (("checks", ChecksConfig), ("spex", SpexConfig)):
        data = mgr._load_json_config(name, use_last_used=False)
        saved = Path(run_dir) / f"{name}_config.json"
        if saved.exists():
            mgr._update_dict_recursively(data, mgr._migrate_config_data(json.loads(saved.read_text()), name))
        if name == "checks" and overrides:
            _check_known(overrides, data)
            mgr._update_dict_recursively(data, overrides)
        out[name] = mgr._deserialize_dataclass(cls, data)
    return out


def _install_config(run_dir, detectors, overrides=None):
    """Put the run's config in ConfigManager's cache, for this process only (nothing is saved),
    with qct-parse cut down to the steps the replay needs."""
    from AV_Spex.utils.config_manager import ConfigManager
    configs = run_configs(run_dir, overrides)
    q = configs["checks"].tools.qct_parse
    steps = {REPLAYABLE[d] for d in detectors}
    q.run_tool = q.barsDetection = True
    q.evaluateBars = q.thumbExport = q.detect_clamped_levels = q.detect_tone_leak = False
    q.detect_chroma_phase_errors = "detect_chroma_phase_errors" in steps
    q.audio_analysis = "audio_analysis" in steps
    ConfigManager._configs.update(configs)
    return configs


def _audio_stream_count(pkg):
    """More than one only when the run kept an audio stats sidecar (multi-stream input)."""
    if not list(pkg.qc.glob("*.audio_stats.xml.gz")):
        return 1
    try:
        probe = json.loads((pkg.qc / f"{pkg.vid}_ffprobe_output.txt").read_text())
        n = sum(s.get("codec_type") == "audio" for s in probe.get("streams") or [])
    except (OSError, ValueError):
        n = 0
    return max(n, 2)


def replay_duplicate_frames(pkg, report, video_path, fps, frame_config, merged_end, bars_spans):
    """The duplicate-frame result dict frame analysis would record, from the stored QCTools
    report and the video, called the way EnhancedFrameAnalysis calls it."""
    from dataclasses import asdict
    from AV_Spex.checks import duplicate_frame_detection
    from AV_Spex.checks.frame_analysis import QCToolsParser, merge_avoid_segments
    black = [(b["start"], b["end"]) for b in pkg.fa.get("black_segments") or []]
    with tempfile.TemporaryDirectory() as tmp:          # thumbnails are written and not kept
        result = duplicate_frame_detection.detect_duplicate_frames(
            video_path, tmp, qctools_parser=QCToolsParser(str(report), fps=fps),
            check_cancelled=lambda: False, color_bars_end_time=merged_end or 0,
            black_segments=merge_avoid_segments(black, bars_spans),
            min_run_length=frame_config.duplicate_min_run_length)
    if result is None:
        return None
    out = asdict(result)
    for run in out.get("runs") or []:
        run["first_frame_thumbnail"] = run["last_frame_thumbnail"] = None
    return out


def replay_package(pkg_dir, out_dir, run_dir, detectors, overrides=None):
    """Replay one package into out_dir (a package directory of the replay). Returns a dict:
    video_id, ok, seconds, error, `skipped` (why, for a package with nothing to replay) and
    `no_evidence`: spans the bars arbiter could not judge
    because the recorded SSIM scores do not cover them and the video is not mounted."""
    started = time.time()
    pkg_dir, out_dir = Path(pkg_dir), Path(out_dir)
    result = {"video_id": pkg_dir.name, "ok": False, "seconds": 0, "error": "", "no_evidence": [], "no_video": False}
    try:
        from AV_Spex.utils.log_setup import logger
        logger.setLevel(logging.CRITICAL)
        import sys
        here = Path(__file__).resolve().parent
        for p in (here, here.parent):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        from aggregate import Package
        from scorecard import Timing
        from AV_Spex.checks import qct_parse
        from AV_Spex.processing import processing_mgmt as pm

        pkg = Package(pkg_dir)
        vid = result["video_id"] = pkg.vid
        report = pkg.path / (pkg.info.get("qctools_report") or "")
        if not report.is_file() or not report.name.endswith(".qctools.xml.gz"):
            result["skipped"] = f"no .qctools.xml.gz report in the package ({report.name or 'none recorded'})"
            return result

        configs = _install_config(run_dir, detectors, overrides)
        csvs = out_dir / f"{vid}_report_csvs"
        if csvs.exists():
            shutil.rmtree(csvs)
        csvs.mkdir(parents=True)
        # The arbiter reads the recorded scores, and a verify scan appends to them: work on a copy
        ssim = pkg.csvs / f"{vid}{pm.bars_detection_clams.SSIM_SCORES_CSV_SUFFIX}"
        if ssim.exists():
            shutil.copyfile(ssim, csvs / ssim.name)

        # What run_qctparse probes the video for, from what the collection recorded
        video = pkg.info.get("video") or {}
        timing = Timing.for_package(pkg)
        start_tc = ((Timing._cache.get(vid) or {}).get("start_timecode") if timing.known
                    else video.get("start_timecode"))
        streams = _audio_stream_count(pkg)
        qct_parse._get_video_duration = lambda _p: video.get("duration")
        qct_parse._get_video_frame_rate = lambda _p: timing.fps
        qct_parse._get_video_start_timecode = lambda _p: start_tc
        qct_parse._get_audio_stream_count = lambda _p: streams
        qct_parse._export_chroma_thumb = lambda *a, **k: False
        qct_parse._measure_channel_difference = lambda *a, **k: None

        video_path = str(Path(pkg.info.get("source") or "") / (pkg.info.get("video_file") or ""))
        have_video = Path(video_path).is_file()
        clams_regions, clams_head_start, clams_head_end, clams_ran = frozen_clams(pkg.csvs)
        clams_ran = clams_ran and bool(configs["checks"].tools.clams_detection.run_tool)

        def region_is_bars(lo, hi):
            if lo is not None and hi is not None and hi > lo and not have_video:
                score, samples = pm._clams_max_ssim_in_span(str(csvs), vid, lo, hi)
                if (samples < 3 or score is None) and (round(lo, 2), round(hi, 2)) not in result["no_evidence"]:
                    result["no_evidence"].append((round(lo, 2), round(hi, 2)))
                if samples < 3 or score is None:
                    return None
            return pm._clams_region_is_bars(video_path, str(csvs), vid, lo, hi)

        # As process_qctools_output() builds it, with the frozen CLAMS claims
        def bars_consensus(head_start, head_end, all_regions):
            return pm.merge_head_bars_consensus(
                qctparse_head_start=head_start, qctparse_head_end=head_end,
                qctparse_all_regions=all_regions,
                clams_head_start=clams_head_start, clams_head_end_raw=clams_head_end,
                clams_regions=clams_regions, clams_ran=clams_ran, region_is_bars=region_is_bars)

        parsed = qct_parse.run_qctparse(video_path, str(report), str(csvs),
                                        check_cancelled=lambda: False, clams_regions=clams_regions,
                                        bars_consensus=bars_consensus)
        if parsed is None:
            raise RuntimeError("run_qctparse returned nothing")
        _, (merged_end, demoted, regions) = pm.consensus_after_qctparse(parsed, str(csvs), bars_consensus)

        # What frame analysis records of the consensus, which is what the scorecard reads
        fa = {"replay": True, "head_demoted": demoted,
              # not scored: what the color-bars evaluation would measure, against qct-parse's own "head"
              "evaluation_bars_span": parsed.get("evaluation_bars_span"),
              "qctparse_head": [parsed.get("head_bars_start"), parsed.get("head_bars_end")]}
        spans = [(s, e) for s, e in regions or [] if e > s]
        if spans:
            fa["bars_regions"] = [{"start": s, "end": e} for s, e in spans]
        if merged_end:
            fa["color_bars_end_time"] = merged_end
        if "duplicate_frames" in detectors:
            found = replay_duplicate_frames(pkg, report, video_path, timing.fps,
                                            configs["checks"].outputs.frame_analysis, merged_end, spans)
            if found is not None:
                fa["duplicate_frame_detection"] = found
            result["no_video"] = not have_video
        qc = out_dir / f"{vid}_qc_metadata"
        qc.mkdir(parents=True, exist_ok=True)
        (qc / f"{vid}_enhanced_frame_analysis.json").write_text(json.dumps(fa, indent=2))

        info = dict(pkg.info)
        info["replay_of"] = str(pkg_dir)
        info["replayed"] = sorted(detectors)
        info["files"] = sorted(str(p.relative_to(out_dir)) for p in out_dir.rglob("*")
                               if p.is_file() and p.name != "package.json")
        (out_dir / "package.json").write_text(json.dumps(info, indent=2))
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["trace"] = traceback.format_exc()
    result["seconds"] = round(time.time() - started, 1)
    return result
