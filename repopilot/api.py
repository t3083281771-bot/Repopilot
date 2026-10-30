"""HTTP service and durable task lifecycle. Imported repository code is never run."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import re
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from . import __version__
from .config import ROOT, Settings
from .store import Store


class RepoInput(BaseModel):
    source: str = Field(min_length=1, max_length=400)
    ref: str = Field(default="HEAD", min_length=1, max_length=128)


class TaskInput(BaseModel):
    repo_id: str
    question: str = Field(min_length=3, max_length=12000)
    strategy: Literal["bm25", "vector", "hybrid", "hybrid_rerank"] = "hybrid"
    mode: Literal["single", "agent"] = "agent"


class SearchInput(BaseModel):
    repo_id: str
    query: str = Field(min_length=1, max_length=12000)
    strategy: Literal["bm25", "vector", "hybrid", "hybrid_rerank"] = "hybrid"
    k: int = Field(default=5, ge=1, le=20)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    store = Store(settings)
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="repopilot")
    active = set()
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        store.mark_interrupted()
        yield
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="RepoPilot", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.settings = settings
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type", "Last-Event-ID"])

    def require_repo(repo_id, ready=False):
        repo = store.get_repo(repo_id)
        if not repo:
            raise HTTPException(404, "仓库不存在")
        if ready and repo["status"] != "ready":
            raise HTTPException(409, "请等待仓库索引完成")
        return repo

    def require_task(task_id):
        task = store.get_task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task

    def public_repo(row):
        return {key: value for key, value in row.items() if key != "snapshot_path"}

    def schedule(key, action):
        with lock:
            if key in active:
                return
            active.add(key)
        def worker():
            try:
                action()
            finally:
                with lock:
                    active.discard(key)
        pool.submit(worker)

    def index_repo(repo):
        from .importer import Importer
        try:
            Importer(settings, store).import_repo(repo["id"], repo["source"], repo["ref"])
        except Exception as exc:
            store.update_repo(repo["id"], status="failed", error=clean_error(exc))

    def clean_error(exc):
        message = str(exc)[:1600]
        for secret in (settings.deepseek_api_key, settings.embedding_api_key, settings.github_token):
            if secret:
                message = message.replace(secret, "[redacted]")
        return message or type(exc).__name__

    def run_task(task, resume=False):
        from .graph import AgentRunner
        store.update_task(task["id"], status="running", error=None)
        store.add_event(task["id"], {"node": "start", "message": "正在恢复任务" if resume else "开始问题定位"})
        try:
            result = AgentRunner(settings, store).run(task["id"], task["repo_id"], task["question"],
                strategy=task["strategy"], mode=task["mode"], resume=resume,
                emit=lambda event: store.add_event(task["id"], event))
            store.update_task(task["id"], status="completed", result=result, error=None)
        except Exception as exc:
            error = clean_error(exc)
            store.add_event(task["id"], {"node": "error", "message": error})
            store.update_task(task["id"], status="failed", error=error)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "provider": settings.provider,
                "model": settings.deepseek_model if settings.provider == "deepseek" else "deterministic-demo",
                "model_configured": bool(settings.deepseek_api_key) if settings.provider == "deepseek" else True,
                "embedding_provider": settings.embedding_provider}

    @app.get("/api/repos")
    def repos():
        return [public_repo(row) for row in store.list_repos()]

    @app.post("/api/repos", status_code=202)
    def import_repo(body: RepoInput):
        source = body.source.strip().rstrip("/")
        if source != "demo" and not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", source):
            raise HTTPException(422, "请输入公开 GitHub 仓库地址，例如 https://github.com/pallets/click")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", body.ref) or ".." in body.ref or body.ref.startswith("-"):
            raise HTTPException(422, "分支、标签或提交版本格式无效")
        repo = store.create_repo(source, body.ref)
        if repo["status"] in {"queued", "failed"}:
            store.update_repo(repo["id"], status="queued", error=None)
            frozen = {**repo, "ref": repo.get("revision") or repo["ref"]}
            schedule("repo:" + repo["id"], lambda: index_repo(frozen))
        return public_repo(store.get_repo(repo["id"]))

    @app.get("/api/repos/{repo_id}")
    def repo(repo_id: str):
        return public_repo(require_repo(repo_id))

    @app.post("/api/repos/{repo_id}/retry", status_code=202)
    def retry_repo(repo_id: str):
        repo = require_repo(repo_id)
        if repo["status"] in {"running", "queued"}:
            raise HTTPException(409, "索引任务仍在进行")
        store.update_repo(repo_id, status="queued", error=None)
        # A re-index may change embeddings, but must preserve the source snapshot
        # used by existing tasks and citations even when the original ref was HEAD.
        frozen = {**repo, "ref": repo.get("revision") or repo["ref"]}
        schedule("repo:" + repo_id, lambda: index_repo(frozen))
        return public_repo(store.get_repo(repo_id))

    @app.get("/api/repos/{repo_id}/file")
    def read_file(repo_id: str, path: str, start_line: int = 1, end_line: int = 100):
        require_repo(repo_id, ready=True)
        if start_line < 1 or end_line < start_line or end_line - start_line >= 200:
            raise HTTPException(422, "每次最多读取 200 行有效代码")
        from .retrieval import Retriever
        try:
            return Retriever(settings, store).read_file(repo_id, path, start_line, end_line)
        except (ValueError, FileNotFoundError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/search")
    def search(body: SearchInput):
        require_repo(body.repo_id, ready=True)
        from .retrieval import Retriever
        try:
            chunks = Retriever(settings, store).search(body.repo_id, body.query, body.strategy, body.k)
            return [c.model_dump(exclude={"embedding"}) for c in chunks]
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/tasks", status_code=202)
    def create_task(body: TaskInput):
        require_repo(body.repo_id, ready=True)
        if not body.question.strip():
            raise HTTPException(422, "请输入问题描述")
        if settings.provider == "deepseek" and not settings.deepseek_api_key:
            raise HTTPException(422, "请先在 .env 配置 DEEPSEEK_API_KEY 并重启服务")
        task = store.create_task(body.repo_id, body.question.strip(), body.strategy, body.mode)
        schedule("task:" + task["id"], lambda: run_task(task))
        return task

    @app.get("/api/tasks")
    def tasks(repo_id: str | None = None):
        return store.list_tasks(repo_id)

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        return require_task(task_id)

    @app.post("/api/tasks/{task_id}/resume", status_code=202)
    def resume_task(task_id: str):
        task = require_task(task_id)
        if task["status"] != "failed":
            raise HTTPException(409, "只有失败或中断的任务可以恢复")
        require_repo(task["repo_id"], ready=True)
        store.update_task(task_id, status="queued", error=None)
        schedule("task:" + task_id, lambda: run_task(task, resume=True))
        return store.get_task(task_id)

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0):
        require_task(task_id)
        try:
            after = max(0, after, int(request.headers.get("last-event-id", "0")))
        except ValueError as exc:
            raise HTTPException(422, "无效事件编号") from exc
        async def stream():
            cursor = after
            ticks = 0
            while not await request.is_disconnected():
                records = store.get_events(task_id, cursor)
                for record in records:
                    cursor = record["id"]
                    payload = {**record["data"], "id": cursor, "created_at": record["created_at"]}
                    yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                current = store.get_task(task_id)
                if current["status"] in {"completed", "failed"} and len(records) < 500:
                    # The final event may have committed between the two reads.
                    # Terminal task status is written only after its final event.
                    pending = store.get_events(task_id, cursor)
                    if pending:
                        for record in pending:
                            cursor = record["id"]
                            payload = {**record["data"], "id": cursor, "created_at": record["created_at"]}
                            yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        continue
                    payload = {"task_id": task_id, "status": current["status"]}
                    yield f"event: done\ndata: {json.dumps(payload)}\n\n"
                    break
                ticks += 1
                if ticks % 30 == 0:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.2)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/evaluations")
    def evaluations():
        directory = settings.data_dir / "evaluations"
        runs = []
        if directory.exists():
            for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    runs.append({"filename": path.name, **data})
                except (OSError, ValueError):
                    continue
        return {"runs": runs, "note": "演示集与公开仓库评测分别标注；hash 向量属于词法演示。"}

    @app.get("/api/evaluations/{filename}")
    def evaluation(filename: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.json", filename):
            raise HTTPException(400, "无效报告名")
        path = settings.data_dir / "evaluations" / filename
        if not path.is_file():
            raise HTTPException(404, "报告不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    web_dist = ROOT / "apps" / "web" / "dist"
    if (web_dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        if (web_dist / "index.html").exists():
            return FileResponse(web_dist / "index.html")
        return {"service": "RepoPilot", "docs": "/docs", "frontend": "Run npm install && npm run build in apps/web"}

    return app
