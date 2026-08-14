from job_agent.candidate_screening import application_url_unusable, screen_job_for_candidate
from job_agent.models import Job


def _early_career_profile():
    return {
        "country": "United States",
        "work_history": [{"title": "ML Engineer Intern"}],
    }


def test_screening_rejects_explicit_non_us_location_for_us_candidate():
    result = screen_job_for_candidate(
        Job(title="Machine Learning Engineer", company="Example", raw_jd="", location="Mexico City, Mexico"),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_non_us_city_name_for_us_candidate():
    result = screen_job_for_candidate(
        Job(title="Data Engineer", company="Example", raw_jd="", location="Warsaw"),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_common_non_us_city_country_pairs_for_us_candidate():
    for location in [
        "Bucharest, Romania",
        "Galway, Ireland",
        "Auckland",
        "Osborne Park",
        "Budapest, Hungary",
        "Belgrade, Serbia",
        "Aarhus, Denmark",
        "Stockholm, Sweden",
        "Helsinki, Finland",
        "Herzliya, Israel",
        "Milan, Italy",
    ]:
        result = screen_job_for_candidate(
            Job(title="Software Engineer", company="Example", raw_jd="", location=location),
            _early_career_profile(),
        )

        assert result.eligible is False, location
        assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_foreign_city_only_locations_for_us_candidate():
    for location in [
        "London",
        "Munich",
        "Dublin",
        "Paris",
        "Prague",
        "Zürich",
        "Wien",
        "Rotterdam",
        "Kyiv, Ukraine",
        "Brasil",
        "Toulouse",
        "Nantes",
        "Espoo",
        "Bengaluru",
        "Tel Aviv",
        "Abu Dhabi",
        "Hong Kong",
        "Kuala Lumpur",
        "Seoul",
        "Melbourne",
        "Lisbon",
        "Athens",
        "D\u00fcsseldorf",
        "Reykjav\u00edk",
        "Shanghai",
        "Gurugram",
        "Kuwait - Main Office",
        "Norway",
        "Sao Jose dos Campos",
    ]:
        result = screen_job_for_candidate(
            Job(title="Software Engineer", company="Example", raw_jd="", location=location),
            _early_career_profile(),
        )

        assert result.eligible is False, location
        assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_johor_bahru_malaysia_location_for_us_candidate():
    result = screen_job_for_candidate(
        Job(
            title="Cloud Infra/DevOps Engineer",
            company="AvePoint",
            raw_jd="",
            location="Johor Bahru, Johor, Malaysia",
        ),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_additional_european_and_abbreviation_locations():
    for location in [
        "Sofia, Bulgaria",
        "Ramat Gan (Hybrid)",
        "Slovenia / Remote",
        "Riga, Latvia",
        "Bulgaria",
        "Slovakia",
        "Bratislava, Slovakia",
        "Edmonton, AB, CAN",
    ]:
        result = screen_job_for_candidate(
            Job(title="Software Engineer", company="Example", raw_jd="", location=location),
            _early_career_profile(),
        )

        assert result.eligible is False, location
        assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_johor_bahru_origin_statement_in_jd():
    result = screen_job_for_candidate(
        Job(
            title="Cloud Infra/DevOps Engineer",
            company="AvePoint",
            raw_jd="This role is fully onsite, based in Johor Bahru. "
            "Are you a Malaysia Citizen or Malaysia Permanent Resident?",
            location="All",
        ),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_strong_non_us_origin_statement_in_jd():
    result = screen_job_for_candidate(
        Job(
            title="IT Infrastructure Engineer",
            company="Kyivstar",
            raw_jd="Kyivstar.Tech is a Ukrainian hybrid IT company.",
            location="All",
        ),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_portuguese_brazilian_origin_statement_in_jd():
    result = screen_job_for_candidate(
        Job(
            title="Sênior Software Engineer",
            company="Stone",
            raw_jd="A Stone é a maior empresa independente de meios de pagamentos do Brasil.",
            location="Remoto",
        ),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_portuguese_remote_jd_for_us_candidate():
    result = screen_job_for_candidate(
        Job(
            title="Data Scientist I",
            company="Arco",
            raw_jd=(
                "Todas as vagas da Arco são elegíveis para Pessoas com "
                "Deficiência. No dia a dia você irá atuar com o time."
            ),
            location="Remoto",
        ),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("outside" in reason for reason in result.reasons)


def test_screening_rejects_foreign_only_employers_for_us_candidate():
    for company, title, location in [
        ("SFEIR", "GenAI Engineer", "Niort"),
        ("ICEYE", "Flight Software Engineer", "Espoo"),
        ("Arco Educação", "Data Scientist I", "Remoto"),
        ("Kyivstar", "IT Infrastructure Engineer", "All"),
        ("iFood", "Software Engineer", "Brasil"),
        ("Stone", "Software Engineer", "Remoto"),
    ]:
        result = screen_job_for_candidate(
            Job(title=title, company=company, raw_jd="", location=location),
            _early_career_profile(),
        )

        assert result.eligible is False, (company, location)
        assert any("foreign-only" in reason for reason in result.reasons)


def test_screening_rejects_non_us_board_without_us_location():
    result = screen_job_for_candidate(
        Job(
            title="Junior Software Engineer",
            company="Clarity AI",
            raw_jd="Clarity AI is a global tech company.",
            location="Remote",
            apply_url="https://job-boards.eu.greenhouse.io/clarityai/jobs/1",
            source_url="https://job-boards.eu.greenhouse.io/clarityai/jobs/1",
        ),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("non-U.S. application board" in reason for reason in result.reasons)


def test_screening_rejects_listing_without_identifiable_employer():
    result = screen_job_for_candidate(
        Job(title="Software Engineer", company="Unknown Company", raw_jd="", location="Remote"),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("employer" in reason for reason in result.reasons)


def test_screening_rejects_listing_without_identifiable_role():
    result = screen_job_for_candidate(
        Job(title="Unknown Role", company="Example", raw_jd="", location="Remote"),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("role" in reason for reason in result.reasons)


def test_screening_rejects_non_direct_application_urls():
    for url in (
        "https://www.notion.so/careers/job/123",
        "https://angel.co/company/acme/jobs/1",
        "https://news.ycombinator.com/item?id=1",
        "https://job-boards.greenhouse.io/coinbase/jobs/1",
        "https://job-boards.greenhouse.io/epicgames/jobs/2",
        "https://job-boards.greenhouse.io/wayve/jobs/3",
        "https://workatastartup.com/jobs/4",
    ):
        assert application_url_unusable(url)
        result = screen_job_for_candidate(
            Job(
                title="Machine Learning Engineer",
                company="Example",
                raw_jd="",
                location="Remote US",
                apply_url=url,
            ),
            _early_career_profile(),
        )
        assert result.eligible is False
        assert any("application form" in reason for reason in result.reasons)


def test_screening_keeps_direct_ats_board_urls():
    url = "https://boards.greenhouse.io/acme/jobs/123"
    assert not application_url_unusable(url)
    result = screen_job_for_candidate(
        Job(
            title="Machine Learning Engineer",
            company="Example",
            raw_jd="",
            location="Remote US",
            apply_url=url,
        ),
        _early_career_profile(),
    )
    assert result.eligible is True


def test_screening_rejects_clearance_requirement_when_not_clearance_eligible():
    profile = {
        **_early_career_profile(),
        "security_clearance_eligibility": "No",
        "sensitive_answers": {
            "security_clearance_eligibility": {
                "patterns": ["eligible to obtain the security clearance"],
                "answer": "No",
                "approved": True,
            }
        },
    }
    result = screen_job_for_candidate(
        Job(
            title="DevOps Engineer",
            company="Example",
            raw_jd=(
                "A current TS/SCI with Polygraph U.S. Government Security "
                "clearance is required; U.S. citizenship required."
            ),
            location="Columbia, MD",
        ),
        profile,
    )

    assert result.eligible is False
    assert any("security clearance" in reason for reason in result.reasons)


def test_screening_rejects_senior_title_for_early_career_profile():
    result = screen_job_for_candidate(
        Job(title="Senior ML Platform Engineer", company="Example", raw_jd="", location="Remote US"),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("seniority" in reason for reason in result.reasons)


def test_screening_rejects_sr_abbreviation_for_early_career_profile():
    result = screen_job_for_candidate(
        Job(title="Sr. Solutions Engineer", company="Example", raw_jd="", location="United States"),
        _early_career_profile(),
    )

    assert result.eligible is False
    assert any("seniority" in reason for reason in result.reasons)


def test_screening_keeps_us_non_senior_role():
    result = screen_job_for_candidate(
        Job(title="Machine Learning Engineer", company="Example", raw_jd="", location="Remote US"),
        _early_career_profile(),
    )

    assert result.eligible is True


def test_screening_rejects_no_sponsorship_listing_for_candidate_who_requires_it():
    result = screen_job_for_candidate(
        Job(
            title="ML Research Fellow",
            company="Example",
            raw_jd="We are not currently able to sponsor visas. Candidates need independent work authorization.",
            location="Remote US",
        ),
        {**_early_career_profile(), "requires_sponsorship": "Yes"},
    )

    assert result.eligible is False
    assert any("sponsorship" in reason for reason in result.reasons)


def test_screening_rejects_take_over_sponsorship_prohibition():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer I",
            company="Example",
            raw_jd=(
                "Applicants must be authorized to work for any employer in the United States. "
                "We are unable to sponsor or take over sponsorship of an employment Visa at this time."
            ),
            location="Remote US",
        ),
        {**_early_career_profile(), "requires_sponsorship": "Yes"},
    )

    assert result.eligible is False
    assert any("sponsorship" in reason for reason in result.reasons)


def test_screening_keeps_sponsorship_question_as_answerable_field():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer",
            company="Example",
            raw_jd=(
                "Apply for this job. Will you now or in the future require immigration "
                "sponsorship for work authorization? Select... Yes No"
            ),
            location="Remote US",
        ),
        {**_early_career_profile(), "requires_sponsorship": "Yes"},
    )

    assert result.eligible is True


def test_screening_rejects_without_current_or_future_sponsorship_requirement():
    result = screen_job_for_candidate(
        Job(
            title="Software Business Analyst",
            company="Example",
            raw_jd="Candidates must be legally authorized to work in the U.S. without current or future sponsorship.",
            location="Remote US",
        ),
        {**_early_career_profile(), "requires_sponsorship": "Yes"},
    )

    assert result.eligible is False
    assert any("sponsorship" in reason for reason in result.reasons)


def test_screening_rejects_sponsorship_not_available_listing():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer New Grad",
            company="Example",
            raw_jd="Visa sponsorship is not available for our new grad positions.",
            location="New York, NY",
        ),
        {**_early_career_profile(), "requires_sponsorship": "Yes"},
    )

    assert result.eligible is False
    assert any("sponsorship" in reason for reason in result.reasons)


def test_screening_keeps_listing_that_explicitly_sponsors_international_candidates():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer, Full Stack",
            company="Exa",
            raw_jd=(
                "Visas: We're happy to sponsor international candidates "
                "(e.g., STEM OPT, OPT, H1B, O1, E3). While we cannot guarantee "
                "your visa, we have historically been successful in sponsoring candidates."
            ),
            location="San Francisco, CA",
        ),
        {**_early_career_profile(), "requires_sponsorship": "Yes"},
    )

    assert result.eligible is True


def test_screening_rejects_us_citizenship_requirement_for_non_citizen_profile():
    result = screen_job_for_candidate(
        Job(
            title="Data Warehouse Software Engineer I",
            company="Example",
            raw_jd="This position requires U.S. citizenship due to federal government contracting requirements.",
            location="Remote US",
        ),
        {
            **_early_career_profile(),
            "sensitive_answers": {
                "citizenship": {"answer": "No", "approved": True},
            },
        },
    )

    assert result.eligible is False
    assert any("citizenship" in reason for reason in result.reasons)


def test_screening_rejects_us_person_clearance_requirement_for_non_citizen_profile():
    result = screen_job_for_candidate(
        Job(
            title="Machine Learning Engineer",
            company="Example",
            raw_jd=(
                "Applicants for this position must be a U.S. Person. "
                "Are you eligible to obtain and maintain a US Government clearance (requires US citizenship)?"
            ),
            location="El Segundo, CA",
        ),
        {
            **_early_career_profile(),
            "sensitive_answers": {
                "citizenship": {"answer": "No", "approved": True},
            },
        },
    )

    assert result.eligible is False
    assert any("citizenship" in reason for reason in result.reasons)


def test_screening_rejects_explicit_years_requirement_above_profile():
    result = screen_job_for_candidate(
        Job(
            title="AI Automation Engineer",
            company="Example",
            raw_jd="Candidates need 4+ years of experience in GTM systems.",
            location="Remote US",
        ),
        {**_early_career_profile(), "years_experience": "1-2"},
    )

    assert result.eligible is False
    assert any("4 years" in reason for reason in result.reasons)


def test_screening_rejects_domain_qualified_years_requirement_above_profile():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer, Distributed Systems",
            company="Example",
            raw_jd="We'd love to hear from you if you have 5+ years of Software Engineering experience.",
            location="United States",
        ),
        {**_early_career_profile(), "years_experience": "1-2"},
    )

    assert result.eligible is False
    assert any("5 years" in reason for reason in result.reasons)


def test_screening_rejects_range_years_requirement_above_profile():
    result = screen_job_for_candidate(
        Job(
            title="Forward Deployed Engineer",
            company="Example",
            raw_jd="These are the essentials: 3-5+ years of relevant, post-college work experience.",
            location="New York, NY",
        ),
        {**_early_career_profile(), "years_experience": "1-2"},
    )

    assert result.eligible is False
    assert any("5 years" in reason for reason in result.reasons)


def test_screening_rejects_years_building_requirement_above_profile():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer, Data Foundations",
            company="Example",
            raw_jd="About you: 3+ years building production backend or data infrastructure systems.",
            location="San Francisco, CA",
        ),
        {**_early_career_profile(), "years_experience": "1-2"},
    )

    assert result.eligible is False
    assert any("3 years" in reason for reason in result.reasons)


