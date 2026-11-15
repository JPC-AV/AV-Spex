"""dev_tools/scorecard.py and dev_tools/ledger.py, against a small corpus built in tmp_path.

The scorecard decides what counts as a regression and the ledger which tapes are a fair test,
so the rules that matter are pinned here: "did not run" is never "clean", an unjudged event is
never a false alarm, and a held-out tape is not shown for review.
"""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev_tools"))
import ledger  # noqa: E402
import scorecard  # noqa: E402

A, B, C = "JPC_AV_00001", "JPC_AV_00002", "JPC_AV_00003"

DROPOUT_HEAD = ("Audio Dropout Detection Results\nDropout Events Detected,{n}\nDropout Detected,{yn}\n\n"
                "Timestamp Start,Timestamp End,Channel,Worst RMS (dBFS)\n")


def dropout_csv(*events):
    """A qct-parse_audio_dropout.csv holding these (start, end) timecodes."""
    return DROPOUT_HEAD.format(n=len(events), yn="Yes" if events else "No") + "".join(
        f"{s},{e},1,-inf\n" for s, e in events)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(scorecard, "CORPUS", tmp_path)
    monkeypatch.setattr(scorecard, "TRUTH", tmp_path / "truth")
    monkeypatch.setattr(scorecard.Timing, "_cache", None)
    monkeypatch.setattr(ledger, "CORPUS", tmp_path)
    monkeypatch.setattr(ledger, "LEDGER", tmp_path / "ledger.csv")
    monkeypatch.setattr(ledger, "TRUTH", tmp_path / "truth")
    (tmp_path / "truth").mkdir()
    (tmp_path / "labels").mkdir()
    return tmp_path


def package(corpus, vid, run="r1", csvs=None, fa=None):
    """Add a package to a run. csvs: {sidecar name: text}; fa: the frame-analysis JSON, or None."""
    pkg = corpus / "runs" / run / "packages" / vid
    (pkg / f"{vid}_report_csvs").mkdir(parents=True, exist_ok=True)
    (pkg / f"{vid}_qc_metadata").mkdir(exist_ok=True)
    (pkg / "package.json").write_text(json.dumps(
        {"video_id": vid, "source": f"/src/{vid}", "video_file": f"{vid}.mkv", "video": {"fps": 30000 / 1001}}))
    for name, text in (csvs or {}).items():
        (pkg / f"{vid}_report_csvs" / name).write_text(text)
    if fa is not None:
        (pkg / f"{vid}_qc_metadata" / f"{vid}_enhanced_frame_analysis.json").write_text(json.dumps(fa))
    return corpus / "runs" / run


