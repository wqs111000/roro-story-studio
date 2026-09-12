#!/usr/bin/env python3
"""Transcribe narration WAV files with the local FunASR service for content QA."""

from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import ssl
import wave
from pathlib import Path

import websockets


FORBIDDEN_CONTROL_WORDS = (
    "emotion",
    "curious",
    "gentle",
    "happy",
    "proud",
    "sleepy",
    "concerned",
    "contentment",
    "contemplation",
    "affection",
    "elation",
    "pride",
)


def pcm16_mono_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        pcm = source.readframes(source.getnframes())
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
        width = 2
    if channels == 2:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    elif channels != 1:
        raise ValueError(f"Unsupported channel count {channels}: {path}")
    if rate != 16000:
        pcm, _ = audioop.ratecv(pcm, width, 1, rate, 16000, None)
    return pcm


async def transcribe(path: Path, server_url: str) -> str:
    pcm = pcm16_mono_16k(path)
    ssl_context = None
    if server_url.startswith("wss://"):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    async with websockets.connect(
        server_url,
        ssl=ssl_context,
        max_size=None,
        close_timeout=3,
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "mode": "offline",
                    "chunk_size": [5, 10, 5],
                    "wav_name": path.stem,
                    "wav_format": "pcm",
                    "audio_fs": 16000,
                    "is_speaking": True,
                    "itn": True,
                }
            )
        )
        for offset in range(0, len(pcm), 32000):
            await socket.send(pcm[offset : offset + 32000])
        await socket.send(json.dumps({"is_speaking": False}))
        texts: list[str] = []
        while True:
            try:
                message = await asyncio.wait_for(socket.recv(), timeout=30)
            except asyncio.TimeoutError:
                break
            if isinstance(message, bytes):
                continue
            result = json.loads(message)
            text = str(result.get("text", "")).strip()
            if text:
                texts.append(text)
            if result.get("is_final"):
                break
        return max(texts, key=len, default="")


async def run(paths: list[Path], server_url: str) -> int:
    failures: list[dict[str, object]] = []
    for path in paths:
        transcript = await transcribe(path, server_url)
        matched = [word for word in FORBIDDEN_CONTROL_WORDS if word in transcript.lower()]
        if not transcript or matched:
            failures.append(
                {"file": str(path), "empty": not transcript, "forbidden_words": matched}
            )
        print(
            json.dumps({"file": str(path), "transcript": transcript}, ensure_ascii=False),
            flush=True,
        )
    print(
        json.dumps(
            {"checked": len(paths), "failures": failures, "result": "passed" if not failures else "failed"},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if not failures else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--server-url", default="wss://127.0.0.1:10095")
    args = parser.parse_args()
    return asyncio.run(run(args.paths, args.server_url))


if __name__ == "__main__":
    raise SystemExit(main())
