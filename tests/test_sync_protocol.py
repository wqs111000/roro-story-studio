from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from review_store import atomic_write_json  # noqa: E402
from sync_protocol import SyncError, SyncManager, build_catalog, sync_asset_url  # noqa: E402


class SyncProtocolTests(unittest.TestCase):
    def make_book(self, workspace: Path, status: str = "published", release: str = "v1", package: str = "sha256:book-v1") -> Path:
        approved = workspace / "approved"
        root = approved / "book-one"
        release_root = root / "releases" / release
        release_root.mkdir(parents=True)
        files = {
            "story.json": b'{"title":"test","pages":[{"page":1}]}',
            "storyboard.json": b"{}",
            "narration.json": b"{}",
            "images/cover.png": b"cover",
            "images/page-01.png": b"page",
            "audio/main.wav": b"audio",
        }
        for relative, content in files.items():
            path = release_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        hashes = {relative: hashlib.sha256(content).hexdigest() for relative, content in files.items()}
        manifest = {
            "schema_version": 1,
            "story_id": "book-one",
            "release_id": release,
            "title": "测试书",
            "status": "approved",
            "package_digest": package,
            "assets": {
                "story": "story.json",
                "storyboard": "storyboard.json",
                "narration": "narration.json",
                "cover": "images/cover.png",
                "pages": ["images/page-01.png"],
                "primary_audio": "audio/main.wav",
            },
            "asset_hashes": hashes,
        }
        (release_root / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        atomic_write_json(root / "current.json", {"release_id": release, "package_digest": package})
        runtime = workspace / "service" / ".runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        atomic_write_json(runtime / "shelf-status.json", {"book-one": {"status": status, "release_id": release}})
        return release_root

    def test_catalog_is_stable_and_keeps_explicit_unpublished_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self.make_book(workspace, status="unpublished")
            first = build_catalog(workspace / "approved", workspace / "service" / ".runtime")
            second = build_catalog(workspace / "approved", workspace / "service" / ".runtime")
            self.assertEqual(first["catalog_revision"], second["catalog_revision"])
            self.assertEqual(first["catalog_digest"], second["catalog_digest"])
            self.assertEqual(first["stories"][0]["status"], "unpublished")

    def test_same_release_with_different_manifest_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            release_root = self.make_book(workspace, package="sha256:pointer")
            manifest_path = release_root / "package-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["package_digest"] = "sha256:manifest"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(SyncError):
                build_catalog(workspace / "approved", workspace / "service" / ".runtime")

    def test_non_story_approved_resource_is_not_added_to_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self.make_book(workspace)
            character_root = workspace / "approved" / "roro-starlight-form"
            character_root.mkdir()
            (character_root / "character-spec.md").write_text("approved form", encoding="utf-8")
            catalog = build_catalog(workspace / "approved", workspace / "service" / ".runtime")
            self.assertEqual([entry["story_id"] for entry in catalog["stories"]], ["book-one"])

    def test_sync_manager_refuses_same_release_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            self.make_book(workspace, package="sha256:old")
            manager = SyncManager(workspace / "approved", workspace / "runtime")
            entry = build_catalog(workspace / "approved", workspace / "service" / ".runtime")["stories"][0]
            entry["package_digest"] = "sha256:new"
            with patch.object(manager, "_download", return_value=None):
                with self.assertRaises(SyncError):
                    manager._install_story(entry, "http://desktop")

    def test_asset_url_escapes_each_path_component(self):
        self.assertEqual(
            sync_asset_url("http://desktop:8877", "book-one", "v1", "images/page 01.png"),
            "http://desktop:8877/api/sync/assets/book-one/v1/images/page%2001.png",
        )


if __name__ == "__main__":
    unittest.main()
