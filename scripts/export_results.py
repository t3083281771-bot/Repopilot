"""Export already measured results for sharing; never synthesizes metric values."""
import hashlib
import json
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent.parent
dataset = root / "eval/datasets/public-code-qa.json"
digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
reports = []
for path in sorted((root / "runtime/evaluations").glob("*.json")):
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("dataset_sha256") == digest and report.get("embedding_model") == "hash-v1-384":
        reports.append((path, report))
selected = {split: next((item for item in reversed(reports) if item[1]["split"] == split), None)
            for split in ("test", "dev")}
if not selected["test"]:
    raise SystemExit("Run the frozen public test evaluation before exporting")
destination = root / "docs/results"
destination.mkdir(parents=True, exist_ok=True)
source_path, public = selected["test"]
shutil.copyfile(source_path, destination / "public-retrieval.json")
lines = ["# 已运行的定位评测", "", f"数据 SHA-256：`{digest}`。", "",
         "公开源码拟题集：Requests、Flask、Click 的固定提交版本，共 30 题，其中 6 题开发、24 题测试。",
         "这份报告来自冻结测试集的实际运行。题目不是公开 Issue，也没有验证补丁或问题解决率。", "",
         "嵌入为 `hash-v1-384` 词法演示向量；重排为词法启发式。本表不能用于声称语义嵌入或模型重排效果。", "",
         "| 策略 | 测试题数 | 文件 Hit@5 | 文件 Recall@5 | 符号 Hit@5 | P50 ms | P95 ms |",
         "|---|---:|---:|---:|---:|---:|---:|"]
for row in public["summaries"]:
    values = [f"{row[key]*100:.1f}%" if row.get(key) is not None else "—"
              for key in ("file_hit_at_5", "file_recall_at_5", "symbol_hit_at_5")]
    lines.append(f"| {row['strategy']} | {row['cases']} | {' | '.join(values)} | {row['p50_ms']:.1f} | {row['p95_ms']:.1f} |")
lines += ["", "时间包含检索和查询向量计算，未包含首次导入和回答生成；运行于本机 Python "
          + public["environment"] + "，不是生产压测结果。", "",
          "符号指标按去重后的文件与符号名计算，同名 overload 声明可能命中；应结合原始片段和实现范围核验。",
          "文件 Hit@5 仅要求任意标注文件出现，跨函数问题还需要检查文件召回和逐题证据。", "",
          "检索结果不支持默认认定复杂组合一定更好。接入真实语义嵌入和自然故障问题后，需要另行冻结数据并比较。", "",
          "[完整测试结果](results/public-retrieval.json)包含逐题结果、固定提交、参数、时间和指标口径。"]
if selected["dev"]:
    dev_path, dev = selected["dev"]
    shutil.copyfile(dev_path, destination / "dev-workflow-smoke.json")
    lines += ["", "## 工作流联通验证", "",
              "6 条公开开发问题也执行了 single / agent 流程。当前提供方是确定性 demo，Token 为 0；",
              "这些结果仅证明编排、引用和保存能够运行，不证明真实大模型推理效果或 Agent 增益。", "",
              "[完整开发集流程记录](results/dev-workflow-smoke.json)。"]
lines += ["", "标注与局限见 [评测协议](public-eval-protocol.md)。真实 DeepSeek 的推理质量、费用和时延尚未实测。", ""]
(root / "docs/evaluation-results.md").write_text("\n".join(lines), encoding="utf-8")
print(json.dumps({"test_report": source_path.name, "test_cases": public["case_count"],
                  "summaries": public["summaries"]}, ensure_ascii=False, indent=2))
