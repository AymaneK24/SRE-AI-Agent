"""Storage layer. The same queries run on SQLite (one machine) or PostgreSQL (production)."""
from __future__ import annotations

import asyncio
import decimal
import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import schema
from .validation import name_from_email, sanitize_stored_name

log = logging.getLogger("rcc.db")
PLATFORM_ORG = 0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_after(**delta: Any) -> str:
    moment = datetime.now(timezone.utc) + timedelta(**delta)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _audit_hash(prev: str, org_id: int, ts: str, actor: str, action: str, target: str, detail: str) -> str:
    return sha256_text("\x1f".join([prev, str(org_id), ts, actor, action, target, detail]))


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    return {key: float(value) if isinstance(value, decimal.Decimal) else value for key, value in row.items()}


class Database:
    def __init__(self, path: Path | None = None, url: str = "") -> None:
        self.url = url
        self.is_postgres = url.startswith(("postgres://", "postgresql://"))
        if url and not self.is_postgres:
            raise ValueError("RCC_DATABASE_URL must be a PostgreSQL address.")
        self._lock = threading.RLock()
        if self.is_postgres:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(url, min_size=1, max_size=12, kwargs={"row_factory": dict_row}, open=True, timeout=15)
            self._migrate_postgres()
        else:
            assert path is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            self._backup_old_schema(path)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._migrate_sqlite()
            try:
                path.chmod(0o600)
            except OSError:
                pass

    @staticmethod
    def _backup_old_schema(path: Path) -> None:
        """Version 1 was a single-user file. Keep it untouched and start a new database."""
        if not path.exists():
            return
        conn = sqlite3.connect(path)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        if version == 1:
            backup = path.with_name(f"console.v{version}.{int(time.time())}.db")
            path.rename(backup)
            for suffix in ("-wal", "-shm"):
                extra = path.with_name(path.name + suffix)
                if extra.exists():
                    extra.unlink()
            log.warning("Saved the old database as %s and created a new one.", backup.name)

    def _migrate_sqlite(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > schema.SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {version} is newer than this app supports.")
        if version == 0:
            with self._conn:
                self._conn.executescript(schema.sqlite_statements())
        elif version < schema.SCHEMA_VERSION:
            with self._conn:
                for statement in schema.upgrade_statements(version, schema.SQLITE_TYPES):
                    self._conn.execute(statement)
        if 0 < version < 4:
            self._fill_account_fields()
        if version < schema.SCHEMA_VERSION:
            with self._conn:
                self._conn.execute(f"PRAGMA user_version={schema.SCHEMA_VERSION}")

    def _migrate_postgres(self) -> None:
        with self._pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(727001)")
            cursor.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            cursor.execute("SELECT version FROM schema_version")
            row = cursor.fetchone()
            version = row["version"] if row else 0
            if version > schema.SCHEMA_VERSION:
                raise RuntimeError(f"Database schema {version} is newer than this app supports.")
            if version == 0:
                for statement in schema.postgres_statements():
                    cursor.execute(statement)
                cursor.execute("INSERT INTO schema_version(version) VALUES (%s)", (schema.SCHEMA_VERSION,))
            elif version < schema.SCHEMA_VERSION:
                for statement in schema.upgrade_statements(version, schema.POSTGRES_TYPES):
                    cursor.execute(statement)
                cursor.execute("UPDATE schema_version SET version = %s", (schema.SCHEMA_VERSION,))
        if 0 < version < 4:
            self._fill_account_fields()
            for column in ("username", "email", "name"):
                self._execute(f"ALTER TABLE users ALTER COLUMN {column} SET NOT NULL")  # noqa: S608

    def _fill_account_fields(self) -> None:
        """Every account now has an email (also its username) and a name. Fixes older rows without losing them."""
        taken: set[str] = set()
        for row in self._all("SELECT id, username, email, name FROM users ORDER BY id"):
            email = (row["email"] or "").strip().lower()
            if "@" not in email:
                username = (row["username"] or "").strip().lower()
                email = username if "@" in username else f"{username or 'user' + str(row['id'])}@rootcause.local"
            if email in taken:
                local, _, domain = email.partition("@")
                email = f"{local}+{row['id']}@{domain}"
            taken.add(email)
            name = sanitize_stored_name(row["name"] or "", name_from_email(email))
            self._execute("UPDATE users SET email = ?, username = ?, name = ? WHERE id = ?", (email, email, name, row["id"]))

    def close(self) -> None:
        if self.is_postgres:
            self._pool.close()
        else:
            with self._lock:
                self._conn.close()

    @staticmethod
    def _pg_sql(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    # Synchronous primitives -------------------------------------------------
    def _execute(self, sql: str, args: tuple | list = ()) -> int:
        if self.is_postgres:
            with self._pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(self._pg_sql(sql), args)
                return cursor.rowcount
        with self._lock, self._conn:
            return self._conn.execute(sql, args).rowcount

    def _insert(self, sql: str, args: tuple | list = ()) -> int:
        if self.is_postgres:
            with self._pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(self._pg_sql(sql) + " RETURNING id", args)
                return cursor.fetchone()["id"]
        with self._lock, self._conn:
            return self._conn.execute(sql, args).lastrowid

    def _all(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        if self.is_postgres:
            with self._pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute(self._pg_sql(sql), args)
                return [_clean(row) for row in cursor.fetchall()]
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, args).fetchall()]

    # Async API --------------------------------------------------------------
    async def execute(self, sql: str, args: tuple | list = ()) -> int:
        return await asyncio.to_thread(self._execute, sql, args)

    async def insert(self, sql: str, args: tuple | list = ()) -> int:
        return await asyncio.to_thread(self._insert, sql, args)

    async def all(self, sql: str, args: tuple | list = ()) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._all, sql, args)

    async def one(self, sql: str, args: tuple | list = ()) -> dict[str, Any] | None:
        rows = await self.all(sql, args)
        return rows[0] if rows else None

    # Settings ---------------------------------------------------------------
    async def get_setting(self, org_id: int, key: str, default: Any = None) -> Any:
        row = await self.one("SELECT value FROM settings WHERE org_id = ? AND key = ?", (org_id, key))
        return json.loads(row["value"]) if row else default

    async def set_setting(self, org_id: int, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO settings(org_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(org_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (org_id, key, json.dumps(value), utcnow()),
        )

    # Audit trail: each organization has its own hash chain -------------------
    def _audit(self, org_id: int, actor: str, action: str, target: str, detail: dict[str, Any]) -> None:
        body = json.dumps(detail, sort_keys=True, ensure_ascii=False)
        insert = "INSERT INTO audit_log(org_id, ts, actor, action, target, detail, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        latest = "SELECT hash FROM audit_log WHERE org_id = ? ORDER BY id DESC LIMIT 1"
        ts = utcnow()
        if self.is_postgres:
            with self._pool.connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", (727002, org_id))
                cursor.execute(self._pg_sql(latest), (org_id,))
                row = cursor.fetchone()
                prev = row["hash"] if row else "genesis"
                cursor.execute(self._pg_sql(insert), (org_id, ts, actor, action, target, body, prev,
                                                      _audit_hash(prev, org_id, ts, actor, action, target, body)))
            return
        with self._lock, self._conn:
            row = self._conn.execute(latest, (org_id,)).fetchone()
            prev = row["hash"] if row else "genesis"
            self._conn.execute(insert, (org_id, ts, actor, action, target, body, prev,
                                        _audit_hash(prev, org_id, ts, actor, action, target, body)))

    async def audit(self, org_id: int, action: str, target: str = "", detail: dict[str, Any] | None = None, actor: str = "system") -> None:
        await asyncio.to_thread(self._audit, org_id, actor, action, target, detail or {})

    def _verify_audit(self, org_id: int) -> dict[str, Any]:
        prev = "genesis"
        count = 0
        for row in self._all("SELECT * FROM audit_log WHERE org_id = ? ORDER BY id", (org_id,)):
            expected = _audit_hash(prev, org_id, row["ts"], row["actor"], row["action"], row["target"], row["detail"])
            if row["prev_hash"] != prev or row["hash"] != expected:
                return {"intact": False, "entries": count, "broken_at_id": row["id"]}
            prev = row["hash"]
            count += 1
        return {"intact": True, "entries": count, "broken_at_id": None}

    async def verify_audit(self, org_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self._verify_audit, org_id)

    # Search -----------------------------------------------------------------
    async def index_text(self, org_id: int, conversation_id: int, body: str) -> None:
        if body.strip():
            await self.execute(
                "INSERT INTO search_index(org_id, conversation_id, body) VALUES (?, ?, ?)",
                (org_id, conversation_id, body),
            )

    async def unindex(self, conversation_id: int) -> None:
        await self.execute("DELETE FROM search_index WHERE conversation_id = ?", (conversation_id,))

    async def search_conversation_ids(self, org_id: int, terms: list[str], limit: int = 50) -> list[int]:
        """Return this organization's conversation ids by relevance. Search terms stay literal."""
        clean = [term for term in terms if term.strip()][:32]
        if not clean:
            return []
        if self.is_postgres:
            query = " | ".join("'" + term.replace("'", "''").replace("\\", "") + "'" for term in clean)
            rows = await self.all(
                "SELECT conversation_id, max(ts_rank(tsv, to_tsquery('simple', ?))) AS best FROM search_index "
                "WHERE org_id = ? AND tsv @@ to_tsquery('simple', ?) GROUP BY conversation_id ORDER BY best DESC LIMIT ?",
                (query, org_id, query, limit),
            )
        else:
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in clean)
            rows = await self.all(
                "SELECT conversation_id, min(rank) AS best FROM search_index "
                "WHERE search_index MATCH ? AND org_id = ? GROUP BY conversation_id ORDER BY best LIMIT ?",
                (expression, org_id, limit),
            )
        return [int(row["conversation_id"]) for row in rows]
