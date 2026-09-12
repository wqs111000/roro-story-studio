import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
from story_server import StoryHandler


class MigrationProductionTimesTests(unittest.TestCase):
    def test_same_revision_name_in_different_books_has_distinct_stable_times(self):
        with tempfile.TemporaryDirectory() as temporary:
            handler = object.__new__(StoryHandler)
            handler.approved_root = Path(temporary)
            first = handler.approved_root / "book-one/releases/same-revision"
            second = handler.approved_root / "book-two/releases/same-revision"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            handler._read_json = Mock(return_value={"stories": [
                {"id": "same-revision", "production_time": "legacy"},
                {"id": "book-one/releases/same-revision", "production_time": "2026-08-30T12:00:00+00:00"},
                {"id": "book-two/releases/same-revision", "production_time": "2026-09-01T12:00:00+00:00"},
            ]})
            self.assertEqual(handler._story_production_time(first), "2026-08-30T12:00:00+00:00")
            self.assertEqual(handler._story_production_time(second), "2026-09-01T12:00:00+00:00")
            handler._read_json.return_value = {"stories": [{"id": "same-revision", "production_time": "legacy"}]}
            self.assertEqual(handler._story_production_time(first), "legacy")
