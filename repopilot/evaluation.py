"""Reproducible retrieval evaluation. Synthetic and real-Issue data stay labelled."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import time
import uuid

from . import __version__
from .embeddings import embedding_profile
from .importer import Importer
from .retrieval import Retriever


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * q
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def score_hits(chunks, case, k=5):
    """Deduplicate before top-k so overlapping chunks do not inflate hits."""
    files = list(dict.fromkeys(c.path for c in chunks))[:k]
    symbols = list(dict.fromkeys((c.path, c.symbol) for c in chunks))[:k]
    gold_files = set(case["files"])
    gold_symbols = set(case.get("symbols", []))
    file_hits = gold_files.intersection(files)
    symbol_hit = any(symbol in gold_symbols and path in gold_files for path, symbol in symbols)
    return {"file_hit_at_5": float(bool(file_hits)),
            "file_recall_at_5": len(file_hits) / len(gold_files) if gold_files else None,
            "symbol_hit_at_5": float(symbol_hit) if gold_symbols else None,
            "ranked_files": files, "ranked_symbols": [f"{p}:{s}" for p, s in symbols]}


def evaluate_dataset(settings, store, dataset_path, split="test", end_to_end=False):
    path = Path(dataset_path)
    raw = path.read_bytes()
    dataset = json.loads(raw)
    cases = [case for case in dataset["cases"] if split == "all" or case.get("split", "test") == split]
    if not cases:
        raise ValueError("No cases in the requested dataset split")
    importer = Importer(settings, store)
    retriever = Retriever(settings, store)
    rows = []
    index_times = []
    repositories = {}
    for case in cases:
        source = case.get("source", dataset.get("source", "demo"))
        ref = case.get("ref", dataset.get("ref", "HEAD"))
        key = (source, ref)
        if key not in repositories:
            repo = next((row for row in store.list_repos() if row["source"] == source
                         and row["revision"] == ref and row["status"] == "ready"), None)
            if repo is None:
                repo = store.create_repo(source, ref)
            start = time.perf_counter()
            reused = repo["status"] == "ready"
            if not reused:
                repo = importer.import_repo(repo["id"], source, ref)
            index_times.append({"source": source, "revision": repo["revision"],
                                "elapsed_ms": round((time.perf_counter()-start)*1000, 2), "reused": reused})
            repositories[key] = repo
        repo = repositories[key]
        for strategy in ["bm25", "vector", "hybrid", "hybrid_rerank"]:
            start = time.perf_counter()
            # Fetch enough chunks to obtain five unique files/functions fairly.
            hits = retriever.search(repo["id"], case["question"], strategy, k=30)
            elapsed = (time.perf_counter()-start)*1000
            score = score_hits(hits, case)
            rows.append({"case_id": case["id"], "strategy": strategy, "revision": repo["revision"],
                         "elapsed_ms": round(elapsed, 3), **score})
        if end_to_end:
            from .graph import AgentRunner
            for mode in ["single", "agent"]:
                workflow_start = time.perf_counter()
                result = AgentRunner(settings, store).run(uuid.uuid4().hex, repo["id"], case["question"],
                                                          strategy="hybrid", mode=mode)
                wall_ms = (time.perf_counter() - workflow_start) * 1000
                citations = [citation for finding in result["findings"] for citation in finding["citations"]]
                files = list(dict.fromkeys(c["path"] for c in citations))[:5]
                rows.append({"case_id": case["id"], "strategy": "workflow_" + mode,
                    "file_hit_at_5": float(bool(set(files).intersection(case["files"]))),
                    "file_recall_at_5": len(set(files).intersection(case["files"]))/len(case["files"]),
                    "symbol_hit_at_5": None, "ranked_files": files, **result["metrics"],
                    "node_elapsed_ms": result["metrics"].get("node_elapsed_ms", result["metrics"]["elapsed_ms"]),
                    "elapsed_ms": round(wall_ms, 3)})
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["strategy"]].append(row)
    summaries = []
    for strategy, group in grouped.items():
        summary = {"strategy": strategy, "cases": len(group)}
        for metric in ["file_hit_at_5", "file_recall_at_5", "symbol_hit_at_5"]:
            values = [row[metric] for row in group if row.get(metric) is not None]
            summary[metric] = round(sum(values)/len(values), 4) if values else None
        timings = [row.get("elapsed_ms", 0) for row in group]
        summary.update(p50_ms=round(percentile(timings, .5), 3), p95_ms=round(percentile(timings, .95), 3),
                       prompt_tokens=sum(row.get("prompt_tokens", 0) for row in group),
                       completion_tokens=sum(row.get("completion_tokens", 0) for row in group))
        summaries.append(summary)
    report = {"name": dataset["name"], "kind": dataset.get("kind", "unspecified"),
        "created_at": datetime.now(timezone.utc).isoformat(), "split": split, "case_count": len(cases),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(), "project_version": __version__,
        "environment": platform.python_version(), "provider": settings.provider,
        "model": settings.deepseek_model if settings.provider == "deepseek" else "deterministic-demo",
        "embedding_provider": settings.embedding_provider,
        "embedding_model": "hash-v1-384" if settings.embedding_provider == "hash" else settings.embedding_model,
        "embedding_profile": embedding_profile(settings),
        "provenance": dataset.get("provenance", ""), "summaries": summaries, "results": rows,
        "indexing": index_times, "notes": [
            "This measures localization, not issue resolution or patch correctness.",
            "hash embeddings and hybrid_rerank are lexical baselines; no semantic/reranker-model claim.",
            "Indexing latency is separate; retrieval includes query embedding and excludes answer generation.",
            "No dollar cost estimated without provider pricing; usage tokens are reported when available.",
            "Small public benchmark subsets can be present in model training; contamination cannot be excluded."]}
    directory = settings.data_dir / "evaluations"
    directory.mkdir(parents=True, exist_ok=True)
    filename = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".json"
    (directory / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
