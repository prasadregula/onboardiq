"""Policy RAG: chunk-by-heading ingestion, hybrid (vector + keyword) retrieval with citations.

Local store = a JSON file (dev, tests, eval). Azure AI Search adapter for cloud. Embeddings come from
the LLM wrapper (Azure text-embedding-3-small, or the deterministic hashing embedder in mock mode).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config import get_settings
from ..llm import get_llm

log = logging.getLogger("onboardiq.rag")


@dataclass
class Chunk:
    id: str
    policy_id: str  # e.g. KYC-POL-002
    policy_title: str
    section: str  # e.g. "3. Rating bands"
    text: str
    source: str  # filename


@dataclass
class Citation:
    chunk_id: str
    policy_id: str
    policy_title: str
    section: str
    score: float
    text: str

    def ref(self) -> str:
        return f"{self.policy_id} §{self.section.split('.')[0]} ({self.section.split('. ', 1)[-1]})"


def chunk_markdown(path: Path) -> list[Chunk]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"#\s+(\S+)\s+(.+)", text.strip().splitlines()[0])
    policy_id, title = (m.group(1), m.group(2).strip()) if m else (path.stem, path.stem)
    chunks: list[Chunk] = []
    for sec in re.split(r"\n(?=## )", text)[1:]:
        heading, _, body = sec.partition("\n")
        section = heading.lstrip("# ").strip()
        body = body.strip()
        cid = hashlib.sha1(f"{policy_id}|{section}".encode()).hexdigest()[:12]
        chunks.append(Chunk(cid, policy_id, title, section, f"{title} — {section}\n{body}", path.name))
    return chunks


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2]


class LocalPolicyStore:
    name = "local-json"

    def __init__(self, index_path: Path):
        self.index_path = index_path
        self.chunks: list[Chunk] = []
        self.vectors: list[list[float]] = []
        self.version = ""

    def build(self, policies_dir: Path) -> int:
        self.chunks = [c for p in sorted(policies_dir.glob("*.md")) for c in chunk_markdown(p)]
        self.vectors = get_llm().embed([c.text for c in self.chunks]) if self.chunks else []
        self.version = hashlib.sha1("".join(c.id + c.text for c in self.chunks).encode()).hexdigest()[:10]
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: several processes/replicas can build at once on a shared volume (Azure Files). Writing to a
        # unique temp file and renaming means a reader never sees a half-written index.
        tmp = self.index_path.with_name(f"{self.index_path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps({"version": self.version, "embedder": get_llm().name,
                                   "chunks": [asdict(c) for c in self.chunks], "vectors": self.vectors}))
        os.replace(tmp, self.index_path)
        return len(self.chunks)

    def load(self) -> bool:
        if not self.index_path.exists():
            return False
        try:
            data = json.loads(self.index_path.read_text())
            data["chunks"], data["vectors"], data["version"]
        except (ValueError, KeyError, OSError) as e:  # truncated/corrupt index (e.g. concurrent write): rebuild it
            log.warning("policy index %s unreadable (%s); rebuilding", self.index_path, type(e).__name__)
            return False
        if data.get("embedder") != get_llm().name:
            return False  # embedder changed (mock <-> azure): rebuild
        self.chunks = [Chunk(**c) for c in data["chunks"]]
        self.vectors = data["vectors"]
        self.version = data["version"]
        return True

    def ensure(self) -> None:
        if not self.chunks and not self.load():
            self.build(get_settings().policies_dir)

    def search(self, query: str, top_k: int = 5) -> list[Citation]:
        self.ensure()
        if not self.chunks:
            return []
        qv = get_llm().embed([query])[0]
        qtok = set(_tokens(query))
        scored = []
        for c, v in zip(self.chunks, self.vectors):
            cos = _dot(qv, v) / ((_norm(qv) * _norm(v)) or 1.0)
            ctok = _tokens(c.text)
            kw = len(qtok & set(ctok)) / (math.sqrt(len(qtok) or 1) * math.sqrt(len(set(ctok)) or 1))
            scored.append((0.6 * cos + 0.4 * kw, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [Citation(c.id, c.policy_id, c.policy_title, c.section, round(s, 4), c.text) for s, c in scored[:top_k]]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(sum(x * x for x in a))


class AzureSearchPolicyStore:
    """Azure AI Search (hybrid vector + BM25). Index schema: id, policy_id, policy_title, section, text, source,
    vector (Collection(Edm.Single), 1536 dims for text-embedding-3-small). NOTE: untested in this drop; the
    local store is the verified path. Create the index with `python -m app.rag.ingest --azure`."""
    name = "azure-ai-search"

    def __init__(self, endpoint: str, index: str, api_key: str | None):
        from azure.search.documents import SearchClient
        if api_key:
            from azure.core.credentials import AzureKeyCredential
            cred = AzureKeyCredential(api_key)
        else:
            from azure.identity import DefaultAzureCredential
            cred = DefaultAzureCredential()
        self.client = SearchClient(endpoint=endpoint, index_name=index, credential=cred)
        self.version = "azure"

    def ensure(self) -> None:
        pass

    def search(self, query: str, top_k: int = 5) -> list[Citation]:
        from azure.search.documents.models import VectorizedQuery
        qv = get_llm().embed([query])[0]
        vq = VectorizedQuery(vector=qv, k_nearest_neighbors=top_k, fields="vector")
        results = self.client.search(search_text=query, vector_queries=[vq], top=top_k,
                                     select=["id", "policy_id", "policy_title", "section", "text"])
        return [Citation(r["id"], r["policy_id"], r["policy_title"], r["section"], float(r["@search.score"]), r["text"])
                for r in results]


_store = None


def get_policy_store():
    global _store
    if _store is None:
        s = get_settings()
        if s.search_endpoint:
            _store = AzureSearchPolicyStore(s.search_endpoint, s.search_index, s.search_api_key)
        else:
            _store = LocalPolicyStore(s.rag_index_path)
    return _store


def reset_store() -> None:
    global _store
    _store = None
