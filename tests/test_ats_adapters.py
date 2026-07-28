from job_agent import python_runtime
from job_agent.ats_adapters import adapter_profile_key_for_field, detect_ats_from_url, runtime_ats_adapters


def _profile():
    return {
        "name": "Gaoyi Wu",
        "first_name": "Gaoyi",
        "last_name": "Wu",
        "preferred_name": "Alfred",
        "email": "gaoyi@example.com",
        "phone": "+1 555 0100",
        "location": "Hoboken, NJ",
        "state": "NJ",
        "zip": "07030",
        "_application_url": "https://company.example/open-positions#/6608351003/apply",
    }


def test_detect_ats_from_url_supports_greenhouse_custom_hash_routes():
    assert detect_ats_from_url("https://company.example/open-positions#/6608351003/apply") == "greenhouse"


def test_detect_ats_from_url_supports_workday_and_falls_back_to_generic():
    assert detect_ats_from_url("https://acme.wd5.myworkdayjobs.com/en-US/External/job/123") == "workday"
    assert detect_ats_from_url("https://example.com/careers/software-engineer") == "generic"


def test_adapter_profile_key_for_field_uses_data_automation_id():
    field = {
        "tag": "input",
        "automationId": "legalNameSection_firstName",
    }

    assert adapter_profile_key_for_field(field, ats_name="workday") == "first_name"


def test_python_runtime_map_text_value_uses_adapter_registry_for_workday_and_greenhouse():
    profile = _profile()

    assert (
        python_runtime._map_text_value(
            {
                "tag": "input",
                "label": "",
                "id": "",
                "name": "",
                "automationId": "legalNameSection_firstName",
            },
            profile,
        )
        == "Gaoyi"
    )
    assert (
        python_runtime._map_text_value(
            {
                "tag": "input",
                "label": "",
                "id": "",
                "name": "preferred_name",
            },
            profile,
        )
        == "Alfred"
    )


def test_runtime_ats_adapters_exports_greenhouse_and_workday_field_maps():
    exported = {item["name"]: item for item in runtime_ats_adapters()}

    assert exported["greenhouse"]["field_map"]['input[name="preferred_name"]'] == "preferred_name"
    assert exported["workday"]["field_map"]['[data-automation-id="legalNameSection_firstName"]'] == "first_name"
