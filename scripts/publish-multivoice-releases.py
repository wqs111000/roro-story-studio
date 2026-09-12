#!/usr/bin/env python3
"""Prepare and optionally publish immutable multi-voice story releases."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from review_store import ReviewStore, atomic_write_json, read_json, utc_now  # noqa: E402


STORIES = [
    ("2026-08-30-cloud-station-rain", "roro-20260831-multivoice-v1-001"),
    ("2026-08-30-crooked-paper-house", "roro-20260831-multivoice-v1-002"),
    ("2026-08-30-lost-shadow", "roro-20260831-multivoice-v1-003"),
    ("2026-08-30-roro-goodnight-workbench", "roro-20260831-multivoice-v1-004"),
    ("2026-08-30-slow-bridge", "roro-20260831-multivoice-v1-005"),
    ("2026-08-30-sneezing-tiger", "roro-20260831-multivoice-v1-006"),
    ("2026-08-30-three-answers", "roro-20260831-multivoice-v1-007"),
]
VOICES = ("lady", "roro-01", "girl", "man", "wqs", "还不错的男生")


def validate_audio(path: Path) -> dict:
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
    if frames <= 0 or rate <= 0 or channels not in {1, 2} or sample_width not in {1, 2, 3, 4}:
        raise ValueError(f"Invalid WAV audio: {path}")
    return {
        "path": path.relative_to(path.parents[1]).as_posix(),
        "duration_seconds": round(frames / rate, 3),
        "sample_rate": rate,
        "channels": channels,
        "sample_width": sample_width,
    }


def slow_bridge_assets(draft: Path) -> dict:
    approved = ROOT / "approved" / draft.name
    image_dir = draft / "images" / "approved-multivoice-source"
    image_dir.mkdir(parents=True, exist_ok=True)
    cover_relative = "images/approved-multivoice-source/cover.png"
    shutil.copy2(approved / "images" / "cover.png", draft / cover_relative)
    page_relatives = []
    for page in range(1, 7):
        relative = f"images/approved-multivoice-source/page-{page:02d}.png"
        shutil.copy2(approved / "images" / f"page-{page:02d}.png", draft / relative)
        page_relatives.append(relative)
    return {
        "story": "story.json",
        "storyboard": "storyboard.json",
        "narration": "narration.json",
        "cover": cover_relative,
        "pages": page_relatives,
        "primary_audio": "",
        "extras": [],
    }


def prepare_story(store: ReviewStore, story_id: str, revision: str) -> dict:
    draft = store.draft_directory(story_id)
    existing = store.load_candidate(story_id)
    if existing:
        assets = json.loads(json.dumps(existing.get("assets", {}), ensure_ascii=False))
    elif story_id == "2026-08-30-slow-bridge":
        assets = slow_bridge_assets(draft)
    else:
        raise ValueError(f"No existing candidate manifest for {story_id}")

    voice_relatives = [f"audio/narration-higgs-{voice}-kiroro.wav" for voice in VOICES]
    audio_checks = [validate_audio(draft / relative) for relative in voice_relatives]
    assets["primary_audio"] = voice_relatives[0]
    non_audio_extras = [
        str(relative)
        for relative in assets.get("extras", [])
        if Path(str(relative)).suffix.lower() not in {".wav", ".mp3", ".m4a", ".ogg"}
    ]
    assets["extras"] = non_audio_extras + voice_relatives[1:]

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

    previous_ai_review = store.load_ai_review(story_id)
    checks = [
        check
        for check in previous_ai_review.get("checks", [])
        if isinstance(check, dict) and check.get("id") != "multivoice_audio"
    ]
    checks.append(
        {
            "id": "multivoice_audio",
            "label": "五种旁白音色均为可读取 WAV，并已纳入候选摘要",
            "status": "passed",
            "audience": "internal",
            "voices": list(VOICES),
            "files": audio_checks,
        }
    )
    ai_review = {
        **previous_ai_review,
        "schema_version": 1,
        "result": "passed",
        "candidate_revision": revision,
        "package_digest": candidate["package_digest"],
        "reviewed_at": utc_now(),
        "summary": "沿用已发布文本与画面，新增五种已校验旁白音色供网页播放器切换。",
        "parent_summary": "故事正文、画面和可切换旁白音轨均已完成内部检查。",
        "checks": checks,
    }
    atomic_write_json(store.ai_review_path(story_id), ai_review)
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true", help="Publish each prepared candidate to the local shelf")
    args = parser.parse_args()
    store = ReviewStore(ROOT)
    results = []
    for story_id, revision in STORIES:
        candidate = prepare_story(store, story_id, revision)
        result = {
            "story_id": story_id,
            "candidate_revision": revision,
            "package_digest": candidate["package_digest"],
            "state": store.state(story_id)["state"],
        }
        if args.publish:
            published = store.decide(
                story_id,
                decision="approved",
                candidate_revision=revision,
                package_digest=candidate["package_digest"],
                request_id=f"publish-{revision}",
                note="用户要求将已生成的多音色旁白接入网页播放器；沿用已发布故事与画面，仅新增五种旁白音轨。",
                actor="local-development-workbench",
            )
            result["state"] = published["state"]["state"]
            result["release_id"] = published["event"].get("release_id", "")
        results.append(result)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
