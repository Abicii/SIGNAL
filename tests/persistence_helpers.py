from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, insert, inspect, text

from persistence import SignalRepository
from persistence.models import Base


def prepare_repository(repository: SignalRepository, *, reset: bool = False) -> None:
    if os.environ.get("SIGNAL_TEST_DATABASE_URL"):
        url = os.environ["SIGNAL_TEST_DATABASE_URL"]
        if "test" not in url.rsplit("/", 1)[-1].lower():
            raise RuntimeError("SIGNAL_TEST_DATABASE_URL must name a dedicated database containing 'test'")
        from alembic import command
        from alembic.config import Config

        previous_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        try:
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            command.upgrade(config, "head")
        finally:
            if previous_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous_url
        if reset:
            with repository.engine.begin() as connection:
                connection.execute(text("TRUNCATE TABLE plan_changes, planned_sessions, profiles, decision_runs, plan_versions, users CASCADE"))
    else:
        Base.metadata.create_all(repository.engine)
        if "alembic_version" not in set(inspect(repository.engine).get_table_names()):
            version_table = Table(
                "alembic_version", MetaData(),
                Column("version_num", String(32), primary_key=True),
            )
            version_table.create(repository.engine)
            with repository.engine.begin() as connection:
                connection.execute(insert(version_table).values(version_num="0001_initial_persistence"))
    repository.initialize()
