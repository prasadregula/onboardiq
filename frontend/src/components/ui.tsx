export const PRIORITY_STYLE: Record<string, string> = {
  sanctions: 'bg-rose-100 text-rose-800', pep: 'bg-orange-100 text-orange-800', high_risk: 'bg-amber-100 text-amber-800',
  identity: 'bg-fuchsia-100 text-fuchsia-800', possible_match: 'bg-yellow-100 text-yellow-800', medium_risk: 'bg-sky-100 text-sky-800', other: 'bg-slate-100 text-slate-700',
}
export const OUTCOME_STYLE: Record<string, string> = {
  auto_approve: 'bg-emerald-100 text-emerald-800', request_more: 'bg-sky-100 text-sky-800', escalate: 'bg-rose-100 text-rose-800', needs_review: 'bg-amber-100 text-amber-800',
}
export const STATUS_STYLE: Record<string, string> = {
  approved: 'bg-emerald-100 text-emerald-800', rejected: 'bg-slate-800 text-white', awaiting_customer: 'bg-sky-100 text-sky-800',
  needs_review: 'bg-amber-100 text-amber-800', escalated: 'bg-rose-100 text-rose-800', processing: 'bg-indigo-100 text-indigo-800', intake: 'bg-slate-100 text-slate-700',
}
export const SEVERITY_STYLE: Record<string, string> = { critical: 'bg-rose-100 text-rose-800', warning: 'bg-amber-100 text-amber-800', info: 'bg-slate-100 text-slate-700' }
export const RISK_STYLE: Record<string, string> = { low: 'bg-emerald-100 text-emerald-800', medium: 'bg-amber-100 text-amber-800', high: 'bg-rose-100 text-rose-800' }

export function Chip({ value, map, className = '' }: { value: string | null | undefined; map: Record<string, string>; className?: string }) {
  if (!value) return <span className="text-slate-400">—</span>
  return <span className={`chip ${map[value] ?? 'bg-slate-100 text-slate-700'} ${className}`}>{value.replace(/_/g, ' ')}</span>
}

export function fmt(ts: string | null | undefined) {
  if (!ts) return '—'
  const d = new Date(ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z')
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function ago(ts: string | null | undefined) {
  if (!ts) return ''
  const ms = Date.now() - new Date(ts.endsWith('Z') || ts.includes('+') ? ts : ts + 'Z').getTime()
  const h = Math.round(ms / 3.6e6)
  if (Math.abs(h) < 1) return `${Math.round(ms / 6e4)} min`
  if (Math.abs(h) < 48) return `${h} h`
  return `${Math.round(h / 24)} d`
}

export function Section({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="card p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-semibold text-slate-800">{title}</h3>
        {right}
      </div>
      {children}
    </section>
  )
}
