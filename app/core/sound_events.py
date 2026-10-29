"""Bounded, isolated YAMNet analysis and transcript evidence integration."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

VERSION = "yamnet-1-threshold-0.25-hop-0.48-v1"


def analyze(audio_path, models_dir, threads=2):
    # A child owns TensorFlow and its allocator; exiting returns all model RAM.
    with tempfile.TemporaryDirectory(prefix="yamnet-") as folder:
        wav = str(Path(folder) / "audio.wav")
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-threads",
                str(threads),
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
        env = dict(
            os.environ,
            TF_NUM_INTRAOP_THREADS=str(threads),
            TF_NUM_INTEROP_THREADS="1",
            CUDA_VISIBLE_DEVICES="-1",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.core.yamnet_worker",
                wav,
                os.environ.get("YAMNET_MODEL_DIR", "/opt/yamnet"),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=7200,
        )
        return {"version": VERSION, "events": json.loads(result.stdout)}


def add_evidence(units, evidence):
    """Split gaps at sound boundaries; never split or duplicate transcript text."""
    events = evidence.get("events", [])
    result = []
    for unit in units:
        overlapping = [e for e in events if e["start"] < unit["end"] and e["end"] > unit["start"]]
        bounds = {unit["start"], unit["end"]}
        if unit["kind"] == "GAP":
            for event in overlapping:
                bounds.update((max(unit["start"], event["start"]), min(unit["end"], event["end"])))
        bounds = sorted(bounds)
        for start, end in zip(bounds, bounds[1:]):
            item = dict(unit, start=start, end=end, id=len(result) + 1)
            item["sound_events"] = [
                dict(e, start=max(start, e["start"]), end=min(end, e["end"]))
                for e in overlapping
                if e["start"] < end and e["end"] > start
            ]
            result.append(item)
    return result
