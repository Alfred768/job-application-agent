from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from openai import OpenAI


DEFAULT_LOCAL_EMBEDDING_MODEL = "local-hash-embedding-v1"
PROFILE_STORE_SOURCE = "profile_store"


def init_profile_vector_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists profile_embeddings (
            doc_id integer primary key,
            embedding_model text not null,
            embedding_json text not null,
            provider text not null,
            created_at text not null default current_timestamp,
            foreign key(doc_id) references profile_documents(id)
        );
        """
    )
    conn.commit()


def export_profile_chunks(db_path: str | Path, out_path: str | Path) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out.open("w") as handle:
        for row in conn.execute(
            "select id, doc_type, title, source, content, metadata_json "
            "from profile_documents order by id"
        ):
            payload = dict(row)
            payload["metadata"] = json.loads(payload.pop("metadata_json") or "{}")
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1
    conn.close()
    return count


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _decode_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item))
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_format_value(val)}" for key, val in value.items())
    return str(value)


def _fact_map(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "profile_facts"):
        return {}
    rows = conn.execute("select key, value_json from profile_facts order by key")
    return {row["key"]: _decode_json(row["value_json"]) for row in rows}


def _sensitive_answers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "profile_sensitive_answers"):
        return []
    rows = conn.execute(
        "select key, label, patterns_json, answer, approved "
        "from profile_sensitive_answers order by key"
    )
    answers = []
    for row in rows:
        answers.append(
            {
                "key": row["key"],
                "label": row["label"],
                "patterns": _decode_json(row["patterns_json"]),
                "answer": row["answer"],
                "approved": bool(row["approved"]),
            }
        )
    return answers


def _render_fact_lines(facts: dict[str, Any], keys: list[str]) -> str:
    lines = []
    for key in keys:
        if key not in facts:
            continue
        value = facts[key]
        if value in (None, "", [], {}):
            continue
        lines.append(f"{key}: {_format_value(value)}")
    return "\n".join(lines)


def sync_profile_summary_documents(db_path: str | Path) -> int:
    """Materialize profile facts and approved answer-bank entries as vector docs.

    The profile store keeps atomic facts in `profile_facts`, but the vector
    index works over `profile_documents`. This sync step creates stable summary
    documents so contact info, preferences, and approved screening answers are
    searchable alongside work history and projects.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    facts = _fact_map(conn)
    sensitive_answers = _sensitive_answers(conn)
    if not _table_exists(conn, "profile_documents"):
        conn.execute(
            """
            create table profile_documents (
                id integer primary key autoincrement,
                doc_type text not null,
                title text not null,
                source text,
                content text not null,
                metadata_json text not null default '{}'
            )
            """
        )

    documents = [
        {
            "doc_type": "profile_summary",
            "title": "Contact, Links, Address, and Experience",
            "content": _render_fact_lines(
                facts,
                [
                    "name",
                    "first_name",
                    "last_name",
                    "email",
                    "phone",
                    "birthday",
                    "location",
                    "address_line1",
                    "address_line2",
                    "address_line3",
                    "city",
                    "state",
                    "region",
                    "country",
                    "postal_code",
                    "linkedin",
                    "github",
                    "portfolio",
                    "website",
                    "years_experience",
                    "career_stage",
                    "us_arrival_context",
                    "family_employment_history_us",
                    "personal_us_government_employment_history",
                    "personal_us_company_employment_history",
                ],
            ),
            "metadata": {"source_tables": ["profile_facts"]},
        },
        {
            "doc_type": "profile_preferences",
            "title": "Role Preferences, Skills, Industries, and Compensation",
            "content": _render_fact_lines(
                facts,
                [
                    "job_search_status",
                    "role_types",
                    "role_levels",
                    "desired_locations",
                    "interested_roles",
                    "specializations",
                    "skills",
                    "values_in_new_role",
                    "ideal_company_sizes",
                    "exciting_industries",
                    "excluded_industries",
                    "excluded_skills",
                    "minimum_expected_salary",
                    "security_clearance_roles",
                    "work_authorization_by_country",
                ],
            ),
            "metadata": {"source_tables": ["profile_facts"]},
        },
    ]

    approved_lines = []
    for item in sensitive_answers:
        if not item["approved"]:
            continue
        patterns = _format_value(item["patterns"])
        approved_lines.append(
            f"{item['key']}: {item['label']} -> {item['answer']} "
            f"(approved: Yes; patterns: {patterns})"
        )
    if approved_lines:
        documents.append(
            {
                "doc_type": "profile_sensitive_answers",
                "title": "Approved Screening and Sensitive Answers",
                "content": "\n".join(approved_lines),
                "metadata": {"source_tables": ["profile_sensitive_answers"]},
            }
        )

    conn.execute(
        "delete from profile_documents where source = ? and doc_type in "
        "('profile_summary', 'profile_preferences', 'profile_sensitive_answers')",
        (PROFILE_STORE_SOURCE,),
    )
    count = 0
    for document in documents:
        if not document["content"].strip():
            continue
        conn.execute(
            "insert into profile_documents "
            "(doc_type, title, source, content, metadata_json) values (?, ?, ?, ?, ?)",
            (
                document["doc_type"],
                document["title"],
                PROFILE_STORE_SOURCE,
                document["content"],
                json.dumps(document["metadata"], ensure_ascii=False),
            ),
        )
        count += 1
    conn.commit()
    conn.close()
    return count


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9+#.]+", (text or "").lower())


