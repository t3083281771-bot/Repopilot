"""Regressions for fixed source revisions and terminal SSE event delivery."""

import sys
import threading
import time

from fastapi.testclient import TestClient

from repopilot.api import create_app
from repopilot.cli import main
from repopilot.config import Settings
from repopilot.importer import Importer
from repopilot.providers import DemoProvider
from repopilot.store import Store


def test_reposting_failed_repository_preserves_previously_resolved_commit(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    captured = []
    finished = threading.Event()

    def fake_import(importer, repo_id, source, ref="HEAD"):
        captured.append(ref)
        result = importer.store.update_repo(repo_id, status="ready")
        finished.set()
        return result

    monkeypatch.setattr(Importer, "import_repo", fake_import)
    with TestClient(app) as client:
        store = app.state.store
        row = store.create_repo("https://github.com/owner/repo", "HEAD")
        store.update_repo(row["id"], revision="a" * 40, status="failed")
        response = client.post("/api/repos", json={"source": row["source"], "ref": "HEAD"})
        assert response.status_code == 202
        assert finished.wait(5), "Background reindex was not scheduled"
        assert captured == ["a" * 40]


def test_explicit_retry_preserves_previously_resolved_commit(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    captured = []
    finished = threading.Event()

    def fake_import(importer, repo_id, source, ref="HEAD"):
        captured.append(ref)
        result = importer.store.update_repo(repo_id, status="ready")
        finished.set()
        return result

    monkeypatch.setattr(Importer, "import_repo", fake_import)
    with TestClient(app) as client:
        store = app.state.store
        row = store.create_repo("https://github.com/owner/repo", "HEAD")
        store.update_repo(row["id"], revision="a" * 40, status="failed")
        response = client.post(f"/api/repos/{row['id']}/retry")
        assert response.status_code == 202
        assert finished.wait(5), "Background reindex was not scheduled"
        assert captured == ["a" * 40]


def test_cli_reimport_of_failed_repository_preserves_existing_commit(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    store = Store(settings)
    row = store.create_repo("https://github.com/owner/repo", "HEAD")
    store.update_repo(row["id"], revision="a" * 40, status="failed")
    captured = []

    def fake_import(importer, repo_id, source, ref="HEAD"):
        captured.append(ref)
        return importer.store.update_repo(repo_id, status="ready")

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(Importer, "import_repo", fake_import)
    monkeypatch.setattr(sys, "argv", ["repopilot", "import", row["source"]])
    main()
    assert captured == ["a" * 40]


def test_sse_drains_event_committed_between_event_query_and_terminal_status(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        store = app.state.store
        repo = store.create_repo("demo")
        store.update_repo(repo["id"], status="ready", revision="a" * 40)
        task = store.create_task(repo["id"], "Where is the problem?")
        store.update_task(task["id"], status="running")
        get_events = store.get_events
        inserted = False

        def query_then_finish(task_id, after=0):
            nonlocal inserted
            records = get_events(task_id, after)
            if not inserted:
                inserted = True
                store.add_event(task_id, {"node": "complete", "message": "final persisted progress event"})
                store.update_task(task_id, status="completed", result={})
            return records

        monkeypatch.setattr(store, "get_events", query_then_finish)
        response = client.get(f"/api/tasks/{task['id']}/events")
        assert response.status_code == 200
        assert "final persisted progress event" in response.text
        assert response.text.index("event: progress") < response.text.index("event: done")


def test_api_resume_of_interrupted_queued_task_restarts_original_input_without_checkpoint(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    store = app.state.store
    row = store.create_repo("demo")
    repo = Importer(settings, store).import_repo(row["id"], "demo")
    question = "calculate_total quantity=0 为什么仍然收费？"
    task = store.create_task(repo["id"], question, strategy="bm25", mode="single")
    observed_questions = []

    class RecordingDemo(DemoProvider):
        def complete(self, stage, original_question, evidence, **context):
            observed_questions.append(original_question)
            return super().complete(stage, original_question, evidence, **context)

    monkeypatch.setattr("repopilot.graph.make_provider", lambda config: RecordingDemo())
    # Entering lifespan marks the durable queued record as interrupted. No graph
    # was ever invoked, so this task intentionally has no checkpoint to resume.
    with TestClient(app) as client:
        assert client.get(f"/api/tasks/{task['id']}").json()["status"] == "failed"
        response = client.post(f"/api/tasks/{task['id']}/resume", json={"question": "replacement must be ignored"})
        assert response.status_code == 202
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            completed = client.get(f"/api/tasks/{task['id']}").json()
            if completed["status"] in {"completed", "failed"}:
                break
            time.sleep(0.03)
        assert completed["status"] == "completed", completed
        assert completed["question"] == question
        assert observed_questions == [question]
        assert completed["result"]["repository"] == {"id": repo["id"], "revision": repo["revision"]}
        events = client.get(f"/api/tasks/{task['id']}/events").text
        assert '"node": "restart"' in events
        assert "event: done" in events
