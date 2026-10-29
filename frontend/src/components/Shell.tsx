import { createContext, useContext, useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { api, type AgentControl, type Me } from '../lib/api'
import { AUTH_MODE, account, initAuth, signIn, signOut } from '../lib/auth'

export const MeContext = createContext<Me | null>(null)
export const useMe = () => useContext(MeContext)
export const hasRole = (me: Me | null, ...roles: string[]) => !!me && roles.some(r => me.roles.includes(r))

export default function Shell() {
  const [health, setHealth] = useState<any>(null)
  const [me, setMe] = useState<Me | null>(null)
  const [authState, setAuthState] = useState<'loading' | 'signed_out' | 'ready' | 'error'>('loading')
  const [err, setErr] = useState<string | null>(null)
  const [deleg, setDeleg] = useState<{ ok: boolean; text: string } | null>(null)
  // Agent kill switch state (w4-3). Loaded once and after each action - deliberately NOT polled: polling would keep the
  // API and the serverless database awake all day (the readiness-probe cost lesson).
  const [ctl, setCtl] = useState<AgentControl | null>(null)
  const [ctlMsg, setCtlMsg] = useState<string | null>(null)
  const isAdmin = hasRole(me, 'Admin')
  const pauseAgents = async () => {
    const reason = window.prompt('Pause ALL agents. New runs are refused and running ones stop at their next step.\n\nReason (recorded):')
    if (!reason || reason.trim().length < 3) return
    try { setCtl(await api.pauseAgents(reason.trim())) } catch (e: any) { setCtlMsg(e.message) }
  }
  const resumeAgents = async () => {
    try { setCtl(await api.resumeAgents('resumed from the review queue')) } catch (e: any) { setCtlMsg(e.message) }
  }
  const checkRetrieval = async () => {
    setCtlMsg('Running the retrieval check (18 queries)…')
    try {
      const { summary: s } = await api.ragEval()
      setCtlMsg(`Retrieval check · ${s.store}: hit@${s.k} ${s.hit_rate.toFixed(2)} · recall ${s.recall.toFixed(2)} · MRR ${s.mrr.toFixed(2)} · access leaks ${s.access_leaks} · ${s.passed ? 'PASS' : 'FAIL'}`)
    } catch (e: any) { setCtlMsg(e.message) }
  }
  const resetDemo = async () => {
    if (!window.confirm('Delete the fictional golden cases and run all of them again? This takes about 2 minutes.')) return
    try { await api.demoReset(); setCtlMsg('Demo data is being reloaded. Refresh the queue in about 2 minutes.') }
    catch (e: any) { setCtlMsg(e.message) }
  }
  const verifyDelegation = async () => {
    setDeleg({ ok: true, text: 'Checking…' })
    try {
      const d = await api.delegation()
      setDeleg({ ok: d.matches_signed_in_user, text: d.matches_signed_in_user
        ? `On-behalf-of verified: Microsoft Graph sees ${d.delegated_as}`
        : `Graph returned ${d.delegated_as}, which does not match the signed-in user` })
    } catch (e: any) {
      setDeleg({ ok: false, text: `On-behalf-of not available: ${e.message}` })
    }
  }

  useEffect(() => {
    (async () => {
      try {
        await initAuth()
        if (AUTH_MODE === 'entra' && !account()) { setAuthState('signed_out'); return }
        const [h, m] = await Promise.all([api.health().catch(() => ({ ok: false })), api.me()])
        setHealth(h); setMe(m); setAuthState('ready')
        api.agents().then(setCtl).catch(() => setCtl(null))
      } catch (e: any) { setErr(e.message); setAuthState('error') }
    })()
  }, [])

  const link = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-1.5 rounded-lg text-sm font-medium ${isActive ? 'bg-indigo-600 text-white' : 'text-slate-600 hover:bg-slate-100'}`

  if (authState === 'signed_out') {
    return (
      <div className="min-h-screen grid place-items-center">
        <div className="card p-8 text-center space-y-3 max-w-sm">
          <span className="mx-auto w-12 h-12 rounded-xl bg-indigo-600 text-white grid place-items-center font-bold text-lg">IQ</span>
          <h1 className="font-semibold text-lg">OnboardIQ · KYC review</h1>
          <p className="text-sm text-slate-500">Sign in with your work account. Access is granted through OnboardIQ app roles.</p>
          <button className="btn-primary w-full" onClick={signIn}>Sign in with Microsoft</button>
        </div>
      </div>
    )
  }
  if (authState === 'error') {
    return <div className="min-h-screen grid place-items-center"><div className="card p-6 text-rose-700 max-w-md">{err}<div className="mt-3"><button className="btn-ghost" onClick={() => location.reload()}>Retry</button>{AUTH_MODE === 'entra' && <button className="btn-ghost ml-2" onClick={signOut}>Sign out</button>}</div></div></div>
  }

  return (
    <MeContext.Provider value={me}>
      <div className="min-h-screen">
        <header className="bg-white border-b border-slate-200">
          <div className="max-w-7xl mx-auto px-4 h-14 flex items-center gap-6">
            <div className="flex items-center gap-2">
              <span className="w-8 h-8 rounded-lg bg-indigo-600 text-white grid place-items-center font-bold">IQ</span>
              <span className="font-semibold">OnboardIQ</span>
              <span className="text-slate-400 text-sm">KYC review</span>
            </div>
            <nav className="flex gap-1">
              <NavLink to="/" end className={link}>Queue</NavLink>
              <NavLink to="/cases" className={link}>All cases</NavLink>
            </nav>
            <div className="ml-auto flex items-center gap-3 text-xs text-slate-500">
              {health && (
                <span className={`chip ${health.ok ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'}`} title={JSON.stringify(health)}>
                  {health.ok ? `API · ${health.orchestrator_mode} · ${health.llm}${health.chat_model ? ` · ${health.chat_model}` : ''} · ${health.screening_provider}` : 'API offline'}
                </span>
              )}
              {me && <span>{me.name} <span className="text-slate-400">·</span> {me.roles.map(r => <span key={r} className="chip bg-slate-100 text-slate-700 ml-1">{r}</span>)}</span>}
              {isAdmin && ctl && !ctl.paused && <button className="btn-ghost !py-0.5 text-rose-700" onClick={pauseAgents}
                title="Kill switch: stop every agent now (Admin only, recorded)">Pause agents</button>}
              {isAdmin && <button className="btn-ghost !py-0.5" onClick={checkRetrieval}
                title="Run the retrieval golden set against the live policy index (quality + access trimming)">Check retrieval</button>}
              {isAdmin && ctl?.demo_reset_enabled && <button className="btn-ghost !py-0.5" onClick={resetDemo}
                title="Reload and re-run the fictional golden cases (non-production only)">Reset demo data</button>}
              {AUTH_MODE === 'entra' && <button className="btn-ghost !py-0.5" onClick={verifyDelegation}
                title="Exchange your token on-behalf-of you for Microsoft Graph and confirm it is still you">Verify delegation</button>}
              {AUTH_MODE === 'entra' && <button className="btn-ghost !py-0.5" onClick={signOut}>Sign out</button>}
            </div>
          </div>
        </header>
        {ctl?.paused && (
          <div role="alert" className="text-sm px-4 py-2 bg-rose-600 text-white">
            <div className="max-w-7xl mx-auto flex items-center justify-between gap-3">
              <span><b>All agents are paused</b> by {ctl.changed_by}{ctl.changed_at ? ` at ${new Date(ctl.changed_at).toLocaleString()}` : ''}: {ctl.reason}.
                New runs are refused; running ones stop at their next step.</span>
              {isAdmin && <button className="px-3 py-0.5 rounded bg-white text-rose-700 font-medium" onClick={resumeAgents}>Resume agents</button>}
            </div>
          </div>
        )}
        {ctlMsg && (
          <div role="status" className="text-sm px-4 py-2 bg-slate-100 text-slate-800">
            <div className="max-w-7xl mx-auto flex items-center justify-between gap-3">
              <span>{ctlMsg}</span><button className="btn-ghost !py-0.5" onClick={() => setCtlMsg(null)}>Dismiss</button>
            </div>
          </div>
        )}
        {deleg && (
          <div role="status" className={`text-sm px-4 py-2 ${deleg.ok ? 'bg-emerald-50 text-emerald-800' : 'bg-amber-50 text-amber-900'}`}>
            <div className="max-w-7xl mx-auto flex items-center justify-between gap-3">
              <span>{deleg.text}</span>
              <button className="btn-ghost !py-0.5" onClick={() => setDeleg(null)}>Dismiss</button>
            </div>
          </div>
        )}
        <main className="max-w-7xl mx-auto px-4 py-6">{authState === 'loading' ? <div className="text-slate-400">Loading…</div> : <Outlet />}</main>
      </div>
    </MeContext.Provider>
  )
}
