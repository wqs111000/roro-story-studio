from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
from PIL import Image
from review_store import ReviewStore
from story_server import StoryHandler


class ThumbnailTests(unittest.TestCase):
    def test_same_stem_sources_and_parallel_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ReviewStore(root)
            store.shares_path = lambda: root / "runtime" / "story-shares.json"
            def request(folder):
                handler = object.__new__(StoryHandler)
                handler.workspace_root = root
                handler.review_store = store
                asset = root / "drafts" / "story-a" / folder / "cover.png"
                route = f"/studio-thumbnails/story-a/{folder}/cover.png"
                handler.path = route + "?v=" + handler._asset_version(asset)
                handler.send_error = Mock()
                handler._send_stream_file = Mock()
                handler._send_studio_thumbnail(route)
                handler.send_error.assert_not_called()
                return handler._send_stream_file.call_args.args[0]
            for folder, color in (("one", "red"), ("two", "blue")):
                asset = root / "drafts" / "story-a" / folder / "cover.png"
                asset.parent.mkdir(parents=True)
                Image.new("RGB", (640, 640), color).save(asset)
                os.utime(asset, ns=(1000000000, 1000000000))
            with ThreadPoolExecutor(max_workers=4) as pool:
                paths = list(pool.map(request, ["one", "one", "two", "two"]))
            self.assertEqual(paths[0], paths[1])
            self.assertNotEqual(paths[0], paths[2])
            for path in set(paths):
                with Image.open(path) as image:
                    self.assertEqual(image.size, (480, 480))
            self.assertEqual(list((root / "runtime").rglob("*.tmp")), [])
