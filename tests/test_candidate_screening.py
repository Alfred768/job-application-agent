from job_agent.candidate_screening import screen_job_for_candidate
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
