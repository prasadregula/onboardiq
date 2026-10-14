import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, type AuditEvent, type CaseDetail } from '../lib/api'
import { hasRole, useMe } from '../components/Shell'
import { Chip, OUTCOME_STYLE, PRIORITY_STYLE, RISK_STYLE, SEVERITY_STYLE, STATUS_STYLE, Section, fmt } from '../components/ui'

type Tab = 'findings' | 'screening' | 'documents' | 'ownership' | 'risk' | 'timeline' | 'audit'

export default function CasePage() {
  const { id = '' } = useParams()
  const [d, setD] = useState<CaseDetail | null>(null)
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [verify, setVerify] = useState<{ ok: boolean; events: number; broken_at_seq?: number } | null>(null)
  const [tab, setTab] = useState<Tab>('findings')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => Promise.all([api.caseDetail(id), api.audit(id), api.verify(id)])
    .then(([c, a, v]) => { setD(c); setAudit(a); setVerify(v); setErr(null) }).catch(e => setErr(e.message)), [id])
  useEffect(() => { load() }, [load])

  if (err) return <div className="text-rose-700">{err}</div>
  if (!d) return <div className="text-slate-400">Loading…</div>
  const run = d.latest_run
  const task = d.review_task
  const findings = run?.findings ?? []

  const act = async (body: Record<string, unknown>) => {
    if (!task) return
    setBusy(true)
    try { await api.act(task.id, body); await load() } catch (e: any) { alert(e.message) } finally { setBusy(false) }
  }
  const rerun = async () => { setBusy(true); try { await api.runAndWait(id); await load() } catch (e: any) { alert(e.message) } finally { setBusy(false) } }
  const me = useMe()
  const canAct = hasRole(me, 'KycAnalyst', 'BsaOfficer', 'Admin')
  const upload = async (docType: string, file: File) => { setBusy(true); try { await api.uploadDocument(id, docType, file); await load() } catch (e: any) { alert(e.message) } finally { setBusy(false) } }

  const tabs: Array<[Tab, string]> = [['findings', `Findings (${findings.length})`], ['screening', 'Screening'], ['documents', `Documents (${d.documents.length})`],
    ['ownership', 'Ownership'], ['risk', 'Risk & policy'], ['timeline', 'Agent timeline'], ['audit', `Audit (${audit.length})`]]

  return (
    <div className="space-y-4">
      <div className="text-sm text-slate-500"><Link to="/" className="hover:underline">Queue</Link> / {d.case.id}</div>
      <div className="card p-5 flex flex-wrap gap-6 items-start">
        <div className="min-w-[260px]">
          <h1 className="text-xl font-bold">{d.case.applicant_name ?? d.case.id}</h1>
          <div className="text-sm text-slate-500">{d.case.applicant_type} · ref {d.case.external_ref ?? '—'} · created {fmt(d.case.created_at)}</div>
          <div className="mt-2 flex flex-wrap gap-2 items-center">
            <Chip value={d.case.status} map={STATUS_STYLE} />
            <span className="text-xs text-slate-500">agent:</span><Chip value={d.case.outcome} map={OUTCOME_STYLE} />
            <Chip value={d.case.risk_rating} map={RISK_STYLE} />{d.case.risk_score != null && <span className="text-xs text-slate-500">score {d.case.risk_score}</span>}
            {task && <><span className="text-xs text-slate-500">priority:</span><Chip value={task.priority} map={PRIORITY_STYLE} /></>}
          </div>
        </div>
        <div className="flex-1 min-w-[280px]">
          <div className="text-xs uppercase tracking-wide text-slate-500">Decision · {run?.gate ?? '—'}</div>
          <p className="text-sm mt-1">{d.case.decision_reason ?? 'Not yet processed.'}</p>
          {d.case.missing_items.length > 0 && (
            <div className="mt-2 text-sm"><span className="font-medium">Requested from customer:</span>
              <ul className="list-disc ml-5 text-slate-700">{d.case.missing_items.map(m => <li key={m}>{m}</li>)}</ul></div>
          )}
          {run?.model_recommendation && (
            <div className="mt-2 text-xs text-slate-500">Model recommended <b>{run.model_recommendation}</b>{run.model_rationale ? ` — ${run.model_rationale}` : ''}</div>
          )}
        </div>
        {canAct ? <ActionPanel task={task} busy={busy} act={act} rerun={rerun} status={d.case.status} analyst={me?.subject ?? ''} isBsa={hasRole(me, 'BsaOfficer', 'Admin')} />
                : <div className="w-full md:w-72 text-xs text-slate-500 rounded-lg bg-slate-50 p-3">Read-only access ({me?.roles.join(', ')}).</div>}
      </div>

      <div className="flex gap-1 border-b border-slate-200">
        {tabs.map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)} className={`px-3 py-2 text-sm font-medium border-b-2 -mb-px ${tab === k ? 'border-indigo-600 text-indigo-700' : 'border-transparent text-slate-500 hover:text-slate-800'}`}>{label}</button>
        ))}
        {verify && <span className={`ml-auto self-center chip ${verify.ok ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-100 text-rose-800'}`}>
          {verify.ok ? `audit chain verified · ${verify.events} events` : `AUDIT CHAIN BROKEN at #${verify.broken_at_seq}`}</span>}
      </div>

      {tab === 'findings' && <Findings findings={findings} />}
      {tab === 'screening' && <Screening d={d} />}
      {tab === 'documents' && <Documents d={d} onUpload={upload} canAct={canAct} />}
      {tab === 'ownership' && <Ownership d={d} />}
      {tab === 'risk' && <Risk d={d} />}
      {tab === 'timeline' && <Timeline audit={audit} runId={run?.id} />}
      {tab === 'audit' && <Audit audit={audit} />}
    </div>
  )
}

