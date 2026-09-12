#!/usr/bin/env python3
"""Render draft narration variants with the kiroro Higgs TTS tenant."""

from __future__ import annotations

import argparse
import audioop
import io
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import wave
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1] / "service"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
from narration_alignment import require_narration_alignment, spoken_segment_text  # noqa: E402


DEFAULT_VOICES = ("lady", "roro-01", "wqs", "man", "还不错的男生")
DEFAULT_TTS_URL = "http://127.0.0.1:8081"
HIGGS_EMOTIONS = {
    "affection",
    "amusement",
    "anger",
    "arousal",
    "awe",
    "bitterness",
    "confusion",
    "contemplation",
    "contentment",
    "determination",
    "disgust",
    "elation",
    "enthusiasm",
    "fear",
    "helplessness",
    "longing",
    "pride",
    "relief",
    "sadness",
    "shame",
    "surprise",
}
STORY_EMOTION_TO_HIGGS = {
    "calm": "contentment",
    "concerned": "contemplation",
    "curious": "contemplation",
    "gentle": "affection",
    "happy": "elation",
    "proud": "pride",
    "sleepy": "contentment",
}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def request_json(opener: urllib.request.OpenerDirector, url: str, api_key: str) -> dict:
    request = urllib.request.Request(url, headers={"X-API-Key": api_key})
    with opener.open(request, timeout=30) as response:
        return json.load(response)


def higgs_emotion(value: object) -> str:
    emotion = str(value or "").strip().lower()
    mapped = STORY_EMOTION_TO_HIGGS.get(emotion, emotion)
    return mapped if mapped in HIGGS_EMOTIONS else "contentment"


def narration_text(narration: dict) -> str:
    parts: list[str] = []
    for segment in narration.get("segments", []):
        if not isinstance(segment, dict):
            continue
        text = spoken_segment_text(segment)
        if not text:
            continue
        emotion = higgs_emotion(segment.get("emotion"))
        parts.append(f"<|emotion:{emotion}|>{text}")
    return "\n".join(parts)


def page_narration_text(narration: dict, page: int) -> str:
    """Render one page so page boundaries remain exact for every voice."""
    parts: list[str] = []
    previous_emotion = ""
    for segment in narration.get("segments", []):
        if not isinstance(segment, dict) or int(segment.get("page", 0) or 0) != page:
            continue
        text = spoken_segment_text(segment)
        if not text:
            continue
        emotion = higgs_emotion(segment.get("emotion"))
        prefix = f"<|emotion:{emotion}|>" if emotion != previous_emotion else ""
        parts.append(f"{prefix}{text}")
        previous_emotion = emotion
    return "\n".join(parts)


def cover_title_text(title: object) -> str:
    """Return a short child-facing title announcement for the cover."""
    cleaned = re.sub(r"[。.!！?？]+$", "", str(title or "").strip())
    if cleaned.startswith("《") and cleaned.endswith("》"):
        cleaned = cleaned[1:-1].strip()
    if not cleaned:
        cleaned = "家庭绘本"
    return f"<|emotion:contentment|>《{cleaned}》。"


