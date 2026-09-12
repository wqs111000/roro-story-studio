#!/usr/bin/env python3
"""Safely push a digest-bound story candidate to a NAS data directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "service"))

from review_store import (  # noqa: E402
    ReviewError,
    ReviewStore,
    flatten_assets,
    normalize_relative_path,
    sha256_file,
)


class SyncError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json_strict(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SyncError(f"Cannot read valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise SyncError(f"Expected a JSON object: {path}")
    return value


def normalized_assets(assets: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(assets, ensure_ascii=False))
    normalized["story"] = normalize_relative_path(str(assets["story"]))
    normalized["storyboard"] = normalize_relative_path(str(assets["storyboard"]))
    normalized["narration"] = normalize_relative_path(str(assets["narration"]))
    normalized["cover"] = normalize_relative_path(str(assets["cover"]))
    normalized["pages"] = [normalize_relative_path(str(item)) for item in assets["pages"]]
    normalized["primary_audio"] = normalize_relative_path(str(assets["primary_audio"]))
    normalized["extras"] = [normalize_relative_path(str(item)) for item in assets.get("extras", [])]
    return normalized


def bundle_asset_path(bundle: Path, relative: str) -> tuple[str, Path]:
    normalized = normalize_relative_path(relative)
    target = (bundle / Path(*PurePosixPath(normalized).parts)).resolve()
    if bundle.resolve() not in target.parents or not target.is_file() or target.is_symlink():
        raise SyncError(f"Missing or unsafe bundle asset: {normalized}")
    return normalized, target


def verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle = bundle.resolve()
    candidate = read_json_strict(bundle / "review" / "candidate-manifest.json")
    story_id = str(candidate.get("story_id", ""))
    ReviewStore.validate_story_id(story_id)
    revision = str(candidate.get("candidate_revision", ""))
    if not revision or not all(character.isalnum() or character in "._-" for character in revision):
        raise SyncError("Invalid candidate revision")
    assets = candidate.get("assets")
    if not isinstance(assets, dict):
        raise SyncError("Candidate assets are missing")

    normalized = normalized_assets(assets)
    expected_paths = [normalize_relative_path(item) for item in flatten_assets(assets)]
    stored_hashes = candidate.get("asset_hashes")
    if not isinstance(stored_hashes, dict) or set(stored_hashes) != set(expected_paths):
        raise SyncError("Candidate asset hash list does not match the manifest")

    actual_hashes: dict[str, str] = {}
    for relative in expected_paths:
        normalized_path, source = bundle_asset_path(bundle, relative)
        actual = sha256_file(source)
        if actual != stored_hashes[normalized_path]:
            raise SyncError(f"Bundle hash mismatch: {normalized_path}")
        actual_hashes[normalized_path] = actual

    digest_payload = {
        "schema_version": 1,
        "story_id": story_id,
        "candidate_revision": revision,
        "assets": normalized,
        "asset_hashes": dict(sorted(actual_hashes.items())),
    }
    digest_bytes = json.dumps(
        digest_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    package_digest = f"sha256:{hashlib.sha256(digest_bytes).hexdigest()}"
    if candidate.get("package_digest") != package_digest:
        raise SyncError("Bundle package digest does not match its assets")

    ai_review = read_json_strict(bundle / "review" / "ai-review.json")
    if ai_review.get("result") != "passed" or ai_review.get("package_digest") != package_digest:
        raise SyncError("Candidate is not bound to a passing AI review")
    if ai_review.get("candidate_revision") != revision:
        raise SyncError("AI review revision does not match the candidate")
    return {**digest_payload, "package_digest": package_digest}


def copy_file_verified(source: Path, target: Path, expected_hash: str | None = None) -> None:
    if source.is_symlink() or not source.is_file():
        raise SyncError(f"Unsafe source file: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.partial")
    try:
        shutil.copy2(source, temporary)
        if expected_hash and sha256_file(temporary) != expected_hash:
            raise SyncError(f"Copied file failed verification: {target}")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_bundle(local_workspace: Path, story_id: str, incoming_root: Path) -> Path:
    store = ReviewStore(local_workspace)
    try:
        candidate = store.verify_candidate(story_id)
    except ReviewError as error:
        raise SyncError(str(error)) from error
    ai_review = store.load_ai_review(story_id)
    if ai_review.get("result") != "passed" or ai_review.get("package_digest") != candidate["package_digest"]:
        raise SyncError("Local candidate is not bound to a passing AI review")

    story_incoming = incoming_root.resolve() / story_id
    story_incoming.mkdir(parents=True, exist_ok=True)
    revision = candidate["candidate_revision"]
    ready = story_incoming / f"{revision}.ready"
    if ready.exists():
        existing = verify_bundle(ready)
        if existing["package_digest"] != candidate["package_digest"]:
            raise SyncError(f"A different ready bundle already uses revision {revision}")
        return ready

    partial = story_incoming / f".{revision}.{uuid.uuid4().hex}.partial"
    partial.mkdir(parents=False, exist_ok=False)
    try:
        draft = store.draft_directory(story_id)
        for relative, expected_hash in candidate["asset_hashes"].items():
            source = draft / Path(*PurePosixPath(relative).parts)
            target = partial / Path(*PurePosixPath(relative).parts)
            copy_file_verified(source, target, expected_hash)
        review = partial / "review"
        copy_file_verified(store.ai_review_path(story_id), review / "ai-review.json")
        copy_file_verified(store.candidate_path(story_id), review / "candidate-manifest.json")
        (partial / "bundle.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "story_id": story_id,
                    "candidate_revision": revision,
                    "package_digest": candidate["package_digest"],
                    "exported_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        verify_bundle(partial)
        partial.replace(ready)
    finally:
        if partial.exists():
            shutil.rmtree(partial, ignore_errors=True)
    return ready


def import_bundle(bundle: Path, nas_data_root: Path) -> dict[str, Any]:
    candidate = verify_bundle(bundle)
    story_id = candidate["story_id"]
    active = nas_data_root.resolve() / "drafts" / story_id
    active.mkdir(parents=True, exist_ok=True)

    for relative, expected_hash in candidate["asset_hashes"].items():
        source = bundle / Path(*PurePosixPath(relative).parts)
        target = active / Path(*PurePosixPath(relative).parts)
        copy_file_verified(source, target, expected_hash)

    active_review = active / "review"
    active_review.mkdir(parents=True, exist_ok=True)
    copy_file_verified(bundle / "review" / "ai-review.json", active_review / "ai-review.json")
    # The manifest is deliberately last. A network interruption can only leave
    # the previous candidate stale; it cannot expose a partially copied package.
    copy_file_verified(
        bundle / "review" / "candidate-manifest.json",
        active_review / "candidate-manifest.json",
    )
    receipt = {
        "schema_version": 1,
        "story_id": story_id,
        "candidate_revision": candidate["candidate_revision"],
        "package_digest": candidate["package_digest"],
        "imported_at": utc_now(),
    }
    receipt_path = bundle / "import-receipt.json"
    temporary = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(receipt_path)
    return receipt


def push_candidate(local_workspace: Path, nas_data_root: Path, story_id: str) -> dict[str, Any]:
    nas_data_root = nas_data_root.resolve()
    for name in ("incoming", "drafts", "approved", "exports"):
        (nas_data_root / name).mkdir(parents=True, exist_ok=True)
    bundle = build_bundle(local_workspace.resolve(), story_id, nas_data_root / "incoming")
    return import_bundle(bundle, nas_data_root)


def pull_feedback(local_workspace: Path, nas_data_root: Path, story_id: str) -> Path:
    ReviewStore.validate_story_id(story_id)
    source = nas_data_root.resolve() / "drafts" / story_id / "review" / "parent-review.json"
    if not source.is_file() or source.is_symlink():
        raise SyncError("NAS does not have parent feedback for this story")
    destination = local_workspace.resolve() / "drafts" / story_id / "review" / "parent-review.nas.json"
    copy_file_verified(source, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    push = subparsers.add_parser("push", help="Verify, stage, and import one candidate")
    push.add_argument("story_id")
    push.add_argument("--workspace", type=Path, default=PROJECT_ROOT)
    push.add_argument("--nas-data-root", type=Path, required=True)

    pull = subparsers.add_parser("pull-feedback", help="Copy NAS feedback without overwriting local review state")
    pull.add_argument("story_id")
    pull.add_argument("--workspace", type=Path, default=PROJECT_ROOT)
    pull.add_argument("--nas-data-root", type=Path, required=True)

    verify = subparsers.add_parser("verify-bundle", help="Verify a staged candidate bundle")
    verify.add_argument("bundle", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "push":
            result = push_candidate(args.workspace, args.nas_data_root, args.story_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "pull-feedback":
            print(pull_feedback(args.workspace, args.nas_data_root, args.story_id))
        else:
            print(json.dumps(verify_bundle(args.bundle), ensure_ascii=False, indent=2))
    except (OSError, ReviewError, SyncError, ValueError) as error:
        print(f"Candidate sync failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
