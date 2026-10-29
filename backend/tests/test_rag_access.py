"""Policy RAG at enterprise scale (w3-a): access trimming, retrieval quality gate, and the Azure AI Search store
(exercised with fake clients - no Azure needed)."""
from types import SimpleNamespace

import pytest

from app import identity
from app.config import get_settings
from app.rag import store as rag
from tests.conftest import golden


def _ids(cits):
    return {c.policy_id for c in cits}


def test_policy_access_is_declared_in_the_document():
    chunks = rag.chunks_on_disk()
    restricted = [c for c in chunks if c.policy_id == "KYC-POL-006"]
    assert restricted and all(c.allowed_roles == ["Admin", "BsaOfficer"] for c in restricted)
    assert all(c.allowed_roles == rag.ALL_READER_ROLES for c in chunks if c.policy_id != "KYC-POL-006")


def test_local_store_trims_to_the_callers_roles():
    store = rag.get_policy_store()
    q = "BSA officer investigation steps blocked property report OFAC 10 business days"
    assert "KYC-POL-006" not in _ids(store.search(q, top_k=10, roles=["KycAnalyst"]))
    assert "KYC-POL-006" in _ids(store.search(q, top_k=10, roles=["BsaOfficer"]))


def test_agent_inherits_the_access_of_the_person_it_acts_for():
    assert rag.caller_roles() == ["KycAnalyst"]  # no principal (script/job): analyst baseline
    with identity.acting_for({"subject": "bsa1", "kind": "user", "roles": ["BsaOfficer"]}):
        assert rag.caller_roles() == ["BsaOfficer"]
    with identity.acting_for(identity.system_ref("seed")):
        assert rag.caller_roles() == ["KycAnalyst"]


def test_policy_search_api_is_trimmed(client):
    q = {"q": "how the BSA officer investigates a sanctions alert", "k": 10}
    analyst = client.get("/policies/search", params=q, headers={"X-Role": "KycAnalyst"}).json()
    bsa = client.get("/policies/search", params=q, headers={"X-Role": "BsaOfficer"}).json()
    assert "KYC-POL-006" not in {r["policy_id"] for r in analyst}
    assert "KYC-POL-006" in {r["policy_id"] for r in bsa}


def test_risk_agent_never_cites_a_policy_the_analyst_cannot_read(client):
    case_id = client.post("/cases", json=golden("G04")).json()["id"]  # sanctions case: playbook is the closest text
    run = client.post(f"/cases/{case_id}/run", json={"sync": True}, headers={"X-Role": "KycAnalyst"}).json()["run"]
    assert "KYC-POL-006" not in {c["policy_id"] for c in run["risk"]["citations"]}


def test_retrieval_quality_gate():
    from eval.retrieval_eval import evaluate
    with identity.acting_as("risk"):
        report = evaluate(rag.get_policy_store())
    s = report["summary"]
    assert s["access_leaks"] == 0 and s["passed"], s


# ------------------------------------------------------------------------------------------- Azure AI Search store
class FakeSearchClient:
    def __init__(self):
        self.docs, self.calls, self.writes_as = {}, [], []
        self.fail_semantic = False

    def get_document(self, key, selected_fields=None):
        if key not in self.docs:
            raise KeyError(key)
        return self.docs[key]

    def merge_or_upload_documents(self, docs):
        self.writes_as.append(identity.current_role())
        self.docs.update({d["id"]: d for d in docs})

    def delete_documents(self, docs):
        self.writes_as.append(identity.current_role())
        for d in docs:
            self.docs.pop(d["id"], None)

    def search(self, **kw):
        self.calls.append(kw)
        if kw.get("query_type") == "semantic" and self.fail_semantic:
            raise RuntimeError("semantic ranker not enabled")
        if kw.get("search_text") == "*":  # stale-document sweep
            v = kw["filter"].split("'")[1]
            return [{"id": i} for i, d in self.docs.items() if d.get("corpus_version") != v]
        d = next(iter(self.docs.values()))
        return [{**d, "@search.score": 0.03, "@search.reranker_score": 2.5}]


class FakeIndexClient:
    def __init__(self):
        self.created_as = []

    def create_or_update_index(self, idx):
        self.created_as.append((identity.current_role(), {f.name for f in idx.fields}))


@pytest.fixture
def azure_store():
    sc, ic = FakeSearchClient(), FakeIndexClient()
    sc.docs["stale"] = {"id": "stale", "corpus_version": "old"}
    return rag.AzureSearchPolicyStore("https://srch.example.search.windows.net", "onboardiq-policies",
                                      search_client=sc, index_client=ic), sc, ic


