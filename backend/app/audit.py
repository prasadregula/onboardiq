"""Append-only, hash-chained audit trail per case.

hash = sha256(prev_hash | seq | ts | actor | event_type | canonical_json(payload))
GET /cases/{id}/audit/verify recomputes the chain end to end.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from .models import AuditEvent, utcnow

GENESIS = "0" * 64
MAX_PAYLOAD_BYTES = 200_000


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _ts_key(ts: datetime) -> str:
    """Timezone-normalised timestamp so the hash survives DB round trips (SQLite drops tzinfo)."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts.isoformat(timespec="microseconds")


def compute_hash(prev_hash: str, seq: int, ts: datetime, actor: str, event_type: str, payload: dict) -> str:
    # payload is canonicalised through a JSON round trip so datetimes etc. hash the same way they are stored
    payload = json.loads(canonical(payload))
    material = "|".join([prev_hash, str(seq), _ts_key(ts), actor, event_type, canonical(payload)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _shrink(payload: dict) -> dict:
    raw = canonical(payload)
    if len(raw) <= MAX_PAYLOAD_BYTES:
        return payload
    return {"truncated": True, "sha256": hashlib.sha256(raw.encode()).hexdigest(), "bytes": len(raw)}


def record(session: Session, case_id: str, actor: str, event_type: str,
           payload: dict | None = None, run_id: str | None = None) -> AuditEvent:
    payload = _shrink(payload or {})
    last = session.exec(
        select(AuditEvent).where(AuditEvent.case_id == case_id).order_by(AuditEvent.seq.desc())  # type: ignore[attr-defined]
    ).first()
    seq = (last.seq + 1) if last else 1
    prev_hash = last.hash if last else GENESIS
    ts = utcnow()
    ev = AuditEvent(case_id=case_id, seq=seq, ts=ts, actor=actor, event_type=event_type, run_id=run_id,
                    payload=payload, prev_hash=prev_hash,
                    hash=compute_hash(prev_hash, seq, ts, actor, event_type, payload))
    session.add(ev)
    session.flush()
    return ev


def verify_chain(session: Session, case_id: str) -> dict:
    events = session.exec(
        select(AuditEvent).where(AuditEvent.case_id == case_id).order_by(AuditEvent.seq)  # type: ignore[arg-type]
    ).all()
    prev = GENESIS
    for ev in events:
        expected = compute_hash(prev, ev.seq, ev.ts, ev.actor, ev.event_type, ev.payload)
        if ev.prev_hash != prev or ev.hash != expected:
            return {"ok": False, "events": len(events), "broken_at_seq": ev.seq}
        prev = ev.hash
    return {"ok": True, "events": len(events), "head": prev}
