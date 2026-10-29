"""Policy RAG: chunk-by-heading ingestion, hybrid (vector + keyword) retrieval with citations and access trimming.

Two stores with the same behaviour:
- LocalPolicyStore: a JSON file (dev, tests, CI eval).
- AzureSearchPolicyStore: Azure AI Search - hybrid BM25 + vector (HNSW) fused by the service, optional semantic
  re-ranking, and a security filter on every query.

Access trimming (security trimming): each policy declares who may read it with a line under its title,
`<!-- access: BsaOfficer, Admin -->`; without it every reader role may. A query returns only chunks the PERSON the
agent is acting for may read: the agent never retrieves more than the analyst could open themselves. Scripts and jobs
(system principals) get the KycAnalyst baseline.

Embeddings come from the LLM wrapper (Azure text-embedding-3-small, or the deterministic hashing embedder in mock mode).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import get_settings
from ..llm import get_llm

log = logging.getLogger("onboardiq.rag")

ALL_READER_ROLES = ["Admin", "Auditor", "BsaOfficer", "KycAnalyst", "Service"]
SYSTEM_BASELINE_ROLES = ["KycAnalyst"]
_ACCESS_RE = re.compile(r"<!--\s*access:\s*([^>]*?)\s*-->", re.I)


@dataclass
class Chunk:
    id: str
    policy_id: str  # e.g. KYC-POL-002
    policy_title: str
    section: str  # e.g. "3. Rating bands"
    text: str
    source: str  # filename
    allowed_roles: list[str] = field(default_factory=lambda: list(ALL_READER_ROLES))


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
    acc = _ACCESS_RE.search(text.split("\n## ", 1)[0])
    allowed = sorted({r.strip() for r in acc.group(1).split(",") if r.strip()}) if acc else list(ALL_READER_ROLES)
    chunks: list[Chunk] = []
    for sec in re.split(r"\n(?=## )", text)[1:]:
        heading, _, body = sec.partition("\n")
        section = heading.lstrip("# ").strip()
        body = body.strip()
        cid = hashlib.sha1(f"{policy_id}|{section}".encode()).hexdigest()[:12]
        chunks.append(Chunk(cid, policy_id, title, section, f"{title} — {section}\n{body}", path.name, allowed))
    return chunks


def caller_roles(roles: list[str] | None = None) -> list[str]:
    """Roles retrieval is trimmed to: explicit roles (API caller), else the principal the agent acts for."""
    if roles is not None:
        return sorted(set(roles))
    from ..identity import on_behalf_of
    p = on_behalf_of() or {}
    return sorted(set(p.get("roles") or [])) or list(SYSTEM_BASELINE_ROLES)


def corpus_version(chunks: list[Chunk]) -> str:
    return hashlib.sha1("".join(c.id + c.text + ",".join(c.allowed_roles) for c in chunks).encode()).hexdigest()[:10]


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
        self.version = corpus_version(self.chunks)
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
        if self.version != corpus_version(chunks_on_disk()):
            return False  # policies changed (new policy, edited text or access): rebuild
        return True

    def ensure(self) -> None:
        if not self.chunks and not self.load():
            self.build(get_settings().policies_dir)

    def search(self, query: str, top_k: int = 5, roles: list[str] | None = None) -> list[Citation]:
        self.ensure()
        allowed = set(caller_roles(roles))
        visible = [(c, v) for c, v in zip(self.chunks, self.vectors) if allowed & set(c.allowed_roles)]
        if not visible:
            return []
        qv = get_llm().embed([query])[0]
        qtok = set(_tokens(query))
        scored = []
        for c, v in visible:
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


def chunks_on_disk() -> list[Chunk]:
    return [c for p in sorted(get_settings().policies_dir.glob("*.md")) for c in chunk_markdown(p)]


class _AgentCredential:
    """azure-core TokenCredential that resolves to the CURRENT agent identity at call time (see identity.py), so
    queries run as the risk agent and index writes as the API's own identity, through one client."""

    def get_token(self, *scopes, **kwargs):
        from ..identity import current_credential
        return current_credential().get_token(*scopes, **kwargs)