def test_screening_rejects_years_or_equivalent_requirement_above_profile():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer - GenAI inference",
            company="Example",
            raw_jd=(
                "Strong software engineering background "
                "(3+ years or equivalent) in performance-critical systems."
            ),
            location="San Francisco, CA",
        ),
        {**_early_career_profile(), "years_experience": "1-2"},
    )

    assert result.eligible is False
    assert any("3 years" in reason for reason in result.reasons)


def test_screening_rejects_higher_or_clause_years_requirement_above_profile():
    result = screen_job_for_candidate(
        Job(
            title="Enterprise AI Development Strategist",
            company="Example",
            raw_jd=(
                "Ideally you will have: 1-2 years of experience as an account executive "
                "OR 4+ years of experience in outbound sales, business development, SDR, BDR, or closing roles."
            ),
            location="San Francisco, CA",
        ),
        {**_early_career_profile(), "years_experience": "1-2"},
    )

    assert result.eligible is False
    assert any("4 years" in reason for reason in result.reasons)


def test_screening_rejects_years_of_building_requirement_above_profile():
    result = screen_job_for_candidate(
        Job(
            title="Machine Learning Research Engineer",
            company="Example",
            raw_jd="Ideally you'd have: 3+ years of building with LLMs in a production environment.",
            location="San Francisco, CA",
        ),
        {**_early_career_profile(), "years_experience": "1-2"},
    )

    assert result.eligible is False
    assert any("3 years" in reason for reason in result.reasons)


