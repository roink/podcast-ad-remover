"""YAMNet SavedModel inference in bounded batches; stdout is JSON only."""

import csv
import json
import sys
import wave


def main():
    import numpy as np
    import tensorflow as tf

    model = tf.saved_model.load(sys.argv[2])
    with open(model.class_map_path().numpy().decode(), newline="") as handle:
        labels = [row["display_name"] for row in csv.DictReader(handle)]
    wanted = {
        i: name
        for i, name in enumerate(labels)
        if name
        in {
            "Music",
            "Theme music",
            "Jingle (music)",
            "Background music",
            "Intro music",
            "Outro music",
            "Singing",
            "Speech",
        }
    }
    events, active = [], {}
    with wave.open(sys.argv[1], "rb") as audio:
        assert (
            audio.getframerate() == 16000
            and audio.getnchannels() == 1
            and audio.getsampwidth() == 2
        )
        duration = audio.getnframes() / 16000
        # 64 prediction hops plus the receptive-field overlap (0.975 s).
        hop_samples, batch_hops = 7680, 64
        for offset in range(0, audio.getnframes(), hop_samples * batch_hops):
            audio.setpos(offset)
            samples = (
                np.frombuffer(
                    audio.readframes(hop_samples * (batch_hops - 1) + 15600), dtype="<i2"
                ).astype(np.float32)
                / 32768
            )
            scores, _, _ = model(samples)
            for frame, score in enumerate(scores.numpy()[:batch_hops]):
                start = offset / 16000 + frame * 0.48
                if start >= duration:
                    break
                end = min(duration, start + 0.48)
                for index, label in wanted.items():
                    value = float(score[index])
                    if value >= 0.25:
                        if label in active and abs(active[label]["end"] - start) < 1e-6:
                            active[label]["end"] = end
                            active[label]["score"] = max(active[label]["score"], round(value, 3))
                        else:
                            event = dict(start=start, end=end, label=label, score=round(value, 3))
                            events.append(event)
                            active[label] = event
                    else:
                        active.pop(label, None)
    print(json.dumps(sorted(events, key=lambda e: (e["start"], e["label"]))))


if __name__ == "__main__":
    main()
