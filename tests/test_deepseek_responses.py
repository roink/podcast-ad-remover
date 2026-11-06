from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core import timeline
from app.core.ai_services import AnalysisError, OpenAIProvider


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://api.deepseek.com", True),
        ("https://api.deepseek.com/v1/", True),
        ("https://api.deepseek.com/beta", False),
        ("https://api.deepseek.com.example.org", False),
        ("https://openrouter.ai/api/v1", False),
        ("http://localhost:11434/v1", False),
        (None, False),
    ],
)
def test_responses_selected_only_for_official_deepseek(url, expected):
    provider = OpenAIProvider("fixture", ["deepseek-flash"], base_url=url)
    try:
        assert provider.use_responses is expected
    finally:
        provider.client.close()


@pytest.mark.parametrize("schema", [timeline.SCHEMA, timeline.SUMMARY_SCHEMA])
def test_deepseek_schema_and_usage(monkeypatch, schema):
    metrics = {}

    @contextmanager
    def budget(*args, **kwargs):
        yield metrics

    monkeypatch.setattr("app.core.ai_services.provider_request", budget)
    provider = OpenAIProvider("fixture", ["deepseek-flash"], base_url="https://api.deepseek.com")
    provider.client.close()
    provider.client = Mock()
    provider.client.responses.create.return_value = SimpleNamespace(
        status="completed",
        output=[],
        output_text='{"summary":"example"}',
        usage=SimpleNamespace(input_tokens=123, output_tokens=45),
    )
    messages = [{"role": "user", "content": "Synthetic source"}]
    assert provider.generate_structured(messages, schema) == '{"summary":"example"}'
    request = provider.client.responses.create.call_args.kwargs
    assert request == {
        "model": "deepseek-flash",
        "input": messages,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "podcast_classification",
                "strict": True,
                "schema": schema,
            }
        },
    }
    assert metrics == {"input_tokens": 123, "output_tokens": 45}
    assert provider.last_output_mode == "json_schema"
    provider.client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize(
    "status,text,output",
    [
        ("incomplete", "{}", []),
        ("failed", "{}", []),
        ("completed", "  ", []),
        ("completed", "{}", [SimpleNamespace(content=[SimpleNamespace(type="refusal")])]),
    ],
)
def test_unusable_responses_fail_without_fallback(status, text, output):
    provider = OpenAIProvider("fixture", ["deepseek-flash"], base_url="https://api.deepseek.com")
    provider.client.close()
    provider.client = Mock()
    provider.client.responses.create.return_value = SimpleNamespace(
        status=status, output=output, output_text=text, usage=None
    )
    with pytest.raises(AnalysisError):
        provider.generate_structured([{"role": "user", "content": "Source"}], timeline.SCHEMA)
    assert provider.last_model is None
    provider.client.chat.completions.create.assert_not_called()