def test_screening_keeps_new_grad_growth_path_years_copy():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer I - Internal Tooling",
            company="Built",
            raw_jd=(
                "Required: Recent Computer Science graduate or software engineer with up to "
                "~1 year of experience. 0-2 years of professional or serious personal project "
                "experience. What Success Looks Like: You grow from new grad to engineer who "
                "owns critical internal systems in 12-18 months. This role is the fastest path "
                "to 3+ years of experience in 18 months."
            ),
            location="Nashville, TN",
        ),
        {**_early_career_profile(), "years_experience": "1-2"},
    )

    assert result.eligible is True


def _profile_with_overrides(**overrides):
    return {
        **_early_career_profile(),
        "screening_overrides": overrides,
    }


def test_citizenship_override_allows_citizenship_required_role():
    result = screen_job_for_candidate(
        Job(
            title="Data Warehouse Software Engineer I",
            company="Example",
            raw_jd="This position requires U.S. citizenship due to federal contracting.",
            location="Remote US",
        ),
        {
            **_profile_with_overrides(ignore_citizenship_requirements=True),
            "sensitive_answers": {"citizenship": {"answer": "No", "approved": True}},
        },
    )
    assert result.eligible is True


def test_seniority_override_allows_senior_title_for_early_career_profile():
    result = screen_job_for_candidate(
        Job(title="Senior ML Platform Engineer", company="Example", raw_jd="", location="Remote US"),
        _profile_with_overrides(ignore_seniority_title_filter=True),
    )
    assert result.eligible is True


