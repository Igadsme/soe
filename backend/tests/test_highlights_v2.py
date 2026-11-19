"""Highlight extraction v2: signals on real lavfi media, ranking, dedupe, subtitles, loudness and QC (Whisper stubbed)."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import media, media_tasks, pipeline, signals, subtitles, templates, worker
from app.config import settings
from app.media import MediaProbe
from app.pipeline import TranscriptSegment, Word

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True, timeout=120)  # noqa: S603


def seg(start, end, text):
    return TranscriptSegment(start, end, text)


# ------------------------------------------------------------------ ranking (pure)
def test_v2_prefers_coherent_question_answer_windows_with_evidence():
    segments = [seg(0, 3, "um so yeah"), seg(3.5, 7, "Why do most sourdough loaves fail?"),
                seg(7.2, 12, "Because the starter is not active enough."), seg(12.4, 16, "Here's the trick: feed it twice!"),
                seg(20, 24, "and then later we can talk about the weather maybe")]
    best = pipeline.extract_candidates_v2(segments, None, limit=3, min_seconds=4, max_seconds=30, duration=30)
    top = best[0]
    assert top.start <= 3.5 and top.end >= 12 and "Why do most" in top.description
    assert top.evidence["version"] == "v2" and set(top.evidence["components"]) == {
        "hook_strength", "relevance", "emotional_engagement", "clarity", "novelty", "pacing", "payoff"}
    assert top.evidence["boundary"] == "sentence_aligned" and top.evidence["segments"] >= 2
    # Boundaries are snapped into the surrounding pauses, never mid-word.
    assert 3.25 <= top.start <= 3.5
    for first, second in zip(best, best[1:], strict=False):
        assert first.score >= second.score
        assert first.end <= second.start or second.end <= first.start


def test_v2_dedupes_repeated_content_and_windows_respect_bounds():
    line = "This single tip doubled my bread rise overnight, seriously."
    segments = [seg(0, 6, line), seg(10, 16, "Totally different topic about knife skills and safety."), seg(30, 36, line)]
    chosen = pipeline.extract_candidates_v2(segments, None, limit=5, min_seconds=4, max_seconds=10, duration=40)
    texts = [c.evidence["transcript"] for c in chosen]
    assert sum(line in text for text in texts) == 1
    assert all(4 <= c.end - c.start <= 10.8 for c in chosen)
    assert pipeline.jaccard(line, line) == 1 and pipeline.iou((0, 10), (5, 15)) == pytest.approx(1 / 3)


def test_v2_event_windows_from_audio_peaks_and_motion_without_speech():
    audio = signals.AudioProfile(0.5, [-40.0] * 60, ["speech"] * 60, [20.0], [(26.0, 27.0)])
    motion = signals.MotionProfile(1.0, [0.001] * 30, [[0.0] * 4] * 30, 4)
    motion.energy[10] = 0.2
    sig = signals.SourceSignals(30, audio, [16.5], motion, 30)
    chosen = pipeline.extract_candidates_v2([], sig, limit=5, min_seconds=4, max_seconds=20, duration=30)
    starts = sorted(c.start for c in chosen)
    assert len(chosen) == 2 and all(c.category == "event" for c in chosen)
    peak = next(c for c in chosen if c.start > 15)
    assert peak.start == 16.5 and peak.end == 26.0 and peak.evidence["boundary"] == "snapped_to_scene_cut+silence"
    assert starts[0] == 7.0
    assert pipeline.motion_spikes(None) == []


def test_hook_coherence_payoff_heuristics():
    assert pipeline.hook_strength("Wait, why does this never work?") > pipeline.hook_strength("ok then")
    assert pipeline.coherence(["and so it goes"]) < pipeline.coherence(["What happened?", "It worked."])
    assert pipeline.payoff(["That's why it works!"]) == 1.0
    assert pipeline.window_signals(None, 0, 1)["motion"] == 0.0


# ------------------------------------------------------------------ subtitles (pure)
def test_word_level_cues_are_readable_and_non_overlapping():
    text = "Why do most sourdough loaves fail? Because the starter is not active enough. Here's the trick: feed it twice!"
    words = [Word(s, e, w) for s, e, w in subtitles.distribute(1.0, 9.0, text)]
    segment = TranscriptSegment(1.0, 9.0, text, tuple(words))
    timed = subtitles.timed_words([segment], 0.5, 9.5)
    cues = subtitles.build_cues(timed, 9.0)
    assert len(cues) >= 3 and subtitles.readability_violations(cues, 9.0) == []
    assert all(len(line) <= subtitles.MAX_LINE_CHARS for cue in cues for line in cue.lines)
    assert cues[0].start == pytest.approx(0.5, abs=0.01)
    assert cues[0].text.endswith("fail?")  # sentence boundaries end cues


def test_readability_violations_and_srt_ass_roundtrip(tmp_path):
    bad = [subtitles.Cue(0, 9, ["x" * 50]), subtitles.Cue(8, 8.1, ["way too many characters for a tenth of a second"])]
    problems = subtitles.readability_violations(bad, 8.05)
    assert any("longer than" in p for p in problems) and any("overlaps" in p for p in problems)
    assert any("reading speed" in p for p in problems) and any("after the clip" in p for p in problems)
    cues = subtitles.build_cues(subtitles.distribute(0, 4, "Hello there {\\b1}friend"), 4)
    subtitles.write_srt(tmp_path / "a.srt", cues)
    assert [c.text for c in subtitles.parse_srt((tmp_path / "a.srt").read_text())] == [c.text for c in cues]
    subtitles.write_ass(tmp_path / "a.ass", cues, templates.get("bold_captions"), 4, brand="Soe Kitchen")
    ass = (tmp_path / "a.ass").read_text()
    assert "PlayResY: 1920" in ass and "Soe Kitchen" in ass and "{\\b1}" not in ass and "Style: Caption,DejaVu Sans,84" in ass
    assert subtitles.ass_timestamp(3661.5) == "1:01:01.50"
    assert templates.get("nope").name == "clean"
    assert "crop=" in templates.video_filter(templates.get("gameplay_focus"), 0.2, None)
    assert "crop=" not in templates.video_filter(templates.get("clean"), 0.2, Path("/x/a.srt"))


# ------------------------------------------------------------------ signals on real media
@pytest.fixture(scope="module")
def signal_source(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("sig") / "source.mp4"
    # 0-4 s: tone; 4-6 s: silence; 6-7 s: loud noise burst; 7-12 s: tone. Video cuts at 6 s; motion on the left third.
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=320x180:d=6:r=10", "-f", "lavfi", "-i", "color=c=white:s=320x180:d=6:r=10",
           "-f", "lavfi", "-i", "color=c=red:s=40x40:d=12:r=10",
           "-f", "lavfi", "-i", "sine=f=330:d=4,volume=0.1", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=2",
           "-f", "lavfi", "-i", "anoisesrc=d=1:a=0.9", "-f", "lavfi", "-i", "sine=f=330:d=5,volume=0.1",
           "-filter_complex",
           "[0:v][1:v]concat=n=2:v=1:a=0[bg];[bg][2:v]overlay=x='20+40*sin(t*6)':y=70[v];"
           "[3:a]aresample=16000[a0];[4:a]aresample=16000[a1];[5:a]aresample=16000[a2];[6:a]aresample=16000[a3];"
           "[a0][a1][a2][a3]concat=n=4:v=0:a=1[a]",
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
    return path


@needs_ffmpeg
def test_audio_profile_labels_silence_tone_and_burst(signal_source):
    profile = signals.audio_profile(signal_source, hop=0.5, max_seconds=12)
    assert profile.labels[1] == "tonal" and profile.labels[9] == "silence" and "burst" in profile.labels[12:14]
    assert (4.0, 6.0) in profile.silences or any(a <= 4.5 and b >= 5.5 for a, b in profile.silences)
    assert any(5.5 <= peak <= 7.0 for peak in profile.peaks)
    assert profile.loudness_db[profile.window(4.5, 5.5)][0] < -45


@needs_ffmpeg
def test_scene_motion_and_action_center(signal_source):
    assert any(5.5 <= cut <= 6.5 for cut in signals.scene_changes(signal_source))
    motion = signals.motion_profile(signal_source, 12, fps=5)
    assert motion and len(motion.energy) <= 61
    assert motion.action_center(0.5, 5.5) < 0.35  # moving square lives in the left third
    summary = signals.analyze(signal_source, 12, True).summary()
    assert summary["frames_sampled"] > 0 and summary["audio_labels"]["silence"] >= 2


@needs_ffmpeg
def test_motion_frame_budget_caps_sampling(signal_source, monkeypatch):
    monkeypatch.setattr(settings, "signal_max_frames_per_hour", 60)
    motion = signals.motion_profile(signal_source, 12, fps=10)
    assert motion is None or len(motion.energy) <= 10


def test_speaker_turns_from_pauses_and_level_changes():
    profile = signals.AudioProfile(1.0, [-20, -20, -35, -35, -35], ["speech"] * 5, [], [])
    turns = signals.speaker_turns([seg(0, 2, "a"), seg(2.1, 4, "b"), seg(5, 6, "c")], profile)
    assert turns == [2.1, 5]


# ------------------------------------------------------------------ render loudness + QC
@needs_ffmpeg
def test_two_pass_loudnorm_hits_target_within_tolerance(tmp_path):
    source = tmp_path / "quiet.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=8", "-f", "lavfi", "-i", "sine=f=220:d=8,volume=0.02",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source))
    output = media.render_vertical(source, tmp_path / "out.mp4", 1, 7, None, templates.get("clean"))
    log = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(output), "-af", "ebur128", "-f", "null", "-"],  # noqa: S603
                         capture_output=True, text=True, timeout=60).stderr
    integrated = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", log)[-1])
    assert abs(integrated - settings.loudness_target_lufs) <= 1.5
    assert media.probe(output).width == 1080 and media.probe(output).height == 1920
    measured = media.measure_loudness(source, 1, 7, -14)
    assert set(json.loads(json.dumps(measured))) >= {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}


@needs_ffmpeg
def test_qc_flags_black_frozen_silent_clip_but_not_still_with_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "qc_min_clip_seconds", 1)
    black = tmp_path / "black.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=4:r=25", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=4",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(black))
    still = tmp_path / "still.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=1080x1920:d=4:r=25", "-f", "lavfi", "-i", "sine=f=300:d=4,volume=0.3",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(still))
    cue = tmp_path / "c.srt"
    cue.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello there\n", encoding="utf-8")
    bad, ok = media_tasks.quality_check([{"clip_id": 1, "output": str(black), "subtitle": str(cue)},
                                         {"clip_id": 2, "output": str(still), "subtitle": str(cue)}], True)
    assert {"mostly_black_video", "frozen_and_silent", "mostly_silent_audio"} <= set(bad["reasons"])
    assert ok["reasons"] == [] and ok["metrics"]["freeze_ratio"] >= 0.9 and ok["metrics"]["subtitle_readability"] == []
    unreadable = tmp_path / "u.srt"
    unreadable.write_text("1\n00:00:00,000 --> 00:00:00,100\n" + "x" * 80 + "\n", encoding="utf-8")
    flagged, = media_tasks.quality_check([{"clip_id": 3, "output": str(still), "subtitle": str(unreadable)}], True)
    assert "subtitle_readability" in flagged["reasons"]


# ------------------------------------------------------------------ worker integration (Whisper stubbed)
@needs_ffmpeg
def test_render_source_uses_signals_words_and_templates(signal_source, tmp_path, monkeypatch):
    text = "Why does the tone stop? Because silence is part of the test! Here's the trick: listen again."
    words = tuple(Word(s, e, w) for s, e, w in subtitles.distribute(0.2, 3.8, text))
    segments = [TranscriptSegment(0.2, 3.8, text, words), TranscriptSegment(7.2, 11.5, "Second part keeps going strong.")]
    monkeypatch.setattr(worker, "FasterWhisperTranscriber", lambda _size: SimpleNamespace(transcribe=lambda _p: segments))
    monkeypatch.setattr(settings, "highlight_min_seconds", 3)
    monkeypatch.setattr(settings, "edit_template", "gameplay_focus")
    monkeypatch.setattr(settings, "brand_text", "Soe Test")
    monkeypatch.setattr(worker, "ingest_authorized", lambda src, dest: (Path(src), "digest", MediaProbe(12, 320, 180, True)))
    result = worker.render_source(str(signal_source), tmp_path, 3, 12)
    assert result["signals"]["frames_sampled"] > 0 and result["clips"]
    clip = result["clips"][0]
    assert clip["evidence"]["template"] == "gameplay_focus" and clip["evidence"]["word_timestamps"] is True
    assert clip["evidence"]["crop_center"] is not None and clip["evidence"]["subtitle_cues"] >= 2
    assert Path(clip["output_path"]).stat().st_size > 0
    assert subtitles.readability_violations(subtitles.parse_srt(Path(clip["subtitle_path"]).read_text())) == []
    qc, = media_tasks.quality_check([{"clip_id": 1, "output": clip["output_path"], "subtitle": clip["subtitle_path"]}], True)
    assert "unexpected_dimensions" not in qc["reasons"] and "subtitle_readability" not in qc["reasons"]


def test_signal_failure_falls_back_to_transcript_only(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise media.MediaProcessingError("nope")
    monkeypatch.setattr(signals, "audio_profile", boom)
    sig, error = worker.analyze_signals(tmp_path / "x.mp4", MediaProbe(10, 1, 1, True))
    assert sig is None and error == "MediaProcessingError"


def test_render_subtitle_without_word_times_is_readable(tmp_path):
    media_tasks.render_subtitle(tmp_path / "r.srt", 12, "A much longer operator edited caption that must be split into several readable cues for viewers.")
    cues = subtitles.parse_srt((tmp_path / "r.srt").read_text())
    assert len(cues) >= 2 and subtitles.readability_violations(cues, 12) == []
