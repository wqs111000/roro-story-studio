from __future__ import annotations

import audioop
import importlib.util
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "normalize-narration-silence.py"
SPEC = importlib.util.spec_from_file_location("normalize_narration_silence", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def tone(seconds: float, sample_rate: int = 16000) -> bytes:
    samples = bytearray()
    for index in range(round(seconds * sample_rate)):
        value = round(4000 * math.sin(2 * math.pi * 440 * index / sample_rate))
        samples.extend(int(value).to_bytes(2, byteorder="little", signed=True))
    return bytes(samples)


class NormalizeNarrationSilenceTests(unittest.TestCase):
    def test_compresses_only_silence_at_or_above_minimum(self):
        rate = 16000
        short_silence = audioop.mul(b"\x00\x00" * rate, 2, 1)
        long_silence = audioop.mul(b"\x00\x00" * (rate * 8), 2, 1)
        pcm = tone(1, rate) + short_silence + tone(1, rate) + long_silence + tone(1, rate)

        runs = MODULE.silent_runs(
            pcm,
            sample_rate=rate,
            sample_width=2,
            threshold_db=-45,
            minimum_seconds=5,
        )
        normalized = MODULE.compress_silence(
            pcm,
            runs,
            sample_rate=rate,
            sample_width=2,
            keep_seconds=0.8,
        )

        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(len(normalized) / 2 / rate, 4.8, places=1)


if __name__ == "__main__":
    unittest.main()
