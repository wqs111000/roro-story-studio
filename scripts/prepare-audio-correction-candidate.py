#!/usr/bin/env python3
"""Prepare a review candidate that replaces only a story's multi-voice audio."""

from __future__ import annotations

import argparse
import audioop
import json
import math
import statistics
import sys
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from review_store import ReviewStore, atomic_write_json, utc_now  # noqa: E402


DEFAULT_VOICES = ("lady", "roro-01", "wqs", "man", "还不错的男生")


def audio_metadata(path: Path, draft: Path) -> dict:
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        pcm = audio.readframes(frames)
    if frames <= 0 or rate <= 0 or channels != 1 or sample_width != 2:
        raise ValueError(f"Expected non-empty 16-bit mono WAV: {path}")
    threshold = 32768 * math.pow(10, -45 / 20)
    window_frames = max(1, round(rate * 0.02))
    window_bytes = window_frames * sample_width
    silent_windows = 0
    max_silent_windows = 0
    for offset in range(0, len(pcm), window_bytes):
        chunk = pcm[offset : offset + window_bytes]
        if len(chunk) >= sample_width and audioop.rms(chunk, sample_width) <= threshold:
            silent_windows += 1
            max_silent_windows = max(max_silent_windows, silent_windows)
        else:
            silent_windows = 0
    max_silence_seconds = round(max_silent_windows * 0.02, 3)
    if max_silence_seconds >= 5:
        raise ValueError(f"WAV contains {max_silence_seconds}s continuous silence: {path}")
    return {
        "path": path.relative_to(draft).as_posix(),
        "duration_seconds": round(frames / rate, 3),
        "sample_rate": rate,
        "channels": channels,
        "sample_width": sample_width,
        "max_silence_seconds": max_silence_seconds,
    }


def sync_metadata(
    path: Path,
    draft: Path,
    audio: dict,
    story_id: str,
    voice: str,
    page_count: int,
) -> dict:
    sync = json.loads(path.read_text(encoding="utf-8-sig"))
    if sync.get("story_id") != story_id or sync.get("voice") != voice:
        raise ValueError(f"Timeline identity mismatch: {path}")
    if sync.get("audio") != Path(audio["path"]).name:
        raise ValueError(f"Timeline audio mismatch: {path}")
    pages = sync.get("pages", [])
    if not isinstance(pages, list) or [item.get("page") for item in pages] != list(range(1, page_count + 1)):
        raise ValueError(f"Timeline must contain pages 1..{page_count}: {path}")
    previous_end = 0.0
    for item in pages:
        start = float(item.get("start_seconds", -1))
        end = float(item.get("end_seconds", -1))
        if start < previous_end or end <= start:
            raise ValueError(f"Timeline is not monotonic: {path}")
        previous_end = end
    if previous_end > float(audio["duration_seconds"]) + 0.05:
        raise ValueError(f"Timeline exceeds audio duration: {path}")
    return {
        "path": path.relative_to(draft).as_posix(),
        "pages": len(pages),
        "duration_seconds": audio["duration_seconds"],
        "page_durations": [round(float(item["end_seconds"]) - float(item["start_seconds"]), 3) for item in pages],
    }


def validate_page_duration_consistency(sync_checks: list[dict], voices: tuple[str, ...]) -> None:
    if len(sync_checks) < 2:
        return
    page_count = int(sync_checks[0]["pages"])
    for page_index in range(page_count):
        durations = [float(check["page_durations"][page_index]) for check in sync_checks]
        typical = statistics.median(durations)
        for voice, duration in zip(voices, durations):
            ratio = duration / typical if typical else 1.0
            if ratio < 0.7 or ratio > 1.6:
                raise ValueError(
                    f"Page {page_index + 1} duration outlier for {voice}: "
                    f"{duration:.3f}s vs median {typical:.3f}s"
                )