def test_azure_store_builds_with_api_identity_and_removes_stale_chunks(azure_store):
    store, sc, ic = azure_store
    with identity.acting_as("risk"):
        store.ensure()
    role, fields = ic.created_as[0]
    assert role == "api" and {"allowed_roles", "corpus_version", "vector"} <= fields
    assert set(sc.writes_as) == {"api"}  # the risk agent reads the index; only the platform identity writes it
    assert "stale" not in sc.docs and len(sc.docs) == len(rag.chunks_on_disk())
    ic.created_as.clear()
    store._ready = False
    with identity.acting_as("risk"):
        store.ensure()  # corpus unchanged: no rebuild
    assert ic.created_as == []


def test_shared_service_mode_never_creates_or_changes_the_index(azure_store):
    store, sc, ic = azure_store
    store.manage_index = False
    with identity.acting_as("risk"):
        store.ensure()
    assert ic.created_as == []  # provisioned by deploy.sh; the app holds index-scoped data roles only
    assert len(sc.docs) == len(rag.chunks_on_disk()) and set(sc.writes_as) == {"api"}


def test_index_definition_matches_the_provisioning_file():
    """deploy.sh creates the index from infra/search-index.json; it must match what the app writes and queries."""
    import json
    from pathlib import Path
    spec = json.loads((Path(__file__).resolve().parents[2] / "infra" / "search-index.json").read_text())
    idx = rag.AzureSearchPolicyStore("https://x.search.windows.net", "onboardiq-policies", search_client=FakeSearchClient(),
                                     index_client=FakeIndexClient())._index_definition(1536)
    assert spec["name"] == idx.name
    assert {f["name"]: f["type"] for f in spec["fields"]} == {f.name: getattr(f.type, "value", f.type) for f in idx.fields}
    assert {f["name"] for f in spec["fields"] if f.get("filterable")} == {f.name for f in idx.fields if f.filterable}
    assert next(f for f in spec["fields"] if f["name"] == "vector")["dimensions"] == 1536
    assert spec["semantic"]["configurations"][0]["name"] == rag.AzureSearchPolicyStore.SEMANTIC_CONFIG
    chunk_fields = set(rag.Chunk.__dataclass_fields__) | {"corpus_version", "vector"}
    assert chunk_fields == {f["name"] for f in spec["fields"]}  # every uploaded field exists in the index


def test_azure_query_is_hybrid_and_security_filtered(azure_store):
    store, sc, _ = azure_store
    with identity.acting_as("risk"):
        store.ensure()
        cits = store.search("confirmed sanctions match", top_k=5, roles=["KycAnalyst"])
    call = sc.calls[-1]
    assert call["search_text"] == "confirmed sanctions match" and call["vector_queries"]  # keyword + vector
    assert call["filter"] == "allowed_roles/any(r: search.in(r, 'KycAnalyst', ','))"
    assert cits and cits[0].score == 0.03


def test_role_filter_cannot_be_injected():
    f = rag.AzureSearchPolicyStore.role_filter(["KycAnalyst", "x') or true or ('", "Admin"])
    assert f == "allowed_roles/any(r: search.in(r, 'KycAnalyst,Admin', ','))"


def test_semantic_ranking_falls_back_to_hybrid(azure_store):
    store, sc, _ = azure_store
    store.semantic = True
    with identity.acting_as("risk"):
        store.ensure()
        assert store.search("q", roles=["KycAnalyst"])[0].score == 2.5  # re-ranker score when available
        sc.fail_semantic = True
        assert store.search("q", roles=["KycAnalyst"])[0].score == 0.03  # plain hybrid when not
    assert "query_type" not in sc.calls[-1]


def test_store_selection_uses_search_when_configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(rag, "AzureSearchPolicyStore", lambda *a, **k: SimpleNamespace(name="azure-ai-search"))
    s.search_endpoint = "https://srch.example.search.windows.net"
    try:
        rag.reset_store()
        assert rag.get_policy_store().name == "azure-ai-search"
    finally:
        s.search_endpoint = None
        rag.reset_store()


def test_admin_rag_eval_endpoint(client):
    assert client.get("/admin/rag/eval", headers={"X-Role": "KycAnalyst"}).status_code == 403
    r = client.get("/admin/rag/eval", headers={"X-Role": "Admin"}).json()
    assert r["summary"]["store"] == "local-json" and r["summary"]["access_leaks"] == 0
