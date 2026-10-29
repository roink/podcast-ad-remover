"""Language persistence and acoustic evidence preserve transcript/timing integrity."""

from types import SimpleNamespace
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.models import SubscriptionCreate
from app.api.v1.schemas import SubscriptionSettingsUpdate
from app.infra.database import init_db, get_db_connection
from app.infra.repository import SubscriptionRepository
from app.core.timeline import prepare_timeline, source_message
from app.core.sound_events import add_evidence


def test_language_migration_and_repository(isolated_data_dir):
    init_db()
    with get_db_connection() as conn:
        conn.execute("ALTER TABLE subscriptions DROP COLUMN language")
        conn.execute("DELETE FROM schema_migrations WHERE version='20261029_0028_podcast_language'")
        conn.commit()
    init_db()
    repo = SubscriptionRepository()
    sub = repo.create(SubscriptionCreate(feed_url="https://example.org/feed"), "Example", "example")
    assert sub.language == "auto"
    args = dict(
        remove_ads=True,
        remove_promos=True,
        remove_intros=False,
        remove_outros=False,
        custom_instructions=None,
        append_summary=False,
        append_title_intro=False,
        ai_rewrite_description=False,
        ai_audio_summary=False,
    )
    repo.update_settings(sub.id, **args, language="de")
    assert repo.get_by_id(sub.id).language == "de"
    repo.update_settings(sub.id, **args)
    init_db()
    assert repo.get_by_id(sub.id).language == "de"
    with pytest.raises(ValueError):
        repo.update_settings(sub.id, **args, language="invalid")
    with pytest.raises(ValidationError):
        SubscriptionSettingsUpdate(language="invalid")
    assert SubscriptionSettingsUpdate(language="en").language == "en"
    with get_db_connection() as conn:
        assert conn.execute(
            "SELECT version FROM schema_migrations WHERE version='20261029_0028_podcast_language'"
        ).fetchone()
    assert list((Path(isolated_data_dir) / "backups").glob("*.db"))


def test_music_in_gaps_and_under_speech_is_sent_to_classifier():
    transcript = {
        "segments": [{"start": 2, "end": 7, "text": "Keep these exact words."}],
        "sound_evidence": {
            "events": [
                {"start": 1, "end": 4, "label": "Music", "score": 0.8},
                {"start": 8, "end": 9, "label": "Jingle (music)", "score": 0.6},
            ]
        },
    }
    units, _ = prepare_timeline(transcript, 10)
    assert units[0]["start"] == 0 and units[-1]["end"] == 10
    assert all(a["end"] == b["start"] for a, b in zip(units, units[1:]))
    speech = [u for u in units if u["text"]]
    assert len(speech) == 1 and speech[0]["text"] == "Keep these exact words."
    assert speech[0]["sound_events"][0]["start"] == 2
    assert speech[0]["sound_events"][0]["end"] == 4
    assert "Jingle (music)" in source_message(units, 10, {})
    assert transcript["segments"][0]["start"] == 2


def test_empty_evidence_preserves_coverage():
    units, _ = prepare_timeline({"segments": []}, 0.1)
    assert add_evidence(units, {"events": []}) == [dict(units[0], sound_events=[])]


@pytest.mark.parametrize("language", [None, "de", "en"])
def test_chunk_language_hint_reaches_every_chunk(tmp_path, monkeypatch, language):
    from app.core.ai_services import Transcriber
    from app.core.audio import AudioProcessor

    paths = [str(tmp_path / f"{i}.wav") for i in range(2)]

    def chunks(*args, **kwargs):
        for path in paths:
            Path(path).touch()
        return paths

    monkeypatch.setattr(AudioProcessor, "create_audio_chunks", chunks)
    monkeypatch.setattr(AudioProcessor, "prepare_for_transcription", lambda *a, **k: None)
    seen = []

    def transcribe(path, **kwargs):
        seen.append(kwargs["language"])
        return iter([]), SimpleNamespace(language="de")

    worker = Transcriber()
    worker.model = SimpleNamespace(transcribe=transcribe)
    result = worker._transcribe_chunked(str(tmp_path / "source.mp3"), 1300, language=language)
    assert seen == [language, language]
    assert result["language"] == (language or "de")


def test_moonshine_dispatch_unloads_whisper_and_forwards_hint(monkeypatch):
    from app.core import moonshine_transcription
    from app.core.ai_services import Transcriber

    monkeypatch.setenv("TRANSCRIPTION_ENGINE", "moonshine")
    worker = Transcriber()
    calls = []
    monkeypatch.setattr(worker, "unload_model", lambda: calls.append("unloaded"))

    def transcribe(path, language, models_dir, callback):
        assert calls == ["unloaded"]
        assert path == "audio.mp3" and language == "de"
        return {"segments": [], "language": language}

    monkeypatch.setattr(moonshine_transcription, "transcribe", transcribe)
    assert worker.transcribe("audio.mp3", language="de")["language"] == "de"


def test_classifier_cache_changes_with_acoustic_evidence():
    from app.core.timeline import cache_key

    snapshot = dict(prompt="prompt", prompt_version=1, schema_version=1, settings={})
    plain = {"segments": []}
    enriched = dict(plain, sound_evidence={"version": "yamnet-1", "events": []})
    assert cache_key("same-source", plain, 10, snapshot) != cache_key(
        "same-source", enriched, 10, snapshot
    )
