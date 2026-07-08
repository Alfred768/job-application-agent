from job_agent.db import connect, init_db


def test_init_db_creates_core_tables(tmp_path):
    db_path = tmp_path / "agent.db"
    conn = connect(db_path)

    init_db(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    }
    assert {
        "jobs",
        "resume_templates",
        "fit_scores",
        "applications",
        "generated_documents",
    } <= tables
