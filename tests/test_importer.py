import io
from pathlib import Path
import stat
import zipfile

import httpx
import pytest

from repopilot.config import Settings
from repopilot.importer import Importer, _github_source, _snapshot_staging, chunk_repository
from repopilot.store import Store


def configured(tmp_path, **kwargs):
    settings = Settings(data_dir=tmp_path / "runtime", allow_local_import=True, **kwargs)
    return settings, Store(settings)


def test_ast_chunks_preserve_decorators_methods_module_and_exact_lines(tmp_path):
    source = (
        "import math\nLIMIT = 4\n\n"
        "@staticmethod\nasync def solve(value):\n    return value / LIMIT\n\n"
        "class Worker:\n    name = 'worker'\n\n"
        "    def run(self):\n        return solve(2)\n"
    )
    (tmp_path / "worker.py").write_text(source, encoding="utf-8")
    chunks = chunk_repository(tmp_path, "repo")
    by_symbol = {chunk.symbol: chunk for chunk in chunks}
    assert {"<module>", "solve", "Worker", "Worker.run"} <= by_symbol.keys()
    assert by_symbol["solve"].start_line == 4
    assert by_symbol["Worker.run"].start_line == 11
    for chunk in chunks:
        assert chunk.content == "\n".join(source.splitlines()[chunk.start_line - 1:chunk.end_line])
    assert [chunk.id for chunk in chunks] == [chunk.id for chunk in chunk_repository(tmp_path, "repo")]


def test_long_functions_are_bounded_and_overlap_without_losing_tail(tmp_path):
    source = "def large():\n" + "".join(f"    value_{index} = {index}\n" for index in range(240))
    (tmp_path / "large.py").write_text(source, encoding="utf-8")
    chunks = chunk_repository(tmp_path, "repo")
    assert len(chunks) >= 3
    assert all(chunk.end_line - chunk.start_line + 1 <= 80 for chunk in chunks)
    assert chunks[-1].end_line == 241
    assert chunks[1].start_line <= chunks[0].end_line


def test_markdown_fences_do_not_create_fake_heading_chunks(tmp_path):
    (tmp_path / "README.md").write_text("# Intro\ntext\n```python\n# code comment\n```\n## Usage\nhelp", encoding="utf-8")
    chunks = chunk_repository(tmp_path, "repo")
    assert [chunk.symbol for chunk in chunks] == ["Intro", "Usage"]
    assert all(chunk.kind == "markdown" for chunk in chunks)


