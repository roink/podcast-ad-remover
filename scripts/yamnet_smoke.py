"""Offline YAMNet inference check using only temporary synthetic audio."""

import tempfile
import wave
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.sound_events import analyze


def main():
    with tempfile.TemporaryDirectory(prefix="yamnet-smoke-") as folder:
        audio = Path(folder) / "silence.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            handle.writeframes(b"\0" * 32000)
        evidence = analyze(str(audio), "/opt/yamnet")
        assert isinstance(evidence["events"], list)
    print("PASS: offline YAMNet inference with temporary synthetic audio")


if __name__ == "__main__":
    main()