def truth(corpus, detector, *rows):
    """Write truth/<detector>.csv from (video_id, tc_in, tc_out, verdict) tuples."""
    with open(corpus / "truth" / f"{detector}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(scorecard.COLUMNS)
        for vid, tc_in, tc_out, verdict in rows:
            w.writerow([vid, tc_in, tc_out, verdict, "test", ""])


def outcomes(run, tolerance=1.0):
    rows, problems = scorecard.load_truth()
    assert not problems, problems
    results, _ = scorecard.score(run, rows, tolerance)
    return [r["outcome"] for r in results]


# ------------------------------------------------------------------- scoring ---

def test_present_and_absent_rows_score_against_reported_events(corpus):
    run = package(corpus, A, csvs={"qct-parse_audio_dropout.csv": dropout_csv(("00:01:00:00", "00:01:00:10"))})
    truth(corpus, "audio_dropout",
          (A, "00:01:00:00", "00:01:00:10", "present"),    # reported           -> hit
          (A, "00:05:00:00", "00:05:00:10", "present"),    # nothing there      -> MISS
          (A, "00:05:00:00", "00:05:00:10", "absent"),     # nothing there      -> clean
          (A, "00:00:59:00", "00:01:01:00", "absent"))     # reported inside it -> FALSE_ALARM
    assert outcomes(run) == ["hit", "MISS", "clean", "FALSE_ALARM"]


def test_a_detector_that_did_not_run_is_not_clean(corpus):
    """No sidecar means the detector found nothing either way: neither a pass nor a miss."""
    run = package(corpus, A)
    truth(corpus, "audio_dropout",
          (A, "00:01:00:00", "00:01:00:10", "absent"),
          (A, "00:01:00:00", "00:01:00:10", "present"),
          (B, "", "", "absent"))
    assert outcomes(run) == ["not_run", "not_run", "no_package"]


def test_a_sidecar_with_no_events_is_clean(corpus):
    run = package(corpus, A, csvs={"qct-parse_audio_dropout.csv": dropout_csv()})
    truth(corpus, "audio_dropout", (A, "", "", "absent"), (A, "00:01:00:00", "00:01:00:10", "present"))
    assert outcomes(run) == ["clean", "MISS"]


def test_whole_tape_absent_row_catches_any_event(corpus):
    run = package(corpus, A, csvs={"qct-parse_audio_dropout.csv": dropout_csv(("00:20:00:00", "00:20:00:01"))})
    truth(corpus, "audio_dropout", (A, "", "", "absent"))
    assert outcomes(run) == ["FALSE_ALARM"]


def test_tolerance_widens_the_span_on_each_side(corpus):
    run = package(corpus, A, fa={"bars_regions": [{"start": 100.0, "end": 110.0}]})
    truth(corpus, "bars", (A, "110.8", "120", "absent"), (A, "111.5", "120", "absent"))
    assert outcomes(run, tolerance=1.0) == ["FALSE_ALARM", "clean"]
    assert outcomes(run, tolerance=0.0) == ["clean", "clean"]


def test_hit_reports_edge_errors(corpus):
    run = package(corpus, A, fa={"bars_regions": [{"start": 1.0, "end": 43.5}]})
    truth(corpus, "bars", (A, "2", "36", "present"))
    rows, _ = scorecard.load_truth()
    (res,), _ = scorecard.score(run, rows, 1.0)
    assert (res["outcome"], res["start_err"], res["end_err"]) == ("hit", -1.0, 7.5)


def test_compare_lists_only_rows_whose_outcome_changed(corpus, capsys):
    run_a = package(corpus, A, run="a", csvs={"qct-parse_audio_dropout.csv": dropout_csv(("00:01:00:00", "00:01:00:10"))})
    run_b = package(corpus, A, run="b", csvs={"qct-parse_audio_dropout.csv": dropout_csv()})
    truth(corpus, "audio_dropout",
          (A, "00:01:00:00", "00:01:00:10", "absent"),     # FALSE_ALARM -> clean
          (A, "00:09:00:00", "00:09:00:10", "absent"))     # clean in both
    rows, _ = scorecard.load_truth()
    scorecard.compare(run_a, run_b, rows, 1.0)
    out = capsys.readouterr().out
    assert "better" in out and "FALSE_ALARM -> clean" in out
    assert "1 of 2 truth rows changed outcome" in out


# ----------------------------------------------------------------- detectors ---

def test_audible_timecode_reads_the_consensus_table_not_per_method_detections(corpus):
    """The sidecar's second table lists raw per-method detections under the same first header.
    Reading it reported audible timecode on tapes whose sidecar says Detected: No."""
    not_detected = ("Audible Timecode Detection Results\nAudible Timecode Detected,No\nRegions Detected,0\n\n"
                    "Per-Method Detections,1\n\n"
                    "Start Time,End Time,Criterion,Channel,Confidence,Details\n"
                    "00:00:49:08,00:04:59:18,R128-A,n/a,high,x\n")
    detected = ("Audible Timecode Detection Results\nAudible Timecode Detected,Yes\nRegions Detected,1\n\n"
                "Start Time,End Time,Duration,Channel\n"
                "00:00:10:00,00:00:40:00,30.0,2\n\n"
                "Per-Method Detections,1\n\n"
                "Start Time,End Time,Criterion,Channel,Confidence,Details\n"
                "00:10:00:00,00:11:00:00,R128-A,n/a,high,x\n")
    run = package(corpus, A, csvs={"qct-parse_audible_timecode.csv": not_detected})
    package(corpus, B, csvs={"qct-parse_audible_timecode.csv": detected})
    truth(corpus, "audible_timecode",
          (A, "", "", "absent"),
          (B, "00:00:10:00", "00:00:40:00", "present"),
          (B, "00:10:00:00", "00:11:00:00", "absent"))
    assert outcomes(run) == ["clean", "hit", "clean"]


def test_chroma_phase_reads_clock_times_from_the_events_sidecar(corpus):
    events = ("Event,Start (HH:MM:SS.mmm),End (HH:MM:SS.mmm),Duration (s)\n"
              "1,00:23:19.7650,00:23:20.7990,1.034\n")
    run = package(corpus, A, csvs={"qct-parse_chroma_phase_events.csv": events})
    pkg = scorecard.Package(run / "packages" / A)
    assert scorecard.det_chroma_phase(pkg, scorecard.Timing(A, 29.97)) == [(1399.765, 1400.799)]


@pytest.mark.parametrize("fa, expected", [
    ({"bars_regions": [{"start": 1.0, "end": 20.0}], "color_bars_end_time": 20.0}, [(1.0, 20.0)]),
    ({}, None),                                    # no frame analysis at all
    ({"initial_borders": {}}, []),                 # ran, no bars
    ({"color_bars_end_time": 160.8}, None),        # an end with no span list cannot be scored
    ({"bars_regions": [{"start": 462.2, "end": 464.3}], "color_bars_end_time": 27.7}, [(462.2, 464.3)]),
])
def test_bars_uses_the_span_list_and_never_invents_a_span_from_an_end_time(corpus, fa, expected):
    run = package(corpus, A, fa=fa or None)
    assert scorecard.det_bars(scorecard.Package(run / "packages" / A), None) == expected


@pytest.mark.parametrize("fa, expected", [
    ({"duplicate_frame_detection": {"verification_available": False, "runs": []}}, None),
    ({"duplicate_frame_detection": {"runs": [{"start_time": 5.0, "end_time": 6.0}]}}, [(5.0, 6.0)]),
    ({"other": 1}, None),
])
def test_duplicate_frames_unverified_is_not_run(corpus, fa, expected):
    run = package(corpus, A, fa=fa)
    assert scorecard.det_duplicate_frames(scorecard.Package(run / "packages" / A), None) == expected


@pytest.mark.parametrize("result, expected", [
    ({"status": "unknown", "splice_events": []}, None),            # the audio scan could not decode
    ({"status": "clean", "spike_times": []}, None),                # spectrogram-era result
    ({"status": "clean", "splice_events": [], "silence_gaps": []}, []),
    ({"status": "detected", "splice_events": [{"time_s": 12.5}],
      "silence_gaps": [{"start_s": 1745.0, "end_s": 1746.5}]}, [(12.5, 12.5), (1745.0, 1746.5)]),
])
def test_dropped_samples_could_not_measure_is_not_run(corpus, result, expected):
    run = package(corpus, A, fa={"dropped_sample_detection": result})
    assert scorecard.det_dropped_samples(scorecard.Package(run / "packages" / A), None) == expected


# ---------------------------------------------------------------------- time ---

def test_timing_reads_timecode_seconds_and_clock_times(corpus):
    t = scorecard.Timing(A, 30000 / 1001)
    assert t.seconds("52.8") == 52.8
    assert t.seconds("1750") == 1750.0
    assert t.seconds("00:02:40.761") == pytest.approx(160.761)
    assert t.seconds("00:00:10:00") == pytest.approx(10.01)       # NDF: 300 frames at 29.97
    assert t.seconds("01:00:00:00") == pytest.approx(3603.6)      # NDF runs 3.6 s/hour behind
    with pytest.raises(ValueError):
        t.seconds("about a minute in")


def test_timing_subtracts_the_cached_start_timecode(corpus):
    (corpus / "labels" / "timecode.json").write_text(json.dumps(
        {A: {"fps": 29.97, "start_timecode": "01:00:00;00"}}))
    t = scorecard.Timing(A, 30000 / 1001)
    assert t.drop is True
    assert t.seconds("01:00:10;00") == pytest.approx(10.0, abs=0.02)   # drop-frame tracks the clock
    assert scorecard.Timing(B, 29.97).seconds("00:00:10:00") == pytest.approx(10.01, abs=0.001)   # not listed: starts at zero


# --------------------------------------------------------------------- truth ---

def test_load_truth_reports_bad_rows_and_keeps_good_ones(corpus):
    truth(corpus, "bars",
          (A, "1", "20", "present"),
          (A, "1", "20", "maybe"),          # not a verdict
          (A, "1", "", "absent"),           # half a span
          ("", "1", "2", "absent"),         # no tape
          (A, "", "", "present"))           # a present row needs a span
    truth(corpus, "not_a_detector", (A, "", "", "absent"))
    rows, problems = scorecard.load_truth()
    assert [r["verdict"] for r in rows["bars"]] == ["present"]
    assert len(problems) == 5
    assert any("not_a_detector" in p for p in problems)
    assert any("bars.csv:3" in p and "maybe" in p for p in problems)


def test_whole_tape_present_is_allowed_for_whole_tape_detectors(corpus):
    truth(corpus, "audible_timecode", (A, "", "", "present"))
    rows, problems = scorecard.load_truth()
    assert not problems and len(rows["audible_timecode"]) == 1


# ------------------------------------------------------------------ unjudged ---

def unjudged(run, detector):
    rows, problems = scorecard.load_truth()
    assert not problems
    return [(vid, a, b) for vid, a, b, _, _ in scorecard.unjudged_events(run, rows, 1.0, detector)]


def test_unjudged_lists_events_no_truth_row_covers(corpus):
    run = package(corpus, A, csvs={"qct-parse_audio_dropout.csv": dropout_csv(
        ("00:01:00:00", "00:01:00:10"),
        ("00:07:00:00", "00:07:00:05"),
        ("00:07:00:00", "00:07:00:05"))})          # the same event on a second channel
    truth(corpus, "audio_dropout", (A, "00:01:00:00", "00:01:00:10", "absent"))
    assert unjudged(run, "audio_dropout") == [(A, "00:07:00:00", "00:07:00:05")]


def test_unjudged_works_before_any_truth_exists(corpus):
    run = package(corpus, A, fa={"bars_regions": [{"start": 0.0, "end": 30.03}]})
    assert unjudged(run, "bars") == [(A, "00:00:00:00", "00:00:30:00")]


def test_unjudged_leaves_out_tapes_held_out_for_that_detector(corpus):
    run = package(corpus, A, fa={"bars_regions": [{"start": 1.0, "end": 20.0}]})
    package(corpus, B, fa={"bars_regions": [{"start": 2.0, "end": 30.0}]})
    ledger.append([{"video_id": B, "topic": "bars", "role": "heldout", "date": "2026-11-14",
                    "source": "", "note": ""},
                   {"video_id": A, "topic": "chroma_phase", "role": "heldout", "date": "2026-11-14",
                    "source": "", "note": ""}])     # held out for another detector: still shown
    assert [vid for vid, _, _ in unjudged(run, "bars")] == [A]


# --------------------------------------------------------------------- sheet ---

def fill(sheet, verdicts):
    """Set verdict (and note) by row number in a review workbook."""
    import openpyxl
    wb = openpyxl.load_workbook(sheet)
    ws = wb["unjudged"]
    for row, (verdict, note) in verdicts.items():
        ws.cell(row=row, column=6, value=verdict)
        ws.cell(row=row, column=7, value=note)
    wb.save(sheet)


def sheet_run(corpus):
    run = package(corpus, A, csvs={"qct-parse_audio_dropout.csv": dropout_csv(("00:07:00:00", "00:07:00:05"))},
                  fa={"bars_regions": [{"start": 1.0, "end": 20.0}, {"start": 600.0, "end": 602.0}]})
    truth(corpus, "bars")
    sheet = corpus / "review.xlsx"
    scorecard.write_sheet(run, scorecard.load_truth()[0], 1.0, list(scorecard.DETECTORS), sheet)
    return run, sheet


def test_sheet_round_trip_appends_judged_rows_and_leaves_blank_ones_unjudged(corpus, capsys):
    run, sheet = sheet_run(corpus)
    # rows: 2 = audio dropout, 3 and 4 = the two bars spans (detector order, then time)
    fill(sheet, {2: ("absent", "silence, not a dropout"), 3: ("present", "")})
    scorecard.import_sheet(sheet, scorecard.load_truth()[0])

    rows, problems = scorecard.load_truth()
    assert not problems
    assert [(r["video_id"], r["verdict"], r["note"]) for r in rows["audio_dropout"]] == [
        (A, "absent", "silence, not a dropout")]
    assert [(r["tc_in"], r["verdict"]) for r in rows["bars"]] == [("00:00:01:00", "present")]
    assert "review.xlsx" in rows["bars"][0]["source"]
    assert [vid for vid, _, _ in unjudged(run, "audio_dropout")] == []
    assert len(unjudged(run, "bars")) == 1                     # the blank row is still unjudged

    scorecard.import_sheet(sheet, scorecard.load_truth()[0])   # a second import adds nothing
    assert len(scorecard.load_truth()[0]["bars"]) == 1
    assert "0 row(s) added" in capsys.readouterr().out


def test_sheet_import_with_a_bad_verdict_writes_nothing(corpus):
    _, sheet = sheet_run(corpus)
    fill(sheet, {2: ("absent", ""), 3: ("yes", "")})
    with pytest.raises(SystemExit):
        scorecard.import_sheet(sheet, scorecard.load_truth()[0])
    assert not (corpus / "truth" / "audio_dropout.csv").exists()
    assert scorecard.load_truth()[0]["bars"] == []


# -------------------------------------------------------------------- ledger ---

def use(vid, topic, role, date):
    return {"video_id": vid, "topic": topic, "role": role, "date": date, "source": "s", "note": ""}


def test_tape_id_accepts_short_and_full_forms():
    assert ledger.tape_id("2213") == ledger.tape_id("02213") == ledger.tape_id("JPC_AV_02213") == "JPC_AV_02213"
    with pytest.raises(SystemExit):
        ledger.tape_id("LC_1234")


def test_status_follows_the_order_of_uses():
    assert ledger.status(None) == "fresh"
    assert ledger.status([use(A, "t", "dev", "2026-11-01")]) == "dev"
    assert ledger.status([use(A, "t", "heldout", "2026-11-10")]) == "reserved"
    # released: a dev use after the test
    assert ledger.status([use(A, "t", "heldout", "2026-11-10"), use(A, "t", "dev", "2026-11-12")]) == "spent"
    # a dev use on the same day or before does not release it
    assert ledger.status([use(A, "t", "heldout", "2026-11-10"), use(A, "t", "dev", "2026-11-10")]) == "reserved"


def test_reserved_is_per_topic(corpus):
    ledger.append([use(A, "bars", "heldout", "2026-11-10"),
                   use(B, "bars", "heldout", "2026-11-10"), use(B, "bars", "dev", "2026-11-12"),
                   use(C, "chroma_phase", "heldout", "2026-11-10")])
    assert ledger.reserved("bars") == {A}
    assert ledger.reserved("chroma_phase") == {C}
    assert ledger.reserved("tone_leak") == set()


def test_truth_rows_make_a_tape_dev_for_that_detector(corpus):
    truth(corpus, "bars", (A, "1", "20", "present"), (A, "30", "40", "absent"))
    rows = ledger.read_rows()
    assert [(r["video_id"], r["topic"], r["role"]) for r in rows] == [(A, "bars", "dev")]
    package(corpus, A)
    package(corpus, B)
    assert ledger.fresh("bars", rows, ledger.known_tapes()) == [B]
    assert ledger.fresh("chroma_phase", rows, ledger.known_tapes()) == [A, B]
    assert ledger.fresh("chroma_phase", rows, ledger.known_tapes(), untouched=True) == [B]


def test_check_flags_a_tape_that_was_dev_before_it_was_held_out(corpus):
    ledger.append([use(A, "bars", "dev", "2026-11-01"), use(A, "bars", "heldout", "2026-11-10"),
                   use(B, "bars", "heldout", "2026-11-10"), use(B, "bars", "dev", "2026-11-12"),
                   use(C, "bars", "heldout", "2026-11-10")])
    truth(corpus, "bars", (C, "1", "20", "present"))        # undated look: counts as earlier
    found = ledger.problems(ledger.read_rows())
    assert len(found) == 2
    assert any(A in p and "not a fair test" in p for p in found)
    assert any(C in p and "truth/bars.csv" in p for p in found)


def test_check_flags_malformed_rows(corpus):
    ledger.append([use("02213", "bars", "dev", "2026-11-01"), use(A, "bars", "tested", "2026-11-01"),
                   use(A, "", "dev", "2026-11-01"), use(A, "bars", "dev", "Nov 1")])
    assert len(ledger.problems(ledger.read_rows())) == 4


def run_ledger(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["ledger.py", *argv])
    ledger.main()


def test_draw_holds_out_fresh_tapes_by_a_recorded_seed(corpus, monkeypatch, capsys):
    tapes = [f"JPC_AV_{n:05d}" for n in range(1, 11)]
    for vid in tapes:
        package(corpus, vid)
    ledger.append([use(tapes[0], "bars", "dev", "2026-11-01")])

    run_ledger(monkeypatch, "draw", "bars", "4", "--seed", "7")
    picked = capsys.readouterr().out.split()
    assert len(picked) == 4 and tapes[0] not in picked
    assert ledger.reserved("bars") == set(picked)
    drawn = [r for r in ledger.read_rows() if r["role"] == "heldout"]
    assert all("seed 7" in r["source"] and "4 of 9 fresh" in r["source"] for r in drawn)

    # the same seed over the same pool picks the same tapes; drawn tapes are no longer fresh
    import random
    assert picked == sorted(random.Random(7).sample(tapes[1:], 4))
    run_ledger(monkeypatch, "draw", "bars", "5", "--seed", "7")
    assert not set(capsys.readouterr().out.split()) & set(picked)
    with pytest.raises(SystemExit):
        run_ledger(monkeypatch, "draw", "bars", "1")           # none left


def test_add_records_a_use_and_releases_a_held_out_tape(corpus, monkeypatch, capsys):
    ledger.append([use(A, "bars", "heldout", "2026-11-10")])
    run_ledger(monkeypatch, "add", "bars", "dev", "1", "--date", "2026-11-20", "--note", "scored")
    assert ledger.reserved("bars") == set()
    assert ledger.status([r for r in ledger.read_rows() if r["video_id"] == A]) == "spent"
