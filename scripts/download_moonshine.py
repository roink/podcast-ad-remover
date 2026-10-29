"""Bake the explicitly requested Small Streaming models into the image."""

from pathlib import Path
import sys
from moonshine_voice import ModelArch
from moonshine_voice.download import get_model_for_language

for language in ("en", "de"):
    get_model_for_language(
        language,
        ModelArch.SMALL_STREAMING,
        cache_root=Path(sys.argv[1]),
        on_progress=lambda *args: None,
    )
