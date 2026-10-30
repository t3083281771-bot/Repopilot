from pathlib import Path

import pytest

from repopilot.config import Settings
from repopilot.importer import Importer
from repopilot.retrieval import Retriever
from repopilot.store import Store


@pytest.fixture
def repository(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "tags.py").write_text(
        "def normalize_tags(values):\n    return [value.strip().lower() for value in values]\n", encoding="utf-8",
    )
    (source / "db.py").write_text("def connect_database(url):\n    return {'database': url}\n", encoding="utf-8")
    (source / "long.md").write_text("# Guide\n" + "\n".join(f"line {index}" for index in range(300)), encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "runtime", allow_local_import=True)
    store = Store(settings)
    row = store.create_repo(str(source))
    ready = Importer(settings, store).import_repo(row["id"], str(source))
    return settings, store, ready, Retriever(settings, store)


@pytest.mark.parametrize("strategy", ["bm25", "vector", "hybrid", "hybrid_rerank"])
def test_search_locates_symbol_and_does_not_return_embedding_arrays(repository, strategy):
    _, _, row, retriever = repository
    results = retriever.search(row["id"], "normalize_tags", strategy, k=3)
    assert results and results[0].symbol == "normalize_tags"
    assert all(result.embedding == [] for result in results)


def test_symbol_search_and_read_file_use_exact_source_positions(repository):
    _, _, row, retriever = repository
    evidence = retriever.search_symbols(row["id"], "where is normalize_tags defined?")
    assert evidence[0].path == "tags.py"
    result = retriever.read_file(row["id"], "tags.py", 1, 2)
    assert result["content"].startswith("def normalize_tags")
    assert (result["start_line"], result["end_line"]) == (1, 2)


def test_read_file_bounds_and_path_traversal(repository):
    _, _, row, retriever = repository
    assert retriever.read_file(row["id"], "long.md", 1, 9999)["end_line"] == 200
    for path in ("../outside.py", "/etc/passwd", "C:/secret.py", "..\\outside.py", ".env", "missing.py"):
        with pytest.raises(ValueError):
            retriever.read_file(row["id"], path)
    with pytest.raises(ValueError, match="outside"):
        retriever.read_file(row["id"], "tags.py", 500, 600)


def test_retrieval_does_not_accept_unready_repository(repository):
    _, store, row, retriever = repository
    store.update_repo(row["id"], status="failed")
    with pytest.raises(ValueError, match="not ready"):
        retriever.search(row["id"], "normalize_tags", "bm25")


def test_unknown_strategy_and_invalid_k_are_rejected(repository):
    _, _, row, retriever = repository
    with pytest.raises(ValueError, match="strategy"):
        retriever.search(row["id"], "query", "unknown")
    with pytest.raises(ValueError, match="k must"):
        retriever.search(row["id"], "query", k=0)


def test_same_dimension_model_switch_requires_reimport_but_bm25_still_works(repository):
    settings, _, row, retriever = repository
    assert row["embedding_profile"] == "hash:hash-v1-384"
    settings.embedding_provider = "sentence-transformers"
    settings.embedding_model = "a-different-384-dimensional-model"
    # The profile guard executes before downloading/loading any optional model.
    with pytest.raises(ValueError, match="re-import"):
        retriever.search(row["id"], "normalize_tags", "hybrid")
    assert retriever.search(row["id"], "normalize_tags", "bm25")[0].symbol == "normalize_tags"


def test_hash_profile_ignores_unused_semantic_model_setting(repository):
    settings, _, row, retriever = repository
    settings.embedding_model = "changed-but-unused"
    assert retriever.search(row["id"], "normalize_tags", "vector")[0].symbol == "normalize_tags"


def test_legacy_missing_profile_requires_reimport(repository):
    _, store, row, retriever = repository
    store.update_repo(row["id"], embedding_profile="")
    with pytest.raises(ValueError, match="re-import"):
        retriever.search(row["id"], "normalize_tags", "vector")
    assert retriever.search(row["id"], "normalize_tags", "bm25")


def test_read_file_uses_indexed_snapshot_even_if_disk_metadata_is_replaced(repository, tmp_path, monkeypatch):
    _, store, row, retriever = repository
    store.update_repo(row["id"], snapshot_path=str(tmp_path))
    (tmp_path / "tags.py").write_text("private = True", encoding="utf-8")

    def unavailable_disk(*args, **kwargs):
        raise PermissionError("OS snapshot is unavailable")

    monkeypatch.setattr(Path, "read_text", unavailable_disk)
    result = retriever.read_file(row["id"], "tags.py")
    assert result["source"] == "indexed_snapshot"
    assert "normalize_tags" in result["content"]
    assert "private" not in result["content"]


def test_replaced_snapshot_link_does_not_change_indexed_source_read(repository, tmp_path):
    _, _, row, retriever = repository
    target = Path(row["snapshot_path"]) / "tags.py"
    outside = tmp_path / "outside.py"
    outside.write_text("private = True")
    try:
        link = tmp_path / "link-probe"
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symbolic links requires Windows developer mode or privilege")
    link.unlink()
    target.unlink()
    target.symlink_to(outside)
    result = retriever.read_file(row["id"], "tags.py")
    assert result["source"] == "indexed_snapshot"
    assert "normalize_tags" in result["content"]
    assert "private" not in result["content"]


def test_indexed_snapshot_reconstruction_fills_only_blank_gaps(repository):
    _, store, row, retriever = repository
    original = next(chunk for chunk in store.get_chunks(row["id"]) if chunk.path == "tags.py")
    store.save_chunks(row["id"], [
        original.model_copy(update={"id": "first", "start_line": 1, "end_line": 1, "content": "first"}),
        original.model_copy(update={"id": "last", "start_line": 4, "end_line": 4, "content": "last"}),
    ])
    assert retriever.read_file(row["id"], "tags.py")["content"] == "first\n\n\nlast"


def test_indexed_snapshot_overlap_must_be_consistent(repository):
    _, store, row, retriever = repository
    original = next(chunk for chunk in store.get_chunks(row["id"]) if chunk.path == "tags.py")
    first = original.model_copy(update={"id": "first", "start_line": 1, "end_line": 2, "content": "first\nsecond"})
    second = original.model_copy(update={"id": "second", "start_line": 2, "end_line": 3, "content": "second\nthird"})
    store.save_chunks(row["id"], [first, second])
    assert retriever.read_file(row["id"], "tags.py")["content"] == "first\nsecond\nthird"
    second.content = "inconsistent\nthird"
    store.save_chunks(row["id"], [first, second])
    with pytest.raises(ValueError, match="inconsistent overlapping"):
        retriever.read_file(row["id"], "tags.py")


def test_indexed_snapshot_rejects_malformed_declared_line_range(repository):
    _, store, row, retriever = repository
    original = next(chunk for chunk in store.get_chunks(row["id"]) if chunk.path == "tags.py")
    original.end_line += 1
    store.save_chunks(row["id"], [original])
    with pytest.raises(ValueError, match="source line range"):
        retriever.read_file(row["id"], "tags.py")
