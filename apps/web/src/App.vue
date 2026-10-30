<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import Icon from './Icon.vue'
import { api, post } from './api'
import type { Citation, EvaluationIndex, EvaluationReport, EvaluationRun, EvaluationSummary, FileContent, Health, ProgressEvent, Repo, Task } from './api'

const health = ref<Health | null>(null)
const connected = ref(false)
const page = ref<'workspace' | 'evaluations'>('workspace')
const repos = ref<Repo[]>([])
const tasks = ref<Task[]>([])
const selectedRepoId = ref('')
const selectedTask = ref<Task | null>(null)
const question = ref('')
const strategy = ref('hybrid')
const mode = ref('agent')
const importing = ref(false)
const submitting = ref(false)
const importOpen = ref(false)
const repoUrl = ref('')
const repoRef = ref('HEAD')
const logs = ref<ProgressEvent[]>([])
const streamConnected = ref(false)
const selectedCitation = ref<Citation | null>(null)
const fileContent = ref<FileContent | null>(null)
const fileLoading = ref(false)
const evidenceTab = ref<'code' | 'trace'>('code')
const toast = ref('')
const evaluations = ref<EvaluationIndex>({ runs: [], note: '' })
const evaluationLoading = ref(false)
const evaluationDoc = ref<EvaluationReport | null>(null)
const selectedEvaluation = ref('')
const copied = ref(false)
let eventSource: EventSource | null = null
let repoPoll: ReturnType<typeof setInterval> | undefined
let taskPoll: ReturnType<typeof setInterval> | undefined
let toastTimer: ReturnType<typeof setTimeout> | undefined
let copyTimer: ReturnType<typeof setTimeout> | undefined
let taskRequest = 0
let fileRequest = 0
let evaluationRequest = 0

const selectedRepo = computed(() => repos.value.find(repo => repo.id === selectedRepoId.value))
const result = computed(() => selectedTask.value?.result)
const activeTask = computed(() => !!selectedTask.value && isActive(selectedTask.value.status))
const repositoryReady = computed(() => selectedRepo.value?.status === 'ready')
const modelLabel = computed(() => health.value?.provider === 'deepseek' ? 'DeepSeek' : '本地演示')
const citations = computed(() => {
  const found = new Map<string, Citation>()
  for (const citation of [...(result.value?.findings ?? []).flatMap(item => item.citations ?? []), ...(result.value?.evidence ?? [])]) {
    const key = `${citation.path}:${citation.start_line}:${citation.end_line}`
    if (!found.has(key)) found.set(key, citation)
  }
  return [...found.values()]
})
const visibleCode = computed(() => {
  const source = fileContent.value ?? selectedCitation.value
  if (!source) return []
  return source.content.replace(/\n$/, '').split('\n').map((text, index) => ({
    text,
    number: source.start_line + index,
    highlighted: !!selectedCitation.value && source.start_line + index >= selectedCitation.value.start_line && source.start_line + index <= selectedCitation.value.end_line,
  }))
})
const evaluationPretty = computed(() => JSON.stringify(evaluationDoc.value, null, 2))
const evaluationSummaries = computed(() => evaluationDoc.value?.summaries ?? [])
const hasEvaluationTokens = computed(() => evaluationSummaries.value.some(row => typeof row.prompt_tokens === 'number' || typeof row.completion_tokens === 'number'))
const canSubmit = computed(() => repositoryReady.value && question.value.trim().length >= 5 && !submitting.value && !activeTask.value)
const sampleQuestions = [
  'calculate_total 在数量为0时仍收取商品价格，问题在哪里？',
  'paginate 在 page_size=0 时触发 ZeroDivisionError，请定位原因。',
  '请分析 calculate_total 的调用位置，以及数量参数如何传入。',
]

