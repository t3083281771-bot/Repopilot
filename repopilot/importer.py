"""Read-only repository snapshots and source chunks. Imported code is never executed."""

from __future__ import annotations

import ast
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET
import uuid
import zipfile

import httpx

from .embeddings import embedding_profile, get_embedder
from .models import Chunk

if TYPE_CHECKING:
    from .config import Settings
    from .store import Store


EXCLUDED_DIRS = {
    "node_modules", "venv", "__pycache__", "build", "dist", "site-packages",
    "runtime", "htmlcov", "coverage", "target", "vendor", "snapshots",
}
SECRET_NAMES = {"secrets.py", "credentials.py", "credentials.md", "secrets.md"}
WINDOWS_DEVICES = {"con", "prn", "aux", "nul"} | {
    prefix + suffix for prefix in ("com", "lpt") for suffix in "123456789¹²³"
}
MAX_CHUNK_LINES = 80
CHUNK_OVERLAP = 10


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


@contextmanager
def _snapshot_staging(snapshot_parent: Path):
    """Inherit parent permissions; mkdtemp's 0700 ACL breaks Windows readers."""
    trusted_parent = snapshot_parent.resolve(strict=True)
    if _is_link(snapshot_parent):
        raise ValueError("Snapshot staging parent cannot be a symbolic link")
    staged = trusted_parent / f"import-{uuid.uuid4().hex}"
    # No explicit mode or ACL: retain the snapshot directory's inherited access.
    staged.mkdir()
    try:
        yield staged
    finally:
        try:
            staged.lstat()
        except FileNotFoundError:
            exists = False  # Promoted snapshots have already been renamed away.
        else:
            exists = True
        if exists:
            resolved = staged.resolve()
            if (
                snapshot_parent.resolve(strict=True) != trusted_parent
                or resolved == trusted_parent
                or not resolved.is_relative_to(trusted_parent)
                or _is_link(staged)
            ):
                raise ValueError("Refusing unsafe snapshot staging cleanup")
            shutil.rmtree(staged)


def _allowed_parts(parts: tuple[str, ...]) -> bool:
    return all(
        part and part not in {".", ".."} and not part.startswith(".")
        and part.casefold() not in EXCLUDED_DIRS and ":" not in part
        and part == part.rstrip(". ") and part.split(".", 1)[0].casefold() not in WINDOWS_DEVICES
        for part in parts
    )


def _allowed_file(path: PurePosixPath | Path) -> bool:
    return (
        _allowed_parts(path.parts)
        and path.suffix.casefold() in {".py", ".md"}
        and path.name.casefold() not in SECRET_NAMES
    )


def _source_files(
    root: Path, max_files: int = 1500, max_bytes: int = 15_000_000,
    max_file_bytes: int = 300_000,
) -> list[Path]:
    if min(max_files, max_bytes, max_file_bytes) <= 0:
        raise ValueError("Repository budgets must be positive")
    if _is_link(root):
        raise ValueError("Repository root cannot be a symbolic link")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Repository source must be a directory")
    selected: list[Path] = []
    total_bytes = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        folders[:] = sorted(
            name for name in folders
            if _allowed_parts((name,)) and not _is_link(directory_path / name)
        )
        for name in sorted(files):
            candidate = directory_path / name
            relative = candidate.relative_to(root)
            if not _allowed_file(relative) or _is_link(candidate):
                continue
            if not candidate.resolve(strict=True).is_relative_to(root):
                raise ValueError("Repository file escapes its source directory")
            size = candidate.stat().st_size
            if size > max_file_bytes:
                raise ValueError(f"File exceeds size budget: {relative.as_posix()}")
            total_bytes += size
            selected.append(candidate)
            if len(selected) > max_files or total_bytes > max_bytes:
                raise ValueError("Repository exceeds file or byte budget")
    return selected


