import base64

from job_agent.gmail_verification import (
    _code_from_payload,
    _latest_application_confirmation,
    _latest_verification_code,
    _latest_verification_link,
)


def test_extracts_verification_code_from_gmail_payload():
    message = "Hi, copy and paste this code into the security code field: BdtKEPwT"
    payload = {
        "body": {
            "data": base64.urlsafe_b64encode(message.encode()).decode().rstrip("="),
        }
    }

    assert _code_from_payload(payload) == "BdtKEPwT"


def test_extracts_code_from_nested_html_payload():
    message = "Your application: HADuBZ3J"
    payload = {
        "parts": [
            {
                "mimeType": "text/html",
                "body": {
                    "data": base64.urlsafe_b64encode(message.encode()).decode().rstrip("="),
                },
            }
        ]
    }

    assert _code_from_payload(payload) == "HADuBZ3J"


def test_extracts_greenhouse_code_split_across_html_elements():
    message = (
        "<p>Copy and paste this code into the security code field on your application:</p>"
        "<h1>8jmDVPeT</h1>"
    )
    payload = {
        "parts": [
            {
                "mimeType": "text/html",
                "body": {
                    "data": base64.urlsafe_b64encode(message.encode()).decode().rstrip("="),
                },
            }
        ]
    }

    assert _code_from_payload(payload) == "8jmDVPeT"


def test_selects_newest_code_after_the_verification_request():
    def message(received_at_ms, code):
        text = f"Copy this code into the security code field: {code}"
        return {
            "internalDate": str(received_at_ms),
            "payload": {
                "body": {
                    "data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("="),
                }
            },
        }

    assert _latest_verification_code(
        [message(500, "OLDcode1"), message(1_200, "newCode2"), message(900, "midCode3")],
        requested_after_ms=700,
    ) == "newCode2"


def test_selects_newest_workday_verification_link_after_request():
    def message(received_at_ms, url):
        text = f'<a href="{url}">Verify Account</a>'
        return {
            "internalDate": str(received_at_ms),
            "payload": {
                "body": {
                    "data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("="),
                }
            },
        }

    assert _latest_verification_link(
        [
            message(500, "https://old.myworkdayjobs.com/verify?token=old"),
            message(1_200, "https://wd1.myworkday.com/verify?token=new"),
            message(900, "https://example.com/not-workday"),
        ],
        requested_after_ms=700,
    ) == "https://wd1.myworkday.com/verify?token=new"


def test_application_confirmation_requires_exact_company_title_and_confirmation_copy():
    def message(message_id, received_at_ms, subject, body):
        return {
            "id": message_id,
            "internalDate": str(received_at_ms),
            "snippet": body,
            "payload": {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": "talent@example.com"},
                ],
                "body": {
                    "data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("="),
                },
            },
        }

    messages = [
        message(
            "wrong-role",
            2000,
            "Point72 Employment Application - thanks!",
            "Thanks for submitting your application for Data Engineer.",
        ),
        message(
            "exact-role",
            1000,
            "Point72 Employment Application - thanks!",
            "Thanks for submitting your application for Quantitative Researcher - Machine Learning.",
        ),
        message(
            "not-confirmed",
            3000,
            "Point72 role alert",
            "Quantitative Researcher - Machine Learning is still open.",
        ),
    ]

    assert _latest_application_confirmation(
        messages,
        company="Point72",
        title="Quantitative Researcher - Machine Learning",
    ) == {"message_id": "exact-role", "received_at_ms": 1000}
