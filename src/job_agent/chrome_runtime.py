from __future__ import annotations

import base64
import json
import unicodedata
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from job_agent.python_runtime import load_runtime_payload


def _osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def _execute_chrome_js(js: str, url_contains: str | None = None) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(js)
        js_path = handle.name
    try:
        if url_contains:
            target = json.dumps(url_contains)
            return _osascript(
                f"""set jsSource to read POSIX file {json.dumps(js_path)}
set targetUrl to {target}
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      if (URL of t as text) contains targetUrl then
        tell t
          return execute javascript jsSource
        end tell
      end if
    end repeat
  end repeat
end tell
error "No matching Chrome tab for " & targetUrl
"""
            )
        return _osascript(
            f"""set jsSource to read POSIX file {json.dumps(js_path)}
tell application "Google Chrome"
  tell active tab of front window
    execute javascript jsSource
  end tell
end tell
"""
        )
    finally:
        try:
            Path(js_path).unlink()
        except OSError:
            pass


def _profile_value(profile: dict[str, Any], key: str, default: str = "") -> str:
    value = profile.get(key)
    if value is None:
        return default
    return str(value)


def _ascii_text(value: Any) -> str:
    text = str(value or "")
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def _anthropic_answers(profile: dict[str, Any]) -> dict[str, str]:
    answers = profile.get("answers") or {}
    why = (
        answers.get("Why anthropic?")
        or answers.get("Why do you want to work at anthropic?")
        or answers.get("Why are you interested in this role?")
        or ""
    )
    return {
        "first_name": _profile_value(profile, "first_name"),
        "last_name": _profile_value(profile, "last_name"),
        "email": _profile_value(profile, "email"),
        "country": _profile_value(profile, "country", "United States"),
        "phone": _profile_value(profile, "phone"),
        "question_10020555008": _profile_value(profile, "website"),
        "question_10020569008": _profile_value(profile, "linkedin"),
        "question_10020557008": _profile_value(profile, "github"),
        "question_10020561008": "Within a month",
        "question_10020562008": "No specific deadlines or constraints.",
        "question_10020571008": _profile_value(profile, "address_line1"),
        "question_10020570008": "Yes",
        "question_10020559008": "Yes",
        "question_10020563008": "Yes",
        "question_10020564008": _ascii_text(why),
        "question_10020857008": "Yes",
        "question_10020572008": "No",
        "question_10020568008": _ascii_text(answers.get("Additional Information", "")),
        "gender": "Male",
        "hispanic_ethnicity": "No",
        "veteran_status": "I am not a protected veteran",
        "disability_status": "No, I do not have a disability and have not had one in the past",
    }


def run_chrome_runtime(script_path: str | Path, submit: bool = True, code: str | None = None) -> int:
    payload = load_runtime_payload(script_path)
    profile = payload.get("profile") or {}
    resume_path = Path(payload.get("resumeFile") or "")
    if not resume_path.is_absolute():
        resume_path = Path.cwd() / resume_path
    if not resume_path.is_file():
        raise FileNotFoundError(f"resume PDF not found: {resume_path}")

    answers = _anthropic_answers(profile)
    resume_b64 = base64.b64encode(resume_path.read_bytes()).decode("ascii")
    js_payload = {
        "answers": answers,
        "resume": {
            "id": "resume",
            "name": resume_path.name,
            "mime": "application/pdf",
            "b64": resume_b64,
        },
        "submit": bool(submit),
        "code": code or os.getenv("JOB_AGENT_EMAIL_VERIFICATION_CODE") or "",
    }
    js = _render_js(js_payload)
    target_url = str(payload.get("applicationUrl") or "")
    raw = _execute_chrome_js(js, url_contains=target_url)
    if submit:
        time.sleep(10)
        raw = _execute_chrome_js(_render_state_js(), url_contains=target_url)
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            state = {}
        if state.get("verificationRequired") and js_payload.get("code"):
            raw = _execute_chrome_js(_render_code_submit_js(str(js_payload["code"])), url_contains=target_url)
            time.sleep(10)
            raw = _execute_chrome_js(_render_state_js(), url_contains=target_url)
    print(raw)
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return 1
    if state.get("confirmed"):
        print(f"Submission confirmed: {state.get('confirmation')}")
    elif state.get("verificationRequired"):
        print(f"Email verification required: {state.get('verification')}")
    elif state.get("processingError"):
        print(f"Submission processing error: {state.get('processingError')}")
    elif state.get("submittedClick"):
        print("Submit clicked but confirmation not detected: chrome active tab")
    else:
        print("Submit gate: automatic submission not performed because the final Submit control is unavailable.")
    return 0