function isActive(status: string) { return ['queued', 'pending', 'running', 'indexing', 'processing'].includes(status) }
function statusLabel(status: string) {
  return ({ ready: '已就绪', queued: '等待中', pending: '等待中', running: '运行中', indexing: '索引中', processing: '处理中', completed: '已完成', succeeded: '已完成', done: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消' } as Record<string, string>)[status] ?? status
}
function stopReasonLabel(reason: string) {
  return ({
    single_pass: '已完成单轮检索',
    evidence_sufficient: '已收集足够证据',
    round_budget: '已达到检索轮数上限',
    tool_budget: '已达到工具调用上限',
    context_budget: '已达到证据上下文预算',
    no_new_evidence: '没有发现更多新证据',
    invalid_action: '工具参数无效，已停止继续调用',
  } as Record<string, string>)[reason] ?? '分析流程已结束'
}
function repoName(repo: Repo) {
  if (repo.source === 'demo') return 'demo / sample-project'
  return repo.source.replace(/^https?:\/\/(www\.)?github\.com\//, '').replace(/\.git\/?$/, '').replace(/\/$/, '')
}
function shortRevision(revision?: string) { return revision ? revision.slice(0, 8) : '尚未解析版本' }
function mobileChooseRepo(event: Event) {
  const repo = repos.value.find(item => item.id === (event.target as HTMLSelectElement).value)
  if (repo) void chooseRepo(repo)
}
function mobileChooseTask(event: Event) {
  const task = tasks.value.find(item => item.id === (event.target as HTMLSelectElement).value)
  if (task) void openTask(task)
  else newTask()
}
function relativeTime(value: string) {
  if (!value) return ''
  const date = new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
  if (Number.isNaN(date.getTime())) return value.slice(0, 16)
  const minutes = Math.floor((Date.now() - date.getTime()) / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`
  return date.toLocaleDateString('zh-CN')
}
function notify(message: unknown) {
  toast.value = message instanceof Error ? message.message : String(message)
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 7000)
}
function stopStream() {
  eventSource?.close()
  eventSource = null
  streamConnected.value = false
  if (taskPoll) clearInterval(taskPoll)
  taskPoll = undefined
}
async function refreshRepos() {
  repos.value = await api<Repo[]>('/repos')
  if (!selectedRepoId.value && repos.value.length) {
    selectedRepoId.value = repos.value[0]!.id
    await refreshTasks()
  }
}
async function refreshTasks() {
  if (!selectedRepoId.value) { tasks.value = []; return }
  const repoId = selectedRepoId.value
  const data = await api<Task[]>(`/tasks?repo_id=${encodeURIComponent(repoId)}`)
  if (repoId === selectedRepoId.value) tasks.value = data
}
async function chooseRepo(repo: Repo) {
  page.value = 'workspace'
  if (repo.id === selectedRepoId.value) return
  ++taskRequest
  ++fileRequest
  stopStream()
  selectedRepoId.value = repo.id
  selectedTask.value = null
  logs.value = []
  selectedCitation.value = null
  fileContent.value = null
  try { await refreshTasks() } catch (error) { notify(error) }
}
async function importRepo(source: string) {
  if (importing.value) return
  if (source !== 'demo' && !/^https:\/\/github\.com\/[\w.-]+\/[\w.-]+(?:\.git)?\/?$/.test(source.trim())) {
    notify('请输入完整的公开 GitHub 仓库地址，例如 https://github.com/pallets/flask')
    return
  }
  importing.value = true
  try {
    const repo = await post<Repo>('/repos', { source: source.trim(), ref: source === 'demo' ? 'HEAD' : repoRef.value.trim() || 'HEAD' })
    await refreshRepos()
    await chooseRepo(repo)
    importOpen.value = false
    repoUrl.value = ''
    notify(source === 'demo' ? '演示仓库已提交，正在构建代码索引。' : '仓库已提交，正在获取固定版本并构建索引。')
  } catch (error) { notify(error) }
  finally { importing.value = false }
}
async function retryImport(repo: Repo) {
  try {
    await post(`/repos/${encodeURIComponent(repo.id)}/retry`)
    await refreshRepos()
  } catch (error) { notify(error) }
}
function recordEvent(event: MessageEvent) {
  try {
    const parsed = JSON.parse(event.data)
    const data = (parsed.data && typeof parsed.data === 'object' ? parsed.data : parsed) as ProgressEvent
    const id = event.lastEventId ? Number(event.lastEventId) : data.id
    if (id && logs.value.some(log => log.id === id)) return
    logs.value.push({ ...data, id })
  } catch { /* An invalid progress frame must not interrupt the task. */ }
}
async function updateSelectedTask(taskId: string, generation: number) {
  const data = await api<Task>(`/tasks/${encodeURIComponent(taskId)}`)
  if (generation !== taskRequest || selectedTask.value?.id !== taskId) return
  selectedTask.value = data
  if (!isActive(data.status)) {
    stopStream()
    await refreshTasks()
    if (!selectedCitation.value && citations.value.length) await chooseCitation(citations.value[0]!)
  }
}
function connectTask(taskId: string, generation: number) {
  eventSource?.close()
  const last = logs.value.reduce((max, log) => Math.max(max, log.id ?? 0), 0)
  const source = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events?after=${last}`)
  eventSource = source
  source.onopen = () => { if (eventSource === source) streamConnected.value = true }
  source.addEventListener('progress', recordEvent as EventListener)
  source.onmessage = recordEvent
  source.addEventListener('done', () => {
    if (generation !== taskRequest) return
    source.close()
    if (eventSource === source) streamConnected.value = false
    void updateSelectedTask(taskId, generation).catch(notify)
  })
  source.onerror = () => {
    if (eventSource === source) streamConnected.value = false
    // EventSource resumes with Last-Event-ID automatically; polling also tracks completion.
  }
  taskPoll = setInterval(() => { void updateSelectedTask(taskId, generation).catch(() => {}) }, 2200)
}
async function openTask(task: Task) {
  const generation = ++taskRequest
  ++fileRequest
  stopStream()
  selectedTask.value = task
  selectedCitation.value = null
  fileContent.value = null
  logs.value = []
  page.value = 'workspace'
  question.value = task.question
  strategy.value = task.strategy
  mode.value = task.mode
  try {
    await updateSelectedTask(task.id, generation)
    if (generation !== taskRequest) return
    // Completed tasks replay their persisted trace through the same stream.
    connectTask(task.id, generation)
  } catch (error) { notify(error) }
}
async function submitTask() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    const task = await post<Task>('/tasks', { repo_id: selectedRepoId.value, question: question.value.trim(), strategy: strategy.value, mode: mode.value })
    await refreshTasks()
    await openTask(task)
  } catch (error) { notify(error) }
  finally { submitting.value = false }
}
async function resumeTask() {
  if (!selectedTask.value) return
  submitting.value = true
  try {
    const task = await post<Task>(`/tasks/${encodeURIComponent(selectedTask.value.id)}/resume`)
    await openTask(task)
    await refreshTasks()
  } catch (error) { notify(error) }
  finally { submitting.value = false }
}
function newTask() {
  ++taskRequest
  ++fileRequest
  stopStream()
  selectedTask.value = null
  selectedCitation.value = null
  fileContent.value = null
  logs.value = []
  question.value = ''
  page.value = 'workspace'
}
async function chooseCitation(citation: Citation) {
  const generation = ++fileRequest
  selectedCitation.value = citation
  fileContent.value = null
  evidenceTab.value = 'code'
  fileLoading.value = true
  try {
    const start = Math.max(1, citation.start_line - 4)
    const end = Math.min(start + 199, citation.end_line + 4)
    const params = new URLSearchParams({ path: citation.path, start_line: String(start), end_line: String(end) })
    const source = await api<FileContent>(`/repos/${encodeURIComponent(selectedRepoId.value)}/file?${params}`)
    if (generation === fileRequest) fileContent.value = source
  } catch (error) { if (generation === fileRequest) notify(error) }
  finally { if (generation === fileRequest) fileLoading.value = false }
}
async function copyCode() {
  if (!selectedCitation.value) return
  try {
    await navigator.clipboard.writeText(fileContent.value?.content ?? selectedCitation.value.content)
    copied.value = true
    if (copyTimer) clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { copied.value = false }, 1800)
  } catch { notify('无法访问剪贴板，请直接选择代码复制。') }
}
async function openEvaluations() {
  page.value = 'evaluations'
  evaluationLoading.value = true
  try { evaluations.value = await api<EvaluationIndex>('/evaluations') }
  catch (error) { notify(error) }
  finally { evaluationLoading.value = false }
}
function evaluationName(run: string | EvaluationRun) {
  if (typeof run === 'string') return run
  return String(run.filename ?? run.name ?? run.id ?? run.path ?? '未命名评测')
}
async function loadEvaluation(run: string | EvaluationRun) {
  const generation = ++evaluationRequest
  const name = evaluationName(run).split(/[\\/]/).pop()!
  selectedEvaluation.value = name
  evaluationDoc.value = null
  evaluationLoading.value = true
  try {
    const report = await api<EvaluationReport>(`/evaluations/${encodeURIComponent(name)}`)
    if (generation === evaluationRequest) evaluationDoc.value = report
  }
  catch (error) { if (generation === evaluationRequest) notify(error) }
  finally { if (generation === evaluationRequest) evaluationLoading.value = false }
}
function duration(ms?: number) { return ms === undefined ? '—' : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s` }
function evaluationStrategyLabel(value: string) {
  return ({ bm25: 'BM25 检索', vector: '向量检索', hybrid: '混合检索', hybrid_rerank: '混合 + 词法重排', workflow_single: '单轮分析基线', workflow_agent: 'Agent 多轮定位' } as Record<string, string>)[value] ?? value
}
function evaluationPercent(value?: number | null) {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}%` : '—'
}
function evaluationNumber(value?: number | null) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—'
}
function evaluationTokens(row: EvaluationSummary) {
  const values = [row.prompt_tokens, row.completion_tokens].filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  return values.length ? evaluationNumber(values.reduce((total, value) => total + value, 0)) : '—'
}

