"""Optional Gmail API support for ATS email verification codes.

The module is deliberately isolated from the browser runtimes. It uses an
OAuth refresh token with the read-only Gmail scope and only inspects messages
newer than the verification request that triggered the lookup.
"""

from __future__ import annotations

import base64
import html
import re
import time
from typing import Any


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GREENHOUSE_SECURITY_CODE_QUERY = 'from:(greenhouse-mail.io) subject:("Security code")'
_CODE_PATTERNS = (
    re.compile(r"(?:security|verification|confirmation)\s+code[^:]{0,120}:\s*([A-Za-z0-9]{6,16})", re.I),
    re.compile(r"(?:application|code)\s*:\s*([A-Za-z0-9]{6,16})", re.I),
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.I)
_APPLICATION_CONFIRMATION_PATTERNS = (
    re.compile(r"thank(?:s| you) for (?:submitting your application|applying)", re.I),
    re.compile(r"your application (?:has been|was) (?:received|submitted)", re.I),
    re.compile(r"we (?:have )?received your application", re.I),
    re.compile(r"application (?:was )?successfully submitted", re.I),
)


class GmailVerificationError(RuntimeError):
    """Raised only for a configured Gmail integration that cannot be used."""


def authorize_gmail(
    client_secret_file: str,
    token_file: str,
    *,
    open_browser: bool = True,
    port: int = 0,
) -> None:
    """Run the one-time local OAuth flow and store a reusable refresh token."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - dependency error is CLI-only
        raise GmailVerificationError(
            "Gmail support requires google-auth-oauthlib; install the gmail extra."
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, [GMAIL_READONLY_SCOPE])
    credentials = flow.run_local_server(port=port, open_browser=open_browser)
    with open(token_file, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())


def find_verification_code(
    token_file: str,
    *,
    requested_after_ms: int,
    query: str = GREENHOUSE_SECURITY_CODE_QUERY,
) -> str | None:
    """Return a verification code from a Gmail message newer than this request."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailVerificationError(
            "Gmail support requires google-api-python-client and google-auth-oauthlib; install the gmail extra."
        ) from exc

    try:
        credentials = Credentials.from_authorized_user_file(token_file, [GMAIL_READONLY_SCOPE])
    except (OSError, ValueError) as exc:
        raise GmailVerificationError(f"Could not read Gmail token file: {exc}") from exc
    if not credentials.valid:
        if not credentials.refresh_token:
            raise GmailVerificationError("Gmail token has no refresh token; run gmail authorize again.")
        try:
            credentials.refresh(Request())
        except Exception as exc:  # Google libraries expose several transport exception types.
            raise GmailVerificationError(f"Could not refresh Gmail token: {exc}") from exc

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    listing = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    fetched_messages: list[dict[str, Any]] = []
    messages = listing.get("messages") or []
    for item in messages:
        message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
        if isinstance(message, dict):
            fetched_messages.append(message)
    return _latest_verification_code(fetched_messages, requested_after_ms=requested_after_ms)


def find_verification_link(
    token_file: str,
    *,
    requested_after_ms: int,
    query: str,
    url_pattern: str = r"workday|myworkdayjobs",
) -> str | None:
    """Return a verification link from a Gmail message newer than this request."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailVerificationError(
            "Gmail support requires google-api-python-client and google-auth-oauthlib; install the gmail extra."
        ) from exc

    try:
        credentials = Credentials.from_authorized_user_file(token_file, [GMAIL_READONLY_SCOPE])
    except (OSError, ValueError) as exc:
        raise GmailVerificationError(f"Could not read Gmail token file: {exc}") from exc
    if not credentials.valid:
        if not credentials.refresh_token:
            raise GmailVerificationError("Gmail token has no refresh token; run gmail authorize again.")
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise GmailVerificationError(f"Could not refresh Gmail token: {exc}") from exc

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    listing = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    fetched_messages: list[dict[str, Any]] = []
    messages = listing.get("messages") or []
    for item in messages:
        message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
        if isinstance(message, dict):
            fetched_messages.append(message)
    return _latest_verification_link(
        fetched_messages,
        requested_after_ms=requested_after_ms,
        url_pattern=url_pattern,
    )


def find_application_confirmation(
    token_file: str,
    *,
    query: str,
    company: str,
    title: str,
) -> dict[str, Any] | None:
    """Return metadata for an exact application-confirmation email.

    Gmail search narrows the mailbox, but the fetched message still has to
    contain the normalized company, exact role title, and deterministic
    confirmation wording.  This prevents a confirmation for another opening
    at the same employer from reconciling the tracked application.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailVerificationError(
            "Gmail support requires google-api-python-client and google-auth-oauthlib; install the gmail extra."
        ) from exc

    try:
        credentials = Credentials.from_authorized_user_file(token_file, [GMAIL_READONLY_SCOPE])
    except (OSError, ValueError) as exc:
        raise GmailVerificationError(f"Could not read Gmail token file: {exc}") from exc
    if not credentials.valid:
        if not credentials.refresh_token:
            raise GmailVerificationError("Gmail token has no refresh token; run gmail authorize again.")
        try:
            credentials.refresh(Request())
        except Exception as exc:
            raise GmailVerificationError(f"Could not refresh Gmail token: {exc}") from exc

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    listing = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    fetched_messages: list[dict[str, Any]] = []
    for item in listing.get("messages") or []:
        message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
        if isinstance(message, dict):
            fetched_messages.append(message)
    return _latest_application_confirmation(
        fetched_messages,
        company=company,
        title=title,
    )


