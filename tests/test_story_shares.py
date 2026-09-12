from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from review_store import ReviewError, ReviewStore, atomic_write_json  # noqa: E402


class StoryShareTests(unittest.TestCase):
    def _store(self, workspace: Path) -> ReviewStore:
        approved = workspace / "approved" / "story-a" / "releases" / "v1"
        approved.mkdir(parents=True)
        (approved / "story.json").write_text("{}", encoding="utf-8")
        atomic_write_json(workspace / "approved" / "story-a" / "current.json", {
            "release_id": "v1", "package_digest": "sha256:abc"
        })
        store = ReviewStore(workspace)
        atomic_write_json(store.shelf_status_path(), {"story-a": {"status": "published", "release_id": "v1"}})
        return store

    def test_share_is_bound_to_current_release_and_token_is_not_listed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            created = store.create_share("story-a", expires_in_days=7, allow_download=False, actor="test")
            self.assertTrue(created["token"])
            self.assertNotIn("token_hash", created)
            resolved = store.resolve_share(created["token"])
            self.assertEqual(resolved["release_id"], "v1")
            self.assertNotIn("token_hash", store.list_shares("story-a")[0])

    def test_revoke_and_shelf_unpublish_make_share_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            created = store.create_share("story-a", expires_in_days=7, allow_download=False, actor="test")
            store.revoke_share("story-a", created["share_id"], actor="test")
            with self.assertRaisesRegex(ReviewError, "无效或已失效"):
                store.resolve_share(created["token"])

            created = store.create_share("story-a", expires_in_days=7, allow_download=False, actor="test")
            store.set_shelf_status("story-a", status="unpublished", reason="测试下架", release_id="v1", actor="test")
            with self.assertRaisesRegex(ReviewError, "暂时不能分享"):
                store.resolve_share(created["token"])

    def test_invalid_expiry_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            with self.assertRaisesRegex(ReviewError, "有效期"):
                store.create_share("story-a", expires_in_days=1, allow_download=False, actor="test")

    def test_release_change_invalidates_share(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            share = store.create_share("story-a", expires_in_days=7, allow_download=False, actor="test")
            atomic_write_json(store.approved_root / "story-a" / "current.json", {
                "release_id": "v2", "package_digest": "sha256:new"
            })
            with self.assertRaises(ReviewError) as caught:
                store.resolve_share(share["token"])
            self.assertEqual(caught.exception.code, "share_unavailable")

    def test_invalid_expiration_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            share = store.create_share("story-a", expires_in_days=7, allow_download=False, actor="test")
            document = store._share_document()
            document["shares"][0]["expires_at"] = "2020-01-01T00:00:00"
            atomic_write_json(store.shares_path(), document)
            self.assertEqual(store.list_shares("story-a")[0]["status"], "expired")
            with self.assertRaises(ReviewError) as caught:
                store.resolve_share(share["token"])
            self.assertEqual(caught.exception.code, "share_expired")

    def test_share_controls_are_present_in_workbench_and_player(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")
        player = (ROOT / "service" / "ui" / "player.html").read_text(encoding="utf-8")
        self.assertNotIn("share-action", workbench)
        self.assertNotIn("share-modal", workbench)
        self.assertIn("share-action", (ROOT / "service" / "ui" / "library.html").read_text(encoding="utf-8"))
        library = (ROOT / "service" / "ui" / "library.html").read_text(encoding="utf-8")
        self.assertIn("/api/studio/stories/${encodeURIComponent(pendingStoryId)}/shares", library)
        self.assertIn("font: inherit; line-height: 1.2", library)
        self.assertIn("/api/shares/${encodeURIComponent(shareToken)}", player)
        self.assertIn("家庭分享 · 只读绘本", player)


if __name__ == "__main__":
    unittest.main()
