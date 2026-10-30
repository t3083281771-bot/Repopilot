# 交付验证记录

日期：2026 年 10 月 28 日，Asia/Shanghai。

## 已实际完成的验证

- Windows / Python 3.12.14 的独立 `.venv` 中安装依赖，版本记录在 `requirements-lock.txt`。
- `python -m pytest -q`：**82 passed，1 skipped**。跳过项需要本机创建 NTFS 符号链接权限；归档链接、路径遍历、特殊文件等另有不依赖该权限的测试。
- `ruff check repopilot tests scripts`：通过。
- `npm run build`：Vue/TypeScript 检查与生产构建通过。
- 公开仓库真实导入：Requests v2.32.5、Flask 3.1.2、Click 8.2.1；完整提交 SHA 记录在公开评测数据中。
- 遇到 GitHub 匿名 REST 限流时，实际验证 Atom 提交解析后按 SHA 下载的回退路径。
- 内置合成演示集：检索测试和开发集 single/agent 联通运行。
- 冻结公开源码问答集：24 条测试问题的四种检索策略实际运行；6 条开发问题执行 single/agent 演示工作流。
- API 回归覆盖仓库导入、任务、源码引用、SSE 事件回放、最后事件竞态、固定版本重试、缺密钥错误和中断恢复。
- 浏览器实际操作验证演示仓库选择、示例问题提交、Requests 公开源码定位与行号读取，以及冻结测试集指标表；相关截图保存在 `docs/screenshots/`。
- Compose YAML 解析与 PostgreSQL 检查点组件导入通过。

源码查看使用索引时保存的块重建同版本内容，不依赖临时目录残留权限；新导入使用继承父权限的普通 staging 目录。曾提出的批量 ACL 重置被自动审批拒绝，未执行；最终方案没有更改文件访问权限。

## 尚未实际验证的路径

- **真实 DeepSeek 请求**：用户选择稍后自行填写 `.env`。目前验证了模拟 HTTP 响应、限流/超时/鉴权错误、结构输出、引用过滤与用量累计；未调用真实模型，也没有真实推理质量或费用结论。
- **真实语义嵌入**：sentence-transformers 和兼容嵌入 API 适配已实现，API 使用模拟响应测试；未下载本地语义模型进行实测。当前公开结果使用 hash 词法向量。
- **PostgreSQL / Docker 整体运行**：本机没有 Docker，未启动 PostgreSQL 容器或完成容器构建。已准备 pgvector 存储、PostgresSaver、Compose、Dockerfile；默认 SQLite 路径已实测。
- **公网多用户服务与自动修复**：不在首期交付范围；没有执行目标仓库代码、自动写补丁或创建 PR。

## 评测结论的边界

公开题目根据源码拟定，有 AI 辅助并逐题核对；没有独立人工双标。30 条问题中 6 条开发、24 条测试，三个仓库同时出现在两种划分中，不构成跨仓库泛化实验。

文件和符号命中只能反映定位候选，不能证明根因判断、修复成功或模型推理质量。候选存在与证据支持程度需要分开。真实运行结果、逐题数据和局限见 `evaluation-results.md` 与 `public-eval-protocol.md`。