def local_hash_embedding(text: str, dimensions: int = 384) -> list[float]:
    vector = [0.0] * dimensions
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = -1.0 if digest[4] & 1 else 1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _embedding_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("OPENAI_API_KEY/LLM_API_KEY and LLM_BASE_URL are required")
    return OpenAI(api_key=api_key, base_url=base_url, timeout=60)


def openai_embeddings(texts: list[str], model: str | None = None) -> tuple[str, list[list[float]]]:
    embedding_model = (
        model
        or os.getenv("EMBEDDING_MODEL_ID")
        or os.getenv("LLM_EMBEDDING_MODEL_ID")
        or "text-embedding-3-small"
    )
    response = _embedding_client().embeddings.create(model=embedding_model, input=texts)
    return embedding_model, [list(item.embedding) for item in response.data]


def _documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            "select id, doc_type, title, source, content, metadata_json "
            "from profile_documents order by id"
        )
    )


def _document_text(row: sqlite3.Row) -> str:
    return "\n".join(
        part
        for part in [
            str(row["doc_type"] or ""),
            str(row["title"] or ""),
            str(row["source"] or ""),
            str(row["content"] or ""),
        ]
        if part
    )


def index_profile_embeddings(
    db_path: str | Path,
    model: str | None = None,
    provider: str = "openai",
    fallback_local: bool = True,
) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    init_profile_vector_schema(conn)
    docs = _documents(conn)
    texts = [_document_text(row) for row in docs]
    used_provider = provider
    used_model = model or os.getenv("EMBEDDING_MODEL_ID") or os.getenv("LLM_EMBEDDING_MODEL_ID")
    try:
        if provider == "openai":
            used_model, vectors = openai_embeddings(texts, model=model)
        elif provider == "local":
            used_provider = "local"
            used_model = DEFAULT_LOCAL_EMBEDDING_MODEL
            vectors = [local_hash_embedding(text) for text in texts]
        else:
            raise ValueError(f"unsupported embedding provider: {provider}")
    except Exception as exc:
        if not fallback_local:
            conn.close()
            raise
        used_provider = "local"
        used_model = DEFAULT_LOCAL_EMBEDDING_MODEL
        vectors = [local_hash_embedding(text) for text in texts]
        fallback_reason = str(exc)
    else:
        fallback_reason = None

    for row, vector in zip(docs, vectors):
        conn.execute(
            "replace into profile_embeddings "
            "(doc_id, embedding_model, embedding_json, provider, created_at) "
            "values (?, ?, ?, ?, current_timestamp)",
            (row["id"], used_model, json.dumps(vector), used_provider),
        )
    conn.execute(
        "delete from profile_embeddings "
        "where doc_id not in (select id from profile_documents)"
    )
    conn.commit()
    count = conn.execute("select count(*) from profile_embeddings").fetchone()[0]
    conn.close()
    return {
        "documents": len(docs),
        "embeddings": count,
        "provider": used_provider,
        "model": used_model,
        "fallback_reason": fallback_reason,
    }


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def search_profile_embeddings(
    db_path: str | Path,
    query: str,
    top_k: int = 5,
    provider: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_profile_vector_schema(conn)
    stored = list(
        conn.execute(
            "select d.id, d.doc_type, d.title, d.source, d.content, d.metadata_json, "
            "e.embedding_model, e.embedding_json, e.provider "
            "from profile_documents d join profile_embeddings e on e.doc_id = d.id"
        )
    )
    if not stored:
        conn.close()
        return []

    selected_provider = provider or stored[0]["provider"]
    selected_model = model or stored[0]["embedding_model"]
    if selected_provider == "openai":
        try:
            _, query_vector = openai_embeddings([query], model=selected_model)
            qvec = query_vector[0]
        except Exception:
            qvec = local_hash_embedding(query)
    else:
        qvec = local_hash_embedding(query)

    results = []
    for row in stored:
        vector = json.loads(row["embedding_json"])
        results.append(
            {
                "score": cosine_similarity(qvec, vector),
                "id": row["id"],
                "doc_type": row["doc_type"],
                "title": row["title"],
                "source": row["source"],
                "content": row["content"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
        )
    conn.close()
    return sorted(results, key=lambda item: item["score"], reverse=True)[:top_k]
