from __future__ import annotations

import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from review_store import ReviewError, ReviewStore, atomic_write_json  # noqa: E402


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        (self.workspace / "drafts").mkdir()
        (self.workspace / "approved").mkdir()
        self.story_id = "2026-08-31-test-story"
        self.story = self.workspace / "drafts" / self.story_id
        (self.story / "images").mkdir(parents=True)
        (self.story / "audio").mkdir()
        atomic_write_json(
            self.story / "story.json",
            {
                "title": "测试故事",
                "audience": {"age_years": 4, "language": "zh-CN"},
                "pages": [{"page": 1, "scene": "第一页", "narration": "你好"}],
            },
        )
        atomic_write_json(self.story / "storyboard.json", {"pages": [{"page": 1}]})
        atomic_write_json(self.story / "narration.json", {"segments": [{"page": 1, "text": "你好"}]})
        (self.story / "images" / "cover-v1.png").write_bytes(b"cover")
        (self.story / "images" / "page-01-v2.png").write_bytes(b"page")
        with wave.open(str(self.story / "audio" / "narration-higgs-lady.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 1600)
        self.store = ReviewStore(self.workspace)
        seed = {
            "schema_version": 1,
            "story_id": self.story_id,
            "candidate_revision": "roro-test-001",
            "assets": {
                "story": "story.json",
                "storyboard": "storyboard.json",
                "narration": "narration.json",
                "cover": "images/cover-v1.png",
                "pages": ["images/page-01-v2.png"],
                "primary_audio": "audio/narration-higgs-lady.wav",
                "extras": [],
            },
        }
        self.candidate = self.store.calculate_candidate(self.story_id, seed)
        atomic_write_json(self.store.candidate_path(self.story_id), self.candidate)
        atomic_write_json(
            self.store.ai_review_path(self.story_id),
            {
                "schema_version": 1,
                "result": "passed",
                "candidate_revision": self.candidate["candidate_revision"],
                "package_digest": self.candidate["package_digest"],
                "checks": [],
            },
        )

    def tearDown(self):
        self.temporary.cleanup()

    def decision(self, decision: str, request_id: str):
        return self.store.decide(
            self.story_id,
            decision=decision,
            candidate_revision=self.candidate["candidate_revision"],
            package_digest=self.candidate["package_digest"],
            request_id=request_id,
            note="测试",
            issue_tags=["scene"] if decision == "changes_requested" else [],
        )

    def test_ready_reject_and_history(self):
        self.assertEqual(self.store.state(self.story_id)["state"], "ready_for_parent")
        result = self.decision("changes_requested", "request-reject-0001")
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.state(self.story_id)["state"], "changes_requested")
        record = self.store.load_parent_review(self.story_id)
        self.assertEqual(record["current"]["issue_tags"], ["scene"])

    def test_stale_candidate_blocks_decision(self):
        (self.story / "images" / "page-01-v2.png").write_bytes(b"changed")
        self.assertEqual(self.store.state(self.story_id)["state"], "stale")
        with self.assertRaises(ReviewError) as raised:
            self.decision("approved", "request-stale-0001")
        self.assertEqual(raised.exception.code, "stale_candidate")

    def test_approve_publishes_immutable_release_and_is_idempotent(self):
        result = self.decision("approved", "request-approve-0001")
        self.assertEqual(result["state"]["state"], "published")
        pointer = self.store.published_pointer(self.story_id)
        release = self.workspace / "approved" / self.story_id / "releases" / pointer["release_id"]
        self.assertTrue((release / "images" / "page-01-v2.png").is_file())
        self.assertTrue((release / "approval.json").is_file())
        self.assertEqual(json.loads((release / "package-manifest.json").read_text(encoding="utf-8"))["package_digest"], self.candidate["package_digest"])
        repeated = self.decision("approved", "request-approve-0001")
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(len(list((release.parent).iterdir())), 1)

    def test_development_approval_is_recorded_as_unverified_local_action(self):
        result = self.store.decide(
            self.story_id,
            decision="approved",
            candidate_revision=self.candidate["candidate_revision"],
            package_digest=self.candidate["package_digest"],
            request_id="request-development-approve",
            actor="local-development-workbench",
        )

        self.assertEqual(result["event"]["actor"], "local-development-workbench")
        release = self.store.resolve_published_directory(self.story_id)
        manifest = json.loads((release / "package-manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["parent_review"]["identity_verified"])
        self.assertEqual(manifest["parent_review"]["actor"], "local-development-workbench")

    def test_reject_then_approve_preserves_rejection_in_history(self):
        self.decision("changes_requested", "request-reject-0002")
        self.decision("approved", "request-approve-0002")
        record = self.store.load_parent_review(self.story_id)
        self.assertEqual(record["current"]["decision"], "approved")
        self.assertEqual(record["history"][-1]["decision"], "changes_requested")

    def test_new_revision_preserves_previous_release(self):
        self.decision("approved", "request-approve-old")
        first_pointer = self.store.published_pointer(self.story_id)
        first_release = self.store.resolve_published_directory(self.story_id)
        self.assertEqual((first_release / "images" / "page-01-v2.png").read_bytes(), b"page")

        (self.story / "images" / "page-01-v2.png").write_bytes(b"new-page")
        seed = {
            "schema_version": 1,
            "story_id": self.story_id,
            "candidate_revision": "roro-test-002",
            "assets": self.candidate["assets"],
        }
        second = self.store.calculate_candidate(self.story_id, seed)
        atomic_write_json(self.store.candidate_path(self.story_id), second)
        atomic_write_json(
            self.store.ai_review_path(self.story_id),
            {"schema_version": 1, "result": "passed", "candidate_revision": second["candidate_revision"], "package_digest": second["package_digest"], "checks": []},
        )
        self.store.decide(
            self.story_id,
            decision="approved",
            candidate_revision=second["candidate_revision"],
            package_digest=second["package_digest"],
            request_id="request-approve-new",
        )
        second_pointer = self.store.published_pointer(self.story_id)
        releases = self.workspace / "approved" / self.story_id / "releases"
        self.assertEqual(len(list(releases.iterdir())), 2)
        self.assertNotEqual(first_pointer["release_id"], second_pointer["release_id"])
        self.assertEqual((releases / first_pointer["release_id"] / "images" / "page-01-v2.png").read_bytes(), b"page")
        self.assertEqual((releases / second_pointer["release_id"] / "images" / "page-01-v2.png").read_bytes(), b"new-page")

    def test_shelf_unpublish_requires_reason_and_preserves_release(self):
        self.decision("approved", "request-shelf-approve")
        pointer = self.store.published_pointer(self.story_id)
        with self.assertRaises(ReviewError) as raised:
            self.store.set_shelf_status(self.story_id, status="unpublished", reason="", release_id=pointer["release_id"], actor="local-development-workbench")
        self.assertEqual(raised.exception.code, "missing_unpublish_reason")
        entry = self.store.set_shelf_status(self.story_id, status="unpublished", reason="需要重新整理", release_id=pointer["release_id"], actor="local-development-workbench")
        self.assertEqual(entry["status"], "unpublished")
        self.assertEqual(self.store.published_pointer(self.story_id)["release_id"], pointer["release_id"])
        self.assertEqual(self.store.state(self.story_id)["shelf_status"], "unpublished")
        restored = self.store.set_shelf_status(self.story_id, status="published", reason="", release_id=pointer["release_id"], actor="local-development-workbench")
        self.assertEqual(restored["status"], "published")

    def test_shelf_update_requires_current_release_id(self):
        self.decision("approved", "request-shelf-version-guard")
        with self.assertRaises(ReviewError) as raised:
            self.store.set_shelf_status(self.story_id, status="unpublished", reason="版本复核", release_id="", actor="local-development-workbench")
        self.assertEqual(raised.exception.code, "stale_release")


if __name__ == "__main__":
    unittest.main()
