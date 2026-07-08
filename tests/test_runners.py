from job_agent.runners import render_batch_fill_runner


def test_render_batch_fill_runner_invokes_fill_scripts_without_submit_actions():
    script = render_batch_fill_runner(
        [
            {
                "company": "Acme AI",
                "title": "Agent Engineer",
                "fill_script_path": "/tmp/acme/fill-form.js",
            },
            {
                "company": "No Form Co",
                "title": "Research Engineer",
                "fill_script_path": None,
            },
        ]
    )

    assert 'spawnSync("node", [application.fill_script_path]' in script
    assert "/tmp/acme/fill-form.js" in script
    assert "No Form Co" not in script
    assert "Review each page manually before final submission." in script
    assert ".click(" not in script
    assert ".press(" not in script
    assert ".submit(" not in script
