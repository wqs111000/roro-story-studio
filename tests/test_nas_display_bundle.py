import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/nas"))
import prepare_release as display


class NasDisplayBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        release = self.workspace / "approved/book/releases/current-release"
        release.mkdir(parents=True)
        assets = {"story.json": b'{"title":"current"}', "images/page-01.png": b"page", "audio/voice.wav": b"audio"}
        hashes = {}
        for relative, content in assets.items():
            path = release / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            hashes[relative] = hashlib.sha256(content).hexdigest()
        manifest = {"status": "approved", "story_id": "book", "release_id": "current-release",
            "package_digest": "sha256:package", "asset_hashes": hashes}
        (release / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        pointer = release.parents[1] / "current.json"
        pointer.write_text(json.dumps({"release_id": "current-release", "package_digest": "sha256:package"}), encoding="utf-8")
        old = release.parent / "historical-release"
        old.mkdir()
        (old / "must-not-copy.txt").write_text("old")
        draft = self.workspace / "drafts/book"
        draft.mkdir(parents=True)
        (draft / "must-not-copy.txt").write_text("draft")
        service_data = self.workspace / "service/data"
        service_data.mkdir(parents=True)
        (self.workspace / "service/story_server.py").write_text("print('server')\n")
        scripts = self.workspace / "scripts"
        scripts.mkdir()
        (scripts / "build_picture_book.py").write_text("def build_from_assets(*args): pass\n")
        service_assets = self.workspace / "service/assets/characters"
        service_assets.mkdir(parents=True)
        (service_assets / "hero.png").write_bytes(b"hero")
        (service_data / "characters.json").write_text(json.dumps({"characters": [{"image_url": "/character-assets/hero.png"}]}))
        deploy = self.workspace / "deploy/nas"
        deploy.mkdir(parents=True)
        for name in ("compose.yaml", "preflight.sh", "START-HERE.md"):
            (deploy / name).write_text("display-only")
        (deploy / "compose.yaml").write_text(
            'services:\n  app:\n    image: "roro-story-runtime:py312-reportlab4-v1"\n'
        )
        self.image = root / "image.tar"
        self.image.write_bytes(b"image")
        self.destination = root / "nas/staging/display"
        self.visible = [{"id": "book", "title": "Current", "production_time": "2026-09-01T12:00:00Z", "audio_options": [{}, {}]}]

    def test_only_current_visible_release_and_referenced_character_are_copied(self):
        with patch.object(display, "shelf", return_value=self.visible):
            display.prepare(self.workspace, self.destination,
                "/volume1/docker/roro-story-studio/staging/display",
                "roro-story-runtime:py312-reportlab4-v1", "sha256:" + "2" * 64,
                self.image, "192.0.2.195")
        report = display.verify(self.destination)
        self.assertEqual([book["release_id"] for book in report["books"]], ["current-release"])
        self.assertTrue((self.destination / "data/approved/book/releases/current-release/audio/voice.wav").is_file())
        self.assertFalse((self.destination / "data/approved/book/releases/historical-release").exists())
        self.assertFalse((self.destination / "data/drafts").exists())
        self.assertFalse((self.destination / "runtime").exists())
        self.assertTrue((self.destination / "character-assets/hero.png").is_file())
        self.assertIn("RORO_LISTEN_ADDRESS=192.0.2.195", (self.destination / ".env").read_text())

    def test_shelf_change_during_copy_is_rejected(self):
        changed = [{**self.visible[0], "title": "Changed during copy"}]
        with patch.object(display, "shelf", side_effect=[self.visible, changed]):
            with self.assertRaisesRegex(ValueError, "Visible shelf changed"):
                display.prepare(self.workspace, self.destination,
                    "/volume1/docker/roro-story-studio/staging/display",
                    "roro-story-runtime:py312-reportlab4-v1", "sha256:" + "2" * 64,
                    self.image, "192.0.2.195")
        self.assertTrue((self.destination / "INCOMPLETE").exists())

    def test_linked_application_directory_is_rejected(self):
        (self.workspace / "service/ui").mkdir()
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path.name == "ui" or original(path)):
            with self.assertRaisesRegex(ValueError, "Linked application directory"):
                display.application_files(self.workspace)


if __name__ == "__main__": unittest.main()
