from __future__ import annotations

import re
from typing import Any


_ATS_ADAPTERS: tuple[dict[str, Any], ...] = (
    {
        "name": "workday",
        "url_substrings": ("myworkdayjobs.com", "workday.com"),
        "url_regexes": (),
        "dom_selectors": ('[data-automation-id="bottom-navigation-next-button"]', "[data-automation-id]"),
        "field_map": {
            '[data-automation-id="legalNameSection_firstName"]': "first_name",
            '[data-automation-id="legalNameSection_lastName"]': "last_name",
            '[data-automation-id="addressSection_addressLine1"]': "address_line_1",
            '[data-automation-id="addressSection_city"]': "city",
            '[data-automation-id="addressSection_countryRegion"]': "country",
            '[data-automation-id="addressSection_stateProvince"]': "state",
            '[data-automation-id="addressSection_postalCode"]': "postal_code",
            '[data-automation-id="phone-number"]': "phone",
            '[data-automation-id="countryPhoneCode"]': "phone_country_code",
            '[data-automation-id="phone-device-type"]': "phone_device_type",
            '[data-automation-id="email"]': "email",
            '[data-automation-id="linkedinQuestion"]': "linkedin_url",
            '[data-automation-id="websiteQuestion"]': "website",
        },
    },
    {
        "name": "greenhouse",
        "url_substrings": ("boards.greenhouse.io", "job-boards.greenhouse.io"),
        "url_regexes": (r"#/\d+(/apply)?$",),
        "dom_selectors": ("#app_form", "#application_form", "#grnhse_app"),
        "field_map": {
            "#first_name": "first_name",
            "#last_name": "last_name",
            "#email": "email",
            "#phone": "phone",
            "#phone_country_code": "phone_country_code",
            "#job_application_phone_country_code": "phone_country_code",
            'select[name="phone_country_code"]': "phone_country_code",
            "#job_application_location": "location",
            '#job_application_answers_attributes_0_text_value': "linkedin_url",
            "#resume_text": "resume_text",
            "#cover_letter_text": "cover_letter",
            'input[name="job_application[first_name]"]': "first_name",
            'input[name="job_application[last_name]"]': "last_name",
            'input[name="job_application[email]"]': "email",
            'input[name="job_application[phone]"]': "phone",
            'input[name="first_name"]': "first_name",
            'input[name="last_name"]': "last_name",
            'input[name="preferred_name"]': "preferred_name",
            'input[name="email"]': "email",
            'input[name="phone"]': "phone",
            'input[name="resume"]': "resume_text",
        },
    },
    {
        "name": "lever",
        "url_substrings": ("jobs.lever.co", "lever.co"),
        "url_regexes": (),
        "dom_selectors": (".application-form",),
        "field_map": {
            'input[name="name"]': "full_name",
            'input[name="email"]': "email",
            'input[name="phone"]': "phone",
            'input[name="org"]': "current_company",
            'input[name="urls[LinkedIn]"]': "linkedin_url",
            'input[name="urls[GitHub]"]': "github_url",
            'input[name="urls[Portfolio]"]': "website",
            'input[name="urls[Twitter]"]': "twitter_url",
            'input[name="urls[Other]"]': "website",
            'textarea[name="comments"]': "additional_info",
            'input[name="resume"]': "resume_text",
        },
    },
    {
        "name": "icims",
        "url_substrings": ("icims.com",),
        "url_regexes": (),
        "dom_selectors": ("#iCIMS_MainWrapper", 'iframe[id*="icims"]', 'iframe[name*="icims"]'),
        "field_map": {
            "#firstName": "first_name",
            "#lastName": "last_name",
            "#email": "email",
            "#phone": "phone",
            "#addressStreet1": "address_line_1",
            "#addressCity": "city",
            "#addressState": "state",
            "#addressZip": "postal_code",
        },
    },
    {
        "name": "taleo",
        "url_substrings": ("taleo.net",),
        "url_regexes": (),
        "dom_selectors": (".taleo", '[class*="taleo"]'),
        "field_map": {
            "#FirstName": "first_name",
            "#LastName": "last_name",
            "#Email": "email",
            "#Phone": "phone",
            "#Address": "address_line_1",
            "#City": "city",
            "#State": "state",
            "#ZipCode": "postal_code",
        },
    },
)


def runtime_ats_adapters() -> list[dict[str, Any]]:
    """Return a JSON-serializable ATS adapter registry for runtime payloads."""
    return [
        {
            "name": adapter["name"],
            "url_substrings": list(adapter["url_substrings"]),
            "url_regexes": list(adapter["url_regexes"]),
            "dom_selectors": list(adapter["dom_selectors"]),
            "field_map": dict(adapter["field_map"]),
        }
        for adapter in _ATS_ADAPTERS
    ]


def detect_ats_from_url(url: str | None) -> str:
    normalized = str(url or "").strip()
    lowered = normalized.lower()
    if not lowered:
        return "generic"
    for adapter in _ATS_ADAPTERS:
        if any(part in lowered for part in adapter["url_substrings"]):
            return str(adapter["name"])
        if any(re.search(pattern, normalized, re.I) for pattern in adapter["url_regexes"]):
            return str(adapter["name"])
    if "ashbyhq" in lowered or "ashby" in lowered:
        return "ashby"
    if "smartrecruiters" in lowered:
        return "smartrecruiters"
    if "workable" in lowered:
        return "workable"
    if "recruitee" in lowered:
        return "recruitee"
    if "comeet" in lowered or re.search(r"/o/[^/?#]+/c/", normalized, re.I):
        return "comeet"
    return "generic"


def adapter_profile_key_for_field(
    field: dict[str, Any] | None,
    ats_name: str | None = None,
) -> str | None:
    if not isinstance(field, dict):
        return None
    candidates = _field_selector_candidates(field)
    if not candidates:
        return None
    adapters: list[dict[str, Any]] = []
    if ats_name:
        adapters.extend(adapter for adapter in _ATS_ADAPTERS if adapter["name"] == ats_name)
    adapters.extend(adapter for adapter in _ATS_ADAPTERS if adapter not in adapters)
    for adapter in adapters:
        field_map = adapter["field_map"]
        for candidate in candidates:
            mapped = field_map.get(candidate)
            if mapped:
                return str(mapped)
    return None


def _field_selector_candidates(field: dict[str, Any]) -> list[str]:
    tag = str(field.get("tag") or "").strip().lower()
    field_id = str(field.get("id") or "").strip()
    field_name = str(field.get("name") or "").strip()
    automation_id = str(field.get("automationId") or field.get("automation_id") or "").strip()
    candidates: list[str] = []
    if field_id:
        candidates.append(f"#{field_id}")
    if field_name:
        candidates.append(f'[name="{field_name}"]')
        if tag:
            candidates.append(f'{tag}[name="{field_name}"]')
    if automation_id:
        candidates.append(f'[data-automation-id="{automation_id}"]')
    return candidates