def story_title(story_dir: Path) -> str:
    story_path = story_dir / "story.json"
    if not story_path.is_file():
        return story_dir.name
    try:
        story = json.loads(story_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return story_dir.name
    return str(story.get("title") or story_dir.name).strip()


def validate_unrendered_narration(story_dir: Path, narration: dict) -> None:
    """Reject new narration drift while leaving immutable legacy audio reproducible."""
    if not narration.get("alignment_version") and narration.get("audio_rendered") is not False:
        return
    story = json.loads((story_dir / "story.json").read_text(encoding="utf-8-sig"))
    require_narration_alignment(story, narration)


def cached_speech(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    api_key: str,
    voice: str,
    text: str,
    audio_path: Path,
    force: bool,
) -> bytes:
    """Reuse a segment only when its exact TTS input is unchanged."""
    text_path = audio_path.with_suffix(".txt")
    cached_text = ""
    if text_path.is_file():
        try:
            cached_text = text_path.read_text(encoding="utf-8")
        except OSError:
            cached_text = ""
    if audio_path.is_file() and cached_text == text and not force:
        return audio_path.read_bytes()
    try:
        rendered = request_speech(opener, base_url, api_key, voice, text)
    except urllib.error.HTTPError as error:
        if error.code < 500:
            raise
        rendered = request_speech(
            opener,
            base_url,
            api_key,
            voice,
            stable_emotion_fallback_text(text),
        )
    audio = normalized_wav_bytes(rendered)
    write_atomic(audio_path, audio)
    write_atomic(text_path, text.encode("utf-8"))
    return audio


def request_speech(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    api_key: str,
    voice: str,
    text: str,
) -> bytes:
    body = json.dumps(
        {"input": text, "voice": voice, "stream": False},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/audio/speech",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
    )
    with opener.open(request, timeout=600) as response:
        audio = response.read()
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise RuntimeError(f"{voice}: TTS response is not a WAV file")
    return audio


def stable_emotion_fallback_text(text: str) -> str:
    """Preserve spoken copy while simplifying controls after a model-side 5xx."""
    spoken = re.sub(r"<\|emotion:[^|>]+\|>", "", text).strip()
    return f"<|emotion:contentment|>{spoken}"


def normalized_wav_bytes(audio: bytes) -> bytes:
    """Compress only model-produced silent runs of five seconds or longer."""
    with wave.open(io.BytesIO(audio), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        compression = source.getcomptype()
        pcm = source.readframes(source.getnframes())
    if channels != 1 or sample_width != 2 or compression != "NONE":
        raise RuntimeError("Expected PCM 16-bit mono WAV from TTS")

    window_frames = max(1, round(sample_rate * 0.02))
    window_bytes = window_frames * sample_width
    threshold = 32768 * math.pow(10, -45 / 20)
    windows: list[tuple[int, int]] = []
    for start in range(0, len(pcm), window_bytes):
        end = min(len(pcm), start + window_bytes)
        chunk = pcm[start:end]
        if len(chunk) >= sample_width and audioop.rms(chunk, sample_width) <= threshold:
            windows.append((start, end))

    runs: list[tuple[int, int]] = []
    if windows:
        start, end = windows[0]
        for next_start, next_end in windows[1:]:
            if next_start == end:
                end = next_end
                continue
            if (end - start) / sample_width / sample_rate >= 5.0:
                runs.append((start, end))
            start, end = next_start, next_end
        if (end - start) / sample_width / sample_rate >= 5.0:
            runs.append((start, end))

    keep_bytes = round(sample_rate * 0.8) * sample_width
    normalized = bytearray()
    cursor = 0
    for start, end in runs:
        normalized.extend(pcm[cursor:start])
        if start == 0:
            normalized.extend(pcm[max(start, end - keep_bytes) : end])
        elif end == len(pcm):
            normalized.extend(pcm[start : min(end, start + keep_bytes)])
        else:
            first = keep_bytes // 2
            normalized.extend(pcm[start : min(end, start + first)])
            normalized.extend(pcm[max(start, end - (keep_bytes - first)) : end])
        cursor = end
    normalized.extend(pcm[cursor:])

    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(sample_width)
        target.setframerate(sample_rate)
        target.writeframes(bytes(normalized))
    return output.getvalue()


def wav_parts(audio: bytes) -> tuple[tuple[int, int, int], bytes]:
    with wave.open(io.BytesIO(audio), "rb") as source:
        if source.getcomptype() != "NONE":
            raise RuntimeError("Expected uncompressed WAV")
        format_key = (source.getnchannels(), source.getsampwidth(), source.getframerate())
        return format_key, source.readframes(source.getnframes())


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.stem}-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def render_paged(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    api_key: str,
    story_dir: Path,
    voice: str,
    force: bool,
    output_tag: str,
    cover_hold_ms: int,
    page_gap_ms: int,
    refresh_pages: set[int],
) -> str:
    narration_path = story_dir / "narration.json"
    if not narration_path.is_file():
        return f"SKIP {story_dir.name}: narration.json missing"
    narration = json.loads(narration_path.read_text(encoding="utf-8-sig"))
    validate_unrendered_narration(story_dir, narration)
    title = story_title(story_dir)
    title_text = cover_title_text(title)
    page_numbers = sorted(
        {
            int(segment.get("page", 0) or 0)
            for segment in narration.get("segments", [])
            if isinstance(segment, dict) and int(segment.get("page", 0) or 0) > 0
        }
    )
    if not page_numbers:
        return f"SKIP {story_dir.name}: narration has no pages"

    audio_dir = story_dir / "audio"
    output = audio_dir / f"narration-higgs-{voice}-kiroro-{output_tag}.wav"
    sync_path = output.with_suffix(".sync.json")

    segment_dir = audio_dir / "page-segments" / output_tag / voice
    cover_audio = cached_speech(
        opener,
        base_url,
        api_key,
        voice,
        title_text,
        segment_dir / "cover-title.wav",
        force,
    )
    page_audio: list[tuple[int, bytes, list[str]]] = []
    for page in page_numbers:
        text = page_narration_text(narration, page)
        if not text:
            raise RuntimeError(f"{story_dir.name}/{voice}/page-{page}: narration is empty")
        segment_path = segment_dir / f"page-{page:02d}.wav"
        audio = cached_speech(
            opener,
            base_url,
            api_key,
            voice,
            text,
            segment_path,
            force or page in refresh_pages,
        )
        segment_ids = [
            str(segment.get("id", ""))
            for segment in narration.get("segments", [])
            if isinstance(segment, dict) and int(segment.get("page", 0) or 0) == page
        ]
        page_audio.append((page, audio, segment_ids))

    cover_format, cover_pcm = wav_parts(cover_audio)
    formats = [wav_parts(audio) for _, audio, _ in page_audio]
    format_key = cover_format
    if any(item[0] != format_key for item in formats):
        raise RuntimeError(f"{story_dir.name}/{voice}: page WAV formats do not match")
    channels, sample_width, sample_rate = format_key
    frame_width = channels * sample_width
    cover_gap_frames = round(sample_rate * cover_hold_ms / 1000)
    gap_frames = round(sample_rate * page_gap_ms / 1000)
    cover_title_frames = len(cover_pcm) // frame_width
    combined = bytearray(cover_pcm)
    combined.extend(b"\x00" * cover_gap_frames * frame_width)
    cursor_frames = cover_title_frames + cover_gap_frames
    timeline: list[dict] = []
    for index, ((page, _, segment_ids), (_, pcm)) in enumerate(zip(page_audio, formats)):
        page_frames = len(pcm) // frame_width
        start_seconds = cursor_frames / sample_rate
        combined.extend(pcm)
        cursor_frames += page_frames
        end_seconds = cursor_frames / sample_rate
        timeline.append(
            {
                "page": page,
                "start_seconds": round(start_seconds, 3),
                "end_seconds": round(end_seconds, 3),
                "duration_seconds": round(end_seconds - start_seconds, 3),
                "segment_ids": segment_ids,
            }
        )
        if index < len(page_audio) - 1 and gap_frames:
            combined.extend(b"\x00" * gap_frames * frame_width)
            cursor_frames += gap_frames

    output_buffer = io.BytesIO()
    with wave.open(output_buffer, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(sample_width)
        target.setframerate(sample_rate)
        target.writeframes(bytes(combined))
    write_atomic(output, output_buffer.getvalue())
    sync = {
        "schema_version": 1,
        "story_id": story_dir.name,
        "voice": voice,
        "audio": output.name,
        "duration_seconds": round(cursor_frames / sample_rate, 3),
        "cover": {
            "title": title,
            "start_seconds": 0.0,
            "end_seconds": round(cover_title_frames / sample_rate, 3),
            "duration_seconds": round(cover_title_frames / sample_rate, 3),
        },
        "cover_hold_seconds": round((cover_title_frames + cover_gap_frames) / sample_rate, 3),
        "cover_gap_seconds": round(cover_gap_frames / sample_rate, 3),
        "page_gap_seconds": round(gap_frames / sample_rate, 3),
        "pages": timeline,
    }
    write_atomic(sync_path, (json.dumps(sync, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return f"WROTE {story_dir.name}: {output.name} ({len(timeline)} timed pages)"


def render(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    api_key: str,
    story_dir: Path,
    voice: str,
    force: bool,
    output_tag: str,
) -> str:
    narration_path = story_dir / "narration.json"
    if not narration_path.is_file():
        return f"SKIP {story_dir.name}: narration.json missing"
    narration = json.loads(narration_path.read_text(encoding="utf-8-sig"))
    validate_unrendered_narration(story_dir, narration)
    text = narration_text(narration)
    if not text:
        return f"SKIP {story_dir.name}: narration is empty"

    audio_dir = story_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"-{output_tag}" if output_tag else ""
    output = audio_dir / f"narration-higgs-{voice}-kiroro{suffix}.wav"
    if output.exists() and not force:
        return f"KEEP {story_dir.name}: {output.name}"

    audio = request_speech(opener, base_url, api_key, voice, text)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.stem}-",
            suffix=".tmp",
            dir=audio_dir,
            delete=False,
        ) as temp_file:
            temp_file.write(audio)
            temp_path = Path(temp_file.name)
        os.replace(temp_path, output)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return f"WROTE {story_dir.name}: {output.name} ({len(audio)} bytes)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stories", nargs="*", help="Draft story directory names")
    parser.add_argument("--all-drafts", action="store_true", help="Render every draft with narration.json")
    parser.add_argument("--voices", nargs="+", default=list(DEFAULT_VOICES))
    parser.add_argument("--force", action="store_true", help="Replace matching kiroro candidate files")
    parser.add_argument(
        "--page-synchronized",
        action="store_true",
        help="Render each page separately, concatenate it, and write a voice-specific .sync.json timeline",
    )
    parser.add_argument(
        "--cover-hold-ms",
        type=int,
        default=800,
        help="Silence after the spoken cover title before page 1 begins",
    )
    parser.add_argument("--page-gap-ms", type=int, default=250)
    parser.add_argument(
        "--refresh-pages",
        nargs="+",
        type=int,
        default=[],
        help="With --page-synchronized, regenerate only these page segments and rebuild the track",
    )
    parser.add_argument(
        "--output-tag",
        default="",
        help="Append a safe filename tag before .wav (for example corrected-v2)",
    )
    parser.add_argument("--env-file", type=Path, help="File containing TTS_SERVER_URL and TTS_API_KEY")
    args = parser.parse_args()
    if args.output_tag and not all(character.isalnum() or character in "-_" for character in args.output_tag):
        parser.error("--output-tag may contain only letters, numbers, hyphens, and underscores")
    if args.page_synchronized and not args.output_tag:
        parser.error("--page-synchronized requires --output-tag")
    if args.cover_hold_ms < 0 or args.page_gap_ms < 0:
        parser.error("page timing values may not be negative")
    if args.refresh_pages and not args.page_synchronized:
        parser.error("--refresh-pages requires --page-synchronized")
    if any(page <= 0 for page in args.refresh_pages):
        parser.error("--refresh-pages values must be positive")

    workspace = Path(__file__).resolve().parents[1]
    default_env = workspace.parent / "agent-roro" / ".env"
    config = read_env_file(args.env_file or default_env)
    base_url = os.environ.get("TTS_SERVER_URL") or config.get("TTS_SERVER_URL") or DEFAULT_TTS_URL
    api_key = os.environ.get("TTS_API_KEY") or config.get("TTS_API_KEY")
    if not api_key:
        parser.error("TTS_API_KEY is missing; set it in the environment or --env-file")
    base_url = base_url.rstrip("/")

    # Ignore system proxy settings for the local/LAN TTS service.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        health = request_json(opener, f"{base_url}/health", api_key)
        catalog = request_json(opener, f"{base_url}/api/voices", api_key)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"TTS connection failed: {error}", file=sys.stderr)
        return 2
    if health.get("status") != "healthy" or health.get("tenant_id") not in {"kiroro", "default"}:
        print(f"Refusing to render: expected a healthy Higgs tenant, got {health!r}", file=sys.stderr)
        return 2
    if catalog.get("tenant_id") != "kiroro":
        print("Refusing to render: voice catalog is not kiroro", file=sys.stderr)
        return 2
    available = {str(item.get("name")) for item in catalog.get("voices", []) if isinstance(item, dict)}
    missing = [voice for voice in args.voices if voice not in available]
    if missing:
        print(f"Refusing to render: voices unavailable in kiroro: {', '.join(missing)}", file=sys.stderr)
        return 2

    drafts_root = workspace / "drafts"
    if args.all_drafts:
        story_dirs = sorted(path for path in drafts_root.iterdir() if (path / "narration.json").is_file())
    else:
        if not args.stories:
            parser.error("provide story names or --all-drafts")
        story_dirs = [(drafts_root / name).resolve() for name in args.stories]
        if any(drafts_root.resolve() not in path.parents for path in story_dirs):
            parser.error("story paths must stay under drafts/")

    failures = 0
    for story_dir in story_dirs:
        for voice in args.voices:
            try:
                print(
                    (
                        render_paged(
                            opener,
                            base_url,
                            api_key,
                            story_dir,
                            voice,
                            args.force,
                            args.output_tag,
                            args.cover_hold_ms,
                            args.page_gap_ms,
                            set(args.refresh_pages),
                        )
                        if args.page_synchronized
                        else render(
                            opener,
                            base_url,
                            api_key,
                            story_dir,
                            voice,
                            args.force,
                            args.output_tag,
                        )
                    ),
                    flush=True,
                )
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, RuntimeError) as error:
                failures += 1
                print(f"FAILED {story_dir.name}/{voice}: {error}", file=sys.stderr, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
