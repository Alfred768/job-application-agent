from job_agent.field_semantics import classify_field, runtime_semantic_rules, value_for_semantic


def test_semantics_use_accessibility_metadata_when_visible_label_is_missing():
    field = {
        "label": "",
        "id": "candidate-contact-42",
        "ariaLabel": "Applicant contact email",
        "autocomplete": "email",
    }

    semantic = classify_field(field)

    assert semantic is not None
    assert semantic.key == "contact.email"
    assert value_for_semantic(semantic, {"email": "candidate@example.com"}) == "candidate@example.com"


def test_semantics_use_native_tel_autocomplete_without_matching_label_text():
    field = {"label": "", "id": "contact-42", "autocomplete": "tel"}

    semantic = classify_field(field)

    assert semantic is not None
    assert semantic.key == "contact.phone"
    assert value_for_semantic(semantic, {"phone": "+1 555 0100"}) == "+1 555 0100"


def test_semantics_map_combined_legal_name_to_full_name():
    profile = {"name": "Gaoyi Wu", "first_name": "Gaoyi", "last_name": "Wu"}

    semantic = classify_field({"label": "Legal First and Last Name"})

    assert semantic is not None
    assert semantic.key == "identity.full_name"
    assert value_for_semantic(semantic, profile) == "Gaoyi Wu"


def test_semantics_map_most_recent_employer_to_current_company():
    profile = {"work_history": [{"current": True, "company": "Intellisys Lab"}]}

    semantic = classify_field({"label": "Current or Most Recent Employer"})

    assert semantic is not None
    assert semantic.key == "work.current.company"
    assert value_for_semantic(semantic, profile) == "Intellisys Lab"


def test_semantics_disambiguate_education_date_components_from_section_and_id():
    profile = {"education": [{"start_date": "2024-09", "end_date": "2026-05"}]}
    field = {
        "label": "Month",
        "id": "end-month--0",
        "name": "",
        "section": "education",
    }

    semantic = classify_field(field)

    assert semantic is not None
    assert semantic.key == "education.end.month"
    assert value_for_semantic(semantic, profile) == "May"


def test_semantics_read_camel_case_workday_date_sections():
    profile = {"work_history": [{"current": True, "start_date": "2022-01-04"}]}
    month = {
        "label": "Month",
        "id": "workExperience-startDate-dateSectionMonth-input",
        "section": "work",
    }
    day = {**month, "label": "Day", "id": "workExperience-startDate-dateSectionDay-input"}
    year = {**month, "label": "Year", "id": "workExperience-startDate-dateSectionYear-input"}

    assert classify_field(month).key == "work.start.month"
    assert classify_field(day).key == "work.start.day"
    assert classify_field(year).key == "work.start.year"
    assert value_for_semantic(classify_field(month), profile) == "January"
    assert value_for_semantic(classify_field(day), profile) == "04"
    assert value_for_semantic(classify_field(year), profile) == "2022"

    inferred = classify_field({"label": "Month", "id": month["id"]})
    assert inferred is not None
    assert inferred.key == "work.start.month"


def test_semantics_do_not_treat_reference_name_as_applicant_name():
    semantic = classify_field({"label": "Reference name", "id": "reference-name"})

    assert semantic is None


def test_semantics_do_not_treat_schoolwork_in_an_essay_as_education_field():
    field = {
        "label": (
            "Tell us about a time you took full ownership of a challenging moment "
            "(outside of your schoolwork), and saw it through to the end."
        ),
        "id": "question_11185068007",
        "type": "textarea",
    }

    assert classify_field(field) is None

    long_school_question = {
        "label": (
            "Describe how your school experience shaped the way you collaborate, "
            "handle setbacks, and deliver a difficult project with others."
        ),
        "type": "textarea",
    }
    assert classify_field(long_school_question) is None


def test_semantics_do_not_treat_long_work_country_question_as_address_country():
    field = {
        "label": (
            "Will you now or at any time in the future require employer sponsorship "
            "to obtain or maintain employment authorization to work in the country "
            "where this role is based?"
        ),
        "id": "input_CA_49231_input",
        "role": "combobox",
    }

    assert classify_field(field) is None


def test_semantics_distinguish_eligible_work_country_from_address_country():
    field = {
        "label": "In which of the following employment eligible countries are you seeking to work, if hired?",
        "name": "country",
    }

    semantic = classify_field(field)

    assert semantic is not None
    assert semantic.key == "employment.eligible_country"
    assert value_for_semantic(semantic, {"country": "United States"}) == "United States"


def test_node_runtime_receives_the_same_declarative_semantic_rules():
    keys = {rule["key"] for rule in runtime_semantic_rules()}

    assert "contact.email" in keys
    assert "education.end.month" in keys
    assert "work.current.company" in keys
