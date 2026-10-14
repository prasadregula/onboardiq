"""Build the policy index.  python -m app.rag.ingest [--azure]"""
from __future__ import annotations

import sys

from ..config import get_settings
from ..llm import get_llm
from .store import LocalPolicyStore, chunk_markdown


def build_local() -> int:
    s = get_settings()
    store = LocalPolicyStore(s.rag_index_path)
    n = store.build(s.policies_dir)
    print(f"indexed {n} chunks -> {s.rag_index_path} (embedder={get_llm().name}, version={store.version})")
    return n


def build_azure() -> int:
    """Create/refresh the Azure AI Search index (requires ONBOARDIQ_SEARCH_ENDPOINT + Azure OpenAI)."""
    from azure.core.credentials import AzureKeyCredential
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )

    s = get_settings()
    cred = AzureKeyCredential(s.search_api_key) if s.search_api_key else DefaultAzureCredential()
    chunks = [c for p in sorted(s.policies_dir.glob("*.md")) for c in chunk_markdown(p)]
    vectors = get_llm().embed([c.text for c in chunks])
    dims = len(vectors[0])
    idx = SearchIndex(
        name=s.search_index,
        fields=[SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                SimpleField(name="policy_id", type=SearchFieldDataType.String, filterable=True),
                SearchableField(name="policy_title", type=SearchFieldDataType.String),
                SearchableField(name="section", type=SearchFieldDataType.String),
                SearchableField(name="text", type=SearchFieldDataType.String),
                SimpleField(name="source", type=SearchFieldDataType.String),
                SearchField(name="vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                            searchable=True, vector_search_dimensions=dims, vector_search_profile_name="hnsw")],
        vector_search=VectorSearch(algorithms=[HnswAlgorithmConfiguration(name="hnsw-algo")],
                                   profiles=[VectorSearchProfile(name="hnsw", algorithm_configuration_name="hnsw-algo")]),
    )
    SearchIndexClient(s.search_endpoint, cred).create_or_update_index(idx)
    docs = [{**c.__dict__, "vector": v} for c, v in zip(chunks, vectors)]
    SearchClient(s.search_endpoint, s.search_index, cred).upload_documents(docs)
    print(f"uploaded {len(docs)} chunks to Azure AI Search index {s.search_index}")
    return len(docs)


if __name__ == "__main__":
    build_azure() if "--azure" in sys.argv else build_local()