def test_local_import_never_executes_code_and_snapshot_is_immutable(tmp_path):
    settings, store = configured(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    sentinel = tmp_path / "executed.txt"
    (source / "danger.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n", encoding="utf-8")
    (source / ".env").write_text("secret")
    (source / "secrets.py").write_text("key = 'private'")
    (source / ".hidden").mkdir()
    (source / ".hidden" / "code.py").write_text("value = 1")
    repository = store.create_repo(str(source))
    first = Importer(settings, store).import_repo(repository["id"], str(source))
    assert first["status"] == "ready" and first["files_count"] == 1
    assert not sentinel.exists()
    old_snapshot = Path(first["snapshot_path"])
    assert sorted(path.name for path in old_snapshot.iterdir()) == ["danger.py"]
    (source / "danger.py").write_text("value = 42", encoding="utf-8")
    second = Importer(settings, store).import_repo(repository["id"], str(source))
    assert second["revision"] != first["revision"]
    assert "executed" in (old_snapshot / "danger.py").read_text(encoding="utf-8")


def test_file_budget_failure_is_persisted(tmp_path):
    settings, store = configured(tmp_path, max_repo_files=1)
    source = tmp_path / "source"
    source.mkdir()
    for name in ("one.py", "two.py"):
        (source / name).write_text("value = 1")
    repository = store.create_repo(str(source))
    with pytest.raises(ValueError, match="budget"):
        Importer(settings, store).import_repo(repository["id"], str(source))
    assert store.get_repo(repository["id"])["status"] == "failed"
    assert store.get_chunks(repository["id"]) == []
    assert list((settings.data_dir / "snapshots" / repository["id"]).glob("import-*")) == []


def test_staging_inherits_normal_directory_mode_and_promotion_is_preserved(tmp_path, monkeypatch):
    parent = tmp_path / "snapshots"
    parent.mkdir()
    mkdir = Path.mkdir
    observed_modes = []

    def record_mkdir(path, mode=0o777, parents=False, exist_ok=False):
        if path.name.startswith("import-"):
            observed_modes.append(mode)
        return mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", record_mkdir)
    with _snapshot_staging(parent) as staged:
        (staged / "main.py").write_text("value = 1", encoding="utf-8")
        promoted = parent / ("a" * 40)
        staged.rename(promoted)
    assert observed_modes == [0o777]
    assert (promoted / "main.py").read_text(encoding="utf-8") == "value = 1"
    assert list(parent.glob("import-*")) == []


def test_failed_staging_is_cleaned_without_masking_original_error(tmp_path):
    parent = tmp_path / "snapshots"
    parent.mkdir()
    with pytest.raises(RuntimeError, match="original import failure"):
        with _snapshot_staging(parent) as staged:
            (staged / "partial.py").write_text("value = 1", encoding="utf-8")
            raise RuntimeError("original import failure")
    assert list(parent.iterdir()) == []


def test_staging_cleanup_refuses_resolved_path_escape(tmp_path, monkeypatch):
    parent = tmp_path / "snapshots"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    resolve = Path.resolve
    with pytest.raises(ValueError, match="unsafe snapshot staging cleanup"):
        with _snapshot_staging(parent) as staged:
            def escaped_resolve(path, *args, **kwargs):
                return outside if path == staged else resolve(path, *args, **kwargs)
            monkeypatch.setattr(Path, "resolve", escaped_resolve)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert staged.exists()
    monkeypatch.undo()
    staged.rmdir()


@pytest.mark.parametrize("url", [
    "https://github.com.evil.test/user/repo", "https://user@github.com/user/repo",
    "https://github.com/user/repo/tree/main", "http://github.com/user/repo",
    "https://github.com/user/repo?download=1", "https://github.com/user/..",
])
def test_github_url_rejects_untrusted_hosts_and_nonrepository_paths(url):
    with pytest.raises(ValueError):
        _github_source(url)


def install_github_mock(monkeypatch, archive_bytes):
    sha = "a" * 40
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "api.github.com":
            if "/commits/" in request.url.path:
                return httpx.Response(200, json={"sha": sha})
            return httpx.Response(200, json={"private": False})
        assert str(request.url) == f"https://codeload.github.com/owner/repo/zip/{sha}"
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=archive_bytes)

    client_type = httpx.Client
    monkeypatch.setattr("repopilot.importer.httpx.Client", lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    return requests


def zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries:
            if isinstance(name, str):
                raw_name = name
                name = zipfile.ZipInfo(name)
                # ZipInfo normalizes Windows separators on construction; retain
                # hostile wire-format names to test the reader, not the writer.
                name.filename = raw_name
                name.orig_filename = raw_name
            archive.writestr(name, content)
    return buffer.getvalue()


def test_github_import_pins_commit_and_downloads_only_expected_host(tmp_path, monkeypatch):
    requests = install_github_mock(monkeypatch, zip_bytes([("repo-sha/src/main.py", "def hello():\n    return 1\n")]))
    settings, store = configured(tmp_path, github_token="test-token")
    repository = store.create_repo("https://github.com/owner/repo")
    result = Importer(settings, store).import_repo(repository["id"], repository["source"], "feature/name")
    assert result["revision"] == "a" * 40
    assert {request.url.host for request in requests} == {"api.github.com", "codeload.github.com"}
    assert store.get_chunks(repository["id"])[0].path == "src/main.py"


@pytest.mark.parametrize("entry", ["repo-sha/../../escape.py", "repo-sha/src\\escape.py", "/root/escape.py"])
def test_archive_path_traversal_is_rejected(tmp_path, monkeypatch, entry):
    install_github_mock(monkeypatch, zip_bytes([(entry, "value = 1")]))
    settings, store = configured(tmp_path)
    repository = store.create_repo("https://github.com/owner/repo")
    with pytest.raises(ValueError, match="Unsafe"):
        Importer(settings, store).import_repo(repository["id"], repository["source"])


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK])
def test_archive_links_and_special_files_are_excluded_without_losing_source(tmp_path, monkeypatch, file_type):
    entry = zipfile.ZipInfo("repo-sha/link.py")
    entry.create_system = 3
    entry.external_attr = (file_type | 0o777) << 16
    install_github_mock(monkeypatch, zip_bytes([
        (entry, "../../outside.py"), ("repo-sha/main.py", "def normal_source():\n    return 1"),
    ]))
    settings, store = configured(tmp_path)
    repository = store.create_repo("https://github.com/owner/repo")
    result = Importer(settings, store).import_repo(repository["id"], repository["source"])
    assert result["status"] == "ready"
    assert [chunk.path for chunk in store.get_chunks(repository["id"])] == ["main.py"]
    assert not (Path(result["snapshot_path"]) / "link.py").exists()
    assert not (tmp_path / "outside.py").exists()


