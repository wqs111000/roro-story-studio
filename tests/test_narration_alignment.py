from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from narration_alignment import alignment_issues, narration_pages, page_number, spoken_segment_text  # noqa: E402


class NarrationAlignmentTests(unittest.TestCase):
    def test_parent_visible_spoken_line_matches_tts_speaker_cue(self):
        segment = {"speaker": "佑佑", "speech_cue": "佑佑笑着说", "text": "开心！"}

        self.assertEqual(spoken_segment_text(segment), "佑佑笑着说：“开心！”")

    def test_alignment_rejects_cover_title_and_repeated_dialogue(self):
        story = {
            "title": "测试故事",
            "pages": [
                {
                    "page": 1,
                    "narration": "佑佑说：“你好！”",
                    "dialogue": [{"speaker": "佑佑", "text": "你好！"}],
                }
            ],
        }
        narration = {
            "segments": [
                {"page": 1, "speaker": "旁白", "text": "《测试故事》。"},
                {"page": 1, "speaker": "旁白", "text": "佑佑说：“你好！”"},
                {"page": 1, "speaker": "佑佑", "text": "你好！"},
            ]
        }

        issues = alignment_issues(story, narration)

        self.assertTrue(any("封面书名" in issue for issue in issues))
        self.assertTrue(any("朗读时会重复" in issue for issue in issues))

    def test_malformed_page_numbers_are_reported_without_crashing(self):
        story = {"pages": [{"page": "oops", "narration": "你好"}]}
        narration = {"segments": [{"page": "oops", "text": "你好"}]}
        issues = alignment_issues(story, narration)
        self.assertTrue(any("页码格式不正确" in issue for issue in issues))
        self.assertIsNone(page_number("oops"))

    def test_public_fixture_is_fully_aligned(self):
        """Keep CI independent from ignored family drafts and generated media."""
        story = {
            "title": "公开测试故事",
            "pages": [
                {
                    "page": 1,
                    "narration": "小熊走进教室。",
                    "dialogue": [{"speaker": "小熊", "text": "大家早上好！"}],
                },
                {
                    "page": 2,
                    "narration": "小熊和朋友一起整理桌面。",
                    "dialogue": [{"speaker": "朋友", "text": "我们一起做吧！"}],
                },
            ],
        }
        narration = {
            "segments": [
                {"page": 1, "speaker": "旁白", "text": "小熊走进教室。"},
                {"page": 1, "speaker": "小熊", "speech_cue": "小熊笑着说", "text": "大家早上好！"},
                {"page": 2, "speaker": "旁白", "text": "小熊和朋友一起整理桌面。"},
                {"page": 2, "speaker": "朋友", "speech_cue": "朋友说", "text": "我们一起做吧！"},
            ]
        }

        pages = narration_pages(story, narration)

        self.assertEqual(alignment_issues(story, narration), [])
        self.assertTrue(all(page["spoken_lines"] for page in pages.values()))
        self.assertFalse(any(line["text"].startswith("《") for page in pages.values() for line in page["spoken_lines"]))


if __name__ == "__main__":
    unittest.main()
