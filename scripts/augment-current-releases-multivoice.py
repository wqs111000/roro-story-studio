#!/usr/bin/env python3
"""Prepare corrected multi-voice candidates from the latest draft audio.

This script intentionally does not guess from older published releases.  Those
directories can contain pre-emotion-fix WAVs, so choosing the first file found
can silently re-introduce the exact leakage this workflow is meant to prevent.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
from review_store import ReviewStore, atomic_write_json, read_json, utc_now  # noqa: E402

VOICE_RE = re.compile(r"narration-higgs-(lady|roro-01|girl|man|wqs|huihui|还不错的男生)-.*\.(wav|sync\.json)$")
VOICE_ORDER = ("lady", "roro-01", "wqs", "man", "还不错的男生")
REQUIRED_VOICES = frozenset(VOICE_ORDER)


def next_candidate_revision(story_id: str, approved_story_root: Path) -> str:
    """Return a new immutable revision id for regenerated candidate assets.

    A revision identifies a package, not a story.  Reusing the old deterministic
    id after audio or metadata changes causes the publish layer to (correctly)
    reject the candidate as a release conflict.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = re.sub(r"[^a-z0-9]+", "-", story_id.lower()).strip("-")[-12:]
    base = f"roro-{stamp}-audio-correction-{suffix}"
    releases = approved_story_root / "releases"
    revision = base
    counter = 2
    while (releases / revision).exists():
        revision = f"{base}-r{counter}"
        counter += 1
    return revision


def rel_path(root: Path, value: str) -> Path:
    return root.joinpath(*Path(value).as_posix().split("/"))


def best_voice_sources(story_root: Path, current_release: str) -> dict[str, tuple[Path, Path | None]]:
    # Only use a complete, explicitly paged draft set.  Published releases are
    # immutable history and may contain audio rendered before the emotion-token
    # fix; they must never be used as an implicit source.
    draft_audio = ROOT / "drafts" / story_root.name / "audio"
    grouped: dict[str, dict[str, tuple[Path, Path]]] = {}
    for wav in draft_audio.glob("narration-higgs-*-kiroro-*.wav"):
        match = re.fullmatch(
            r"narration-higgs-(lady|roro-01|wqs|man|还不错的男生)-kiroro-(paged-v[^.]+|v6)\.wav",
            wav.name,
        )
        if not match:
            continue
        sync = wav.with_suffix(".sync.json")
        if not sync.exists():
            continue
        try:
            timeline = json.loads(sync.read_text(encoding="utf-8-sig"))
            cover = timeline.get("cover")
            pages = timeline.get("pages") or []
            if not isinstance(cover, dict) or not str(cover.get("title") or "").strip():
                continue
            if not pages or float(pages[0].get("start_seconds", 0)) <= float(cover.get("end_seconds", 0)):
                continue
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        grouped.setdefault(match.group(2), {})[match.group(1)] = (wav, sync)

    complete = [
        (tag, sources)
        for tag, sources in grouped.items()
        if REQUIRED_VOICES.issubset(sources)
    ]
    if not complete:
        return {}
    # Prefer the newest complete tag as a unit, rather than mixing voices from
    # different revisions.  The minimum mtime prevents one late copied file
    # from making an incomplete set look newer than it is.
    _, sources = max(
        complete,
        key=lambda item: min(path.stat().st_mtime for path, _ in item[1].values()),
    )
    return sources


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish prepared candidates; omitted by default so parent review remains required",
    )
    parser.add_argument(
        "--asr-all-tracks-passed",
        action="store_true",
        help="Record that every candidate voice track passed the local FunASR leakage check",
    )
    parser.add_argument(
        "--story-id",
        action="append",
        dest="story_ids",
        help="Prepare only this story (repeat for multiple stories)",
    )
    args = parser.parse_args()
    store = ReviewStore(ROOT)
    results = []
    for story_root in sorted(p for p in store.approved_root.iterdir() if p.is_dir() and p.name != ".staging"):
        if args.story_ids and story_root.name not in args.story_ids:
            continue
        pointer_path = story_root / "current.json"
        if not pointer_path.exists():
            continue
        pointer = read_json(pointer_path)
        current_release = str(pointer.get("release_id", ""))
        current_dir = story_root / "releases" / current_release
        package = read_json(current_dir / "package-manifest.json")
        sources = best_voice_sources(story_root, current_release)
        if len(sources) < len(REQUIRED_VOICES):
            continue
        story_id = story_root.name
        draft = store.draft_directory(story_id)
        assets = copy.deepcopy(package["assets"])
        for relative in package["asset_hashes"]:
            source = rel_path(current_dir, relative)
            target = rel_path(draft, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        audio_paths = []
        sync_paths = []
        for voice in VOICE_ORDER:
            if voice not in sources:
                continue
            source, sync = sources[voice]
            name = f"audio/narration-higgs-{voice}-current.wav"
            target = rel_path(draft, name)
            shutil.copy2(source, target)
            audio_paths.append(name)
            if sync:
                sync_name = name[:-4] + ".sync.json"
                shutil.copy2(sync, rel_path(draft, sync_name))
                sync_paths.append(sync_name)
        assets["primary_audio"] = audio_paths[0]
        extras = [x for x in assets.get("extras", []) if not VOICE_RE.search(Path(str(x)).name)]
        assets["extras"] = extras + audio_paths[1:] + sync_paths
        revision = next_candidate_revision(story_id, story_root)
        candidate = store.calculate_candidate(story_id, {"schema_version": 1, "story_id": story_id, "candidate_revision": revision, "assets": assets})
        atomic_write_json(store.candidate_path(story_id), candidate)
        previous = store.load_ai_review(story_id)
        checks = [
            c
            for c in previous.get("checks", [])
            if isinstance(c, dict) and c.get("id") not in {"multivoice_audio", "audio_asr_all_tracks"}
        ]
        checks.append({"id": "multivoice_audio", "label": "当前版本已补入可切换旁白音轨", "status": "passed", "audience": "internal", "voices": [VOICE_RE.search(Path(p).name).group(1) for p in audio_paths]})
        asr_check = {"id": "audio_asr_all_tracks", "label": "全部音轨 ASR 未发现情绪控制标签被朗读", "status": "passed" if args.asr_all_tracks_passed else "pending", "audience": "internal"}
        if not args.asr_all_tracks_passed:
            asr_check["note"] = "本机 FunASR 服务未启动，需恢复 wss://127.0.0.1:10095 后复核。"
        checks.append(asr_check)
        summary = "保留当前文字与画面，仅补入采用修正版渲染器生成且已通过 ASR 防泄漏检查的可切换旁白音轨。" if args.asr_all_tracks_passed else "保留当前文字与画面，仅补入采用修正版渲染器生成的可切换旁白音轨；ASR 听感复核待 FunASR 服务恢复。"
        atomic_write_json(store.ai_review_path(story_id), {**previous, "result": "passed", "candidate_revision": revision, "package_digest": candidate["package_digest"], "reviewed_at": utc_now(), "summary": summary, "checks": checks})
        result = {"story_id": story_id, "candidate_revision": revision, "voices": len(audio_paths)}
        if args.publish:
            published = store.decide(story_id, decision="approved", candidate_revision=revision, package_digest=candidate["package_digest"], request_id=f"publish-{revision}", note="在当前最新版本基础上补入多声音旁白。", actor="local-development-workbench")
            result["release_id"] = published["event"].get("release_id", revision)
        results.append(result)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