onMounted(async () => {
  try {
    health.value = await api<Health>('/health')
    connected.value = true
    await refreshRepos()
  } catch (error) { notify(`无法连接本地服务：${error instanceof Error ? error.message : String(error)}`) }
  repoPoll = setInterval(async () => {
    try {
      await refreshRepos()
      connected.value = true
      if (!health.value) health.value = await api<Health>('/health')
    } catch { connected.value = false }
  }, 2500)
})
onBeforeUnmount(() => {
  stopStream()
  if (repoPoll) clearInterval(repoPoll)
  if (toastTimer) clearTimeout(toastTimer)
  if (copyTimer) clearTimeout(copyTimer)
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#" aria-label="RepoPilot 首页" @click.prevent="page = 'workspace'">
        <span class="brand-mark"><Icon name="pilot" :size="24" /></span>
        <span>RepoPilot<small>YOUR CODE, UNDERSTOOD.</small></span>
      </a>

      <div class="sidebar-label">工作空间 <span class="tiny-tag">LOCAL</span></div>
      <nav class="main-nav" aria-label="主导航">
        <button :class="{ active: page === 'workspace' }" @click="page = 'workspace'"><Icon name="grid" />定位工作台</button>
        <button :class="{ active: page === 'evaluations' }" @click="openEvaluations"><Icon name="chart" />评测实验室 <span class="nav-shortcut">↗</span></button>
      </nav>

      <div class="sidebar-section-heading"><span>代码仓库 <em>{{ repos.length }}</em></span><button class="icon-button" title="导入仓库" aria-label="导入仓库" @click="importOpen = true"><Icon name="plus" :size="16" /></button></div>
      <div class="repo-list">
        <button v-for="repo in repos" :key="repo.id" class="repo-item" :class="{ selected: repo.id === selectedRepoId }" @click="chooseRepo(repo)">
          <span class="repo-symbol"><Icon name="box" :size="17" /></span>
          <span class="repo-item-text"><strong>{{ repoName(repo) }}</strong><small><span class="status-dot" :class="repo.status" />{{ statusLabel(repo.status) }}<span v-if="repo.status === 'ready'"> · {{ repo.files_count }} 个文件</span></small></span>
        </button>
        <div v-if="!repos.length" class="sidebar-empty">还没有代码仓库<br>从演示项目开始探索</div>
        <button class="add-repo-button" @click="importOpen = true"><Icon name="plus" :size="16" />导入 GitHub 仓库</button>
      </div>

      <div class="sidebar-section-heading history-heading"><span>最近任务 <em>{{ tasks.length }}</em></span><button class="icon-button" title="新建任务" aria-label="新建任务" @click="newTask"><Icon name="plus" :size="16" /></button></div>
      <div class="task-list">
        <button v-for="task in tasks.slice(0, 12)" :key="task.id" class="history-item" :class="{ selected: selectedTask?.id === task.id }" @click="openTask(task)">
          <Icon :name="isActive(task.status) ? 'clock' : task.status === 'failed' || task.status === 'interrupted' ? 'warning' : 'file'" :size="15" />
          <span>{{ task.question }}<small>{{ relativeTime(task.created_at) }} · {{ statusLabel(task.status) }}</small></span>
        </button>
        <div v-if="!tasks.length" class="sidebar-empty compact">你的定位过程会保存在这里。</div>
      </div>

      <div class="sidebar-bottom">
        <div class="local-note"><Icon name="shield" :size="17" /><span>只读分析 · 源码有据<small>索引与任务保存在本地</small></span></div>
        <div class="connection-status"><span class="status-dot" :class="connected ? 'ready' : 'failed'" />{{ connected ? '本地服务已连接' : '等待本地服务' }}<span>v{{ health?.version ?? '0.1.0' }}</span></div>
      </div>
    </aside>

    <div class="main-shell">
      <header class="topbar">
        <div class="breadcrumb"><span>工作空间</span><Icon name="chevron" :size="13" /><strong>{{ page === 'workspace' ? '定位工作台' : '评测实验室' }}</strong></div>
        <div class="topbar-right"><span class="model-pill" :class="{ demo: health?.provider !== 'deepseek' }"><span class="status-dot" :class="connected ? 'ready' : 'failed'" />{{ modelLabel }}<span v-if="health?.provider === 'deepseek' && !health?.model_configured"> · 待配置</span></span><button class="icon-button" title="刷新仓库与任务" aria-label="刷新仓库与任务" @click="refreshRepos().then(refreshTasks).catch(notify)"><Icon name="refresh" :size="16" /></button></div>
      </header>

      <template v-if="page === 'workspace'">
        <div class="workspace-layout">
          <main class="workspace-main">
            <div class="mobile-workspace-tools"><select :value="selectedRepoId" aria-label="选择代码仓库" @change="mobileChooseRepo"><option v-if="!repos.length" value="">还没有仓库</option><option v-for="repo in repos" :key="repo.id" :value="repo.id">{{ repoName(repo) }} · {{ statusLabel(repo.status) }}</option></select><button class="icon-button" aria-label="导入仓库" @click="importOpen = true"><Icon name="plus" :size="18" /></button><select :value="selectedTask?.id ?? ''" aria-label="选择历史任务" @change="mobileChooseTask"><option value="">新任务</option><option v-for="task in tasks" :key="task.id" :value="task.id">{{ task.question.slice(0, 25) }}</option></select></div>
            <div class="page-heading"><div><div class="eyebrow">REPOSITORY INTELLIGENCE</div><h1>把问题，定位到代码<span>。</span></h1><p>沿着源码寻找线索，让每一次判断都有据可循。</p></div><span class="heading-orbit"><Icon name="pilot" :size="32" /></span></div>

            <div v-if="health?.provider === 'demo'" class="mode-banner"><Icon name="info" :size="17" /><p><strong>本地演示模式</strong>使用确定性流程生成证据摘要，未调用大模型。真实问题分析可在配置 DeepSeek 后开启。</p><span>DEMO</span></div>
            <div v-else-if="health && !health.model_configured" class="mode-banner warning-banner"><Icon name="warning" :size="17" /><p><strong>DeepSeek 尚未配置</strong>请在项目的本地配置中填写密钥，然后重启服务。</p></div>

            <section v-if="selectedRepo" class="repository-context">
              <div class="context-title"><Icon name="box" :size="17" /><strong>{{ repoName(selectedRepo) }}</strong><span class="badge" :class="selectedRepo.status">{{ statusLabel(selectedRepo.status) }}</span></div>
              <div class="context-details"><span><Icon name="git" :size="13" />{{ shortRevision(selectedRepo.revision) }}</span><span>{{ selectedRepo.files_count || 0 }} 文件</span><span>{{ selectedRepo.chunks_count || 0 }} 代码片段</span></div>
              <div v-if="isActive(selectedRepo.status)" class="indexing-state"><span class="spinner" />正在获取仓库并构建索引，完成后即可开始定位。<div class="indeterminate"><span /></div></div>
              <div v-if="selectedRepo.status === 'failed'" class="inline-error"><p>{{ selectedRepo.error || '仓库导入失败，请重试。' }}</p><button class="text-button" @click="retryImport(selectedRepo)"><Icon name="refresh" :size="14" />重试导入</button></div>
            </section>

            <section class="question-card" :class="{ disabled: !repositoryReady }">
              <div class="card-label"><span><Icon name="sparkle" :size="17" />开始一次问题定位</span><button v-if="selectedTask" class="text-button" @click="newTask"><Icon name="plus" :size="14" />新任务</button><span v-else class="keyboard-hint">Ctrl + Enter 提交</span></div>
              <label class="visually-hidden" for="question">问题描述</label>
              <textarea id="question" v-model="question" :disabled="!repositoryReady || activeTask" placeholder="描述你遇到的问题，也可以粘贴错误栈或函数名…" maxlength="12000" @keydown.ctrl.enter.prevent="submitTask" />
              <div class="composer-footer"><div class="composer-options"><label><Icon name="search" :size="14" /><select v-model="strategy" aria-label="检索策略" :disabled="activeTask"><option value="bm25">BM25 检索</option><option value="vector">向量检索</option><option value="hybrid">混合检索</option><option value="hybrid_rerank">混合 + 词法重排</option></select></label><span class="option-separator" /><label><Icon name="layers" :size="14" /><select v-model="mode" aria-label="分析模式" :disabled="activeTask"><option value="agent">Agent 多轮定位</option><option value="single">单轮分析基线</option></select></label></div><button class="primary-button" :disabled="!canSubmit" @click="submitTask"><span v-if="submitting || activeTask" class="spinner" /><Icon v-else name="arrow" :size="16" />{{ activeTask ? '正在分析' : '开始定位' }}</button></div>
            </section>
            <div class="embedding-note"><Icon name="info" :size="12" /><span v-if="health?.embedding_provider === 'hash'">当前向量为 Hash 词法演示向量，不代表语义检索效果。</span><span v-else>嵌入提供方：{{ health?.embedding_provider ?? '等待服务连接' }} · 固定仓库版本，保留源码位置。</span></div>

            <section v-if="!selectedTask" class="welcome-section">
              <template v-if="!selectedRepo">
                <div class="empty-illustration"><div class="illustration-window"><span /><span /><span /><div class="illustration-code"><i /><i /><i /><i /></div></div><span class="illustration-lens"><Icon name="search" :size="24" /></span><span class="illustration-spark">✦</span></div>
                <h2>从一个代码仓库开始</h2><p>导入公开 GitHub 项目，或用内置示例体验完整定位流程。</p>
                <div class="welcome-actions"><button class="primary-button" :disabled="importing || !connected" @click="importRepo('demo')"><Icon name="sparkle" :size="16" />{{ importing ? '正在导入…' : '体验演示项目' }}</button><button class="secondary-button" @click="importOpen = true"><Icon name="plus" :size="16" />导入仓库</button></div>
              </template>
              <template v-else>
                <div class="section-title"><Icon name="terminal" :size="16" /><h2>{{ selectedRepo.source === 'demo' ? '试试这些问题' : '准备好，开始阅读代码' }}</h2><span>EXPLORE</span></div>
                <div v-if="selectedRepo.source === 'demo'" class="sample-grid"><button v-for="(sample, index) in sampleQuestions" :key="sample" :disabled="!repositoryReady" @click="question = sample"><span class="sample-number">0{{ index + 1 }}</span><p>{{ sample }}</p><Icon name="arrow" :size="15" /></button></div>
                <p v-else class="empty-context-hint">输入具体的异常现象、函数名或错误栈，通常能帮助检索更快找到相关代码。</p>
              </template>
              <div class="workflow-strip"><div><span>01</span><strong>检索线索</strong><small>源码与符号索引</small></div><Icon name="chevron" :size="14" /><div><span>02</span><strong>核对证据</strong><small>有限多轮搜索</small></div><Icon name="chevron" :size="14" /><div><span>03</span><strong>定位问题</strong><small>引用文件与行号</small></div></div>
            </section>

            <section v-if="selectedTask && (activeTask || selectedTask.status === 'failed' || selectedTask.status === 'interrupted')" class="execution-card">
              <div class="section-title"><span v-if="activeTask" class="spinner" /><Icon v-else name="warning" :size="17" /><h2>{{ activeTask ? '正在沿着线索分析' : '任务需要继续处理' }}</h2><span class="badge" :class="selectedTask.status">{{ statusLabel(selectedTask.status) }}</span></div>
              <p class="execution-description">{{ activeTask ? '每一步工具调用都会记录在运行轨迹中，结果将自动显示。' : selectedTask.error || '执行被中断，可从保存的状态继续。' }}</p>
              <div v-if="logs.length" class="current-step"><Icon name="chevron" :size="14" /><span>{{ logs[logs.length - 1]?.message }}</span></div>
              <button v-if="!activeTask" class="secondary-button" :disabled="submitting" @click="resumeTask"><Icon name="refresh" :size="15" />从保存状态恢复</button>
            </section>

            <div v-if="result" class="analysis-results">
              <div class="report-header"><div class="section-title"><span class="success-icon"><Icon name="check" :size="14" /></span><h2>定位报告</h2><span class="report-id">{{ selectedTask?.id.slice(0, 8) }}</span></div><span class="report-time">{{ relativeTime(selectedTask?.updated_at ?? '') }}</span></div>
              <div class="report-summary"><span class="eyebrow">ANALYSIS SUMMARY</span><p>{{ result.summary }}</p></div>
              <div class="metrics-grid"><div><Icon name="clock" :size="15" /><strong>{{ duration(result.metrics.elapsed_ms) }}</strong><span>本次耗时</span></div><div><Icon name="layers" :size="15" /><strong>{{ result.metrics.rounds }}</strong><span>检索轮次</span></div><div><Icon name="terminal" :size="15" /><strong>{{ result.metrics.tool_calls }}</strong><span>工具调用</span></div><div><Icon name="sparkle" :size="15" /><strong>{{ ((result.metrics.prompt_tokens ?? 0) + (result.metrics.completion_tokens ?? 0)).toLocaleString() }}</strong><span>{{ result.metrics.provider === 'demo' ? 'Token · 演示未调用' : 'Token 消耗' }}</span></div></div>
              <article v-for="(finding, index) in result.findings" :key="index" class="finding-card"><div class="finding-heading"><span class="finding-number">{{ String(index + 1).padStart(2, '0') }}</span><h3>{{ finding.title }}</h3></div><p>{{ finding.explanation }}</p><div class="citation-chips"><button v-for="citation in finding.citations" :key="`${citation.path}:${citation.start_line}`" @click="chooseCitation(citation)"><Icon name="file" :size="13" /><span>{{ citation.path }}</span><em>L{{ citation.start_line }}–{{ citation.end_line }}</em><Icon name="chevron" :size="12" /></button></div></article>
              <section v-if="result.suggestions?.length" class="report-section"><div class="section-title"><Icon name="sparkle" :size="16" /><h2>建议的下一步</h2></div><ol class="suggestion-list"><li v-for="suggestion in result.suggestions" :key="suggestion">{{ suggestion }}</li></ol></section>
              <section v-if="result.uncertainties?.length" class="uncertainty-card"><div><Icon name="info" :size="17" /><h3>仍需验证的内容</h3></div><ul><li v-for="uncertainty in result.uncertainties" :key="uncertainty">{{ uncertainty }}</li></ul></section>
              <div class="report-footer"><Icon name="shield" :size="13" /><span>仅提供定位与建议，未执行仓库代码</span><span>{{ stopReasonLabel(result.stop_reason) }}</span></div>
            </div>
            <footer class="workspace-footer"><span>RepoPilot</span><span>让代码理解，回到证据本身。</span></footer>
          </main>

          <aside class="evidence-panel">
            <div class="panel-heading"><Icon name="code" :size="18" /><h2>证据与过程</h2><span class="evidence-count">{{ citations.length }}</span></div>
            <div class="panel-tabs"><button :class="{ active: evidenceTab === 'code' }" @click="evidenceTab = 'code'">源码证据 <span>{{ citations.length }}</span></button><button :class="{ active: evidenceTab === 'trace' }" @click="evidenceTab = 'trace'">运行轨迹 <span>{{ logs.length }}</span><i v-if="activeTask" class="live-dot" /></button></div>
            <template v-if="evidenceTab === 'code'">
              <div v-if="!citations.length" class="panel-empty"><span><Icon name="file" :size="28" /></span><h3>让判断有迹可循</h3><p>分析完成后，相关文件、函数与代码行号将显示在这里。</p><div class="empty-code-lines"><i /><i /><i /><i /><i /></div><small><Icon name="link" :size="12" />每条引用关联固定仓库版本</small></div>
              <template v-else>
                <div class="evidence-list"><button v-for="(citation, index) in citations" :key="`${citation.path}:${citation.start_line}:${citation.end_line}`" :class="{ active: selectedCitation?.path === citation.path && selectedCitation?.start_line === citation.start_line }" @click="chooseCitation(citation)"><span class="file-kind">{{ citation.path.endsWith('.py') ? 'PY' : 'MD' }}</span><span><strong>{{ citation.path }}</strong><small>{{ citation.symbol || '文件内容' }} · L{{ citation.start_line }}–{{ citation.end_line }}</small></span><em>{{ index + 1 }}</em></button></div>
                <div v-if="selectedCitation" class="source-viewer"><div class="source-toolbar"><span><Icon name="git" :size="12" />{{ shortRevision(selectedRepo?.revision) }}</span><button class="icon-button" :title="copied ? '已复制' : '复制代码'" :aria-label="copied ? '已复制' : '复制代码'" @click="copyCode"><Icon :name="copied ? 'check' : 'copy'" :size="14" /></button></div><div class="source-caption"><Icon name="file" :size="13" /><span>{{ selectedCitation.path }}</span><span v-if="fileLoading" class="spinner" /></div><div class="code-scroll"><pre><code><span v-for="line in visibleCode" :key="line.number" class="code-line" :class="{ highlighted: line.highlighted }"><span class="line-number">{{ line.number }}</span><span class="line-content">{{ line.text || ' ' }}</span></span></code></pre></div><div class="source-note"><span class="highlight-swatch" />高亮范围为当前引用的源码位置</div></div>
              </template>
            </template>
            <template v-else>
              <div v-if="!logs.length" class="panel-empty"><span><Icon name="layers" :size="28" /></span><h3>看见每一步推理依据</h3><p>任务开始后，这里会记录检索、证据检查与报告生成过程。</p></div>
              <div v-else class="trace-list"><div class="trace-status"><span class="status-dot" :class="activeTask ? 'running' : 'ready'" />{{ activeTask ? streamConnected ? '实时事件已连接' : '正在重连，任务仍在后台运行' : '已保存的执行记录' }}</div><div v-for="(log, index) in logs" :key="log.id ?? index" class="trace-item"><span class="trace-node"><Icon :name="log.node === 'report' || log.node === 'finish' ? 'file' : log.node === 'search' || log.node === 'retrieve' ? 'search' : 'layers'" :size="13" /></span><div><div class="trace-title"><strong>{{ log.node }}</strong><span v-if="log.round">第 {{ log.round }} 轮</span></div><p>{{ log.message }}</p></div></div></div>
            </template>
            <div class="panel-footnote"><Icon name="info" :size="13" /><span>引用存在不等于结论正确，仍需结合上下文验证。</span></div>
          </aside>
        </div>
      </template>

      <main v-else class="evaluation-page">
        <div class="page-heading"><div><div class="eyebrow">MEASURE, COMPARE, UNDERSTAND.</div><h1>用实验，检验每次改进<span>。</span></h1><p>查看固定数据集上的真实结果，比较定位效果、时延与调用成本。</p></div><button class="secondary-button" :disabled="evaluationLoading" @click="openEvaluations"><Icon name="refresh" :size="15" />刷新结果</button></div>
        <div class="evaluation-principles"><div><span>01</span><strong>版本固定</strong><p>源码与评测样本对应同一提交。</p></div><div><span>02</span><strong>基线可比</strong><p>区分检索优化与 Agent 循环收益。</p></div><div><span>03</span><strong>结果可查</strong><p>保存原始记录，不预设性能提升。</p></div></div>
        <div class="evaluation-layout">
          <section class="evaluation-runs">
            <div class="section-title"><Icon name="chart" :size="17" /><h2>实验记录</h2><span>{{ evaluations.runs.length }}</span></div>
            <p class="evaluation-note">{{ evaluations.note || '这里仅展示已经运行并保存的评测结果。' }}</p>
            <button v-for="run in evaluations.runs" :key="evaluationName(run)" :class="{ active: selectedEvaluation === evaluationName(run).split(/[\\/]/).pop() }" @click="loadEvaluation(run)"><Icon name="file" :size="17" /><span>{{ evaluationName(run) }}</span><Icon name="chevron" :size="14" /></button>
            <div v-if="!evaluations.runs.length" class="evaluation-empty"><Icon name="chart" :size="28" /><h3>还没有已保存的实验</h3><p>在项目中运行评测命令后，结果会显示在这里。启动与评测方法见项目 README。</p></div>
          </section>
          <section class="evaluation-output">
            <div class="section-title"><Icon name="chart" :size="17" /><h2>{{ selectedEvaluation || '评测对比报告' }}</h2><span v-if="evaluationLoading" class="spinner" /></div>
            <template v-if="evaluationDoc">
              <div class="evaluation-report-heading"><h3>{{ evaluationDoc.name }}</h3><span>仅展示已保存的实际运行结果</span></div>
              <dl class="evaluation-meta">
                <div><dt>样本类型 · kind</dt><dd>{{ evaluationDoc.kind || '—' }}</dd></div>
                <div><dt>数据划分 · split</dt><dd>{{ evaluationDoc.split || '—' }}</dd></div>
                <div><dt>样本总数</dt><dd>{{ evaluationNumber(evaluationDoc.case_count) }}</dd></div>
                <div><dt>嵌入提供方</dt><dd>{{ evaluationDoc.embedding_provider || '—' }}</dd></div>
              </dl>
              <div v-if="evaluationDoc.embedding_provider === 'hash'" class="evaluation-context-note"><Icon name="info" :size="15" /><p>Hash 向量属于词法演示向量，本报告不代表语义嵌入模型的检索效果。“混合 + 词法重排”采用启发式排序。</p></div>
              <div v-if="evaluationDoc.kind === 'public-code-qa'" class="evaluation-context-note provenance-note"><Icon name="file" :size="15" /><p>本报告评估公开仓库的源码理解问答，未使用真实 GitHub Issue，不衡量缺陷修复成功率。</p></div>
              <div v-else-if="evaluationDoc.kind === 'synthetic-demo'" class="evaluation-context-note provenance-note"><Icon name="file" :size="15" /><p>本报告使用自建合成示例，用于演示流程和回归验证，不代表真实 GitHub Issue 上的效果。</p></div>
              <div v-if="evaluationSummaries.length" class="evaluation-table-scroll">
                <table class="evaluation-table">
                  <caption class="visually-hidden">不同检索策略与工作流的定位效果和运行成本对比</caption>
                  <thead><tr><th scope="col">策略</th><th scope="col">样本数</th><th scope="col">文件 Hit@5</th><th scope="col">文件 Recall@5</th><th scope="col">符号 Hit@5</th><th scope="col">P50 / P95<span>毫秒</span></th><th v-if="hasEvaluationTokens" scope="col">Token 总量</th></tr></thead>
                  <tbody><tr v-for="row in evaluationSummaries" :key="row.strategy"><th scope="row">{{ evaluationStrategyLabel(row.strategy) }}</th><td>{{ evaluationNumber(row.cases) }}</td><td>{{ evaluationPercent(row.file_hit_at_5) }}</td><td>{{ evaluationPercent(row.file_recall_at_5) }}</td><td>{{ evaluationPercent(row.symbol_hit_at_5) }}</td><td class="latency-cell">{{ evaluationNumber(row.p50_ms) }} / {{ evaluationNumber(row.p95_ms) }}</td><td v-if="hasEvaluationTokens">{{ evaluationTokens(row) }}</td></tr></tbody>
                </table>
              </div>
              <div v-else class="evaluation-empty summary-empty"><Icon name="chart" :size="23" /><p>这份报告尚未记录汇总指标，可展开原始 JSON 查看详情。</p></div>
              <p class="evaluation-metric-note">“—”表示未记录或不适用。Token 为输入与输出的合计。索引耗时单独记录；检索与工作流策略的时延覆盖范围详见原始报告。</p>
              <details class="evaluation-raw"><summary><Icon name="terminal" :size="14" /><span>查看完整原始 JSON</span><Icon name="down" :size="13" /></summary><pre>{{ evaluationPretty }}</pre></details>
            </template>
            <div v-else class="evaluation-empty"><Icon name="file" :size="30" /><p>{{ evaluationLoading ? '正在读取评测报告…' : '选择一份实验记录，查看完整指标、运行配置和逐题结果。' }}</p></div>
          </section>
        </div>
      </main>
    </div>

    <Transition name="toast"><div v-if="toast" class="toast-message" role="status"><Icon name="info" :size="18" /><span>{{ toast }}</span><button class="icon-button" aria-label="关闭提示" @click="toast = ''"><Icon name="close" :size="15" /></button></div></Transition>

    <div v-if="importOpen" class="modal-backdrop" @click.self="importOpen = false" @keydown.esc="importOpen = false"><section class="import-modal" role="dialog" aria-modal="true" aria-labelledby="import-title"><div class="modal-top"><span class="modal-icon"><Icon name="box" :size="23" /></span><button class="icon-button" aria-label="关闭导入窗口" @click="importOpen = false"><Icon name="close" :size="19" /></button></div><h2 id="import-title">连接一个代码仓库</h2><p>读取公开 Python 项目，锁定版本并构建源码索引。</p><form @submit.prevent="importRepo(repoUrl)"><label for="repo-url">GitHub 仓库地址</label><input id="repo-url" v-model="repoUrl" autofocus type="url" placeholder="https://github.com/owner/repository" required /><label for="repo-ref">分支、标签或提交版本 <span>可选</span></label><input id="repo-ref" v-model="repoRef" placeholder="HEAD" maxlength="200" /><div class="modal-note"><Icon name="shield" :size="15" /><span>只读取源码与文档，不执行仓库中的代码。</span></div><button class="primary-button full-width" :disabled="importing || !connected"><span v-if="importing" class="spinner" /><Icon v-else name="plus" :size="16" />{{ importing ? '正在提交…' : '导入并建立索引' }}</button></form><div class="modal-divider"><span>或者先熟悉一下</span></div><button class="demo-import-button" :disabled="importing || !connected" @click="importRepo('demo')"><span><Icon name="sparkle" :size="19" /><strong>使用内置演示仓库<small>无需 GitHub 账户，即刻开始探索</small></strong></span><Icon name="arrow" :size="17" /></button></section></div>
  </div>
</template>
