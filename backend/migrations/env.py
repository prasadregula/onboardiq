"""Alembic environment - uses ONBOARDIQ_DATABASE_URL and the SQLModel metadata."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel

from app import models  # noqa: F401  (registers tables)
from app.config import get_settings

config = context.config
# Only configure logging from alembic.ini when run from the CLI. In-process (app startup) fileConfig() would replace
# the app's handlers and disable existing loggers - hiding the very traceback you need when a migration fails.
if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"}, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Use the app's engine so migrations get the same connection setup (incl. the passwordless Entra token for
    # Azure SQL) as the running app.
    from app.db import get_engine
    with get_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
