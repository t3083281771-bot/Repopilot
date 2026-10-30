from repopilot.config import Settings
from repopilot.evaluation import percentile, score_hits
from repopilot.models import Chunk
from repopilot.store import Store


def test_restart_marks_jobs_interrupted_and_keeps_events(tmp_path):
    settings = Settings(data_dir=tmp_path)
    first = Store(settings)
    repo = first.create_repo("demo")
    task = first.create_task(repo["id"], "question")
    first.update_task(task["id"], status="running")
    event_id = first.add_event(task["id"], {"node": "retrieve"})
    second = Store(settings)
    second.mark_interrupted()
    assert second.get_task(task["id"])["status"] == "failed"
    assert second.get_repo(repo["id"])["status"] == "failed"
    assert second.get_events(task["id"], 0)[0]["id"] == event_id
    assert second.get_events(task["id"], event_id) == []


def test_unique_file_scoring_and_percentiles():
    def chunk(i, path):
        return Chunk(id=str(i), repo_id="r", path=path, symbol="foo", start_line=1, end_line=1, content="x")
    candidates = [chunk(i, "noise.py") for i in range(10)] + [chunk(11, "target.py")]
    score = score_hits(candidates, {"files": ["target.py", "other.py"], "symbols": ["foo"]})
    assert score["file_hit_at_5"] == 1
    assert score["file_recall_at_5"] == .5
    assert score["symbol_hit_at_5"] == 1
    assert percentile([1, 2, 3], .5) == 2
