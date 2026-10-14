import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type QueueItem, type Stats } from '../lib/api'
import { hasRole, useMe } from '../components/Shell'
import { Chip, PRIORITY_STYLE, RISK_STYLE, ago } from '../components/ui'

const PRIORITIES = ['sanctions', 'pep', 'high_risk', 'identity', 'possible_match', 'medium_risk', 'other']

export default function Queue() {
  const [items, setItems] = useState<QueueItem[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [filter, setFilter] = useState<string>('all')
  const [mine, setMine] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const me = useMe()
  const canAct = hasRole(me, 'KycAnalyst', 'BsaOfficer', 'Admin')
  const load = () => Promise.all([api.queue(), api.stats()]).then(([q, s]) => { setItems(q); setStats(s); setErr(null) }).catch(e => setErr(String(e.message)))
  useEffect(() => { load() }, [])

  const claim = async (t: QueueItem) => { await api.act(t.task_id, { action: 'claim' }); load() }
  const shown = items.filter(i => (filter === 'all' || i.priority === filter) && (!mine || i.assigned_to === me?.subject))

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Stat label="Open review tasks" value={stats?.open_review_tasks ?? '—'} />
        <Stat label="Sanctions / PEP" value={(stats?.open_by_priority.sanctions ?? 0) + (stats?.open_by_priority.pep ?? 0)} tone="rose" />
        <Stat label="Escalated cases" value={stats?.cases_by_status.escalated ?? 0} tone="amber" />
        <Stat label="Awaiting customer" value={stats?.cases_by_status.awaiting_customer ?? 0} tone="sky" />
        <Stat label="Auto-approved" value={stats?.cases_by_status.approved ?? 0} tone="emerald" />
      </div>

      <div className="card">
        <div className="p-4 border-b border-slate-100 flex flex-wrap items-center gap-2">
          <h2 className="font-semibold mr-3">Review queue</h2>
          {['all', ...PRIORITIES].map(p => (
            <button key={p} onClick={() => setFilter(p)}
              className={`chip cursor-pointer ${filter === p ? 'ring-2 ring-indigo-500' : ''} ${p === 'all' ? 'bg-slate-100 text-slate-700' : PRIORITY_STYLE[p]}`}>
              {p.replace('_', ' ')}{p !== 'all' && stats?.open_by_priority[p] ? ` · ${stats.open_by_priority[p]}` : ''}
            </button>
          ))}
          <label className="ml-auto text-sm flex items-center gap-2"><input type="checkbox" checked={mine} onChange={e => setMine(e.target.checked)} /> Mine only</label>
          <button className="btn-ghost" onClick={load}>Refresh</button>
        </div>
        {err && <div className="p-4 text-rose-700 text-sm">{err} — is the API running on :8080?</div>}
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
            <tr className="border-b border-slate-100">
              <th className="p-3">Priority</th><th className="p-3">Applicant</th><th className="p-3">Why</th><th className="p-3">Risk</th>
              <th className="p-3">Assigned</th><th className="p-3">Age / SLA</th><th className="p-3"></th>
            </tr>
          </thead>
          <tbody>
            {shown.map(t => {
              const overdue = t.due_at && new Date(t.due_at + (t.due_at.endsWith('Z') ? '' : 'Z')).getTime() < Date.now()
              return (
                <tr key={t.task_id} className="border-b border-slate-50 hover:bg-slate-50">
                  <td className="p-3"><Chip value={t.priority} map={PRIORITY_STYLE} /></td>
                  <td className="p-3">
                    <Link to={`/cases/${t.case_id}`} className="font-medium text-indigo-700 hover:underline">{t.applicant_name ?? t.case_id}</Link>
                    <div className="text-xs text-slate-500">{t.applicant_type} · {t.case_id}</div>
                  </td>
                  <td className="p-3"><div className="flex flex-wrap gap-1">{t.reason_codes.map(c => <span key={c} className="chip bg-slate-100 text-slate-700 mono">{c}</span>)}</div></td>
                  <td className="p-3"><Chip value={t.risk_rating} map={RISK_STYLE} /></td>
                  <td className="p-3">{t.assigned_to ?? <span className="text-slate-400">unassigned</span>}</td>
                  <td className="p-3"><span className={overdue ? 'text-rose-700 font-semibold' : ''}>{ago(t.created_at)} old{overdue ? ' · SLA breached' : ''}</span></td>
                  <td className="p-3 text-right">
                    {t.status === 'open' && canAct ? <button className="btn-primary" onClick={() => claim(t)}>Claim</button>
                      : <Link to={`/cases/${t.case_id}`} className="btn-ghost">Open</Link>}
                  </td>
                </tr>
              )
            })}
            {shown.length === 0 && !err && <tr><td colSpan={7} className="p-8 text-center text-slate-400">Queue is empty</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function Stat({ label, value, tone = 'indigo' }: { label: string; value: number | string; tone?: string }) {
  const tones: Record<string, string> = { indigo: 'text-indigo-700', rose: 'text-rose-700', amber: 'text-amber-700', sky: 'text-sky-700', emerald: 'text-emerald-700' }
  return (
    <div className="card p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-2xl font-bold ${tones[tone]}`}>{value}</div>
    </div>
  )
}
