"""Database engine. SQLite for dev/tests (schema via create_all); Azure SQL / Postgres via Alembic migrations.

Azure SQL examples (ONBOARDIQ_DATABASE_URL):
  passwordless:      mssql+pyodbc://@sql-onboardiq-dev.database.windows.net/onboardiq?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes
                     (no user in the URL -> Entra token from managed identity / az login, see _use_entra_token)
  sql login:         mssql+pyodbc://user:pass@sql-onboardiq-dev.database.windows.net/onboardiq?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes
Requires the pyodbc package and msodbcsql18 in the container image (see Dockerfile).
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from .config import BASE_DIR, get_settings

log = logging.getLogger("onboardiq.db")
_engine = None


def get_engine():
    global _engine
    if _engine is None:
        s = get_settings()
        kwargs: dict = {"json_serializer": lambda o: json.dumps(o, default=str), "pool_pre_ping": True}
        if s.database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)
            if s.database_url.startswith("mssql"):
                kwargs["fast_executemany"] = True
        _engine = create_engine(s.database_url, **kwargs)
        if s.database_url.startswith("mssql") and "://@" in s.database_url:
            _use_entra_token(_engine)  # no SQL username/password in the URL -> passwordless (managed identity / az login)
        if s.database_url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
                dbapi_conn.execute("PRAGMA journal_mode=WAL")
    return _engine


_SQL_TOKEN_ATTR = 1256  # SQL_COPT_SS_ACCESS_TOKEN
_STRIP_KEYS = re.compile(r";\s*(Trusted_Connection|Authentication|UID|PWD)=[^;]*", re.IGNORECASE)


def _use_entra_token(engine) -> None:
    """Passwordless Azure SQL: get an Entra token with azure-identity and hand it to the ODBC driver.

    Microsoft's documented SQLAlchemy pattern. More reliable than the driver's own Authentication=ActiveDirectoryMsi:
    SQLAlchemy adds Trusted_Connection=Yes to a URL with no username, which the driver rejects ("Cannot use
    Authentication option with Integrated Security option"), and the driver's MSI support varies by hosting platform.
    In the Container App this uses the app's system-assigned identity; locally it uses `az login`."""
    import struct

    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    cred = ManagedIdentityCredential() if os.getenv("IDENTITY_ENDPOINT") else DefaultAzureCredential(
        exclude_interactive_browser_credential=True)

    @event.listens_for(engine, "do_connect")
    def _inject_token(dialect, conn_rec, cargs, cparams):
        cargs[0] = _STRIP_KEYS.sub("", cargs[0])
        raw = cred.get_token("https://database.windows.net/.default").token.encode("utf-16-le")
        cparams["attrs_before"] = {_SQL_TOKEN_ATTR: struct.pack(f"<I{len(raw)}s", len(raw), raw)}


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)
    s = get_settings()
    if s.database_url.startswith("sqlite"):
        SQLModel.metadata.create_all(get_engine())
        _sqlite_add_missing_columns()
    else:
        run_migrations()


def _sqlite_add_missing_columns() -> None:
    """Dev convenience: create_all() creates missing tables but never alters existing ones, so a SQLite dev DB made
    by an older drop lacks new columns. Add any missing nullable/defaulted column in place (ALTER TABLE ADD COLUMN).
    Azure SQL / Postgres use the Alembic migrations instead."""
    from sqlalchemy import inspect, text
    engine = get_engine()
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                clause = ""
                if default is not None:
                    clause = f" NOT NULL DEFAULT {default!r}" if not isinstance(default, bool) else f" NOT NULL DEFAULT {int(default)}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{clause}'))
                log.info("added column %s.%s to existing SQLite database", table.name, col.name)


def run_migrations() -> None:
    """alembic upgrade head, in-process (Container App startup).

    Several workers/replicas can start at once. Alembic does NOT serialise concurrent upgrades, so on SQL Server we
    take an exclusive application lock (sp_getapplock) first: one process migrates, the others wait and then find the
    schema already at head. Without it, two processes race to CREATE TABLE, the loser fails startup, uvicorn kills the
    winner too (rolling back its transaction) and the container crash-loops forever."""
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    cfg.attributes["skip_logging_config"] = True  # keep the app's JSON logging (alembic.ini would replace it)
    engine = get_engine()
    if engine.dialect.name != "mssql":
        log.info("running database migrations")
        command.upgrade(cfg, "head")
        return
    with engine.connect() as lock_conn:
        log.info("waiting for migration lock")
        rc = lock_conn.exec_driver_sql(
            "DECLARE @r int; EXEC @r = sp_getapplock @Resource = 'onboardiq-migrations', @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = 300000; SELECT @r").scalar()
        if rc is None or rc < 0:
            raise RuntimeError(f"could not acquire migration lock (sp_getapplock returned {rc})")
        try:
            log.info("running database migrations")
            command.upgrade(cfg, "head")
            log.info("database migrations complete")
        finally:
            lock_conn.exec_driver_sql("EXEC sp_releaseapplock @Resource = 'onboardiq-migrations', @LockOwner = 'Session'")


def reset_engine_for_tests(url: str) -> None:
    """Point the engine at a fresh database (tests / eval)."""
    global _engine
    get_settings().database_url = url
    _engine = None
    init_db()


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
