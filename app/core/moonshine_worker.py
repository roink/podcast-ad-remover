"""Offline Moonshine Small EN/DE worker, bounded to two-minute audio windows."""

import json
import math
import os
from pathlib import Path
import sys
import wave


def emit(value):
    print(json.dumps(value), flush=True)


def detect_language(path, models_dir):
    import gc
    import numpy as np
    from faster_whisper import WhisperModel

    # Tiny is used only for automatic language detection, never alongside Moonshine.
    model = WhisperModel(
        "tiny", device="cpu", compute_type="int8", cpu_threads=2, download_root=str(models_dir)
    )
    with wave.open(path, "rb") as audio:
        sample = np.frombuffer(audio.readframes(16000 * 90), dtype="<i2").astype(np.float32) / 32768
    _, info = model.transcribe(sample, beam_size=1, vad_filter=True)
    language = info.language
    del model
    gc.collect()
    return language


def main():
    import numpy as np
    from moonshine_voice import Transcriber, ModelArch
    from moonshine_voice.download import get_model_for_language
    from app.core.transcript_segments import merge_chunk_segments

    path, hint, models_dir = sys.argv[1:]
    language = detect_language(path, models_dir) if hint == "auto" else hint
    if language not in {"en", "de"}:
        raise ValueError(
            f"Automatic detected {language!r}; this build supports Moonshine Small English and German. Set the podcast language explicitly if incorrect."
        )
    cache = Path(os.environ.get("MOONSHINE_MODELS_DIR", "/opt/moonshine"))
    model_path, arch = get_model_for_language(
        language, ModelArch.SMALL_STREAMING, cache_root=cache, on_progress=lambda *args: None
    )
    segments = []
    with (
        Transcriber(model_path=model_path, model_arch=arch) as model,
        wave.open(path, "rb") as audio,
    ):
        duration = audio.getnframes() / 16000
        step, overlap = 120 * 16000, 4 * 16000
        for chunk_index, offset in enumerate(range(0, audio.getnframes(), step)):
            begin = max(0, offset - overlap)
            audio.setpos(begin)
            samples = (
                np.frombuffer(
                    audio.readframes(step + offset - begin + overlap), dtype="<i2"
                ).astype(np.float32)
                / 32768
            )
            result = model.transcribe_without_streaming(samples.tolist(), sample_rate=16000)
            for line in result.lines:
                start, end = (
                    begin / 16000 + line.start_time,
                    begin / 16000 + line.start_time + line.duration,
                )
                if (
                    not line.text.strip()
                    or not math.isfinite(start)
                    or not math.isfinite(end)
                    or end <= start
                ):
                    continue
                # Overlap provides context; assign lines to the central window.
                if (start + end) / 2 < offset / 16000 or (start + end) / 2 >= min(
                    duration, (offset + step) / 16000
                ):
                    continue
                segments.append(
                    dict(
                        start=max(0, start),
                        end=min(duration, end),
                        text=line.text.strip(),
                        chunk_index=chunk_index,
                    )
                )
            emit({"progress": [min(duration, (offset + step) / 16000), duration]})
    segments = merge_chunk_segments(segments)
    emit(
        {
            "result": {
                "text": " ".join(s["text"] for s in segments),
                "segments": segments,
                "language": language,
                "_execution": {
                    "engine": "moonshine",
                    "model": "small-streaming-" + language,
                    "model_arch": int(arch),
                    "device": "cpu",
                    "compute_type": "quantized",
                },
            }
        }
    )


if __name__ == "__main__":
    main()
