"""Keep parent-visible story text aligned with the text sent to narration TTS."""

from __future__ import annotations

import re
from typing import Any


NARRATOR_NAMES = {"旁白", "叙述", "narrator"}


def page_number(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def spoken_segment_text(segment: dict[str, Any]) -> str:
    """Render a narration segment exactly as the TTS renderer speaks it."""
    text = str(segment.get("text", "")).strip()
    if not text:
        return ""
    speaker = str(segment.get("speaker", "旁白")).strip() or "旁白"
    if speaker in NARRATOR_NAMES:
        return text
    cue = str(segment.get("speech_cue", "")).strip()
    if not cue:
        cue = "大家一起说" if speaker == "大家" else f"{speaker}说"
    return f"{cue}：“{text}”"


def narration_pages(story: dict[str, Any], narration: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Return ordered, parent-facing lines for every story page."""
    pages: dict[int, dict[str, Any]] = {}
    for index, page in enumerate(story.get("pages", [])):
        if not isinstance(page, dict):
            continue
        page_no = page_number(page.get("page", index + 1))
        if page_no is None or page_no < 1:
            continue
        pages[page_no] = {"page": page_no, "spoken_lines": [], "spoken_text": ""}
    for segment in narration.get("segments", []):
        if not isinstance(segment, dict):
            continue
        page_no = page_number(segment.get("page", 0) or 0)
        spoken = spoken_segment_text(segment)
        if page_no not in pages or not spoken:
            continue
        pages[page_no]["spoken_lines"].append(
            {
                "speaker": str(segment.get("speaker", "旁白")).strip() or "旁白",
                "text": str(segment.get("text", "")).strip(),
                "spoken_text": spoken,
            }
        )
    for page in pages.values():
        page["spoken_text"] = "\n".join(line["spoken_text"] for line in page["spoken_lines"])
    return pages


def alignment_issues(story: dict[str, Any], narration: dict[str, Any]) -> list[str]:
    """Find structural drift that would make visible story content and TTS disagree."""
    issues: list[str] = []
    segments = [item for item in narration.get("segments", []) if isinstance(item, dict)]
    title = str(story.get("title", "")).strip().strip("《》")
    title_pattern = re.compile(rf"^《{re.escape(title)}》[。.!！?？]*$") if title else None
    if title_pattern and any(title_pattern.fullmatch(str(item.get("text", "")).strip()) for item in segments):
        issues.append("旁白分段中重复包含封面书名；书名应由封面音频单独朗读。")

    page_numbers: set[int] = set()
    for index, page in enumerate(story.get("pages", [])):
        if not isinstance(page, dict):
            continue
        page_no = page_number(page.get("page", index + 1))
        if page_no is None or page_no < 1:
            issues.append(f"第 {index + 1} 页的页码格式不正确。")
            continue
        page_numbers.add(page_no)
        page_segments = [item for item in segments if page_number(item.get("page", 0) or 0) == page_no]
        if not page_segments:
            issues.append(f"第 {page_no} 页没有旁白分段。")
            continue
        narrator_texts = [
            str(item.get("text", "")).strip()
            for item in page_segments
            if str(item.get("speaker", "旁白")).strip() in NARRATOR_NAMES
        ]
        narration_text = str(page.get("narration", "")).strip()
        if narration_text and narration_text not in narrator_texts:
            issues.append(f"第 {page_no} 页正文与旁白叙述不一致。")
        interaction = str(page.get("interaction") or "").strip()
        narrative_only_texts = [text for text in narrator_texts if text != interaction]
        for dialogue in page.get("dialogue", []):
            if not isinstance(dialogue, dict):
                continue
            speaker = str(dialogue.get("speaker", "")).strip()
            text = str(dialogue.get("text", "")).strip()
            matches = [
                item for item in page_segments
                if str(item.get("speaker", "")).strip() == speaker
                and str(item.get("text", "")).strip() == text
            ]
            if len(matches) != 1:
                issues.append(f"第 {page_no} 页“{speaker}”的对白未在旁白稿中准确出现一次。")
            if text and any(text in narrator_text for narrator_text in narrative_only_texts):
                issues.append(f"第 {page_no} 页对白“{text}”同时写进叙述，朗读时会重复。")
        if interaction and interaction not in [str(item.get("text", "")).strip() for item in page_segments]:
            issues.append(f"第 {page_no} 页互动提示未进入实际朗读稿。")

    extra_pages = sorted(
        {
            page_number(item.get("page", 0) or 0)
            for item in segments
            if page_number(item.get("page", 0) or 0) is not None
            and page_number(item.get("page", 0) or 0) not in page_numbers
        }
    )
    if extra_pages:
        issues.append(f"旁白稿包含不存在的页面：{', '.join(map(str, extra_pages))}。")
    return issues


def require_narration_alignment(story: dict[str, Any], narration: dict[str, Any]) -> None:
    issues = alignment_issues(story, narration)
    if issues:
        raise ValueError("旁白稿与故事正文不一致：" + "；".join(issues))