class AzureSearchPolicyStore:
    """Azure AI Search: hybrid BM25 + vector query with a security filter, optional semantic re-ranking.

    Keyless: the risk agent's identity reads (Search Index Data Reader); the API's own identity uploads chunks (Search
    Index Data Contributor). Every chunk carries `allowed_roles` (filterable) and `corpus_version`; documents are
    re-uploaded only when the policy corpus changes.

    manage_index=False (a SHARED search service, e.g. one free tier used by several apps): the index is provisioned
    once by deploy.sh and both identities hold roles scoped to this one index, so the app can fill and query its own
    index but cannot create, change or even read any other index on the service."""
    name = "azure-ai-search"
    SEMANTIC_CONFIG = "policies"

    def __init__(self, endpoint: str, index: str, api_key: str | None = None, semantic: bool = False,
                 search_client=None, index_client=None, manage_index: bool = True):
        from azure.search.documents import SearchClient
        from azure.search.documents.indexes import SearchIndexClient
        if api_key:
            from azure.core.credentials import AzureKeyCredential
            cred = AzureKeyCredential(api_key)
        else:
            cred = _AgentCredential()
        self.endpoint, self.index_name, self.semantic, self.manage_index = endpoint, index, semantic, manage_index
        self.client = search_client or SearchClient(endpoint=endpoint, index_name=index, credential=cred)
        self.index_client = index_client or SearchIndexClient(endpoint, cred)
        self.version = ""
        self._ready = False

    # ----------------------------------------------------------------------------------------------------- ingest
    def _index_definition(self, dims: int):
        from azure.search.documents.indexes.models import (
            HnswAlgorithmConfiguration,
            SearchableField,
            SearchField,
            SearchFieldDataType,
            SearchIndex,
            SemanticConfiguration,
            SemanticField,
            SemanticPrioritizedFields,
            SemanticSearch,
            SimpleField,
            VectorSearch,
            VectorSearchProfile,
        )
        return SearchIndex(
            name=self.index_name,
            fields=[SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                    SimpleField(name="policy_id", type=SearchFieldDataType.String, filterable=True),
                    SearchableField(name="policy_title", type=SearchFieldDataType.String),
                    SearchableField(name="section", type=SearchFieldDataType.String),
                    SearchableField(name="text", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
                    SimpleField(name="source", type=SearchFieldDataType.String),
                    SimpleField(name="allowed_roles", type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                                filterable=True),
                    SimpleField(name="corpus_version", type=SearchFieldDataType.String, filterable=True),
                    SearchField(name="vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                                searchable=True, vector_search_dimensions=dims, vector_search_profile_name="hnsw")],
            vector_search=VectorSearch(algorithms=[HnswAlgorithmConfiguration(name="hnsw-algo")],
                                       profiles=[VectorSearchProfile(name="hnsw", algorithm_configuration_name="hnsw-algo")]),
            semantic_search=SemanticSearch(configurations=[SemanticConfiguration(
                name=self.SEMANTIC_CONFIG, prioritized_fields=SemanticPrioritizedFields(
                    title_field=SemanticField(field_name="section"), content_fields=[SemanticField(field_name="text")]))]),
        )

    def _indexed_version(self, chunks: list[Chunk]) -> str | None:
        try:
            return self.client.get_document(key=chunks[0].id, selected_fields=["corpus_version"]).get("corpus_version")
        except Exception:  # noqa: BLE001 - index or document missing
            return None

    def build(self, policies_dir: Path | None = None) -> int:
        """(Re)create the index and upload every chunk. Embeddings run under the current (risk) identity; the writes
        run under the API's own identity - the risk agent can read the index but never change it."""
        from ..identity import acting_as
        chunks = chunks_on_disk()
        version = corpus_version(chunks)
        vectors = get_llm().embed([c.text for c in chunks])
        with acting_as("api"):
            if self.manage_index:
                self.index_client.create_or_update_index(self._index_definition(len(vectors[0])))
            docs = [{**asdict(c), "corpus_version": version, "vector": v} for c, v in zip(chunks, vectors)]
            self.client.merge_or_upload_documents(docs)
            stale = [d["id"] for d in self.client.search(search_text="*", filter=f"corpus_version ne '{version}'",
                                                         select=["id"], top=1000)]
            if stale:
                self.client.delete_documents([{"id": i} for i in stale])
        self.version, self._ready = version, True
        log.info("policy index %s: %d chunks uploaded, %d stale removed (version %s)", self.index_name, len(docs),
                 len(stale), version)
        return len(docs)

    def ensure(self) -> None:
        if self._ready:
            return
        chunks = chunks_on_disk()
        version = corpus_version(chunks)
        from ..identity import acting_as
        with acting_as("api"):
            current = self._indexed_version(chunks)
        if current == version:
            self.version, self._ready = version, True
        else:
            self.build()

    # ------------------------------------------------------------------------------------------------------ query
    @staticmethod
    def role_filter(roles: list[str]) -> str:
        safe = [r for r in roles if re.fullmatch(r"[A-Za-z]+", r)]  # role names only: nothing to inject into OData
        return f"allowed_roles/any(r: search.in(r, '{','.join(safe)}', ','))"

    def search(self, query: str, top_k: int = 5, roles: list[str] | None = None) -> list[Citation]:
        from azure.search.documents.models import VectorizedQuery
        self.ensure()
        flt = self.role_filter(caller_roles(roles))
        qv = get_llm().embed([query])[0]
        vq = VectorizedQuery(vector=qv, k_nearest_neighbors=max(top_k, 10), fields="vector")
        select = ["id", "policy_id", "policy_title", "section", "text"]
        kwargs = dict(search_text=query, vector_queries=[vq], filter=flt, top=top_k, select=select)
        if self.semantic:
            try:
                rows = list(self.client.search(**kwargs, query_type="semantic",
                                               semantic_configuration_name=self.SEMANTIC_CONFIG))
                return [self._cite(r, r.get("@search.reranker_score")) for r in rows]
            except Exception as e:  # noqa: BLE001 - semantic ranker unavailable or over quota: plain hybrid
                log.warning("semantic ranking unavailable (%s); using hybrid ranking", type(e).__name__)
        return [self._cite(r) for r in self.client.search(**kwargs)]

    @staticmethod
    def _cite(r, score=None) -> Citation:
        return Citation(r["id"], r["policy_id"], r["policy_title"], r["section"],
                        round(float(score if score is not None else r["@search.score"]), 4), r["text"])


_store = None


def get_policy_store():
    global _store
    if _store is None:
        s = get_settings()
        if s.search_endpoint:
            _store = AzureSearchPolicyStore(s.search_endpoint, s.search_index, s.search_api_key, s.search_semantic,
                                            manage_index=s.search_manage_index)
        else:
            _store = LocalPolicyStore(s.rag_index_path)
    return _store


def reset_store() -> None:
    global _store
    _store = None
