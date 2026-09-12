#!/usr/bin/env python3
"""Compress only abnormally long silent runs in generated narration WAV files."""

from __future__ import annotations

import argparse
import audioop
import math
import os
import tempfile
import wave
from pathlib import Path


def silent_runs(
    pcm: bytes,
    *,
    sample_rate: int,
    sample_width: int,
    threshold_db: float,
    minimum_seconds: float,
    window_ms: int = 20,
) -> list[tuple[int, int]]:
    frame_bytes = sample_width
    window_frames = max(1, round(sample_rate * window_ms / 1000))
    window_bytes = window_frames * frame_bytes
    threshold = 32768 * math.pow(10, threshold_db / 20)
    silent_windows: list[tuple[int, int]] = []
    for start in range(0, len(pcm), window_bytes):
        end = min(len(pcm), start + window_bytes)
        chunk = pcm[start:end]
        if len(chunk) >= sample_width and audioop.rms(chunk, sample_width) <= threshold:
            silent_windows.append((start, end))

    runs: list[tuple[int, int]] = []
    if not silent_windows:
        return runs
    start, end = silent_windows[0]
    for next_start, next_end in silent_windows[1:]:
        if next_start == end:
            end = next_end
            continue
        if (end - start) / frame_bytes / sample_rate >= minimum_seconds:
            runs.append((start, end))
        start, end = next_start, next_end
    if (end - start) / frame_bytes / sample_rate >= minimum_seconds:
        runs.append((start, end))
    return runs


def compress_silence(
    pcm: bytes,
    runs: list[tuple[int, int]],
    *,
    sample_rate: int,
    sample_width: int,
    keep_seconds: float,
) -> bytes:
    if not runs:
        return pcm
    keep_bytes = round(sample_rate * keep_seconds) * sample_width
    half_keep = keep_bytes // 2
    result = bytearray()
    cursor = 0
    for start, end in runs:
        result.extend(pcm[cursor:start])
        if start == 0:
            result.extend(pcm[max(start, end - keep_bytes) : end])
        elif end == len(pcm):
            result.extend(pcm[start : min(end, start + keep_bytes)])
        else:
            result.extend(pcm[start : min(end, start + half_keep)])
            result.extend(pcm[max(start, end - (keep_bytes - half_keep)) : end])
        cursor = end
    result.extend(pcm[cursor:])
    return bytes(result)


def normalize(
    source: Path,
    destination: Path,
    *,
    threshold_db: float,
    minimum_seconds: float,
    keep_seconds: float,
) -> dict:
    with wave.open(str(source), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        sample_rate = audio.getframerate()
        compression = audio.getcomptype()
        pcm = audio.readframes(audio.getnframes())
    if channels != 1 or sample_width != 2 or compression != "NONE":
        raise ValueError(f"Expected PCM 16-bit mono WAV: {source}")

    runs = silent_runs(
        pcm,
        sample_rate=sample_rate,
        sample_width=sample_width,
        threshold_db=threshold_db,
        minimum_seconds=minimum_seconds,
    )
    normalized = compress_silence(
        pcm,
        runs,
        sample_rate=sample_rate,
        sample_width=sample_width,
        keep_seconds=keep_seconds,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.stem}-",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(sample_width)
            output.setframerate(sample_rate)
            output.writeframes(normalized)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()

    frame_bytes = channels * sample_width
    return {
        "source": source,
        "destination": destination,
        "runs": len(runs),
        "before_seconds": len(pcm) / frame_bytes / sample_rate,
        "after_seconds": len(normalized) / frame_bytes / sample_rate,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--source-tag", default="corrected-v2")
    parser.add_argument("--output-tag", default="corrected-v3")
    parser.add_argument("--threshold-db", type=float, default=-45.0)
    parser.add_argument("--minimum-seconds", type=float, default=5.0)
    parser.add_argument("--keep-seconds", type=float, default=0.8)
    args = parser.parse_args()
    for source in args.paths:
        if args.source_tag not in source.name:
            parser.error(f"source tag {args.source_tag!r} missing from {source}")
        destination = source.with_name(source.name.replace(args.source_tag, args.output_tag, 1))
        result = normalize(
            source,
            destination,
            threshold_db=args.threshold_db,
            minimum_seconds=args.minimum_seconds,
            keep_seconds=args.keep_seconds,
        )
        print(
            f"WROTE {destination}: runs={result['runs']} "
            f"duration={result['before_seconds']:.2f}->{result['after_seconds']:.2f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
