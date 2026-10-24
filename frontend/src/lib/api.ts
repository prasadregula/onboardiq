import { authHeaders } from './auth'
export { ANALYST, AUTH_MODE } from './auth'

const BASE = import.meta.env.VITE_API_BASE ?? '/api'

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const auth = await authHeaders()
  // Fail visibly instead of spinning forever when the API is cold-starting or down.
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), 60_000)
  let r: Response
  try {
    r = await fetch(`${BASE}${path}`, {
    ...init,
    signal: ctl.signal,
    headers: { ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...auth, ...(init.headers ?? {}) },
    })
  } catch (e: any) {
    if (e?.name === 'AbortError') throw new Error('API did not respond within 60 s - it may be starting up. Retry in a minute.')
    throw new Error(`API unreachable (${e?.message ?? e}) - check the API is running and CORS allows this site.`)
  } finally {
    clearTimeout(timer)
  }
  if (!r.ok) {
    let detail = r.statusText
    try { detail = (await r.json()).detail ?? detail } catch { /* ignore */ }
    const rid = r.headers.get('X-Request-Id')
    throw new Error(`${r.status}: ${detail}${rid ? ` (request ${rid})` : ''}`)
  }
  if (r.status === 204) return undefined as T
  return r.json()
}

export type QueueItem = {
  task_id: string; case_id: string; applicant_name: string | null; applicant_type: string; priority: string
  reason_codes: string[]; status: string; assigned_to: string | null; outcome: string | null; risk_rating: string | null
  created_at: string; due_at: string | null
}
export type Finding = { code: string; severity: 'info' | 'warning' | 'critical'; message: string; source_tool: string; evidence: Record<string, unknown>; missing_item?: string | null }
export type CaseDetail = {
  case: { id: string; external_ref: string | null; applicant_type: string; applicant_name: string | null; status: string; outcome: string | null
    risk_rating: string | null; risk_score: number | null; created_at: string; declared: Record<string, unknown>; decision_reason: string | null; missing_items: string[] }
  parties: Array<{ id: string; role: string; name: string; dob: string | null; country_of_residence: string | null; nationality: string | null; ownership_pct: number | null; is_control_person: boolean }>
  documents: Array<{ id: string; doc_type: string; filename: string; party_id: string | null; confidence: number | null; extracted: { fields: Record<string, unknown>; provider: string; model: string } | null }>
  latest_run: null | { id: string; mode: string; model: string | null; status: string; outcome: string | null; gate: string | null; tool_calls: number; tokens_in: number; tokens_out: number; tokens_cached?: number; llm_calls?: number; llm_latency_ms?: number; cost_usd?: number
    findings: Finding[]; risk: null | { rating: string; score: number; floor_rating: string; floor_score: number; rationale: string; model_used: string | null
      factors: Array<{ name: string; weight: number; policy_ref: string; source: string }>; citations: Array<{ ref: string; policy_id: string; section: string; excerpt: string; score: number }> }
    tool_results: Record<string, any>; model_recommendation: string | null; model_rationale: string | null; started_at: string; finished_at: string | null }
  runs: Array<{ id: string; mode: string; status: string; outcome: string | null; gate: string | null; started_at: string; tool_calls: number }>
  review_task: null | { id: string; priority: string; status: string; assigned_to: string | null; reason_codes: string[]; due_at: string | null; analyst_action: string | null; analyst_notes: string | null }
}
export type AuditEvent = { seq: number; ts: string; actor: string; event_type: string; run_id: string | null; payload: any; hash: string; prev_hash: string }
export type Stats = { cases_by_status: Record<string, number>; open_review_tasks: number; open_by_priority: Record<string, number> }

export type Me = { subject: string; name: string; roles: string[]; kind: string }
export type AgentControl = {
  paused: boolean; reason: string | null; changed_by: string | null; changed_at: string | null; demo_reset_enabled: boolean
  budget: { max_tool_calls: number; max_tokens: number; max_cost_usd: number; max_seconds: number }
  history: Array<{ ts: string; actor: string; action: string; reason: string | null }>
}

export const api = {
  health: () => req<any>('/health'),
  me: () => req<Me>('/me'),
  delegation: () => req<{ signed_in: string; delegated_as: string; display_name: string; matches_signed_in_user: boolean; note: string }>('/me/delegation'),
  runs: (id: string) => req<any[]>(`/cases/${id}/runs`),
  uploadDocument: (id: string, docType: string, file: File, partyId?: string) => {
    const fd = new FormData(); fd.append('doc_type', docType); fd.append('file', file); if (partyId) fd.append('party_id', partyId)
    return req<any>(`/cases/${id}/documents`, { method: 'POST', body: fd })
  },
  stats: () => req<Stats>('/stats'),
  queue: (status = 'open,claimed') => req<QueueItem[]>(`/review/queue?status=${status}`),
  cases: (status?: string) => req<any[]>(`/cases${status ? `?status=${status}` : ''}`),
  caseDetail: (id: string) => req<CaseDetail>(`/cases/${id}`),
  audit: (id: string) => req<AuditEvent[]>(`/cases/${id}/audit`),
  verify: (id: string) => req<{ ok: boolean; events: number; broken_at_seq?: number }>(`/cases/${id}/audit/verify`),
  run: (id: string, mode?: string) => req<any>(`/cases/${id}/run`, { method: 'POST', body: JSON.stringify(mode ? { mode } : {}) }),
  /** Start a run and wait for the case to leave `processing` (handles both 202-async and sync APIs). */
  runAndWait: async (id: string, mode?: string, timeoutMs = 120_000) => {
    await req<any>(`/cases/${id}/run`, { method: 'POST', body: JSON.stringify(mode ? { mode } : {}) })
    const t0 = Date.now()
    while (Date.now() - t0 < timeoutMs) {
      const d = await req<CaseDetail>(`/cases/${id}`)
      if (d.case.status !== 'processing') return d
      await new Promise(r => setTimeout(r, 1500))
    }
    throw new Error('run did not finish in time')
  },
  agents: () => req<AgentControl>('/admin/agents'),
  pauseAgents: (reason: string) => req<AgentControl>('/admin/agents/pause', { method: 'POST', body: JSON.stringify({ reason }) }),
  resumeAgents: (reason?: string) => req<AgentControl>('/admin/agents/resume', { method: 'POST', body: JSON.stringify({ reason: reason ?? null }) }),
  demoReset: () => req<any>('/admin/demo/reset', { method: 'POST' }),
  act: (taskId: string, body: Record<string, unknown>) => req<any>(`/review/tasks/${taskId}/action`, { method: 'POST', body: JSON.stringify(body) }),
}
