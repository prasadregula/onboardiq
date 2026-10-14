"""Passwordless Azure SQL: the Entra token is injected and conflicting ODBC keywords are stripped."""
import struct
import sys
import types

import pytest


def test_entra_token_injected(monkeypatch):
    if "pyodbc" not in sys.modules:  # dialect import needs a pyodbc module; a stub is enough (no real connection)
        monkeypatch.setitem(sys.modules, "pyodbc", types.SimpleNamespace(version="5.3.0", paramstyle="qmark",
                                                                        Error=Exception, SQL_DRIVER_NAME=6,
                                                                        Cursor=type("Cursor", (), {"nextset": None})))
    from sqlalchemy import create_engine

    from app import db

    class FakeCred:
        def get_token(self, scope):
            assert scope == "https://database.windows.net/.default"
            return types.SimpleNamespace(token="tok123")

    import azure.identity as ai
    monkeypatch.setattr(ai, "DefaultAzureCredential", lambda **k: FakeCred())
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    eng = create_engine("mssql+pyodbc://@srv.database.windows.net/onboardiq?driver=ODBC+Driver+18+for+SQL+Server"
                        "&Authentication=ActiveDirectoryMsi&Encrypt=yes")
    db._use_entra_token(eng)
    captured = {}

    def fake_connect(*cargs, **cparams):
        captured.update(cargs=cargs, cparams=cparams)
        raise RuntimeError("stop")
    monkeypatch.setattr(eng.dialect, "connect", fake_connect)
    with pytest.raises(Exception):
        eng.connect()
    conn_str = captured["cargs"][0]
    assert "Trusted_Connection" not in conn_str and "Authentication" not in conn_str
    assert "Server=srv.database.windows.net" in conn_str and "Encrypt=yes" in conn_str
    raw = "tok123".encode("utf-16-le")
    assert captured["cparams"]["attrs_before"][1256] == struct.pack(f"<I{len(raw)}s", len(raw), raw)
