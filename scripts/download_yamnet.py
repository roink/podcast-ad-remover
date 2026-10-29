"""Fetch the official YAMNet v1 SavedModel at image build time."""

from pathlib import Path
import sys
import tarfile
import tempfile
import urllib.request


def main():
    target = Path(sys.argv[1])
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as archive:
        with urllib.request.urlopen(
            "https://tfhub.dev/google/yamnet/1?tf-hub-format=compressed", timeout=120
        ) as response:
            while chunk := response.read(1024 * 1024):
                archive.write(chunk)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as bundle:
            bundle.extractall(target, filter="data")
    if not (target / "saved_model.pb").is_file():
        raise RuntimeError("YAMNet download did not contain a SavedModel")


if __name__ == "__main__":
    main()