function ActionPanel({ task, busy, act, rerun, status, analyst, isBsa }: { task: CaseDetail['review_task']; busy: boolean; act: (b: Record<string, unknown>) => Promise<void>; rerun: () => Promise<void>; status: string; analyst: string; isBsa: boolean }) {
  const [notes, setNotes] = useState('')
  const [msg, setMsg] = useState('')
  const [second, setSecond] = useState('')
  const [disp, setDisp] = useState('')
  if (!task || task.status === 'done') {
    return (
      <div className="w-full md:w-72 space-y-2">
        {task?.status === 'done' && <div className="text-sm rounded-lg bg-slate-50 p-3"><b>Resolved:</b> {task.analyst_action} by {task.assigned_to}<div className="text-slate-600">{task.analyst_notes}</div></div>}
        {status === 'processing' && <div className="text-sm text-indigo-700 animate-pulse">Agent run in progress…</div>}
        {(status === 'awaiting_customer' || status === 'intake') && <button className="btn-ghost w-full" disabled={busy} onClick={rerun}>{busy ? 'Running…' : 'Re-run agent'}</button>}
      </div>
    )
  }
  const mine = task.assigned_to === analyst || (isBsa && task.status === 'claimed')
  const disposition: Record<string, unknown> = {}
  if (disp) disposition.screening = disp
  if (second) disposition.second_approver = second
  return (
    <div className="w-full md:w-80 card p-4 bg-slate-50 space-y-2">
      <div className="text-xs uppercase tracking-wide text-slate-500">Analyst action · {task.status}{task.assigned_to ? ` by ${task.assigned_to}` : ''} · due {fmt(task.due_at)}</div>
      {task.status === 'open' && <button className="btn-primary w-full" disabled={busy} onClick={() => act({ action: 'claim' })}>Claim this case</button>}
      {task.status === 'claimed' && !mine && <div className="text-sm text-amber-700">Claimed by {task.assigned_to}. <button className="underline" onClick={() => act({ action: 'release' })}>Release</button></div>}
      {mine && (
        <>
          <textarea className="w-full text-sm rounded-lg border border-slate-300 p-2" rows={3} placeholder="Notes (required for approve/reject)" value={notes} onChange={e => setNotes(e.target.value)} />
          {(task.priority === 'sanctions' || task.priority === 'possible_match' || task.priority === 'pep') && (
            <select className="w-full text-sm rounded-lg border border-slate-300 p-2" value={disp} onChange={e => setDisp(e.target.value)}>
              <option value="">Screening disposition…</option><option value="false_positive">False positive</option><option value="true_positive">True positive</option><option value="insufficient_information">Insufficient information</option>
            </select>
          )}
          {task.priority === 'sanctions' && !isBsa && <input className="w-full text-sm rounded-lg border border-slate-300 p-2" placeholder="Second approver (four-eyes, required)" value={second} onChange={e => setSecond(e.target.value)} />}
          {task.priority === 'sanctions' && isBsa && <div className="text-xs text-slate-500">You are the BSA second approver for this sanctions case.</div>}
          <div className="grid grid-cols-2 gap-2">
            <button className="btn-primary" disabled={busy || !notes} onClick={() => act({ action: 'approve', notes, disposition })}>Approve</button>
            <button className="btn-danger" disabled={busy || !notes} onClick={() => act({ action: 'reject', notes, disposition })}>Reject</button>
          </div>
          <input className="w-full text-sm rounded-lg border border-slate-300 p-2" placeholder="Message to customer (for request more)" value={msg} onChange={e => setMsg(e.target.value)} />
          <div className="grid grid-cols-2 gap-2">
            <button className="btn-ghost" disabled={busy || !msg} onClick={() => act({ action: 'request_more', notes, message_to_customer: msg })}>Request more</button>
            <button className="btn-ghost" disabled={busy} onClick={() => act({ action: 'escalate_to_bsa', notes })}>Escalate to BSA</button>
          </div>
          <button className="text-xs text-slate-500 underline" onClick={() => act({ action: 'release' })}>Release claim</button>
        </>
      )}
    </div>
  )
}

