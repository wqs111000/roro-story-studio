#!/usr/bin/env python3
"""Re-key existing candidates whose immutable release id was already used.

This does not publish anything and does not touch approved releases.  It only
recalculates the candidate manifest and digest under a fresh revision so the
parent can review and explicitly publish it again.
"""
from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
from review_store import ReviewStore, atomic_write_json, utc_now  # noqa: E402


def next_revision(story_id: str, store: ReviewStore) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = re.sub(r"[^a-z0-9]+", "-", story_id.lower()).strip("-")[-12:]
    base = f"roro-{stamp}-reissue-{suffix}"
    revision = f"{base}-{uuid.uuid4().hex[:6]}"
    while any(p.name == revision for p in (store.approved_root / story_id / "releases").glob("*")):
        revision = f"{base}-{uuid.uuid4().hex[:6]}"
    return revision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("story_ids", nargs="+", help="Story ids to re-key")
    args = parser.parse_args()
    store = ReviewStore(ROOT)
    results = []
    for story_id in args.story_ids:
        candidate_path = store.candidate_path(story_id)
        candidate = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
        old_revision = str(candidate.get("candidate_revision", ""))
        revision = next_revision(story_id, store)
        recalculated = store.calculate_candidate(
            story_id,
            {
                "schema_version": candidate.get("schema_version", 1),
                "story_id": story_id,
                "candidate_revision": revision,
                "assets": candidate["assets"],
            },
        )
        atomic_write_json(candidate_path, recalculated)
        ai_path = store.ai_review_path(story_id)
        ai_review = store.load_ai_review(story_id)
        ai_review.update(
            {
                "candidate_revision": revision,
                "package_digest": recalculated["package_digest"],
                "reviewed_at": utc_now(),
                "summary": "为避免覆盖已有不可变发布版本，已将同一候选内容重新绑定到唯一版本号。",
            }
        )
        atomic_write_json(ai_path, ai_review)
        results.append({"story_id": story_id, "old_revision": old_revision, "candidate_revision": revision, "package_digest": recalculated["package_digest"]})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
