import json

import pytest

from job_agent.capmonster import (
    CapMonsterClient,
    CapMonsterConfig,
    CapMonsterError,
    build_complex_image_task,
    build_datadome_task,
    build_funcaptcha_task,
    build_geetest_task,
    build_hcaptcha_task,
    build_recaptcha_v2_task,
    build_recaptcha_v2_enterprise_task,
    build_recaptcha_v3_task,
    build_turnstile_task,
    proxy_settings_from_env,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_capmonster_config_requires_key_to_enable(monkeypatch):
    monkeypatch.delenv("CAPMONSTER_API_KEY", raising=False)
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")

    config = CapMonsterConfig.from_env()

    assert config.enabled is False


def test_capmonster_config_reads_enabled_settings(monkeypatch):
    monkeypatch.setenv("CAPMONSTER_API_KEY", "cap-key")
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "yes")
    monkeypatch.setenv("CAPMONSTER_POLL_INTERVAL_SECONDS", "1.5")
    monkeypatch.setenv("CAPMONSTER_TIMEOUT_SECONDS", "45")

    config = CapMonsterConfig.from_env()

    assert config.enabled is True
    assert config.api_key == "cap-key"
    assert config.poll_interval_seconds == 1.5
    assert config.timeout_seconds == 45


def test_capmonster_task_builders_match_supported_runtime_challenges():
    assert build_recaptcha_v2_task(
        "https://example.com/apply",
        "site-key",
        invisible=True,
        user_agent="Mozilla/5.0",
        cookies="session=abc",
        recaptcha_data_s_value="data-s-token",
    ) == {
        "type": "RecaptchaV2Task",
        "websiteURL": "https://example.com/apply",
        "websiteKey": "site-key",
        "isInvisible": True,
        "userAgent": "Mozilla/5.0",
        "cookies": "session=abc",
        "recaptchaDataSValue": "data-s-token",
    }
    assert build_turnstile_task(
        "https://example.com/apply",
        "turnstile-key",
        user_agent="Mozilla/5.0",
        page_action="apply",
        data="cdata-token",
    ) == {
        "type": "TurnstileTask",
        "websiteURL": "https://example.com/apply",
        "websiteKey": "turnstile-key",
        "userAgent": "Mozilla/5.0",
        "pageAction": "apply",
        "data": "cdata-token",
    }
    assert build_turnstile_task(
        "https://example.com/apply",
        "turnstile-key",
        user_agent="Mozilla/5.0",
        page_action="managed",
        data="cdata-token",
        cloudflare_task_type="cf_clearance",
        page_data="chl-page-data",
        html_page_base64="PGh0bWw+",
        api_js_url="https://challenges.cloudflare.com/turnstile/v0/api.js",
    ) == {
        "type": "TurnstileTask",
        "websiteURL": "https://example.com/apply",
        "websiteKey": "turnstile-key",
        "userAgent": "Mozilla/5.0",
        "pageAction": "managed",
        "data": "cdata-token",
        "cloudflareTaskType": "cf_clearance",
        "pageData": "chl-page-data",
        "htmlPageBase64": "PGh0bWw+",
        "apiJsUrl": "https://challenges.cloudflare.com/turnstile/v0/api.js",
    }
    assert build_hcaptcha_task("https://example.com/apply", "hcaptcha-key", invisible=True) == {
        "type": "HCaptchaTaskProxyless",
        "websiteURL": "https://example.com/apply",
        "websiteKey": "hcaptcha-key",
        "isInvisible": True,
    }
    assert build_recaptcha_v2_enterprise_task(
        "https://example.com/apply",
        "enterprise-key",
        enterprise_payload={"s": "payload-token"},
        invisible=True,
        user_agent="Mozilla/5.0",
    ) == {
        "type": "RecaptchaV2EnterpriseTaskProxyless",
        "websiteURL": "https://example.com/apply",
        "websiteKey": "enterprise-key",
        "enterprisePayload": {"s": "payload-token"},
        "isInvisible": True,
        "userAgent": "Mozilla/5.0",
    }