def _make_chunks(
    lines: list[str], repo_id: str, path: str, symbol: str,
    start: int, end: int, kind: str = "code",
) -> list[Chunk]:
    result = []
    step = MAX_CHUNK_LINES - CHUNK_OVERLAP
    for first in range(start, end + 1, step):
        last = min(first + MAX_CHUNK_LINES - 1, end)
        content = "\n".join(lines[first - 1:last])
        if content.strip():
            identity = f"{repo_id}\0{path}\0{symbol}\0{first}\0{last}\0{content}"
            result.append(Chunk(
                id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32],
                repo_id=repo_id, path=path, symbol=symbol,
                start_line=first, end_line=last, content=content, kind=kind,
            ))
        if last == end:
            break
    return result


def _definition_start(node: ast.AST) -> int:
    return min([node.lineno] + [item.lineno for item in getattr(node, "decorator_list", [])])


def _python_chunks(text: str, repo_id: str, path: str) -> list[Chunk]:
    lines = text.splitlines()
    if not lines:
        return []
    try:
        module = ast.parse(text)
    except (SyntaxError, RecursionError, ValueError):
        return _make_chunks(lines, repo_id, path, "<module>", 1, len(lines))
    chunks: list[Chunk] = []

    def visit(body: list[ast.stmt], start: int, end: int, parent: str) -> None:
        cursor = start
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            first, last = _definition_start(node), node.end_lineno or node.lineno
            chunks.extend(_make_chunks(lines, repo_id, path, parent or "<module>", cursor, first - 1))
            symbol = f"{parent}.{node.name}" if parent else node.name
            if isinstance(node, ast.ClassDef):
                visit(node.body, first, last, symbol)
            else:
                chunks.extend(_make_chunks(lines, repo_id, path, symbol, first, last))
            cursor = last + 1
        chunks.extend(_make_chunks(lines, repo_id, path, parent or "<module>", cursor, end))

    visit(module.body, 1, len(lines), "")
    return chunks


def _markdown_chunks(text: str, repo_id: str, path: str) -> list[Chunk]:
    lines = text.splitlines()
    chunks: list[Chunk] = []
    start, heading = 1, PurePosixPath(path).stem
    fenced = False
    for line_number, line in enumerate(lines, 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line) if not fenced else None
        if match:
            chunks.extend(_make_chunks(lines, repo_id, path, heading, start, line_number - 1, "markdown"))
            start, heading = line_number, match.group(1)
    chunks.extend(_make_chunks(lines, repo_id, path, heading, start, len(lines), "markdown"))
    return chunks


def chunk_repository(
    path: Path, repo_id: str, *, max_files: int = 1500,
    max_bytes: int = 15_000_000, max_file_bytes: int = 300_000,
) -> list[Chunk]:
    path = Path(path)
    files = _source_files(path, max_files, max_bytes, max_file_bytes)
    root = path.resolve()
    chunks: list[Chunk] = []
    for source in files:
        raw = source.read_bytes()
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8-sig", errors="replace")
        relative = source.relative_to(root).as_posix()
        chunks.extend(
            _python_chunks(text, repo_id, relative) if source.suffix.casefold() == ".py"
            else _markdown_chunks(text, repo_id, relative)
        )
        if len(chunks) > 20_000:
            raise ValueError("Repository exceeds chunk budget")
    return chunks


def _github_source(source: str) -> tuple[str, str]:
    url = urlsplit(source)
    if (
        url.scheme != "https" or url.netloc.casefold() != "github.com"
        or url.query or url.fragment or url.username or url.password
    ):
        raise ValueError("Use an HTTPS public GitHub repository URL: https://github.com/owner/repo")
    parts = url.path.strip("/").split("/")
    if len(parts) != 2:
        raise ValueError("GitHub URL must identify one owner/repository")
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository)
        or repository in {".", ".."}
    ):
        raise ValueError("Invalid GitHub owner or repository")
    return owner, repository


