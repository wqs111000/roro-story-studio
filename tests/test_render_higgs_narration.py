from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render-higgs-narration.py"
SPEC = importlib.util.spec_from_file_location("render_higgs_narration", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RenderHiggsNarrationTests(unittest.TestCase):
    @staticmethod
    def wav_bytes(frames: int, sample_rate: int = 8000) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(sample_rate)
            target.writeframes(b"\x01\x00" * frames)
        return output.getvalue()

    def test_story_emotions_are_mapped_to_supported_higgs_tokens(self):
        narration = {
            "segments": [
                {"emotion": "curious", "text": "第一句。"},
                {"emotion": "concerned", "text": "第二句。"},
                {"emotion": "gentle", "text": "第三句。"},
                {"emotion": "happy", "text": "第四句。"},
                {"emotion": "proud", "text": "第五句。"},
                {"emotion": "calm", "text": "第六句。"},
                {"emotion": "sleepy", "text": "第七句。"},
            ]
        }

        rendered = MODULE.narration_text(narration)

        self.assertEqual(
            rendered.splitlines(),
            [
                "<|emotion:contemplation|>第一句。",
                "<|emotion:contemplation|>第二句。",
                "<|emotion:affection|>第三句。",
                "<|emotion:elation|>第四句。",
                "<|emotion:pride|>第五句。",
                "<|emotion:contentment|>第六句。",
                "<|emotion:contentment|>第七句。",
            ],
        )

    def test_unknown_emotion_never_leaks_into_tts_input(self):
        rendered = MODULE.narration_text(
            {"segments": [{"emotion": "mystery-business-label", "text": "正文。"}]}
        )

        self.assertEqual(rendered, "<|emotion:contentment|>正文。")
        self.assertNotIn("mystery-business-label", rendered)

    def test_dialogue_has_an_audible_default_speaker_cue(self):
        rendered = MODULE.narration_text(
            {
                "segments": [
                    {"speaker": "佑佑", "emotion": "gentle", "text": "也许，它在等一封信。"},
                    {"speaker": "大家", "emotion": "happy", "text": "我们一起试试看。"},
                ]
            }
        )

        self.assertEqual(
            rendered.splitlines(),
            [
                "<|emotion:affection|>佑佑说：“也许，它在等一封信。”",
                "<|emotion:elation|>大家一起说：“我们一起试试看。”",
            ],
        )

    def test_dialogue_can_use_a_natural_story_specific_speech_cue(self):
        rendered = MODULE.narration_text(
            {
                "segments": [
                    {
                        "speaker": "暮暮",
                        "speech_cue": "暮暮望着云梯，轻声提醒",
                        "emotion": "gentle",
                        "text": "每一步都要放稳。",
                    }
                ]
            }
        )

        self.assertEqual(
            rendered,
            "<|emotion:affection|>暮暮望着云梯，轻声提醒：“每一步都要放稳。”",
        )

    def test_page_narration_text_keeps_page_boundaries_exact(self):
        narration = {
            "segments": [
                {"id": "p1", "page": 1, "speaker": "旁白", "emotion": "gentle", "text": "第一页。"},
                {"id": "p2", "page": 2, "speaker": "佑佑", "emotion": "happy", "text": "第二页。"},
            ]
        }

        rendered = MODULE.page_narration_text(narration, 2)

        self.assertEqual(rendered, "<|emotion:elation|>佑佑说：“第二页。”")
        self.assertNotIn("第一页", rendered)

    def test_page_narration_text_deduplicates_adjacent_emotion_controls(self):
        narration = {
            "segments": [
                {"page": 1, "speaker": "旁白", "emotion": "curious", "text": "看见一封信。"},
                {"page": 1, "speaker": "佑佑", "emotion": "curious", "text": "打开看看。"},
            ]
        }

        rendered = MODULE.page_narration_text(narration, 1)

        self.assertEqual(rendered.count("<|emotion:contemplation|>"), 1)
        self.assertIn("佑佑说", rendered)

    def test_stable_emotion_fallback_preserves_spoken_copy(self):
        source = "<|emotion:contemplation|>旁白。\n<|emotion:pride|>佑佑说：“好呀。”"

        rendered = MODULE.stable_emotion_fallback_text(source)

        self.assertEqual(rendered.count("<|emotion:"), 1)
        self.assertTrue(rendered.startswith("<|emotion:contentment|>"))
        self.assertIn("佑佑说：“好呀。”", rendered)

    def test_cover_title_text_is_short_and_does_not_duplicate_book_marks(self):
        self.assertEqual(
            MODULE.cover_title_text("《会打喷嚏的小老虎》。"),
            "<|emotion:contentment|>《会打喷嚏的小老虎》。",
        )

    def test_new_audio_is_blocked_when_story_and_narration_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            story_dir = Path(temporary)
            (story_dir / "story.json").write_text(
                json.dumps({"title": "测试", "pages": [{"page": 1, "narration": "页面正文。", "dialogue": []}]}, ensure_ascii=False),
                encoding="utf-8",
            )
            narration = {
                "audio_rendered": False,
                "segments": [{"page": 1, "speaker": "旁白", "text": "另一份正文。"}],
            }

            with self.assertRaisesRegex(ValueError, "旁白稿与故事正文不一致"):
                MODULE.validate_unrendered_narration(story_dir, narration)

    def test_paged_render_reads_title_on_cover_and_reuses_unchanged_segments(self):
        with tempfile.TemporaryDirectory() as temporary:
            story_dir = Path(temporary) / "test-story"
            story_dir.mkdir()
            (story_dir / "story.json").write_text(
                json.dumps({"title": "会打喷嚏的小老虎"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (story_dir / "narration.json").write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "id": "p1-01",
                                "page": 1,
                                "speaker": "旁白",
                                "emotion": "gentle",
                                "text": "第一页。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            requests: list[str] = []

            def fake_request(_opener, _base_url, _api_key, _voice, text):
                requests.append(text)
                return self.wav_bytes(800 if "《" in text else 1600)

            with patch.object(MODULE, "request_speech", side_effect=fake_request):
                MODULE.render_paged(
                    object(), "http://tts", "key", story_dir, "lady", False, "v1", 500, 0, set()
                )
                MODULE.render_paged(
                    object(), "http://tts", "key", story_dir, "lady", False, "v1", 500, 0, set()
                )

            # The second call rebuilds locally but reuses both cached TTS segments.
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0], "<|emotion:contentment|>《会打喷嚏的小老虎》。")
            sync = json.loads(
                (story_dir / "audio" / "narration-higgs-lady-kiroro-v1.sync.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(sync["cover"]["title"], "会打喷嚏的小老虎")
            self.assertAlmostEqual(sync["cover"]["duration_seconds"], 0.1, places=3)
            self.assertAlmostEqual(sync["cover_gap_seconds"], 0.5, places=3)
            self.assertAlmostEqual(sync["pages"][0]["start_seconds"], 0.6, places=3)

    def test_changed_page_text_invalidates_only_that_cached_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            audio_path = Path(temporary) / "page-01.wav"
            requests: list[str] = []

            def fake_request(_opener, _base_url, _api_key, _voice, text):
                requests.append(text)
                return self.wav_bytes(400)

            with patch.object(MODULE, "request_speech", side_effect=fake_request):
                MODULE.cached_speech(object(), "http://tts", "key", "lady", "旧文本", audio_path, False)
                MODULE.cached_speech(object(), "http://tts", "key", "lady", "旧文本", audio_path, False)
                MODULE.cached_speech(object(), "http://tts", "key", "lady", "新文本", audio_path, False)

            self.assertEqual(requests, ["旧文本", "新文本"])
            self.assertEqual(audio_path.with_suffix(".txt").read_text(encoding="utf-8"), "新文本")


if __name__ == "__main__":
    unittest.main()
