"""Document storage: local filesystem (dev) or Azure Blob Storage (managed identity / connection string).
Documents are private; the API streams them back to authorised readers, never via public URLs."""
from __future__ import annotations

from pathlib import Path

from ..config import Settings, get_settings


class StorageProvider:
    name = "base"

    @property
    def svc(self):
        """Blob client for the CURRENT agent role (api uploads with read/write; extraction reads with Blob Data Reader)."""
        from azure.storage.blob import BlobServiceClient

        from ..identity import credential_for, current_role
        role = "api" if self._conn else current_role()
        if role not in self._clients:
            self._clients[role] = (BlobServiceClient.from_connection_string(self._conn) if self._conn
                                   else BlobServiceClient(account_url=self._url, credential=credential_for(role)))
        return self._clients[role]

    def put(self, key: str, data: bytes, content_type: str | None) -> str:
        raise NotImplementedError

    def get(self, uri: str) -> bytes:
        raise NotImplementedError

    def exists(self, uri: str) -> bool:
        raise NotImplementedError


class LocalStorage(StorageProvider):
    name = "local"

    def __init__(self, root: Path):
        self.root = root

    @property
    def svc(self):
        """Blob client for the CURRENT agent role (api uploads with read/write; extraction reads with Blob Data Reader)."""
        from azure.storage.blob import BlobServiceClient

        from ..identity import credential_for, current_role
        role = "api" if self._conn else current_role()
        if role not in self._clients:
            self._clients[role] = (BlobServiceClient.from_connection_string(self._conn) if self._conn
                                   else BlobServiceClient(account_url=self._url, credential=credential_for(role)))
        return self._clients[role]

    def put(self, key: str, data: bytes, content_type: str | None) -> str:
        from ..agent.policy_point import require_write
        require_write("storage")  # separation of duties: agents read documents, only the API writes them
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def get(self, uri: str) -> bytes:
        return Path(uri).read_bytes()

    def exists(self, uri: str) -> bool:
        return Path(uri).exists()


class AzureBlobStorage(StorageProvider):
    """URIs are 'azblob://<container>/<key>'. Auth: connection string if given, else DefaultAzureCredential
    (Container App managed identity needs 'Storage Blob Data Contributor')."""
    name = "azure-blob"

    def __init__(self, s: Settings):
        from azure.storage.blob import BlobServiceClient
        self._conn = s.storage_connection_string
        self._url = s.storage_account_url
        self._clients: dict[str, BlobServiceClient] = {}
        self.container = s.storage_container
        try:
            self.svc.create_container(self.container)
        except Exception:  # noqa: BLE001 - exists or no permission to create; uploads will surface real errors
            pass

    @property
    def svc(self):
        """Blob client for the CURRENT agent role (api uploads with read/write; extraction reads with Blob Data Reader)."""
        from azure.storage.blob import BlobServiceClient

        from ..identity import credential_for, current_role
        role = "api" if self._conn else current_role()
        if role not in self._clients:
            self._clients[role] = (BlobServiceClient.from_connection_string(self._conn) if self._conn
                                   else BlobServiceClient(account_url=self._url, credential=credential_for(role)))
        return self._clients[role]

    def put(self, key: str, data: bytes, content_type: str | None) -> str:
        from ..agent.policy_point import require_write
        require_write("storage")  # separation of duties: agents read documents, only the API writes them
        from azure.storage.blob import ContentSettings
        self.svc.get_blob_client(self.container, key).upload_blob(
            data, overwrite=True, content_settings=ContentSettings(content_type=content_type or "application/octet-stream"))
        return f"azblob://{self.container}/{key}"

    def _split(self, uri: str) -> tuple[str, str]:
        container, _, key = uri.removeprefix("azblob://").partition("/")
        return container, key

    def get(self, uri: str) -> bytes:
        c, k = self._split(uri)
        return self.svc.get_blob_client(c, k).download_blob().readall()

    def exists(self, uri: str) -> bool:
        c, k = self._split(uri)
        return self.svc.get_blob_client(c, k).exists()


_storage: StorageProvider | None = None


def get_storage() -> StorageProvider:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_provider == "azure" and (s.storage_account_url or s.storage_connection_string):
            _storage = AzureBlobStorage(s)
        else:
            _storage = LocalStorage(Path(s.blob_dir))
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
