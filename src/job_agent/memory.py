"""Long-term application history adapter for the Agent Core."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class SQLiteApplicationMemory:
    """Persist sanitized agent summaries alongside application history."""

    _ALLOWED_NAMESPACES = {"agent_run", "policy_event", "repair_event"}

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def search(self, query: str, *, limit: int = 5) -> list[Mapping[str, Any]]:
        if not self.database_path.is_file():
            return []
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            records = self._search_applications(connection, query, limit)
            if len(records) < limit:
                records.extend(
                    self._search_agent_memory(
                        connection,
                        query,
                        limit=limit - len(records),
                    )
                )
            return records
        finally:
            connection.close()

    def remember(self, namespace: str, record: Mapping[str, Any]) -> None:
        if namespace not in self._ALLOWED_NAMESPACES:
            raise ValueError(f"unsupported memory namespace: {namespace}")
        sanitized = self._sanitize(record)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            self._init_schema(connection)
            connection.execute(
                """
                insert into agent_memory(namespace, record_json, created_at)
                values (?, ?, ?)
                """,
                (
                    namespace,
                    json.dumps(sanitized, sort_keys=True, ensure_ascii=True),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _search_applications(
        connection: sqlite3.Connection,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not SQLiteApplicationMemory._table_exists(connection, "applications"):
            return []
        tokens = [token for token in query.strip().split() if token]
        clauses = [
            """
            lower(
                coalesce(company, '') || ' ' ||
                coalesce(title, '') || ' ' ||
                coalesce(apply_url, '')
            ) like ?
            """
            for _ in tokens
        ]
        where = " and ".join(clauses) if clauses else "1 = 1"
        parameters = [f"%{token.lower()}%" for token in tokens]
        rows = connection.execute(
            f"""
            select company, title, status, apply_url, submitted_at
            from applications
            where {where}
            order by id desc
            limit ?
            """,
            (*parameters, max(0, limit)),
        ).fetchall()
        return [
            {
                "namespace": "application_history",
                "company": row["company"],
                "title": row["title"],
                "status": row["status"],
                "apply_url": row["apply_url"],
                "submitted_at": row["submitted_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _search_agent_memory(
        connection: sqlite3.Connection,
        query: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0 or not SQLiteApplicationMemory._table_exists(
            connection,
            "agent_memory",
        ):
            return []
        rows = connection.execute(
            """
            select namespace, record_json, created_at
            from agent_memory
            where record_json like ?
            order by id desc
            limit ?
            """,
            (f"%{query.strip()}%", limit),
        ).fetchall()
        records = []
        for row in rows:
            try:
                payload = json.loads(row["record_json"])
            except json.JSONDecodeError:
                payload = {}
            records.append(
                {
                    "namespace": row["namespace"],
                    "created_at": row["created_at"],
                    **payload,
                }
            )
        return records

    @staticmethod
    def _init_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            create table if not exists agent_memory (
                id integer primary key autoincrement,
                namespace text not null,
                record_json text not null,
                created_at text not null
            )
            """
        )

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        sensitive_keys = {
            "answer",
            "answers",
            "candidate_password",
            "email",
            "password",
            "phone",
            "profile",
            "resume_text",
            "sensitive_kb",
        }
        if isinstance(value, Mapping):
            return {
                str(key): cls._sanitize(nested)
                for key, nested in value.items()
                if str(key).lower() not in sensitive_keys
            }
        if isinstance(value, (list, tuple)):
            return [cls._sanitize(item) for item in value]
        if isinstance(value, Path):
            return value.name
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)
