"""Moonshine Small transcription with a process boundary for predictable RAM release."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def transcribe(audio_path, language, models_dir, progress_callback=None):
    with tempfile.TemporaryDirectory(prefix="moonshine-") as folder:
        wav = str(Path(folder) / "audio.wav")
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-threads",
                "2",
                "-i",
                audio_path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                wav,
            ],
            check=True,
            timeout=3600,
        )
        command = [
            sys.executable,
            "-m",
            "app.core.moonshine_worker",
            wav,
            language or "auto",
            str(models_dir),
        ]
        # Leave diagnostics on stderr; only structured progress/results use stdout.
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as worker:
            try:
                result = None
                for line in worker.stdout:
                    event = json.loads(line)
                    if "progress" in event and progress_callback:
                        progress_callback(*event["progress"])
                    if "result" in event:
                        result = event["result"]
                if worker.wait() or result is None:
                    raise RuntimeError("Moonshine transcription failed; see worker diagnostics")
                return result
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    try:
                        worker.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        worker.kill()
                        worker.wait()
