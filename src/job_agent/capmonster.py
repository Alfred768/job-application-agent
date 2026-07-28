from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


CAPMONSTER_API_BASE = "https://api.capmonster.cloud"


@dataclass(frozen=True)
class CapMonsterConfig:
    api_key: str | None = None
    enabled: bool = False
    poll_interval_seconds: float = 3.0
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "CapMonsterConfig":
        api_key = os.getenv("CAPMONSTER_API_KEY") or None
        enabled = _parse_bool(os.getenv("CAPMONSTER_SOLVE_CAPTCHA"), default=False)
        return cls(
            api_key=api_key,
            enabled=enabled and bool(api_key),
            poll_interval_seconds=_parse_float(os.getenv("CAPMONSTER_POLL_INTERVAL_SECONDS"), default=3.0),
            timeout_seconds=_parse_float(os.getenv("CAPMONSTER_TIMEOUT_SECONDS"), default=120.0),
        )


class CapMonsterError(RuntimeError):
    pass


class CapMonsterClient:
    def __init__(self, api_key: str, api_base: str = CAPMONSTER_API_BASE):
        if not api_key:
            raise ValueError("CapMonster API key is required")
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")

    def create_task(self, task: dict[str, Any]) -> int:
        response = self._post("/createTask", {"clientKey": self.api_key, "task": task})
        task_id = response.get("taskId")
        if not task_id:
            raise CapMonsterError("CapMonster did not return taskId")
        return int(task_id)

    def get_task_result(self, task_id: int) -> dict[str, Any]:
        return self._post("/getTaskResult", {"clientKey": self.api_key, "taskId": task_id})

    def solve_task(
        self,
        task: dict[str, Any],
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 3.0,
    ) -> dict[str, Any]:
        task_id = self.create_task(task)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(max(0.25, poll_interval_seconds))
            result = self.get_task_result(task_id)
            status = result.get("status")
            if status == "ready":
                solution = result.get("solution")
                if not isinstance(solution, dict):
                    raise CapMonsterError("CapMonster ready response did not include a solution")
                return solution
            if status not in {"processing", None}:
                raise CapMonsterError(f"Unexpected CapMonster task status: {status}")
        raise TimeoutError(f"CapMonster task {task_id} was not ready before timeout")

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.api_base}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except URLError as exc:
            raise CapMonsterError(f"CapMonster request failed: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CapMonsterError("CapMonster returned invalid JSON") from exc
        if int(parsed.get("errorId") or 0) != 0:
            code = parsed.get("errorCode") or "UNKNOWN_ERROR"
            description = parsed.get("errorDescription") or "CapMonster request failed"
            raise CapMonsterError(f"{code}: {description}")
        return parsed


def build_recaptcha_v2_task(
    website_url: str,
    website_key: str,
    invisible: bool = False,
    user_agent: str | None = None,
    cookies: str | None = None,
    recaptcha_data_s_value: str | None = None,
    task_type: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": _recaptcha_v2_task_type(task_type),
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if invisible:
        task["isInvisible"] = True
    if user_agent:
        task["userAgent"] = user_agent
    if cookies:
        task["cookies"] = cookies
    if recaptcha_data_s_value:
        task["recaptchaDataSValue"] = recaptcha_data_s_value
    return task


def build_recaptcha_v2_enterprise_task(
    website_url: str,
    website_key: str,
    enterprise_payload: dict[str, Any] | None = None,
    page_action: str | None = None,
    api_domain: str | None = None,
    invisible: bool = False,
    user_agent: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": "RecaptchaV2EnterpriseTaskProxyless",
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if enterprise_payload:
        task["enterprisePayload"] = enterprise_payload
    if page_action:
        task["pageAction"] = page_action
    if api_domain:
        task["apiDomain"] = api_domain
    if invisible:
        task["isInvisible"] = True
    if user_agent:
        task["userAgent"] = user_agent
    return task


def build_recaptcha_v3_task(
    website_url: str,
    website_key: str,
    page_action: str | None = None,
    min_score: float | None = None,
    enterprise: bool = False,
    user_agent: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": "RecaptchaV3EnterpriseTask" if enterprise else "RecaptchaV3TaskProxyless",
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if page_action:
        task["pageAction"] = page_action
    if min_score is not None:
        task["minScore"] = min_score
    if user_agent:
        task["userAgent"] = user_agent
    return task


def build_turnstile_task(
    website_url: str,
    website_key: str,
    user_agent: str | None = None,
    page_action: str | None = None,
    data: str | None = None,
    cloudflare_task_type: str | None = None,
    page_data: str | None = None,
    html_page_base64: str | None = None,
    api_js_url: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": "TurnstileTask",
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if user_agent:
        task["userAgent"] = user_agent
    if page_action:
        task["pageAction"] = page_action
    if data:
        task["data"] = data
    if cloudflare_task_type:
        task["cloudflareTaskType"] = cloudflare_task_type
    if page_data:
        task["pageData"] = page_data
    if html_page_base64:
        task["htmlPageBase64"] = html_page_base64
    if api_js_url:
        task["apiJsUrl"] = api_js_url
    return task


def build_hcaptcha_task(
    website_url: str,
    website_key: str,
    invisible: bool = False,
    data: str | None = None,
    user_agent: str | None = None,
    cookies: str | None = None,
    fallback_to_actual_ua: bool | None = None,
    task_type: str | None = None,
) -> dict[str, Any]:
    selected_task_type = _hcaptcha_task_type(task_type)
    task: dict[str, Any] = {
        "type": selected_task_type,
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if invisible:
        task["isInvisible"] = True
    if data:
        task["data"] = data
    if user_agent:
        task["userAgent"] = user_agent
    if cookies:
        task["cookies"] = cookies
    if fallback_to_actual_ua is not None:
        task["fallbackToActualUA"] = fallback_to_actual_ua
    return task


def build_complex_image_task(
    image_base64: str,
    task_text: str,
    task_class: str = "recognition",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_metadata = {"Task": task_text}
    if metadata:
        task_metadata.update(metadata)
    return {
        "type": "ComplexImageTask",
        "class": task_class,
        "imagesBase64": [image_base64],
        "metadata": task_metadata,
    }


def build_funcaptcha_task(
    website_url: str,
    website_public_key: str,
    funcaptcha_api_js_subdomain: str | None = None,
    data: str | dict[str, Any] | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": "FunCaptchaTask",
        "websiteURL": website_url,
        "websitePublicKey": website_public_key,
    }
    if funcaptcha_api_js_subdomain:
        task["funcaptchaApiJSSubdomain"] = funcaptcha_api_js_subdomain
    if data:
        task["data"] = json.dumps(data) if isinstance(data, dict) else data
    if user_agent:
        task["userAgent"] = user_agent
    return task


def build_geetest_task(
    website_url: str,
    gt: str,
    challenge: str | None = None,
    version: int = 3,
    geetest_api_server_subdomain: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": "GeeTestTask",
        "websiteURL": website_url,
        "gt": gt,
        "version": version,
    }
    if challenge:
        task["challenge"] = challenge
    if geetest_api_server_subdomain:
        task["geetestApiServerSubdomain"] = geetest_api_server_subdomain
    if user_agent:
        task["userAgent"] = user_agent
    return task


def build_datadome_task(
    website_url: str,
    captcha_url: str,
    datadome_cookie: str,
    proxy_settings: dict[str, Any],
    datadome_version: str = "new",
    user_agent: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "type": "CustomTask",
        "class": "DataDome",
        "websiteURL": website_url,
        "metadata": {
            "captchaUrl": captcha_url,
            "datadomeCookie": datadome_cookie,
            "datadomeVersion": datadome_version,
        },
    }
    if user_agent:
        task["userAgent"] = user_agent
    task.update(proxy_settings)
    return task


def proxy_settings_from_env(required: bool = False) -> dict[str, Any] | None:
    proxy_type = os.getenv("CAPMONSTER_PROXY_TYPE") or None
    proxy_address = os.getenv("CAPMONSTER_PROXY_ADDRESS") or None
    proxy_port = _parse_int(os.getenv("CAPMONSTER_PROXY_PORT"), default=0)
    proxy_login = os.getenv("CAPMONSTER_PROXY_LOGIN") or None
    proxy_password = os.getenv("CAPMONSTER_PROXY_PASSWORD") or None
    if not proxy_type and not proxy_address and not proxy_port and not required:
        return None
    if not proxy_type or not proxy_address or not proxy_port:
        return None
    settings: dict[str, Any] = {
        "proxyType": proxy_type,
        "proxyAddress": proxy_address,
        "proxyPort": proxy_port,
    }
    if proxy_login:
        settings["proxyLogin"] = proxy_login
    if proxy_password:
        settings["proxyPassword"] = proxy_password
    return settings


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_int(raw: str | None, default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _hcaptcha_task_type(raw: str | None = None) -> str:
    normalized = (raw or os.getenv("CAPMONSTER_HCAPTCHA_TASK_TYPE") or "").strip()
    if normalized in {"HCaptchaTask", "HCaptchaTaskProxyless"}:
        return normalized
    return "HCaptchaTaskProxyless"


def _recaptcha_v2_task_type(raw: str | None = None) -> str:
    normalized = (raw or os.getenv("CAPMONSTER_RECAPTCHA_V2_TASK_TYPE") or "").strip()
    if normalized in {"RecaptchaV2Task", "RecaptchaV2TaskProxyless", "NoCaptchaTaskProxyless"}:
        return normalized
    return "RecaptchaV2Task"
