import json

import httpx
import pytest

from repopilot.config import Settings
from repopilot.providers import DeepSeekProvider, DemoProvider, ProviderError, make_provider


def settings(tmp_path, **kwargs):
    return Settings(data_dir=tmp_path, provider="deepseek", deepseek_api_key="test-only-not-a-real-key", **kwargs)


def reply(data, *, prompt=11, completion=7):
    return {"choices": [{"message": {"content": json.dumps(data)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


def test_missing_key_never_silently_falls_back(tmp_path):
    with pytest.raises(ProviderError, match="DEEPSEEK_API_KEY"):
        make_provider(Settings(data_dir=tmp_path, provider="deepseek"))


def test_deepseek_structured_payload_and_usage(tmp_path):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply({"action": "report"}))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = DeepSeekProvider(settings(tmp_path), client=client)
        result = provider.complete("assess", "question", [])
    assert result.data == {"action": "report"}
    assert result.usage.prompt_tokens == 11
    assert result.usage.completion_tokens == 7
    assert result.usage.provider_calls == 1
    assert result.usage.estimated_calls == 0
    assert requests[0]["model"] == "deepseek-flash"
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["max_tokens"] == 1600


def test_transient_retry_counts_failed_usage(tmp_path, monkeypatch):
    monkeypatch.setattr("repopilot.providers.time.sleep", lambda _: None)
    attempts = []

    def handle(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, json={"usage": {"prompt_tokens": 3, "completion_tokens": 0}})
        return httpx.Response(200, json=reply({"action": "report"}))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = DeepSeekProvider(settings(tmp_path), client=client).complete("assess", "question", [])
    assert len(attempts) == 2
    assert result.usage.prompt_tokens == 14
    assert result.usage.completion_tokens == 7
    assert result.usage.failed_provider_calls == 1
    assert result.usage.provider_calls == 2


def test_auth_failure_is_not_retried_or_leaked(tmp_path, monkeypatch):
    monkeypatch.setattr("repopilot.providers.time.sleep", lambda _: pytest.fail("must not retry 401"))
    attempts = []

    def handle(request):
        attempts.append(request)
        return httpx.Response(401, json={"error": "test-only-not-a-real-key"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as caught:
            DeepSeekProvider(settings(tmp_path), client=client).complete("assess", "question", [])
    assert len(attempts) == 1
    assert "401" in str(caught.value)
    assert "test-only" not in str(caught.value)
    assert caught.value.usage.estimated_calls == 1
    assert caught.value.usage.failed_provider_calls == 1


def test_bad_json_keeps_billable_usage_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("repopilot.providers.time.sleep", lambda _: pytest.fail("invalid JSON is not transient"))

    def handle(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "bad json"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 4}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as caught:
            DeepSeekProvider(settings(tmp_path), client=client).complete("report", "question", [])
    assert caught.value.usage.prompt_tokens == 9
    assert caught.value.usage.completion_tokens == 4
    assert caught.value.usage.failed_provider_calls == 1


def test_transport_retries_are_bounded_and_estimates_labelled(tmp_path, monkeypatch):
    monkeypatch.setattr("repopilot.providers.time.sleep", lambda _: None)
    attempts = []

    def handle(request):
        attempts.append(request)
        raise httpx.ReadTimeout("private transport details", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as caught:
            DeepSeekProvider(settings(tmp_path), client=client).complete("report", "question", [])
    assert len(attempts) == 3
    assert caught.value.usage.provider_calls == 3
    assert caught.value.usage.failed_provider_calls == 3
    assert caught.value.usage.estimated_calls == 3
    assert caught.value.usage.prompt_tokens > 0
    assert "private transport" not in str(caught.value)


def test_demo_is_explicit_and_deterministic():
    provider = DemoProvider()
    first = provider.complete("report", "question", [])
    second = provider.complete("report", "question", [])
    assert first.data == second.data
    assert "演示模式" in first.data["summary"]
    assert first.usage.provider_calls == 0
