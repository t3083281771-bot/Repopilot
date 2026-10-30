export interface Health {
  status: string
  provider: string
  model: string
  model_configured: boolean
  embedding_provider: string
  version: string
}

export interface Repo {
  id: string
  source: string
  ref?: string
  revision: string
  status: string
  files_count: number
  chunks_count: number
  error?: string | null
  created_at: string
  updated_at: string
}

export interface Citation {
  chunk_id?: string
  id?: string
  path: string
  symbol: string
  start_line: number
  end_line: number
  content: string
  kind?: string
  score?: number
}

export interface Finding {
  title: string
  explanation: string
  citations: Citation[]
}

export interface AnalysisResult {
  summary: string
  findings: Finding[]
  suggestions: string[]
  uncertainties: string[]
  stop_reason: string
  metrics: {
    rounds: number
    tool_calls: number
    prompt_tokens: number
    completion_tokens: number
    elapsed_ms: number
    provider: string
    model: string
    embedding_provider: string
    [key: string]: unknown
  }
  evidence: Citation[]
}

export interface Task {
  id: string
  repo_id: string
  question: string
  strategy: string
  mode: string
  status: string
  result: AnalysisResult | null
  error: string | null
  created_at: string
  updated_at: string
}

export interface ProgressEvent {
  id?: number
  node: string
  message: string
  round?: number
  created_at?: string
  [key: string]: unknown
}

export interface FileContent {
  path: string
  start_line: number
  end_line: number
  content: string
}

export interface EvaluationSummary {
  strategy: string
  cases: number
  file_hit_at_5: number | null
  file_recall_at_5: number | null
  symbol_hit_at_5: number | null
  p50_ms: number | null
  p95_ms: number | null
  prompt_tokens?: number | null
  completion_tokens?: number | null
}

export interface EvaluationReport {
  name: string
  kind: string
  split: string
  case_count: number
  embedding_provider: string
  provider: string
  model: string
  created_at: string
  summaries: EvaluationSummary[]
  results: Record<string, unknown>[]
  notes?: string[]
}

export interface EvaluationRun extends Partial<EvaluationReport> {
  filename?: string
  id?: string
  path?: string
}

export interface EvaluationIndex {
  runs: (string | EvaluationRun)[]
  note: string
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
  })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const data = await response.json()
      detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? data)
    } catch { /* Preserve HTTP status if the response has no JSON body. */ }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export function post<T>(path: string, body?: unknown) {
  return api<T>(path, { method: 'POST', ...(body !== undefined ? { body: JSON.stringify(body) } : {}) })
}
