import json

import pytest

from repopilot.config import Settings
from repopilot.graph import AgentRunner
from repopilot.models import Chunk
from repopilot.providers import ProviderError, ProviderResponse, Usage


class FakeStore:
    def __init__(self):
        self.chunk = Chunk(id="known", repo_id="repo", path="src/prices.py", symbol="calculate", start_line=3, end_line=5, content="def calculate(x):\n    return x / 0\n")
        self.revision = "abc123"

    def get_repo(self, repo_id):
        return {"id": "repo", "status": "ready", "revision": self.revision} if repo_id == "repo" else None

    def get_chunk(self, chunk_id):
        return self.chunk if chunk_id == self.chunk.id else None


class FakeRetriever:
    def __init__(self, store):
        self.store = store
        self.searches = []
        self.reads = []

    def search(self, repo_id, query, strategy="hybrid", k=5):
        self.searches.append(query)
        return [self.store.chunk]

    def search_symbols(self, repo_id, query, k=5):
        return self.search(repo_id, query, k=k)

    def read_file(self, repo_id, path, start_line=1, end_line=100):
        self.reads.append((path, start_line, end_line))
        return {"path": path, "start_line": start_line, "end_line": end_line, "content": self.store.chunk.content}


class ScriptedProvider:
    name = "demo"
    model = "test-model"

    def __init__(self, script):
        self.script = iter(script)
        self.questions = []

    def complete(self, stage, question, evidence, **context):
        self.questions.append(question)
        value = next(self.script)
        if isinstance(value, Exception):
            raise value
        return ProviderResponse(value, Usage(prompt_tokens=10, completion_tokens=2, provider_calls=1))


def report(ids=None):
    return {"summary": "candidate", "findings": [{"title": "division", "explanation": "Inspect denominator", "chunk_ids": ids or ["known"], "path": "invented.py", "start_line": 999}], "suggestions": [], "uncertainties": []}


def runner(tmp_path, *, max_rounds=3):
    config = Settings(data_dir=tmp_path, max_rounds=max_rounds)
    store = FakeStore()
    result = AgentRunner(config, store)
    result.retriever = FakeRetriever(store)
    return result


def test_citations_are_constructed_from_index_not_model(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([report(["known", "fabricated"])])
    result = agent.run("task", "repo", "why", mode="single")
    citation = result["findings"][0]["citations"][0]
    assert citation["path"] == "src/prices.py"
    assert citation["start_line"] == 3
    assert len(result["findings"][0]["citations"]) == 1
    assert any("无法核验" in item for item in result["uncertainties"])
    assert result["metrics"]["tool_calls"] == 1
    assert result["stop_reason"] == "single_pass"


def test_no_new_evidence_stops_without_infinite_loop(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([{"action": "search", "query": "different query"}, report()])
    result = agent.run("task", "repo", "why")
    assert result["stop_reason"] == "no_new_evidence"
    assert result["metrics"]["rounds"] == 2
    assert result["metrics"]["prompt_tokens"] == 20
    assert agent.retriever.searches == ["why", "different query"]


def test_round_budget_skips_further_planning(tmp_path):
    agent = runner(tmp_path, max_rounds=1)
    agent.provider = ScriptedProvider([report()])
    result = agent.run("task", "repo", "why")
    assert result["stop_reason"] == "round_budget"
    assert result["metrics"]["rounds"] == 1
    assert result["metrics"]["provider_calls"] == 1


def test_read_tool_only_accepts_prior_chunk_and_uses_canonical_path(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([
        {"action": "read", "chunk_id": "known", "path": "../../secret"},
        {"action": "report"}, report(),
    ])
    events = []
    result = agent.run("task", "repo", "why", emit=events.append)
    assert result["stop_reason"] == "evidence_sufficient"
    assert len(agent.retriever.reads) == 1
    assert agent.retriever.reads[0][0] == "src/prices.py"
    assert any(item["node"] == "route" for item in events)
    assert any(item["node"] == "complete" for item in events)


def test_unapproved_tool_is_not_dispatched(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([{"action": "shell", "query": "arbitrary command"}, report()])
    result = agent.run("task", "repo", "why")
    assert result["stop_reason"] == "invalid_action"
    assert result["metrics"]["tool_calls"] == 1


def test_recovery_across_runner_instances_preserves_evidence_and_failed_usage(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([
        {"action": "report"},
        ProviderError("temporary failure", Usage(7, 1, provider_calls=1, failed_provider_calls=1)),
    ])
    with pytest.raises(RuntimeError, match="temporary failure"):
        agent.run("task", "repo", "original question")
    recovered = runner(tmp_path)
    recovered.provider = ScriptedProvider([report()])
    result = recovered.run("task", "repo", "replacement must not be used", resume=True)
    assert recovered.retriever.searches == []
    assert recovered.provider.questions == ["original question"]
    assert result["metrics"]["prompt_tokens"] == 27
    assert result["metrics"]["completion_tokens"] == 5
    assert result["metrics"]["failed_provider_calls"] == 1
    assert result["metrics"]["provider_calls"] == 3
    assert result["metrics"]["tool_calls"] == 1
    assert result["findings"][0]["citations"][0]["chunk_id"] == "known"
    assert (tmp_path / "checkpoints.sqlite3").is_file()


def test_completed_resume_does_not_call_provider_again(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([report()])
    original = agent.run("task", "repo", "why", mode="single")
    recovered = runner(tmp_path)
    recovered.provider = ScriptedProvider([])
    restored = recovered.run("task", "repo", "why", resume=True)
    assert recovered.provider.questions == []
    assert restored["metrics"]["elapsed_ms"] >= 0
    assert original["metrics"]["elapsed_ms"] >= 0
    # Wall time describes this invocation; persisted content, usage and node
    # time must remain identical when a completed result is loaded again.
    restored["metrics"].pop("elapsed_ms")
    original["metrics"].pop("elapsed_ms")
    assert restored == original


def test_recovery_rejects_changed_repository_revision(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([report()])
    agent.run("task", "repo", "why", mode="single")
    agent.store.revision = "changed"
    with pytest.raises(ValueError, match="版本"):
        agent.run("task", "repo", "why", resume=True)


def test_entirely_fabricated_citations_remove_claims(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([report(["fabricated"])])
    result = agent.run("task", "repo", "why", mode="single")
    assert result["findings"] == []
    assert "未生成" in result["summary"]


def test_malformed_action_still_preserves_model_usage(tmp_path):
    agent = runner(tmp_path)
    agent.provider = ScriptedProvider([{"action": ["shell"]}, report()])
    result = agent.run("task", "repo", "why")
    assert result["stop_reason"] == "invalid_action"
    assert result["metrics"]["provider_calls"] == 2


def test_model_context_respects_serialized_budget_with_escaped_content(tmp_path):
    agent = runner(tmp_path)
    agent.settings.max_context_chars = 500
    evidence = agent.store.chunk.model_dump()
    evidence["content"] = '\n"\\' * 1000
    packed, reads = agent._model_context({"evidence": [evidence], "reads": []})
    assert packed
    assert len(json.dumps(packed, ensure_ascii=False)) + len(json.dumps(reads, ensure_ascii=False)) <= 502