def _latest_verification_code(messages: list[dict[str, Any]], *, requested_after_ms: int) -> str | None:
    """Select the newest valid code, without relying on Gmail list ordering."""
    candidates: list[tuple[int, str]] = []
    for message in messages:
        try:
            received_at_ms = int(message.get("internalDate") or 0)
        except (TypeError, ValueError):
            received_at_ms = 0
        if received_at_ms < requested_after_ms:
            continue
        code = _code_from_payload(message.get("payload") or {})
        if code:
            candidates.append((received_at_ms, code))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _latest_verification_link(
    messages: list[dict[str, Any]],
    *,
    requested_after_ms: int,
    url_pattern: str = r"workday|myworkdayjobs",
) -> str | None:
    candidates: list[tuple[int, str]] = []
    matcher = re.compile(url_pattern, re.I)
    for message in messages:
        try:
            received_at_ms = int(message.get("internalDate") or 0)
        except (TypeError, ValueError):
            received_at_ms = 0
        if received_at_ms < requested_after_ms:
            continue
        link = _verification_link_from_payload(message.get("payload") or {}, url_pattern=matcher)
        if link:
            candidates.append((received_at_ms, link))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _latest_application_confirmation(
    messages: list[dict[str, Any]],
    *,
    company: str,
    title: str,
) -> dict[str, Any] | None:
    company_norm = _confirmation_norm(company)
    title_norm = _confirmation_norm(title)
    if not company_norm or not title_norm:
        return None
    candidates: list[tuple[int, dict[str, Any]]] = []
    for message in messages:
        payload = message.get("payload") or {}
        headers = payload.get("headers") or []
        header_text = " ".join(
            str(item.get("value") or "")
            for item in headers
            if isinstance(item, dict)
            and str(item.get("name") or "").casefold() in {"subject", "from"}
        )
        text = " ".join(
            [
                header_text,
                str(message.get("snippet") or ""),
                *(_html_to_text(value) for value in _payload_texts(payload)),
            ]
        )
        normalized = _confirmation_norm(text)
        if company_norm not in normalized or title_norm not in normalized:
            continue
        if not any(pattern.search(text) for pattern in _APPLICATION_CONFIRMATION_PATTERNS):
            continue
        try:
            received_at_ms = int(message.get("internalDate") or 0)
        except (TypeError, ValueError):
            received_at_ms = 0
        candidates.append(
            (
                received_at_ms,
                {
                    "message_id": str(message.get("id") or ""),
                    "received_at_ms": received_at_ms,
                },
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _confirmation_norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _code_from_payload(payload: dict[str, Any]) -> str | None:
    for text in _payload_texts(payload):
        for candidate in (text, _html_to_text(text)):
            for pattern in _CODE_PATTERNS:
                match = pattern.search(candidate)
                if match:
                    return match.group(1)
    return None


def _verification_link_from_payload(payload: dict[str, Any], *, url_pattern: re.Pattern[str]) -> str | None:
    for text in _payload_texts(payload):
        for candidate in (text, html.unescape(text), _html_to_text(text)):
            for raw_url in _URL_PATTERN.findall(candidate):
                cleaned = html.unescape(raw_url).rstrip(").,;]")
                if matcher := url_pattern.search(cleaned):
                    return cleaned
    return None


def _html_to_text(text: str) -> str:
    """Expose verification text split by HTML tags such as ``<p>`` and ``<h1>``."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text))).strip()


def _payload_texts(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    body = payload.get("body") or {}
    encoded = body.get("data")
    if isinstance(encoded, str) and encoded:
        try:
            texts.append(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            pass
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            texts.extend(_payload_texts(part))
    return texts


def wait_for_verification_code(
    token_file: str,
    *,
    requested_after_ms: int,
    wait_seconds: float,
    query: str,
    poll_seconds: float = 2.0,
) -> str | None:
    """Poll Gmail until the request-specific code arrives or the timeout expires."""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        code = find_verification_code(
            token_file,
            requested_after_ms=requested_after_ms,
            query=query,
        )
        if code:
            return code
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.1, poll_seconds))


def wait_for_verification_link(
    token_file: str,
    *,
    requested_after_ms: int,
    wait_seconds: float,
    query: str,
    url_pattern: str = r"workday|myworkdayjobs",
    poll_seconds: float = 2.0,
) -> str | None:
    """Poll Gmail until a request-specific verification link arrives or timeout expires."""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        link = find_verification_link(
            token_file,
            requested_after_ms=requested_after_ms,
            query=query,
            url_pattern=url_pattern,
        )
        if link:
            return link
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.1, poll_seconds))
