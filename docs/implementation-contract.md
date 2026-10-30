# RepoPilot implementation contract

Python >=3.11. Package `repopilot`. Root owns config.py, models.py, store.py,
api.py, CLI, evaluation, deployment and integration. Other owners must not edit these.

## Shared models (Pydantic, owned by root)

`Chunk`: id:str, repo_id:str, path:str (POSIX relative), symbol:str,
start_line:int, end_line:int (inclusive, 1-based), content:str, kind:str='code',
embedding:list[float]=[], score:float=0. `model_dump()` and `model_copy()` supported.

`Settings`: data_dir:Path, database_url:str, provider:str ('demo'|'deepseek'),
deepseek_api_key:str, deepseek_base_url:str, deepseek_model:str,
embedding_provider:str ('hash'|'sentence-transformers'|'api'), embedding_model:str,
embedding_base_url:str, embedding_api_key:str, max_repo_files:int=1500,
max_repo_bytes:int=15000000, max_file_bytes:int=300000,
request_timeout:float=60, max_rounds:int=3, max_tool_calls:int=10,
max_context_chars:int=22000, max_output_tokens:int=1600,
github_token:str, allow_local_import:bool=False.

## Store (owned by root)

`Store(settings)`; initialize schema automatically. Thread-safe per-call connections.
`get_repo(repo_id)->dict|None`; `list_repos()->list[dict]`;
`update_repo(repo_id, **fields)->dict`; `save_chunks(repo_id, chunks:list[Chunk])`;
`get_chunks(repo_id)->list[Chunk]`; `get_chunk(chunk_id)->Chunk|None`;
`vector_search(repo_id, vector:list[float], k:int)->list[Chunk]`.
Repository dictionaries include id, source, revision, snapshot_path, status,
files_count, chunks_count, error, created_at, updated_at.

## Retrieval/importer owner

`Importer(settings, store)`;
`import_repo(repo_id:str, source:str, ref:str='HEAD')->dict`: update repo to running,
import demo fixture or github HTTPS public repository at resolved commit, create
immutable snapshot, parse/index, ready or failed. No execution of imported code.
`source='demo'` uses bundled examples/demo_repo. Optional local imports only with
settings.allow_local_import (used by CLI/evaluation). Enforce path, file, download
budgets and exclude symlinks, .git, hidden, env/secrets/build artifacts.
`chunk_repository(path:Path, repo_id:str)->list[Chunk]` (may accept extra budgets).
`Retriever(settings, store)`;
`search(repo_id, query, strategy='hybrid', k=5)->list[Chunk]`: bm25, vector, hybrid,
hybrid_rerank; k bounds checked by API. hash vectors are explicitly lexical demo
vectors, never claim semantic embedding performance. Real embeddings pluggable.
`read_file(repo_id,path,start_line=1,end_line=100)->dict` with path/start_line/end_line/content;
read only indexed files from immutable snapshot, max 200 lines.
`search_symbols(repo_id, query, k=5)->list[Chunk]`.

## Graph/provider owner

`AgentRunner(settings, store)`;
`run(task_id:str, repo_id:str, question:str, strategy:str='hybrid',
 mode:str='agent', emit:Callable[[dict],None]|None=None, resume:bool=False)->dict`.
Modes: single (retrieval once + report) and agent (bounded adaptive retrieval).
Graph uses LangGraph StateGraph with durable checkpoint keyed by task_id. Root API
handles task status/event persistence and calls emit. Runner must emit human-readable
events {node:str,message:str,round:int,...}. Resume invokes checkpoint without replacing
input; preserve provider token counts in state. No arbitrary tool/command execution.

Result shape: {summary:str, findings:[{title:str,explanation:str,
 citations:[{chunk_id,path,symbol,start_line,end_line,content}]}],
 suggestions:list[str], uncertainties:list[str], stop_reason:str,
 metrics:{rounds:int,tool_calls:int,prompt_tokens:int,completion_tokens:int,
 elapsed_ms:float,provider:str,model:str,embedding_provider:str},
 evidence:list[Chunk dict]}.
DeepSeek HTTP /chat/completions structured JSON, base URL configurable, default
model deepseek-flash (official docs checked 2026-10-28), thinking disabled where supported.
Never invent citations: validate chunk ids and produce file positions server-side.
Demo provider uses deterministic evidence summaries, explicitly marked demo.
Missing real key must fail clearly, never silently fall back to demo.

## HTTP API/frontend contract

GET /api/health -> {status,provider,model,model_configured,embedding_provider,version}
GET /api/repos -> array of repository dicts.
POST /api/repos {source,ref?} -> repo dict (202); background indexing.
POST /api/repos/{id}/retry -> repo dict (202).
GET /api/repos/{id} -> repo dict.
GET /api/repos/{id}/file?path=&start_line=&end_line= -> read_file dict.
POST /api/tasks {repo_id,question,strategy?,mode?} -> task dict (202).
GET /api/tasks?repo_id= -> array of task dicts.
GET /api/tasks/{id} -> task dict {id,repo_id,question,strategy,mode,status,result,error,...}.
GET /api/tasks/{id}/events -> SSE `id: N\nevent: progress\ndata: {..}\n\n`,
terminal event `event: done`, supports Last-Event-ID and ?after=N.
POST /api/tasks/{id}/resume -> task dict (202).
GET /api/evaluations -> {runs: list, note:str}.
GET /api/evaluations/{filename} -> saved evaluation report.
POST /api/search {repo_id,query,strategy?,k?} -> Chunk array.
Errors JSON {detail:str}. UTF-8 Chinese UI. API public binding disabled by default.
Frontend Vite proxy /api -> http://127.0.0.1:8765; production FastAPI serves web/dist.

## File ownership

retrieval agent: repopilot/importer.py, retrieval.py, embeddings.py;
tests/test_importer.py, test_retrieval.py, test_embeddings.py.
graph agent: repopilot/graph.py, providers.py; tests/test_graph.py, test_providers.py.
frontend agent: apps/web/** only. Root owns everything else.
