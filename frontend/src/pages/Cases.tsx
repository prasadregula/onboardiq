import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { Chip, OUTCOME_STYLE, RISK_STYLE, STATUS_STYLE, fmt } from '../components/ui'

export default function Cases() {
  const [rows, setRows] = useState<any[]>([])
  useEffect(() => { api.cases().then(setRows) }, [])
  return (
    <div className="card">
      <div className="p-4 border-b border-slate-100 font-semibold">All cases</div>
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-slate-500"><tr className="border-b border-slate-100">
          <th className="p-3">Applicant</th><th className="p-3">Type</th><th className="p-3">Status</th><th className="p-3">Agent outcome</th><th className="p-3">Risk</th><th className="p-3">Created</th></tr></thead>
        <tbody>{rows.map(c => (
          <tr key={c.id} className="border-b border-slate-50 hover:bg-slate-50">
            <td className="p-3"><Link to={`/cases/${c.id}`} className="font-medium text-indigo-700 hover:underline">{c.applicant_name ?? c.id}</Link><div className="text-xs text-slate-500">{c.external_ref} · {c.id}</div></td>
            <td className="p-3">{c.applicant_type}</td>
            <td className="p-3"><Chip value={c.status} map={STATUS_STYLE} /></td>
            <td className="p-3"><Chip value={c.outcome} map={OUTCOME_STYLE} /></td>
            <td className="p-3"><Chip value={c.risk_rating} map={RISK_STYLE} /> {c.risk_score != null && <span className="text-xs text-slate-500">{c.risk_score}</span>}</td>
            <td className="p-3 text-slate-500">{fmt(c.created_at)}</td>
          </tr>))}</tbody>
      </table>
    </div>
  )
}