def test_hcaptcha_task_includes_official_acceptance_fields():
    assert build_hcaptcha_task(
        "https://example.com/apply",
        "hcaptcha-key",
        data="rqdata-value",
        user_agent="Mozilla/5.0",
        cookies="session=abc",
        fallback_to_actual_ua=False,
    ) == {
        "type": "HCaptchaTaskProxyless",
        "websiteURL": "https://example.com/apply",
        "websiteKey": "hcaptcha-key",
        "data": "rqdata-value",
        "userAgent": "Mozilla/5.0",
        "cookies": "session=abc",
        "fallbackToActualUA": False,
    }
    assert build_hcaptcha_task(
        "https://example.com/apply",
        "hcaptcha-key",
        task_type="HCaptchaTask",
    ) == {
        "type": "HCaptchaTask",
        "websiteURL": "https://example.com/apply",
        "websiteKey": "hcaptcha-key",
    }
    assert build_complex_image_task(
        "base64-image",
        "Select every image containing a bus",
        task_class="recaptcha",
        metadata={"Grid": "3x3"},
    ) == {
        "type": "ComplexImageTask",
        "class": "recaptcha",
        "imagesBase64": ["base64-image"],
        "metadata": {"Task": "Select every image containing a bus", "Grid": "3x3"},
    }
    assert build_recaptcha_v3_task(
        "https://example.com/apply",
        "v3-key",
        page_action="apply",
        min_score=0.7,
        enterprise=True,
        user_agent="Mozilla/5.0",
    ) == {
        "type": "RecaptchaV3EnterpriseTask",
        "websiteURL": "https://example.com/apply",
        "websiteKey": "v3-key",
        "pageAction": "apply",
        "minScore": 0.7,
        "userAgent": "Mozilla/5.0",
    }
    assert build_funcaptcha_task(
        "https://example.com/apply",
        "public-key",
        funcaptcha_api_js_subdomain="client-api.arkoselabs.com",
        data={"blob": "blob-token"},
        user_agent="Mozilla/5.0",
    ) == {
        "type": "FunCaptchaTask",
        "websiteURL": "https://example.com/apply",
        "websitePublicKey": "public-key",
        "funcaptchaApiJSSubdomain": "client-api.arkoselabs.com",
        "data": '{"blob": "blob-token"}',
        "userAgent": "Mozilla/5.0",
    }
    assert build_geetest_task("https://example.com/apply", "gt-key", challenge="challenge-token") == {
        "type": "GeeTestTask",
        "websiteURL": "https://example.com/apply",
        "gt": "gt-key",
        "version": 3,
        "challenge": "challenge-token",
    }
    assert build_datadome_task(
        "https://example.com/apply",
        "https://geo.captcha-delivery.com/interstitial/?initialCid=abc",
        "datadome=cookie",
        {"proxyType": "http", "proxyAddress": "127.0.0.1", "proxyPort": 8080},
        user_agent="Mozilla/5.0",
    ) == {
        "type": "CustomTask",
        "class": "DataDome",
        "websiteURL": "https://example.com/apply",
        "metadata": {
            "captchaUrl": "https://geo.captcha-delivery.com/interstitial/?initialCid=abc",
            "datadomeCookie": "datadome=cookie",
            "datadomeVersion": "new",
        },
        "userAgent": "Mozilla/5.0",
        "proxyType": "http",
        "proxyAddress": "127.0.0.1",
        "proxyPort": 8080,
    }


def test_capmonster_proxy_settings_from_env(monkeypatch):
    monkeypatch.setenv("CAPMONSTER_PROXY_TYPE", "http")
    monkeypatch.setenv("CAPMONSTER_PROXY_ADDRESS", "proxy.example.com")
    monkeypatch.setenv("CAPMONSTER_PROXY_PORT", "8080")
    monkeypatch.setenv("CAPMONSTER_PROXY_LOGIN", "user")
    monkeypatch.setenv("CAPMONSTER_PROXY_PASSWORD", "pass")

    assert proxy_settings_from_env(required=True) == {
        "proxyType": "http",
        "proxyAddress": "proxy.example.com",
        "proxyPort": 8080,
        "proxyLogin": "user",
        "proxyPassword": "pass",
    }


def test_capmonster_client_solves_task(monkeypatch):
    calls = []
    responses = [
        {"errorId": 0, "taskId": 123},
        {"errorId": 0, "status": "processing"},
        {"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": "token"}},
    ]

    def fake_urlopen(request, timeout):
        calls.append(
            {
                "url": request.full_url,
                "payload": json.loads(request.data.decode("utf-8")),
                "timeout": timeout,
            }
        )
        return _Response(responses.pop(0))

    monkeypatch.setattr("job_agent.capmonster.urlopen", fake_urlopen)
    monkeypatch.setattr("job_agent.capmonster.time.sleep", lambda _: None)

    solution = CapMonsterClient("cap-key").solve_task(
        build_recaptcha_v2_task("https://example.com", "site-key"),
        timeout_seconds=5,
        poll_interval_seconds=0.25,
    )

    assert solution == {"gRecaptchaResponse": "token"}
    assert calls[0]["url"] == "https://api.capmonster.cloud/createTask"
    assert calls[0]["payload"]["clientKey"] == "cap-key"
    assert calls[0]["payload"]["task"]["type"] == "RecaptchaV2Task"
    assert calls[1]["url"] == "https://api.capmonster.cloud/getTaskResult"
    assert calls[1]["payload"]["taskId"] == 123


def test_capmonster_client_raises_on_api_error(monkeypatch):
    def fake_urlopen(request, timeout):
        return _Response(
            {
                "errorId": 1,
                "errorCode": "ERROR_KEY_DOES_NOT_EXIST",
                "errorDescription": "key not found",
            }
        )

    monkeypatch.setattr("job_agent.capmonster.urlopen", fake_urlopen)

    with pytest.raises(CapMonsterError, match="ERROR_KEY_DOES_NOT_EXIST"):
        CapMonsterClient("bad-key").create_task(build_turnstile_task("https://example.com", "site-key"))
