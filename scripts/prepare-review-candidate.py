#!/usr/bin/env python3
"""Lock an explicit story candidate after Codex has completed internal review."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / "service"))

from review_store import ReviewStore, atomic_write_json  # noqa: E402


PAGE_RE = re.compile(r"page[-_]0?(\d+)(?:-v(\d+))?\.png$", re.I)
VERSION_RE = re.compile(r"(?:^|[-_])v(\d+)(?:\.[^.]+)?$", re.I)


def version_score(path: Path) -> tuple[int, float, str]:
    match = VERSION_RE.search(path.name)
    return (int(match.group(1)) if match else 0, path.stat().st_mtime, path.name.lower())


def choose_pages(image_dir: Path, page_count: int) -> list[str]:
    choices: dict[int, list[Path]] = {}
    for path in image_dir.glob("*.png"):
        match = PAGE_RE.fullmatch(path.name)
        if match:
            choices.setdefault(int(match.group(1)), []).append(path)
    selected = []
    for page_no in range(1, page_count + 1):
        candidates = choices.get(page_no, [])
        if not candidates:
            raise SystemExit(f"Missing page {page_no} in {image_dir}")
        path = max(candidates, key=version_score)
        selected.append(path.relative_to(image_dir.parent).as_posix())
    return selected


def choose_cover(image_dir: Path) -> str:
    candidates = [path for path in image_dir.glob("cover*.png") if path.is_file()]
    if not candidates:
        raise SystemExit(f"Missing cover in {image_dir}")
    return max(candidates, key=version_score).relative_to(image_dir.parent).as_posix()


def choose_audio(audio_dir: Path) -> str:
    candidates = [path for path in audio_dir.iterdir() if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg"}]
    if not candidates:
        raise SystemExit(f"Missing audio in {audio_dir}")

    def score(path: Path) -> tuple[int, float, str]:
        name = path.name.lower()
        priority = 0
        if "higgs-lady-v2" in name:
            priority = 50
        elif "higgs-lady" in name:
            priority = 40
        elif "lady" in name:
            priority = 30
        elif "roro-01" in name:
            priority = 20
        return (priority, path.stat().st_mtime, name)

    return max(candidates, key=score).relative_to(audio_dir.parent).as_posix()


def matching_audio_extras(root: Path, primary_relative: str) -> list[str]:
    """Bind the matching alternate voices and all exact timeline files."""
    primary = root / Path(primary_relative)
    tracks = [primary]
    if "-lady-" in primary.name:
        for voice in ("roro-01", "wqs"):
            sibling = primary.with_name(primary.name.replace("-lady-", f"-{voice}-", 1))
            if sibling.is_file():
                tracks.append(sibling)
    extras = [track for track in tracks if track != primary]
    extras.extend(
        sync
        for track in tracks
        if (sync := track.with_suffix(".sync.json")).is_file()
    )
    return [path.relative_to(root).as_posix() for path in extras]


def validate_evidence(candidate: dict, evidence: dict) -> None:
    required = {"content", "audible_speakers", "character", "scene", "full_bleed",
                "contact_sheet", "audio", "cover_title", "page_timeline"}
    if evidence.get("package_digest") != candidate["package_digest"]:
        raise ValueError("Internal review is not bound to the current package")
    if evidence.get("result") != "passed" or not evidence.get("reviewer") or not evidence.get("reviewed_at"):
        raise ValueError("Internal review requires a reviewer, time and explicit result")
    checks = evidence.get("checks", [])
    if not required.issubset({check.get("id") for check in checks}):
        raise ValueError("Missing visual, content or audio checks")
    if any(check.get("status") != "passed" or not check.get("evidence") for check in checks):
        raise ValueError("Every check requires a passed result and actual review evidence")


def validate_selected_images(workspace: Path, root: Path, assets: dict, visual: dict) -> None:
    """Bind reviewed images to page positions, not just an unordered file set."""
    expected = {
        page: (root / path).resolve()
        for page, path in enumerate([assets["cover"], *assets["pages"]])
    }
    rows = visual.get("pages", [])
    actual = {row["page"]: (workspace / row["image"]).resolve() for row in rows}
    if len(rows) != len(actual) or actual != expected:
        raise ValueError("Visual review must match the selected cover (page 0) and every page image in order")


def prepare(story_id: str, revision: str, summary: str, inspect_only: bool = False) -> dict:
    store = ReviewStore(WORKSPACE)
    root = store.draft_directory(story_id)
    story = json.loads((root / "story.json").read_text(encoding="utf-8"))
    page_count = len(story.get("pages", []))
    primary_audio = choose_audio(root / "audio")
    assets = {
        "story": "story.json",
        "storyboard": "storyboard.json",
        "narration": "narration.json",
        "cover": choose_cover(root / "images"),
        "pages": choose_pages(root / "images", page_count),
        "primary_audio": primary_audio,
        "extras": matching_audio_extras(root, primary_audio),
    }
    candidate_seed = {
        "schema_version": 1,
        "story_id": story_id,
        "candidate_revision": revision,
        "assets": assets,
    }
    candidate = store.calculate_candidate(story_id, candidate_seed)
    if inspect_only:
        return candidate
    # Packaging must not manufacture evidence that images or audio were reviewed.
    evidence_path = root / "internal-review.json"
    if not evidence_path.is_file():
        raise ValueError("Missing internal-review.json: perform visual/audio review first; --inspect prints the candidate binding without writing.")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    validate_evidence(candidate, evidence)
    from visual_plan import plan_from_storyboard, validate_review
    plan = plan_from_storyboard(json.loads((root / "storyboard.json").read_text(encoding="utf-8")))
    visual = json.loads((root / "visual-review.json").read_text(encoding="utf-8"))
    validate_review(WORKSPACE, plan, visual)
    validate_selected_images(WORKSPACE, root, assets, visual)
    ai_review = {
        **evidence,
        "schema_version": 1,
        "story_id": story_id,
        "candidate_revision": revision,
        "package_digest": candidate["package_digest"],
        "summary": summary,
        "checks": [{**check, "audience": "internal"} for check in evidence["checks"]],
    }
    atomic_write_json(store.candidate_path(story_id), candidate)
    atomic_write_json(store.ai_review_path(story_id), ai_review)
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("story_ids", nargs="+")
    parser.add_argument("--revision-prefix", default=datetime.now().strftime("roro-%Y%m%d"))
    parser.add_argument("--summary", default="Codex 已完成文本、角色、场景、全画幅、整本联系表、标记页原图和音频内部审核。")
    parser.add_argument("--inspect", action="store_true", help="Print selected assets and package digest without writing or passing review")
    args = parser.parse_args()
    for index, story_id in enumerate(args.story_ids, start=1):
        revision = f"{args.revision_prefix}-{index:03d}"
        candidate = prepare(story_id, revision, args.summary, inspect_only=args.inspect)
        if args.inspect:
            print(json.dumps(candidate, ensure_ascii=False, indent=2))
        print(f"{story_id}: {candidate['candidate_revision']} {candidate['package_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