function Findings({ findings }: { findings: CaseDetail['latest_run'] extends infer R ? (R extends { findings: infer F } ? F : never) : never }) {
  const order = { critical: 0, warning: 1, info: 2 }
  const rows = [...(findings as any[])].sort((a, b) => order[a.severity as keyof typeof order] - order[b.severity as keyof typeof order])
  return (
    <Section title="Findings">
      {rows.length === 0 && <div className="text-sm text-slate-400">No findings — all checks passed.</div>}
      <div className="space-y-2">
        {rows.map((f, i) => (
          <div key={i} className="flex gap-3 items-start rounded-lg border border-slate-100 p-3">
            <Chip value={f.severity} map={SEVERITY_STYLE} className="mt-0.5" />
            <div className="flex-1">
              <div className="text-sm"><span className="mono font-semibold">{f.code}</span> <span className="text-xs text-slate-400">via {f.source_tool}</span></div>
              <div className="text-sm text-slate-700">{f.message}</div>
              {f.missing_item && <div className="text-xs text-sky-700 mt-1">Ask customer for: {f.missing_item}</div>}
              {Object.keys(f.evidence ?? {}).length > 0 && <details className="mt-1"><summary className="text-xs text-slate-500 cursor-pointer">evidence</summary><pre className="text-xs bg-slate-50 rounded p-2 overflow-auto">{JSON.stringify(f.evidence, null, 1)}</pre></details>}
            </div>
          </div>
        ))}
      </div>
    </Section>
  )
}

function Screening({ d }: { d: CaseDetail }) {
  const r = d.latest_run?.tool_results?.screen_parties
  if (!r) return <Section title="Screening"><div className="text-sm text-slate-400">Not run.</div></Section>
  return (
    <Section title={`Screening · ${r.parties_screened} parties · provider ${r.provider}`}>
      {r.hits.length === 0 && <div className="text-sm text-emerald-700">No hits on sanctions, PEP or adverse-media lists.</div>}
      <table className="w-full text-sm">
        <thead className="text-xs uppercase text-slate-500 text-left"><tr><th className="p-2">Party</th><th className="p-2">Matched</th><th className="p-2">List</th><th className="p-2">Score</th><th className="p-2">Class</th><th className="p-2">Why</th></tr></thead>
        <tbody>{r.hits.map((h: any, i: number) => (
          <tr key={i} className="border-t border-slate-100">
            <td className="p-2">{h.party_name}<div className="text-xs text-slate-500">{h.party_role}</div></td>
            <td className="p-2">{h.matched_name}<div className="text-xs text-slate-500">{h.program} · {h.entry_id}</div></td>
            <td className="p-2 mono text-xs">{h.list_name}</td>
            <td className="p-2 font-semibold">{h.score.toFixed(2)}</td>
            <td className="p-2"><span className={`chip ${h.classification === 'confirmed' ? 'bg-rose-100 text-rose-800' : 'bg-amber-100 text-amber-800'}`}>{h.classification} {h.hit_type}</span></td>
            <td className="p-2 text-xs text-slate-600">{h.reasons.join(', ')}</td>
          </tr>))}</tbody>
      </table>
    </Section>
  )
}

