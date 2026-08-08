import json
import sqlite3

from job_agent.profile_vector_store import (
    cosine_similarity,
    export_profile_chunks,
    index_profile_embeddings,
    local_hash_embedding,
    search_profile_embeddings,
    sync_profile_summary_documents,
)


def _profile_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table profile_documents (
            id integer primary key autoincrement,
            doc_type text not null,
            title text not null,
            source text,
            content text not null,
            metadata_json text not null default '{}'
        );
        """
    )
    conn.execute(
        "insert into profile_documents (doc_type, title, source, content, metadata_json) values (?, ?, ?, ?, ?)",
        (
            "project",
            "RAG Evaluation",
            "",
            "Built RAG evaluation pipelines with LangChain and BERT metrics.",
            json.dumps({"skills": ["RAG", "LangChain"]}),
        ),
    )
    conn.execute(
        "insert into profile_documents (doc_type, title, source, content, metadata_json) values (?, ?, ?, ?, ?)",
        (
            "work_history",
            "Churn Prediction",
            "DHL",
            "Built XGBoost churn prediction and SHAP explainability workflows.",
            "{}",
        ),
    )
    conn.commit()
    conn.close()


def test_local_hash_embedding_is_normalized_and_stable():
    left = local_hash_embedding("RAG LangChain evaluation")
    right = local_hash_embedding("RAG LangChain evaluation")

    assert left == right
    assert round(cosine_similarity(left, right), 6) == 1.0


def test_profile_vector_store_indexes_and_searches_local_embeddings(tmp_path):
    db = tmp_path / "profile.db"
    chunks = tmp_path / "chunks.jsonl"
    _profile_db(db)

    assert export_profile_chunks(db, chunks) == 2
    result = index_profile_embeddings(db, provider="local")
    matches = search_profile_embeddings(db, "LangChain RAG evaluation", top_k=1, provider="local")

    assert result["embeddings"] == 2
    assert result["provider"] == "local"
    assert matches[0]["title"] == "RAG Evaluation"


def test_profile_summary_documents_are_synced_for_vector_search(tmp_path):
    db = tmp_path / "profile.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table profile_facts (
          key text primary key,
          value_json text not null,
          category text not null
        );
        create table profile_documents (
          id integer primary key autoincrement,
          doc_type text not null,
          title text not null,
          source text,
          content text not null,
          metadata_json text not null default '{}'
        );
        create table profile_sensitive_answers (
          key text primary key,
          label text not null,
          patterns_json text not null,
          answer text not null,
          approved integer not null
        );
        """
    )
    facts = {
        "address_line1": "132 New York Avenue",
        "city": "Jersey City",
        "state": "NJ",
        "postal_code": "07307",
        "years_experience": "1-2",
        "work_authorization_by_country": {
            "us": "Yes",
            "canada": "No",
            "united_kingdom": "No",
            "requires_sponsorship": "Yes",
        },
    }
    for key, value in facts.items():
        conn.execute(
            "insert into profile_facts (key, value_json, category) values (?, ?, ?)",
            (key, json.dumps(value), "profile"),
        )
    conn.execute(
        "insert into profile_sensitive_answers "
        "(key, label, patterns_json, answer, approved) values (?, ?, ?, ?, ?)",
        (
            "relocation",
            "Relocation",
            json.dumps(["relocation", "relocate"]),
            "Yes",
            1,
        ),
    )
    conn.commit()
    conn.close()

    synced = sync_profile_summary_documents(db)
    result = index_profile_embeddings(db, provider="local")
    matches = search_profile_embeddings(
        db,
        "132 New York Avenue 1-2 years authorization relocation",
        top_k=3,
        provider="local",
    )

    assert synced == 3
    assert result["embeddings"] == 3
    assert {match["doc_type"] for match in matches} >= {
        "profile_summary",
        "profile_sensitive_answers",
    }


def test_reindex_removes_stale_profile_embeddings_after_resync(tmp_path):
    db = tmp_path / "profile.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        create table profile_facts (
          key text primary key,
          value_json text not null,
          category text not null
        );
        create table profile_documents (
          id integer primary key autoincrement,
          doc_type text not null,
          title text not null,
          source text,
          content text not null,
          metadata_json text not null default '{}'
        );
        """
    )
    conn.execute(
        "insert into profile_facts (key, value_json, category) values (?, ?, ?)",
        ("address_line1", json.dumps("132 New York Avenue"), "profile"),
    )
    conn.execute(
        "insert into profile_facts (key, value_json, category) values (?, ?, ?)",
        (
            "personal_us_company_employment_history",
            json.dumps("Never worked for a United States company."),
            "candidate_fact",
        ),
    )
    conn.commit()
    conn.close()

    sync_profile_summary_documents(db)
    first = index_profile_embeddings(db, provider="local")
    sync_profile_summary_documents(db)
    second = index_profile_embeddings(db, provider="local")

    assert first["documents"] == 1
    assert second["documents"] == 1
    assert second["embeddings"] == 1
    matches = search_profile_embeddings(
        db,
        "United States company employment history",
        top_k=1,
        provider="local",
    )
    assert "Never worked for a United States company." in matches[0]["content"]