def _render_js(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"""(() => {{
  const CFG = {data};
  const signals = [];
  function visible(node) {{
    if (!node) return false;
    const rects = node.getClientRects ? node.getClientRects() : [];
    return !!(node.offsetParent || (rects && rects.length));
  }}
  function nativeSet(el, value) {{
    if (!el) return false;
    el.scrollIntoView({{ block: 'center', inline: 'nearest' }});
    el.focus();
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: String(value) }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    el.blur();
    return true;
  }}
  function byId(id) {{ return document.getElementById(id); }}
  function fill(id, value) {{
    if (!value) return false;
    const ok = nativeSet(byId(id), String(value));
    if (ok) signals.push(['fill', id, String(value).slice(0, 80)]);
    return ok;
  }}
  function setFile(id, fileSpec) {{
    const input = byId(id);
    if (!input) return false;
    const raw = atob(fileSpec.b64);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    const file = new File([bytes], fileSpec.name, {{ type: fileSpec.mime }});
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    signals.push(['file', id, file.name]);
    return true;
  }}
  function submitOnce() {{
    const buttons = Array.from(document.querySelectorAll('button,input[type=submit]')).filter(visible);
    const submit = buttons.reverse().find((b) => /submit\\s+application|submit|apply/i.test((b.innerText || b.value || '').trim()));
    if (!submit) return false;
    submit.scrollIntoView({{ block: 'center', inline: 'nearest' }});
    submit.click();
    signals.push(['click', 'submit', (submit.innerText || submit.value || '').trim()]);
    return true;
  }}
  function bodyText() {{ return (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim(); }}
  function confirmation() {{
    const text = bodyText().toLowerCase();
    const patterns = ['thank you for applying', 'thanks for applying', 'application submitted', 'successfully submitted', 'application received', 'we have received your application', 'received your application'];
    return patterns.find((p) => text.includes(p)) || '';
  }}
  function verification() {{
    const text = bodyText().toLowerCase();
    const patterns = ['security code', 'verification code', 'enter the 8-character code', 'confirm you\\'re a human'];
    return patterns.find((p) => text.includes(p)) || '';
  }}
  function processingError() {{
    const text = bodyText().toLowerCase();
    const patterns = ['there was an error processing your application', 'error processing your application'];
    return patterns.find((p) => text.includes(p)) || '';
  }}
  function fillCodeIfPresent(code) {{
    if (!code) return false;
    const candidates = Array.from(document.querySelectorAll('input,textarea')).filter(visible).filter((el) => {{
      const text = [el.id, el.name, el.placeholder, el.getAttribute('aria-label'), el.autocomplete].join(' ').toLowerCase();
      const around = (el.closest('label,.field,.form-field,.application-question,form') || el).innerText || '';
      return /security\\s+code|verification\\s+code|one[-\\s]?time|code/.test(text + ' ' + around.toLowerCase());
    }});
    const target = candidates[0];
    if (!target) return false;
    return nativeSet(target, code);
  }}
  setFile(CFG.resume.id, CFG.resume);
  for (const [id, value] of Object.entries(CFG.answers)) fill(id, value);
  let submittedClick = false;
  if (CFG.submit) {{
    submittedClick = submitOnce();
  }}
  const text = bodyText();
  const conf = confirmation();
  const ver = verification();
  const err = processingError();
  return JSON.stringify({{
    url: location.href,
    title: document.title,
    filledCount: signals.filter((s) => s[0] === 'fill').length,
    fileSelected: !!(byId('resume') && byId('resume').files && byId('resume').files.length),
    submittedClick,
    confirmed: !!conf,
    confirmation: conf,
    verificationRequired: !!ver && !conf,
    verification: ver,
    processingError: err,
    signals,
    tail: text.slice(-3000)
  }});
}})();"""


def _render_state_js() -> str:
    return """(() => {
  const text = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim();
  const lower = text.toLowerCase();
  const find = (patterns) => patterns.find((p) => lower.includes(p)) || '';
  const confirmation = find(['thank you for applying', 'thanks for applying', 'application submitted', 'successfully submitted', 'application received', 'we have received your application', 'received your application']);
  const verification = find(['security code', 'verification code', 'enter the 8-character code', 'confirm you\\'re a human']);
  const processingError = find(['there was an error processing your application', 'error processing your application']);
  const resume = document.getElementById('resume');
  return JSON.stringify({
    url: location.href,
    title: document.title,
    fileSelected: !!(resume && resume.files && resume.files.length),
    confirmed: !!confirmation,
    confirmation,
    verificationRequired: !!verification && !confirmation,
    verification,
    processingError,
    tail: text.slice(-3000)
  });
})();"""


def _render_code_submit_js(code: str) -> str:
    return f"""(() => {{
  const code = {json.dumps(code)};
  function visible(node) {{
    if (!node) return false;
    const rects = node.getClientRects ? node.getClientRects() : [];
    return !!(node.offsetParent || (rects && rects.length));
  }}
  function nativeSet(el, value) {{
    if (!el) return false;
    el.scrollIntoView({{ block: 'center', inline: 'nearest' }});
    el.focus();
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: String(value) }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return true;
  }}
  const candidates = Array.from(document.querySelectorAll('input,textarea')).filter(visible).filter((el) => {{
    const text = [el.id, el.name, el.placeholder, el.getAttribute('aria-label'), el.autocomplete].join(' ').toLowerCase();
    const around = (el.closest('label,.field,.form-field,.application-question,form') || el).innerText || '';
    return /security\\s+code|verification\\s+code|one[-\\s]?time|code/.test(text + ' ' + around.toLowerCase());
  }});
  const filled = candidates[0] ? nativeSet(candidates[0], code) : false;
  const buttons = Array.from(document.querySelectorAll('button,input[type=submit]')).filter(visible);
  const submit = buttons.reverse().find((b) => /submit\\s+application|submit|apply/i.test((b.innerText || b.value || '').trim()));
  if (submit) submit.click();
  return JSON.stringify({{ filled, clicked: !!submit }});
}})();"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Usage: python -m job_agent.chrome_runtime <autofill-runtime.js> [--no-submit]", file=sys.stderr)
        return 2
    submit = "--no-submit" not in args
    script_args = [arg for arg in args if arg != "--no-submit"]
    return run_chrome_runtime(script_args[0], submit=submit)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
