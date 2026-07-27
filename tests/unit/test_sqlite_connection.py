from __future__ import annotations

import ast
import gc
import os
import sqlite3
from pathlib import Path

import pytest

from db.base import BaseRepository
from sqlite_connection import connect


def _scalar(database: str, query: str) -> object:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(query).fetchone()[0]
    finally:
        connection.close()


def _sqlite_fd_count(database: str) -> int:
    count = 0
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if database in target:
            count += 1
    return count


def test_direct_factory_commits_and_closes_after_success(tmp_path: Path) -> None:
    database = str(tmp_path / "success.db")
    connection = connect(database)

    with connection as active_connection:
        assert active_connection is connection
        active_connection.execute("CREATE TABLE entries (value TEXT NOT NULL)")
        active_connection.execute("INSERT INTO entries(value) VALUES ('committed')")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    assert _scalar(database, "SELECT COUNT(*) FROM entries") == 1


def test_direct_factory_rolls_back_and_closes_after_exception(tmp_path: Path) -> None:
    database = str(tmp_path / "rollback.db")
    setup_connection = sqlite3.connect(database)
    try:
        setup_connection.execute("CREATE TABLE entries (value TEXT NOT NULL)")
        setup_connection.commit()
    finally:
        setup_connection.close()

    connection = connect(database)
    with pytest.raises(RuntimeError, match="abort transaction"):
        with connection:
            connection.execute("INSERT INTO entries(value) VALUES ('rolled back')")
            raise RuntimeError("abort transaction")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    assert _scalar(database, "SELECT COUNT(*) FROM entries") == 0


def test_base_repository_connection_closes_after_context(tmp_path: Path) -> None:
    repository = BaseRepository(str(tmp_path / "repository.db"))
    connection = repository._connect()

    with connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux /proc FD regression test")
def test_repository_setup_exception_closes_sqlite_fd(tmp_path: Path) -> None:
    database = tmp_path / "corrupt.db"
    database.write_bytes(b"not a sqlite database")
    repository = BaseRepository(str(database))

    gc.collect()
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = _sqlite_fd_count(str(database))
        with pytest.raises(sqlite3.DatabaseError):
            repository._connect()
        after = _sqlite_fd_count(str(database))
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()

    assert after == before


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="Linux /proc FD regression test")
def test_repeated_repository_contexts_do_not_grow_sqlite_fds(tmp_path: Path) -> None:
    database = str(tmp_path / "fd-soak.db")
    repository = BaseRepository(database)
    with repository._connect() as connection:
        connection.execute("CREATE TABLE entries (value TEXT NOT NULL)")

    gc.collect()
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = _sqlite_fd_count(database)
        for _ in range(200):
            with repository._connect() as connection:
                connection.execute("SELECT COUNT(*) FROM entries").fetchone()
        after = _sqlite_fd_count(database)
    finally:
        if gc_was_enabled:
            gc.enable()

    assert after == before


def test_production_contexts_do_not_use_raw_sqlite_connect() -> None:
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    paths = list(root.glob("*.py"))

    for directory in ("db", "routes", "services"):
        paths.extend((root / directory).rglob("*.py"))

    for path in sorted(paths):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "sqlite3"
                    and call.func.attr == "connect"
                ):
                    offenders.append(f"{path.relative_to(root)}:{call.lineno}")

    assert offenders == [], "raw sqlite3.connect context managers leak connections:\n" + "\n".join(offenders)
