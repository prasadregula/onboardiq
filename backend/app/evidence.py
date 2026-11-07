"""Evidence pack (w6): one case, one run, as a record an auditor or examiner can read without the application.

GET /cases/{id}/evidence[?run_id=...][&format=json|html]  (Auditor, BsaOfficer, Admin)

It answers the questions a model-risk or compliance reviewer asks of an automated decision:
  * What was decided, by which rule, and did the model's recommendation change it?
  * Which model, prompt version and policy corpus were used? Which controls were on?
  * What exactly was the model shown? (the masked transcript, plus a SHA-256 of every request, recorded before the call)
  * Which agent identity ran each step, for which person, and what was refused?
  * Who reviewed it, and what did they decide?
  * Is the record intact? (the audit hash chain is verified when the pack is generated)

Every export is itself recorded in the case's audit chain (who, when), because the pack contains personal data.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from . import audit
from .config import get_settings
from .models import AuditEvent, Case, CaseRun, Document, Party, ReviewTask

STEP_EVENTS = ("tool_call", "tool_error", "tool_denied", "run_aborted", "run_failed")


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else v


def build(session: Session, case_id: str, run_id: str | None = None, generated_by: str = "system") -> dict:
    case = session.get(Case, case_id)
    if case is None:
        raise KeyError(case_id)
    runs = session.exec(select(CaseRun).where(CaseRun.case_id == case_id).order_by(CaseRun.started_at)).all()  # type: ignore[arg-type]
    run = next((r for r in runs if r.id == run_id), None) if run_id else (runs[-1] if runs else None)
    if run_id and run is None:
        raise KeyError(run_id)
    events = session.exec(select(AuditEvent).where(AuditEvent.case_id == case_id).order_by(AuditEvent.seq)).all()  # type: ignore[arg-type]
    in_run = [e for e in events if run and e.run_id == run.id]
    s = get_settings()

    def first(kind: str) -> dict:
        return next((e.payload for e in in_run if e.event_type == kind), {})

    started, decision = first("run_started"), first("decision")
    steps = []
    for e in in_run:
        if e.event_type in STEP_EVENTS:
            p = e.payload
            steps.append({"seq": e.seq, "ts": _iso(e.ts), "event": e.event_type, "tool": p.get("tool"),
                          "agent_role": p.get("agent_role") or p.get("caller_role"),
                          "identity": p.get("identity") or p.get("caller_identity"),
                          "on_behalf_of": (p.get("on_behalf_of") or {}).get("subject"),
                          "arguments": p.get("arguments"), "duration_ms": p.get("duration_ms"),
                          "detail": p.get("error") or p.get("reason") or p.get("code") or ""})
    transcript = [{"seq": e.seq, "role": e.payload.get("role"), "purpose": e.payload.get("purpose"),
                   "tool": e.payload.get("tool"), "content": e.payload.get("content"),
                   "tool_calls": e.payload.get("tool_calls")}
                  for e in in_run if e.event_type == "llm_message"]
    requests = [{"seq": e.seq, "ts": _iso(e.ts), **e.payload} for e in in_run if e.event_type == "model_request"]
    tasks = session.exec(select(ReviewTask).where(ReviewTask.case_id == case_id)).all()
    reviews = [e for e in events if e.event_type in ("review_action", "analyst_note")]
    exports = [e for e in events if e.event_type == "evidence_exported"]
    chain = audit.verify_chain(session, case_id)
    risk = (run.risk if run else None) or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "generated_by": generated_by,
        "case": {"id": case.id, "external_ref": case.external_ref, "applicant_type": case.applicant_type.value,
                 "status": case.status.value, "outcome": case.outcome.value if case.outcome else None,
                 "decision_reason": case.decision_reason, "risk_rating": case.risk_rating, "risk_score": case.risk_score,
                 "missing_items": case.missing_items, "created_at": _iso(case.created_at)},
        "parties": [{"role": p.role, "name": p.name, "dob": p.dob, "country": p.country_of_residence or p.nationality,
                     "ownership_pct": p.ownership_pct}
                    for p in session.exec(select(Party).where(Party.case_id == case_id)).all()],
        "documents": [{"id": d.id, "type": d.doc_type, "filename": d.filename,
                       "confidence": (d.extracted or {}).get("confidence") or (d.mock_extraction or {}).get("confidence")}
                      for d in session.exec(select(Document).where(Document.case_id == case_id)).all()],
        "run": None if run is None else {
            "id": run.id, "mode": run.mode, "status": run.status, "started_at": _iso(run.started_at),
            "finished_at": _iso(run.finished_at), "model": run.model, "prompt_version": run.prompt_version,
            "outcome": run.outcome.value if run.outcome else None, "gate": run.gate, "error": run.error,
            "model_recommendation": run.model_recommendation, "model_rationale": run.model_rationale,
            "tool_calls": run.tool_calls, "llm_calls": run.llm_calls, "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out, "cost_usd": run.cost_usd, "initiated_by": started.get("initiated_by"),
            "budget": started.get("budget")},
        "decision": decision, "findings": (run.findings if run else []) or [],
        "risk": {k: risk.get(k) for k in ("rating", "score", "factors", "citations", "rejected_model_factors",
                                           "rationale", "model_used") if k in risk},
        "controls": {"environment": s.env, "llm_route": f"gateway:{s.llm_gateway}" if s.llm_gateway else "direct",
                     "pii_masking": "pseudonymise+azure-language" if s.pii_endpoint else "pseudonymise",
                     "injection_shield": "prompt_shields+patterns" if s.content_safety_endpoint else "patterns",
                     "policy_store": "azure-ai-search" if s.search_endpoint else "local",
                     "minimisation": first("model_view_minimised")},
        "steps": steps, "model_requests": requests, "transcript": transcript,
        "review": {"tasks": [{"priority": t.priority, "status": t.status, "assigned_to": t.assigned_to,
                              "action": t.analyst_action, "notes": t.analyst_notes, "created_at": _iso(t.created_at),
                              "resolved_at": _iso(t.resolved_at)} for t in tasks],
                   "actions": [{"seq": e.seq, "ts": _iso(e.ts), "actor": e.actor, "event": e.event_type,
                                "payload": e.payload} for e in reviews]},
        "exports": [{"seq": e.seq, "ts": _iso(e.ts), "actor": e.actor} for e in exports],
        "audit": {"verified": chain["ok"], "events": chain["events"], "head": chain.get("head"),
                  "broken_at_seq": chain.get("broken_at_seq"),
                  "events_list": [{"seq": e.seq, "ts": _iso(e.ts), "actor": e.actor, "event": e.event_type,
                                   "run_id": e.run_id, "hash": e.hash} for e in events]},
    }


# ----------------------------------------------------------------------------------------------- HTML rendering
_CSS = """
:root{--fg:#17202b;--muted:#5a6878;--line:#d6dde5;--soft:#f3f6f9;--ok:#1f7a3a;--bad:#b23a3a;--acc:#0b5d6b}
*{box-sizing:border-box}body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);margin:0;background:#fff}
main{max-width:1000px;margin:0 auto;padding:28px 20px 60px}h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;
padding-bottom:4px;border-bottom:2px solid var(--acc)}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px 18px}
.kv b{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:6px}th,td{border-bottom:1px solid var(--line);padding:5px 6px;text-align:left;vertical-align:top}
th{background:var(--soft);font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
code,pre{font-family:ui-monospace,Consolas,monospace;font-size:11.5px}pre{white-space:pre-wrap;word-break:break-word;background:var(--soft);padding:8px;border-radius:4px;margin:4px 0}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:600;font-size:12px}.ok{background:#e6f4ea;color:var(--ok)}.bad{background:#fbe9e9;color:var(--bad)}
.note{background:#fff8e1;border-left:3px solid #c99a00;padding:8px 10px;margin:8px 0;font-size:13px}
details{margin:4px 0}summary{cursor:pointer;font-weight:600}
@media print{details{display:block}details>*{display:block}summary{list-style:none}a{color:inherit}}
"""


_TS = __import__("re").compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(\+00:00|Z)?$")


def _e(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, str) and (m := _TS.match(v)):
        return f"{m.group(1)} {m.group(2)} UTC"
    if isinstance(v, (dict, list)):
        return html.escape(json.dumps(v, default=str))
    return html.escape(str(v))


def _table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    if not rows:
        return '<p class="muted">None.</p>'
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in cols)
    body = "".join("<tr>" + "".join(f"<td>{_e(r.get(k))}</td>" for k, _ in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _kv(pairs: list[tuple[str, Any]]) -> str:
    return '<div class="grid">' + "".join(f'<div class="kv"><b>{html.escape(k)}</b>{_e(v)}</div>' for k, v in pairs) + "</div>"


def _budget(b: dict | None) -> str | None:
    if not b:
        return None
    return (f'{b.get("max_tool_calls")} tool calls · {b.get("max_tokens"):,} tokens · '
            f'US${b.get("max_cost_usd")} · {b.get("max_seconds")} s')


def render_html(p: dict) -> str:
    c, r, a = p["case"], p["run"] or {}, p["audit"]
    badge = (f'<span class="badge ok">Audit chain verified · {a["events"]} events</span>' if a["verified"]
             else f'<span class="badge bad">AUDIT CHAIN BROKEN at event #{a["broken_at_seq"]}</span>')
    d = p["decision"]
    tightened = d.get("model_tightened")
    model_effect = ("The model's recommendation made the outcome stricter." if tightened else
                    "The policy gates decided; the model's recommendation could only make the outcome stricter and did not.")
    transcript = "".join(
        f'<details><summary>#{m["seq"]} · {html.escape(str(m["role"]))}'
        f'{" · " + html.escape(m["tool"]) if m.get("tool") else ""}{" · " + html.escape(m["purpose"]) if m.get("purpose") else ""}</summary>'
        f'<pre>{_e(m["content"] if m.get("content") is not None else m.get("tool_calls"))}</pre></details>'
        for m in p["transcript"]) or '<p class="muted">No model was used for this run (scripted mode).</p>'
    title = f'Evidence pack · {c["external_ref"] or c["id"]}'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{_CSS}</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="muted">OnboardIQ automated KYC decision record · case {_e(c["id"])} · run {_e(r.get("id"))} ·
generated {_e(p["generated_at"])} by {_e(p["generated_by"])}</p>
<p>{badge}</p>
<div class="note">Contains personal data. This export is recorded in the case's audit trail. Fictional demo data.</div>

<h2>1. Decision</h2>
{_kv([("Outcome", c["outcome"]), ("Gate", r.get("gate")), ("Risk", f'{c["risk_rating"]} (score {c["risk_score"]})'),
      ("Model recommended", r.get("model_recommendation")), ("Case status", c["status"]), ("Decided", r.get("finished_at"))])}
<p><b>Reason:</b> {_e(c["decision_reason"])}</p>
<p><b>Effect of the model:</b> {html.escape(model_effect)}</p>
<p><b>Model rationale</b> (names restored for the reader; the model only saw tokens): {_e(r.get("model_rationale"))}</p>
{('<p><b>Requested from the customer:</b> ' + _e(c["missing_items"]) + '</p>') if c["missing_items"] else ''}

<h2>2. Model, versions and controls in effect</h2>
{_kv([("Model (provider version)", r.get("model")), ("Prompt version", r.get("prompt_version")), ("Mode", r.get("mode")),
      ("Model route", p["controls"]["llm_route"]), ("PII masking", p["controls"]["pii_masking"]),
      ("Injection screening", p["controls"]["injection_shield"]), ("Policy store", p["controls"]["policy_store"]),
      ("Environment", p["controls"]["environment"]), ("Run budget", _budget(r.get("budget"))),
      ("Model calls / tool calls", f'{r.get("llm_calls")} / {r.get("tool_calls")}'),
      ("Tokens in / out", f'{r.get("tokens_in")} / {r.get("tokens_out")}'), ("Est. cost (US$)", r.get("cost_usd")),
      ("Initiated by", (r.get("initiated_by") or {}).get("subject"))])}

<h2>3. Parties and documents</h2>
{_table(p["parties"], [("role", "Role"), ("name", "Name"), ("dob", "Date of birth"), ("country", "Country"), ("ownership_pct", "Ownership %")])}
{_table(p["documents"], [("type", "Document"), ("filename", "File"), ("confidence", "Extraction confidence"), ("id", "Id")])}

<h2>4. Findings</h2>
{_table(p["findings"], [("code", "Code"), ("severity", "Severity"), ("message", "Message"), ("source_tool", "Raised by")])}

<h2>5. Risk rating</h2>
{_kv([("Rating", p["risk"].get("rating")), ("Score", p["risk"].get("score")), ("Grounding model", p["risk"].get("model_used"))])}
{_table(p["risk"].get("factors") or [], [("name", "Factor"), ("weight", "Weight"), ("policy_ref", "Policy"), ("source", "Source")])}
<p class="muted">Model-proposed factors rejected for citing a policy section that was not retrieved:</p>
{_table(p["risk"].get("rejected_model_factors") or [], [("name", "Factor"), ("policy_ref", "Cited"), ("reason", "Why rejected")])}
<p class="muted">Policy sections retrieved (with the reader's access trimming):</p>
{_table(p["risk"].get("citations") or [], [("ref", "Section"), ("score", "Score")])}

<h2>6. Agent steps: who did what, for whom</h2>
{_table(p["steps"], [("seq", "#"), ("ts", "Time"), ("event", "Event"), ("tool", "Tool"), ("identity", "Agent identity"),
                     ("on_behalf_of", "For"), ("duration_ms", "ms"), ("detail", "Refusal / error")])}

<h2>7. What the model was shown</h2>
<p>Each request was fingerprinted (SHA-256) and recorded in the audit chain <i>before</i> it was sent. Personal data
appears as tokens (PERSON_1, DOB_1, ADDRESS_1); document text is never sent.</p>
{_table(p["model_requests"], [("seq", "#"), ("ts", "Time"), ("purpose", "Purpose"), ("messages", "Messages"), ("bytes", "Bytes"), ("sha256", "SHA-256")])}
<p class="muted">Personal-data masking applied to this run:</p>
{_kv([(k.replace("_", " "), v) for k, v in (p["controls"]["minimisation"] or {}).items()])}
{transcript}

<h2>8. Human review</h2>
{_table(p["review"]["tasks"], [("priority", "Priority"), ("status", "Status"), ("assigned_to", "Analyst"), ("action", "Action"), ("notes", "Notes"), ("resolved_at", "Resolved")])}
{_table(p["review"]["actions"], [("seq", "#"), ("ts", "Time"), ("actor", "By"), ("event", "Event"), ("payload", "Detail")])}

<h2>9. Audit trail</h2>
<p>{badge} Head hash <code>{_e(a.get("head"))}</code>. Every event's hash covers the previous event's hash, so changing
or deleting any row breaks verification from that row on. Exports of this pack: {len(p["exports"])}.</p>
{_table(a["events_list"], [("seq", "#"), ("ts", "Time"), ("actor", "Actor"), ("event", "Event"), ("run_id", "Run"), ("hash", "Hash")])}
</main></body></html>"""
