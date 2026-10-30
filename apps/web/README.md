# RepoPilot Web

Vue 3 + TypeScript 工作台。所有分析、仓库状态和评测记录均来自后端 API；前端不合成分析结果。

需要 Node.js 22.12 或更新版本。

```powershell
npm ci
npm run dev
```

开发地址：`http://127.0.0.1:5173`。Vite 将 `/api` 代理到 `http://127.0.0.1:8765`。

```powershell
npm run build
```

构建先执行 TypeScript 检查，再生成 `dist/`；生产环境由 FastAPI 托管该目录。
界面不依赖远程字体、图片或 CDN。

功能包括仓库导入与重试、任务历史和恢复、SSE 运行轨迹、固定版本源码行号、
已保存评测报告查看。SSE 中断时自动重连，并使用任务状态轮询兜底。
源码和模型输出通过 Vue 文本插值显示，不作为 HTML 渲染。