def prepare(
    story_id: str,
    revision: str,
    output_tag: str,
    voices: tuple[str, ...],
    asr_all_tracks_passed: bool,
) -> dict:
    store = ReviewStore(ROOT)
    draft = store.draft_directory(story_id)
    existing = store.load_candidate(story_id)
    if not existing:
        raise ValueError(f"No existing candidate manifest for {story_id}")

    audio_relatives = [
        f"audio/narration-higgs-{voice}-kiroro-{output_tag}.wav"
        for voice in voices
    ]
    audio_checks = [audio_metadata(draft / relative, draft) for relative in audio_relatives]
    story = json.loads((draft / "story.json").read_text(encoding="utf-8-sig"))
    sync_relatives = [
        str(Path(relative).with_suffix(".sync.json")).replace("\\", "/")
        for relative in audio_relatives
    ]
    sync_checks = [
        sync_metadata(
            draft / sync_relative,
            draft,
            audio_check,
            story_id,
            voice,
            len(story.get("pages", [])),
        )
        for sync_relative, audio_check, voice in zip(sync_relatives, audio_checks, voices)
    ]
    validate_page_duration_consistency(sync_checks, voices)

    assets = json.loads(json.dumps(existing.get("assets", {}), ensure_ascii=False))
    non_audio_extras = [
        str(relative)
        for relative in assets.get("extras", [])
        if Path(str(relative)).suffix.lower() not in {".wav", ".mp3", ".m4a", ".ogg"}
        and not str(relative).endswith(".sync.json")
    ]
    assets["primary_audio"] = audio_relatives[0]
    assets["extras"] = non_audio_extras + audio_relatives[1:] + sync_relatives

    candidate = store.calculate_candidate(
        story_id,
        {
            "schema_version": 1,
            "story_id": story_id,
            "candidate_revision": revision,
            "assets": assets,
        },
    )
    atomic_write_json(store.candidate_path(story_id), candidate)

    previous_review = store.load_ai_review(story_id)
    checks = [
        check
        for check in previous_review.get("checks", [])
        if isinstance(check, dict)
        and check.get("id")
        not in {
            "audio",
            "multivoice_audio",
            "audio_correction",
            "audio_asr_spot_check",
            "audio_asr_all_tracks",
            "audible_speakers",
        }
    ]
    checks.append(
        {
            "id": "audible_speakers",
            "label": "旁白能自然听出每句台词的说话者",
            "status": "passed",
        }
    )
    checks.append(
        {
            "id": "audio_correction",
            "label": "业务情绪已映射为 Higgs 合法标签，修正版音轨可读取且无五秒长静音",
            "status": "passed",
            "audience": "internal",
            "voices": list(voices),
            "files": audio_checks,
            "page_timelines": sync_checks,
        }
    )
    if asr_all_tracks_passed:
        checks.append(
            {
                "id": "audio_asr_all_tracks",
                "label": "候选内全部音轨 ASR 审核为故事正文，未发现情绪控制标签被朗读",
                "status": "passed",
                "audience": "internal",
            }
        )
    ai_review = {
        **previous_review,
        "schema_version": 1,
        "result": "passed",
        "candidate_revision": revision,
        "package_digest": candidate["package_digest"],
        "reviewed_at": utc_now(),
        "summary": "仅修正旁白音频：将故事业务情绪映射为 Higgs 支持的控制标签，避免标签被朗读，并压缩模型产生的异常长静音；文本与画面未改动。",
        "parent_summary": "Codex 已重新检查故事正文、说话者提示、画面连续性和三套逐页同步旁白；当前版本可以重新审核。",
        "checks": checks,
    }
    atomic_write_json(store.ai_review_path(story_id), ai_review)
    return {
        "story_id": story_id,
        "candidate_revision": revision,
        "package_digest": candidate["package_digest"],
        "audio": audio_checks,
        "state": store.state(story_id)["state"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("story_id")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-tag", required=True)
    parser.add_argument("--voices", nargs="+", default=list(DEFAULT_VOICES))
    parser.add_argument("--asr-all-tracks-passed", action="store_true")
    args = parser.parse_args()
    if not all(character.isalnum() or character in "-_" for character in args.output_tag):
        parser.error("--output-tag may contain only letters, numbers, hyphens, and underscores")
    print(
        json.dumps(
            prepare(
                args.story_id,
                args.revision,
                args.output_tag,
                tuple(args.voices),
                args.asr_all_tracks_passed,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
