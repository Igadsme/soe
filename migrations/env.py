from logging.config import fileConfig
import os
import sys
from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config
root = os.path.dirname(os.path.abspath(config.config_file_name or "alembic.ini"))
for candidate in (os.path.join(root, "backend"), root):
    if os.path.isdir(os.path.join(candidate, "app")):
        sys.path.insert(0, candidate)
        break
from app.models import Base  # noqa: E402

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("SOE_DATABASE_URL", config.get_main_option("sqlalchemy.url"))
if database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=database_url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
