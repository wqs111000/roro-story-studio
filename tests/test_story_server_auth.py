from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

from review_store import ReviewError  # noqa: E402
from story_server import CatalogCache, ReviewAuth, StoryHandler, VOICE_DISPLAY_MAP  # noqa: E402


class ReviewAuthTests(unittest.TestCase):
    def test_display_only_blocks_workbench_routes_and_every_post(self):
        handler = object.__new__(StoryHandler)
        handler.display_only = True
        self.assertTrue(handler._display_route_blocked("/workbench"))
        self.assertTrue(handler._display_route_blocked("/api/studio/stories"))
        self.assertTrue(handler._display_route_blocked("/studio-assets/example/page.png"))
        self.assertFalse(handler._display_route_blocked("/api/stories"))
        self.assertFalse(handler._display_route_blocked("/download/example"))
        handler._send_json_error = Mock()
        handler.do_POST()
        handler._send_json_error.assert_called_once_with(
            "display_only", "此服务仅供浏览和播放，制作审核请在电脑端操作。", 403)

    def test_catalog_cache_reuses_a_catalogue_until_invalidated(self):
        cache = CatalogCache()
        calls = []

        def build():
            calls.append("build")
            return {"revision": len(calls)}

        self.assertEqual(cache.get("stories", build), {"revision": 1})
        self.assertEqual(cache.get("stories", build), {"revision": 1})
        cache.invalidate("stories")
        self.assertEqual(cache.get("stories", build), {"revision": 2})
        self.assertEqual(calls, ["build", "build"])

    def test_story_library_reuses_the_workbench_story_catalogue(self):
        handler = object.__new__(StoryHandler)
        handler.catalog_cache = CatalogCache()
        handler._studio_stories = Mock(return_value=[{"id": "draft-a", "title": "A"}])
        handler._story_library_items = Mock(return_value=[])

        handler._story_library_payload()

        handler._story_library_items.assert_called_once_with({"draft-a": {"id": "draft-a", "title": "A"}})
        self.assertEqual(handler._studio_stories.call_count, 1)

    def test_story_library_only_contains_complete_story_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            complete = workspace / "drafts" / "complete-story"
            character = workspace / "drafts" / "character-draft"
            complete.mkdir(parents=True)
            character.mkdir(parents=True)
            (complete / "story.json").write_text(json.dumps({"title": "完整故事", "theme": "合作", "source_mode": "original_adventure", "pages": [{"page": index + 1, "narration": f"第{index + 1}页"} for index in range(4)]}, ensure_ascii=False), encoding="utf-8")
            (character / "story.json").write_text(json.dumps({"title": "角色草稿", "pages": []}, ensure_ascii=False), encoding="utf-8")
            handler = object.__new__(StoryHandler)
            handler.workspace_root = workspace
            handler._studio_story_summary = Mock(return_value={"review_state": "authoring", "published": False, "asset_counts": {}, "updated_at": "2026-09-01T00:00:00Z", "cover_url": ""})

            items = handler._story_library_items()

            self.assertEqual([item["title"] for item in items], ["完整故事"])
            self.assertEqual(items[0]["library_state"], "ready")
            self.assertTrue(items[0]["selectable"])
            self.assertEqual(len(items[0]["pages"]), 4)

    def test_story_library_exposes_the_exact_parent_visible_spoken_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            story_dir = workspace / "drafts" / "spoken-story"
            story_dir.mkdir(parents=True)
            pages = [
                {"page": index + 1, "scene": f"场景 {index + 1}", "narration": f"第{index + 1}页。", "dialogue": [], "interaction": None}
                for index in range(4)
            ]
            pages[0]["dialogue"] = [{"speaker": "佑佑", "text": "你好！"}]
            story = {"title": "朗读测试", "pages": pages}
            narration = {"segments": [
                {"page": 1, "speaker": "旁白", "text": "第1页。"},
                {"page": 1, "speaker": "佑佑", "speech_cue": "佑佑笑着说", "text": "你好！"},
                *[{"page": index + 1, "speaker": "旁白", "text": f"第{index + 1}页。"} for index in range(1, 4)],
            ]}
            (story_dir / "story.json").write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
            (story_dir / "narration.json").write_text(json.dumps(narration, ensure_ascii=False), encoding="utf-8")
            handler = object.__new__(StoryHandler)
            handler.workspace_root = workspace
            handler._studio_story_summary = Mock(return_value={"review_state": "authoring", "published": False, "asset_counts": {}, "updated_at": "2026-09-01T00:00:00Z", "cover_url": ""})

            item = handler._story_library_items()[0]

            self.assertEqual(item["narration_status"], "ready")
            self.assertEqual(item["cover_spoken_text"], "《朗读测试》。")
            self.assertEqual(item["pages"][0]["spoken_lines"][1]["spoken_text"], "佑佑笑着说：“你好！”")

    def test_workbench_labels_actual_spoken_copy_in_story_and_review_views(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertIn("本页实际朗读", workbench)
        self.assertIn("实际会听到", workbench)
        self.assertIn("封面会先朗读", workbench)
        self.assertIn("renderSpokenLines", workbench)

    def test_story_library_only_exposes_six_most_recent_discarded_stories(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            drafts = workspace / "drafts"
            drafts.mkdir(parents=True)
            history = []
            for index in range(7):
                story_id = f"story-{index}"
                story_dir = drafts / story_id
                story_dir.mkdir()
                (story_dir / "story.json").write_text(json.dumps({"title": story_id, "pages": [{"page": page + 1, "narration": "正文"} for page in range(4)]}), encoding="utf-8")
                history.append({"story_id": f"draft-{story_id}", "discarded_at": f"2026-09-01T00:0{index}:00+00:00"})
            queue = workspace / "inbox" / "story-production-queue.json"
            queue.parent.mkdir(parents=True)
            queue.write_text(json.dumps({"story_ids": [], "discarded_ids": [entry["story_id"] for entry in history], "discarded_history": history}), encoding="utf-8")
            handler = object.__new__(StoryHandler)
            handler.workspace_root = workspace
            handler._studio_story_summary = Mock(return_value={"review_state": "authoring", "published": False, "asset_counts": {}, "updated_at": "2026-09-01T00:00:00Z", "cover_url": ""})

            items = handler._story_library_items()

            self.assertEqual(len(items), 6)
            self.assertNotIn("story-0", [item["story_id"] for item in items])
            self.assertEqual(items[0]["story_id"], "story-6")

    def test_story_library_discard_state_is_recoverable_and_not_selectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            story_dir = workspace / "drafts" / "complete-story"
            story_dir.mkdir(parents=True)
            (story_dir / "story.json").write_text(json.dumps({"title": "完整故事", "source_mode": "daily_life", "pages": [{"page": index + 1, "narration": f"第{index + 1}页"} for index in range(4)]}, ensure_ascii=False), encoding="utf-8")
            queue = workspace / "inbox" / "story-production-queue.json"
            queue.parent.mkdir(parents=True)
            queue.write_text(json.dumps({"story_ids": ["draft-complete-story"], "discarded_ids": ["draft-complete-story"]}), encoding="utf-8")
            handler = object.__new__(StoryHandler)
            handler.workspace_root = workspace
            handler._studio_story_summary = Mock(return_value={"review_state": "authoring", "published": False, "asset_counts": {}, "updated_at": "2026-09-01T00:00:00Z", "cover_url": ""})

            selected, discarded = handler._story_library_state()
            item = handler._story_library_items()[0]

            self.assertEqual(selected, set())
            self.assertEqual(discarded, {"draft-complete-story"})
            self.assertTrue(item["discarded"])
            self.assertEqual(item["library_state"], "discarded")
            self.assertFalse(item["selectable"])
            self.assertFalse(item["selected"])

    def test_workbench_separates_story_candidates_from_production_and_review(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertIn("备选故事库", workbench)
        self.assertIn("这里只显示尚未加入制作、也未丢弃的完整故事", workbench)
        self.assertIn('data-view="working"', workbench)
        self.assertIn('data-view="review"', workbench)
        self.assertIn('data-view="published"', workbench)
        self.assertNotIn('data-view="discarded"', workbench)
        self.assertIn("/api/studio/story-library/selection", workbench)
        self.assertIn("/api/studio/story-library/disposition", workbench)
        self.assertIn('id="story-library-modal-pages"', workbench)
        self.assertIn('trash-toggle', workbench)
        self.assertIn('data-trash-restore', workbench)
        self.assertIn("只使用家长提供的真实事件和生活素材", workbench)
        self.assertIn("母题再创作", workbench)
        self.assertIn("角色、语言和情节全部重写", workbench)
        self.assertIn("加入制作", workbench)
        self.assertIn('data-library-detail="${escapeHtml(item.id)}">查看', workbench)
        self.assertIn("正在展开故事内容…", workbench)
        self.assertIn("ensureLibraryWriteAuth", workbench)
        self.assertIn("Promise.allSettled([loadLibrary(),loadStories()])", workbench)
        self.assertIn('data-library-start="${escapeHtml(item.id)}">制作', workbench)
        self.assertIn("丢弃", workbench)
        self.assertIn('data-library-discard', workbench)
        self.assertIn('data-library-discard="${escapeHtml(item.id)}">丢弃', workbench)
        self.assertIn('data-library-restore="${escapeHtml(item.id)}">恢复', workbench)
        self.assertIn("window.refreshTrash?.()", workbench)
        self.assertNotIn("window.location.reload()", workbench)
        self.assertIn("grid-template-columns:52px minmax(0,1fr)", workbench)
        self.assertNotIn("height:112px;position:relative;display:grid;place-items:center", workbench)
        self.assertIn("!item.discarded && !item.selected", workbench)
        self.assertNotIn("item.selected = true", workbench)
        self.assertIn("ids.add(pendingSelectionId)", workbench)
        self.assertIn("story.workflow_state==='production'", workbench)
        self.assertIn("story.workflow_state==='review'", workbench)
        self.assertIn("查看当前进度和缺少的素材", workbench)
        self.assertIn("完整候选在这里完成画面与旁白审核", workbench)

    def test_frontend_does_not_expose_internal_codex_name(self):
        for path in (ROOT / "service" / "ui").glob("*.html"):
            with self.subTest(path=path.name):
                self.assertNotIn("codex", path.read_text(encoding="utf-8").lower())

    def test_local_compose_uses_configurable_listen_address(self):
        compose = (ROOT / "deploy" / "local" / "compose.yaml").read_text(encoding="utf-8")
        start_script = (ROOT / "service" / "start-story-service.ps1").read_text(encoding="utf-8")
        server = (ROOT / "service" / "story_server.py").read_text(encoding="utf-8")

        self.assertIn('${RORO_LISTEN_ADDRESS:-127.0.0.1}:${RORO_PORT:-8877}:8877', compose)
        self.assertNotIn('${RORO_LISTEN_ADDRESS:-0.0.0.0}', compose)
        self.assertIn("[string]$HostAddress = '127.0.0.1'", start_script)
        self.assertIn('parser.add_argument("--host", default="127.0.0.1")', server)

    def test_public_https_origin_allows_proxy_host_and_uses_secure_cookie(self):
        handler = object.__new__(StoryHandler)
        handler.public_origin = "https://roro.example.iepose.cn"
        handler.headers = {
            "Origin": "https://roro.example.iepose.cn",
            "Host": "127.0.0.1:8877",
        }

        handler._require_same_origin()

        self.assertIn("; Secure", handler._review_cookie("session", 3600))

    def test_public_origin_rejects_a_different_site(self):
        handler = object.__new__(StoryHandler)
        handler.public_origin = "https://roro.example.iepose.cn"
        handler.headers = {
            "Origin": "https://attacker.example",
            "Host": "127.0.0.1:8877",
        }

        with self.assertRaises(ReviewError) as raised:
            handler._require_same_origin()

        self.assertEqual(raised.exception.code, "invalid_origin")

    def test_lan_origin_remains_usable_when_public_origin_is_configured(self):
        handler = object.__new__(StoryHandler)
        handler.public_origin = "https://roro.example.iepose.cn"
        handler.headers = {
            "Origin": "http://192.0.2.20:8877",
            "Host": "192.0.2.20:8877",
        }

        handler._require_same_origin()

        self.assertNotIn("; Secure", handler._review_cookie("session", 3600))

    def test_candidate_preview_and_published_story_serve_the_same_player(self):
        candidate_handler = object.__new__(StoryHandler)
        candidate_handler.path = "/studio-preview/2026-08-30-test-story"
        candidate_handler._studio_preview_detail = Mock(return_value={"id": "2026-08-30-test-story"})
        candidate_handler._send_ui = Mock()

        published_handler = object.__new__(StoryHandler)
        published_handler.path = "/player/2026-08-30-test-story"
        published_handler._approved_story_detail = Mock(return_value={"id": "2026-08-30-test-story"})
        published_handler._send_ui = Mock()

        candidate_handler.do_GET()
        published_handler.do_GET()

        candidate_handler._send_ui.assert_called_once_with("player.html")
        published_handler._send_ui.assert_called_once_with("player.html")

    def test_workbench_preview_uses_candidate_player_route(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")
        player = (ROOT / "service" / "ui" / "player.html").read_text(encoding="utf-8")

        self.assertIn('href="/studio-preview/${encodeURIComponent(s.id)}">预览当前候选</a>', workbench)
        self.assertIn("location.pathname.startsWith('/studio-preview/')", player)
        self.assertIn("`/api/studio/preview/${encodeURIComponent(storyId)}`", player)
        self.assertIn("option.page_timeline", player)
        self.assertIn("pageProgress", player)
        self.assertIn("slide.end = Number(exactTimeline.get(1)?.start_seconds) || 0", player)
        self.assertNotIn("const PAGE_LEAD_SECONDS = 0.3", player)

    def test_workbench_hides_codex_internal_review_evidence(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertNotIn('id="ai-summary"', workbench)
        self.assertNotIn('id="checks"', workbench)
        self.assertNotIn("internalCheckIds", workbench)
        self.assertNotIn("Codex 内部审核", workbench)
        self.assertNotIn("内部审核通过", workbench)
        self.assertNotIn("内部处理中", workbench)
        self.assertNotIn("业务情绪已映射为 Higgs 合法标签", workbench)
        self.assertNotIn("候选内全部音轨 ASR 审核为故事正文", workbench)

    def test_workbench_voice_choices_use_fixed_three_column_layout(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertIn(".audio-voices{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))", workbench)
        self.assertIn('id="audio-count"', workbench)
        self.assertIn("点击切换", workbench)
        self.assertNotIn("左右滑动切换", workbench)
        self.assertNotIn(".audio-voices{display:flex", workbench)
        self.assertNotIn("scroll-snap-type:x", workbench)

    def test_workbench_card_actions_are_two_aligned_short_labels(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr)) auto", workbench)
        self.assertIn(".library-card-actions{grid-template-columns:repeat(3,minmax(0,1fr))}", workbench)
        self.assertIn('data-library-start="${escapeHtml(item.id)}">制作', workbench)
        self.assertIn('data-library-discard="${escapeHtml(item.id)}">丢弃', workbench)
        self.assertIn(".card-footer .button{width:100%;min-width:0;white-space:nowrap}", workbench)
        self.assertIn("state.filter==='review'?'开始审核':'查看进度'", workbench)
        self.assertIn('打开书架版本', workbench)
        self.assertIn('>预览当前候选</a>', workbench)
        self.assertIn('新版待审核', workbench)
        self.assertNotIn('书架已有旧版', workbench)

    def test_workbench_review_controls_have_regression_guards(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertIn('id="audio-seek" class="audio-range" type="range"', workbench)
        self.assertIn("$('audio-seek').addEventListener('input'", workbench)
        self.assertIn("$('audio-seek').addEventListener('keydown',seekReviewAudioByKey)", workbench)
        self.assertIn("$('audio-seek').addEventListener('pointerdown'", workbench)
        self.assertIn("$('audio').pause();$('review-view').classList.remove('open')", workbench)
        self.assertIn("state.stories.filter(s=>s.published===true&&s.shelf_status==='published').length", workbench)
        self.assertIn("if(state.filter==='published')return story.published===true", workbench)
        self.assertIn('id="review-preview"', workbench)
        self.assertIn('id="image-modal"', workbench)
        self.assertIn("请至少选择一个问题类型", workbench)
        self.assertIn("[hidden]{display:none!important}", workbench)
        self.assertIn("function makeRequestId()", workbench)
        self.assertIn("request_id:makeRequestId()", workbench)
        self.assertNotIn("request_id:crypto.randomUUID()", workbench)

    def test_workbench_published_count_uses_shelf_state(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertIn("$('metric-published').textContent=state.stories.filter(s=>s.published===true&&s.shelf_status==='published').length", workbench)
        self.assertIn("$('metric-candidates').textContent = libraryState.items.filter(item => !item.discarded && !item.selected).length", workbench)
        self.assertIn("$('metric-unpublished').textContent=state.stories.filter(s=>s.published===true&&s.shelf_status==='unpublished').length", workbench)
        self.assertIn("if(state.filter==='published')return story.published===true", workbench)

    def test_workbench_uses_compact_status_navigation_and_return_paths(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")
        player = (ROOT / "service" / "ui" / "player.html").read_text(encoding="utf-8")

        self.assertIn('data-view="review" aria-pressed="false">待审核', workbench)
        self.assertIn('data-view="working" aria-pressed="false">制作中', workbench)
        self.assertIn('data-view="published" aria-pressed="false">已上架', workbench)
        self.assertIn("ready_for_parent:['待审核','review']", workbench)
        self.assertIn("changes_requested:['需修改','returned']", workbench)
        self.assertIn("window.showWorkbenchView", workbench)
        self.assertIn("location.hash = selectedView", workbench)
        self.assertIn("← 返回${{candidates:'备选',working:'制作中',review:'待审核',published:'已上架'}", workbench)
        self.assertIn("/workbench#${workbenchView}", player)

    def test_workbench_review_view_supports_batch_approval(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertIn('id="approve-all-review"', workbench)
        self.assertIn('一键全部通过', workbench)
        self.assertIn('id="batch-approve-modal"', workbench)
        self.assertIn("story.review_state === 'ready_for_parent' && story.candidate_revision && story.package_digest", workbench)
        self.assertIn("decision: 'approved'", workbench)
        self.assertIn("window.resumeBatchApproval", workbench)
        self.assertIn("已通过 ${approved} 本", workbench)

    def test_workbench_unpublished_cards_show_reason_and_align_actions(self):
        workbench = (ROOT / "service" / "ui" / "workbench.html").read_text(encoding="utf-8")

        self.assertIn("const shelfReason = shelfStatus === 'unpublished'", workbench)
        self.assertIn("<b>下架原因</b>", workbench)
        self.assertIn("story.shelf_reason || '未填写'", workbench)
        self.assertIn("class=\"action-placeholder\"", workbench)
        self.assertIn(".story-card{display:flex;flex-direction:column}", workbench)
        self.assertIn(".story-card .card-footer{margin-top:auto;grid-template-columns:repeat(3,minmax(0,1fr))}", workbench)

    def test_player_manual_navigation_does_not_seek_audio_or_race_seeking(self):
        player = (ROOT / "service" / "ui" / "player.html").read_text(encoding="utf-8")

        self.assertIn('id="audio-seek" class="audio-range" type="range"', player)
        self.assertIn('id="audio-toggle" class="audio-toggle"', player)
        self.assertIn("const autoplayRequested = new URLSearchParams(location.search).get('autoplay') === '1';", player)
        self.assertIn("const attempt = audio.play();", player)
        self.assertIn("render(index - 1, 'manual')", player)
        self.assertIn("render(index + 1, 'manual')", player)
        self.assertIn("render(Number(dot.dataset.index), 'manual')", player)
        self.assertIn("if ((manualBrowse && !force) || audioSeeking) return", player)
        self.assertNotIn("audio.currentTime = slide.start", player)
        self.assertIn("旁白播放和进度保持不变", player)
        self.assertNotIn("audio.addEventListener('seeking', syncToAudio)", player)
        self.assertIn("audio.addEventListener('seeked', () =>", player)

    def test_player_keeps_full_illustration_visible_on_ipad(self):
        player = (ROOT / "service" / "ui" / "player.html").read_text(encoding="utf-8")
        compact = "".join(player.split())

        self.assertIn("object-fit:contain", compact)
        self.assertIn("height:min(75vw,calc(100dvh-143px),840px)", compact)
        self.assertIn("place-items:startcenter", compact)
        self.assertIn("width:min(1120px,100%)", compact)
        self.assertNotIn("object-fit:cover", compact)
        self.assertLess(compact.index('</section><divid="caption"'), compact.index('<navid="page-nav"'))

    def test_player_caption_text_uses_one_body_font_size(self):
        player = (ROOT / "service" / "ui" / "player.html").read_text(encoding="utf-8")

        self.assertIn(".caption .dialogue { margin-top:6px; font-size:1em;", player)
        self.assertIn(".caption .interaction { margin-top:7px;", player)
        self.assertIn("color:#765d36; font-size:1em;", player)
        self.assertNotIn("font-size:.82em", player)
        self.assertNotIn("font-size:.75em", player)

    def test_library_play_link_requests_autoplay(self):
        library = (ROOT / "service" / "ui" / "library.html").read_text(encoding="utf-8")

        self.assertIn("autoplay=1", library)
        self.assertIn("id=\"audio-seek\"", (ROOT / "service" / "ui" / "player.html").read_text(encoding="utf-8"))

    def test_library_prioritizes_visible_covers_and_hides_production_form_terms(self):
        library = (ROOT / "service" / "ui" / "library.html").read_text(encoding="utf-8")

        self.assertIn("function childFacingSynopsis", library)
        self.assertIn("神话猴形态的佑爸", library)
        self.assertIn("霸王龙形态的佑爸", library)
        self.assertIn("(?:暖阳|星光)形态\\s*Roro", library)
        self.assertIn("escapeHtml(childFacingSynopsis(story.synopsis))", library)
        self.assertIn("loading=\"${index < 3 ? 'eager' : 'lazy'}\"", library)
        self.assertIn("index === 0 ? ' fetchpriority=\"high\"'", library)

    def test_library_offers_named_pdf_download_for_every_story(self):
        library = (ROOT / "service" / "ui" / "library.html").read_text(encoding="utf-8")

        self.assertIn('class="download"', library)
        self.assertIn('href="${escapeHtml(story.download_url)}"', library)
        self.assertIn('download aria-label="下载《${escapeHtml(story.title)}》PDF 绘本"', library)
        self.assertIn("grid-template-columns: 1fr 1fr", library)

    def test_download_filename_is_versioned_and_windows_safe(self):
        filename = StoryHandler._download_filename('月亮/邮局：云梯信?', 'roro-20260901-v1-009')

        self.assertEqual(filename, 'Roro绘本-月亮邮局：云梯信-roro-20260901-v1-009.pdf')
        self.assertNotRegex(filename, r'[<>:"/\\|?*]')

    def test_audio_voice_recognizes_all_supported_published_voices(self):
        expected = {
            "narration-higgs-lady-kiroro.wav": "lady",
            "narration-higgs-roro-01-kiroro.wav": "roro-01",
            "narration-higgs-girl-kiroro.wav": "girl",
            "narration-higgs-man-kiroro.wav": "man",
            "narration-higgs-wqs-kiroro.wav": "wqs",
            "narration-higgs-还不错的男生-kiroro.wav": "还不错的男生",
            "narration-huihui-review.wav": "huihui",
        }
        for filename, voice in expected.items():
            with self.subTest(filename=filename):
                self.assertEqual(StoryHandler._audio_voice(filename), voice)

    def test_audio_options_use_parent_facing_voice_names(self):
        expected = {
            "lady": "温柔女声",
            "roro-01": "可爱女声",
            "girl": "清亮童声",
            "man": "沉稳男声",
            "还不错的男生": "成熟男声",
            "wqs": "低沉男声",
            "huihui": "亲切女声",
        }

        self.assertEqual(
            {voice: details["label"] for voice, details in VOICE_DISPLAY_MAP.items()},
            expected,
        )

    def test_development_mode_provides_session_without_parent_pin(self):
        auth = ReviewAuth(None, mode="development")

        session = auth.session("")

        self.assertIsNotNone(session)
        self.assertTrue(session["csrf"])
        self.assertEqual(auth.login("", "192.0.2.30")["session_id"], "development")

    def test_pin_mode_rejects_missing_pin_configuration(self):
        with self.assertRaises(ValueError):
            ReviewAuth(None, mode="pin")

    def test_successful_login_creates_session_and_clears_failures(self):
        auth = ReviewAuth("123456")
        with self.assertRaises(ReviewError):
            auth.login("000000", "192.0.2.10")

        result = auth.login("123456", "192.0.2.10")

        self.assertIsNotNone(auth.session(result["session_id"]))
        self.assertNotIn("192.0.2.10", auth.failed_logins)

    def test_repeated_bad_pin_is_rate_limited_per_client(self):
        auth = ReviewAuth("123456", max_failed_attempts=3)
        for _ in range(2):
            with self.assertRaises(ReviewError) as raised:
                auth.login("000000", "192.0.2.20")
            self.assertEqual(raised.exception.code, "invalid_pin")

        with self.assertRaises(ReviewError) as raised:
            auth.login("000000", "192.0.2.20")
        self.assertEqual(raised.exception.code, "login_rate_limited")
        self.assertEqual(raised.exception.status, 429)

        other_client = auth.login("123456", "192.0.2.21")
        self.assertIsNotNone(auth.session(other_client["session_id"]))


if __name__ == "__main__":
    unittest.main()
