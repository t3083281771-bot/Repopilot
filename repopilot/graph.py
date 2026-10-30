"""Bounded, read-only LangGraph agent with durable, resumable state."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
from typing import Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .providers import ProviderError, Usage, make_provider


class AgentState(TypedDict, total=False):
    task_id: str
    repo_id: str
    revision: str
    question: str
    strategy: str
    mode: str
    phase: str
    resume_phase: str
    action: dict
    evidence: list[dict]
    reads: list[dict]
    history: list[dict]
    rounds: int
    tool_calls: int
    usage: dict
    stop_reason: str
    error: str
    elapsed_ms: float
    result: dict


STOP_MESSAGES = {
    "single_pass": "已完成单轮检索",
    "evidence_sufficient": "已收集足够证据",
    "round_budget": "达到检索轮数上限",
    "tool_budget": "达到工具调用上限",
    "context_budget": "达到证据上下文预算",
    "no_new_evidence": "本轮没有发现新证据",
    "invalid_action": "模型请求了无效或未经授权的工具参数",
}


class AgentRunner:
    def __init__(self, settings, store):
        from .retrieval import Retriever

        self.settings = settings
        self.store = store
        self.retriever = Retriever(settings, store)
        # Lazy initialization keeps startup/key-free demo imports functional.
        self.provider = None

    @contextmanager
    def _checkpointer(self):
        if self.settings.database_url.startswith(("postgresql://", "postgres://")):
            try:
                from langgraph.checkpoint.postgres import PostgresSaver
            except ImportError:
                raise RuntimeError("PostgreSQL 检查点需要安装 langgraph-checkpoint-postgres。") from None
            with PostgresSaver.from_conn_string(self.settings.database_url) as saver:
                saver.setup()
                yield saver
            return
        connection = sqlite3.connect(
            str(self.settings.data_dir / "checkpoints.sqlite3"),
            check_same_thread=False, timeout=30,
        )
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield SqliteSaver(connection)
        finally:
            connection.close()

    def _provider(self):
        if self.provider is None:
            self.provider = make_provider(self.settings)
        return self.provider

    @staticmethod
    def _usage_update(state: AgentState, added: Usage) -> dict:
        totals = Usage(**state.get("usage", {}))
        totals.add(added)
        return totals.to_dict()

    def _model_context(self, state: AgentState) -> tuple[list[dict], list[dict]]:
        remaining = max(0, self.settings.max_context_chars)
        evidence = []
        reads = []
        for source, destination in ((state.get("evidence", []), evidence), (state.get("reads", []), reads)):
            for raw in source:
                item = {key: value for key, value in raw.items() if key != "embedding"}
                content = item["content"]
                if len(json.dumps({**item, "content": ""}, ensure_ascii=False)) + 2 > remaining:
                    break
                # JSON escaping expands newlines/quotes. Clip the serialized
                # representation, not only the original content character count.
                low, high = 0, len(content)
                while low < high:
                    middle = (low + high + 1) // 2
                    size = len(json.dumps({**item, "content": content[:middle]}, ensure_ascii=False)) + 2
                    if size <= remaining:
                        low = middle
                    else:
                        high = middle - 1
                item["content"] = content[:low]
                remaining -= len(json.dumps(item, ensure_ascii=False)) + 2
                destination.append(item)
        return evidence, reads

    def _budget_reason(self, state: AgentState) -> str:
        if state["tool_calls"] >= max(0, self.settings.max_tool_calls):
            return "tool_budget"
        if state["rounds"] >= max(0, self.settings.max_rounds):
            return "round_budget"
        content_chars = sum(len(item["content"]) for item in state.get("evidence", []) + state.get("reads", []))
        if content_chars >= self.settings.max_context_chars:
            return "context_budget"
        return ""

    @staticmethod
    def _failed(phase: str, message: str, **updates) -> dict:
        return {"phase": "failed", "resume_phase": phase, "error": message, **updates}

    def _retrieve(self, state: AgentState, emit) -> dict:
        budget = self._budget_reason(state)
        if budget:
            return {"phase": "report", "stop_reason": budget}
        action = state["action"]
        kind = action.get("action", "search")
        emit({"node": "retrieve", "message": "正在读取代码上下文" if kind == "read" else "正在检索仓库证据", "round": state["rounds"] + 1, "tool": kind})
        evidence = list(state["evidence"])
        reads = list(state["reads"])
        previous_ids = {item["id"] for item in evidence}
        calls = state["tool_calls"] + 1
        rounds = state["rounds"] + 1
        new_evidence = 0
        try:
            if kind == "read":
                selected = next((item for item in evidence if item["id"] == action.get("chunk_id")), None)
                if selected is None:
                    return {"phase": "report", "stop_reason": "invalid_action"}
                start = max(1, selected["start_line"] - 15)
                end = min(start + 199, selected["end_line"] + 15)
                item = self.retriever.read_file(state["repo_id"], selected["path"], start, end)
                item = {**item, "chunk_id": selected["id"]}
                signature = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
                if not any(value.get("signature") == signature for value in reads):
                    reads.append({**item, "signature": signature})
                    new_evidence = 1
            elif kind in {"search", "symbols"}:
                query = str(action.get("query", state["question"])).strip()[:2000]
                if not query:
                    return {"phase": "report", "stop_reason": "invalid_action"}
                if kind == "symbols":
                    chunks = self.retriever.search_symbols(state["repo_id"], query, k=5)
                else:
                    chunks = self.retriever.search(state["repo_id"], query, strategy=state["strategy"], k=5)
                for chunk in chunks:
                    if chunk.repo_id == state["repo_id"] and chunk.id not in previous_ids:
                        evidence.append(chunk.model_dump(exclude={"embedding"}))
                        previous_ids.add(chunk.id)
                        new_evidence += 1
            else:
                return {"phase": "report", "stop_reason": "invalid_action"}
        except Exception as exc:
            # Store completed attempt counts before surfacing failure. Resuming
            # retries this operation without discarding earlier evidence/usage.
            return self._failed("retrieve", f"检索工具失败：{type(exc).__name__}", tool_calls=calls, rounds=rounds)
        history = state["history"] + [{"action": kind, "query": action.get("query", ""), "new_evidence": new_evidence}]
        updates = {"evidence": evidence, "reads": reads, "history": history, "tool_calls": calls, "rounds": rounds}
        next_state = {**state, **updates}
        reason = "single_pass" if state["mode"] == "single" else ""
        if not reason and not new_evidence:
            reason = "no_new_evidence"
        if not reason:
            reason = self._budget_reason(next_state)
        emit({"node": "evidence", "message": f"本轮新增 {new_evidence} 条证据，累计 {len(evidence)} 个代码片段", "round": rounds, "new_evidence": new_evidence, "evidence_count": len(evidence)})
        return {**updates, "phase": "report" if reason else "assess", "stop_reason": reason}

    def _assess(self, state: AgentState, emit) -> dict:
        emit({"node": "assess", "message": "正在判断证据是否充分并选择下一步", "round": state["rounds"]})
        evidence, reads = self._model_context(state)
        try:
            response = self._provider().complete(
                "assess", state["question"], evidence,
                reads=reads, history=state["history"], round_number=state["rounds"],
                remaining_rounds=max(0, self.settings.max_rounds - state["rounds"]),
                remaining_tool_calls=max(0, self.settings.max_tool_calls - state["tool_calls"]),
            )
        except ProviderError as exc:
            return self._failed("assess", str(exc), usage=self._usage_update(state, exc.usage))
        data = response.data
        updates = {"usage": self._usage_update(state, response.usage)}
        action = data.get("action")
        if not isinstance(action, str):
            return {**updates, "phase": "report", "stop_reason": "invalid_action"}
        if action == "report":
            return {**updates, "phase": "report", "stop_reason": "evidence_sufficient"}
        valid = action in {"search", "symbols", "read"}
        if action in {"search", "symbols"}:
            valid = isinstance(data.get("query"), str) and bool(data["query"].strip())
        elif action == "read":
            valid = data.get("chunk_id") in {item["id"] for item in state["evidence"]}
        if not valid:
            return {**updates, "phase": "report", "stop_reason": "invalid_action"}
        # Tool names and input shapes are allowlisted here, never executed from text.
        emit({"node": "route", "message": str(data.get("reason", "继续补充证据"))[:500], "round": state["rounds"], "action": action})
        return {**updates, "phase": "retrieve", "action": {key: data[key] for key in ("action", "query", "chunk_id") if key in data}}

    @staticmethod
    def _strings(value) -> list[str]:
        return [item[:4000] for item in value[:20] if isinstance(item, str)] if isinstance(value, list) else []

    def _ground_report(self, data: dict, state: AgentState) -> dict:
        allowed_ids = {item["id"] for item in state["evidence"]}
        findings = []
        dropped = False
        raw_findings = data.get("findings", [])
        if not isinstance(raw_findings, list):
            raise ProviderError("模型报告 findings 结构无效。")
        for finding in raw_findings[:12]:
            if not isinstance(finding, dict):
                dropped = True
                continue
            ids = finding.get("chunk_ids", [])
            if not isinstance(ids, list):
                dropped = True
                continue
            citations = []
            seen = set()
            for chunk_id in ids[:10]:
                if not isinstance(chunk_id, str) or chunk_id not in allowed_ids or chunk_id in seen:
                    dropped = True
                    continue
                chunk = self.store.get_chunk(chunk_id)
                if chunk is None or chunk.repo_id != state["repo_id"]:
                    dropped = True
                    continue
                seen.add(chunk_id)
                citations.append({"chunk_id": chunk.id, **{key: getattr(chunk, key) for key in ("path", "symbol", "start_line", "end_line", "content")}})
            if citations and isinstance(finding.get("explanation"), str):
                findings.append({"title": str(finding.get("title", "候选位置"))[:500], "explanation": finding["explanation"][:6000], "citations": citations})
            else:
                dropped = True
        uncertainties = self._strings(data.get("uncertainties", []))
        if dropped:
            uncertainties.append("模型返回了无法核验的引用；无有效证据支持的条目已省略。")
        if not findings:
            uncertainties.append("当前证据不足以生成可核验的问题定位结论。")
        summary = data.get("summary", "")
        if not isinstance(summary, str):
            raise ProviderError("模型报告 summary 结构无效。")
        if not findings and dropped:
            summary = "未生成具有有效证据引用的定位结论，请补充问题信息或更换检索方式。"
        return {"summary": summary[:8000], "findings": findings, "suggestions": self._strings(data.get("suggestions", [])), "uncertainties": uncertainties}

    def _report(self, state: AgentState, emit) -> dict:
        reason = state.get("stop_reason") or "evidence_sufficient"
        emit({"node": "report", "message": STOP_MESSAGES.get(reason, reason) + "，正在生成带引用的报告", "round": state["rounds"], "stop_reason": reason})
        evidence, reads = self._model_context(state)
        usage = state["usage"]
        try:
            response = self._provider().complete("report", state["question"], evidence, reads=reads, history=state["history"], stop_reason=reason)
            usage = self._usage_update(state, response.usage)
            result = self._ground_report(response.data, state)
        except ProviderError as exc:
            # Validation failures occur after a successful billable response.
            # Preserve its usage as well as any failed HTTP attempt usage.
            return self._failed("report", str(exc), usage=self._usage_update({**state, "usage": usage}, exc.usage))
        except Exception as exc:
            return self._failed("report", f"报告核验失败：{type(exc).__name__}", usage=usage)
        return {"phase": "done", "usage": usage, "result": result, "stop_reason": reason}

    def _graph(self, checkpointer, emit):
        graph = StateGraph(AgentState)

        def dispatch(state):
            return {}

        def timed(operation):
            def node(state):
                started = time.perf_counter()
                updates = operation(state, emit)
                updates["elapsed_ms"] = state.get("elapsed_ms", 0) + (time.perf_counter() - started) * 1000
                return updates
            return node

        graph.add_node("dispatch", dispatch)
        graph.add_node("retrieve", timed(self._retrieve))
        graph.add_node("assess", timed(self._assess))
        graph.add_node("report", timed(self._report))
        graph.add_edge(START, "dispatch")
        graph.add_conditional_edges("dispatch", lambda state: state["phase"], {"retrieve": "retrieve", "assess": "assess", "report": "report", "done": END, "failed": END})
        for node in ("retrieve", "assess", "report"):
            graph.add_edge(node, "dispatch")
        return graph.compile(checkpointer=checkpointer)

    def run(self, task_id: str, repo_id: str, question: str, strategy: str = "hybrid", mode: str = "agent", emit: Callable[[dict], None] | None = None, resume: bool = False) -> dict:
        self._run_started = time.perf_counter()
        if mode not in {"single", "agent"}:
            raise ValueError("mode 必须是 single 或 agent")
        repo = self.store.get_repo(repo_id)
        if not repo or repo.get("status") != "ready":
            raise ValueError("仓库尚未完成索引")
        callback = emit or (lambda event: None)
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": max(30, self.settings.max_rounds * 6 + 12)}
        with self._checkpointer() as checkpointer:
            graph = self._graph(checkpointer, callback)
            checkpoint = graph.get_state(config)
            saved = checkpoint.values
            if resume and not saved:
                # A queued task can be interrupted before its first graph node.
                # The API passes the original persisted task input, so starting
                # it now is a safe restart and does not overwrite a checkpoint.
                callback({"node": "restart", "message": "尚无检查点，使用原任务输入重新开始", "round": 0})
                resume = False
            if resume:
                if saved.get("repo_id") != repo_id or saved.get("revision") != repo.get("revision"):
                    raise ValueError("仓库版本与任务检查点不一致，不能恢复")
                callback({"node": "resume", "message": "正在从持久化检查点恢复任务", "round": saved.get("rounds", 0)})
                if saved.get("phase") == "failed":
                    # Change only the failure marker/scheduling; never replace
                    # the original question, accumulated evidence or token usage.
                    graph.update_state(config, {"phase": saved["resume_phase"], "resume_phase": "", "error": ""}, as_node="dispatch")
                elif saved.get("phase") == "done":
                    return self._result(saved)
                state = graph.invoke(None, config)
            else:
                if saved:
                    raise ValueError("该任务已有检查点，请使用恢复操作")
                if not question.strip():
                    raise ValueError("问题不能为空")
                callback({"node": "plan", "message": "已固定仓库版本，开始只读问题定位", "round": 0})
                state = graph.invoke({
                    "task_id": task_id, "repo_id": repo_id, "revision": repo.get("revision", ""),
                    "question": question, "strategy": strategy, "mode": mode, "phase": "retrieve",
                    "resume_phase": "", "action": {"action": "search", "query": question},
                    "evidence": [], "reads": [], "history": [], "rounds": 0, "tool_calls": 0,
                    "usage": Usage().to_dict(), "stop_reason": "", "error": "", "elapsed_ms": 0.0,
                }, config)
            if state.get("phase") == "failed":
                callback({"node": "failure", "message": state["error"], "round": state["rounds"], "recoverable": True})
                raise RuntimeError(state["error"])
            result = self._result(state)
            callback({"node": "complete", "message": "定位报告已生成，引用已由仓库索引核验", "round": state["rounds"]})
            return result

    def _result(self, state: AgentState) -> dict:
        provider = self.provider
        model = provider.model if provider is not None else ("deterministic-demo" if self.settings.provider == "demo" else self.settings.deepseek_model)
        return {
            **state["result"], "stop_reason": state["stop_reason"],
            "repository": {"id": state["repo_id"], "revision": state["revision"]},
            "metrics": {
                "rounds": state["rounds"], "tool_calls": state["tool_calls"], **state["usage"],
                "usage_estimated": bool(state["usage"].get("estimated_calls")),
                "usage_note": "部分失败请求未返回 usage，含本地估算，不能作为精确账单。" if state["usage"].get("estimated_calls") else "提供方返回的 usage 累计；演示模式无模型费用。",
                "elapsed_ms": round((time.perf_counter() - self._run_started) * 1000, 2),
                "node_elapsed_ms": round(state["elapsed_ms"], 2), "provider": self.settings.provider,
                "model": model, "embedding_provider": self.settings.embedding_provider,
            },
            "evidence": state["evidence"],
        }