def test_unsafe_link_entry_path_is_rejected_before_link_exclusion(tmp_path, monkeypatch):
    entry = zipfile.ZipInfo("repo-sha/../../escape.py")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    install_github_mock(monkeypatch, zip_bytes([(entry, "elsewhere.py")]))
    settings, store = configured(tmp_path)
    repository = store.create_repo("https://github.com/owner/repo")
    with pytest.raises(ValueError, match="escape.py"):
        Importer(settings, store).import_repo(repository["id"], repository["source"])


def test_local_import_must_be_explicitly_enabled(tmp_path):
    settings = Settings(data_dir=tmp_path / "runtime")
    store = Store(settings)
    repository = store.create_repo(str(tmp_path))
    with pytest.raises(ValueError, match="disabled"):
        Importer(settings, store).import_repo(repository["id"], str(tmp_path))


def fallback_import(tmp_path, monkeypatch, *, atom_id=None, api_status=403, redirect=None, ref="main"):
    sha = "b" * 40
    requests = []
    archive = zip_bytes([("repo-sha/main.py", "def hello():\n    return 1")])
    identifier = atom_id if atom_id is not None else f"tag:github.com,2008:Grit::Commit/{sha}"

    def handler(request):
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(api_status, json={"message": "rate limit exceeded"})
        if request.url.host == "github.com":
            assert "authorization" not in request.headers
            if redirect:
                return httpx.Response(302, headers={"location": redirect})
            return httpx.Response(200, content=(
                '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                f"<id>{identifier}</id></entry></feed>"
            ).encode())
        assert request.url.host == "codeload.github.com"
        assert request.url.path.endswith(f"/zip/{sha}")
        assert "authorization" not in request.headers
        return httpx.Response(200, content=archive)

    client_type = httpx.Client
    monkeypatch.setattr("repopilot.importer.httpx.Client", lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    settings, store = configured(tmp_path)
    repository = store.create_repo("https://github.com/owner/repo", ref)
    result = Importer(settings, store).import_repo(repository["id"], repository["source"], ref)
    return result, requests


@pytest.mark.parametrize("api_status", [403, 429])
def test_anonymous_api_quota_falls_back_to_validated_public_atom_commit(tmp_path, monkeypatch, api_status):
    result, requests = fallback_import(tmp_path, monkeypatch, api_status=api_status)
    assert result["revision"] == "b" * 40
    assert [request.url.host for request in requests] == ["api.github.com", "github.com", "codeload.github.com"]


def test_full_commit_sha_can_be_downloaded_publicly_without_atom(tmp_path, monkeypatch):
    result, requests = fallback_import(tmp_path, monkeypatch, ref="b" * 40)
    assert result["revision"] == "b" * 40
    assert [request.url.host for request in requests] == ["api.github.com", "codeload.github.com"]


@pytest.mark.parametrize("identifier", [
    "tag:evil.test,2008:Grit::Commit/" + "b" * 40,
    "tag:github.com,2008:Grit::Commit/" + "b" * 39,
    "unrelated-text-" + "b" * 40,
])
def test_public_atom_rejects_identifiers_that_are_not_github_commit_ids(tmp_path, monkeypatch, identifier):
    with pytest.raises(ValueError, match="commit identifier"):
        fallback_import(tmp_path, monkeypatch, atom_id=identifier)


def test_public_atom_rejects_external_redirect_before_contacting_target(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Unsafe redirect"):
        fallback_import(tmp_path, monkeypatch, redirect="https://evil.test/private.atom")


def test_authentication_errors_do_not_trigger_public_feed_fallback(tmp_path, monkeypatch):
    with pytest.raises(httpx.HTTPStatusError) as error:
        fallback_import(tmp_path, monkeypatch, api_status=401)
    assert error.value.response.status_code == 401