def test_experience_override_allows_above_profile_years_requirement():
    result = screen_job_for_candidate(
        Job(
            title="Software Engineer, Distributed Systems",
            company="Example",
            raw_jd="We'd love to hear from you if you have 5+ years of Software Engineering experience.",
            location="United States",
        ),
        {**_profile_with_overrides(ignore_experience_requirements=True), "years_experience": "1-2"},
    )
    assert result.eligible is True


def test_phd_equivalent_bypasses_seniority_and_experience_filters():
    profile = {
        "country": "United States",
        "work_history": [{"title": "Research Assistant", "employment_type": "Internship"}],
        "years_experience": "3",
        "phd_equivalent": True,
    }
    senior_title = screen_job_for_candidate(
        Job(title="Senior ML Platform Engineer", company="Example", raw_jd="", location="Remote US"),
        profile,
    )
    assert senior_title.eligible is True
    experience = screen_job_for_candidate(
        Job(
            title="Software Engineer",
            company="Example",
            raw_jd="Candidates need 4+ years of experience in production ML.",
            location="Remote US",
        ),
        profile,
    )
    assert experience.eligible is True


def test_phd_equivalent_does_not_bypass_sponsorship_requirement():
    profile = {
        "country": "United States",
        "work_history": [{"title": "Research Assistant", "employment_type": "Internship"}],
        "years_experience": "3",
        "phd_equivalent": True,
        "requires_sponsorship": "Yes",
    }
    result = screen_job_for_candidate(
        Job(
            title="ML Research Fellow",
            company="Example",
            raw_jd="We are not currently able to sponsor visas. Candidates need independent work authorization.",
            location="Remote US",
        ),
        profile,
    )
    assert result.eligible is False
    assert any("sponsorship" in reason for reason in result.reasons)


def test_sponsorship_override_allows_no_sponsorship_listing():
    result = screen_job_for_candidate(
        Job(
            title="ML Research Fellow",
            company="Example",
            raw_jd="We are not currently able to sponsor visas. Candidates need independent work authorization.",
            location="Remote US",
        ),
        {**_early_career_profile(), "requires_sponsorship": "Yes", "screening_overrides": {"ignore_sponsorship_filter": True}},
    )
    assert result.eligible is True
