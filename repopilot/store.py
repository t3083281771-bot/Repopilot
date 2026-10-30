"""Small SQLAlchemy store: SQLite locally, PostgreSQL/pgvector in Compose."""
from datetime import datetime, timezone
import math
import uuid

from sqlalchemy import (JSON, Column, ForeignKey, Integer, MetaData, String,
                        Table, Text, create_engine, delete, insert, select, update)
from pgvector.sqlalchemy import Vector

from .models import Chunk


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, settings):
        self.settings = settings
        url = settings.database_url
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}
                                    if url.startswith("sqlite") else {}, pool_pre_ping=True)
        self.is_postgres = self.engine.dialect.name == "postgresql"
        self.meta = MetaData()
        self.repos = Table("repositories", self.meta,
            Column("id", String(40), primary_key=True), Column("source", Text, nullable=False),
            Column("ref", Text, default="HEAD"), Column("revision", Text, default=""),
            Column("snapshot_path", Text, default=""), Column("status", String(20), default="queued"),
            Column("files_count", Integer, default=0), Column("chunks_count", Integer, default=0),
            Column("error", Text), Column("embedding_profile", Text, default=""),
            Column("created_at", Text), Column("updated_at", Text))
        self.chunks = Table("chunks", self.meta,
            Column("id", String(80), primary_key=True),
            Column("repo_id", String(40), ForeignKey("repositories.id"), index=True),
            Column("path", Text), Column("symbol", Text), Column("start_line", Integer),
            Column("end_line", Integer), Column("content", Text), Column("kind", String(20)),
            Column("embedding", Vector().with_variant(JSON(), "sqlite")))
        self.tasks = Table("tasks", self.meta,
            Column("id", String(40), primary_key=True), Column("repo_id", String(40), index=True),
            Column("question", Text), Column("strategy", String(30)), Column("mode", String(20)),
            Column("status", String(20)), Column("result", JSON), Column("error", Text),
            Column("created_at", Text), Column("updated_at", Text))
        self.events = Table("task_events", self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("task_id", String(40), index=True), Column("data", JSON), Column("created_at", Text))
        if self.is_postgres:
            from sqlalchemy import text
            with self.engine.begin() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        self.meta.create_all(self.engine)
        if not self.is_postgres:
            from sqlalchemy import text
            with self.engine.begin() as connection:
                connection.execute(text("PRAGMA journal_mode=WAL"))

    def _one(self, table, record_id):
        with self.engine.connect() as connection:
            row = connection.execute(select(table).where(table.c.id == record_id)).mappings().first()
            return dict(row) if row else None

    def create_repo(self, source, ref="HEAD"):
        with self.engine.begin() as connection:
            existing = connection.execute(select(self.repos).where(
                (self.repos.c.source == source) & (self.repos.c.ref == ref)
            ).order_by(self.repos.c.created_at.desc())).mappings().first()
            if existing:
                return dict(existing)
            row = dict(id=uuid.uuid4().hex, source=source, ref=ref, revision="", snapshot_path="", embedding_profile="",
                       status="queued", files_count=0, chunks_count=0, error=None,
                       created_at=now(), updated_at=now())
            connection.execute(insert(self.repos).values(**row))
            return row

    def get_repo(self, repo_id):
        return self._one(self.repos, repo_id)

    def list_repos(self):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                select(self.repos).order_by(self.repos.c.created_at.desc())).mappings()]

    def update_repo(self, repo_id, **fields):
        fields["updated_at"] = now()
        with self.engine.begin() as connection:
            connection.execute(update(self.repos).where(self.repos.c.id == repo_id).values(**fields))
        return self.get_repo(repo_id)

    def save_chunks(self, repo_id, chunks):
        records = [{key: value for key, value in c.model_dump().items() if key != "score"} for c in chunks]
        with self.engine.begin() as connection:
            connection.execute(delete(self.chunks).where(self.chunks.c.repo_id == repo_id))
            if records:
                connection.execute(insert(self.chunks), records)

    def _chunk(self, row):
        data = dict(row)
        vector = data.get("embedding")
        data["embedding"] = list(vector) if vector is not None else []
        return Chunk(**data)

    def get_chunks(self, repo_id):
        with self.engine.connect() as connection:
            return [self._chunk(row) for row in connection.execute(
                select(self.chunks).where(self.chunks.c.repo_id == repo_id)
                .order_by(self.chunks.c.path, self.chunks.c.start_line)).mappings()]

    def get_chunk(self, chunk_id):
        row = self._one(self.chunks, chunk_id)
        return self._chunk(row) if row else None

    def vector_search(self, repo_id, vector, k):
        if self.is_postgres:
            distance = self.chunks.c.embedding.cosine_distance(vector)
            query = select(self.chunks, (1 - distance).label("score")).where(
                self.chunks.c.repo_id == repo_id).order_by(distance).limit(k)
            with self.engine.connect() as connection:
                return [self._chunk(row) for row in connection.execute(query).mappings()]
        norm = math.sqrt(sum(v*v for v in vector))
        scored = []
        for chunk in self.get_chunks(repo_id):
            if len(chunk.embedding) != len(vector):
                raise ValueError("Embedding dimensions changed; re-import the repository")
            other = math.sqrt(sum(v*v for v in chunk.embedding))
            score = sum(a*b for a, b in zip(vector, chunk.embedding)) / (norm*other) if norm*other else 0
            scored.append(chunk.model_copy(update={"score": score}))
        return sorted(scored, key=lambda c: (-c.score, c.path, c.start_line))[:k]

    def create_task(self, repo_id, question, strategy="hybrid", mode="agent"):
        row = dict(id=uuid.uuid4().hex, repo_id=repo_id, question=question,
                   strategy=strategy, mode=mode, status="queued", result=None, error=None,
                   created_at=now(), updated_at=now())
        with self.engine.begin() as connection:
            connection.execute(insert(self.tasks).values(**row))
        return row

    def get_task(self, task_id):
        return self._one(self.tasks, task_id)

    def list_tasks(self, repo_id=None):
        query = select(self.tasks).order_by(self.tasks.c.created_at.desc()).limit(100)
        if repo_id:
            query = query.where(self.tasks.c.repo_id == repo_id)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def update_task(self, task_id, **fields):
        fields["updated_at"] = now()
        with self.engine.begin() as connection:
            connection.execute(update(self.tasks).where(self.tasks.c.id == task_id).values(**fields))
        return self.get_task(task_id)

    def add_event(self, task_id, data):
        with self.engine.begin() as connection:
            result = connection.execute(insert(self.events).values(task_id=task_id, data=data, created_at=now()))
            return result.inserted_primary_key[0]

    def get_events(self, task_id, after=0):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(self.events).where(
                (self.events.c.task_id == task_id) & (self.events.c.id > after))
                .order_by(self.events.c.id).limit(500)).mappings()]

    def mark_interrupted(self):
        """A stopped process cannot leave an eternal running spinner."""
        with self.engine.begin() as connection:
            connection.execute(update(self.repos).where(self.repos.c.status.in_(["running", "queued"]))
                .values(status="failed", error="服务重启中断了索引，请点击重试。", updated_at=now()))
            connection.execute(update(self.tasks).where(self.tasks.c.status.in_(["running", "queued"]))
                .values(status="failed", error="服务重启中断了任务，可从保存状态恢复。", updated_at=now()))