function Documents({ d, onUpload, canAct }: { d: CaseDetail; onUpload: (t: string, f: File) => Promise<void>; canAct: boolean }) {
  const partyName = (pid: string | null) => d.parties.find(p => p.id === pid)?.name ?? '—'
  const comps = d.latest_run?.tool_results?.check_consistency?.comparisons ?? []
  const [docType, setDocType] = useState('utility_bill')
  const fileRef = useRef<HTMLInputElement>(null)
  return (
    <div className="space-y-4">
      <Section title="Documents & extraction" right={canAct && d.case.status !== 'processing' ? (
        <span className="flex items-center gap-2 text-sm">
          <select className="border rounded-lg px-2 py-1 text-sm" value={docType} onChange={e => setDocType(e.target.value)}>
            {['passport', 'drivers_license', 'national_id', 'utility_bill', 'bank_statement', 'articles_of_incorporation', 'ein_letter', 'ownership_declaration'].map(t => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
          </select>
          <button className="btn-ghost" onClick={() => fileRef.current?.click()}>Upload</button>
          <input ref={fileRef} type="file" className="hidden" accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff" onChange={e => e.target.files?.[0] && onUpload(docType, e.target.files[0])} />
        </span>) : undefined}>
        <div className="grid md:grid-cols-2 gap-3">
          {d.documents.map(doc => (
            <div key={doc.id} className="rounded-lg border border-slate-100 p-3">
              <div className="flex items-center justify-between">
                <div className="font-medium text-sm">{doc.doc_type.replace(/_/g, ' ')} <span className="text-xs text-slate-500">· {doc.filename}</span></div>
                {doc.confidence != null && <span className={`chip ${doc.confidence >= 0.8 ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'}`}>conf {doc.confidence.toFixed(2)}</span>}
              </div>
              <div className="text-xs text-slate-500">party: {partyName(doc.party_id)} · {doc.extracted ? `${doc.extracted.provider} / ${doc.extracted.model}` : 'not extracted'}</div>
              {doc.extracted && <dl className="kv mt-2 grid grid-cols-2 gap-x-3">{Object.entries(doc.extracted.fields).filter(([k]) => k !== '_text').map(([k, v]) => (
                <div key={k}><dt>{k}</dt><dd className="truncate" title={String(v)}>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd></div>))}</dl>}
            </div>
          ))}
        </div>
      </Section>
      <Section title="Cross-document comparisons">
        {comps.length === 0 && <div className="text-sm text-slate-400">No comparisons available.</div>}
        <table className="w-full text-sm"><thead className="text-xs uppercase text-slate-500 text-left"><tr><th className="p-2">Field</th><th className="p-2">Subject</th><th className="p-2">A</th><th className="p-2">B</th><th className="p-2">Result</th></tr></thead>
          <tbody>{comps.map((c: any, i: number) => (
            <tr key={i} className="border-t border-slate-100"><td className="p-2 mono text-xs">{c.field}</td><td className="p-2">{c.subject}</td>
              <td className="p-2"><span className="text-xs text-slate-500">{c.a_source}:</span> {String(c.a_value)}</td>
              <td className="p-2"><span className="text-xs text-slate-500">{c.b_source}:</span> {String(c.b_value)}</td>
              <td className="p-2"><span className={`chip ${c.agrees ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'}`}>{c.agrees ? 'agrees' : 'conflict'}{c.similarity != null ? ` ${c.similarity.toFixed(2)}` : ''}</span></td></tr>))}</tbody></table>
      </Section>
    </div>
  )
}

function Ownership({ d }: { d: CaseDetail }) {
  const r = d.latest_run?.tool_results?.resolve_beneficial_owners
  const parties = d.parties
  return (
    <div className="space-y-4">
      <Section title="Parties">
        <table className="w-full text-sm"><thead className="text-xs uppercase text-slate-500 text-left"><tr><th className="p-2">Role</th><th className="p-2">Name</th><th className="p-2">DOB</th><th className="p-2">Country</th><th className="p-2">Ownership</th><th className="p-2">Control</th></tr></thead>
          <tbody>{parties.map(p => <tr key={p.id} className="border-t border-slate-100"><td className="p-2">{p.role.replace('_', ' ')}</td><td className="p-2 font-medium">{p.name}</td><td className="p-2">{p.dob ?? '—'}</td><td className="p-2">{p.country_of_residence ?? p.nationality ?? '—'}</td><td className="p-2">{p.ownership_pct != null ? `${p.ownership_pct}%` : '—'}</td><td className="p-2">{p.is_control_person ? '✓' : ''}</td></tr>)}</tbody></table>
      </Section>
      {r?.applicable && (
        <Section title={`Beneficial ownership · ${r.declared_coverage_pct}% identified · threshold ${r.threshold_pct}% · control: ${r.control_person ?? 'none'}`}>
          <table className="w-full text-sm"><thead className="text-xs uppercase text-slate-500 text-left"><tr><th className="p-2">Owner</th><th className="p-2">Direct</th><th className="p-2">Effective</th><th className="p-2">Via</th><th className="p-2">Prong</th><th className="p-2">ID on file</th></tr></thead>
            <tbody>{r.owners.map((o: any) => <tr key={o.name} className="border-t border-slate-100"><td className="p-2 font-medium">{o.name}</td><td className="p-2">{o.direct_pct}%</td><td className="p-2 font-semibold">{o.effective_pct}%</td><td className="p-2 text-xs text-slate-500">{o.via.join(' → ') || 'direct'}</td>
              <td className="p-2">{o.meets_ownership_prong && <span className="chip bg-indigo-100 text-indigo-800">ownership</span>} {o.is_control_person && <span className="chip bg-violet-100 text-violet-800">control</span>}</td>
              <td className="p-2">{o.id_document_on_file ? <span className="text-emerald-700">✓</span> : <span className="text-rose-700">missing</span>}</td></tr>)}</tbody></table>
          {Array.isArray(d.case.declared.ownership_tree) && <details className="mt-3"><summary className="text-xs text-slate-500 cursor-pointer">declared ownership tree</summary>
            <ul className="text-sm mt-1 ml-4 list-disc">{(d.case.declared.ownership_tree as any[]).map((e, i) => <li key={i}>{e.owner} owns {e.pct}% of {e.of}</li>)}</ul></details>}
        </Section>
      )}
      {r && !r.applicable && <Section title="Beneficial ownership"><div className="text-sm text-slate-400">Not applicable (individual applicant).</div></Section>}
    </div>
  )
}

function Risk({ d }: { d: CaseDetail }) {
  const r = d.latest_run?.risk
  if (!r) return <Section title="Risk"><div className="text-sm text-slate-400">Not rated.</div></Section>
  return (
    <div className="grid md:grid-cols-2 gap-4">
      <Section title="Risk rating" right={<><Chip value={r.rating} map={RISK_STYLE} /> <span className="text-sm font-semibold">{r.score}</span></>}>
        <div className="text-xs text-slate-500 mb-2">policy floor: {r.floor_rating} ({r.floor_score}) · {r.model_used ? `model ${r.model_used} consulted` : 'deterministic scorer only'}</div>
        <table className="w-full text-sm"><tbody>{r.factors.map((f, i) => <tr key={i} className="border-t border-slate-100"><td className="p-2">{f.name}<div className="text-xs text-slate-500">{f.policy_ref} · {f.source}</div></td><td className="p-2 text-right font-semibold">+{f.weight}</td></tr>)}
          {r.factors.length === 0 && <tr><td className="p-2 text-slate-400">No risk factors present</td></tr>}</tbody></table>
        <p className="text-sm mt-3 text-slate-700">{r.rationale}</p>
      </Section>
      <Section title="Policy citations (RAG)">
        <div className="space-y-2">{r.citations.map(c => (
          <div key={c.ref + c.section} className="rounded-lg border border-slate-100 p-3">
            <div className="text-sm font-medium">{c.ref} <span className="text-xs text-slate-400">score {c.score.toFixed(2)}</span></div>
            <div className="text-xs text-slate-600 mt-1 whitespace-pre-line">{c.excerpt}…</div>
          </div>))}</div>
      </Section>
    </div>
  )
}

function Timeline({ audit, runId }: { audit: AuditEvent[]; runId?: string }) {
  const evs = audit.filter(e => e.run_id === runId && ['tool_call', 'tool_result', 'tool_error', 'llm_message', 'policy_gate', 'decision', 'run_started', 'run_finished'].includes(e.event_type))
  const colour: Record<string, string> = { tool_call: 'bg-indigo-500', tool_result: 'bg-emerald-500', tool_error: 'bg-rose-500', llm_message: 'bg-violet-500', policy_gate: 'bg-amber-500', decision: 'bg-slate-800', run_started: 'bg-slate-400', run_finished: 'bg-slate-400' }
  return (
    <Section title="Agent timeline (latest run)">
      <ol className="relative border-l border-slate-200 ml-2">{evs.map(e => (
        <li key={e.seq} className="ml-4 mb-3">
          <span className={`absolute -left-1.5 mt-1.5 w-3 h-3 rounded-full ${colour[e.event_type] ?? 'bg-slate-300'}`} />
          <div className="text-xs text-slate-500">#{e.seq} · {fmt(e.ts)} · {e.actor}</div>
          <div className="text-sm font-medium">{e.event_type}{e.payload?.tool ? ` · ${e.payload.tool}` : ''}{e.payload?.duration_ms != null ? ` · ${e.payload.duration_ms} ms` : ''}{e.payload?.gate ? ` · ${e.payload.gate} → ${e.payload.outcome}` : ''}</div>
          {e.event_type === 'tool_error' && <div className="text-sm text-rose-700">{e.payload.error}</div>}
          {e.event_type === 'llm_message' && <div className="text-xs text-slate-600 whitespace-pre-wrap max-h-32 overflow-auto">{e.payload.content ?? (e.payload.tool_calls ?? []).map((t: any) => `→ ${t.name}(${JSON.stringify(t.arguments)})`).join('\n')}</div>}
          {(e.event_type === 'tool_result' || e.event_type === 'decision') && <details><summary className="text-xs text-slate-500 cursor-pointer">payload</summary><pre className="text-xs bg-slate-50 rounded p-2 overflow-auto max-h-64">{JSON.stringify(e.payload, null, 1)}</pre></details>}
        </li>))}</ol>
    </Section>
  )
}

function Audit({ audit }: { audit: AuditEvent[] }) {
  return (
    <Section title="Audit trail (hash-chained)">
      <table className="w-full text-sm"><thead className="text-xs uppercase text-slate-500 text-left"><tr><th className="p-2">#</th><th className="p-2">When</th><th className="p-2">Actor</th><th className="p-2">Event</th><th className="p-2">Hash</th></tr></thead>
        <tbody>{audit.map(e => (
          <tr key={e.seq} className="border-t border-slate-100 align-top">
            <td className="p-2">{e.seq}</td><td className="p-2 whitespace-nowrap">{fmt(e.ts)}</td><td className="p-2">{e.actor}</td>
            <td className="p-2">{e.event_type}<details><summary className="text-xs text-slate-400 cursor-pointer">payload</summary><pre className="text-xs bg-slate-50 rounded p-2 overflow-auto max-h-48">{JSON.stringify(e.payload, null, 1)}</pre></details></td>
            <td className="p-2 mono text-xs text-slate-400">{e.hash.slice(0, 12)}…</td>
          </tr>))}</tbody></table>
    </Section>
  )
}
