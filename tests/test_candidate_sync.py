from __future__ import annotations

import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))
sys.path.insert(0, str(ROOT / "scripts" / "nas"))

from candidate_sync import SyncError, push_candidate, verify_bundle  # noqa: E402
from review_store import ReviewStore, atomic_write_json  # noqa: E402


class CandidateSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.local = root / "local"
        self.nas = root / "nas-data"
        (self.local / "drafts").mkdir(parents=True)
        (self.local / "approved").mkdir()
        self.story_id = "2026-09-01-nas-sync-test"
        self.story = self.local / "drafts" / self.story_id
        (self.story / "images").mkdir(parents=True)
        (self.story / "audio").mkdir()
        atomic_write_json(
            self.story / "story.json",
            {
                "title": "同步测试",
                "audience": {"age_years": 4, "language": "zh-CN"},
                "pages": [{"page": 1, "scene": "第一页", "narration": "你好"}],
            },
        )
        atomic_write_json(self.story / "storyboard.json", {"pages": [{"page": 1}]})
        atomic_write_json(self.story / "narration.json", {"segments": [{"page": 1, "text": "你好"}]})
        (self.story / "images" / "cover-v1.png").write_bytes(b"cover")
        (self.story / "images" / "page-01-v1.png").write_bytes(b"page-one")
        with wave.open(str(self.story / "audio" / "narration.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 1600)
        self.store = ReviewStore(self.local)
        self.write_candidate("roro-sync-001")

    def tearDown(self):
        self.temporary.cleanup()

    def write_candidate(self, revision: str) -> dict:
        seed = {
            "schema_version": 1,
            "story_id": self.story_id,
            "candidate_revision": revision,
            "assets": {
                "story": "story.json",
                "storyboard": "storyboard.json",
                "narration": "narration.json",
                "cover": "images/cover-v1.png",
                "pages": ["images/page-01-v1.png"],
                "primary_audio": "audio/narration.wav",
                "extras": [],
            },
        }
        candidate = self.store.calculate_candidate(self.story_id, seed)
        atomic_write_json(self.store.candidate_path(self.story_id), candidate)
        atomic_write_json(
            self.store.ai_review_path(self.story_id),
            {
                "schema_version": 1,
                "result": "passed",
                "candidate_revision": revision,
                "package_digest": candidate["package_digest"],
                "checks": [],
            },
        )
        return candidate

    def test_push_verifies_and_imports_candidate(self):
        receipt = push_candidate(self.local, self.nas, self.story_id)

        imported = ReviewStore(self.nas).verify_candidate(self.story_id)
        ready = self.nas / "incoming" / self.story_id / "roro-sync-001.ready"
        self.assertEqual(imported["package_digest"], receipt["package_digest"])
        self.assertTrue((ready / "import-receipt.json").is_file())
        self.assertEqual(verify_bundle(ready)["package_digest"], receipt["package_digest"])

    def test_new_revision_preserves_nas_parent_review(self):
        push_candidate(self.local, self.nas, self.story_id)
        nas_store = ReviewStore(self.nas)
        parent_record = {
            "schema_version": 1,
            "current": {"decision": "changes_requested", "note": "请修改"},
            "history": [],
        }
        atomic_write_json(nas_store.parent_review_path(self.story_id), parent_record)

        (self.story / "images" / "page-01-v1.png").write_bytes(b"page-two")
        second = self.write_candidate("roro-sync-002")
        push_candidate(self.local, self.nas, self.story_id)

        self.assertEqual(nas_store.load_parent_review(self.story_id), parent_record)
        self.assertEqual(nas_store.verify_candidate(self.story_id)["package_digest"], second["package_digest"])

    def test_modified_ready_bundle_is_rejected(self):
        push_candidate(self.local, self.nas, self.story_id)
        ready = self.nas / "incoming" / self.story_id / "roro-sync-001.ready"
        (ready / "images" / "page-01-v1.png").write_bytes(b"tampered")

        with self.assertRaises(SyncError):
            verify_bundle(ready)


if __name__ == "__main__":
    unittest.main()