class Importer:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store

    def _budgets(self) -> dict:
        return {
            "max_files": self.settings.max_repo_files,
            "max_bytes": self.settings.max_repo_bytes,
            "max_file_bytes": self.settings.max_file_bytes,
        }

    def _public_atom_revision(self, client: httpx.Client, owner: str, repository: str, ref: str) -> str:
        """Public GitHub feed fallback when the anonymous REST quota is exhausted."""
        if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            # The subsequent unauthenticated codeload request establishes public access.
            return ref.lower()
        url = f"https://github.com/{owner}/{repository}/commits/{quote(ref, safe='')}.atom"
        content = bytearray()
        for redirect in range(4):
            with client.stream(
                "GET", url, headers={"Accept": "application/atom+xml", "User-Agent": "RepoPilot"},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = urljoin(url, response.headers.get("location", ""))
                    parsed = urlsplit(target)
                    if (
                        redirect == 3 or not response.headers.get("location")
                        or parsed.scheme != "https" or parsed.netloc.casefold() != "github.com"
                        or not parsed.path.startswith(f"/{owner}/{repository}/commits/")
                        or parsed.query or parsed.fragment
                    ):
                        raise ValueError("Unsafe redirect in public GitHub commit feed")
                    url = target
                    continue
                response.raise_for_status()
                for part in response.iter_bytes(chunk_size=16 * 1024):
                    content.extend(part)
                    if len(content) > min(self.settings.max_repo_bytes, 512_000):
                        raise ValueError("GitHub commit feed exceeds byte budget")
                break
        namespace = "{http://www.w3.org/2005/Atom}"
        try:
            feed = ET.fromstring(bytes(content))
        except ET.ParseError as exc:
            raise ValueError("GitHub returned an invalid public Atom feed") from exc
        if feed.tag != f"{namespace}feed":
            raise ValueError("GitHub response is not an Atom commit feed")
        identifier = feed.find(f"{namespace}entry/{namespace}id")
        match = re.fullmatch(
            r"tag:github\.com,2008:Grit::Commit/([0-9a-f]{40})",
            identifier.text.strip() if identifier is not None and identifier.text else "",
        )
        if not match:
            raise ValueError("GitHub feed does not contain a valid first commit identifier")
        return match.group(1)

    def _github_archive(self, source: str, ref: str, destination: Path) -> str:
        owner, repository = _github_source(source)
        if not ref or len(ref) > 200 or any(ord(char) < 32 for char in ref):
            raise ValueError("Invalid repository revision")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "RepoPilot"}
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        with httpx.Client(timeout=self.settings.request_timeout, follow_redirects=False) as client:
            try:
                metadata = client.get(f"https://api.github.com/repos/{owner}/{repository}", headers=headers)
                metadata.raise_for_status()
                if metadata.json().get("private", True):
                    raise ValueError("Only public GitHub repositories are supported")
                response = client.get(
                    f"https://api.github.com/repos/{owner}/{repository}/commits/{quote(ref, safe='')}",
                    headers=headers,
                )
                response.raise_for_status()
                revision = response.json().get("sha", "")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {403, 429}:
                    raise
                revision = self._public_atom_revision(client, owner, repository, ref)
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("GitHub returned an invalid commit SHA")
            archive_bytes = bytearray()
            # Never forward the API authorization header to the archive service.
            with client.stream(
                "GET", f"https://codeload.github.com/{owner}/{repository}/zip/{revision}",
                headers={"User-Agent": "RepoPilot"},
            ) as archive:
                archive.raise_for_status()
                for part in archive.iter_bytes(chunk_size=64 * 1024):
                    archive_bytes.extend(part)
                    if len(archive_bytes) > self.settings.max_repo_bytes:
                        raise ValueError("Repository download exceeds byte budget")
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(files) > self.settings.max_repo_files:
                raise ValueError("Repository archive exceeds file budget")
            if sum(entry.file_size for entry in files) > self.settings.max_repo_bytes:
                raise ValueError("Repository archive exceeds expanded byte budget")
            prefixes: set[str] = set()
            for entry in archive.infolist():
                # ZipInfo.filename normalizes backslashes on Windows. Validate
                # orig_filename so the original archive path is checked too.
                name = entry.orig_filename
                relative = PurePosixPath(name)
                mode = entry.external_attr >> 16
                if (
                    relative.is_absolute() or "\\" in name or ".." in relative.parts
                    or any(ord(char) < 32 for char in name)
                    or any(":" in part for part in relative.parts)
                ):
                    raise ValueError(f"Unsafe repository archive path: {name!r}")
                # A GitHub source archive can legitimately contain documentation
                # or certificate links. Never create or dereference these entries.
                if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    continue
                if relative.parts:
                    prefixes.add(relative.parts[0])
                if len(prefixes) > 1:
                    raise ValueError("Repository archive has multiple roots")
                if entry.is_dir() or stat.S_IFMT(mode) == stat.S_IFDIR or len(relative.parts) < 2:
                    continue
                target_relative = PurePosixPath(*relative.parts[1:])
                if not _allowed_file(target_relative):
                    continue
                if entry.file_size > self.settings.max_file_bytes:
                    raise ValueError("Repository file exceeds byte budget")
                target = destination.joinpath(*target_relative.parts)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Archive path escapes snapshot")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source_file, target.open("xb") as target_file:
                    content = source_file.read(self.settings.max_file_bytes + 1)
                    if len(content) > self.settings.max_file_bytes:
                        raise ValueError("Expanded file exceeds byte budget")
                    target_file.write(content)
        return revision

    def _local_snapshot(self, source: Path, destination: Path) -> str:
        source = source.absolute()
        files = _source_files(source, **self._budgets())
        source = source.resolve()
        digest = hashlib.sha256()
        total_bytes = 0
        for path in files:
            relative = path.relative_to(source)
            if _is_link(path) or not path.resolve(strict=True).is_relative_to(source):
                raise ValueError("Repository file changed to an unsafe path during import")
            with path.open("rb") as source_file:
                content = source_file.read(self.settings.max_file_bytes + 1)
            total_bytes += len(content)
            if len(content) > self.settings.max_file_bytes or total_bytes > self.settings.max_repo_bytes:
                raise ValueError("Repository content changed beyond byte budget during import")
            digest.update(relative.as_posix().encode("utf-8") + b"\0" + content + b"\0")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return digest.hexdigest()

    def import_repo(self, repo_id: str, source: str, ref: str = "HEAD") -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", repo_id):
            raise ValueError("Invalid repository ID")
        if not self.store.get_repo(repo_id):
            raise ValueError("Repository not found")
        self.store.update_repo(repo_id, source=source, status="running", error="")
        try:
            snapshot_parent = self.settings.data_dir.resolve()
            for component in ("snapshots", repo_id):
                snapshot_parent = snapshot_parent / component
                if snapshot_parent.exists() and _is_link(snapshot_parent):
                    raise ValueError("Snapshot directory cannot be a symbolic link")
                if not snapshot_parent.resolve().is_relative_to(self.settings.data_dir):
                    raise ValueError("Snapshot directory escapes data directory")
                snapshot_parent.mkdir(exist_ok=True)
            with _snapshot_staging(snapshot_parent) as staged:
                if source == "demo":
                    demo = Path(__file__).resolve().parent.parent / "examples" / "demo_repo"
                    revision = self._local_snapshot(demo, staged)
                elif source.startswith("https://"):
                    revision = self._github_archive(source, ref, staged)
                else:
                    if not self.settings.allow_local_import:
                        raise ValueError("Local repository imports are disabled")
                    if "://" in source:
                        raise ValueError("Unsupported repository URL")
                    revision = self._local_snapshot(Path(source), staged)
                snapshot = snapshot_parent / revision
                if snapshot.exists():
                    if _is_link(snapshot):
                        raise ValueError("Snapshot cannot be a symbolic link")
                else:
                    staged.rename(snapshot)
            chunks = chunk_repository(snapshot, repo_id, **self._budgets())
            if not chunks:
                raise ValueError("Repository contains no supported nonempty Python or Markdown files")
            embedder = get_embedder(self.settings)
            vectors = embedder.embed([f"{chunk.path}\n{chunk.symbol}\n{chunk.content}" for chunk in chunks])
            if len(vectors) != len(chunks):
                raise ValueError("Embedding count does not match source chunks")
            for chunk, vector in zip(chunks, vectors):
                chunk.embedding = vector
            self.store.save_chunks(repo_id, chunks)
            return self.store.update_repo(
                repo_id, status="ready", revision=revision, snapshot_path=str(snapshot),
                files_count=len({chunk.path for chunk in chunks}), chunks_count=len(chunks), error="",
                embedding_profile=embedding_profile(self.settings),
            )
        except Exception as exc:
            self.store.update_repo(repo_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
            raise
