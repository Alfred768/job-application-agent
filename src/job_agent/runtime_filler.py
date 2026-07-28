"""Simplify-style runtime application autofill engine.

Instead of generating a brittle, per-snapshot Playwright script that fills by
exact label, this module emits a single *generic* runtime autofill script that
mirrors how the Simplify Copilot extension works:

1. Open the application page in a real browser.
2. Live-scrape every form control on the current page (text, email, tel,
   textarea, select, radio group, checkbox, file upload).
3. Map each control to the saved profile using fuzzy label matching + a
   screening-question answer bank.
4. Fill low-risk fields automatically; leave sensitive fields for review
   unless an approved answer exists.
5. Detect a "Next"/"Continue" button and advance through multi-page
   applications, filling each page.
6. Upload the selected original PDF on Resume/CV file fields.
7. Click the final Submit button by default only when no fields remain
   review-required. Set JOB_AGENT_SUBMIT_COMPLETE=0 to stop before Submit.

The generated script is self-contained Node.js + Playwright.
"""

from __future__ import annotations

import json
from typing import Any

from job_agent.ats_adapters import runtime_ats_adapters
from job_agent.field_semantics import runtime_autocomplete_semantics, runtime_semantic_rules


def render_runtime_autofill_script(
    profile: dict[str, Any],
    resume_file: str | None = None,
    resume_source_dir: str | None = None,
    required_resume_pdf: str | None = None,
    cover_letter_file: str | None = None,
    application_url: str | None = None,
    max_pages: int = 12,
    headless: bool = True,
) -> str:
    """Render a generic runtime autofill script for the given profile.

    Args:
        profile: approved profile facts (name, email, phone, links, location,
            cover_letter, education, work_history, and ``answers`` bank).
        resume_file: path to the selected original PDF for Resume/CV uploads.
        resume_source_dir: optional directory that the selected PDF must come from.
        required_resume_pdf: optional exact PDF path that must be uploaded.
        cover_letter_file: path to a generated cover letter for required
            Cover Letter uploads.
        application_url: application page URL to open.
        max_pages: safety cap on multi-page navigation.
        headless: run headless when True (CI/verification); False for manual use.
    """
    payload = {
        "profile": profile,
        "resumeFile": resume_file,
        "resumeSourceDir": resume_source_dir,
        "requiredResumePdf": required_resume_pdf,
        "coverLetterFile": cover_letter_file or profile.get("cover_letter_file"),
        "applicationUrl": application_url,
        "maxPages": max_pages,
        "headless": headless,
        "fieldSemantics": runtime_semantic_rules(),
        "fieldAutocompleteSemantics": runtime_autocomplete_semantics(),
        "atsAdapters": runtime_ats_adapters(),
    }
    return _TEMPLATE.replace("__AUTOFILL_PAYLOAD__", json.dumps(payload, ensure_ascii=False))


_TEMPLATE = r"""const { chromium } = require("playwright");
const crypto = require("crypto");
const fs = require("fs");
const https = require("https");
const path = require("path");
const CFG = __AUTOFILL_PAYLOAD__;
const CAPTCHA_RECOVERY_ATTEMPTS = 1;

const SENSITIVE = [
  "sponsor", "sponsorship", "visa", "authorization", "authorized",
  "disability", "veteran", "gender", "ethnicity", "hispanic", "latino", "race", "salary",
  "relocation", "start date", "legal", "attestation", "eeo", "demographic",
  "clearance", "citizen", "certify", "arbitration", "acknowledgement", "acknowledgment",
  "consent", "privacy", "personal data", "confirm the statement", "notetaker", "notetakers", "transcribe",
  "true and accurate", "false or misleading", "terms and conditions",
];

const NEXT_PATTERNS = /^\s*(next|continue|save\s+and\s+continue|create\s+account|sign\s+up|sign\s+in|->|→|step\s|\d+\s*\/\s*\d+|forward|\u4e0b\u4e00\u6b65|\u7ee7\u7eed|\u4fdd\u5b58\u5e76\u7ee7\u7eed)/i;
const SUBMIT_PATTERNS = /(submit|apply|send\s+application|complete\s+application|finish|submit\s+application|\u63d0\u4ea4(?:\u7533\u8bf7)?|\u5b8c\u6210\u7533\u8bf7)/i;
const SUBMITTED_LINE_PREFIX = "Submission confirmed:";
const SUBMIT_CLICKED_UNCONFIRMED_LINE_PREFIX = "Submit clicked but confirmation not detected:";
const EMAIL_VERIFICATION_REQUIRED_LINE_PREFIX = "Email verification required:";
const SUBMISSION_PROCESSING_ERROR_LINE_PREFIX = "Submission processing error:";
const CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX = "Candidate account required:";
const APPLICATION_FORM_UNAVAILABLE_LINE_PREFIX = "Application form unavailable:";
const CANDIDATE_ACCOUNT_PASSWORD_STORE_FILENAME = ".job-agent-candidate-passwords.json";
const CANDIDATE_ACCOUNT_PASSWORD_LENGTH = 20;
const CANDIDATE_ACCOUNT_PASSWORD_SPECIALS = "!@#$%^*_-";
const DEFAULT_BROWSER_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

function norm(s) {
  return String(s || "")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function parseViewport(raw) {
  const match = String(raw || "").trim().toLowerCase().match(/^(\d{3,5})\s*[x,]\s*(\d{3,5})$/);
  if (!match) return { width: 1365, height: 900 };
  return {
    width: Math.min(3840, Math.max(800, Number(match[1]))),
    height: Math.min(2160, Math.max(600, Number(match[2]))),
  };
}

function browserContextOptions() {
  const locale = String(process.env.JOB_AGENT_BROWSER_LOCALE || "en-US").trim() || "en-US";
  return {
    userAgent: String(process.env.JOB_AGENT_BROWSER_USER_AGENT || DEFAULT_BROWSER_USER_AGENT).trim(),
    locale,
    timezoneId: String(process.env.JOB_AGENT_BROWSER_TIMEZONE || "America/New_York").trim() || "America/New_York",
    viewport: parseViewport(process.env.JOB_AGENT_BROWSER_VIEWPORT),
    deviceScaleFactor: 1,
    colorScheme: "light",
    extraHTTPHeaders: { "Accept-Language": `${locale},en;q=0.9` },
  };
}

async function installBrowserFingerprintMitigation(context) {
  try {
    await context.addInitScript(() => {
      try {
        Object.defineProperty(Navigator.prototype, "webdriver", {
          configurable: true,
          get: () => undefined,
        });
      } catch (e) {}
      try {
        Object.defineProperty(navigator, "languages", {
          configurable: true,
          get: () => ["en-US", "en"],
        });
      } catch (e) {}
      try {
        Object.defineProperty(navigator, "plugins", {
          configurable: true,
          get: () => [1, 2, 3, 4, 5],
        });
      } catch (e) {}
      try {
        window.chrome = window.chrome || {};
        window.chrome.runtime = window.chrome.runtime || {};
      } catch (e) {}
    });
  } catch (_) {}
}

function humanSubmitDelayMs() {
  const raw = String(process.env.JOB_AGENT_SUBMIT_HUMAN_DELAY_SECONDS || "1.5-4.0").trim().toLowerCase();
  if (["0", "false", "no", "off"].includes(raw)) return 0;
  const range = raw.match(/^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$/);
  let low;
  let high;
  if (range) {
    low = Number(range[1]);
    high = Number(range[2]);
    if (high < low) [low, high] = [high, low];
  } else {
    const fixed = Number(raw);
    low = Number.isFinite(fixed) ? fixed : 1.5;
    high = Number.isFinite(fixed) ? fixed : 4.0;
  }
  const lowMs = Math.max(0, Math.floor(low * 1000));
  const highMs = Math.max(lowMs, Math.floor(high * 1000));
  if (highMs === lowMs) return lowMs;
  return lowMs + crypto.randomInt(highMs - lowMs + 1);
}

async function waitBeforeSubmit(page) {
  const delay = humanSubmitDelayMs();
  if (!delay) return;
  if (page.mouse && typeof page.mouse.move === "function") {
    await page.mouse.move(120 + crypto.randomInt(180), 160 + crypto.randomInt(220)).catch(() => {});
  }
  await page.waitForTimeout(delay).catch(() => {});
}

function runtimeApplicationUrl(applicationUrl) {
  const raw = String(applicationUrl || "").trim();
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    if (parsed.hostname.toLowerCase().endsWith("coinbase.com") && parsed.pathname.includes("/careers/positions/")) {
      const queryToken = parsed.searchParams.get("gh_jid");
      const pathMatch = parsed.pathname.match(/\/positions\/(\d+)/);
      const token = queryToken || (pathMatch && pathMatch[1]);
      if (token) return `https://job-boards.greenhouse.io/embed/job_app?for=coinbase&token=${token}`;
    }
    if (parsed.hostname.toLowerCase().endsWith("c3.ai") && parsed.pathname.includes("/job-description/")) {
      const queryToken = parsed.searchParams.get("gh_jid");
      const pathMatch = parsed.pathname.match(/\/job-description\/(\d+)/);
      const token = queryToken || (pathMatch && pathMatch[1]);
      if (token) return `https://job-boards.greenhouse.io/embed/job_app?for=c3iot&token=${token}`;
    }
    if (parsed.hostname.toLowerCase().endsWith("samsara.com") && parsed.pathname.includes("/company/careers/roles/")) {
      const queryToken = parsed.searchParams.get("gh_jid");
      const pathMatch = parsed.pathname.match(/\/roles\/(\d+)/);
      const token = queryToken || (pathMatch && pathMatch[1]);
      if (token) return `https://job-boards.greenhouse.io/embed/job_app?for=samsara&token=${token}`;
    }
    if (parsed.hostname.toLowerCase().endsWith("pinterestcareers.com") && parsed.pathname.replace(/\/$/, "") === "/jobs") {
      const token = parsed.searchParams.get("gh_jid");
      if (token) return `https://job-boards.greenhouse.io/embed/job_app?for=pinterest&token=${token}`;
    }
  } catch (_) {
    return raw;
  }
  return raw;
}

function hasWholePhrase(text, phrase) {
  const normalizedPhrase = norm(phrase);
  return Boolean(normalizedPhrase) && (` ${norm(text)} `).includes(` ${normalizedPhrase} `);
}

function formatLocalDate(value) {
  const pad = (part) => String(part).padStart(2, "0");
  return `${pad(value.getMonth() + 1)}/${pad(value.getDate())}/${value.getFullYear()}`;
}

function dateTarget(value) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  const iso = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (iso) return new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));
  const us = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (us) return new Date(Number(us[3]), Number(us[1]) - 1, Number(us[2]));
  const parsed = new Date(raw);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatNativeDate(value) {
  const pad = (part) => String(part).padStart(2, "0");
  return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
}

async function normalizeDateInputValue(locator, value) {
  const raw = String(value || "").trim();
  const placeholder = norm(
    typeof locator.getAttribute === "function"
      ? await locator.getAttribute("placeholder").catch(() => "")
      : ""
  );
  const inputType = norm(
    typeof locator.getAttribute === "function"
      ? await locator.getAttribute("type").catch(() => "")
      : ""
  );
  const nativeDate = inputType === "date";
  const dateLikePlaceholder = placeholder.includes("date") || /\b(?:mm|dd|yyyy)\b/.test(placeholder);
  if (!nativeDate && !dateLikePlaceholder) return raw;
  const normalized = norm(raw);
  const now = new Date();
  now.setHours(12, 0, 0, 0);
  const format = (target) => nativeDate ? formatNativeDate(target) : formatLocalDate(target);
  if (["within a month", "in one month", "one month"].includes(normalized)) {
    const year = now.getFullYear() + (now.getMonth() === 11 ? 1 : 0);
    const month = (now.getMonth() + 1) % 12;
    const lastDay = new Date(year, month + 1, 0).getDate();
    return format(new Date(year, month, Math.min(now.getDate(), lastDay)));
  }
  if (["within two weeks", "in two weeks", "two weeks"].includes(normalized)) {
    now.setDate(now.getDate() + 14);
    return format(now);
  }
  if (["immediately", "as soon as possible", "asap"].includes(normalized)) {
    return format(now);
  }
  return raw;
}

const PLACEHOLDER_ANSWERS = new Set(["needs review", "n/a", "tbd", "na", ""]);
const STOPWORDS = new Set(["a", "an", "and", "are", "be", "do", "for", "i", "in", "is", "of", "or", "the", "to", "you", "your"]);
const SENSITIVE_STEMS = new Set([
  "acknowledg", "arbitrat", "attest", "authoriz", "background", "certif", "citizen", "compensat",
  "consent", "disab", "eligib", "ethnic", "gender", "hispanic", "latino", "legal", "notetak",
  "pay", "privacy", "race", "relocat", "salary", "sex", "sponsor", "transcrib", "veteran", "visa", "work",
]);
const SINGLE_TOKEN_MATCH_STEMS = new Set([...SENSITIVE_STEMS].filter((stem) => !["eligib", "legal", "work"].includes(stem)));

function stemToken(token) {
  const t = String(token || "").toLowerCase();
  const prefixes = [
    ["authoriz", "authoriz"],
    ["sponsor", "sponsor"],
    ["relocat", "relocat"],
    ["compensat", "compensat"],
    ["eligib", "eligib"],
    ["certif", "certif"],
    ["attest", "attest"],
    ["disab", "disab"],
    ["ethnic", "ethnic"],
  ];
  for (const [prefix, stem] of prefixes) {
    if (t.startsWith(prefix)) return stem;
  }
  for (const suffix of ["ation", "ions", "ing", "ed", "es", "s"]) {
    if (t.length > suffix.length + 3 && t.endsWith(suffix)) return t.slice(0, -suffix.length);
  }
  return t;
}

function meaningfulTokens(text) {
  const countryTokens = new Set(["u", "s", "us", "usa", "united", "states"]);
  return new Set(norm(text).split(" ").filter((t) => t && !STOPWORDS.has(t) && !countryTokens.has(t)).map(stemToken));
}

function isLowRiskIdentityLabel(label) {
  const tokens = meaningfulTokens(label);
  if (!tokens.has("legal")) return false;
  const hasNameToken = tokens.has("name") || tokens.has("first") || tokens.has("last") || tokens.has("middle");
  if (!hasNameToken) return false;
  const sensitiveContext = ["attest", "authoriz", "background", "certif", "sponsor", "visa", "salary", "pay"];
  return !sensitiveContext.some((token) => tokens.has(token));
}

function isSensitive(label) {
  const n = norm(label);
  if (isLowRiskIdentityLabel(n)) return false;
  return SENSITIVE.some((kw) => n.includes(norm(kw)));
}

function isSubset(left, right) {
  for (const item of left) {
    if (!right.has(item)) return false;
  }
  return true;
}

function countryMarkers(text) {
  const n = " " + norm(text) + " ";
  const markers = new Set();
  if ([" united states ", " usa ", " u s a ", " u s ", " us "].some((token) => n.includes(token))) markers.add("us");
  if (n.includes(" canada ") || n.includes(" canadian ")) markers.add("canada");
  if ([" united kingdom ", " uk ", " u k ", " british ", " britain "].some((token) => n.includes(token))) markers.add("uk");
  return markers;
}

function sensitivePatternMatches(label, pattern) {
  const ln = norm(label);
  const pn = norm(pattern);
  if (!ln || !pn) return false;
  const lc = countryMarkers(ln);
  const pc = countryMarkers(pn);
  if (pc.size) {
    if (!lc.size) return false;
    if (![...pc].some((country) => lc.has(country))) return false;
  }
  if (ln.includes(pn) || pn.includes(ln)) return true;
  const lt = meaningfulTokens(ln);
  const pt = meaningfulTokens(pn);
  if (!lt.size || !pt.size) return false;
  for (const exclusiveStem of ["citizen", "sponsor"]) {
    if (pt.has(exclusiveStem) && !lt.has(exclusiveStem)) return false;
  }
  const visaTypeTokens = new Set(["opt", "h1b", "tn"]);
  if ([...pt].some((token) => visaTypeTokens.has(token)) && ![...lt].some((token) => visaTypeTokens.has(token))) return false;
  if (isSubset(pt, lt) || isSubset(lt, pt)) return true;
  const common = [...lt].filter((token) => pt.has(token));
  if (common.length >= 2) {
    if (pt.has("top") && !lt.has("top")) return false;
    return common.some((token) => SENSITIVE_STEMS.has(token));
  }
  return common.some((token) => SINGLE_TOKEN_MATCH_STEMS.has(token));
}

// Fuzzy match a field label against the saved answer bank.
function findAnswer(label, answers) {
  if (!answers) return null;
  const ln = norm(label);
  let best = null;
  let bestScore = 0;
  for (const [key, value] of Object.entries(answers)) {
    const kn = norm(key);
    if (!kn || !ln) continue;
    // never auto-fill placeholder answers like "Needs review"
    if (PLACEHOLDER_ANSWERS.has(norm(String(value)))) continue;
    let score = 0;
    if (ln === kn) score = 1;
    else if (ln.includes(kn) || kn.includes(ln)) score = 0.8;
    else {
      const lt = new Set(ln.split(" ").filter(Boolean));
      const kt = new Set(kn.split(" ").filter(Boolean));
      let common = 0;
      kt.forEach((t) => { if (lt.has(t)) common++; });
      score = Math.min(common / Math.max(1, kt.size), common / Math.max(1, lt.size));
    }
    if (score > bestScore) { bestScore = score; best = value; }
  }
  return bestScore >= 0.6 ? best : null;
}

function requiresUserAuthoredAnswer(label, profile) {
  const combined = [label, profile.target_company, profile.target_title].filter(Boolean).join(" ");
  const n = norm(combined);
  const blocked = [
    "do not use llm",
    "do not use llms",
    "do not use ai",
    "without llm assistance",
    "without ai assistance",
    "without using ai",
  ];
  if (blocked.some((phrase) => n.includes(phrase))) return true;
  return !!profile.application_requires_user_authored_answers && ["why", "essay", "written", "answer", "question"].some((token) => n.includes(token));
}

function sourceUrlLooksLikeCompanyCareersSite(sourceUrl, company) {
  const rawUrl = String(sourceUrl || "").trim();
  const rawCompany = String(company || "").trim();
  if (!rawUrl || !rawCompany) return false;
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch (_) {
    return false;
  }
  const domain = norm(parsed.hostname || "");
  if (!domain || (!domain.includes("career") && !domain.includes("job"))) return false;
  const tokens = norm(rawCompany).split(" ").filter((token) => token.length >= 4);
  if (!tokens.length) return false;
  const requiredHits = tokens.length === 1 ? 1 : 2;
  return tokens.filter((token) => domain.includes(token)).length >= requiredHits;
}

function preferredSourceAnswer(label, profile, answers) {
  const savedSource = profile.application_source || profile.source_of_application ||
    findAnswer(label, answers) || answers["How did you hear about this opportunity?"] ||
    answers["How did you hear about us?"];
  if (!savedSource) return null;
  const answer = String(savedSource);
  if (answer.includes(">")) return answer;
  const company = String(profile.target_company || "").trim();
  if (["xai", "spacexai"].includes(norm(company)) && isCompanyWebsiteAnswer(answer)) {
    return "Company careers page / website";
  }
  const sourceKind = norm(profile.application_source_kind || profile.job_source || profile.source || "");
  const sourceUrl = profile.application_source_url || profile.job_source_url || profile.source_url || "";
  if (
    company &&
    isCompanyWebsiteAnswer(answer) &&
    (sourceKind.includes("official careers") || sourceUrlLooksLikeCompanyCareersSite(sourceUrl, company))
  ) {
    return `Job Board > ${company} Job Board`;
  }
  const sourceUrlLower = String(sourceUrl || "").toLowerCase();
  if (company && (sourceUrlLower.includes("myworkdayjobs.com") || sourceUrlLower.includes("workdayjobs.com"))) {
    return "Career Website";
  }
  return answer;
}

function isWorkdayApplicationUrl(profile) {
  const applicationUrl = String(CFG.applicationUrl || profile._application_url || "").toLowerCase();
  const sourceUrl = String(profile.application_source_url || profile.job_source_url || profile.source_url || "").toLowerCase();
  const sourceKind = norm(profile.application_source_kind || profile.job_source || profile.source || "");
  return applicationUrl.includes("myworkdayjobs.com") ||
    applicationUrl.includes("workdayjobs.com") ||
    sourceUrl.includes("myworkdayjobs.com") ||
    sourceUrl.includes("workdayjobs.com") ||
    sourceKind === "workday";
}

function isPhoneDeviceTypeField(fieldOrLabel) {
  const text = fieldOrLabel && typeof fieldOrLabel === "object"
    ? [fieldOrLabel.label, fieldOrLabel.id, fieldOrLabel.name, fieldOrLabel.ariaLabel, fieldOrLabel.automationId].filter(Boolean).join(" ")
    : String(fieldOrLabel || "");
  const n = norm(text.replace(/-/g, " "));
  return n.includes("phone device type") || n.includes("phone type") || n.includes("phone device");
}

function workdayPhoneDeviceTypeAnswer(fieldOrLabel, profile) {
  if (!isWorkdayApplicationUrl(profile) || !isPhoneDeviceTypeField(fieldOrLabel)) return null;
  const options = fieldOrLabel && typeof fieldOrLabel === "object" && Array.isArray(fieldOrLabel.options)
    ? fieldOrLabel.options.map((option) => norm(optionText(option)))
    : [];
  if (options.includes("primary")) return "Primary";
  return "Primary";
}

function profileEvidenceText(profile) {
  return norm(JSON.stringify({
    summary: profile.summary,
    skills: profile.skills,
    projects: profile.projects,
    work_history: profile.work_history,
    answers: profile.answers,
  }));
}

function hasProfileEvidence(profileText, ...needles) {
  return needles.some((needle) => profileText.includes(norm(needle)));
}

function matchScreeningRule(label, rules) {
  const n = norm(label);
  if (!n || !Array.isArray(rules)) return null;
  for (const rule of rules) {
    if (!rule || typeof rule !== "object") continue;
    const patterns = Array.isArray(rule.patterns) ? rule.patterns : [];
    if (patterns.some((pattern) => {
      const p = norm(pattern);
      return p && n.includes(p);
    })) {
      return rule.answer != null ? String(rule.answer) : null;
    }
  }
  return null;
}

function productionScreeningAnswer(label, profile) {
  const n = norm(label);
  if (!(
    n.includes("able to work onsite") ||
    n.includes("built and deployed") ||
    n.includes("automatically optimizes decisions") ||
    n.includes("advertising systems") ||
    n.includes("multi gpu cluster") ||
    n.includes("data pipelines for llm post training") ||
    n.includes("rl training on an llm") ||
    n.includes("production backend services") ||
    n.includes("shipped ml ai models") ||
    n.includes("directly impacted business metrics")
  )) return null;
  const answers = profile.answers || {};
  const saved = findAnswer(label, answers);
  if (saved != null) return String(saved);
  const profileText = profileEvidenceText(profile);
  if (n.includes("able to work onsite") && n.includes("office") && (n.includes("mountain view") || n.includes("bay area") || n.includes("5 days"))) {
    const relocation = answers["Are you open to relocation?"]
      || answers["Are you open to working in-person in one of our offices 25% of the time?"]
      || approvedSensitiveEntryAnswer(profile, "relocation")
      || matchSensitive("relocation");
    return truthyAnswer(relocation) ? "Yes" : null;
  }
  if (n.includes("built and deployed") && n.includes("production system") && n.includes("llm") && (n.includes("rag") || n.includes("tool use") || n.includes("agent"))) {
    const hasLlmSystem = hasProfileEvidence(profileText, "llm", "rag", "langchain", "agent");
    const hasDeployment = hasProfileEvidence(profileText, "kubernetes", "deployed", "dockerized", "production");
    return hasLlmSystem && hasDeployment ? "Yes" : "No";
  }
  if (n.includes("automatically optimizes decisions") && n.includes("feedback signals")) {
    return hasProfileEvidence(profileText, "automated retraining", "drift detection", "feedback scoring") ? "Yes" : "No";
  }
  if (n.includes("advertising systems") && n.includes("recommendation systems") && (n.includes("ranking") || n.includes("optimization systems"))) {
    return hasProfileEvidence(profileText, "advertising", "recommendation system", "ranking") ? "Yes" : "No";
  }
  if (n.includes("multi gpu cluster") && n.includes("llm")) {
    return hasProfileEvidence(profileText, "multi-gpu", "multi gpu", "8+ gpu", "deepspeed", "fsdp") ? "Yes" : "No";
  }
  if (n.includes("data pipelines for llm post training")) {
    return hasProfileEvidence(
      profileText,
      "fine-tuning pipelines",
      "post-training",
      "preference pairs",
      "reward signals",
      "scheduled retraining",
      "edge-data ingestion"
    ) ? "Yes" : "No";
  }
  if (n.includes("rl training on an llm") || (n.includes("personally run") && n.includes("ppo") && n.includes("grpo"))) {
    return hasProfileEvidence(profileText, "rlhf", "ppo", "grpo", "dpo", "reinforcement learning") ? "Yes" : "No";
  }
  if (n.includes("production backend services") && (n.includes("apis") || n.includes("async systems") || n.includes("distributed components"))) {
    const hasService = hasProfileEvidence(profileText, "rest microservice", "api", "fastapi", "distributed", "kafka");
    const hasProduction = hasProfileEvidence(profileText, "deployed", "dockerized", "production");
    return hasService && hasProduction ? "Yes" : "No";
  }
  if (n.includes("shipped ml ai models") && n.includes("production traffic")) {
    const hasModel = hasProfileEvidence(profileText, "xgboost", "transformer", "model", "ml");
    const hasDeployment = hasProfileEvidence(profileText, "deployed", "dockerized", "productionizing", "production");
    return hasModel && hasDeployment ? "Yes" : "No";
  }
  if (n.includes("directly impacted business metrics") && (n.includes("revenue") || n.includes("conversion") || n.includes("advertiser spend"))) {
    return hasProfileEvidence(profileText, "customer retention", "retention targeting", "workflow efficiency", "reporting latency", "business analytics") ? "Yes" : "No";
  }
  return null;
}

function developerFacingProductsAnswer(label, profile) {
  const n = norm(label);
  if (!(
    n.includes("developer facing") &&
    (n.includes("product") || n.includes("tool")) &&
    (n.includes("api") || n.includes("sdk") || n.includes("cli"))
  )) return null;
  const saved = findAnswer(label, profile.answers || {});
  if (saved != null) return String(saved);
  const profileText = profileEvidenceText(profile);
  const hasDeveloperTooling = hasProfileEvidence(
    profileText,
    "developer tools",
    "developer facing",
    "api",
    "apis",
    "rest",
    "fastapi",
    "sdk",
    "cli",
    "github cli",
    "notion api",
    "tool integrations"
  );
  return hasDeveloperTooling ? "Yes" : "No";
}

function autoAnswer(label, profile, sensitive = false) {
  if (!label || requiresUserAuthoredAnswer(label, profile)) return null;
  const n = norm(label);
  const company = String(profile.target_company || "the company");
  const title = String(profile.target_title || "this role");
  const answers = profile.answers || {};
  if (n.includes("suffix")) return profile.suffix || answers["Suffix"] || null;
  if (n.includes("middle name")) return profile.middle_name || answers["Middle Name"] || null;
  if (n.includes("address line 2")) return profile.address_line2 || answers["Address 2"] || null;
  if (n === "county" || n.endsWith(" county")) return profile.county || answers["County"] || null;
  if (n.includes("degree in computer science")) {
    const csEntries = (profile.education || []).filter((item) => item && typeof item === "object" && norm(item.field).includes("computer science"));
    if (n.includes("what level") || (n.includes("level") && n.includes("school"))) {
      const degreeText = norm(csEntries.length ? csEntries[0].degree : "");
      if (degreeText.includes("master")) return "Masters";
      if (degreeText.includes("bachelor")) return "Bachelors";
      if (degreeText.includes("phd") || degreeText.includes("doctor")) return "PhD";
      return csEntries.length ? "Other" : null;
    }
    return csEntries.length ? "Yes" : "No";
  }
  if (n.includes("which part of the bay area") && n.includes("based")) {
    const location = String(profile.location || "Jersey City, NJ, USA");
    return `I am currently based in ${location}, not in the Bay Area, and I am willing to relocate to San Francisco for the required in-office schedule.`;
  }
  if (n.includes("currently based in one of the following geographies")) {
    const location = norm(profile.location || "");
    const targetCities = ["denver", "st louis", "saint louis", "indianapolis"].filter((city) => n.includes(city));
    return targetCities.some((city) => location.includes(city)) ? "Yes" : "No";
  }
  if (
    n.includes("currently based in any of these countries") ||
    (n.includes("countries where we are accepting applications") && n.includes("currently based"))
  ) {
    return targetApplicationCountry(profile) || inferCountry(profile);
  }
  if (
    (n.includes("willing to work") || n.includes("able to work") || n.includes("excited and able")) &&
    (n.includes("office") || n.includes("on site") || n.includes("onsite")) &&
    (
      n.includes("four days") ||
      n.includes("4 days") ||
      n.includes("monday friday") ||
      n.includes("monday through friday") ||
      n.includes("nyc") ||
      n.includes("sf") ||
      n.includes("san francisco") ||
      n.includes("new york") ||
      n.includes("stockholm")
    )
  ) {
    const relocation = answers["Are you open to relocation?"]
      || answers["Are you open to working in-person in one of our offices 25% of the time?"]
      || approvedSensitiveEntryAnswer(profile, "relocation")
      || matchSensitive("relocation");
    return truthyAnswer(relocation) ? "Yes" : null;
  }
  if (n.includes("hands on engineering experience") && n.includes("python") && (n.includes("ml framework") || n.includes("pytorch"))) {
    return hasProfileEvidence(profileEvidenceText(profile), "python", "pytorch") ? "Yes" : "No";
  }
  if (n.includes("customer facing") && (n.includes("enterprise customer") || n.includes("customer")) && (n.includes("travel") || n.includes("embedding") || n.includes("embedded"))) {
    const travel = answers["Are you willing to travel?"] || matchScreeningRule(label, profile.screening_answer_rules);
    return travel == null || truthyAnswer(travel) ? "Yes" : "No";
  }
  if (n.includes("student or new grad")) {
    const levels = norm((profile.target_levels || []).join(" "));
    const profileBlob = profileEvidenceText(profile);
    return levels.includes("new grad") || profileBlob.includes("new grad") || profileBlob.includes("student") ? "Yes" : "No";
  }
  if (n.includes("earliest start date")) {
    const start = answers["What is your earliest start date?"]
      || approvedSensitiveEntryAnswer(profile, "start_date")
      || matchSensitive("start_date")
      || "Within a month";
    return norm(start).includes("within a month") ? "Immediately/next few months, full-time" : String(start);
  }
  if (n.includes("expected graduation") && (n.includes("month") || n.includes("year"))) {
    return String(profile.graduation_date || educationEndDateValue(profile) || "May 2026");
  }
  if (n.includes("where have you published your work")) {
    const savedPublications = findAnswer(label, answers);
    if (savedPublications != null) return String(savedPublications);
    if (profile.publications) return String(profile.publications);
    return "N/A";
  }
  if (n.includes("ai frameworks") && n.includes("hands on")) {
    return "I have used LangChain hands-on to build a multi-agent financial-audit workflow with retrieval, tool-style orchestration, human-in-the-loop feedback, and a BERT-based semantic similarity evaluator. I have also built RAG and LLM evaluation workflows around Hugging Face Transformers, PyTorch, and custom retrieval/evaluation harnesses. I have not used AutoGen in production, but I understand the multi-agent orchestration pattern and have built comparable agent workflows with LangChain.";
  }
  if (n.includes("working directly with clients") || n.includes("consulting capacity")) {
    return "At DHL Express, I worked with business and analytics stakeholders on customer-retention and reporting workflows, translating operational goals into SQL/Pandas ETLs, an XGBoost churn model, and Power BI analytics that improved retention targeting precision by 30%. I am comfortable gathering requirements, explaining tradeoffs to non-engineering stakeholders, and turning business pain points into deployed automation or ML workflows.";
  }
  if (n.includes("managed ai agents")) {
    return "I built XClaw, an AI agent orchestration desktop platform that supports 500+ LLMs and routes work through 50+ execution skills such as GitHub automation, scheduled briefings, task extraction, and tool-driven workflows. I also built a LangChain multi-agent audit workflow where agents retrieved financial context, generated audit outputs, and were evaluated against expert reports with a BERT-based semantic similarity benchmark.";
  }
  if (n.includes("system you") && n.includes("built before")) {
    return "I built a LangChain multi-agent auditing and evaluation system for financial audit workflows. The system combined retrieval, agent orchestration, human-in-the-loop feedback, and a BERT-based semantic similarity evaluator to compare AI-generated audit reports with expert outputs. It reached an 85% alignment rate with human experts and improved audit workflow efficiency by 40%.";
  }
  if (
    n.includes("comfortable") &&
    (n.includes("coming to the office") || n.includes("work from the office") || n.includes("in office")) &&
    (n.includes("3 days") || n.includes("three days") || n.includes("tuesday"))
  ) {
    const relocation = answers["Are you open to relocation?"]
      || answers["Are you open to working in-person in one of our offices 25% of the time?"]
      || approvedSensitiveEntryAnswer(profile, "relocation")
      || matchSensitive("relocation");
    return truthyAnswer(relocation) ? "Yes" : null;
  }
  if (
    (n.includes("foster city") || n.includes("hq 3 days per week")) &&
    (n.includes("3 days") || n.includes("three days")) &&
    (n.includes("work from") || n.includes("work at") || n.includes("hq"))
  ) {
    const relocation = answers["Are you open to relocation?"]
      || approvedSensitiveEntryAnswer(profile, "relocation")
      || matchSensitive("relocation");
    return truthyAnswer(relocation) ? "Yes" : null;
  }
  if (n.includes("highest level of education") && (n.includes("institution") || n.includes("from which"))) {
    const education = (profile.education || []).find((item) => item && typeof item === "object") || {};
    const degree = String(education.degree || "Master's Degree");
    const field = String(education.field || "Computer Science");
    const school = String(education.school || "Stevens Institute of Technology");
    return `${degree} in ${field} from ${school}`;
  }
  if (n.includes("highest level of education") && n.includes("completed")) {
    const education = (profile.education || []).find((item) => item && typeof item === "object") || {};
    const degree = String(education.degree || "Master's Degree");
    const normalizedDegree = norm(degree);
    if (normalizedDegree.includes("master")) return "Master's Degree";
    if (normalizedDegree.includes("bachelor")) return "Bachelor's Degree";
    if (normalizedDegree.includes("doctor") || normalizedDegree.includes("phd")) return "Doctoral Degree";
    return degree;
  }
  if (isSourceQuestion(n)) {
    return preferredSourceAnswer(label, profile, answers);
  }
  if (n.includes("spring career fair")) {
    const savedCareerFair = findAnswer(label, answers);
    return savedCareerFair != null ? String(savedCareerFair) : "No";
  }
  if (
    (n.includes("i understand") || n.includes("please confirm") || n.includes("confirm")) &&
    (n.includes("in person role") || n.includes("in-person role"))
  ) {
    const relocation = answers["Are you open to relocation?"]
      || approvedSensitiveEntryAnswer(profile, "relocation")
      || matchSensitive("relocation");
    return truthyAnswer(relocation) ? "Yes" : null;
  }
  if (
    n.includes("do you have") &&
    n.includes("personal project") &&
    (n.includes("proud") || n.includes("share"))
  ) {
    const projects = profile.projects || profile.outside_experience || [];
    return projects.some((project) => project && typeof project === "object") ? "Yes" : "No";
  }
  if (n.includes("personal project") && (n.includes("proud") || n.includes("share"))) {
    const projects = profile.projects || profile.outside_experience || [];
    for (const project of projects) {
      if (!project || typeof project !== "object") continue;
      const titleText = `${project.title || ""} ${project.name || ""}`;
      if (norm(titleText).includes("xclaw")) {
        const url = project.url || "https://github.com/Alfred768/xclaw";
        return `XClaw is a desktop interface for Open Claw that I built to orchestrate autonomous AI agent workflows across hundreds of LLMs, with streaming Markdown UX, tool integrations, scheduled automation, and messaging integrations. ${url}`;
      }
    }
  }
  if (
    n.includes("application you built yourself") &&
    n.includes("problem you were solving") &&
    n.includes("measure success")
  ) {
    return "I built XClaw, a desktop interface for Open Claw, to make autonomous AI-agent workflows easier to run and observe from one place. The problem was that agent work often spans many models, tools, and channels, but users need a coherent interface for streaming responses, executing actions, and tracking useful outputs. I built the application with a desktop UI, real-time LLM streaming, rich Markdown rendering, 50+ execution skills, scheduled daily briefings, NLP-based task extraction, and integrations with GitHub CLI, Notion API, WhatsApp, Telegram, and Discord. I measured success by whether the system could reliably route work across hundreds of LLMs, turn chat messages into actionable tasks, and support repeatable automations such as daily briefings and tool-driven workflows instead of one-off prompts.";
  }
  if (n.includes("what excites you about this opportunity")) {
    return `I am excited by ${company}'s mission-driven product work and by the chance to grow in a new grad software engineering role where I can contribute across customer-facing and internal platforms. My background includes Python, REST APIs, React, Docker/Kubernetes, data pipelines, and ML-focused internship projects, and I would value the mentorship, code review, and fast feedback loop described for this team.`;
  }
  if (n.includes("high level of grit")) {
    return "At Intellisys Lab, I worked on federated LLM fine-tuning where reliability problems showed up across distributed edge-device training, data ingestion, and regression monitoring. I responded by breaking the problem into measurable checks, adding MLflow experiment tracking, improving Kafka-based data flows, and iterating on evaluation harnesses instead of treating failures as one-off issues. That persistence helped improve LLM accuracy by 54% over centralized baselines and made the workflow more reproducible.";
  }
  if (n.includes("full ownership") && n.includes("challenging moment")) {
    return "During my DHL Express internship, I took ownership of a customer-retention ML workflow that required turning business goals into usable data and model outputs. I built SQL/Pandas ETLs, trained an XGBoost churn model with SHAP explainability, handled class imbalance, and helped productionize retraining and reporting workflows with AWS ECS Fargate, MLflow, Jenkins, and Power BI. The work improved retention targeting precision by 30% and reduced model reporting latency by 30%.";
  }
  if (
    n.includes("took ownership") &&
    (n.includes("without a playbook") || n.includes("figured it out")) &&
    n.includes("saw it through")
  ) {
    return "At DHL Express, I took ownership of a customer-retention ML workflow where the problem was not handed to me as a clean technical spec. I had to turn a broad business goal into usable data, model behavior, and reporting outputs. I built SQL/Pandas ETLs, trained an XGBoost churn model with SHAP explainability, handled class imbalance, and productionized retraining/reporting workflows using AWS ECS Fargate, MLflow, Jenkins, and Power BI. I kept iterating with business stakeholders until the workflow was measurable and useful; it improved retention targeting precision by 30% and reduced model reporting latency by 30%.";
  }
  if (n.includes("relative") && (n.includes("work for") || n.includes("currently work") || n.includes("employed")) && !n.includes("if so") && !n.includes("who")) {
    return "No";
  }
  if ((
      n.includes("anchor days") ||
      n.includes("working from one of our offices") ||
      (n.includes("hybrid schedule") && (n.includes("in office") || n.includes("in-office"))) ||
      n.includes("hybrid policy")
    ) && (n.includes("office") || n.includes("in person") || n.includes("hybrid policy"))) {
    const officeAnswer = answers["Are you open to working in-person in one of our offices 25% of the time?"]
      || answers["Are you able to commit to working from one of our offices on Anchor Days each week?"]
      || answers["Are you open to a hybrid schedule with in-office days on Monday, Wednesday, and Friday?"]
      || findAnswer("Are you open to working in-person in one of our offices 25% of the time?", answers);
    if (officeAnswer != null) return String(officeAnswer);
  }
  if (n.includes("1099") && (n.includes("without requiring") || n.includes("without sponsorship") || n.includes("complete any paperwork"))) {
    for (const [key, record] of Object.entries(profile.sensitive_answers || {})) {
      if (!norm(key).includes("sponsor") || !record || !record.approved) continue;
      if (["yes", "true", "1"].includes(norm(record.answer))) return "No";
    }
  }
  if (
    (n.includes("legally authorized") || n.includes("authorized to work")) &&
    (n.includes("without requiring") || n.includes("without sponsorship")) &&
    (n.includes("sponsorship") || n.includes("visa"))
  ) {
    const sponsorship = approvedSensitiveEntryAnswer(profile, "sponsorship")
      || String((profile.work_authorization_by_country || {}).requires_sponsorship || "");
    if (sponsorship != null) return truthyAnswer(sponsorship) ? "No" : "Yes";
  }
  if (n.includes("compensation offer") && n.includes("factors")) {
    const savedCompensationFactor = findAnswer(label, answers);
    return savedCompensationFactor != null ? String(savedCompensationFactor) : "No";
  }
  if (
    n.includes("desired compensation") ||
    n.includes("compensation range") ||
    n.includes("compensation expectation") ||
    n.includes("compensation expectations") ||
    n.includes("desired pay") ||
    n.includes("salary expectation")
  ) {
    const savedCompensation = findAnswer(label, answers);
    if (savedCompensation != null) return String(savedCompensation);
    const approvedSalary = approvedSensitiveEntryAnswer(profile, "salary");
    if (approvedSalary != null) return String(approvedSalary);
    return profile.minimum_expected_salary ? String(profile.minimum_expected_salary) : null;
  }
  if (n.includes("current work status")) {
    const savedStatus = findAnswer(label, answers);
    if (savedStatus != null) return String(savedStatus);
    return profile.job_search_status ? String(profile.job_search_status) : null;
  }
  if (n.includes("at least 18 years of age")) {
    const savedAge = findAnswer(label, answers);
    if (savedAge != null) return String(savedAge);
    const birthday = dateTarget(profile.birthday);
    if (birthday) {
      const today = new Date();
      let age = today.getFullYear() - birthday.getFullYear();
      if ((today.getMonth() < birthday.getMonth()) || (today.getMonth() === birthday.getMonth() && today.getDate() < birthday.getDate())) age -= 1;
      return age >= 18 ? "Yes" : "No";
    }
  }
	  if (n.includes("confirm receipt") && (n.includes("privacy notice") || n.includes("arbitration agreement"))) {
	    const savedNotice = findAnswer(label, answers);
	    if (savedNotice != null) return String(savedNotice);
    const privacy = approvedSensitiveEntryAnswer(profile, "privacy_consent");
    const legal = approvedSensitiveEntryAnswer(profile, "legal_attestation");
	    const terms = approvedSensitiveEntryAnswer(profile, "terms_consent");
	    if ([privacy, legal, terms].some((value) => truthyAnswer(value))) return "Confirmed";
	  }
	  if (n.includes("privacy notice") && (n.includes("acknowledgement") || n.includes("acknowledgment"))) {
	    const savedNotice = findAnswer(label, answers);
	    if (savedNotice != null) return String(savedNotice);
	    const privacy = approvedSensitiveEntryAnswer(profile, "privacy_consent");
	    const legal = approvedSensitiveEntryAnswer(profile, "legal_attestation");
	    const terms = approvedSensitiveEntryAnswer(profile, "terms_consent");
	    if ([privacy, legal, terms].some((value) => truthyAnswer(value))) return "I Acknowledge";
	  }
	  if (n.includes("consent to process") || (n.includes("process") && n.includes("personal data") && n.includes("consent"))) {
	    const savedConsent = findAnswer(label, answers);
	    if (savedConsent != null) return String(savedConsent);
	    const privacy = approvedSensitiveEntryAnswer(profile, "privacy_consent");
	    const terms = approvedSensitiveEntryAnswer(profile, "terms_consent");
	    if ([privacy, terms].some((value) => truthyAnswer(value))) return "I Agree";
	  }
  const legalTermsConsent = legalTermsConsentAnswer(label, profile);
  if (legalTermsConsent != null) return legalTermsConsent;
  if (n.includes("may use ai tools") && n.includes("application and interview process")) {
    const savedAiAck = findAnswer(label, answers);
    if (savedAiAck != null) return String(savedAiAck);
    const legal = approvedSensitiveEntryAnswer(profile, "legal_attestation");
    return legal == null || truthyAnswer(legal) ? "Yes" : null;
  }
  if (n.includes("best describes how you use ai tools today")) {
    const savedAiUse = findAnswer(label, answers);
    if (savedAiUse != null) return String(savedAiUse);
    const profileText = profileEvidenceText(profile);
    if (profileText.includes("agent") || profileText.includes("ai tool") || profileText.includes("automation")) {
      return "I design or automate workflows with AI tools (e.g., building agents, integrating AI into team processes).";
    }
    return "I have experimented with AI tools (professionally and/or personally).";
  }
  if (n.includes("know anyone") && (n.includes("currently at") || n.includes("currently work"))) {
    const savedConnection = findAnswer(label, answers);
    if (savedConnection != null) return String(savedConnection);
    return "No";
  }
  if (n.includes("built ai agents") || (n.includes("built") && n.includes("ai agents"))) {
    const savedAgents = findAnswer(label, answers);
    if (savedAgents != null) return String(savedAgents);
    return "Yes. I built XClaw, an AI agent orchestration desktop platform that routes work across LLMs, tools, and execution skills, and I built a LangChain multi-agent audit workflow with retrieval, human-in-the-loop feedback, and BERT-based evaluation against expert audit reports.";
  }
  if (n.includes("what ai tools") && (n.includes("currently using") || n.includes("using today"))) {
    const savedTools = findAnswer(label, answers);
    if (savedTools != null) return String(savedTools);
    return "I use ChatGPT/Codex for coding assistance, debugging, and workflow automation; LangChain for agent/RAG prototypes; OpenAI and Anthropic APIs for LLM workflows; and Python, PyTorch, MLflow, Kafka, Docker, and Kubernetes for model training, evaluation, deployment, and monitoring.";
  }
  if (n.includes("large language models") && (n.includes("worked with") || n.includes("completed academic projects"))) {
    const savedLlmExposure = findAnswer(label, answers);
    if (savedLlmExposure != null) return String(savedLlmExposure);
    const profileText = profileEvidenceText(profile);
    return ["llm", "large language", "openai", "anthropic", "langchain", "rag"].some((term) => profileText.includes(term)) ? "Yes" : "No";
  }
  if (n.includes("working proficiency in python") && (n.includes("scripts") || n.includes("apis") || n.includes("data structures"))) {
    const savedPython = findAnswer(label, answers);
    if (savedPython != null) return String(savedPython);
    const skills = new Set((profile.skills || []).map((skill) => norm(skill)));
    const profileText = profileEvidenceText(profile);
    return skills.has("python") || profileText.includes("python") ? "Yes" : "No";
  }
  if (n.includes("relatives currently work") || n.includes("relatives currently employed")) {
    const savedRelative = findAnswer(label, answers);
    return savedRelative != null ? String(savedRelative) : "N/A";
  }
  if (n.includes("referred") && (n.includes("full name") || n.includes("employee name") || n.includes("referring individual"))) {
    const savedReferralName = findAnswer(label, answers);
    return savedReferralName != null ? String(savedReferralName) : "N/A";
  }
  if (
    (n.includes("were you referred") || n.includes("are you referred") || n.includes("employee referral")) &&
    (n.includes("current employee") || n.includes("employee of the company") || n.includes("company employee") || n.includes("by an employee"))
  ) {
    const savedReferral = findAnswer(label, answers);
    return savedReferral != null ? String(savedReferral) : "No";
  }
  const productionScreening = productionScreeningAnswer(label, profile);
  if (productionScreening != null) return productionScreening;
  if (n.includes("securities industry") && (n.includes("registered") || n.includes("attempted"))) {
    const savedSecurities = findAnswer(label, answers);
    return savedSecurities != null ? String(savedSecurities) : "No";
  }
  if (
    (n.includes("government official") || n.includes("financial regulator") || (n.includes("military") && n.includes("law enforcement"))) &&
    (
      n.includes("currently") ||
      n.includes("previously") ||
      n.includes("influence") ||
      n.includes("post employment") ||
      n.includes("post-employment") ||
      n.includes("immediate family") ||
      n.includes("close associate") ||
      n.includes("referred") ||
      n.includes("recommended")
    )
  ) {
    const savedGovernment = findAnswer(label, answers);
    return savedGovernment != null ? String(savedGovernment) : "No";
  }
  if (n.includes("current government official") || n.includes("former government official")) {
    const savedGovernment = findAnswer(label, answers);
    return savedGovernment != null ? String(savedGovernment) : "No, I am not a current or former Government Official";
  }
  if (n.includes("close relative of a government official")) {
    const savedRelative = findAnswer(label, answers);
    return savedRelative != null ? String(savedRelative) : "No, I am not a relative of a government official.";
  }
  if (n.includes("referred to this position") && (n.includes("senior leader") || n.includes("decision maker") || n.includes("decisionmaker"))) {
    const savedReferral = findAnswer(label, answers);
    return savedReferral != null ? String(savedReferral) : "No";
  }
  if (
    n.includes("if you answered yes") &&
    (n.includes("employment authorization") || n.includes("immigration") || n.includes("sponsorship")) &&
    (n.includes("explanation") || n.includes("explain") || n.includes("provide"))
  ) {
    const savedExplanation = findAnswer(label, answers);
    if (savedExplanation != null) return String(savedExplanation);
    const sponsorship = approvedSensitiveEntryAnswer(profile, "sponsorship")
      || String((profile.work_authorization_by_country || {}).requires_sponsorship || "");
    if (truthyAnswer(sponsorship)) {
      return "I am currently authorized to work in the United States and will require immigration-related employer sponsorship in the future to maintain employment authorization.";
    }
    return null;
  }
  const biopharmaCompliance = biopharmaComplianceAnswer(label, profile);
  if (biopharmaCompliance != null) return biopharmaCompliance;
  if (n.includes("how many years of professional experience") && n.includes("excluding internships")) {
    return zeroBasedProfessionalExperienceRangeAnswer(profile);
  }
  if (
    (n.includes("full time software engineer") || n.includes("full time software engineering")) &&
    n.includes("professional setting") &&
    n.includes("excluding internships")
  ) {
    return hasFullTimeSoftwareEngineeringExperience(profile) ? "Yes" : "No";
  }
  if (n.includes("which programming languages") && n.includes("regularly use") && n.includes("professional setting")) {
    return String(profile.preferred_programming_language || "Python");
  }
  if (n.includes("full time") && n.includes("internship")) {
    const savedRoleType = answers["What type of roles are you looking for?"] || findAnswer(label, answers);
    return savedRoleType != null ? String(savedRoleType) : "Full-Time";
  }
  const otherCountries = otherCountriesLocationAnswer(label, profile);
  if (otherCountries != null) return otherCountries;
  const yearsMatch = n.match(/at least\s+(\d+)\s*(?:\+)?\s+years?/);
  if (yearsMatch && n.includes("experience")) {
    const rawYears = String(profile.years_experience || profile.relevant_years_experience || profile.post_college_years_experience || "");
    const nums = Array.from(rawYears.matchAll(/\d+/g)).map((match) => Number(match[0]));
    if (nums.length) return Math.max(...nums) >= Number(yearsMatch[1]) ? "Yes" : "No";
  }
	  if (n.includes("pronouns")) {
	    const value = profile.pronouns || answers["Pronouns"];
    const valueNorm = norm(value);
    if (valueNorm.includes("he") && valueNorm.includes("him")) return "He / Him";
    if (valueNorm.includes("she") && valueNorm.includes("her")) return "She / Her";
    if (valueNorm.includes("they") && valueNorm.includes("them")) return "They / Them";
	    return value ? String(value) : null;
	  }
	  if (n.includes("sexual orientation")) {
	    const demographics = profile.demographics || {};
	    const value = demographics.sexual_orientation || profile.sexual_orientation;
	    return value ? String(value) : "I don't wish to answer";
	  }
  if (n.includes("community support domain")) {
    return "I do not have direct Community Support domain experience, but I have worked on customer-focused ML and analytics problems. At DHL Express, I built churn prediction, sentiment analysis, SQL/Pandas data workflows, and Power BI reporting to improve customer retention targeting and operational decision-making.";
  }
  if (n.includes("exceptional work")) {
    return "I built and evaluated production-minded ML systems across research and applied settings. At Intellisys Lab, I deployed federated LLM fine-tuning and evaluation workflows on Kubernetes across 100+ edge devices with Kafka ingestion and MLflow tracking, improving LLM accuracy by 54% over centralized baselines. At DHL Express, I built an XGBoost customer-churn pipeline, Transformer sentiment service, SQL/Pandas ETLs, and AWS ECS retraining workflows that improved customer-retention targeting precision by 30%.";
  }
  if (n.includes("spacexai employment history") || n.includes("spacex employment history")) {
    const savedHistory = findAnswer(label, answers);
    return savedHistory != null ? String(savedHistory) : "I have never worked for SpaceX or SpaceXAI";
  }
  if (
    (n.includes("previously") || n.includes("currently") || n.includes("current")) &&
    (
      n.includes("contractor") ||
      n.includes("consultant") ||
      n.includes("former employee") ||
      n.includes("access to") ||
      n.includes("engaged with")
    )
  ) {
    const savedEngagement = findAnswer(label, answers);
    return savedEngagement != null ? String(savedEngagement) : "No";
  }
  if (n.includes("non compete") || n.includes("non solicitation")) {
    const savedAgreement = findAnswer(label, answers);
    return savedAgreement != null ? String(savedAgreement) : "No";
  }
  if (n.includes("worked for airbnb") || (n.includes("currently") && n.includes("ever worked") && n.includes("airbnb"))) {
    const savedAirbnbHistory = findAnswer(label, answers);
    return savedAirbnbHistory != null ? String(savedAirbnbHistory) : "No";
  }
  if (n.includes("been employed by") && (n.includes("past") || n.includes("subsidiary") || n.includes("affiliate"))) {
    const savedEmployment = findAnswer(label, answers);
    return savedEmployment != null ? String(savedEmployment) : "No";
  }
  if (n.includes("contact") && n.includes("current employer") && (profile.work_history || []).length) {
    const savedContact = findAnswer(label, answers);
    return savedContact != null ? String(savedContact) : "No";
  }
  if (n.includes("employment and military service") && n.includes("add another employment")) {
    return "Thank you";
  }
  if (n.includes("essential functions") && n.includes("reasonable accommodation")) {
    const savedAbility = findAnswer(label, answers);
    return savedAbility != null ? String(savedAbility) : "Yes";
  }
  if (
    n.includes("when can you start") ||
    n.includes("soonest date") ||
    n.includes("earliest availability") ||
    ((n.includes("available") || n.includes("availability")) && (n.includes("start") || n.includes("begin")))
  ) {
    const availability = answers["When can you start?"]
      || answers["What is your earliest availability?"]
      || answers["What is the soonest date you would be available to start?"]
      || profile.earliest_availability
      || profile.availability
      || profile.start_date;
    if (availability != null && String(availability).trim()) return String(availability);
  }
  if (n.includes("commutable proximity") && n.includes("relocat")) {
    const value = matchSensitive(label);
    if (truthyAnswer(value)) return "I am willing to relocate before starting employment.";
    if (value != null) return String(value);
  }
  if (n.includes("relocat")) {
    const value = matchSensitive(label);
    if (value != null) return String(value);
  }
  if ((n.includes("currently based") || n.includes("currently living")) && (n.includes("san francisco") || n.includes("bay area"))) {
    const location = norm(profile.location || "");
    return location.includes("san francisco") || location.includes("bay area") ? "Yes" : "No";
  }
  if (n.includes("review") && n.includes("linked document") && profileCompanySlug(profile) === "lyft") {
    const value = matchSensitive("privacy policy");
    if (value != null) return "I acknowledge that I have read and understood the terms of the Lyft Candidate Privacy Notice.";
  }
  if (n.includes("candidate privacy policy") && profileCompanySlug(profile) === "airbnb") {
    const value = matchSensitive("privacy policy");
    if (value != null) return "I acknowledge that I have read and understood the Airbnb Candidate Privacy Policy.";
  }
  if (n === "language") {
    return String(profile.language || profile.human_language || "English");
  }
  const saved = findAnswer(label, answers);
  if (saved != null) return String(saved);
  const developerFacing = developerFacingProductsAnswer(label, profile);
  if (developerFacing != null) return developerFacing;
  if (isMotivationQuestion(n, company)) {
    const generated = motivationAnswerForLabel(label, profile);
    if (generated) return String(generated);
    return null;
  }
  if (n.includes("strong fit") && (n.includes("role") || n.includes("position"))) {
    const finalAnswer = answers["Use this final response to make your case for why we should prioritize interviewing you. You may include anything you think is most relevant or differentiating"]
      || answers["What makes you a strong fit for this role?"]
      || answers["Why " + company + "?"]
      || answers["Why do you want to work at " + company + "?"];
    if (finalAnswer) return String(finalAnswer);
    const titleText = title && title !== "this role" ? title : "this role";
    return `I am a strong fit for ${titleText} because my background combines LLM/RAG evaluation, distributed model training workflows, and production ML engineering. At Intellisys Lab, I built federated LLM fine-tuning and evaluation workflows with Kubernetes, Kafka, MLflow, TensorFlow Federated, and custom RAG metrics. At DHL Express, I productionized ML retraining, monitoring, Dockerized model services, and SQL/Pandas data pipelines with measurable business impact.`;
  }
  if (n.includes("additional information") || n.includes("anything else")) return String(answers["Additional Information"] || "");
  if (sensitive) return matchSensitive(label) || demographicAnswer(label, profile);
  return null;
}

function isMotivationQuestion(normalizedLabel, company) {
  const companyNorm = norm(company || "");
  if (normalizedLabel.includes("what excites you about")) return true;
  if (normalizedLabel.includes("this role interests you")) return true;
  if (normalizedLabel.includes("what about") && normalizedLabel.includes("interests you")) return true;
  if (normalizedLabel.includes("why are you applying to")) return true;
  if (normalizedLabel.includes("why") && normalizedLabel.includes("team")) return true;
  return normalizedLabel.includes("why") && (
    normalizedLabel.includes("company") ||
    normalizedLabel.includes("role") ||
    (companyNorm && normalizedLabel.includes(companyNorm))
  );
}

function motivationAnswerForLabel(label, profile) {
  const answers = profile.answers || {};
  const company = String(profile.target_company || "the company");
  let answer = answers["Why " + company + "?"]
    || answers["Why do you want to work at " + company + "?"]
    || answers["What excites you about " + company + "?"]
    || answers["What excites you about this opportunity?"]
    || answers["Why are you applying to " + company + "?"]
    || answers["Why are you interested in this role?"]
    || answers["Why this role?"];
  if (!answer) return null;
  answer = String(answer).trim();
  const n = norm(label);
  if (n.includes("agent platform") && !norm(answer).includes("agent")) {
    answer += " I am especially interested in agent platform work because my projects include LangChain multi-agent workflows and agent tooling.";
  }
  if (
    (n.includes("distributed systems") || n.includes("core infrastructure") || n.includes("infrastructure team")) &&
    !["distributed", "infrastructure", "kubernetes", "kafka"].some((token) => norm(answer).includes(token))
  ) {
    answer += " I am especially interested in this team because my background includes Kubernetes, Kafka, MLflow, and distributed model-training workflows.";
  }
  return answer;
}

function priorityAutoAnswer(label, profile) {
  const n = norm(label);
  if (isSchoolComboboxField({ label }) && !n.includes("degree") && !n.includes("level")) {
    return currentEducationValue(profile, "school");
  }
  if (n.includes("currently based in one of the following geographies")) {
    return autoAnswer(label, profile, false);
  }
  if (
    n.includes("currently based in any of these countries") ||
    (n.includes("countries where we are accepting applications") && n.includes("currently based"))
  ) {
    return targetApplicationCountry(profile) || inferCountry(profile);
  }
  if (n.includes("degree in computer science")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("how many years of professional experience") && n.includes("excluding internships")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("how many years of relevant professional experience")) {
    return relevantProfessionalExperienceRangeAnswer(profile);
  }
  if (
    (n.includes("full time software engineer") || n.includes("full time software engineering")) &&
    n.includes("professional setting") &&
    n.includes("excluding internships")
  ) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("which programming languages") && n.includes("regularly use") && n.includes("professional setting")) {
    return autoAnswer(label, profile, false);
  }
  if (isSourceQuestion(n)) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("pronouns")) {
    return autoAnswer(label, profile, false);
  }
  if (n === "language") {
    return autoAnswer(label, profile, false);
  }
	  if (n.includes("gender") || n.includes("transgender")) {
	    const demographic = demographicAnswer(label, profile);
	    if (demographic != null) return demographic;
	  }
	  if (n.includes("sexual orientation")) {
	    return autoAnswer(label, profile, false);
	  }
  if (n.includes("hispanic") || n.includes("latino")) {
    const demographic = demographicAnswer(label, profile);
    if (demographic != null) return demographic;
  }
  if (n.includes("race") || n.includes("veteran") || n.includes("disability") || n.includes("disabled")) {
    const demographic = demographicAnswer(label, profile);
    if (demographic != null) return demographic;
  }
  if (n.includes("contact") && n.includes("current employer")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("been employed by") && (n.includes("past") || n.includes("subsidiary") || n.includes("affiliate"))) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("commutable proximity") && n.includes("relocat")) {
    return autoAnswer(label, profile, false);
  }
  if ((n.includes("anchor days") || n.includes("working from one of our offices")) && (n.includes("office") || n.includes("in person"))) {
    return autoAnswer(label, profile, false);
  }
  if (
    (n.includes("foster city") || n.includes("hq 3 days per week")) &&
    (n.includes("3 days") || n.includes("three days"))
  ) {
    return autoAnswer(label, profile, false);
  }
  const developerFacing = developerFacingProductsAnswer(label, profile);
  if (developerFacing != null) return developerFacing;
  if (n.includes("1099") && (n.includes("without requiring") || n.includes("without sponsorship") || n.includes("complete any paperwork"))) {
    return autoAnswer(label, profile, false);
  }
  if (
    (n.includes("legally authorized") || n.includes("authorized to work")) &&
    (n.includes("without requiring") || n.includes("without sponsorship")) &&
    (n.includes("sponsorship") || n.includes("visa"))
  ) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("full time") && n.includes("internship")) {
    return autoAnswer(label, profile, false);
  }
  if (otherCountriesLocationAnswer(label, profile) != null) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("compensation offer") && n.includes("factors")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("at least 18 years of age")) {
    return autoAnswer(label, profile, false);
  }
	  if (n.includes("confirm receipt") && (n.includes("privacy notice") || n.includes("arbitration agreement"))) {
	    return autoAnswer(label, profile, false);
	  }
	  if (n.includes("privacy notice") && (n.includes("acknowledgement") || n.includes("acknowledgment"))) {
	    return autoAnswer(label, profile, false);
	  }
	  if (n.includes("consent to process") || (n.includes("process") && n.includes("personal data") && n.includes("consent"))) {
	    return autoAnswer(label, profile, false);
	  }
  if (legalTermsConsentAnswer(label, profile) != null) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("may use ai tools") && n.includes("application and interview process")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("best describes how you use ai tools today")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("current government official") || n.includes("former government official")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("close relative of a government official")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("referred to this position") && (n.includes("senior leader") || n.includes("decision maker") || n.includes("decisionmaker"))) {
    return autoAnswer(label, profile, false);
  }
  if (
    n.includes("if you answered yes") &&
    (n.includes("employment authorization") || n.includes("immigration") || n.includes("sponsorship")) &&
    (n.includes("explanation") || n.includes("explain") || n.includes("provide"))
  ) {
    return autoAnswer(label, profile, false);
  }
  if (biopharmaComplianceAnswer(label, profile) != null) {
    return autoAnswer(label, profile, false);
  }
  const yearsMatch = n.match(/at least\s+(\d+)\s*(?:\+)?\s+years?/);
  if (yearsMatch && n.includes("experience")) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("ai screening") && (n.includes("agree") || n.includes("proceed") || n.includes("consent"))) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("citizenship") && (n.includes("employment eligibility") || n.includes("work eligibility"))) {
    return autoAnswer(label, profile, false);
  }
  if ((n.includes("currently based") || n.includes("currently living")) && (n.includes("san francisco") || n.includes("bay area"))) {
    return autoAnswer(label, profile, false);
  }
  if (n.includes("candidate privacy policy") && profileCompanySlug(profile) === "airbnb") {
    return autoAnswer(label, profile, false);
  }
  return null;
}

function legalTermsConsentAnswer(label, profile) {
  const n = norm(label);
  const isTermsConsent = n.includes("terms and conditions") &&
    (
      n.includes("read and consent") ||
      n.includes("read and agree") ||
      n.includes("acceptance") ||
      n.includes("i agree") ||
      n.includes("agree to the terms") ||
      n.includes("by clicking")
    );
  const isTruthfulnessAttestation = (
    (n.includes("true and accurate") || n.includes("true and correct") || n.includes("false or misleading")) &&
    (n.includes("i confirm") || n.includes("i certify") || n.includes("i hereby certify") || n.includes("i understand") || n.includes("i attest"))
  );
  const isPrivacyConsent = n.includes("personal data") &&
    (n.includes("consent") || n.includes("consents") || n.includes("agree") || n.includes("accept")) &&
    (n.includes("by clicking") || n.includes("i accept") || n.includes("i agree"));
  const isStatementAck = n.includes("carefully read") &&
    n.includes("understand") &&
    n.includes("agree") &&
    n.includes("statement");
  if (!(isTermsConsent || isStatementAck || isPrivacyConsent || isTruthfulnessAttestation)) {
    return null;
  }
  const approved = isPrivacyConsent
    ? (approvedSensitiveEntryAnswer(profile, "privacy_consent")
      || approvedSensitiveEntryAnswer(profile, "terms_consent")
      || approvedSensitiveEntryAnswer(profile, "legal_attestation"))
    : (approvedSensitiveEntryAnswer(profile, "terms_consent")
      || approvedSensitiveEntryAnswer(profile, "legal_attestation"));
  if (approved == null) return null;
  return truthyAnswer(approved) ? "Yes" : String(approved);
}

function biopharmaComplianceAnswer(label, profile) {
  const n = norm(label);
  let defaultAnswer = null;
  if (
    (n.includes("conflict of interest") || n.includes("conflicts of interest")) &&
    n.includes("relatives") &&
    (n.includes("work in any capacity") || n.includes("work at"))
  ) {
    defaultAnswer = "No";
  }
  if (n.includes("willing to commute") && n.includes("area where this position is located")) defaultAnswer = "Yes";
  if (n.includes("oig list of excluded individuals entities")) defaultAnswer = "No";
  if (n.includes("general services administration") && n.includes("excluded")) defaultAnswer = "No";
  if (n.includes("debarred under the generic drug enforcement act")) defaultAnswer = "No";
  if (n.includes("debarment proceedings pending")) defaultAnswer = "No";
  if (n.includes("us licensed physician") || n.includes("u s licensed physician")) defaultAnswer = "No";
  if (
    (n.includes("fda") || n.includes("hhs")) &&
    (n.includes("investigated") || n.includes("disqualified") || n.includes("restricted")) &&
    n.includes("investigational drugs")
  ) {
    defaultAnswer = "No";
  }
  if (
    n.includes("pending inquiry by any governmental entity") ||
    (n.includes("licensing association") && n.includes("administrative action"))
  ) {
    defaultAnswer = "No";
  }
  if (defaultAnswer == null) return null;
  const saved = findAnswer(label, (profile && profile.answers) || {});
  return saved != null ? String(saved) : defaultAnswer;
}

function candidateAccountPasswordStorePath() {
  const override = String(process.env.JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE || "").trim();
  return override ? path.resolve(override) : path.resolve(process.cwd(), CANDIDATE_ACCOUNT_PASSWORD_STORE_FILENAME);
}

function candidateAccountPasswordStoreKey(applicationUrl, email) {
  const rawEmail = String(email || "").trim().toLowerCase();
  const rawUrl = String(applicationUrl || "").trim();
  if (!rawEmail || !rawUrl) return null;
  try {
    const host = new URL(rawUrl).hostname.toLowerCase();
    if (!host) return null;
    return `${host}\u0000${rawEmail}`;
  } catch (_) {
    return null;
  }
}

function loadCandidateAccountPasswordStore(storePath) {
  try {
    const parsed = JSON.parse(String(fs.readFileSync(storePath, "utf8") || "{}"));
    if (!parsed || typeof parsed !== "object") return { version: 1, accounts: {} };
    if (!parsed.accounts || typeof parsed.accounts !== "object") parsed.accounts = {};
    if (!parsed.version) parsed.version = 1;
    return parsed;
  } catch (_) {
    return { version: 1, accounts: {} };
  }
}

function saveCandidateAccountPasswordStore(storePath, payload) {
  fs.mkdirSync(path.dirname(storePath), { recursive: true });
  const tmpPath = `${storePath}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2));
  fs.renameSync(tmpPath, storePath);
  try { fs.chmodSync(storePath, 0o600); } catch (_) {}
}

function generateCandidateAccountPassword(length = CANDIDATE_ACCOUNT_PASSWORD_LENGTH) {
  const size = Math.max(16, Number(length) || CANDIDATE_ACCOUNT_PASSWORD_LENGTH);
  const alphabet = `abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789${CANDIDATE_ACCOUNT_PASSWORD_SPECIALS}`;
  const randomChar = () => alphabet[crypto.randomInt(0, alphabet.length)];
  while (true) {
    const password = Array.from({ length: size }, randomChar).join("");
    if (/[a-z]/.test(password) && /[A-Z]/.test(password) && /\d/.test(password) && /[!@#$%^*_-]/.test(password)) {
      return password;
    }
  }
}

function candidateAccountPassword(options = {}) {
  const direct = String(process.env.JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD || "").trim();
  if (direct) return direct;
  const passwordFile = String(process.env.JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE || "").trim();
  if (passwordFile) {
    try {
      const stored = String(fs.readFileSync(passwordFile, "utf8") || "").trim();
      if (stored) return stored;
    } catch (_) {}
  }
  const email = String(options.email || (CFG.profile && CFG.profile.email) || "").trim().toLowerCase();
  const applicationUrl = String(options.applicationUrl || CFG.applicationUrl || "").trim();
  const key = candidateAccountPasswordStoreKey(applicationUrl, email);
  if (!key) return null;
  const storePath = candidateAccountPasswordStorePath();
  const store = loadCandidateAccountPasswordStore(storePath);
  const accounts = store.accounts || {};
  const record = accounts[key];
  if (record && typeof record === "object") {
    const stored = String(record.password || "").trim();
    if (stored) return stored;
  }
  if (typeof record === "string" && record.trim()) return record.trim();
  if (!options.createIfMissing) return null;
  let host = "";
  try {
    host = new URL(applicationUrl).hostname.toLowerCase();
  } catch (_) {
    return null;
  }
  const password = generateCandidateAccountPassword();
  const timestamp = new Date().toISOString();
  accounts[key] = { host, email, password, created_at: timestamp, updated_at: timestamp };
  store.accounts = accounts;
  saveCandidateAccountPasswordStore(storePath, store);
  return password;
}

function isCandidateAccountCreationField(field) {
  const combined = norm([
    field.label, field.id, field.name, field.section, field.ariaLabel, field.ariaDescription, field.placeholder,
  ].filter(Boolean).join(" "));
  return [
    "verify password",
    "verifypassword",
    "verify new password",
    "confirm password",
    "new password",
    "create account",
  ].some((marker) => combined.includes(marker));
}

function isCandidateAccountCreationConsentCheckbox(field, ctx) {
  if (!(ctx && ctx.hasCandidateAccountCreation)) return false;
  const automationId = norm(String(field.automationId || field.automation_id || ""));
  const label = norm(String(field.label || ""));
  if (automationId === "createaccountcheckbox") return true;
  return ["agree", "accept", "yes", "i agree"].includes(label);
}

// Match a field label against the pre-filled, user-approved sensitive-answer
// knowledge base (profile.sensitive_answers). Each entry has patterns + an
// approved answer; only approved entries are used.
function matchSensitive(label) {
  const kb = (CFG.profile && CFG.profile.sensitive_answers) || {};
  const specificKeys = new Set(["citizenship", "active_security_clearance", "security_clearance_eligibility", "security_clearance", "sponsorship_type", "ai_notetaker_consent"]);
  const broadPreferenceKeys = new Set(["security_clearance_interest"]);
  const priority = ([key, entry]) => {
    const group = specificKeys.has(key) ? 0 : (broadPreferenceKeys.has(key) ? 2 : 1);
    const longest = Math.max(0, ...((entry && entry.patterns) || []).map((p) => String(p || "").length));
    return [group, -longest];
  };
  const entries = Object.entries(kb).sort((left, right) => {
    const lp = priority(left);
    const rp = priority(right);
    return (lp[0] - rp[0]) || (lp[1] - rp[1]);
  });
  for (const [, entry] of entries) {
    if (!entry || !entry.approved || !entry.answer) continue;
    const pats = entry.patterns || [];
    if (pats.some((p) => sensitivePatternMatches(label, p))) return String(entry.answer);
  }
  const directProfileFields = {
    work_authorization: ["authorized to work", "work authorization", "legally authorized", "eligible to work"],
    sponsorship: ["sponsorship", "require sponsorship", "visa sponsorship", "sponsor", "require visa"],
    salary: ["salary", "compensation", "desired salary", "salary expectation", "pay expectation"],
    relocation: ["relocation", "relocate", "willing to relocate", "open to relocate"],
    start_date: ["start date", "earliest start", "available to start"],
    citizenship: ["citizen", "citizenship", "us citizen", "u s citizen"],
    security_clearance: ["security clearance", "clearance", "active clearance"],
    legal_attestation: ["legal attestation", "i attest", "i certify", "true and correct", "background check", "i authorize", "arbitration agreement"],
    privacy_consent: ["privacy policy", "personal data", "process your personal data", "ai notetaker", "transcribe conversations"],
  };
  for (const [key, patterns] of Object.entries(directProfileFields)) {
    const value = CFG.profile && CFG.profile[key];
    if (value == null || typeof value === "object" || PLACEHOLDER_ANSWERS.has(norm(String(value)))) continue;
    if (patterns.some((pattern) => sensitivePatternMatches(label, pattern))) return String(value);
  }
  return null;
}

function approvedSensitiveEntryAnswer(profile, key) {
  const entry = (profile.sensitive_answers || {})[key];
  if (entry && entry.approved && entry.answer != null && String(entry.answer).trim() !== "") {
    return String(entry.answer);
  }
  return null;
}

function truthyAnswer(value) {
  return ["yes", "true", "1", "y"].includes(norm(value));
}

function requiresExternalApplicationPortal(label) {
  const n = norm(label);
  if (n.includes("constellation application form")) return true;
  if (n.includes("official hiring partner") && n.includes("application form")) return true;
  if (n.includes("do not need to submit") && n.includes("greenhouse application")) return true;
  return false;
}

function profileCompanySlug(profile) {
  const company = norm(profile.target_company || "");
  if (company) return company;
  const applicationUrl = String(profile._application_url || "").toLowerCase();
  const match = applicationUrl.match(/\/(?:job-board|boards)\/([^/?#]+)\//);
  if (match) return norm(match[1]);
  for (const token of ["lyft", "anthropic", "affirm", "airbnb", "coinbase"]) {
    if (applicationUrl.includes(token)) return token;
  }
  return "";
}

function workAuthorizationDropdownAnswer(label, profile) {
  const n = norm(label);
  if (!n.includes("work authorization") && !n.includes("authorized to work") && !n.includes("authorization to work") && !n.includes("right to work")) return null;
  const sponsorship = approvedSensitiveEntryAnswer(profile, "sponsorship")
    || String((profile.work_authorization_by_country || {}).requires_sponsorship || "");
  const authorization = matchSensitive(label)
    || approvedSensitiveEntryAnswer(profile, "work_authorization_current_country")
    || approvedSensitiveEntryAnswer(profile, "work_authorization_us");
  const inferredCompany = profileCompanySlug(profile).replace(/\b\w/g, (letter) => letter.toUpperCase());
  const company = String(profile.target_company || inferredCompany).trim();
  const companyPossessive = company ? `${company}'s ` : "";
  const sponsorshipField = n.includes("sponsor") || n.includes("sponsorship");
  const authorizationField = n.includes("work authorization") || n.includes("legally authorized") || n.includes("authorized to work") || n.includes("authorization to work") || n.includes("right to work");
  if (n.includes("unrestricted") && authorizationField && !sponsorshipField) {
    if (truthyAnswer(sponsorship)) return "No";
    if (truthyAnswer(authorization)) return "Yes";
  }
  if (authorizationField && !sponsorshipField && truthyAnswer(sponsorship) && n === "work authorization") {
    return `I require/will require ${companyPossessive}sponsorship to obtain work authorization in the country in which this position is based`;
  }
  if (authorizationField && !sponsorshipField && truthyAnswer(authorization)) {
    if (n.includes("for any employer") || n.includes("select one")) return "Yes";
    if (
      n.includes("country where this position is located") ||
      n.includes("country where the position is located") ||
      n.includes("country in which you are applying") ||
      n.includes("country which you are applying") ||
      n.includes("in the us") ||
      n.includes("in the u s") ||
      n.includes("in the united states")
    ) return "Yes";
    return "Yes, I am currently legally authorized to work in the country where the job is located.";
  }
  if ((sponsorshipField || !authorizationField) && truthyAnswer(sponsorship)) {
    if (n.includes("retain or extend") || n.includes("now or in the future") || n.includes("will you in the future") || n.includes("at any point in the future")) {
      if (n.includes("do you now") || n.includes("will you in the future")) return "Yes";
      if (n.includes("at any point in the future")) return "Yes";
      return "Yes, I will require immigration sponsorship in the future to legally work in the country where the job is located.";
    }
    return `I require/will require ${companyPossessive}sponsorship to obtain work authorization in the country in which this position is based`;
  }
  if (truthyAnswer(authorization)) {
    return "I am authorized to work for any employer in the country in which this position is based.";
  }
  if (authorization != null) {
    return "My status to work in the country in which this position is based is unknown.";
  }
  return null;
}

function otherCountriesLocationAnswer(label, profile) {
  const n = norm(label);
  if (!(
    n.includes("no suitable positions") &&
    (n.includes("u s") || n.includes("us") || n.includes("united states")) &&
    n.includes("open to positions in other countries")
  )) return null;
  const saved = findAnswer(label, (profile && profile.answers) || {});
  if (saved != null) return String(saved);
  return "No";
}

function legalSignatureValue(label, profile) {
  const n = norm(label);
  if (!n.includes("full name") || !n.includes("date")) return null;
  if (!n.includes("signature") && !n.includes("signify")) return null;
  const approved = matchSensitive(label) || matchSensitive("i certify true and complete");
  if (!truthyAnswer(approved)) return null;
  const name = String(profile.name || "").trim();
  if (!name) return null;
  const today = new Date();
  const mm = String(today.getMonth() + 1).padStart(2, "0");
  const dd = String(today.getDate()).padStart(2, "0");
  return `${name} ${mm}/${dd}/${today.getFullYear()}`;
}

function optionText(option) {
  if (option == null) return "";
  if (typeof option === "string") return option;
  return option.label || option.value || "";
}

function optionValue(option) {
  if (option == null) return "";
  if (typeof option === "string") return option;
  return option.value || option.label || "";
}

const LOCATION_STATE_NAMES = {
  al: "alabama", ak: "alaska", az: "arizona", ar: "arkansas", ca: "california",
  co: "colorado", ct: "connecticut", de: "delaware", fl: "florida", ga: "georgia",
  hi: "hawaii", id: "idaho", il: "illinois", "in": "indiana", ia: "iowa",
  ks: "kansas", ky: "kentucky", la: "louisiana", me: "maine", md: "maryland",
  ma: "massachusetts", mi: "michigan", mn: "minnesota", ms: "mississippi", mo: "missouri",
  mt: "montana", ne: "nebraska", nv: "nevada", nh: "new hampshire", nj: "new jersey",
  nm: "new mexico", ny: "new york", nc: "north carolina", nd: "north dakota", oh: "ohio",
  ok: "oklahoma", or: "oregon", pa: "pennsylvania", ri: "rhode island", sc: "south carolina",
  sd: "south dakota", tn: "tennessee", tx: "texas", ut: "utah", vt: "vermont", va: "virginia",
  wa: "washington", wv: "west virginia", wi: "wisconsin", wy: "wyoming", dc: "district of columbia",
};

function expandedLocationText(value) {
  return norm(value).split(" ").flatMap((token) => {
    if (token === "us" || token === "usa") return ["united", "states"];
    return (LOCATION_STATE_NAMES[token] || token).split(" ");
  }).join(" ");
}

function optionMatches(option, answer) {
  const optLabel = norm(optionText(option));
  const optValue = norm(optionValue(option));
  const wants = answerAliases(answer).map(norm).filter(Boolean);
  const want = wants[0] || "";
  if (!want || (!optLabel && !optValue)) return false;
  if (norm(answer) === "no" && (optLabel.includes("veteran") || optValue.includes("veteran"))) {
    return isNegativeVeteranOption(optLabel) || isNegativeVeteranOption(optValue);
  }
  const exactDemographicWants = new Set(["asian", "east asian", "asian not hispanic or latino", "man", "woman", "male", "female"]);
  if (wants.some((candidate) => (
    optLabel === candidate || optValue === candidate ||
    expandedLocationText(optLabel) === expandedLocationText(candidate) ||
    expandedLocationText(optValue) === expandedLocationText(candidate)
  ))) return true;
  if (wants.some((candidate) => exactDemographicWants.has(candidate) && optLabel.startsWith(candidate + " "))) return true;
  const fuzzyWants = wants.filter((candidate) => !exactDemographicWants.has(candidate));
  const genericOption = new Set(["other", "no answer", "select", "select one"]);
  if (!genericOption.has(optLabel) && fuzzyWants.some((candidate) => optLabel.length >= 3 && (optLabel.includes(candidate) || candidate.includes(optLabel)))) return true;
  if (!genericOption.has(optValue) && fuzzyWants.some((candidate) => optValue.length >= 3 && (optValue.includes(candidate) || candidate.includes(optValue)))) return true;
  const labelTokens = new Set(optLabel.split(" ").filter(Boolean));
  const valueTokens = new Set(optValue.split(" ").filter(Boolean));
  return fuzzyWants.some((candidate) => {
    const wantTokens = candidate.split(" ").filter(Boolean);
    return wantTokens.length > 0 && wantTokens.every((token) => labelTokens.has(token) || valueTokens.has(token));
  });
}

function desiredLocationValues(profile) {
  const values = [];
  for (const raw of profile.desired_locations || []) {
    if (typeof raw === "string" && raw.trim()) values.push(raw.trim());
  }
  const answers = profile.answers || {};
  for (const key of ["Where would you like to work?", "Preferred location", "Desired location"]) {
    const raw = answers[key];
    if (typeof raw === "string" && raw.trim()) {
      values.push(...raw.split(/[,;/|]/).map((item) => item.trim()).filter(Boolean));
    }
  }
  return values;
}

function locationsCompatible(option, desired) {
  const aliases = {
    "new york city": "new york",
    "nyc": "new york",
  };
  let optionTextValue = expandedLocationText(option);
  let desiredText = expandedLocationText(desired);
  optionTextValue = aliases[optionTextValue] || optionTextValue;
  desiredText = aliases[desiredText] || desiredText;
  return Boolean(optionTextValue && desiredText && (
    optionTextValue === desiredText ||
    optionTextValue.includes(desiredText) ||
    desiredText.includes(optionTextValue)
  ));
}

function looksLikeLocationCheckboxOption(label) {
  const n = norm(label);
  if (!n) return false;
  if (n.includes("remote") && (n.includes("us") || n.includes("usa") || n.includes("united states"))) return true;
  const tokens = new Set(n.split(" ").filter(Boolean));
  if (tokens.size > 8) return false;
  if ([...tokens].some((token) => Object.prototype.hasOwnProperty.call(LOCATION_STATE_NAMES, token))) return true;
  return Object.values(LOCATION_STATE_NAMES).some((state) => n.includes(state));
}

function officeLocationCheckboxPlan(f, profile) {
  const label = String(f.label || "");
  const combined = norm([f.label, f.section, f.ariaLabel, f.ariaDescription, f.name, f.id].filter(Boolean).join(" "));
  if (!(
    combined.includes("which office location") ||
    combined.includes("office locations") ||
    combined.includes("location s are you interested") ||
    looksLikeLocationCheckboxOption(label)
  )) return null;
  const option = norm(label);
  if (!option) return null;
  if (option.includes("remote") && (option.includes("us") || option.includes("united states") || option.includes("usa"))) {
    return { action: "check" };
  }
  if (desiredLocationValues(profile).some((desired) => locationsCompatible(label, desired))) {
    return { action: "check" };
  }
  return { action: "skip", reason: "office location option not selected from candidate preferences", blocking: false };
}

function preferredOfficeLocationOption(f, profile) {
  const label = norm(f.label || "");
  if (!(
    (label.includes("which office location") || label.includes("preferred office location")) &&
    (label.includes("prefer") || label.includes("would you"))
  )) return null;
  const options = f.options || [];
  if (!options.length) return null;
  const answers = profile.answers || {};
  const preference = norm([
    profile.target_location,
    ...(profile.desired_locations || []),
    answers["Where would you like to work?"],
    answers["Please indicate all of the locations that you would be interested in relocating to for this position."],
    profile.location,
  ].filter(Boolean).join(" "));
  let officeKeywords = ["new york"];
  if (["new york", "nyc", "jersey city", "new jersey"].some((token) => preference.includes(token))) {
    officeKeywords = ["new york", "ny"];
  } else if (["san francisco", "bay area", "sf"].some((token) => preference.includes(token))) {
    officeKeywords = ["san francisco", "sf"];
  }
  return options.find((option) => {
    const optionLabel = norm(optionText(option));
    return officeKeywords.some((keyword) => optionLabel.includes(keyword));
  }) || null;
}

function preferredOfficeLocationAnswer(f, profile) {
  const label = norm(f.label || "");
  if (!(
    (label.includes("which office location") || label.includes("preferred office location")) &&
    (label.includes("prefer") || label.includes("would you"))
  )) return null;
  const answers = profile.answers || {};
  const primaryPreference = norm([
    ...(profile.desired_locations || []),
    answers["Where would you like to work?"],
    answers["Please indicate all of the locations that you would be interested in relocating to for this position."],
    profile.location,
  ].filter(Boolean).join(" "));
  if (["new york", "nyc", "jersey city", "new jersey"].some((token) => primaryPreference.includes(token))) return "New York";
  if (["san francisco", "bay area", "sf"].some((token) => primaryPreference.includes(token))) return "San Francisco";
  const targetLocation = norm(profile.target_location);
  if (targetLocation.includes("new york")) return "New York";
  if (targetLocation.includes("san francisco")) return "San Francisco";
  return "New York";
}

function isNegativeVeteranOption(optionTextValue) {
  const n = norm(optionTextValue);
  if (!n || !n.includes("veteran")) return false;
  if (n.includes("identify as a veteran") || n.includes("i am a veteran")) return false;
  return n === "no" || n.includes("not a veteran") || n.includes("not a protected veteran") || n.includes("non veteran");
}

function findOption(options, answer) {
  return (options || []).find((option) => optionMatches(option, answer)) || null;
}

function isSourceQuestion(label) {
  const n = norm(label);
  return n.includes("how did you hear") || n.includes("where did you hear") || n.includes("where have you learned about");
}

function isCompanyWebsiteAnswer(answer) {
  return new Set([
    "company website", "company site", "company careers", "company career site", "career site", "career website", "careers website", "careers site",
  ]).has(norm(answer));
}

function matchingOptions(field, answer) {
  const matches = (field.options || []).filter((option) => optionMatches(option, answer));
  if (matches.length || !isSourceQuestion(field.label) || !isCompanyWebsiteAnswer(answer)) return matches;
  return (field.options || []).filter((option) => norm(optionText(option)) === "other");
}

function isNegativeAnswer(answer) {
  return ["no", "false", "none", "n/a", "na", "do not consent", "i do not consent"].includes(norm(answer));
}

function answerAliases(answer) {
  const raw = String(answer || "");
  const aliases = [raw];
  aliases.push(...graduationDateAliases(raw));
  const n = norm(raw);
  if ([
    "prefer not to say", "prefer not to answer", "decline", "decline to answer",
    "i don t wish to answer", "i do not wish to answer", "i do not want to answer",
  ].includes(n)) {
    aliases.push(
      "Decline to self-identify",
      "Decline To Self Identify",
      "I decline to self-identify",
      "I don't wish to answer",
      "I do not wish to answer",
      "I do not want to answer"
    );
  }
  if (["east asian", "asian"].includes(n)) {
    aliases.push("Asian", "East Asian", "Asian (Not Hispanic or Latino)");
  }
  if (n === "male") aliases.push("Man");
  if (n === "man") aliases.push("Male");
  if (n === "female") aliases.push("Woman");
  if (n === "linkedin") aliases.push("LinkedIn Jobs");
  if (["yes", "confirmed", "agree", "i agree", "acknowledge", "i acknowledge"].includes(n)) {
    aliases.push("I Agree", "I Acknowledge", "Yes, I agree", "Yes, I acknowledge", "Confirmed");
  }
  if (["master s degree", "masters degree", "master degree"].includes(n)) {
    aliases.push("Master's Degree", "Master Degree");
  }
  if (["within a month", "in one month", "one month", "immediately", "as soon as possible", "asap"].includes(n)) {
    aliases.push(
      "Immediately/next few months, full-time",
      "Immediately / next few months, full-time",
      "Immediately/next few months",
      "Immediately"
    );
  }
  if (n === "computer science") {
    aliases.push("Computer Science", "Computer and Information Sciences", "Computer and Information Sciences, General");
  }
  if (["united states", "united states of america", "usa", "us", "u s", "u s a"].includes(n)) {
    aliases.push(
      "United States",
      "United States +1",
      "United States of America",
      "United States of America +1",
      "USA",
      "USA +1",
      "US",
      "US +1",
      "U.S.",
      "U.S.A."
    );
  }
  if (["+1", "1", "united states of america (+1)", "united states (+1)"].includes(n)) {
    aliases.push("+1", "United States +1", "United States (+1)", "United States of America (+1)", "USA (+1)");
  }
  if (n === "no") {
    aliases.push(
      "I'm not open to other locations",
      "Not open to other locations",
      "I am not a veteran",
      "I am not a protected veteran",
      "Not a veteran",
      "Not a protected veteran",
      "I am not disabled",
      "I do not have a disability",
      "No, I do not have a disability",
      "No, I don't have a disability",
      "No, I don't have a disability and have not had one in the past",
      "No - I do not consent to receiving text messages"
    );
  }
  if (["company website", "company site", "company careers", "company career site", "career site", "career website", "careers website", "careers site"].includes(n)) {
    aliases.push(
      "Website",
      "Company Website",
      "Corporate Website",
      "Career Site",
      "Careers Website",
      "Career Website",
      "Careers Page",
      "Company Careers",
      "Careers Site"
    );
  }
  if (n === "primary") {
    aliases.push("Cell", "Mobile");
  }
  // Normalize verbose LLM-generated yes/no intents back to simple option labels.
  if (n.startsWith("yes") && n.length > 3) {
    aliases.push("Yes");
  }
  if (n.startsWith("no") && n.length > 2) {
    aliases.push("No");
  }
  // Work authorization / sponsorship intents.
  if (n.includes("authorized") && n.includes("work") && n.includes("any employer")) {
    aliases.push(
      "I am authorized to work for any employer",
      "Authorized to work",
      "Yes"
    );
  }
  if (n.includes("sponsor")) {
    aliases.push(
      "I require sponsorship",
      "I will require sponsorship",
      "Sponsorship required",
      "Yes"
    );
  }
  // Pronoun aliases.
  if (n.includes("he") && n.includes("him")) aliases.push("He / Him");
  if (n.includes("she") && n.includes("her")) aliases.push("She / Her");
  if (n.includes("they") && n.includes("them")) aliases.push("They / Them");
  return aliases;
}

function firstProfileEntry(entries) {
  if (!Array.isArray(entries)) return null;
  return entries.find((entry) => entry && typeof entry === "object") || null;
}

function currentWorkValue(profile, key) {
  const current = (profile.work_history || []).find((entry) => entry && entry.current) || firstProfileEntry(profile.work_history);
  if (!current) return null;
  if (current[key]) return current[key];
  if (current.current && key === "end_month") return MONTH_NAMES[(new Date()).getMonth() + 1];
  if (current.current && key === "end_year") return String((new Date()).getFullYear());
  return current[key] || null;
}

function currentEducationValue(profile, key) {
  const education = firstProfileEntry(profile.education);
  return education && education[key] ? education[key] : null;
}

const MONTH_NAMES = {
  1: "January", 2: "February", 3: "March", 4: "April",
  5: "May", 6: "June", 7: "July", 8: "August",
  9: "September", 10: "October", 11: "November", 12: "December",
};

function graduationDateAliases(raw) {
  const match = String(raw || "").match(/\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(20\d{2}|19\d{2})\b/i);
  if (!match) return [];
  const month = Number(Object.entries(MONTH_NAMES).find(([, name]) => norm(name) === norm(match[1]))?.[0] || 0);
  const year = Number(match[2]);
  if (!month || !year) return [];
  const aliases = [];
  const today = new Date();
  if (year < today.getFullYear() || (year === today.getFullYear() && month < today.getMonth() + 1)) {
    aliases.push("Already graduated");
  }
  if (month <= 4) aliases.push(`Jan - April ${year}`);
  else if (month <= 8) aliases.push(`May - Aug ${year}`, `May - August ${year}`);
  else aliases.push(`Sept - Dec ${year}`, `September - December ${year}`);
  return aliases;
}

function entryDatePart(entry, boundary, part) {
  const explicit = entry && entry[`${boundary}_${part}`];
  let value = explicit == null || String(explicit).trim() === "" ? null : String(explicit).trim();
  if (value == null) {
    const raw = String((entry && entry[`${boundary}_date`]) || "").trim();
    const match = raw.match(/^(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?$/);
    if (!match) return null;
    value = part === "month" ? match[2] : (part === "day" ? match[3] : match[1]);
  }
  if (value == null || String(value).trim() === "") return null;
  if (part === "year") return value;
  if (part === "day") {
    const day = Number(value);
    return Number.isInteger(day) && day >= 1 && day <= 31 ? String(day).padStart(2, "0") : value;
  }
  const month = Number(value);
  return Number.isInteger(month) && MONTH_NAMES[month] ? MONTH_NAMES[month] : value;
}

function educationDatePart(profile, boundary, part) {
  return entryDatePart(firstProfileEntry(profile.education) || {}, boundary, part);
}

function educationFieldForLevel(profile, level) {
  const wanted = norm(level);
  const entry = (profile.education || []).find((item) => item && typeof item === "object" && norm(item.degree).includes(wanted));
  return entry && entry.field ? String(entry.field) : null;
}

function degreeFieldQuestion(label) {
  const n = norm(label);
  if (n.includes("bachelor") && (n.includes("field") || n.includes("major"))) return "bachelor";
  if (n.includes("master") && (n.includes("field") || n.includes("major"))) return "master";
  return null;
}

function privacyPreservingPronounOption(field) {
  if (!norm(field && field.label).includes("pronouns")) return null;
  for (const preferred of ["use name only", "prefer not to say", "not represented here"]) {
    for (const option of (field && field.options) || []) {
      if (norm(optionText(option)) === preferred) return option;
    }
  }
  return null;
}

function educationEndDateValue(profile) {
  const raw = currentEducationValue(profile, "end_date");
  if (!raw) return null;
  const value = String(raw);
  const match = value.match(/^(\d{4})-(\d{2})$/);
  if (!match) return value;
  return `${MONTH_NAMES[Number(match[2])] || match[2]} ${match[1]}`;
}

function yearsExperienceValue(profile) {
  return profile.years_experience || profile.relevant_years_experience || profile.post_college_years_experience || null;
}

function zeroBasedProfessionalExperienceRangeAnswer(profile) {
  const rawYears = yearsExperienceValue(profile);
  const nums = Array.from(String(rawYears || "").matchAll(/\d+(?:\.\d+)?/g)).map((match) => Number(match[0]));
  if (!nums.length) return null;
  const years = Math.max(...nums);
  if (years <= 2) return "0-2 years";
  if (years <= 4) return "3-4 years";
  if (years <= 10) return "5-10 years";
  return "10+ years";
}

function relevantProfessionalExperienceRangeAnswer(profile) {
  const rawYears = yearsExperienceValue(profile);
  const nums = Array.from(String(rawYears || "").matchAll(/\d+(?:\.\d+)?/g)).map((match) => Number(match[0]));
  if (!nums.length) return null;
  const years = Math.max(...nums);
  if (years <= 2) return "1-2 years";
  if (years <= 5) return "3-5 years";
  if (years <= 8) return "6-8 years";
  return "8+";
}

function hasFullTimeSoftwareEngineeringExperience(profile) {
  for (const entry of profile.work_history || []) {
    if (!entry || typeof entry !== "object") continue;
    const roleText = norm([entry.title, entry.employment_type, entry.type].filter(Boolean).join(" "));
    if (!roleText || roleText.includes("intern")) continue;
    if (!roleText.includes("full time") && !roleText.includes("fulltime")) continue;
    if ([
      "software engineer",
      "software engineering",
      "sde",
      "backend engineer",
      "full stack engineer",
      "fullstack engineer",
    ].some((term) => roleText.includes(term))) return true;
  }
  return false;
}

function desiredSalaryRangeValue(profile) {
  const raw = String(profile.minimum_expected_salary || ((profile.answers || {})["What is your minimum expected salary?"]) || "");
  const match = raw.match(/(\d[\d,]*(?:\.\d+)?)\s*(k)?/i);
  if (!match) return null;
  let minimum = Number(match[1].replace(/,/g, ""));
  if (match[2]) minimum *= 1000;
  const ranges = [
    [25000, "$25,000 to $50,000"],
    [50001, "50,001 to 75,000"],
    [75001, "75,001 to 100,000"],
    [100001, "100,001 to 125,000"],
    [125001, "125,001 to 150,000"],
    [150001, "150,001 and above"],
  ];
  const selected = ranges.find(([lower]) => lower >= minimum);
  return String((selected || ranges[ranges.length - 1])[1]);
}

function demographicAnswer(label, profile) {
  const demographics = profile.demographics || {};
  const n = norm(label);
  if (!demographics || !n) return null;
  if (n.includes("transgender")) {
    const explicit = demographics.transgender || profile.transgender;
    if (explicit) return String(explicit);
    return "I don't wish to answer";
  }
	  if (n.includes("gender") || n.includes("sex")) {
	    const gender = demographics.gender || null;
	    if (norm(gender) === "male") return "Man";
	    if (norm(gender) === "female") return "Woman";
	    return gender;
	  }
  if (n.includes("hispanic") || n.includes("latino")) {
    const explicit = demographics.hispanic_latino || demographics.hispanic || demographics.latino;
    if (explicit) return explicit;
    const rawEthnicity = demographics.ethnicity || demographics.race || "";
    if (isDeclineAnswer(rawEthnicity)) return String(rawEthnicity);
    const ethnicity = norm(rawEthnicity);
    if (["asian", "east asian", "south asian", "southeast asian", "asian not hispanic or latino"].includes(ethnicity)) return "No";
    if (["hispanic", "latino", "hispanic or latino"].includes(ethnicity)) return "Yes";
    return null;
  }
  if (n.includes("ethnicity")) {
    return demographics.ethnicity || demographics.hispanic_latino || null;
  }
  if (n.includes("race")) return demographics.race || null;
  if (n.includes("veteran")) return demographics.veteran || null;
  if (n.includes("disability") || n.includes("disabled")) return demographics.disability || null;
  return null;
}

function isDemographicLabel(label) {
  const n = norm(label);
  return ["gender", "sex", "ethnicity", "hispanic", "latino", "race", "veteran", "disability"].some((token) => n.includes(token));
}

function isDeclineAnswer(answer) {
  return [
    "prefer not to say", "prefer not to answer", "decline", "decline to answer",
    "i don t wish to answer", "i do not wish to answer", "i do not want to answer",
    "decline to self identify", "i decline to self identify",
  ].includes(norm(answer));
}

function semanticFieldContext(fieldOrLabel) {
  if (!fieldOrLabel || typeof fieldOrLabel !== "object") {
    const text = norm(fieldOrLabel);
    return { text, section: "", primaryLabel: text, evidence: text ? [{ text, weight: 1 }] : [] };
  }
  const parts = [
    fieldOrLabel.label, fieldOrLabel.id, fieldOrLabel.name, fieldOrLabel.section,
    fieldOrLabel.ariaLabel, fieldOrLabel.ariaDescription, fieldOrLabel.placeholder,
    fieldOrLabel.autocomplete,
  ].filter(Boolean);
  const evidence = [
    [fieldOrLabel.label, 1],
    [fieldOrLabel.ariaLabel || fieldOrLabel.aria_label, 0.95],
    [[fieldOrLabel.id, fieldOrLabel.name].filter(Boolean).join(" "), 0.82],
    [fieldOrLabel.placeholder, 0.72],
    [fieldOrLabel.ariaDescription || fieldOrLabel.aria_description, 0.45],
  ].map(([value, weight]) => ({ text: norm(value), weight })).filter((item) => item.text);
  const explicitSection = norm(fieldOrLabel.section || "");
  const structural = norm([fieldOrLabel.id, fieldOrLabel.name, fieldOrLabel.automationId].filter(Boolean).join(" "));
  const section = explicitSection || (
    structural.includes("education") || structural.includes("academic") ? "education" :
      (["employment", "work history", "work experience"].some((token) => structural.includes(token)) ? "work" : "")
  );
  return {
    text: norm(parts.join(" ")),
    section,
    primaryLabel: norm(fieldOrLabel.label || ""),
    evidence,
  };
}

function semanticHasPhrase(text, phrase) {
  const normalizedPhrase = norm(phrase);
  return Boolean(normalizedPhrase) && (` ${text} `).includes(` ${normalizedPhrase} `);
}

function semanticRuleScore(rule, sourceWeight) {
  const allSpecificity = (rule.all || []).reduce((total, token) => total + norm(token).split(" ").filter(Boolean).length, 0);
  const anySpecificity = Math.max(0, ...(rule.any || []).map((token) => norm(token).split(" ").filter(Boolean).length));
  return Number(rule.confidence == null ? 1 : rule.confidence) * sourceWeight + allSpecificity * 0.09 + anySpecificity * 0.04;
}

function semanticForField(fieldOrLabel) {
  const context = semanticFieldContext(fieldOrLabel);
  if (!context.text) return null;
  if (fieldOrLabel && typeof fieldOrLabel === "object") {
    const autocomplete = norm(fieldOrLabel.autocomplete || "");
    const autocompleteRules = CFG.fieldAutocompleteSemantics || {};
    const key = autocompleteRules[autocomplete] || autocomplete.split(" ")
      .map((token) => autocompleteRules[token])
      .find(Boolean);
    if (key) return { key, text: context.text, section: context.section, confidence: 1 };
  }
  const candidates = [];
  for (const [index, rule] of (CFG.fieldSemantics || []).entries()) {
    if (rule.section && rule.section !== context.section) continue;
    if ((rule.none || []).some((token) => semanticHasPhrase(context.text, token))) continue;
    for (const source of context.evidence || []) {
      if (rule.maxTokens && source.text.split(" ").length > rule.maxTokens) continue;
      if (rule.maxTokens && context.primaryLabel && context.primaryLabel.split(" ").length > rule.maxTokens) continue;
      if ((rule.all || []).some((token) => !semanticHasPhrase(source.text, token))) continue;
      if ((rule.any || []).length && !(rule.any || []).some((token) => semanticHasPhrase(source.text, token))) continue;
      candidates.push({ rule, index, score: semanticRuleScore(rule, source.weight) });
      break;
    }
  }
  if (!candidates.length) return null;
  candidates.sort((left, right) => right.score - left.score || left.index - right.index);
  const best = candidates[0].rule;
  return { key: best.key, text: context.text, section: context.section, confidence: best.confidence == null ? 1 : best.confidence };
}

function currentWorkEntry(profile) {
  return (profile.work_history || []).find((entry) => entry && entry.current) || firstProfileEntry(profile.work_history) || {};
}

function semanticValue(semantic, profile) {
  if (!semantic) return null;
  const key = semantic.key;
  const text = semantic.text || "";
  if (key === "identity.full_name") return profile.name || null;
  if (key === "identity.first_name") return profile.first_name || (profile.name ? profile.name.split(" ")[0] : null);
  if (key === "identity.last_name") return profile.last_name || (profile.name ? profile.name.split(" ").slice(1).join(" ") : null);
  if (key === "identity.preferred_name") return profile.preferred_name || profile.first_name || (profile.name ? profile.name.split(" ")[0] : null);
  if (key === "identity.pronunciation") return profile.name_pronunciation || profile.pronunciation || null;
  if (key === "contact.email") return profile.email || null;
  if (key === "contact.phone") {
    if (text.includes("phone number") || text.includes("phonenumber")) {
      let digits = String(profile.phone || "").replace(/\D+/g, "");
      if (digits.length === 11 && digits.startsWith("1")) digits = digits.slice(1);
      return digits || profile.phone || null;
    }
    return profile.phone || null;
  }
  if (key === "contact.phone.country_code") return inferPhoneCountryCode(profile);
  if (key === "contact.phone.extension") return profile.phone_extension || null;
  if (key === "contact.phone.type") return profile.phone_type || "Mobile";
  if (key === "link.linkedin") return profile.linkedin || null;
  if (key === "link.github") return profile.github || null;
  if (key === "link.portfolio") return profile.portfolio || profile.website || null;
  if (key === "link.website") return profile.website || profile.portfolio || null;
  if (key === "address.line1") return profile.address_line1 || profile.street_address || null;
  if (key === "address.line2") return profile.address_line2 || null;
  if (key === "address.city") return profile.city || cityFromLocation(profile.location);
  if (key === "address.region") return profile.region || profile.state || null;
  if (key === "address.country") return mapCountryValue(profile);
  if (key === "employment.eligible_country") return mapCountryValue(profile);
  if (key === "address.postal_code") return profile.postal_code || profile.zip || null;
  if (key === "location.current") return profile.location || profile.city || null;
  if (key === "work.current.company") return currentWorkValue(profile, "company");
  if (key === "work.current.title") return currentWorkValue(profile, "title");
  if (key === "work.current.description") return currentWorkValue(profile, "description");
  if (key.startsWith("work.") && (key.endsWith(".month") || key.endsWith(".day") || key.endsWith(".year"))) {
    const [, boundary, part] = key.split(".");
    return entryDatePart(currentWorkEntry(profile), boundary, part);
  }
  if (key.startsWith("work.") && key.endsWith(".date")) {
    const [, boundary] = key.split(".");
    return currentWorkValue(profile, `${boundary}_date`);
  }
  if (key === "career.years_experience") return yearsExperienceValue(profile);
  if (key === "education.school") return currentEducationValue(profile, "school");
  if (key === "education.degree") return currentEducationValue(profile, "degree");
  if (key === "education.field") return currentEducationValue(profile, "field");
  if (key === "education.gpa") return currentEducationValue(profile, "gpa");
  if (key.startsWith("education.") && (key.endsWith(".month") || key.endsWith(".day") || key.endsWith(".year"))) {
    let [, boundary, part] = key.split(".");
    if (boundary === "graduation") boundary = "end";
    return educationDatePart(profile, boundary, part);
  }
  if (key.startsWith("education.") && key.endsWith(".date")) {
    let [, boundary] = key.split(".");
    if (boundary === "graduation") return profile.graduation_date || educationEndDateValue(profile);
    return currentEducationValue(profile, `${boundary}_date`);
  }
  return null;
}

function adapterFieldCandidates(field) {
  if (!field || typeof field !== "object") return [];
  const tag = String(field.tag || "").trim().toLowerCase();
  const id = String(field.id || "").trim();
  const name = String(field.name || "").trim();
  const automationId = String(field.automationId || field.automation_id || "").trim();
  const candidates = [];
  if (id) candidates.push(`#${id}`);
  if (name) {
    candidates.push(`[name="${name}"]`);
    if (tag) candidates.push(`${tag}[name="${name}"]`);
  }
  if (automationId) candidates.push(`[data-automation-id="${automationId}"]`);
  return candidates;
}

function adapterFieldProfileKey(fieldOrLabel) {
  if (!fieldOrLabel || typeof fieldOrLabel !== "object") return null;
  const candidates = adapterFieldCandidates(fieldOrLabel);
  if (!candidates.length) return null;
  const preferred = detectATS(CFG.applicationUrl);
  const adapters = Array.isArray(CFG.atsAdapters) ? CFG.atsAdapters : [];
  const ordered = [
    ...adapters.filter((adapter) => adapter && adapter.name === preferred),
    ...adapters.filter((adapter) => adapter && adapter.name !== preferred),
  ];
  for (const adapter of ordered) {
    const fieldMap = (adapter && adapter.field_map) || {};
    for (const candidate of candidates) {
      if (fieldMap[candidate]) return fieldMap[candidate];
    }
  }
  return null;
}

function adapterProfileValue(key, profile) {
  if (!key) return null;
  const answers = profile.answers || {};
  if (key === "full_name") return profile.name || null;
  if (key === "first_name") return profile.first_name || (profile.name ? profile.name.split(" ")[0] : null);
  if (key === "last_name") return profile.last_name || (profile.name ? profile.name.split(" ").slice(1).join(" ") : null);
  if (key === "preferred_name") return profile.preferred_name || profile.first_name || (profile.name ? profile.name.split(" ")[0] : null);
  if (key === "email") return profile.email || null;
  if (key === "phone") return profile.phone || null;
  if (key === "phone_country_code") return inferPhoneCountryCode(profile);
  if (key === "phone_device_type") return workdayPhoneDeviceTypeAnswer(key, profile) || profile.phone_type || "Mobile";
  if (key === "linkedin_url") return profile.linkedin || null;
  if (key === "github_url") return profile.github || null;
  if (key === "twitter_url") return profile.twitter || null;
  if (key === "website") return profile.website || profile.portfolio || null;
  if (key === "location") return profile.location || profile.city || null;
  if (key === "country") return mapCountryValue(profile);
  if (key === "state") return profile.region || profile.state || null;
  if (key === "city") return profile.city || cityFromLocation(profile.location);
  if (key === "address_line_1") return profile.address_line1 || profile.street_address || profile.address || answers["Address"] || null;
  if (key === "postal_code") return profile.postal_code || profile.zip || answers["Postal Code"] || null;
  if (key === "current_company") return currentWorkValue(profile, "company");
  if (key === "cover_letter") return profile.cover_letter || null;
  if (key === "resume_text") return profile.resume_text || profile.resume || null;
  if (key === "additional_info") return ((profile.answers || {})["Additional Information"]) || null;
  return null;
}

function mapTextValue(fieldOrLabel, profile) {
  const label = fieldOrLabel && typeof fieldOrLabel === "object"
    ? [fieldOrLabel.label, fieldOrLabel.id, fieldOrLabel.name, fieldOrLabel.section, fieldOrLabel.ariaLabel, fieldOrLabel.ariaDescription, fieldOrLabel.placeholder, fieldOrLabel.autocomplete].filter(Boolean).join(" ")
    : fieldOrLabel;
  const n = norm(label);
  const compact = n.replace(/\s+/g, "");
  const today = new Date();
  if (!n) return null;
  const workdayPhoneType = workdayPhoneDeviceTypeAnswer(fieldOrLabel, profile);
  if (workdayPhoneType) return workdayPhoneType;
  if (n.includes("suffix")) return profile.suffix || (profile.answers || {})["Suffix"] || null;
  if (n.includes("middle name")) return profile.middle_name || (profile.answers || {})["Middle Name"] || null;
  if (n.includes("address line 2")) return profile.address_line2 || (profile.answers || {})["Address 2"] || null;
  if (n === "county" || n.endsWith(" county")) return profile.county || (profile.answers || {})["County"] || null;
  if (["salary", "compensation", "pay expectation", "salary expectation"].some((token) => n.includes(token))) return null;
  if (n.includes("state") && (n.includes("currently reside") || n.includes("current residence") || n.includes("reside in"))) return profile.region || profile.state || null;
  if (n === "state" || n.includes("state province") || n.includes("province") || compact.includes("countryregion")) return profile.region || profile.state || null;
  const semantic = semanticForField(fieldOrLabel);
  const semanticMapped = semanticValue(semantic, profile);
  if (semanticMapped !== null && semanticMapped !== undefined && semanticMapped !== "") return semanticMapped;
  const adapterMapped = adapterProfileValue(adapterFieldProfileKey(fieldOrLabel), profile);
  if (adapterMapped !== null && adapterMapped !== undefined && adapterMapped !== "") return adapterMapped;
  if (compact.includes("cname")) return profile.name || null;
  if (compact.includes("cemail")) return profile.email || null;
  if (compact.includes("cphonenumber")) return profile.phone || null;
  if (compact.includes("caddress")) return profile.address_line1 || profile.street_address || null;
  if (compact.includes("ccoverletter")) return profile.cover_letter || null;
  if (n.includes("phone device type") || compact.includes("phonetype")) return "Mobile";
  if (n.includes("extension")) return profile.phone_extension || null;
  if (n.includes("email") || n.includes("e-mail")) return profile.email || null;
  if (n.includes("country phone code") || n.includes("phone country code")) {
    const code = inferPhoneCountryCode(profile);
    return code === "+1" ? "United States of America (+1)" : code;
  }
  if (n.includes("phone number")) {
    let digits = String(profile.phone || "").replace(/\D+/g, "");
    if (digits.length === 11 && digits.startsWith("1")) digits = digits.slice(1);
    return digits || profile.phone || null;
  }
  if (n.includes("phone") || n.includes("mobile") || n.includes("telephone") || n.includes("contact number")) return profile.phone || null;
  if (n.includes("linkedin")) return profile.linkedin || null;
  if (n.includes("github")) return profile.github || null;
  if (n.includes("portfolio")) return profile.portfolio || profile.website || null;
  if (n.includes("website") || n.includes("personal site") || n.includes("homepage")) return profile.website || profile.portfolio || null;
  if (n.includes("cover letter")) return profile.cover_letter || null;
  if (hasWholePhrase(n, "country") && n.split(" ").length <= 8) return mapCountryValue(profile);
  if (n === "address" || n === "address 1" || compact.includes("resumatoraddressvalue") || n.includes("address line 1") || n.includes("street address") || n.includes("mailing address")) return profile.address_line1 || profile.street_address || profile.address || (profile.answers || {})["Address"] || null;
  if (n.includes("address line 2")) return profile.address_line2 || (profile.answers || {})["Address 2"] || null;
  if (n.includes("postal code") || n.includes("zip code") || n === "zip") return profile.postal_code || profile.zip || (profile.answers || {})["Postal Code"] || null;
  if (hasWholePhrase(n, "city")) return profile.city || cityFromLocation(profile.location);
  if (hasWholePhrase(n, "location") || hasWholePhrase(n, "address")) return profile.location || profile.city || null;
  if (n.includes("first name")) return profile.first_name || (profile.name ? profile.name.split(" ")[0] : null);
  if (n.includes("last name")) return profile.last_name || (profile.name ? profile.name.split(" ").slice(1).join(" ") : null);
  if (n.includes("preferred name")) return profile.preferred_name || profile.first_name || (profile.name ? profile.name.split(" ")[0] : null);
  if (n.includes("pronunciation") || n.includes("pronounce")) return profile.name_pronunciation || profile.pronunciation || null;
  if (n.includes("legal name") || n.includes("full name") || n.includes("your name") || n.includes("name")) return profile.name || null;
  if (compact.includes("selfidentifieddisabilitydata") && n.includes("name")) return profile.name || null;
  if (n.includes("date of birth") || n.includes("birthday")) return profile.birthday || null;
  if (n === "date") return `${String(today.getMonth() + 1).padStart(2, "0")}/${String(today.getDate()).padStart(2, "0")}/${today.getFullYear()}`;
  if (compact.includes("datesignedon") && n.includes("month")) return String(today.getMonth() + 1).padStart(2, "0");
  if (compact.includes("datesignedon") && n.includes("day")) return String(today.getDate()).padStart(2, "0");
  if (compact.includes("datesignedon") && n.includes("year")) return String(today.getFullYear());
  if (compact.includes("datesignedon") || n.includes("date signed") || n.includes("date of signature")) return formatNativeDate(today);
  if (
    n.includes("currently based in any of these countries") ||
    (n.includes("countries where we are accepting applications") && n.includes("currently based"))
  ) {
    return targetApplicationCountry(profile) || inferCountry(profile);
  }
  if (n.includes("currently located") || n.includes("current location") || n.includes("currently based")) return profile.location || profile.city || null;
  if (n.includes("current company") || n.includes("current employer") || n === "company" || compact.includes("companyname")) return currentWorkValue(profile, "company");
  if (n.includes("current title") || n.includes("current role") || n.includes("current position") || n.includes("job title") || compact.includes("jobtitle")) return currentWorkValue(profile, "title");
  if (n.includes("role description") || compact.includes("roledescription")) return currentWorkValue(profile, "description");
  if (compact.includes("education")) {
    if (n.includes("start") && n.includes("month")) return educationDatePart(profile, "start", "month");
    if (n.includes("start") && n.includes("year")) return educationDatePart(profile, "start", "year");
    if (n.includes("end") && n.includes("month")) return educationDatePart(profile, "end", "month");
    if (n.includes("end") && n.includes("year")) return educationDatePart(profile, "end", "year");
  }
  if (compact.includes("startdate") && n.includes("month")) return currentWorkValue(profile, "start_month");
  if (compact.includes("startdate") && n.includes("year")) return currentWorkValue(profile, "start_year");
  if (compact.includes("enddate") && n.includes("month")) return currentWorkValue(profile, "end_month");
  if (compact.includes("enddate") && n.includes("year")) return currentWorkValue(profile, "end_year");
  if (n.includes("years") && n.includes("experience")) return yearsExperienceValue(profile);
  if (n.includes("graduation date") || n.includes("anticipated graduation")) return profile.graduation_date || educationEndDateValue(profile);
  if (n.includes("preferred programming language")) return profile.preferred_programming_language || "Python";
  if (n.split(" ").length <= 14 && ["university", "school", "college", "institution"].some((term) => hasWholePhrase(n, term))) return currentEducationValue(profile, "school");
  if (n.includes("degree")) return currentEducationValue(profile, "degree");
  if (n.includes("field of study") || n.includes("major")) return currentEducationValue(profile, "field");
  if (compact.includes("gradeaverage") || n.includes("gpa")) return currentEducationValue(profile, "gpa");
  if (compact.includes("firstyearattended")) return currentEducationValue(profile, "start_year");
  if (compact.includes("lastyearattended")) return currentEducationValue(profile, "end_year");
  return null;
}

function isOptionalBlankField(label) {
  const n = norm(label);
  return n.includes("optional")
    || n.includes("middle name")
    || n.includes("suffix")
    || n.includes("address line 2")
    || n === "county"
    || n.endsWith(" county")
    || n.includes("phone extension")
    || n === "extension"
    || n.includes("type to add skills")
    || n.includes("employee id")
    || n.includes("additional information")
    || n.includes("anything else");
}

function isHoneypotField(label) {
  const n = norm(label);
  return n.startsWith("hp ")
    || n.includes("robots only")
    || n.includes("do not enter if you re human")
    || n.includes("do not fill")
    || n.includes("leave this field blank")
    || n.includes("website this input is for robots");
}

function mapCountryValue(profile) {
  if (profile.country) return profile.country;
  const loc = norm(profile.location || "");
  if (!loc) return null;
  const states = new Set([
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
  ]);
  const tokens = new Set(loc.split(" ").filter(Boolean));
  if (loc.includes("united states") || tokens.has("usa") || tokens.has("us") || [...tokens].some((token) => states.has(token))) {
    return "United States";
  }
  return null;
}

function inferPhoneCountryCode(profile) {
  const explicit = String(profile.phone_country_code || "").trim();
  if (explicit) return explicit;
  const phone = String(profile.phone || "").trim();
  const match = phone.match(/^\+(\d{1,3})\b/);
  if (match) return `+${match[1]}`;
  const country = norm(mapCountryValue(profile) || "");
  if (["united states", "united states of america", "usa", "us", "canada"].includes(country)) return "+1";
  return null;
}

function cityFromLocation(location) {
  const raw = String(location || "").trim();
  if (!raw || !raw.includes(",")) return null;
  return raw.split(",")[0].trim() || null;
}

async function scrapeFields(page) {
  return page.evaluate(() => {
    const isVisibleElement = (node) => {
      if (!node) return false;
      if (node.getAttribute && node.getAttribute("aria-hidden") === "true") return false;
      const style = typeof window !== "undefined" && window.getComputedStyle ? window.getComputedStyle(node) : null;
      if (style && (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse")) return false;
      if (node.offsetParent) return true;
      const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
      if (!rects || rects.length === 0) return false;
      return Array.from(rects).some((rect) => rect.width > 0 && rect.height > 0);
    };
    const isHitVisibleElement = (node) => {
      if (!isVisibleElement(node)) return false;
      if (typeof document === "undefined" || typeof document.elementFromPoint !== "function" || typeof node.getBoundingClientRect !== "function") return true;
      const rects = Array.from(node.getClientRects ? node.getClientRects() : [])
        .filter((rect) => rect.width > 0 && rect.height > 0);
      const rect = rects[0] || node.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) return false;
      if (
        rect.bottom < 0
        || rect.top > window.innerHeight
        || rect.right < 0
        || rect.left > window.innerWidth
      ) return true;
      const points = [
        [rect.left + rect.width / 2, rect.top + rect.height / 2],
        [rect.left + Math.min(rect.width - 1, 4), rect.top + Math.min(rect.height - 1, 4)],
        [rect.right - Math.min(rect.width - 1, 4), rect.bottom - Math.min(rect.height - 1, 4)],
      ];
      const ownWorkdayField = node.closest && node.closest('[data-automation-id^="formField-"]');
      for (const [rawX, rawY] of points) {
        const x = Math.max(0, Math.min(window.innerWidth - 1, rawX));
        const y = Math.max(0, Math.min(window.innerHeight - 1, rawY));
        const top = document.elementFromPoint(x, y);
        if (!top) continue;
        if (top === node || node.contains(top) || top.contains(node)) return true;
        if (
          ownWorkdayField
          && top.closest
          && top.closest('[data-automation-id^="formField-"]') === ownWorkdayField
        ) return true;
      }
      return false;
    };
    const textForIds = (ids) =>
      (ids || "")
        .split(/\s+/)
        .map((id) => id && document.getElementById ? document.getElementById(id) : null)
        .filter((node) => node && node.textContent)
        .map((node) => node.textContent.trim())
        .filter(Boolean)
        .join(" ");
    const cleanQuestionText = (text) => {
      const lines = (text || "").split("\n").map((line) => line.trim()).filter(Boolean);
      const keep = [];
      for (const line of lines) {
        if (line === "✱" || line === "*" || /^select(\.\.\.)?$/i.test(line) || /^(yes|no|upload|attach)/i.test(line)) break;
        keep.push(line);
      }
      return keep.join(" ");
    };
    const optionLabelFor = (control) => {
      if (control.id) {
        const explicit = Array.from(document.querySelectorAll("label")).find((label) =>
          label.htmlFor === control.id || label.getAttribute("for") === control.id
        );
        if (explicit && explicit.textContent) return explicit.textContent.trim();
      }
      const wrapping = control.closest("label");
      if (wrapping && wrapping.textContent) {
        const clone = wrapping.cloneNode(true);
        clone.querySelectorAll("select,input,textarea,button").forEach((node) => node.remove());
        const text = clone.textContent.trim();
        if (text) return text;
      }
      const option = control.closest("li.option");
      if (option && option.textContent) {
        const clone = option.cloneNode(true);
        clone.querySelectorAll("select,input,textarea,button").forEach((node) => node.remove());
        const text = clone.textContent.trim();
        if (text) return text;
      }
      return control.getAttribute("aria-label") || control.getAttribute("data-value") ||
        control.getAttribute("data-option-value") || control.value || (control.textContent || "").trim() || "";
    };
    const workdayButtonLabel = (control) => {
      const aria = (control.getAttribute("aria-label") || "").trim();
      const text = (control.textContent || "").trim();
      let raw = aria || text || control.name || control.id || "";
      raw = raw.replace(/\b(required|select one|mobile|united states of america|\(\+1\))\b/gi, " ");
      raw = raw.replace(/\s+/g, " ").trim();
      return raw || control.name || control.id || "";
    };
    const workdaySelectedText = (control) => {
      const field = control.closest('[data-automation-id^="formField-"]');
      if (!field) return "";
      const selected = Array.from(field.querySelectorAll('[data-automation-id="selectedItem"]'))
        .map((node) => (node.textContent || "").trim())
        .filter(Boolean);
      if (selected.length) return Array.from(new Set(selected)).join(", ");
      const text = field.textContent || "";
      const match = text.match(/1 item selected,?\s*([^\\n]+?)(?:\\1)?(?:Error:|$)/i);
      return match ? match[1].trim() : "";
    };
    const leverQuestionLabel = (control) => {
      const question = control.closest(".application-question");
      if (!question) return "";
      const direct = Array.from(question.children)
        .map((node) => cleanQuestionText(node.innerText || node.textContent || ""))
        .find((text) => text && !/^(yes|no|select|select\.\.\.|upload|attach)/i.test(text));
      if (direct) return direct;
      return cleanQuestionText(question.innerText || question.textContent || "");
    };
    const breezyQuestionLabel = (control) => {
      const question = control.closest("li.question");
      if (!question) return "";
      const heading = question.querySelector("h1,h2,h3,h4,h5,h6");
      if (!heading) return "";
      const clone = heading.cloneNode(true);
      clone.querySelectorAll(".required,input,textarea,select,button").forEach((node) => node.remove());
      return cleanQuestionText(clone.innerText || clone.textContent || "");
    };
    const fieldEntryLabel = (control) => {
      const entry = control.closest(".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]");
      if (!entry) return "";
      const explicit = entry.querySelector("label,.ashby-application-form-question-title");
      return explicit && explicit.textContent ? explicit.textContent.trim() : "";
    };
    const workdayFieldLabel = (control) => {
      const field = control.closest('[data-automation-id^="formField-"]');
      if (!field) return "";
      const explicit = field.querySelector("label,[data-automation-id='formLabel']");
      const explicitText = explicit && explicit.textContent ? explicit.textContent.trim() : "";
      const isGenericCheckboxLabel = (
        (control.type || "").toLowerCase() === "checkbox"
        && /^(agree|accept|yes|i agree)$/i.test(explicitText)
      );
      let text = (explicitText && !isGenericCheckboxLabel) ? explicitText : (field.textContent || "");
      text = text.replace(/\b\d+\s+items?\s+selected\b.*$/i, "");
      text = text.replace(/\bExpanded\b.*$/i, "");
      text = text.replace(/\bError:.*$/i, "");
      if ((control.type || "").toLowerCase() === "file") {
        return text.split(/Drop files here|orSelect files|Select files/i)[0].trim();
      }
      return cleanQuestionText(text);
    };
    const workdayQuestionLabel = (control) => {
      const field = control.closest('[data-automation-id^="formField-"]');
      if (!field || !field.textContent) return "";
      const raw = field.textContent.trim();
      if (raw.includes("*")) return (raw.split("*")[0] + "*").trim();
      return workdayFieldLabel(control);
    };
    const workdayContainerSelectLabel = (field) => {
      if (!field || !field.textContent) return "";
      const explicit = field.querySelector("label,[data-automation-id='formLabel']");
      if (explicit && explicit.textContent && explicit.textContent.trim()) {
        return explicit.textContent.trim();
      }
      const text = (field.textContent || "")
        .replace(/\bError:.*$/i, "")
        .replace(/\bExpanded\b.*$/i, "")
        .trim();
      const match = text.match(/(.*?\*)\s*Select One/i);
      return match ? cleanQuestionText(match[1]) : "";
    };
    const workdayOptionLabel = (control) => {
      let node = control.parentElement;
      const field = control.closest('[data-automation-id^="formField-"]');
      while (node && node !== field) {
        const text = (node.textContent || "").trim();
        if (text) return text;
        node = node.parentElement;
      }
      return optionLabelFor(control);
    };
    const questionnaireLabel = (control) => {
      const optionText = cleanQuestionText(optionLabelFor(control));
      let node = control.parentElement;
      for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
        const text = cleanQuestionText(node.textContent || "");
        if (!text || text === optionText) continue;
        if (text.length > optionText.length + 10) return text;
      }
      return "";
    };
    const textWithoutControls = (node) => {
      if (!node) return "";
      const clone = typeof node.cloneNode === "function" ? node.cloneNode(true) : node;
      if (clone && typeof clone.querySelectorAll === "function") {
        clone.querySelectorAll("input,textarea,select,button,[role='option'],[role='radio'],[role='checkbox']")
          .forEach((child) => child.remove());
      }
      return cleanQuestionText(clone.innerText || clone.textContent || "");
    };
    const isPromptNode = (node) => {
      if (!node || !node.getAttribute) return false;
      const marker = [
        node.tagName || "", node.id || "", node.className || "",
        node.getAttribute("role") || "", node.getAttribute("data-testid") || "",
        node.getAttribute("data-qa") || "", node.getAttribute("data-test") || "",
        node.getAttribute("data-field-label") || "", node.getAttribute("data-question") || "",
      ].join(" ").toLowerCase();
      return /(^|\s)(label|legend)(\s|$)|question|prompt|field.?title|form.?title|heading/.test(marker) ||
        /^H[1-6]$/.test(node.tagName || "") || node.getAttribute("role") === "heading";
    };
    const genericPromptLabel = (control) => {
      let node = control;
      for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
        const parent = node.parentElement;
        if (parent && parent.children) {
          const siblings = Array.from(parent.children);
          const index = siblings.indexOf(node);
          for (const sibling of siblings.slice(0, index).reverse()) {
            if (!isPromptNode(sibling)) continue;
            const text = textWithoutControls(sibling);
            if (text) return text;
          }
        }
        const candidates = typeof node.querySelectorAll === "function"
          ? Array.from(node.querySelectorAll(
            "label,legend,h1,h2,h3,h4,h5,h6,[role='heading'],[data-field-label],[data-question],[data-question-label],[data-label],[data-testid],[data-qa],[data-test]"
          )).filter((candidate) => candidate !== control && typeof candidate.contains === "function" && !candidate.contains(control) && isPromptNode(candidate))
          : [];
        for (const candidate of candidates) {
          const text = textWithoutControls(candidate);
          if (text) return text;
        }
      }
      return "";
    };
    const labelFor = (control) => {
      const breezyLabel = breezyQuestionLabel(control);
      if (breezyLabel) return breezyLabel;
      const leverLabel = leverQuestionLabel(control);
      if (leverLabel) return leverLabel;
      const fieldLabel = fieldEntryLabel(control);
      if (fieldLabel) return fieldLabel;
      const workdayLabel = workdayFieldLabel(control);
      if (workdayLabel) return workdayLabel;
      const labelledBy = textForIds(control.getAttribute("aria-labelledby"));
      if (labelledBy) return labelledBy;
      if (control.id) {
        const explicit = Array.from(document.querySelectorAll("label")).find((label) =>
          label.htmlFor === control.id || label.getAttribute("for") === control.id
        );
        if (explicit && explicit.textContent) return explicit.textContent.trim();
      }
      const wrapping = control.closest("label");
      if (wrapping && wrapping.textContent) {
        // Strip nested controls (e.g. a <select> inside a <label>) so option
        // text does not pollute the field label.
        const clone = wrapping.cloneNode(true);
        clone.querySelectorAll("select,input,textarea,button").forEach((n) => n.remove());
        const txt = clone.textContent.trim();
        if (txt) return txt;
      }
      const genericPrompt = genericPromptLabel(control);
      if (genericPrompt) return genericPrompt;
      const describedBy = textForIds(control.getAttribute("aria-describedby"));
      return control.getAttribute("aria-label") || control.getAttribute("placeholder") || describedBy || control.name || "";
    };
    const groupLabelFor = (control) => {
      const breezyLabel = breezyQuestionLabel(control);
      if (breezyLabel) return breezyLabel;
      const leverLabel = leverQuestionLabel(control);
      if (leverLabel) return leverLabel;
      const fieldLabel = fieldEntryLabel(control);
      if (fieldLabel) return fieldLabel;
      const fs = control.closest("fieldset");
      if (fs) {
        const legend = fs.querySelector("legend");
        if (legend && legend.textContent) return legend.textContent.trim();
      }
      const labelledBy = textForIds(control.getAttribute("aria-labelledby"));
      if (labelledBy) return labelledBy;
      const genericPrompt = genericPromptLabel(control);
      if (genericPrompt) return genericPrompt;
      const describedBy = textForIds(control.getAttribute("aria-describedby"));
      return control.getAttribute("aria-label") || describedBy || control.getAttribute("name") || "";
    };
    const sectionFor = (control) => {
      let node = control;
      for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
        const marker = [
          node.id || "",
          typeof node.className === "string" ? node.className : "",
          node.getAttribute ? (node.getAttribute("data-automation-id") || "") : "",
        ].join(" ").toLowerCase();
        if (marker.includes("education")) return "education";
        if (marker.includes("employment") || marker.includes("work-history") || marker.includes("work_history") || marker.includes("work-experience") || marker.includes("workexperience") || marker.includes("employment-history") || marker.includes("employmenthistory")) return "work";
      }
      return "";
    };
    const metadataFor = (control) => ({
      ariaLabel: control.getAttribute("aria-label") || "",
      ariaDescription: textForIds(control.getAttribute("aria-describedby")),
      placeholder: control.getAttribute("placeholder") || "",
      autocomplete: control.getAttribute("autocomplete") || "",
      automationId: control.getAttribute("data-automation-id") || "",
      ariaControls: control.getAttribute("aria-controls") || "",
      ariaOwns: control.getAttribute("aria-owns") || "",
      contentEditable: Boolean(control.isContentEditable),
    });
    const ashbyRequired = (control) => {
      const entry = control && control.closest && control.closest(
        '.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]'
      );
      const label = entry && entry.querySelector(
        'label,.ashby-application-form-question-title'
      );
      return Boolean(label && /(^|\s)[^\s]*required[^\s]*(\s|$)/i.test(String(label.className || "")));
    };
    const isSkippable = (t) => ["hidden", "submit", "button", "image"].includes(t);

    const out = [];
    const radiosByName = {};
    const checkboxesByName = {};
    let autofillIndex = 0;
    let customGroupIndex = 0;
    document.querySelectorAll("[data-job-agent-autofill-index]").forEach((node) => {
      node.removeAttribute("data-job-agent-autofill-index");
    });
    document.querySelectorAll("input, textarea, select").forEach((c) => {
      const t = (c.type || c.tagName).toLowerCase();
      if (isSkippable(t)) return;
      const workdayField = c.closest('[data-automation-id^="formField-"]');
      const allowHiddenWorkdayCheckbox = t === "checkbox" && workdayField && isHitVisibleElement(workdayField);
      if (t !== "file" && !isHitVisibleElement(c) && !allowHiddenWorkdayCheckbox) return; // only visible controls on the current page
      const autofillId = String(autofillIndex++);
      c.setAttribute("data-job-agent-autofill-index", autofillId);
      if (t === "radio") {
        const groupLabel = groupLabelFor(c);
        const name = c.name || groupLabel || c.id || c.value || autofillId;
        if (!radiosByName[name]) {
          radiosByName[name] = {
            kind: "radiogroup", type: "radio",
            label: groupLabel, name, required: false, options: [],
          };
        }
        radiosByName[name].options.push({ id: c.id, value: c.value, label: optionLabelFor(c), autofillId });
        if (c.required || c.getAttribute("aria-required") === "true" || ashbyRequired(c)) radiosByName[name].required = true;
        return;
      }
      if (t === "checkbox") {
        const breezyLabel = breezyQuestionLabel(c);
        const breezyQuestion = c.closest("li.question");
        const breezyBoxes = breezyQuestion
          ? Array.from(breezyQuestion.querySelectorAll('input[type="checkbox"]')).filter(isHitVisibleElement)
          : [];
        if (breezyLabel && breezyBoxes.length > 1 && c.name) {
          if (!checkboxesByName[c.name]) {
            checkboxesByName[c.name] = {
              kind: "checkboxgroup", type: "checkbox", label: breezyLabel,
              name: c.name, required: false, options: [],
            };
          }
          checkboxesByName[c.name].options.push({ id: c.id, value: c.value, label: optionLabelFor(c), autofillId });
          if (c.required || c.getAttribute("aria-required") === "true") checkboxesByName[c.name].required = true;
          return;
        }
        const questionLabel = questionnaireLabel(c);
        if (questionLabel && /true and accurate|false or misleading|i certify|i confirm|read and consent|terms and conditions/i.test(questionLabel)) {
          out.push({
            kind: "single", tag: "input", type: "checkbox",
            label: questionLabel, id: c.id || "", name: c.name || "",
            role: c.getAttribute("role") || "", autofillId,
            required: Boolean(c.required || /\\*/.test(questionLabel)), options: [], value: c.checked ? c.value : "",
          });
          return;
        }
        const workdayField = c.closest('[data-automation-id^="formField-"]');
        const workdayBoxes = workdayField
          ? Array.from(workdayField.querySelectorAll('input[type="checkbox"]')).filter(isHitVisibleElement)
          : [];
        if (workdayBoxes.length > 1) {
          const groupLabel = workdayQuestionLabel(c);
          const name = workdayField.getAttribute("data-automation-id") || groupLabel || autofillId;
          if (!checkboxesByName[name]) {
            checkboxesByName[name] = {
              kind: "checkboxgroup", type: "checkbox",
              label: groupLabel, name, required: false, options: [],
            };
          }
          checkboxesByName[name].options.push({ id: c.id, value: c.value, label: workdayOptionLabel(c), autofillId });
          return;
        }
        const groupLabel = leverQuestionLabel(c);
        if (groupLabel && c.name) {
          if (!checkboxesByName[c.name]) {
            checkboxesByName[c.name] = {
              kind: "checkboxgroup", type: "checkbox",
              label: groupLabel, name: c.name, required: false, options: [],
            };
          }
          checkboxesByName[c.name].options.push({ id: c.id, value: c.value, label: optionLabelFor(c), autofillId });
          if (c.required || c.getAttribute("aria-required") === "true") checkboxesByName[c.name].required = true;
          return;
        }
      }
      const tag = c.tagName.toLowerCase();
      const options = tag === "select"
        ? Array.from(c.options).map((o) => o.textContent.trim()).filter(Boolean)
        : [];
      const label = labelFor(c) || workdayFieldLabel(c);
      const role = c.getAttribute("role") || "";
      if (!label && !c.id && !c.name) return;
      out.push({
        kind: "single", tag, type: (c.getAttribute("type") || tag).toLowerCase(),
        label, id: c.id || "", name: c.name || "", role, autofillId,
        section: sectionFor(c), ...metadataFor(c),
        required: Boolean(c.required || c.getAttribute("aria-required") === "true" || /\\*/.test(label)),
        options, value: c.value || workdaySelectedText(c),
      });
    });
    // Capability-based support for modern custom selects. These controls are
    // often a button/div with ARIA instead of an <input> or <select>.
    document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="menu"]').forEach((control) => {
      if (!isHitVisibleElement(control) || control.matches("input,textarea,select")) return;
      if (control.matches('button[type="submit"], button[type="reset"]')) return;
      const label = labelFor(control) || workdayFieldLabel(control) || workdayButtonLabel(control);
      if (!label && !control.id && !control.getAttribute("name")) return;
      const autofillId = String(autofillIndex++);
      control.setAttribute("data-job-agent-autofill-index", autofillId);
      const selected = workdaySelectedText(control)
        || control.getAttribute("aria-valuetext")
        || control.value
        || (control.textContent || "").trim();
      out.push({
        kind: "single", tag: control.tagName.toLowerCase(),
        type: (control.getAttribute("type") || control.tagName).toLowerCase(),
        label, id: control.id || "", name: control.getAttribute("name") || "",
        role: control.getAttribute("role") || "combobox",
        section: sectionFor(control), autofillId, ...metadataFor(control),
        required: Boolean(control.getAttribute("aria-required") === "true" || control.closest('[aria-required="true"]') || /\\*/.test(label)),
        options: [], value: selected,
      });
    });
    // Rich-text inputs and ARIA choice controls are common in bespoke
    // application forms. Treat their semantics as the contract instead of
    // requiring a particular ATS DOM or a native input element.
    document.querySelectorAll('[contenteditable="true"], [contenteditable="plaintext-only"]').forEach((control) => {
      if (!isHitVisibleElement(control) || control.matches('[role="combobox"], [aria-haspopup]')) return;
      const label = labelFor(control);
      if (!label && !control.id && !control.getAttribute("name")) return;
      const autofillId = String(autofillIndex++);
      control.setAttribute("data-job-agent-autofill-index", autofillId);
      out.push({
        kind: "single", tag: control.tagName.toLowerCase(), type: "contenteditable",
        label, id: control.id || "", name: control.getAttribute("name") || "",
        role: control.getAttribute("role") || "textbox",
        section: sectionFor(control), autofillId, ...metadataFor(control),
        required: Boolean(control.getAttribute("aria-required") === "true" || control.closest('[aria-required="true"]') || /\\*/.test(label)),
        options: [], value: (control.textContent || "").trim(),
      });
    });
    const customGroups = {};
    const customGroupFor = (control, kind) => {
      const groupRole = kind === "radio" ? "radiogroup" : "group";
      const root = control.closest(
        `[role="${groupRole}"], fieldset, [role="group"], [data-field], [data-question], [data-field-path]`
      ) || control.parentElement || control;
      let marker = root.getAttribute && root.getAttribute("data-job-agent-group-index");
      if (!marker) {
        marker = String(customGroupIndex++);
        if (root.setAttribute) root.setAttribute("data-job-agent-group-index", marker);
      }
      return { root, key: `${kind}:${marker}` };
    };
    document.querySelectorAll('[role="radio"], [role="checkbox"]').forEach((control) => {
      if (!isHitVisibleElement(control) || control.matches("input") || control.closest('[role="listbox"], [role="menu"]')) return;
      const type = control.getAttribute("role") === "radio" ? "radio" : "checkbox";
      const kind = type === "radio" ? "radiogroup" : "checkboxgroup";
      const group = customGroupFor(control, type);
      const groupLabel = groupLabelFor(control) || labelFor(group.root) || "";
      if (!customGroups[group.key]) {
        customGroups[group.key] = {
          kind, type, label: groupLabel, name: group.key,
          required: false, options: [], custom: true,
        };
      }
      const autofillId = String(autofillIndex++);
      control.setAttribute("data-job-agent-autofill-index", autofillId);
      customGroups[group.key].options.push({
        id: control.id || "", value: control.getAttribute("data-value") || control.getAttribute("value") || "",
        label: optionLabelFor(control), autofillId, custom: true,
        tag: control.tagName.toLowerCase(), role: control.getAttribute("role") || "",
        checked: control.getAttribute("aria-checked") === "true",
      });
      if (
        control.getAttribute("aria-required") === "true" ||
        (group.root && group.root.getAttribute && group.root.getAttribute("aria-required") === "true") ||
        (group.root && group.root.closest && group.root.closest('[aria-required="true"]'))
      ) customGroups[group.key].required = true;
    });
    document.querySelectorAll(".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]").forEach((entry) => {
      const labelNode = entry.querySelector("label,.ashby-application-form-question-title");
      const label = labelNode && labelNode.textContent ? labelNode.textContent.trim() : "";
      if (!label) return;
      const buttons = Array.from(entry.querySelectorAll("button"))
        .filter(isHitVisibleElement)
        .map((button) => ({ node: button, text: (button.textContent || button.value || "").trim() }))
        .filter((button) => button.text && !/upload|submit application|apply/i.test(button.text));
      if (buttons.length < 2) return;
      const options = [];
      buttons.forEach((button) => {
        const autofillId = String(autofillIndex++);
        button.node.setAttribute("data-job-agent-autofill-index", autofillId);
        options.push({ label: button.text, value: button.text, autofillId });
      });
      out.push({ kind: "buttongroup", type: "button", label, name: label, required: ashbyRequired(entry), options });
    });
    document.querySelectorAll('button[name][id], [data-automation-id^="formField-"] button').forEach((button) => {
      if (!isHitVisibleElement(button)) return;
      if (button.getAttribute("role") === "combobox" || button.getAttribute("aria-haspopup")) return;
      const name = button.getAttribute("name") || "";
      const id = button.id || "";
      const formField = button.closest('[data-automation-id^="formField-"]');
      if (!["country", "countryRegion", "phoneType", "degree", "veteranStatus", "gender", "ethnicity"].includes(name) && !id.startsWith("primaryQuestionnaire--") && !formField) return;
      const text = (button.textContent || "").trim();
      if (/upload|select files|remove|back|save and continue/i.test(text)) return;
      const autofillId = String(autofillIndex++);
      button.setAttribute("data-job-agent-autofill-index", autofillId);
      const label = workdayFieldLabel(button) || workdayButtonLabel(button);
      if (!label || out.some((field) => field.tag === "button" && field.label === label)) return;
      const required = Boolean(
        button.getAttribute("aria-required") === "true" ||
        (button.closest && button.closest('[aria-required="true"]')) ||
        /(^|\s)required(\s|$)/i.test(String(button.getAttribute("aria-label") || "")) ||
        /\*/.test(label)
      );
      out.push({
        kind: "single", tag: "button", type: "button",
        label, id, name, role: button.getAttribute("role") || "",
        autofillId, required, options: [], value: text,
      });
    });
    document.querySelectorAll('[data-automation-id^="formField-"]').forEach((field) => {
      if (!isVisibleElement(field)) return;
      const rawText = (field.textContent || "").replace(/\s+/g, " ").trim();
      if (!/\bSelect One\b/i.test(rawText)) return;
      const label = workdayContainerSelectLabel(field);
      if (!label || out.some((item) => item.label === label)) return;
      const target = field.querySelector(
        'button, [role="combobox"], [aria-haspopup], input, [tabindex]:not([tabindex="-1"])'
      ) || field;
      const autofillId = String(autofillIndex++);
      target.setAttribute("data-job-agent-autofill-index", autofillId);
      out.push({
        kind: "single",
        tag: target.tagName.toLowerCase(),
        type: (target.getAttribute("type") || target.tagName).toLowerCase(),
        label,
        id: target.id || "",
        name: target.getAttribute("name") || "",
        role: target.getAttribute("role") || "combobox",
        section: sectionFor(target),
        autofillId,
        ...metadataFor(target),
        required: Boolean(
          target.getAttribute("aria-required") === "true" ||
          field.getAttribute("aria-required") === "true" ||
          /\*/.test(label)
        ),
        options: [],
        value: workdaySelectedText(target) || "Select One",
      });
    });
    Object.values(radiosByName).forEach((g) => out.push(g));
    Object.values(checkboxesByName).forEach((g) => out.push(g));
    Object.values(customGroups).forEach((group) => {
      if (group.type !== "checkbox" || group.options.length !== 1) {
        out.push(group);
        return;
      }
      const option = group.options[0];
      out.push({
        kind: "single", tag: option.tag, type: "checkbox",
        label: group.label || option.label, id: option.id || "", name: group.name,
        role: "checkbox", autofillId: option.autofillId, required: group.required,
        options: [], value: option.checked ? "true" : "",
      });
    });
    return out;
  });
}

function cssAttrValue(value) {
  return String(value)
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\n/g, "\\a ");
}

function attrSelector(attr, value) {
  return "[" + attr + '="' + cssAttrValue(value) + '"]';
}

function selectorFor(f) {
  // HTML ids and names are not reliably unique on third-party ATS forms.
  if (f.autofillId) return attrSelector("data-job-agent-autofill-index", f.autofillId);
  if (f.id) return attrSelector("id", f.id);
  if (f.name) return attrSelector("name", f.name);
  return null;
}

async function recoverTextFillLocator(page, field, locator) {
  let target = null;
  try {
    target = await locator.evaluate((el) => {
      const tag = String((el && el.tagName) || "").toLowerCase();
      const type = String((el && el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
      const role = String((el && el.getAttribute && el.getAttribute("role")) || "").toLowerCase();
      return { tag, type, role, editable: Boolean(el && el.isContentEditable) };
    });
  } catch (_) {
    return locator;
  }
  if (!target) return locator;
  if (target.editable || target.tag === "textarea") return locator;
  if (target.tag === "input" && !["radio", "checkbox", "file", "submit", "button", "hidden", "image"].includes(target.type)) return locator;
  const label = String((field && field.label) || "").trim();
  if (!label) return locator;
  let marker = null;
  try {
    marker = await page.evaluate((payload) => {
      const normLocal = (value) => String(value || "")
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      const text = (node) => String((node && (node.innerText || node.textContent)) || "")
        .replace(/\s+/g, " ")
        .trim();
      const labelTextFor = (control) => {
        const parts = [
          control.getAttribute("aria-label"),
          control.getAttribute("placeholder"),
          control.getAttribute("name"),
          control.id,
        ];
        if (control.id) {
          const explicit = Array.from(document.querySelectorAll("label")).find((labelNode) =>
            labelNode.htmlFor === control.id || labelNode.getAttribute("for") === control.id
          );
          if (explicit) parts.push(text(explicit));
        }
        const entry = control.closest && control.closest(
          ".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path],fieldset,[role='group']"
        );
        if (entry) parts.push(text(entry));
        return parts.filter(Boolean).join(" ");
      };
      const wanted = normLocal(payload.label);
      if (!wanted) return null;
      const wantedTokens = wanted.split(" ").filter((token) =>
        token.length >= 4 && !["most", "recent", "progress", "degree"].includes(token)
      );
      const controls = Array.from(document.querySelectorAll(
        "input:not([type='hidden']), textarea, [contenteditable='true'], [contenteditable='plaintext-only']"
      )).filter((control) => {
        const tag = String(control.tagName || "").toLowerCase();
        const type = String(control.getAttribute("type") || tag).toLowerCase();
        return !["radio", "checkbox", "file", "submit", "button", "image"].includes(type);
      });
      let best = null;
      let bestScore = 0;
      for (const control of controls) {
        const haystack = normLocal(labelTextFor(control));
        if (!haystack) continue;
        let score = 0;
        if (haystack.includes(wanted)) score = 100;
        const common = wantedTokens.filter((token) => haystack.includes(token)).length;
        score = Math.max(score, common * 20);
        if (
          wanted.includes("graduation") &&
          (haystack.includes("graduation") || haystack.includes("anticipated graduation"))
        ) score = Math.max(score, 90);
        if (wanted.includes("date") && normLocal(control.getAttribute("placeholder")).includes("date")) {
          score = Math.max(score, 60 + common * 10);
        }
        if (score > bestScore) {
          best = control;
          bestScore = score;
        }
      }
      if (!best || bestScore < 50) return null;
      const id = "job-agent-fill-recovered-" + Date.now() + "-" + Math.floor(Math.random() * 1000000);
      best.setAttribute("data-job-agent-fill-target", id);
      return id;
    }, { label });
  } catch (_) {
    marker = null;
  }
  return marker ? page.locator(attrSelector("data-job-agent-fill-target", marker)).first() : locator;
}

function normalizeComboboxAnswer(ans) {
  if (ans == null) return ans;
  const raw = String(ans);
  const n = norm(raw);
  // Verbose yes/no intents back to simple labels so custom selects can match.
  if (n.startsWith("yes") && n.length > 3) return "Yes";
  if (n.startsWith("no") && n.length > 2) return "No";
  // Work authorization / sponsorship intents.
  if (n.includes("authorized") && n.includes("work") && n.includes("any employer")) {
    return "I am authorized to work for any employer in the country in which this position is based.";
  }
  if (n.includes("sponsor")) {
    const company = String((CFG.profile && CFG.profile.target_company) || "").trim();
    const companyPossessive = company ? `${company}'s ` : "";
    return `I require/will require ${companyPossessive}sponsorship to obtain work authorization in the country in which this position is based`;
  }
  // Pronoun intents.
  if (n.includes("he") && n.includes("him")) return "He / Him";
  if (n.includes("she") && n.includes("her")) return "She / Her";
  if (n.includes("they") && n.includes("them")) return "They / Them";
  return ans;
}

function comboboxAnswer(label, mappingLabel, profile, sensitive, priorityAns, answers, field) {
  if (!sensitive && isSourceQuestion(label)) {
    const preferred = autoAnswer(label, profile);
    if (preferred != null) return normalizeComboboxAnswer(preferred);
  }
  const preferredOffice = field ? preferredOfficeLocationOption(field, profile) : null;
  if (!sensitive && preferredOffice) return normalizeComboboxAnswer(optionText(preferredOffice));
  const preferredOfficeAnswer = field ? preferredOfficeLocationAnswer(field, profile) : null;
  if (!sensitive && preferredOfficeAnswer) return normalizeComboboxAnswer(preferredOfficeAnswer);
  let ans = priorityAns || (sensitive
    ? (matchSensitive(label) || demographicAnswer(label, profile) || autoAnswer(label, profile, true))
    : (findAnswer(label, answers) || mapTextValue(field || mappingLabel, profile) || autoAnswer(label, profile)));
  const mapped = norm(mappingLabel);
  if (!sensitive && profile.location && (mapped.includes("location") || mapped.includes("city"))) {
    ans = profile.location;
  }
  return normalizeComboboxAnswer(ans);
}

function planField(f, profile, ctx) {
  const answers = profile.answers || {};
  const label = f.label;
  const semantic = semanticForField(f);
  const profileDateSemantic = Boolean(
    semantic && /^(education|work)\./.test(semantic.key || "") && /\.(date|month|day|year)$/.test(semantic.key || "")
  );
  const mappingLabel = [
    f.label, f.id, f.name, f.section, f.ariaLabel, f.ariaDescription,
    f.placeholder, f.autocomplete,
  ].filter(Boolean).join(" ");
  const optionLabels = (f.options || []).map((option) => option.label || option.value || "").join(" ");
  const answerLabel = norm(mappingLabel).replace(/\s+/g, "").includes("communicationconsent") || norm(optionLabels).includes("text messages")
    ? `${label} Do you consent to receiving text messages?`
    : label;
  if (isHoneypotField(mappingLabel)) {
    return { action: "skip", reason: "honeypot field", blocking: false };
  }
  if (requiresExternalApplicationPortal(`${label} ${mappingLabel}`)) {
    return { action: "skip", reason: "external application portal required", sensitive: false, blocking: true };
  }
  // Distinguish a candidate's dated work/education history from an
  // availability question for the new role. Both may say "start date", but
  // only the latter requires the sensitive-answer policy.
  const sensitive = !profileDateSemantic && (isSensitive(label) || (
    (["radiogroup", "buttongroup", "checkboxgroup"].includes(f.kind) || f.tag === "button") && isSensitive(mappingLabel)
  ));
  if (!f.required && isDemographicLabel(label)) {
    const demographic = matchSensitive(answerLabel) || demographicAnswer(answerLabel, profile);
    if (demographic == null || isDeclineAnswer(demographic)) {
      return { action: "skip", reason: "optional demographic left unselected", sensitive: true, blocking: false };
    }
  }
  let priorityAns = priorityAutoAnswer(answerLabel, profile);
  if (priorityAns == null) {
    priorityAns = workAuthorizationDropdownAnswer(answerLabel, profile);
  }
  const signatureValue = legalSignatureValue(mappingLabel, profile);
  if (signatureValue != null && ["input", "textarea"].includes(f.tag)) {
    return { action: "fill", value: signatureValue, sensitive: true };
  }
  const contactLocation = norm(label).includes("current location") || norm(label).includes("location city");
  if (
    contactLocation &&
    profile.location &&
    ["input", "textarea"].includes(String(f.tag || "").toLowerCase()) &&
    !["radiogroup", "buttongroup", "checkboxgroup"].includes(f.kind)
  ) {
    return { action: "combobox", value: String(profile.location) };
  }

  // Fields that belong to repeatable work/education sections are filled by
  // fillRepeatableSection — skip them here, but ONLY when such a section is
  // actually present on the page (so a standalone "Current Location" contact
  // field is still filled by the generic mapper).
  if (ctx) {
    const wf = mapWorkField(label);
    const ef = mapEduField(label);
    const skipWork = ctx.hasWork && wf && wf !== "location" && wf !== "current";
    const skipEdu = ctx.hasEdu && ef;
    if (skipWork || skipEdu) {
      return { action: "skip", reason: "handled by repeatable section filler" };
    }
  }

  // Radio/button group: map the QUESTION (legend/field label) to an answer,
  // then pick the option.
  if (f.kind === "radiogroup" || f.kind === "buttongroup") {
    const ans = priorityAns || (sensitive ? (matchSensitive(answerLabel) || demographicAnswer(answerLabel, profile) || autoAnswer(answerLabel, profile, true)) : (findAnswer(label, answers) || mapTextValue(f, profile) || mapTextValue(mappingLabel, profile) || autoAnswer(answerLabel, profile)));
    if (ans == null) {
      const privacyOption = privacyPreservingPronounOption(f);
      if (privacyOption) {
        if (f.kind === "buttongroup") {
          return { action: "buttonclick", optionAutofillId: privacyOption.autofillId, optionValue: privacyOption.value, optionText: optionText(privacyOption) };
        }
        return { action: "check", optionId: privacyOption.id, optionValue: privacyOption.value, optionText: optionText(privacyOption), optionAutofillId: privacyOption.autofillId, groupName: f.name };
      }
      if (requiresUserAuthoredAnswer(label, profile)) {
        return { action: "skip", reason: "question requires user-authored answer / no AI assistance", sensitive, blocking: true };
      }
      if (!f.required) return { action: "skip", reason: "non-required unmapped field", sensitive, blocking: false };
      return { action: "skip", reason: "no approved answer for screening question", sensitive };
    }
    const opt = matchingOptions(f, ans)[0] || null;
    if (opt && f.kind === "buttongroup") {
      return { action: "buttonclick", optionAutofillId: opt.autofillId, optionValue: opt.value, optionText: optionText(opt) };
    }
    if (opt) {
      return { action: "check", optionId: opt.id, optionValue: opt.value, optionText: optionText(opt), optionAutofillId: opt.autofillId, groupName: f.name };
    }
    if (n.includes("how many years of relevant professional experience") && ans) {
      return { action: "buttonclick", optionValue: String(ans), optionText: String(ans) };
    }
    if (isNegativeAnswer(ans) && !f.required) {
      return { action: "skip", reason: "approved No answer has no matching optional option", sensitive, blocking: false };
    }
    return { action: "skip", reason: "no option matches saved answer", sensitive };
  }
  if (f.kind === "checkboxgroup") {
    const ans = priorityAns || (sensitive ? (matchSensitive(mappingLabel) || demographicAnswer(mappingLabel, profile) || autoAnswer(label, profile, true)) : (findAnswer(label, answers) || mapTextValue(f, profile) || mapTextValue(mappingLabel, profile) || autoAnswer(label, profile)));
    if (ans == null) {
      if (!f.required) return { action: "skip", reason: "non-required unmapped field", sensitive, blocking: false };
      return { action: "skip", reason: "checkbox group needs saved answer / manual selection", sensitive };
    }
    const matches = matchingOptions(f, ans);
    if (matches.length) return { action: "checkmany", options: matches };
    if (isNegativeAnswer(ans) && !f.required) {
      return { action: "skip", reason: "approved No answer has no matching optional checkbox option", sensitive, blocking: false };
    }
    return { action: "skip", reason: "no checkbox option matches saved answer", sensitive };
  }

  // File upload
  if (f.type === "file") {
    const ln = norm([label, f.id, f.name].filter(Boolean).join(" "));
    if (ln.includes("cover letter") && CFG.coverLetterFile) {
      return { action: "upload", value: CFG.coverLetterFile };
    }
    if ((ln.includes("resume") || ln.includes("cv") || (ln.includes("upload a file") && !ln.includes("cover letter")) || (ln.includes("attachment") && !ln.includes("cover letter"))) && CFG.resumeFile) {
      return { action: "upload", value: CFG.resumeFile };
    }
    if (!f.required) return { action: "skip", reason: "optional non-resume file field", blocking: false };
    return { action: "skip", reason: "file field not resume/cv or no resume configured" };
  }
  if (f.type === "password") {
    const applicationUrl = String(CFG.applicationUrl || "").toLowerCase();
    const isWorkdayApplication = applicationUrl.includes("myworkdayjobs.com") || applicationUrl.includes("workdayjobs.com");
    const password = candidateAccountPassword({
      createIfMissing: !!(ctx && ctx.hasCandidateAccountCreation) || isWorkdayApplication,
    });
    if (password) return { action: "fill", value: password, sensitive: true };
    return { action: "skip", reason: "candidate account creation required", blocking: true };
  }
  const currentValue = fieldSelectedText(f);
  if (currentValue && norm(label).includes("country phone code")) {
    return { action: "skip", reason: "field already selected" };
  }
  // Custom ATS dropdown/combobox controls, e.g. Greenhouse React Select.
  if (f.role === "combobox") {
    const ans = comboboxAnswer(label, mappingLabel, profile, sensitive, priorityAns, answers, f);
    const current = fieldSelectedText(f);
    if (current && current !== "expanded") {
      if (ans == null) return { action: "skip", reason: "combobox already selected" };
      const last = norm(String(ans).split(">").pop() || ans);
      if (last && current.includes(last)) return { action: "skip", reason: "combobox already selected" };
    }
    if (ans != null) {
      const opt = matchingOptions(f, ans)[0] || null;
      return { action: "combobox", value: opt ? optionText(opt) : String(ans) };
    }
    if (!f.required) {
      return { action: "skip", reason: "non-required combobox has no approved answer", sensitive, blocking: false };
    }
    return { action: "skip", reason: "combobox needs saved answer / manual selection", sensitive };
  }
  if (f.tag === "button") {
    const current = fieldSelectedText(f);
    if (current && current !== "select one") return { action: "skip", reason: "button dropdown already selected" };
    const salaryRange = norm(label).includes("desired annual base salary range") ? desiredSalaryRangeValue(profile) : null;
    const ans = salaryRange || priorityAns || (sensitive ? (matchSensitive(label) || demographicAnswer(label, profile) || autoAnswer(label, profile, true)) : (findAnswer(label, answers) || mapTextValue(f, profile) || mapTextValue(mappingLabel, profile) || autoAnswer(label, profile)));
    if (ans != null) return { action: "customselect", value: String(ans) };
    if (!f.required) return { action: "skip", reason: "non-required unmapped field", blocking: false };
    return { action: "skip", reason: "button dropdown needs saved answer / manual selection", sensitive };
  }
  if (f.type === "input" && norm(label).includes("how did you hear")) {
    if (currentValue) return { action: "skip", reason: "field already selected" };
    const ans = autoAnswer(label, profile) || findAnswer(label, answers);
    if (ans != null) return { action: "combobox", value: String(ans) };
    return { action: "skip", reason: "combobox needs saved answer / manual selection", sensitive };
  }
  // Select (dropdown)
  if (f.tag === "select") {
    const level = degreeFieldQuestion(label);
    let ans = priorityAns || (sensitive ? (matchSensitive(label) || demographicAnswer(label, profile) || autoAnswer(label, profile, true)) : (findAnswer(label, answers) || mapTextValue(f, profile) || mapTextValue(mappingLabel, profile) || autoAnswer(label, profile)));
    if (level) ans = educationFieldForLevel(profile, level) || ans;
    const opt = ans ? findOption(f.options, ans) : null;
    if (opt) {
      return { action: "select", value: optionText(opt) };
    }
    if (level && ans) {
      const other = (f.options || []).find((option) => norm(optionText(option)) === "other");
      if (other) return { action: "select", value: optionText(other) };
    }
    const sourceOpt = matchingOptions(f, ans)[0] || null;
    if (sourceOpt) return { action: "select", value: optionText(sourceOpt) };
    if (!f.required) return { action: "skip", reason: "non-required unmapped field", blocking: false };
    return { action: "skip", reason: "no matching option / answer", sensitive };
  }
  // Checkbox (e.g. consent / yes-no screening)
  if (f.type === "checkbox") {
    const officeLocationPlan = officeLocationCheckboxPlan(f, profile);
    if (officeLocationPlan) return officeLocationPlan;
    if (norm(mappingLabel).includes("preferred name")) return { action: "skip", reason: "preferred name checkbox not needed", blocking: false };
    if (norm(mappingLabel).includes("currently work") && currentWorkValue(profile, "current")) return { action: "check" };
    if (isCandidateAccountCreationConsentCheckbox(f, ctx)) {
      const approved = matchSensitive("process your personal data");
      if (approved != null) {
        const want = norm(String(approved));
        if (want === "yes" || want === "true" || want === "1") return { action: "check", value: String(approved), sensitive: true };
        if (want === "no" || want === "false" || want === "0") {
          return { action: "skip", reason: "candidate account creation privacy consent declined", sensitive: true, blocking: true };
        }
      }
      return { action: "skip", reason: "candidate account creation privacy consent needs approved answer", sensitive: true, blocking: true };
    }
    const legalContextAnswer = legalTermsConsentAnswer(mappingLabel, profile);
    if (legalContextAnswer != null) {
      if (truthyAnswer(legalContextAnswer)) return { action: "check", value: String(legalContextAnswer), sensitive: norm(mappingLabel).includes("personal data") };
      return { action: "skip", reason: "approved legal/consent answer is negative", sensitive: true, blocking: !!f.required };
    }
    if (sensitive) {
      const approved = priorityAns || matchSensitive(label) || demographicAnswer(label, profile) || autoAnswer(label, profile, true);
      if (approved != null) {
        const want = norm(String(approved));
        if (want === "yes" || want === "true" || want === "1") return { action: "check", value: String(approved) };
        if (f.required) return { action: "skip", reason: "required checkbox conflicts with approved No answer", sensitive: true, blocking: true };
        return { action: "skip", reason: "approved No answer leaves checkbox unchecked", sensitive: true, blocking: false };
      }
      if (!f.required) return { action: "skip", reason: "non-required unmapped field", blocking: false };
      return { action: "skip", reason: "sensitive checkbox needs approved answer", sensitive: true };
    }
    const ans = findAnswer(label, answers) || autoAnswer(label, profile);
    if (ans != null) {
      const want = norm(String(ans));
      if (want === "yes" || want === "true" || want === "1") return { action: "check", value: String(ans) };
      if (want === "no" || want === "false" || want === "0") {
        if (f.required) return { action: "skip", reason: "required checkbox conflicts with approved No answer", blocking: true };
        return { action: "skip", reason: "approved No answer leaves checkbox unchecked", blocking: false };
      }
      return { action: "skip", reason: "saved answer is negative for checkbox", blocking: true };
    }
    if (!f.required) return { action: "skip", reason: "non-required unmapped field", blocking: false };
    return { action: "skip", reason: "consent/checkbox needs explicit review", sensitive: isSensitive(label) };
  }
  // Text / email / tel / textarea
  if (f.type === "radio" || f.role === "radio" || f.type === "checkbox" || f.role === "checkbox" || f.role === "switch") {
    const ans = priorityAns || (sensitive ? (matchSensitive(mappingLabel) || demographicAnswer(mappingLabel, profile) || autoAnswer(label, profile, true)) : (findAnswer(label, answers) || autoAnswer(label, profile)));
    if (ans != null) {
      const want = norm(String(ans));
      const selected = norm([label, f.value].filter(Boolean).join(" "));
      if (
        want === "yes" ||
        want === "true" ||
        want === "1" ||
        optionMatches({ label, value: f.value }, ans) ||
        (selected && answerAliases(ans).map(norm).some((alias) => alias && selected.includes(alias)))
      ) {
        return { action: "check", value: String(ans), sensitive };
      }
      if (["no", "false", "0"].includes(want)) {
        return { action: "skip", reason: "single selectable answer is negative", sensitive, blocking: !!f.required };
      }
    }
    if (!f.required) return { action: "skip", reason: "non-required unmapped field", sensitive, blocking: false };
    return { action: "skip", reason: "single selectable needs saved answer / manual selection", sensitive };
  }
  if (sensitive) {
    const approved = priorityAns || matchSensitive(label) || demographicAnswer(label, profile) || autoAnswer(label, profile, true);
    if (approved != null) return { action: "fill", value: String(approved) };
    return { action: "skip", reason: "sensitive field needs review", sensitive: true };
  }
  if (isOptionalBlankField(label)) {
    return { action: "skip", reason: "optional empty field", blocking: false };
  }
  if (norm(mappingLabel).includes("phone number")) {
    const mappedPhone = mapTextValue(f, profile) || mapTextValue(mappingLabel, profile);
    if (mappedPhone) return { action: "fill", value: String(mappedPhone) };
  }
  if (isWorkdayApplicationUrl(profile) && ["field of study", "major"].includes(norm(label))) {
    const fieldOfStudy = currentEducationValue(profile, "field");
    if (fieldOfStudy) return { action: "combobox", value: String(fieldOfStudy) };
  }
  const mapped = mapTextValue(f, profile) || mapTextValue(label, profile) || mapTextValue(mappingLabel, profile);
  if (mapped) {
    return { action: "fill", value: mapped };
  }
  const ans = findAnswer(label, answers);
  if (ans != null) {
    return { action: "fill", value: String(ans) };
  }
  const auto = autoAnswer(label, profile);
  if (auto) return { action: "fill", value: String(auto) };
  if (requiresUserAuthoredAnswer(label, profile)) {
    return { action: "skip", reason: "question requires user-authored answer / no AI assistance", blocking: true };
  }
  if (!f.required) {
    return { action: "skip", reason: "non-required unmapped field", blocking: false };
  }
  return { action: "skip", reason: "unmapped field" };
}

function workdayDatePartValue(value, part, now = new Date()) {
  const raw = String(value || "").trim();
  if (!raw) return null;
  const normalized = norm(raw);
  let target = null;
  if (["within a month", "in one month", "one month"].includes(normalized)) {
    target = new Date(now.getFullYear(), now.getMonth() + 1, now.getDate());
  } else if (["within two weeks", "in two weeks", "two weeks"].includes(normalized)) {
    target = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 14);
  } else if (["immediately", "as soon as possible", "asap"].includes(normalized)) {
    target = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  } else {
    let match = raw.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/);
    if (match) target = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    if (!target) {
      match = raw.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
      if (match) target = new Date(Number(match[3]), Number(match[1]) - 1, Number(match[2]));
    }
    if (!target && /\d{4}/.test(raw) && /[a-z]/i.test(raw)) {
      const parsed = Date.parse(raw);
      if (!Number.isNaN(parsed)) target = new Date(parsed);
    }
  }
  if (target && !Number.isNaN(target.getTime())) {
    if (part === "Month") return String(target.getMonth() + 1).padStart(2, "0");
    if (part === "Day") return String(target.getDate()).padStart(2, "0");
    return String(target.getFullYear());
  }
  if (part === "Month") {
    const month = Object.entries(MONTH_NAMES).find(([, name]) => norm(name) === normalized);
    if (month) return String(Number(month[0])).padStart(2, "0");
    if (/^\d{1,2}$/.test(raw) && Number(raw) >= 1 && Number(raw) <= 12) return String(Number(raw)).padStart(2, "0");
  }
  if (part === "Day" && /^\d{1,2}$/.test(raw) && Number(raw) >= 1 && Number(raw) <= 31) return String(Number(raw)).padStart(2, "0");
  if (part === "Year" && /^\d{4}$/.test(raw)) return raw;
  return null;
}

async function workdayDateSectionsMatch(page, prefix, values) {
  try {
    for (const [part, value] of Object.entries(values)) {
      const locator = page.locator(attrSelector("id", `${prefix}-dateSection${part}-input`)).first();
      if (!(await locator.count())) return false;
      const readback = String(await locator.inputValue().catch(() => "")).trim();
      if (!/^\d+$/.test(readback) || Number(readback) !== Number(value)) return false;
    }
  } catch (error) {
    return false;
  }
  return true;
}

async function fillWorkdayDateSections(page, fieldId, target = new Date()) {
  const prefix = String(fieldId || "").split("-dateSection", 1)[0];
  if (!prefix) throw new Error("Workday date field has no section prefix");
  const values = {
    Month: String(target.getMonth() + 1).padStart(2, "0"),
    Day: String(target.getDate()).padStart(2, "0"),
    Year: String(target.getFullYear()),
  };
  if (await workdayDateSectionsMatch(page, prefix, values)) return formatLocalDate(target);

  // Workday's calendar commits React state more reliably than direct DOM
  // writes. Fall back to keyboard events for pages without a visible calendar.
  try {
    const icon = page.locator(attrSelector("data-fkit-id", prefix) + " " + attrSelector("data-automation-id", "dateIcon")).first();
    if (await icon.count() && await icon.isVisible()) {
      await icon.click();
      await page.waitForTimeout(250);
      const mmdd = values.Month + values.Day;
      const day = page.locator(
        attrSelector("data-automation-id", "datePicker") + " " +
        attrSelector("data-uxi-datepicker-year", values.Year) +
        attrSelector("data-uxi-datepicker-month", String(target.getMonth() + 1)) +
        attrSelector("data-uxi-datepicker-mmdd", mmdd)
      ).first();
      if (await day.count() && await day.isVisible()) {
        await day.click();
        await page.waitForTimeout(700);
        if (await workdayDateSectionsMatch(page, prefix, values)) return formatLocalDate(target);
      }
    }
  } catch (error) {}

  const failed = [];
  for (const [part, value] of Object.entries(values)) {
    const locator = page.locator(attrSelector("id", `${prefix}-dateSection${part}-input`)).first();
    try {
      if (!(await locator.count())) {
        failed.push(part);
        continue;
      }
      await locator.click();
      await locator.press("Control+A");
      await locator.press("Backspace");
      if (typeof locator.pressSequentially === "function") await locator.pressSequentially(value, { delay: 100 });
      else await locator.fill(value);
      await locator.press("Tab").catch(() => {});
      await page.waitForTimeout(400);
      const readback = String(await locator.inputValue().catch(() => "")).trim();
      if (!/^\d+$/.test(readback) || Number(readback) !== Number(value)) failed.push(`${part}=${readback || "empty"}`);
    } catch (error) {
      failed.push(part);
    }
  }
  if (failed.length) throw new Error("Workday date sections did not retain typed values: " + failed.join(", "));
  return formatLocalDate(target);
}

async function fillWorkdayDateSection(page, field, value) {
  const fieldId = String((field && field.id) || "");
  const match = fieldId.match(/^(.*)-dateSection(Month|Day|Year)-input$/);
  if (!match) return null;
  const [, prefix, part] = match;
  if (norm(prefix).replace(/\s+/g, "").includes("datesignedon")) {
    return fillWorkdayDateSections(page, fieldId);
  }
  const desired = workdayDatePartValue(value, part);
  if (!desired) return null;
  const locator = page.locator(attrSelector("id", fieldId)).first();
  if (!(await locator.count())) throw new Error("Workday date section missing: " + part);
  const current = String(await locator.inputValue().catch(() => "")).trim();
  if (/^\d+$/.test(current) && Number(current) === Number(desired)) return current;
  await locator.click();
  await locator.press("Control+A");
  await locator.press("Backspace");
  if (typeof locator.pressSequentially === "function") await locator.pressSequentially(desired, { delay: 100 });
  else await locator.fill(desired);
  await locator.press("Tab").catch(() => {});
  await page.waitForTimeout(400);
  const readback = String(await locator.inputValue().catch(() => "")).trim();
  if (/^\d+$/.test(readback) && Number(readback) === Number(desired)) return readback;
  throw new Error(`Workday date section did not retain ${part}=${desired}: ${readback || "empty"}`);
}

function isPlaceholderSelection(value) {
  return /^(select|select one|choose|please select|--.*--)?$/i.test(String(value || "").trim());
}

function selectionMatchesAnswer(value, answer) {
  const selected = norm(value);
  if (!selected || isPlaceholderSelection(value)) return false;
  return answerAliases(answer).some((alias) => {
    const expected = norm(alias);
    return expected && (selected === expected || (
      expected.length >= 3 && (selected.includes(expected) || expected.includes(selected))
    ));
  });
}

function fieldSelectedText(field) {
  const values = [];
  for (const key of ["value", "ariaLabel", "ariaDescription"]) {
    const text = norm(field && field[key]);
    if (!text || ["search", "select", "select one", "expanded"].includes(text) || text.includes("select one")) continue;
    if (text.includes("0 items selected")) continue;
    values.push(text);
  }
  return values.join(" ").trim();
}

function requiresStrictComboboxCommitReadback(field) {
  const label = norm((field && field.label) || "");
  const id = norm((field && field.id) || "");
  if (id === "country" ||
    label.startsWith("country") ||
    label.includes("country phone code") ||
    label.includes("phone country code") ||
    label.includes("how did you hear") ||
    label.includes("where did you hear") ||
    label.includes("where have you learned about")) return true;
  return norm((field && field.role) || "") === "combobox";
}

function isSchoolComboboxField(field) {
  const label = norm((field && field.label) || "");
  if (label.includes("schoolwork") || label.includes("school work")) return false;
  if ((label.includes("experience") || label.includes("years")) && label.includes("college")) return false;
  return ["school", "university", "institution", "college"].includes(label)
    || ["school", "university", "institution", "college"].some((term) => hasWholePhrase(label, term));
}

async function typeIntoComboboxSearch(page, selector, field, query) {
  if (!query) return false;
  const locator = page.locator(selector).first();
  if (isSchoolComboboxField(field)) {
    try {
      await locator.click({ timeout: 3000 });
      await page.waitForTimeout(250);
      if (page.keyboard && typeof page.keyboard.press === "function") {
        await page.keyboard.press("Control+A").catch(() => {});
        await page.keyboard.press("Backspace").catch(() => {});
      }
      if (page.keyboard && typeof page.keyboard.insertText === "function") {
        await page.keyboard.insertText(query).catch(() => {});
      } else if (page.keyboard && typeof page.keyboard.type === "function") {
        await page.keyboard.type(query).catch(() => {});
      }
      await page.waitForTimeout(900);
      return true;
    } catch (e) {}
  }
  try {
    const didType = await locator.evaluate((node, text) => {
      const visible = (el) => !!(el && (el.offsetParent || el.getClientRects().length));
      const root = node.closest(
        '[data-field-entry-id], .field, .application--form--field, .select, .select__control, .select-container'
      ) || node.parentElement || node;
      const candidates = Array.from(root.querySelectorAll('input:not([type="hidden"]), textarea'))
        .filter((el) => !el.disabled && !el.readOnly && visible(el));
      const input = candidates.find((el) =>
        String(el.getAttribute("role") || "").toLowerCase() === "combobox" ||
        String(el.getAttribute("aria-autocomplete") || "").toLowerCase() === "list" ||
        /react-select|select|school|institution|university|college/i.test(
          [el.id, el.name, el.getAttribute("aria-label"), el.getAttribute("placeholder")].join(" ")
        )
      ) || candidates[0] || (node.matches && node.matches('input:not([type="hidden"]), textarea') ? node : null);
      if (!input) return false;
      input.focus();
      const proto = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(input, "");
      else input.value = "";
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
      if (setter) setter.call(input, text);
      else input.value = text;
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }, query);
    if (didType) {
      await page.waitForTimeout(250);
      try {
        await locator.click({ timeout: 3000 }).catch(() => {});
        await page.waitForTimeout(150);
        if (page.keyboard && typeof page.keyboard.press === "function") {
          await page.keyboard.press("Control+A").catch(() => {});
          await page.keyboard.press("Backspace").catch(() => {});
        }
        if (page.keyboard && typeof page.keyboard.insertText === "function") {
          await page.keyboard.insertText(query).catch(() => {});
        } else if (page.keyboard && typeof page.keyboard.type === "function") {
          await page.keyboard.type(query).catch(() => {});
        }
      } catch (e) {}
      await page.waitForTimeout(650);
      return true;
    }
  } catch (e) {}
  try {
    await locator.click({ timeout: 3000 });
    if (page.keyboard && typeof page.keyboard.press === "function") {
      await page.keyboard.press("Control+A").catch(() => {});
      await page.keyboard.press("Backspace").catch(() => {});
    }
    if (page.keyboard && typeof page.keyboard.insertText === "function") {
      await page.keyboard.insertText(query).catch(() => {});
    } else if (page.keyboard && typeof page.keyboard.type === "function") {
      await page.keyboard.type(query).catch(() => {});
    }
    await page.waitForTimeout(650);
    return true;
  } catch (e) {
    return false;
  }
}

async function commitSchoolComboboxNativeValue(page, selector, value) {
  if (!value) return;
  try {
    await page.locator(selector).first().evaluate((node, text) => {
      const input = node.matches && node.matches('input:not([type="hidden"]), textarea')
        ? node
        : node.querySelector && node.querySelector('input:not([type="hidden"]), textarea');
      if (!input) return false;
      const proto = input.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(input, text);
      else input.value = text;
      input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
      return true;
    }, value);
  } catch (e) {}
}

async function selectGreenhouseSchoolCombobox(page, selector, field, answer) {
  if (!answer || !isSchoolComboboxField(field)) return null;
  const pageUrl = String(page.url && page.url() || "").toLowerCase();
  if (!pageUrl.includes("greenhouse.io")) return null;
  const locator = page.locator(selector).first();
  try {
    await locator.scrollIntoViewIfNeeded().catch(() => {});
    await locator.click({ timeout: 3000 });
    await page.waitForTimeout(300);
    if (page.keyboard && typeof page.keyboard.press === "function") {
      await page.keyboard.press("Control+A").catch(() => {});
      await page.keyboard.press("Backspace").catch(() => {});
    }
    if (page.keyboard && typeof page.keyboard.insertText === "function") {
      await page.keyboard.insertText(answer).catch(() => {});
    } else if (page.keyboard && typeof page.keyboard.type === "function") {
      await page.keyboard.type(answer).catch(() => {});
    }
    await page.waitForTimeout(1000);
    let clicked = false;
    if (typeof page.getByRole === "function") {
      await page.getByRole("option", { name: answer, exact: true }).first().click({ timeout: 3000 }).then(() => {
        clicked = true;
      }).catch(() => {});
    }
    if (!clicked && typeof page.getByText === "function") {
      await page.getByText(answer, { exact: true }).last().click({ timeout: 3000 }).then(() => {
        clicked = true;
      }).catch(() => {});
    }
    if (clicked) {
      await page.waitForTimeout(500);
      await commitSchoolComboboxNativeValue(page, selector, answer);
      return answer;
    }
  } catch (e) {}
  return null;
}

async function controlSelectionReadback(page, field) {
  const context = {
    id: String((field && field.id) || ""),
    name: String((field && field.name) || ""),
    autofillId: String((field && field.autofillId) || ""),
    strictCommittedSelection: requiresStrictComboboxCommitReadback(field),
  };
  let directValue = "";
  const selector = selectorFor(field || {});
  if (selector) {
    directValue = String(await page.locator(selector).first().inputValue().catch(() => "")).trim();
  }
  let state = { values: [], expanded: false };
  try {
    state = await page.evaluate((payload) => {
      const visibleText = (node) => String((node && node.textContent) || "").replace(/\s+/g, " ").trim();
      const controls = Array.from(document.querySelectorAll(
        'input, textarea, button, [contenteditable="true"], [contenteditable="plaintext-only"], [role="combobox"], [aria-haspopup]'
      ));
      const control = controls.find((node) => payload.autofillId
        ? node.getAttribute("data-job-agent-autofill-index") === payload.autofillId
        : (
          (payload.id && node.id === payload.id) ||
          (payload.name && node.getAttribute("name") === payload.name)
        )
      ) || document.activeElement;
      if (!control) return { values: [], expanded: false };
      const values = [];
      const strictCommittedSelection = Boolean(payload.strictCommittedSelection);
      const add = (value) => {
        const text = String(value || "").replace(/\s+/g, " ").trim();
        if (text && !/^(select|select one|choose|please select|--.*--)?$/i.test(text) && !values.includes(text)) values.push(text);
      };
      const expanded = control.getAttribute("aria-expanded") === "true";
      add(control.getAttribute("aria-valuetext"));
      add(control.getAttribute("data-value"));
      add(control.getAttribute("data-selected-value"));
      if (!expanded && !strictCommittedSelection) add(control.value);
      if (String(control.tagName || "").toLowerCase() === "button" || control.isContentEditable || control.getAttribute("aria-haspopup")) add(visibleText(control));
	      const reactSelectRoot = control.closest && control.closest('[class*="select__control"]');
	      const reactValueRoot = control.closest && control.closest('[class*="select__value-container"]');
	      const root = (control.closest && control.closest('[data-automation-id^="formField-"], [role="group"], [role="radiogroup"], fieldset')) || control;
	      const controlled = String(control.getAttribute("aria-controls") || control.getAttribute("aria-owns") || "")
	        .split(/\s+/)
	        .map((id) => id && document.getElementById(id))
	        .filter(Boolean);
	      const roots = Array.from(new Set(
	        [reactSelectRoot, reactValueRoot, root, ...controlled].filter(Boolean)
	      ));
	      for (const candidateRoot of roots) {
	        if (!candidateRoot || !candidateRoot.querySelectorAll) continue;
	        candidateRoot.querySelectorAll(
	          '[class*="select__single-value"], [class*="select__multi-value__label"]'
	        ).forEach((node) => add(visibleText(node)));
	        candidateRoot.querySelectorAll('[data-automation-id="selectedItem"]').forEach((node) => add(visibleText(node) || node.getAttribute("data-value") || node.getAttribute("aria-label")));
        candidateRoot.querySelectorAll('[data-automation-id="promptSelectionLabel"]').forEach((node) => add(visibleText(node) || node.getAttribute("data-value") || node.getAttribute("aria-label")));
        if (!expanded) {
          candidateRoot.querySelectorAll('[aria-selected="true"], [aria-checked="true"], [data-state="selected"], [data-state="checked"], [data-state="on"]').forEach((node) => add(visibleText(node) || node.getAttribute("data-value") || node.getAttribute("aria-label")));
        }
        candidateRoot.querySelectorAll('input[type="radio"]:checked').forEach((node) => {
          const label = (node.closest && node.closest("label")) ||
            Array.from(document.querySelectorAll("label")).find((item) => item.htmlFor === node.id || item.getAttribute("for") === node.id);
          add(visibleText(label) || node.getAttribute("aria-label") || node.value);
        });
      }
      const describedBy = String((control.getAttribute("aria-describedby") || ""))
        .split(/\s+/)
        .map((id) => id && document.getElementById(id))
        .filter(Boolean)
        .map((node) => visibleText(node))
        .filter(Boolean)
        .join(" ");
      if (/^[1-9]\d*\s+item(?:s)?\s+selected\b/i.test(describedBy)) add(describedBy);
      const activeId = control.getAttribute("aria-activedescendant");
      if (!expanded && activeId) add(visibleText(document.getElementById(activeId)));
      return { values, expanded };
    }, context);
  } catch (error) {}
  const values = Array.isArray(state && state.values) ? state.values.slice() : [];
  const strictCommittedSelection = requiresStrictComboboxCommitReadback(field);
  if (!(state && state.expanded) && directValue && !strictCommittedSelection && !isPlaceholderSelection(directValue) && !values.includes(directValue)) {
    values.push(directValue);
  }
  return { values, expanded: Boolean(state && state.expanded) };
}

async function verifyControlSelection(page, field, answer) {
  const { values, expanded } = await controlSelectionReadback(page, field);
  const selected = values.find((value) => selectionMatchesAnswer(value, answer));
  if (selected) return selected;
  if (expanded) throw new Error("dropdown remained open without a committed selection");
  if (!values.length) return null;
  throw new Error("dropdown selection readback does not match requested answer");
}

async function selectWorkdayNestedPromptOption(page, answer, field) {
  // Workday's source-of-application menu can expose a parent row such as
  // "Website" and only commit a value after a radio in a nested prompt is
  // clicked. A normal text click leaves the form visually highlighted but
  // invalid, which is why "Company website" was unreliable across roles.
  try {
    const labels = Array.from(new Set([answer, ...answerAliases(answer)]));
    let candidate = null;
    for (const label of labels) {
      const item = page.locator('[data-automation-id="menuItem"]').filter({ hasText: label }).last();
      if (await item.count() && await item.isVisible()) {
        candidate = item;
        break;
      }
    }
    if (!candidate) return null;
    await candidate.click();
    await page.waitForTimeout(500);
    let radio = null;
    for (const label of labels) {
      const matchingRadio = page.locator('[data-automation-id="activeListContainer"] [data-automation-id="radioBtn"]').filter({ hasText: label }).last();
      if (await matchingRadio.count() && await matchingRadio.isVisible()) {
        radio = matchingRadio;
        break;
      }
    }
    if (!radio) {
      const onlyRadio = page.locator('[data-automation-id="activeListContainer"] [data-automation-id="radioBtn"]').first();
      if (await onlyRadio.count() === 1 && await onlyRadio.isVisible()) radio = onlyRadio;
    }
    if (radio) {
      await radio.click();
      await page.waitForTimeout(500);
    }
    return await verifyControlSelection(page, field, answer);
  } catch (error) {
    return null;
  }
}

async function applyFill(page, f, plan) {
  if (plan.action === "check" && plan.optionId) {
    const locator = page.locator(attrSelector("id", plan.optionId)).first();
    await checkedWithFallback(locator);
    return;
  }
  if (plan.action === "check" && plan.optionAutofillId) {
    const locator = page.locator(attrSelector("data-job-agent-autofill-index", plan.optionAutofillId)).first();
    await checkedWithFallback(locator);
    return;
  }
  if (plan.action === "buttonclick" && (plan.optionAutofillId || plan.optionText)) {
    const ashbySelected = await clickAshbyButtonGroup(page, f, plan);
    if (ashbySelected === true) return;
    if (ashbySelected === false) throw new Error("Ashby button option did not retain selected state");
    if (!plan.optionAutofillId) throw new Error("button option had no autofill selector and Ashby text click failed");
    const locator = page.locator(attrSelector("data-job-agent-autofill-index", plan.optionAutofillId)).first();
    await locator.click();
    if (typeof locator.locator === "function" && !(await isAshbyYesNoOption(locator))) {
      const hidden = locator.locator("xpath=..").locator("input[type='checkbox']").first();
      if (await hidden.count()) {
        const desired = norm(plan.optionText || plan.optionValue) === "yes";
        if ((await hidden.isChecked()) === desired) await hidden.dispatchEvent("click");
        await hidden.dispatchEvent("click");
        if ((await hidden.isChecked()) !== desired) throw new Error("hidden Yes/No input state did not update");
      }
    }
    return;
  }
  if (plan.action === "checkmany") {
    for (const option of (plan.options || [])) {
      await checkedWithFallback(page.locator(attrSelector("data-job-agent-autofill-index", option.autofillId)).first());
    }
    return;
  }
  if (plan.action === "check" && plan.groupName && plan.optionValue) {
    // radio without id: target by name + value
    const locator = page.locator(attrSelector("name", plan.groupName) + attrSelector("value", plan.optionValue)).first();
    await checkedWithFallback(locator);
    return;
  }
  const sel = selectorFor(f);
  if (plan.action === "fill") {
    if (!sel) throw new Error("no selector");
    const segmentedReadback = await fillWorkdayDateSection(page, f, plan.value);
    if (segmentedReadback !== null) return;
    let locator = page.locator(sel).first();
    locator = await recoverTextFillLocator(page, f, locator);
    const fillValue = await normalizeDateInputValue(locator, plan.value);
    await locator.fill(fillValue);
    if (typeof locator.press === "function") await locator.press("Tab").catch(() => {});
    const readback = await locator.inputValue().catch(() => "");
    if (!readback && fillValue) {
      await locator.click();
      if (typeof locator.pressSequentially === "function") {
        await locator.pressSequentially(fillValue, { delay: 10 });
      } else {
        await locator.fill(fillValue);
      }
      if (typeof locator.press === "function") await locator.press("Tab").catch(() => {});
      const retryReadback = await locator.inputValue().catch(() => "");
      if (!retryReadback) throw new Error("fill readback empty after setting non-empty value");
    }
  } else if (plan.action === "select") {
    if (!sel) throw new Error("no selector");
    await page.locator(sel).first().selectOption({ label: plan.value });
  } else if (plan.action === "upload") {
    if (!sel) throw new Error("no selector");
    await page.locator(sel).first().setInputFiles(plan.value);
  } else if (plan.action === "check") {
    if (!sel) throw new Error("no selector");
    await checkedWithFallback(page.locator(sel).first());
  } else if (plan.action === "combobox") {
    if (!sel) throw new Error("no selector");
    const steps = String(plan.value || "").split(/\s*>\s*/).map((s) => s.trim()).filter(Boolean);
    const supportsTextEntry = ["input", "textarea"].includes(String(f.tag || "").toLowerCase()) || Boolean(f.contentEditable);
    let verifiedSelection = null;
    if (steps.length === 1 && isSchoolComboboxField(f)) {
      const directSchool = await selectGreenhouseSchoolCombobox(page, sel, f, steps[0]);
      if (directSchool) return directSchool;
    }
    for (let i = 0; i < steps.length; i++) {
      const step = steps[i];
      const fieldLabel = norm(f.label || "");
      const countryLikeField = String(f.id || "").toLowerCase() === "country"
        || fieldLabel.startsWith("country")
        || fieldLabel.includes("country phone code")
        || fieldLabel.includes("phone country code");
      const searchStep = (fieldLabel.includes("location") || fieldLabel.includes("city"))
        ? (step.split(",")[0].trim() || step)
        : step;
      const schoolLikeField = isSchoolComboboxField(f);
      let clicked = null;
      let nestedPromptSelection = null;
      for (let attempt = 0; attempt < 3 && !clicked; attempt++) {
        if (i === 0 || attempt > 0) {
          await page.locator(sel).first().click().catch(() => {});
          await page.waitForTimeout(150 + attempt * 250);
          if (String(f.id || "") === "country" || (norm(f.label || "").startsWith("country") && step.includes("+"))) {
            if (typeof page.getByRole === "function") {
              await page.getByRole("option", { name: step, exact: true }).first().click({ timeout: 3000 }).then(() => {
                clicked = step;
              }).catch(() => {});
            }
            if (clicked) break;
          }
          if (["yes", "no"].includes(norm(step))) {
            if (typeof page.getByRole === "function") {
              await page.getByRole("option", { name: step, exact: true }).first().click({ timeout: 3000 }).then(() => {
                clicked = step;
              }).catch(() => {});
            }
            if (clicked) break;
          }
          if (["i don t wish to answer", "i don't wish to answer"].includes(norm(step))) {
            if (typeof page.getByRole === "function") {
              await page.getByRole("option", { name: "I don't wish to answer", exact: true }).first().click({ timeout: 3000 }).then(() => {
                clicked = "I don't wish to answer";
              }).catch(() => {});
            }
            if (clicked) break;
          }
          if (typeof page.getByRole === "function") {
            await page.getByRole("option", { name: step, exact: true }).first().click({ timeout: 3000 }).then(() => {
              clicked = step;
            }).catch(() => {});
          }
          if (clicked) break;
          if (schoolLikeField) {
            await typeIntoComboboxSearch(page, sel, f, searchStep);
            if (typeof page.getByRole === "function") {
              await page.getByRole("option", { name: step, exact: true }).first().click({ timeout: 3000 }).then(() => {
                clicked = step;
              }).catch(() => {});
            }
            if (clicked) break;
            clicked = await clickVisibleOptionWithPlaywright(page, step, f);
            if (clicked) break;
            if (page.keyboard && typeof page.keyboard.press === "function") {
              await page.keyboard.press("Enter").catch(() => {});
              await page.waitForTimeout(450);
              const verifiedSchool = await verifyControlSelection(page, f, step).catch(() => null);
              if (verifiedSchool) {
                clicked = verifiedSchool;
                break;
              }
            }
          }
          if (supportsTextEntry) {
            await page.locator(sel).first().fill(searchStep).catch(() => {});
            await page.waitForTimeout(350 + attempt * 350);
            if (!countryLikeField && page.keyboard && typeof page.keyboard.press === "function") {
              await page.keyboard.press("Enter").catch(() => {});
            }
            await page.waitForTimeout(250);
            if (fieldLabel.includes("how did you hear")) {
              nestedPromptSelection = await selectWorkdayNestedPromptOption(page, step, f);
            }
            clicked = nestedPromptSelection || await clickVisibleOptionWithPlaywright(page, step, f);
            if (clicked) break;
            const verifiedAfterEnter = await verifyControlSelection(page, f, step).catch(() => null);
            if (verifiedAfterEnter) {
              clicked = verifiedAfterEnter;
              break;
            }
          } else {
            if (fieldLabel.includes("how did you hear")) {
              nestedPromptSelection = await selectWorkdayNestedPromptOption(page, step, f);
            }
            clicked = nestedPromptSelection || await clickVisibleOptionWithPlaywright(page, step, f);
            if (clicked) break;
            if (page.keyboard && typeof page.keyboard.insertText === "function") {
              await page.keyboard.insertText(searchStep).catch(() => {});
            } else if (page.keyboard && typeof page.keyboard.type === "function") {
              await page.keyboard.type(searchStep).catch(() => {});
            }
            await page.waitForTimeout(350 + attempt * 350);
          }
        }
        if (!clicked) {
          if (fieldLabel.includes("how did you hear")) {
            nestedPromptSelection = await selectWorkdayNestedPromptOption(page, step, f);
          }
          clicked = nestedPromptSelection || await clickVisibleOptionWithPlaywright(page, step, f);
        }
      }
      if (!clicked) {
        if (steps.length === 1) {
          let current = "";
          try {
            current = await controlReadback(page.locator(sel).first(), f);
          } catch (e) {}
          const cn = norm(current);
          const want = norm(step);
          if (!fieldLabel.includes("location") && !fieldLabel.includes("city") && cn && (cn.includes(want) || want.includes(cn))) {
            const verified = await verifyControlSelection(page, f, step);
            if (verified) return verified;
          }
        }
        await page.locator(sel).first().click().catch(() => {});
        await page.waitForTimeout(300);
        const available = await page.evaluate(() => Array.from(new Set(
          Array.from(document.querySelectorAll('[data-automation-id="menuItem"], [role="option"], [data-automation-id="radioBtn"], li'))
            .filter((node) => node.offsetParent || node.getClientRects().length)
            .map((node) => (node.textContent || "").replace(/\s+/g, " ").trim())
        )).filter(Boolean).slice(0, 100)).catch(() => []);
        const fallbackChoice = available.find((text) => optionMatches(text, step));
        if (fallbackChoice) {
          if (typeof page.getByRole === "function") {
            await page.getByRole("option", { name: fallbackChoice, exact: true }).first().click({ timeout: 3000 }).then(() => {
              clicked = fallbackChoice;
            }).catch(() => {});
          }
          if (!clicked) {
            await page.locator('[data-automation-id="menuItem"]').filter({ hasText: fallbackChoice }).last().click({ timeout: 3000 }).then(() => {
              clicked = fallbackChoice;
            }).catch(() => {});
          }
          if (!clicked && typeof page.getByText === "function") {
            await page.getByText(fallbackChoice, { exact: true }).last().click({ timeout: 3000 }).then(() => {
              clicked = fallbackChoice;
            }).catch(() => {});
          }
          if (clicked) {
            await page.waitForTimeout(500);
          }
        }
	        if (clicked) {
	          await page.waitForTimeout(1000);
	          verifiedSelection = await verifyControlSelection(page, f, step).catch(() => null);
	          if (verifiedSelection && schoolLikeField) {
	            await commitSchoolComboboxNativeValue(page, sel, verifiedSelection);
          }
          continue;
        }
        throw new Error("no combobox option matches saved answer");
      }
      await page.waitForTimeout(1000);
      if (i === steps.length - 1) {
        verifiedSelection = await verifyControlSelection(page, f, step);
        if (!verifiedSelection) {
          const visual = await page.locator(sel).first()
            .locator("xpath=ancestor::div[contains(@class,'select-shell')][1]")
            .textContent()
            .catch(() => "");
          const normalizedVisual = norm(visual || "");
          const normalizedStep = norm(step);
	          if (normalizedStep && (
	            normalizedVisual.includes(normalizedStep)
	            || normalizedStep.includes(normalizedVisual)
	            || (String(visual || "").includes("+1") && step.includes("+1"))
	          )) verifiedSelection = step;
	        }
	        if (verifiedSelection && schoolLikeField) {
	          await commitSchoolComboboxNativeValue(page, sel, verifiedSelection);
	        }
	      }
    }
    if (!verifiedSelection && f.required) {
      const pageUrl = String(page.url && page.url() || "").toLowerCase();
      if (pageUrl.includes("greenhouse.io")) {
        throw new Error("Greenhouse option click did not commit a selected value");
      }
      throw new Error("required dropdown selection could not be verified");
    }
    return verifiedSelection;
  } else if (plan.action === "customselect") {
    if (!sel) throw new Error("no selector");
    await page.locator(sel).first().click();
    await page.waitForTimeout(700);
    if (!(await clickVisibleOptionWithPlaywright(page, plan.value, f))) {
      if (page.keyboard && typeof page.keyboard.press === "function") {
        await page.keyboard.press("Escape").catch(() => {});
      }
      throw new Error("no button dropdown option matches saved answer");
    }
    await page.waitForTimeout(500);
    const verifiedSelection = await verifyControlSelection(page, f, plan.value);
    if (!verifiedSelection && f.required) throw new Error("required dropdown selection could not be verified");
    return verifiedSelection;
  }
}

async function clickVisibleOptionWithPlaywright(page, answer, field = null) {
  const option = await page.evaluate((payload) => {
    const normLocal = (s) => (s || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
    const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
    const stateNames = {
      al: "alabama", ak: "alaska", az: "arizona", ar: "arkansas", ca: "california",
      co: "colorado", ct: "connecticut", de: "delaware", fl: "florida", ga: "georgia",
      hi: "hawaii", id: "idaho", il: "illinois", in: "indiana", ia: "iowa", ks: "kansas",
      ky: "kentucky", la: "louisiana", me: "maine", md: "maryland", ma: "massachusetts",
      mi: "michigan", mn: "minnesota", ms: "mississippi", mo: "missouri", mt: "montana",
      ne: "nebraska", nv: "nevada", nh: "new hampshire", nj: "new jersey", nm: "new mexico",
      ny: "new york", nc: "north carolina", nd: "north dakota", oh: "ohio", ok: "oklahoma",
      or: "oregon", pa: "pennsylvania", ri: "rhode island", sc: "south carolina",
      sd: "south dakota", tn: "tennessee", tx: "texas", ut: "utah", vt: "vermont",
      va: "virginia", wa: "washington", wv: "west virginia", wi: "wisconsin",
      wy: "wyoming", dc: "district of columbia"
    };
    const expandLocation = (s) => normLocal(s).split(" ").flatMap((token) => {
      if (token === "us" || token === "usa") return ["united", "states"];
      return (stateNames[token] || token).split(" ");
    }).join(" ");
	    const want = normLocal(payload.answer);
	    const wants = [want];
	    for (const alias of (payload.aliases || [])) {
	      const n = normLocal(alias);
	      if (n && !wants.includes(n)) wants.push(n);
	    }
    if (["prefer not to say", "prefer not to answer", "decline", "decline to answer"].includes(want)) {
      wants.push("i do not wish to self identify", "i do not wish to answer", "not declared", "declined to state");
    }
    if (["company website", "company site", "company careers", "company career site", "career site", "career website", "careers website", "careers site"].includes(want)) {
      wants.push("corporate website", "career site", "careers website", "career website", "company careers", "careers site", "company careers page website");
    }
    if (["master s degree", "masters degree", "master degree"].includes(want)) {
      wants.push("master degree", "masters degree", "master s degree");
    }
    if (["east asian", "asian"].includes(want)) {
      wants.push("asian", "asian not hispanic or latino");
    }
    // Normalize verbose LLM-generated yes/no intents back to simple option labels.
    if (want.startsWith("yes") && want.length > 3) wants.push("yes");
    if (want.startsWith("no") && want.length > 2) wants.push("no");
    // Work authorization / sponsorship intents.
    if (want.includes("authorized") && want.includes("work") && want.includes("any employer")) {
      wants.push("authorized to work", "i am authorized to work for any employer", "yes");
    }
    if (want.includes("sponsor")) wants.push("require sponsorship", "yes");
    // Pronoun intents.
    if (want.includes("he") && want.includes("him")) wants.push("he him");
    if (want.includes("she") && want.includes("her")) wants.push("she her");
    if (want.includes("they") && want.includes("them")) wants.push("they them");
    const context = payload.field || {};
    const control = context.id ? document.getElementById(context.id) :
      (context.autofillId ? document.querySelector(`[data-job-agent-autofill-index="${context.autofillId}"]`) : document.activeElement);
    const controlledIds = [
      context.ariaControls,
      context.ariaOwns,
      control && control.getAttribute && control.getAttribute("aria-controls"),
      control && control.getAttribute && control.getAttribute("aria-owns"),
    ].filter(Boolean).join(" ")
      .split(/\s+/)
      .map((id) => id && document.getElementById(id))
      .filter(Boolean);
    const listboxes = Array.from(new Set([
      ...Array.from(document.querySelectorAll('[role="listbox"], [role="menu"]')),
      ...Array.from(document.querySelectorAll('[role="tree"], [role="dialog"], [data-automation-id="activeListContainer"], [data-popper-placement], [data-radix-popper-content-wrapper], [data-headlessui-state~="open"]')),
    ])).filter(visible);
	    let roots = controlledIds.filter(visible);
	    if (listboxes.length) {
	      const labelled = control && control.id
	        ? listboxes.filter((node) => String((node.getAttribute && node.getAttribute("aria-labelledby")) || "").split(/\s+/).includes(control.id))
	        : [];
	      const candidates = labelled.length ? labelled : listboxes;
      const controlBox = control && control.getBoundingClientRect ? control.getBoundingClientRect() : null;
      candidates.sort((left, right) => {
        if (!controlBox) return 0;
        const distance = (node) => {
          const box = node.getBoundingClientRect();
          return Math.abs(box.top - controlBox.bottom) + Math.abs(box.left - controlBox.left);
	        };
	        return distance(left) - distance(right);
	      });
	      roots = Array.from(new Set([...roots, ...candidates.slice(0, roots.length ? 2 : 1)]));
	    }
    const optionSelector = '[role="option"], [role="menuitem"], [role="radio"], [role="checkbox"], [data-automation-id="menuItem"], [data-option-value], [data-value], li, button';
    const optionNodes = roots.length
      ? roots.flatMap((root) => [
        ...(root.matches && root.matches(optionSelector) ? [root] : []),
        ...Array.from(root.querySelectorAll(optionSelector)),
      ])
      : Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [data-automation-id="menuItem"]'));
    const options = Array.from(new Set(optionNodes))
      .filter(visible)
      .map((node, index) => {
        const autofillId = `option-${index}`;
        const canTarget = typeof node.setAttribute === "function";
        if (canTarget) node.setAttribute("data-job-agent-option-index", autofillId);
        const text = (node.textContent || "").replace(/\s+/g, " ").trim();
        const attribute = (name) => typeof node.getAttribute === "function" ? node.getAttribute(name) : "";
        const value = attribute("aria-label") || attribute("data-option-value") || attribute("data-value") || "";
        return { id: node.id || "", text, value, autofillId: canTarget ? autofillId : null };
      })
      .filter((node) => node.text || node.value);
    const score = (node) => {
      const text = normLocal(`${node.text} ${node.value}`);
      return wants.reduce((best, candidate) => {
        if (text === candidate) return Math.max(best, 100);
        const expandedText = expandLocation(text);
        const expandedCandidate = expandLocation(candidate);
        if (expandedText === expandedCandidate) return Math.max(best, 95);
        if (expandedText.includes(expandedCandidate)) return Math.max(best, 70);
        if (expandedCandidate.includes(expandedText)) return Math.max(best, 60);
        return best;
      }, 0);
    };
    const option = options.map((node, index) => ({ ...node, index, score: score(node) }))
      .filter((node) => node.score > 0)
      .sort((a, b) => b.score - a.score || a.index - b.index)[0];
    if (!option) return null;
    return option;
	  }, { answer, aliases: answerAliases(answer), field: field || {} });
  if (!option) return null;
  const display = option.text || option.value || "";
  if (option.autofillId !== undefined && option.autofillId !== null) {
    try {
      await page.locator(attrSelector("data-job-agent-option-index", option.autofillId)).first().click({ timeout: 3000 });
      return display;
    } catch (e) {
      // Keep compatibility with controls that prevent attribute-targeted clicks.
    }
  }
  if (option.text) {
    try {
      await page.locator('[data-automation-id="menuItem"]').filter({ hasText: option.text }).last().click({ timeout: 3000 });
      return option.text;
    } catch (e) {
      // Fall through to exact text.
    }
    try {
      await page.getByText(option.text, { exact: true }).last().click({ timeout: 3000 });
      return option.text;
    } catch (e) {
      // Fall through to the stable id selector when exact text is ambiguous.
    }
  }
  if (option.id) await page.locator(attrSelector("id", option.id)).first().click();
  else await page.getByText(display, { exact: false }).first().click();
  return display;
}

async function locatorAttribute(locator, attribute) {
  try {
    return typeof locator.getAttribute === "function" ? await locator.getAttribute(attribute) : "";
  } catch (e) {
    return "";
  }
}

async function selectableState(locator) {
  const role = await locatorAttribute(locator, "role");
  if (["checkbox", "radio", "switch"].includes(String(role || "").toLowerCase())) {
    const ariaChecked = String(await locatorAttribute(locator, "aria-checked")).toLowerCase();
    const ariaPressed = String(await locatorAttribute(locator, "aria-pressed")).toLowerCase();
    const dataState = String(await locatorAttribute(locator, "data-state")).toLowerCase();
    return ariaChecked === "true" || ariaPressed === "true" || ["checked", "on", "selected"].includes(dataState);
  }
  if (typeof locator.isChecked === "function") return locator.isChecked().catch(() => false);
  return false;
}

async function checkedWithFallback(locator) {
  const role = await locatorAttribute(locator, "role");
  if (["checkbox", "radio", "switch"].includes(String(role || "").toLowerCase())) {
    if (!(await selectableState(locator))) await locator.click({ timeout: 3000 });
    if (!(await selectableState(locator))) throw new Error("ARIA choice did not retain selected state");
    return true;
  }
  try {
    await locator.check({ timeout: 3000 });
  } catch (e) {
    await locator.evaluate((node) => {
      node.checked = true;
      node.dispatchEvent(new Event("input", { bubbles: true }));
      node.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  return selectableState(locator);
}

async function clickAshbyButtonGroup(page, field, plan) {
  const label = String(field && field.label || "").trim();
  const optionText = String(plan.optionText || plan.optionValue || "").trim();
  if (!label || !optionText) return null;
  try {
    const marker = await page.evaluate((payload) => {
      const localNorm = (value) => String(value || "")
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      const labelNorm = localNorm(payload.label);
      const optionNorm = localNorm(payload.optionText);
      if (!labelNorm || !optionNorm) return null;
      const entries = Array.from(document.querySelectorAll(
        ".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]"
      ));
      const entry = entries.find((candidate) => {
        const labelNode = candidate.querySelector("label,.ashby-application-form-question-title");
        const text = localNorm(labelNode && labelNode.textContent ? labelNode.textContent : candidate.textContent);
        return text === labelNorm || text.includes(labelNorm) || labelNorm.includes(text);
      });
      let button = entry ? Array.from(entry.querySelectorAll("button")).find((candidate) =>
        localNorm(candidate.textContent || candidate.value) === optionNorm
      ) : null;
      if (!button) {
        const exactButtons = Array.from(document.querySelectorAll("button")).filter((candidate) =>
          localNorm(candidate.textContent || candidate.value) === optionNorm
        );
        button = exactButtons.find((candidate) => {
          const container = candidate.closest(".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path],fieldset,section,div");
          const text = localNorm(container && container.textContent);
          return text.includes(labelNorm);
        }) || (exactButtons.length === 1 ? exactButtons[0] : null);
      }
      let target = button;
      if (!target) {
        const root = entry || document;
        const textFor = (node) => {
          if (!node) return "";
          if (node.matches && node.matches('input[type="radio"],input[type="checkbox"]')) {
            const explicit = node.id
              ? Array.from(document.querySelectorAll("label")).find((label) =>
                  label.htmlFor === node.id || label.getAttribute("for") === node.id
                )
              : null;
            const wrapping = node.closest("label");
            const parent = node.parentElement;
            return localNorm(node.getAttribute("aria-label") || node.value || (explicit && explicit.textContent) || (wrapping && wrapping.textContent) || (parent && parent.textContent));
          }
          return localNorm(node.getAttribute && node.getAttribute("aria-label") || node.textContent || node.value);
        };
        const candidates = Array.from(root.querySelectorAll('label,[role="radio"],[role="button"],input[type="radio"],input[type="checkbox"]'));
        target = candidates.find((candidate) => textFor(candidate) === optionNorm)
          || candidates.find((candidate) => textFor(candidate).includes(optionNorm));
      }
      if (!target) return null;
      const marker = `ashby-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      target.setAttribute("data-job-agent-ashby-click-target", marker);
      return marker;
    }, { label, optionText });
    if (!marker) return null;
    const locator = page.locator(attrSelector("data-job-agent-ashby-click-target", marker)).first();
    await locator.scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
    const optionNorm = norm(optionText);
    const desiredYes = optionNorm === "yes";
    const yesNoOption = ["yes", "no"].includes(optionNorm);
    let state = await page.evaluate((marker) => {
      const target = document.querySelector(`[data-job-agent-ashby-click-target="${marker}"]`);
      if (!target) return null;
      const entry = target.closest(".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]");
      const input = target.matches && target.matches('input[type="radio"],input[type="checkbox"]')
        ? target
        : (target.querySelector && target.querySelector('input[type="radio"],input[type="checkbox"]')) ||
          (entry && entry.querySelector('input[type="radio"],input[type="checkbox"]'));
      const style = window.getComputedStyle(target);
      const active = String(target.className || "").includes("_active_") ||
        target.getAttribute("aria-pressed") === "true" ||
        target.getAttribute("aria-checked") === "true" ||
        target.getAttribute("aria-selected") === "true" ||
        ["checked", "on", "selected"].includes(target.getAttribute("data-state")) ||
        (style.color === "rgb(255, 255, 255)" && style.backgroundColor !== "rgba(0, 0, 0, 0)");
      return { active, checked: input ? Boolean(input.checked) : null };
    }, marker);
    if (state && (state.active || (yesNoOption && state.checked === desiredYes) || (!yesNoOption && state.checked === true))) return true;
    await locator.click({ timeout: 3000 });
    await page.waitForTimeout(200);
    state = await page.evaluate((marker) => {
      const target = document.querySelector(`[data-job-agent-ashby-click-target="${marker}"]`);
      if (!target) return null;
      const entry = target.closest(".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]");
      const input = target.matches && target.matches('input[type="radio"],input[type="checkbox"]')
        ? target
        : (target.querySelector && target.querySelector('input[type="radio"],input[type="checkbox"]')) ||
          (entry && entry.querySelector('input[type="radio"],input[type="checkbox"]'));
      const style = window.getComputedStyle(target);
      const active = String(target.className || "").includes("_active_") ||
        target.getAttribute("aria-pressed") === "true" ||
        target.getAttribute("aria-checked") === "true" ||
        target.getAttribute("aria-selected") === "true" ||
        ["checked", "on", "selected"].includes(target.getAttribute("data-state")) ||
        (style.color === "rgb(255, 255, 255)" && style.backgroundColor !== "rgba(0, 0, 0, 0)");
      return { active, checked: input ? Boolean(input.checked) : null };
    }, marker);
    return Boolean(state && (state.active || (yesNoOption && state.checked === desiredYes) || (!yesNoOption && state.checked === true)));
  } catch (e) {
    return null;
  }
}

async function isAshbyYesNoOption(locator) {
  try {
    return Boolean(await locator.evaluate((node) => Boolean(
      node &&
      node.closest &&
      node.closest(".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]") &&
      String(node.className || "").includes("_option_")
    )));
  } catch (e) {
    return false;
  }
}

async function auditRequiredFields(page) {
  const findings = await page.evaluate(() => {
    const visible = (node) => {
      if (!node || node.getAttribute("aria-hidden") === "true") return false;
      if (node.offsetParent) return true;
      const rects = node.getClientRects ? node.getClientRects() : [];
      const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
      return Boolean(rects.length) && !(style && (style.display === "none" || style.visibility === "hidden"));
    };
    const labelFor = (control) => {
      const ids = String(control.getAttribute("aria-labelledby") || "").split(/\s+/)
        .map((id) => id && document.getElementById(id))
        .filter(Boolean)
        .map((node) => (node.textContent || "").trim())
        .filter(Boolean)
        .join(" ");
      const cleanQuestionText = (text) => {
        const lines = (text || "").split("\n").map((line) => line.trim()).filter(Boolean);
        const keep = [];
        for (const line of lines) {
          if (line === "✱" || line === "*" || /^select(\.\.\.)?$/i.test(line) || /^(yes|no|upload|attach)$/i.test(line)) break;
          keep.push(line);
        }
        return keep.join(" ");
      };
      const optionLabelFor = (target) => {
        if (target.id) {
          const explicit = Array.from(document.querySelectorAll("label")).find((node) =>
            node.htmlFor === target.id || node.getAttribute("for") === target.id
          );
          if (explicit && explicit.textContent) return explicit.textContent.trim();
        }
        const wrapping = target.closest && target.closest("label");
        if (wrapping && wrapping.textContent) {
          const clone = wrapping.cloneNode(true);
          clone.querySelectorAll("select,input,textarea,button,[role='option'],[role='radio'],[role='checkbox']").forEach((node) => node.remove());
          const text = clone.textContent.trim();
          if (text) return text;
        }
        return target.getAttribute("aria-label") || target.getAttribute("data-value") ||
          target.getAttribute("data-option-value") || target.value || (target.textContent || "").trim() || "";
      };
      const textWithoutControls = (node) => {
        if (!node) return "";
        const clone = typeof node.cloneNode === "function" ? node.cloneNode(true) : node;
        if (clone && typeof clone.querySelectorAll === "function") {
          clone.querySelectorAll("input,textarea,select,button,[role='option'],[role='radio'],[role='checkbox']")
            .forEach((child) => child.remove());
        }
        return cleanQuestionText(clone.innerText || clone.textContent || "");
      };
      const isPromptNode = (node) => {
        if (!node || !node.getAttribute) return false;
        const marker = [
          node.tagName || "", node.id || "", node.className || "",
          node.getAttribute("role") || "", node.getAttribute("data-testid") || "",
          node.getAttribute("data-qa") || "", node.getAttribute("data-test") || "",
          node.getAttribute("data-field-label") || "", node.getAttribute("data-question") || "",
        ].join(" ").toLowerCase();
        return /(^|\s)(label|legend)(\s|$)|question|prompt|field.?title|form.?title|heading/.test(marker) ||
          /^H[1-6]$/.test(node.tagName || "") || node.getAttribute("role") === "heading";
      };
      const genericPromptLabel = (target) => {
        let node = target;
        for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
          const parent = node.parentElement;
          if (parent && parent.children) {
            const siblings = Array.from(parent.children);
            const index = siblings.indexOf(node);
            for (const sibling of siblings.slice(0, index).reverse()) {
              if (!isPromptNode(sibling)) continue;
              const text = textWithoutControls(sibling);
              if (text) return text;
            }
          }
          const candidates = typeof node.querySelectorAll === "function"
            ? Array.from(node.querySelectorAll(
              "label,legend,h1,h2,h3,h4,h5,h6,[role='heading'],[data-field-label],[data-question],[data-question-label],[data-label],[data-testid],[data-qa],[data-test]"
            )).filter((candidate) => candidate !== target && typeof candidate.contains === "function" && !candidate.contains(target) && isPromptNode(candidate))
            : [];
          for (const candidate of candidates) {
            const text = textWithoutControls(candidate);
            if (text) return text;
          }
        }
        return "";
      };
      const workdayFieldLabel = (target) => {
        const field = target.closest && target.closest('[data-automation-id^="formField-"]');
        if (!field) return "";
        const explicit = field.querySelector("label,[data-automation-id='formLabel']");
        const explicitText = explicit && explicit.textContent ? explicit.textContent.trim() : "";
        const isGenericCheckboxLabel = (
          (target.getAttribute("type") || "").toLowerCase() === "checkbox" &&
          /^(agree|accept|yes|i agree)$/i.test(explicitText)
        );
        let text = (explicitText && !isGenericCheckboxLabel) ? explicitText : (field.textContent || "");
        text = text.replace(/\b\d+\s+items?\s+selected\b.*$/i, "");
        text = text.replace(/\bExpanded\b.*$/i, "");
        text = text.replace(/\bError:.*$/i, "");
        return cleanQuestionText(text);
      };
      const workdayQuestionLabel = (target) => {
        const field = target.closest && target.closest('[data-automation-id^="formField-"]');
        if (!field || !field.textContent) return "";
        const raw = field.textContent.trim();
        if (raw.includes("*")) return (raw.split("*")[0] + "*").trim();
        return workdayFieldLabel(target);
      };
      if (workdayFieldLabel(control)) return workdayFieldLabel(control);
      if (ids) return cleanQuestionText(ids);
      if (control.id) {
        const label = Array.from(document.querySelectorAll("label")).find((node) =>
          node.htmlFor === control.id || node.getAttribute("for") === control.id
        );
        if (label && label.textContent) return cleanQuestionText(label.textContent.trim());
      }
      const wrapping = control.closest && control.closest("label");
      if (wrapping && wrapping.textContent) {
        const clone = wrapping.cloneNode(true);
        clone.querySelectorAll("select,input,textarea,button,[role='option'],[role='radio'],[role='checkbox']").forEach((node) => node.remove());
        const txt = clone.textContent.trim();
        if (txt) return cleanQuestionText(txt);
      }
      const fieldset = control.closest && control.closest("fieldset");
      const legend = fieldset && fieldset.querySelector("legend");
      if (legend && legend.textContent) return cleanQuestionText(legend.textContent);
      const genericPrompt = genericPromptLabel(control);
      if (genericPrompt) return genericPrompt;
      const describedBy = textForIds(control.getAttribute("aria-describedby"));
      return cleanQuestionText(
        control.getAttribute("aria-label") || control.getAttribute("placeholder") || describedBy || control.name || control.id || "required field"
      );
    };
    const groupLabelFor = (control) => {
      const cleanQuestionText = (text) => {
        const lines = (text || "").split("\n").map((line) => line.trim()).filter(Boolean);
        const keep = [];
        for (const line of lines) {
          if (line === "✱" || line === "*" || /^select(\.\.\.)?$/i.test(line) || /^(yes|no|upload|attach)$/i.test(line)) break;
          keep.push(line);
        }
        return keep.join(" ");
      };
      const textWithoutControls = (node) => {
        if (!node) return "";
        const clone = typeof node.cloneNode === "function" ? node.cloneNode(true) : node;
        if (clone && typeof clone.querySelectorAll === "function") {
          clone.querySelectorAll("input,textarea,select,button,[role='option'],[role='radio'],[role='checkbox']")
            .forEach((child) => child.remove());
        }
        return cleanQuestionText(clone.innerText || clone.textContent || "");
      };
      const workdayField = control.closest && control.closest('[data-automation-id^="formField-"]');
      if (workdayField && workdayField.textContent) {
        const raw = workdayField.textContent.trim();
        if (raw.includes("*")) return (raw.split("*")[0] + "*").trim();
      }
      const fieldset = control.closest && control.closest("fieldset");
      const legend = fieldset && fieldset.querySelector("legend");
      if (legend && legend.textContent) return cleanQuestionText(legend.textContent);
      const labelledBy = textForIds(control.getAttribute("aria-labelledby"));
      if (labelledBy) return cleanQuestionText(labelledBy);
      const root = control.closest && control.closest('[role="radiogroup"], fieldset, [role="group"], [data-automation-id^="formField-"]');
      if (root) {
        const rootText = textWithoutControls(root);
        if (rootText) return rootText;
      }
      return labelFor(control);
    };
    const isPlaceholder = (value) => /^(select|select one|choose|please select|--.*--)?$/i.test(String(value || "").trim());
    const selectedPresentation = (control) => {
      const expanded = control.getAttribute("aria-expanded") === "true";
      // React Select and similar widgets retain an empty text input after a
      // selection. The visible chip/value is the committed form value.
      const root = control.closest && control.closest('[class*="select__control"], [class*="select__value-container"]');
      if (root) {
        const selected = Array.from(root.querySelectorAll(
          '[class*="select__single-value"], [class*="select__multi-value__label"], [data-automation-id="selectedItem"], [aria-selected="true"]'
        )).map((node) => String(node.textContent || "").trim()).filter(Boolean);
        if (selected.length) return selected.join(" ");
      }
      const fieldRoot = control.closest && control.closest('[data-automation-id^="formField-"], [role="group"], fieldset');
      if (fieldRoot) {
        const selected = Array.from(fieldRoot.querySelectorAll(
          '[data-automation-id="selectedItem"]'
        )).map((node) => String(node.textContent || "").trim()).filter(Boolean);
        if (selected.length) return selected.join(" ");
        if (!expanded) {
          const genericSelected = Array.from(fieldRoot.querySelectorAll(
            '[aria-selected="true"], [aria-checked="true"], [data-state="selected"], [data-state="checked"], [data-state="on"]'
          )).map((node) => String(node.textContent || "").trim()).filter(Boolean);
          if (genericSelected.length) return genericSelected.join(" ");
        }
      }
      const describedBy = textForIds(control.getAttribute("aria-describedby"));
      if (/^[1-9]\d*\s+item(?:s)?\s+selected\b/i.test(describedBy)) return describedBy;
      return "";
    };
    const labelAppearsRequired = (label) => /(?:\*|✱)\s*$/.test(String(label || "").trim());
    // Use joined selectors to keep the audit separate from scrapeFields in
    // browser harnesses while still inspecting native controls in production.
    const nativeControls = document.querySelectorAll(["input", "textarea", "select"].join(","));
    const roleControls = document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="menu"], [role="radio"], [role="checkbox"], [role="switch"], [contenteditable="true"], [contenteditable="plaintext-only"]');
    const controls = Array.from(new Set([...nativeControls, ...roleControls]))
      .filter((control) => visible(control) && !control.disabled);
    const out = [];
    const seenRadioGroups = new Set();
    for (const control of controls) {
      const ashbyRequired = () => {
        const entry = control.closest && control.closest(
          '.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]'
        );
        const label = entry && entry.querySelector(
          'label,.ashby-application-form-question-title'
        );
        return Boolean(label && /(^|\s)[^\s]*required[^\s]*(\s|$)/i.test(String(label.className || "")));
      };
      const required = control.required || control.getAttribute("aria-required") === "true" ||
        Boolean(control.closest && control.closest('[aria-required="true"]')) || ashbyRequired() ||
        labelAppearsRequired(labelFor(control));
      if (!required) continue;
      const type = (control.getAttribute("type") || control.tagName || "").toLowerCase();
      const role = control.getAttribute("role") || "";
      let invalid = control.getAttribute("aria-invalid") === "true";
      let empty = false;
      let committed = "";
      if (type === "radio" || role === "radio") {
        const root = control.closest && control.closest('[role="radiogroup"], fieldset, [role="group"]');
        const group = root || control.name || control.getAttribute("aria-labelledby") || control.id;
        if (!group || seenRadioGroups.has(group)) continue;
        seenRadioGroups.add(group);
        empty = !controls.some((candidate) => {
          const candidateRole = candidate.getAttribute("role") || "";
          const candidateRoot = candidate.closest && candidate.closest('[role="radiogroup"], fieldset, [role="group"]');
          const sameGroup = root
            ? candidateRoot === root
            : (candidate.name || candidate.getAttribute("aria-labelledby") || candidate.id) === group;
          return sameGroup &&
            ((candidate.getAttribute("type") || "").toLowerCase() === "radio" || candidateRole === "radio") &&
            (candidate.checked || candidate.getAttribute("aria-checked") === "true");
        });
      } else if (type === "checkbox" || role === "checkbox" || role === "switch") {
        empty = !(control.checked || control.getAttribute("aria-checked") === "true" ||
          control.getAttribute("aria-pressed") === "true" || ["checked", "on", "selected"].includes(control.getAttribute("data-state")));
      } else if (type === "file") {
        empty = !(control.files && control.files.length);
      } else if (control.tagName.toLowerCase() === "select") {
        const selected = control.options && control.selectedIndex >= 0 ? control.options[control.selectedIndex] : null;
        empty = !String(control.value || "").trim() || Boolean(selected && (selected.disabled || isPlaceholder(selected.textContent)));
      } else {
        committed = selectedPresentation(control);
        if (role === "combobox" || control.getAttribute("aria-haspopup") || committed) {
          empty = !committed && isPlaceholder(
            control.value ||
            control.getAttribute("aria-valuetext") ||
            control.getAttribute("data-value") ||
            control.textContent
          );
          // The wrapper can retain aria-invalid until submit runs its next
          // validation cycle even when React has committed this selection.
          if (committed) invalid = false;
        } else if (control.isContentEditable) {
          empty = !String(control.textContent || "").trim();
        } else {
          empty = !String(control.value || "").trim();
        }
      }
      const nativeInvalid = committed ? false
        : (typeof control.checkValidity === "function" ? !control.checkValidity() : false);
      if (!empty && !nativeInvalid) invalid = false;
      else invalid = invalid || nativeInvalid;
      if (empty || invalid) {
        const auditLabel = (
          type === "radio" || role === "radio" ||
          type === "checkbox" || role === "checkbox" || role === "switch"
        ) ? groupLabelFor(control) : labelFor(control);
        out.push({
          label: String(auditLabel).replace(/\s+/g, " ").trim(),
          reason: invalid ? "browser reports field as invalid" : "required field remains empty after fill",
        });
      }
    }
    return out.slice(0, 30);
  }).catch(() => []);
  return Array.isArray(findings) ? findings.filter((item) => item && item.label) : [];
}

function isEmailVerificationField(label) {
  const normalized = norm(label);
  return [
    "security code",
    "verification code",
    "one time code",
    "one time password",
    "8 character code",
    "8 character security code",
  ].some((phrase) => normalized.includes(phrase));
}

function sameRequiredField(left, right) {
  const leftNormalized = norm(left);
  const rightNormalized = norm(right);
  return Boolean(leftNormalized && rightNormalized) && (
    leftNormalized === rightNormalized ||
    leftNormalized.includes(rightNormalized) ||
    rightNormalized.includes(leftNormalized)
  );
}

function hasSuccessfulFillReadback(label, filled) {
  for (const item of filled || []) {
    if (!sameRequiredField(label, item && item.label || "")) continue;
    const readback = item && item.readback;
    if (readback === null || readback === undefined || readback === false || readback === "" || readback === "readback-error") continue;
    if (typeof readback === "string" && readback.startsWith("selected: ")) return true;
    if (readback === "file-selected") return true;
    if (String(readback).trim()) return true;
  }
  return false;
}

function invalidFindingCanUseSuccessfulReadback(label) {
  const n = norm(label);
  return n.includes("email") ||
    (n.includes("full time") && n.includes("internship")) ||
    n.includes("when will you graduate");
}

function filterSuccessfulReadbackReviews(review, filled) {
  const staleReasons = new Set([
    "browser reports field as invalid",
    "required field remains empty after fill",
  ]);
  return (review || []).filter((item) => !(
    item &&
    item.blocking !== false &&
    staleReasons.has(String(item.reason || "")) &&
    invalidFindingCanUseSuccessfulReadback(String(item.label || "")) &&
    hasSuccessfulFillReadback(String(item.label || ""), filled)
  ));
}

function isOfficeLocationGroupLabel(label) {
  const n = norm(label);
  return n.includes("which office location") ||
    n.includes("office locations") ||
    n.includes("location s are you interested");
}

function hasCheckedOfficeLocation(filled) {
  for (const item of filled || []) {
    const label = String(item && item.label || "");
    if (!looksLikeLocationCheckboxOption(label)) continue;
    const readback = String(item && item.readback || "").trim().toLowerCase();
    const action = String(item && item.action || "").trim().toLowerCase();
    if ((action === "check" || action === "checkmany") && ["checked", "selected", "true"].includes(readback)) return true;
    if ((action === "check" || action === "checkmany") && readback.startsWith("selected:")) return true;
  }
  return false;
}

function appendRequiredAudit(review, findings, filled = []) {
  for (const finding of findings || []) {
    const label = String(finding.label || "required field");
    if (isEmailVerificationField(label)) continue;
    if (
      (
        String(finding.reason || "") === "required field remains empty after fill" ||
        (
          String(finding.reason || "") === "browser reports field as invalid" &&
          invalidFindingCanUseSuccessfulReadback(label)
        )
      ) &&
      hasSuccessfulFillReadback(label, filled)
    ) continue;
    if (isOfficeLocationGroupLabel(label) && hasCheckedOfficeLocation(filled)) continue;
    if ((review || []).some((item) => sameRequiredField(label, item && item.label || ""))) continue;
    review.push({
      label,
      reason: finding.reason || "required field remains empty after fill",
      sensitive: isSensitive(label),
      blocking: true,
    });
  }
}

async function repairInvalidRequiredFields(page, findings, profile) {
  const repaired = [];
  for (const finding of findings || []) {
    const label = String(finding && finding.label || "");
    if (!label || isEmailVerificationField(label)) continue;
    const value = mapTextValue(label, profile);
    if (!value) continue;
    const ok = await page.evaluate(({ label, value }) => {
      const norm = (text) => String(text || "")
        .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
        .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      const visible = (node) => {
        if (!node || node.disabled || node.getAttribute("aria-hidden") === "true") return false;
        if (node.offsetParent) return true;
        const rects = node.getClientRects ? node.getClientRects() : [];
        const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
        return Boolean(rects.length) && !(style && (style.display === "none" || style.visibility === "hidden"));
      };
      const textForIds = (ids) => String(ids || "").split(/\s+/)
        .map((id) => id && document.getElementById(id))
        .filter(Boolean)
        .map((node) => (node.textContent || "").trim())
        .filter(Boolean)
        .join(" ");
      const clean = (text) => String(text || "").replace(/[✱*]/g, " ").replace(/\s+/g, " ").trim();
      const labelFor = (control) => {
        const ashbyEntry = control.closest && control.closest(
          '.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]'
        );
        if (ashbyEntry) {
          const ashbyLabel = ashbyEntry.querySelector('label,.ashby-application-form-question-title,[data-field-label]');
          if (ashbyLabel && ashbyLabel.textContent) return clean(ashbyLabel.textContent);
        }
        const byIds = textForIds(control.getAttribute("aria-labelledby"));
        if (byIds) return clean(byIds);
        if (control.id) {
          const explicit = Array.from(document.querySelectorAll("label")).find((node) =>
            node.htmlFor === control.id || node.getAttribute("for") === control.id
          );
          if (explicit && explicit.textContent) return clean(explicit.textContent);
        }
        const wrapping = control.closest && control.closest("label");
        if (wrapping && wrapping.textContent) return clean(wrapping.textContent);
        const describedBy = textForIds(control.getAttribute("aria-describedby"));
        return clean(control.getAttribute("aria-label") || control.getAttribute("placeholder") || describedBy || control.name || control.id);
      };
      const wanted = norm(label);
      const controls = Array.from(document.querySelectorAll(
        'input,textarea,[role="textbox"],[role="searchbox"],[contenteditable="true"],[contenteditable="plaintext-only"]'
      )).filter((control) => {
        const type = String(control.getAttribute("type") || control.tagName || "").toLowerCase();
        return visible(control) && !["hidden", "file", "checkbox", "radio", "submit", "button"].includes(type);
      });
      const ranked = controls
        .map((control) => {
          const actual = norm(labelFor(control));
          const exact = actual === wanted;
          const close = exact || actual.includes(wanted) || wanted.includes(actual);
          const invalid = control.getAttribute("aria-invalid") === "true" ||
            (typeof control.checkValidity === "function" && !control.checkValidity());
          const empty = !String(control.value || control.textContent || "").trim();
          return { control, close, score: (exact ? 8 : 0) + (close ? 4 : 0) + (invalid ? 2 : 0) + (empty ? 1 : 0) };
        })
        .filter((item) => item.close && item.score > 0)
        .sort((left, right) => right.score - left.score);
      const target = ranked.length ? ranked[0].control : null;
      if (!target) return false;
      try { target.focus(); } catch (_) {}
      if (target.isContentEditable) {
        target.textContent = value;
      } else {
        const proto = target.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
        if (descriptor && descriptor.set) descriptor.set.call(target, value);
        else target.value = value;
      }
      try {
        target.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
      } catch (_) {
        target.dispatchEvent(new Event("input", { bubbles: true }));
      }
      target.dispatchEvent(new Event("change", { bubbles: true }));
      target.dispatchEvent(new Event("blur", { bubbles: true }));
      return true;
    }, { label, value }).catch(() => false);
    if (ok) {
      console.log("Autofill repair field: " + label + " (direct-fill)");
      repaired.push({ label, action: "fill", readback: "filled" });
    }
  }
  return repaired;
}

function retainUnresolvedControlReviews(review, findings) {
  const unresolved = findings || [];
  return (review || []).filter((item) => {
    if (!item || item.blocking === false) return true;
    const reason = String(item.reason || "");
    // A React combobox can report a transient click/readback error even after
    // the browser has committed a value. The final required-field audit is the
    // source of truth for those control-level failures.
    if (reason !== "fill error: no combobox option matches saved answer") return true;
    const label = String(item.label || "");
    return unresolved.some((finding) => {
      return sameRequiredField(label, String(finding && finding.label || ""));
    });
  });
}

async function fillPage(page, profile) {
  let fields = await ensureApplicationFieldsReady(page);
  const ctx = {
    hasWork: await hasSectionHeading(page, "work (experience|history)|employment"),
    hasEdu: await hasSectionHeading(page, "education|academic"),
    hasCandidateAccountCreation: fields.some((field) => isCandidateAccountCreationField(field)),
  };
  const filled = [];
  const review = [];
  const handled = new Set();
  const fieldIdentity = (field) => {
    const kind = String(field.kind || "single");
    if (field.autofillId) return `marker\u0000${kind}\u0000${field.autofillId}`;
    if (field.id) return `id\u0000${kind}\u0000${field.id}`;
    return ["shape", kind, field.tag || "", field.type || "", field.name || "", field.label || "", field.section || ""].join("\u0000");
  };
  const fieldSignature = (batch) => (batch || []).map((field) => [
    field.kind || "", field.tag || "", field.type || "", field.id || "", field.name || "", field.label || "", field.required ? "1" : "0",
  ].join("\u0000")).sort().join("\n");
  const seenSignatures = new Set([fieldSignature(fields)]);
  const processFields = async (batch) => {
  for (const f of batch) {
    const identity = fieldIdentity(f);
    if (handled.has(identity)) continue;
    const plan = planField(f, profile, ctx);
    if (plan.action === "skip") {
      handled.add(identity);
      // fields handled by the repeatable-section filler are not review-required
      if (plan.reason && plan.reason.indexOf("handled by repeatable") === 0) continue;
      if (plan.reason === "button dropdown already selected") continue;
      if (plan.reason === "combobox already selected") continue;
      if (plan.reason === "field already selected") continue;
      if (plan.reason === "optional empty field") continue;
      if (plan.reason === "optional non-resume file field") continue;
      if (plan.reason === "approved No answer leaves checkbox unchecked") continue;
      if (plan.reason === "approved No answer has no matching optional option") continue;
      if (plan.reason === "approved No answer has no matching optional checkbox option") continue;
      if (plan.reason === "non-required unmapped field") continue;
      if (plan.reason === "honeypot field") continue;
      review.push({ label: f.label, reason: plan.reason || "skipped", sensitive: !!plan.sensitive, blocking: plan.blocking !== false });
      continue;
    }
    try {
      const appliedReadback = await applyFill(page, f, plan);
      // Read back the actual DOM value to self-verify the fill took effect.
      let readback = null;
      try {
        if (plan.action === "fill" || plan.action === "select") {
          const sel = selectorFor(f);
          if (sel) readback = await page.locator(sel).first().inputValue();
        } else if (plan.action === "check" && plan.optionId) {
          const checked = await selectableState(page.locator(attrSelector("id", plan.optionId)).first());
          readback = checked ? ("selected: " + (plan.optionText || plan.optionValue || "selected")) : false;
        } else if (plan.action === "check" && plan.optionAutofillId) {
          const checked = await selectableState(page.locator(attrSelector("data-job-agent-autofill-index", plan.optionAutofillId)).first());
          readback = checked ? ("selected: " + (plan.optionText || plan.optionValue || "selected")) : false;
        } else if (plan.action === "check") {
          const sel = selectorFor(f);
          if (sel) readback = await selectableState(page.locator(sel).first());
        } else if (plan.action === "upload") {
          readback = "file-selected";
        } else if (plan.action === "checkmany") {
          const selected = [];
          for (const option of (plan.options || [])) {
            const checked = await selectableState(page.locator(attrSelector("data-job-agent-autofill-index", option.autofillId)).first());
            if (checked) selected.push(option.value || option.label || "selected");
          }
          readback = selected.length ? ("selected: " + selected.join(", ")) : null;
        } else if (plan.action === "combobox" || plan.action === "customselect" || plan.action === "buttonclick") {
          readback = appliedReadback || "selected-unverified";
        }
      } catch (e) { readback = "readback-error"; }
      filled.push({ label: f.label, action: plan.action, value: plan.value, readback });
      handled.add(identity);
    } catch (e) {
      handled.add(identity);
      review.push({ label: f.label, reason: "fill error: " + e.message, sensitive: !!plan.sensitive, blocking: true });
    }
  }
  };
  const fileDescriptors = fields.filter((field) => field.type === "file")
    .sort((left, right) => Number(norm(left.label).includes("cover letter")) - Number(norm(right.label).includes("cover letter")));
  for (const descriptor of fileDescriptors) {
    fields = await scrapeFields(page);
    const current = fields.find((field) => field.type === "file" && (
      (descriptor.id && field.id === descriptor.id) || field.label === descriptor.label
    )) || descriptor;
    const before = filled.length;
    await processFields([current]);
    if (filled.length > before && filled[filled.length - 1].action === "upload") {
      await page.waitForTimeout(norm(current.label).includes("resume") ? 6000 : 1500);
    }
  }
  fields = await scrapeFields(page);
  let nonFileFields = fields.filter((field) => field.type !== "file");
  await processFields(nonFileFields.filter((field) => field.kind === "buttongroup" && field.required));
  await processFields(nonFileFields);
  // A choice or resume upload can reveal conditional questions without a page
  // transition. Re-scan until the visible form structure stabilizes, while
  // avoiding duplicate interaction with controls already completed above.
  for (let pass = 0; pass < 3; pass++) {
    await page.waitForTimeout(300);
    const dynamicFields = await scrapeFields(page);
    const signature = fieldSignature(dynamicFields);
    if (seenSignatures.has(signature)) break;
    seenSignatures.add(signature);
    const dynamicFiles = dynamicFields.filter((field) => field.type === "file")
      .sort((left, right) => Number(norm(left.label).includes("cover letter")) - Number(norm(right.label).includes("cover letter")));
    for (const field of dynamicFiles) {
      const before = filled.length;
      await processFields([field]);
      if (filled.length > before && filled[filled.length - 1].action === "upload") {
        await page.waitForTimeout(norm(field.label).includes("resume") ? 6000 : 1500);
      }
    }
    nonFileFields = (await scrapeFields(page)).filter((field) => field.type !== "file");
    await processFields(nonFileFields.filter((field) => field.kind === "buttongroup" && field.required));
    await processFields(nonFileFields);
  }
  return { filled, review };
}

async function ensureApplicationFieldsReady(page, attempts = 8, delayMs = 1000) {
  const totalAttempts = Math.max(1, attempts);
  for (let attempt = 0; attempt < totalAttempts; attempt += 1) {
    const fields = await scrapeFields(page);
    if (meaningfulApplicationFields(fields).length) return fields;
    if (attempt === totalAttempts - 1) return fields;
    await waitForApplicationFormContext(page, 1, delayMs);
    await page.waitForTimeout(delayMs).catch(() => {});
  }
  return await scrapeFields(page);
}

async function findNextButton(page) {
  const btns = await page.evaluate(() => {
    const isVisibleElement = (node) => {
      if (!node) return false;
      if (node.offsetParent) return true;
      const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
      if (!rects || rects.length === 0) return false;
      const style = typeof window !== "undefined" && window.getComputedStyle ? window.getComputedStyle(node) : null;
      return !(style && (style.display === "none" || style.visibility === "hidden"));
    };
    return Array.from(document.querySelectorAll("button, input[type='button'], a"))
      .filter(isVisibleElement) // visible only (multi-page: hidden pages skipped)
      .map((b) => ({
        text: (b.textContent || b.value || "").trim(),
        id: b.id,
        className: typeof b.className === "string" ? b.className : "",
        title: typeof b.getAttribute === "function" ? (b.getAttribute("title") || "") : "",
        ariaLabel: typeof b.getAttribute === "function" ? (b.getAttribute("aria-label") || "") : "",
        href: b.href || "",
        tag: b.tagName.toLowerCase(),
        name: b.name || "",
        inDatepicker: typeof b.closest === "function" ? Boolean(b.closest([
          ".ui-datepicker",
          ".datepicker",
          ".flatpickr-calendar",
          ".react-datepicker",
          "[class*='datepicker' i]",
          "[id*='datepicker' i]",
          "[class*='calendar' i]",
          "[id*='calendar' i]"
        ].join(","))) : false,
      }))
      .filter((b) => b.text);
  });
  for (const b of btns) {
    if (NEXT_PATTERNS.test(b.text) && !SUBMIT_PATTERNS.test(b.text) && !isCalendarNavigationButton(b)) {
      return b;
    }
  }
  return null;
}

async function findSubmitButton(page) {
  const btns = await page.evaluate(() => {
    const isVisibleElement = (node) => {
      if (!node) return false;
      if (node.offsetParent) return true;
      const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
      if (!rects || rects.length === 0) return false;
      const style = typeof window !== "undefined" && window.getComputedStyle ? window.getComputedStyle(node) : null;
      return !(style && (style.display === "none" || style.visibility === "hidden"));
    };
    let index = 0;
    return Array.from(document.querySelectorAll("button, input[type='submit'], a"))
      .filter(isVisibleElement) // visible only
      .map((b) => {
        const rect = typeof b.getBoundingClientRect === "function" ? b.getBoundingClientRect() : { top: 0 };
        const autofillId = String(index++);
        if (typeof b.setAttribute === "function") b.setAttribute("data-job-agent-button-index", autofillId);
        return {
          text: (b.textContent || b.value || "").trim(),
          id: b.id,
          tag: b.tagName.toLowerCase(),
          type: (typeof b.getAttribute === "function" ? (b.getAttribute("type") || "") : "").toLowerCase(),
          href: b.href || "",
          inForm: typeof b.closest === "function" ? Boolean(b.closest("form")) : false,
          y: rect.top + (typeof window !== "undefined" ? (window.scrollY || 0) : 0),
          autofillId,
        };
      })
      .filter((b) => b.text);
  });
  const candidates = btns.filter((b) => SUBMIT_PATTERNS.test(b.text) && !(
    ["apply", "apply now", "apply manually", "autofill with resume"].includes(norm(b.text)) &&
    !b.inForm &&
    b.type !== "submit"
  ));
  if (!candidates.length) return null;
  const score = (b) => {
    let value = 0;
    if (b.tag !== "a") value += 100;
    if (b.type === "submit") value += 100;
    if (b.inForm) value += 80;
    if (/submit\s+application|complete\s+application|send\s+application/i.test(b.text)) value += 50;
    return [value, Number(b.y || 0)];
  };
  candidates.sort((left, right) => {
    const l = score(left);
    const r = score(right);
    return (r[0] - l[0]) || (r[1] - l[1]);
  });
  return candidates[0];
}

async function clickButton(page, b) {
  const clickWithFallback = async (locator) => {
    try {
      await locator.click();
      return;
    } catch (initialError) {
      if (b.text) {
        const overlay = page.locator(
          attrSelector("data-automation-id", "click_filter") + attrSelector("aria-label", b.text)
        ).first();
        try {
          if (typeof overlay.count === "function" && await overlay.count() &&
              (typeof overlay.isVisible !== "function" || await overlay.isVisible())) {
            await overlay.click({ force: true });
            return;
          }
        } catch (_) {}
      }
      try {
        await locator.click({ force: true });
        return;
      } catch (_) {
        throw initialError;
      }
    }
  };
  if (b.autofillId) { await clickWithFallback(page.locator(attrSelector("data-job-agent-button-index", b.autofillId)).first()); return; }
  if (b.id) { await clickWithFallback(page.locator(attrSelector("id", b.id)).first()); return; }
  if (b.href && !isNoopHref(b.href)) {
    const clicked = await page.evaluate((href) => {
      const link = Array.from(document.querySelectorAll("a")).find((node) => node.href === href);
      if (!link) return false;
      link.click();
      return true;
    }, b.href);
    if (clicked) return;
  }
  await clickWithFallback(page.getByText(b.text, { exact: false }).first());
}

function isCalendarNavigationButton(b) {
  if (b.inDatepicker) return true;
  const text = [b.id, b.className, b.title, b.ariaLabel, b.href].join(" ").toLowerCase();
  return ["datepicker", "date-picker", "calendar", "ui-datepicker"].some((token) => text.includes(token));
}

function isNoopHref(href) {
  const value = String(href || "").trim();
  return !value || value === "#" || value.endsWith("#");
}

async function embeddedApplicationFrameUrl(page) {
  try {
    const frameUrl = await page.evaluate(() => {
      const frames = Array.from(document.querySelectorAll('iframe[src]'))
        .map((frame) => String(frame.src || '').trim())
        .filter(Boolean);
      return frames.find((src) => /boards\.greenhouse\.io\/embed\/job_app\?/i.test(src)) || null;
    });
    return String(frameUrl || "").trim() || null;
  } catch (error) {
    return null;
  }
}

async function openEmbeddedApplicationIframeIfNeeded(page) {
  const frameUrl = await embeddedApplicationFrameUrl(page);
  if (!frameUrl) return false;
  const currentUrl = page.url && typeof page.url === "function" ? String(page.url() || "").trim() : "";
  if (currentUrl === frameUrl) return false;
  try {
    await page.goto(frameUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  } catch (error) {
    try {
      await page.goto(frameUrl, { timeout: 30000 });
    } catch (retryError) {
      return false;
    }
  }
  await page.waitForLoadState("domcontentloaded", { timeout: 10000 }).catch(() => {});
  await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(1500).catch(() => {});
  return true;
}

async function openApplicationFormIfNeeded(page) {
  let opened = false;
  for (let i = 0; i < 3; i++) {
    if (await openEmbeddedApplicationIframeIfNeeded(page)) opened = true;
    const fields = await scrapeFields(page);
    const entry = await findApplicationEntry(page);
    if (fields.length && !(await isWorkdayApplyGate(page, fields))) {
      if (!(await isJobPageApplyButton(page, entry, fields))) return opened;
    }
    if (!fields.length && await openWorkdayEmailSignInIfNeeded(page)) {
      opened = true;
      continue;
    }
    if (!entry) return opened;
    await clickButton(page, entry);
    opened = true;
    await page.waitForLoadState("domcontentloaded", { timeout: 10000 }).catch(() => {});
    await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(2500);
  }
  return opened;
}

async function findWorkdayEmailSignInEntry(page) {
  if (!page.url || typeof page.url !== "function") return null;
  const url = String(page.url()).toLowerCase();
  if (!url.includes("myworkdayjobs.com")) return null;
  const entries = await page.evaluate(() => {
    const visible = (node) => {
      if (!node) return false;
      if (node.offsetParent) return true;
      const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
      return rects && rects.length > 0;
    };
    let index = 0;
    return Array.from(document.querySelectorAll("a,button,[role='button']"))
      .filter(visible)
      .map((node) => {
        const autofillId = String(index++);
        node.setAttribute("data-job-agent-button-index", autofillId);
        return {
          text: (node.textContent || node.value || "").trim(),
          id: node.id || "",
          tag: node.tagName.toLowerCase(),
          href: node.href || "",
          automationId: node.getAttribute("data-automation-id") || "",
          autofillId,
        };
      })
      .filter((node) => node.text);
  }).catch(() => []);
  for (const entry of Array.isArray(entries) ? entries : []) {
    const text = norm(entry.text || "");
    if (text === "sign in with email" || text === "sign in with e mail") return entry;
  }
  return null;
}

async function openWorkdayEmailSignInIfNeeded(page) {
  const entry = await findWorkdayEmailSignInEntry(page);
  if (!entry) return false;
  await clickButton(page, entry);
  await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(1500);
  return true;
}

async function findWorkdayCreateAccountEntry(page) {
  if (!page.url || typeof page.url !== "function") return null;
  const url = String(page.url()).toLowerCase();
  if (!url.includes("myworkdayjobs.com")) return null;
  const entries = await page.evaluate(() => {
    const visible = (node) => {
      if (!node) return false;
      if (node.offsetParent) return true;
      const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
      return rects && rects.length > 0;
    };
    let index = 0;
    return Array.from(document.querySelectorAll("a,button,[role='button']"))
      .filter(visible)
      .map((node) => {
        const autofillId = String(index++);
        node.setAttribute("data-job-agent-button-index", autofillId);
        return {
          text: (node.textContent || node.value || "").trim(),
          id: node.id || "",
          tag: node.tagName.toLowerCase(),
          href: node.href || "",
          automationId: node.getAttribute("data-automation-id") || "",
          autofillId,
        };
      })
      .filter((node) => node.text);
  }).catch(() => []);
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (norm(entry.text || "") === "create account") return entry;
  }
  return null;
}

async function openWorkdayCreateAccountFromSignInIfAvailable(page, options = {}) {
  const requireFailure = options.requireFailure !== false;
  if (requireFailure && !(await workdaySignInFailureReason(page, { allowGeneric: false }))) return false;
  const entry = await findWorkdayCreateAccountEntry(page);
  if (entry) {
    try {
      await clickButton(page, entry);
    } catch (_) {
      const locator = page.getByText("Create Account", { exact: true }).first();
      if (typeof locator.count === "function" && !(await locator.count())) return false;
      if (typeof locator.isVisible === "function" && !(await locator.isVisible())) return false;
      await locator.click();
    }
  } else {
    const locator = page.getByText("Create Account", { exact: true }).first();
    if (typeof locator.count === "function" && !(await locator.count())) return false;
    if (typeof locator.isVisible === "function" && !(await locator.isVisible())) return false;
    await locator.click();
  }
  await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(1500);
  return true;
}

function meaningfulApplicationFields(fields) {
  return (fields || []).filter((field) => {
    const label = [
      field.label, field.id, field.name, field.section, field.ariaLabel, field.ariaDescription, field.placeholder,
    ].filter(Boolean).join(" ");
    const normalizedLabel = norm(label);
    const normalizedFieldLabel = norm(field.label || "");
    const normalizedId = norm(field.id || "");
    const normalizedAutomation = norm(field.automationId || "");
    if (isHoneypotField(label)) return false;
    if (normalizedFieldLabel === "settings" || normalizedId === "settingsselectorbutton" || normalizedAutomation === "utilitymenubutton") return false;
    if (normalizedLabel === "settings" || normalizedLabel === "search" || normalizedLabel.startsWith("search for jobs")) return false;
    if (
      field.type === "file" &&
      (
        normalizedLabel.includes("how well you match") ||
        normalizedLabel.includes("match with this job") ||
        ["upload your resume", "upload resume"].includes(normalizedFieldLabel)
      )
    ) return false;
    return true;
  });
}

async function isWorkdayApplyGate(page, fields) {
  if (!page.url || typeof page.url !== "function") return false;
  const url = String(page.url()).toLowerCase();
  if (!url.includes("myworkdayjobs.com")) return false;
  const entry = await findApplicationEntry(page);
  if (!entry) return false;
  const currentFields = Array.isArray(fields) ? fields : await scrapeFields(page);
  const meaningfulFields = meaningfulApplicationFields(currentFields);
  if (!meaningfulFields.length) return false;
  if (meaningfulFields.some((field) => field.type === "file")) return false;
  const loginMarkers = ["email", "username", "password"];
  return meaningfulFields.every((field) => {
    const label = norm(field.label || "");
    return loginMarkers.some((marker) => label.includes(marker));
  }) && norm(entry.text).includes("apply manually");
}

async function hasApplicationFormContext(page) {
  // Real Playwright Page instances expose url(). Keep generated-runtime unit
  // test doubles on the pre-existing field-processing path.
  if (!page.url || typeof page.url !== "function") return true;
  const fields = await scrapeFields(page);
  if (!fields.length) return false;
  const meaningfulFields = meaningfulApplicationFields(fields);
  if (!meaningfulFields.length) return false;
  if (await isWorkdayApplyGate(page, fields)) return false;
  const entry = await findApplicationEntry(page);
  if (await isJobPageApplyButton(page, entry, fields)) return false;
  const url = String(page.url()).toLowerCase();
  if (["greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com"].some((host) => url.includes(host))) return true;
  const labels = meaningfulFields.map((field) => norm(field.label || "")).join(" ");
  if (meaningfulFields.some((field) => field.type === "file") || labels.includes("password")) return true;
  const identityMarkers = ["first name", "last name", "phone", "resume", "curriculum vitae", "cover letter"];
  return identityMarkers.filter((marker) => labels.includes(marker)).length >= 2;
}

function applicationHostAliases(applicationUrl) {
  let host = "";
  let parsed = null;
  try {
    parsed = new URL(String(applicationUrl || ""));
    host = parsed.hostname.toLowerCase();
  } catch (_) {}
  const aliases = new Set();
  if (host) aliases.add(host);
  if (host.includes("greenhouse.io") || (parsed && /\bgh_jid=\d+/i.test(String(parsed.search || "")))) {
    aliases.add("boards.greenhouse.io");
    aliases.add("job-boards.greenhouse.io");
  }
  return Array.from(aliases).filter(Boolean);
}

function isApplicationContextUrl(currentUrl, applicationUrl) {
  let currentHost = "";
  try { currentHost = new URL(String(currentUrl || "")).hostname.toLowerCase(); } catch (_) {}
  if (!currentHost) return true;
  const aliases = applicationHostAliases(applicationUrl);
  if (aliases.includes(currentHost)) return true;
  if (currentHost.endsWith(".myworkdayjobs.com") && aliases.some((host) => host.endsWith(".myworkdayjobs.com"))) return true;
  return false;
}

async function installApplicationNavigationGuard(page, applicationUrl) {
  if (!applicationUrl || !page.evaluate) return false;
  const hosts = applicationHostAliases(applicationUrl);
  if (!hosts.length) return false;
  return Boolean(await page.evaluate((payload) => {
    if (window.__jobAgentApplicationNavigationGuardInstalled) return true;
    window.__jobAgentApplicationNavigationGuardInstalled = true;
    const applicationHosts = new Set((payload.hosts || []).filter(Boolean));
    const infoPattern = /privacy|notice|policy|terms|arbitration|personnel|candidate|pdf/i;
    document.addEventListener("click", (event) => {
      const anchor = event.target && event.target.closest ? event.target.closest("a[href]") : null;
      if (!anchor) return;
      let url = null;
      try { url = new URL(anchor.href, window.location.href); } catch (_) { return; }
      if (!url || applicationHosts.has(url.hostname.toLowerCase())) return;
      const text = [
        anchor.textContent || "",
        anchor.getAttribute("aria-label") || "",
        anchor.getAttribute("title") || "",
        anchor.href || "",
      ].join(" ");
      if (!infoPattern.test(text)) return;
      event.preventDefault();
      event.stopPropagation();
      if (typeof event.stopImmediatePropagation === "function") event.stopImmediatePropagation();
    }, true);
    return true;
  }, { hosts }).catch(() => false));
}

async function restoreApplicationContextIfExternal(page, applicationUrl) {
  if (!applicationUrl || !page.url || typeof page.url !== "function") return false;
  const currentUrl = String(page.url() || "");
  if (isApplicationContextUrl(currentUrl, applicationUrl)) return false;
  try {
    await page.goBack({ waitUntil: "domcontentloaded", timeout: 15000 });
    await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
    await openApplicationFormIfNeeded(page);
    if (await waitForApplicationFormContext(page, 2, 500)) return true;
  } catch (_) {}
  try {
    await page.goto(applicationUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
    await openApplicationFormIfNeeded(page);
    return await waitForApplicationFormContext(page, 3, 750);
  } catch (_) {
    return false;
  }
}

async function restoreWorkdayApplicationFromCandidateHome(page, applicationUrl) {
  if (!applicationUrl || !page.url || typeof page.url !== "function") return false;
  const currentUrl = String(page.url() || "");
  const currentUrlLower = currentUrl.toLowerCase();
  if (!currentUrlLower.includes("myworkdayjobs.com") || !currentUrlLower.includes("/userhome")) return false;
  const text = norm(await currentPageText(page));
  if (!text.includes("candidate home") || !text.includes("you have no applications")) return false;
  try {
    await page.goto(applicationUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(1500);
    await openApplicationFormIfNeeded(page);
    return true;
  } catch (_) {
    return false;
  }
}

async function waitForApplicationFormContext(page, attempts = 5, delayMs = 1000) {
  const totalAttempts = Math.max(1, attempts);
  for (let attempt = 0; attempt < totalAttempts; attempt += 1) {
    if (await hasApplicationFormContext(page)) return true;
    if (attempt === totalAttempts - 1) break;
    await openApplicationFormIfNeeded(page);
    await page.waitForTimeout(delayMs).catch(() => {});
  }
  return await hasApplicationFormContext(page);
}

async function recoverApplicationFormFromJobPage(page, applicationUrl) {
  let parsed = null;
  try {
    parsed = new URL(String(applicationUrl || ""));
  } catch (_) {
    return false;
  }
  const pathName = parsed.pathname.replace(/\/+$/, "");
  if (parsed.hostname !== "jobs.ashbyhq.com" || !pathName.endsWith("/application")) return false;
  parsed.pathname = pathName.slice(0, -"/application".length);
  parsed.search = "";
  parsed.hash = "";
  try {
    await page.goto(parsed.toString(), { waitUntil: "domcontentloaded", timeout: 30000 });
    if (typeof page.waitForLoadState === "function") {
      await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
    }
    try {
      await page.waitForTimeout(1000);
    } catch (_) {}
    await openApplicationFormIfNeeded(page);
    return await waitForApplicationFormContext(page, 4, 750);
  } catch (_) {
    return false;
  }
}

async function isJobPageApplyButton(page, button, fields = null) {
  const currentFields = Array.isArray(fields) ? fields : await scrapeFields(page);
  const meaningfulFields = meaningfulApplicationFields(currentFields);
  const nonFormApplyEntry = Boolean(button && !button.inForm);
  return Boolean(
    button &&
    ["apply", "apply now", "apply manually", "autofill with resume"].includes(norm(button.text || "")) &&
    (!meaningfulFields.length || (nonFormApplyEntry && onlyResumeMatchProbeFields(meaningfulFields)))
  );
}

function onlyResumeMatchProbeFields(fields) {
  if (!fields || !fields.length) return false;
  return fields.every((field) => {
    if (field.type !== "file") return false;
    const label = [
      field.label, field.id, field.name, field.section, field.ariaLabel, field.ariaDescription, field.placeholder,
    ].filter(Boolean).join(" ");
    const normalizedLabel = norm(label);
    return !normalizedLabel ||
      normalizedLabel.includes("how well you match") ||
      normalizedLabel.includes("match with this job") ||
      normalizedLabel.includes("upload resume") ||
      normalizedLabel.includes("upload your resume");
  });
}

async function findApplicationEntry(page) {
  const entries = await page.evaluate(() => {
    const isVisibleElement = (node) => {
      if (!node) return false;
      if (node.offsetParent) return true;
      const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
      return rects && rects.length > 0;
    };
    return Array.from(document.querySelectorAll("a, button"))
      .filter(isVisibleElement)
      .map((node) => ({
        text: (node.textContent || node.value || "").trim(),
        id: node.id || "",
        tag: node.tagName.toLowerCase(),
        href: node.href || "",
        inForm: Boolean(node.closest("form")),
      }))
      .filter((node) => node.text);
  }).catch(() => []);
  const visibleEntries = Array.isArray(entries) ? entries : [];
  for (const entry of visibleEntries) {
    const text = norm(entry.text);
    const href = String(entry.href || "").toLowerCase();
    if (text === "apply manually" || href.includes("/apply/applymanually")) {
      return entry;
    }
  }
  for (const entry of visibleEntries) {
    const text = norm(entry.text);
    const href = String(entry.href || "").toLowerCase();
    if (
      ["application", "apply for this job", "apply now", "autofill with resume", "i m interested", "i am interested", "interested"].includes(text) ||
      (["apply", "apply now"].includes(text) && (href.includes("/application") || href.includes("/apply")))
    ) {
      return entry;
    }
  }
  return null;
}

// --- ATS provider detection (Simplify adapts per provider) ---
function detectATS(url) {
  const raw = String(url || "");
  const u = raw.toLowerCase();
  for (const adapter of (CFG.atsAdapters || [])) {
    if (!adapter) continue;
    const substrings = Array.isArray(adapter.url_substrings) ? adapter.url_substrings : [];
    if (substrings.some((part) => part && u.includes(String(part).toLowerCase()))) return adapter.name;
    const regexes = Array.isArray(adapter.url_regexes) ? adapter.url_regexes : [];
    if (regexes.some((pattern) => {
      try { return !!new RegExp(pattern, "i").test(raw); } catch (_) { return false; }
    })) return adapter.name;
  }
  if (u.includes("ashbyhq") || u.includes("ashby")) return "ashby";
  if (u.includes("smartrecruiters")) return "smartrecruiters";
  if (u.includes("workable")) return "workable";
  if (u.includes("recruitee")) return "recruitee";
  if (u.includes("comeet")) return "comeet";
  return "generic";
}

// --- Work-history / education field mappers (for repeatable sections) ---
function mapWorkField(label) {
  const n = norm(label);
  if (!n) return null;
  if (n.includes("job title") || n.includes("position title") || n.includes("role title")) return "title";
  if (n.includes("company") || n.includes("employer") || n.includes("organization")) return "company";
  if (n.includes("start date") || (n.includes("from") && n.includes("date")) || n === "from") return "start_date";
  if (n.includes("end date") || (n.includes("to") && n.includes("date")) || n.includes("graduation") === false && n === "to") return "end_date";
  if (n.includes("description") || n.includes("responsibilities") || n.includes("what did you do")) return "description";
  if (n.includes("location") || n.includes("city")) return "location";
  if (n.includes("current") || n.includes("present")) return "current";
  return null;
}

function mapEduField(label) {
  const n = norm(label);
  if (!n) return null;
  if (n.includes("school") || n.includes("university") || n.includes("institution") || n.includes("college")) return "school";
  if (n.includes("degree")) return "degree";
  if (n.includes("field of study") || n.includes("major") || n.includes("field")) return "field";
  if (n.includes("start date") || n === "from") return "start_date";
  if (n.includes("end date") || n.includes("graduation") || n === "to") return "end_date";
  if (n.includes("gpa")) return "gpa";
  return null;
}

// Fill the LAST visible input whose label matches any pattern (newest block).
// Returns {label, readback} on success so multi-entry fills can be verified.
async function fillLastByLabel(page, patterns, value) {
  if (!value) return null;
  const fields = await scrapeFields(page);
  const dateOnlyValue = /^\d{4}[-/]\d{1,2}$/.test(String(value));
  let last = null;
  for (const f of fields) {
    if (f.kind === "radiogroup" || f.type === "file" || f.type === "radio" || f.type === "checkbox") continue;
    const n = norm(f.label);
    if (dateOnlyValue && /\b(month|year)\b/.test(n)) continue;
    if (patterns.some((p) => n.includes(norm(p)))) last = f;
  }
  if (!last) return null;
  const sel = selectorFor(last);
  if (!sel) return null;
  try {
    if (last.role === "combobox") await applyFill(page, last, { action: "combobox", value: String(value) });
    else if (last.tag === "select") await page.locator(sel).first().selectOption({ label: String(value) });
    else await page.locator(sel).first().fill(String(value));
    let readback = null;
    try { readback = await page.locator(sel).first().inputValue(); } catch (e) {}
    return { label: last.label, readback };
  } catch (e) { return null; }
}

// Click an "add another" / "+ add" button for a repeatable section.
async function clickAddAnother(page, keyword) {
  const btn = await page.evaluate((kw) => {
    const pats = ["add another " + kw, "add " + kw, "add another", "+ add " + kw, "add more " + kw];
    const visible = Array.from(document.querySelectorAll("button, a, input[type='button']"))
      .filter((b) => b.offsetParent);
    for (const b of visible) {
      const t = (b.textContent || b.value || "").trim().toLowerCase();
      if (pats.some((p) => t.includes(p))) return { id: b.id, text: t };
    }
    return null;
  }, keyword);
  if (!btn) return false;
  try {
    if (btn.id) await page.locator(attrSelector("id", btn.id)).first().click();
    else await page.getByText(btn.text, { exact: false }).first().click();
    await page.waitForTimeout(400);
    return true;
  } catch (e) { return false; }
}

async function hasSectionHeading(page, regex) {
  return page.evaluate((re) => {
    const nodes = Array.from(document.querySelectorAll("h1,h2,h3,h4,legend"))
      .filter((n) => n.offsetParent); // visible only (skip hidden pages)
    return nodes.some((n) => new RegExp(re, "i").test(n.textContent || ""));
  }, regex);
}

// Fill a repeatable section (work history / education) with multiple entries,
// clicking "add another" between entries (Simplify fills multi-entry sections).
async function fillRepeatableSection(page, entries, fieldMapper, addKeyword, labelPatterns) {
  const out = [];
  if (!entries || !entries.length) return out;
  for (let i = 0; i < entries.length; i++) {
    const entry = { ...entries[i] };
    for (const boundary of ["start", "end"]) {
      entry[`${boundary}_month`] = entryDatePart(entry, boundary, "month") || entry[`${boundary}_month`];
      entry[`${boundary}_year`] = entryDatePart(entry, boundary, "year") || entry[`${boundary}_year`];
    }
    if (i > 0) {
      const ok = await clickAddAnother(page, addKeyword);
      if (!ok) { out.push({ entry: i, status: "could not add another block" }); break; }
    }
    for (const [fieldKey, pats] of Object.entries(labelPatterns)) {
      const val = entry[fieldKey];
      if (val !== undefined && val !== "" && val !== false) {
        const res = await fillLastByLabel(page, pats, String(val));
        if (res) out.push({ entry: i, field: fieldKey, label: res.label, readback: res.readback });
      }
    }
  }
  return out;
}

function readbackStatus(readback) {
  if (readback === true) return "checked";
  if (readback === false) return "unchecked";
  if (readback === "file-selected") return "file selected";
  if (readback === "selected") return "selected";
  if (readback === "readback-error") return "readback-error";
  if (readback === null || readback === undefined) return "unknown";
  if (String(readback).startsWith("selected: ")) return String(readback);
  if (String(readback).length > 0) return "filled";
  return "empty";
}

function displayText(value, limit = 180) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  return text.slice(0, limit - 3).trimEnd() + "...";
}

async function currentPageText(page) {
  const state = await page.evaluate(() => ({
    text: (document.body && document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60000),
  })).catch(() => null);
  return state && state.text ? String(state.text) : "";
}

async function currentApplicationStep(page) {
  const value = await page.evaluate(() => {
    const visible = (node) => !!(node && (node.offsetParent || (node.getClientRects && node.getClientRects().length)));
    const known = new Set([
      "My Information", "My Experience", "Application Questions",
      "Voluntary Disclosures", "Self Identify", "Review",
    ]);
    return Array.from(document.querySelectorAll("h1,h2,h3,legend"))
      .filter(visible)
      .map((node) => (node.textContent || "").replace(/\s+/g, " ").trim())
      .find((text) => known.has(text)) || "";
  }).catch(() => "");
  return String(value || "");
}

function formFieldSignature(fields) {
  return (fields || []).map((field) => [
    field.kind || "", field.tag || "", field.type || "", field.id || "", field.name || "", field.label || "", field.required ? "1" : "0",
  ].join("\u0000")).sort().join("\n");
}

function workdaySignInFieldSet(fields) {
  const meaningfulFields = (fields || []).filter((field) => !isHoneypotField([
    field.label, field.id, field.name, field.section, field.ariaLabel, field.ariaDescription, field.placeholder,
  ].filter(Boolean).join(" ")));
  if (meaningfulFields.length > 3) return false;
  if (meaningfulFields.some((field) => field.type === "file")) return false;
  const labels = meaningfulFields.map((field) => norm(field.label || ""));
  if (labels.some((label) =>
    label.includes("verify") ||
    label.includes("confirm") ||
    label.includes("new password") ||
    label.includes("create account")
  )) return false;
  const hasEmail = labels.some((label) => label.includes("email") || label.includes("username"));
  const hasPassword = meaningfulFields.some((field) => String(field.type || "").toLowerCase() === "password");
  return hasEmail && hasPassword;
}

function workdaySignInFillSignature(page, filled) {
  if (!page.url || typeof page.url !== "function") return "";
  const url = String(page.url()).toLowerCase();
  if (!url.includes("myworkdayjobs.com")) return "";
  const labels = (filled || [])
    .filter((item) => item && item.action !== "upload")
    .map((item) => norm(item.label || ""))
    .filter(Boolean);
  if (labels.length < 2) return "";
  if (labels.some((label) =>
    label.includes("verify") ||
    label.includes("confirm") ||
    label.includes("new password") ||
    label.includes("create account")
  )) return "";
  const hasEmail = labels.some((label) => label.includes("email") || label.includes("username"));
  const hasPassword = labels.some((label) => label.includes("password"));
  const onlySignInFields = labels.every((label) =>
    label.includes("email") || label.includes("username") || label.includes("password")
  );
  if (!hasEmail || !hasPassword || !onlySignInFields) return "";
  return Array.from(new Set(labels.map((label) =>
    label.includes("email") || label.includes("username") ? "email" : "password"
  ))).sort().join("|");
}

async function workdayAccountVerificationReason(page) {
  if (!page.url || typeof page.url !== "function") return null;
  const url = String(page.url()).toLowerCase();
  if (!url.includes("myworkdayjobs.com")) return null;
  const text = norm(await currentPageText(page));
  if (!text) return null;
  if (
    text.includes("verify your account before you sign in") ||
    text.includes("account may need verification") ||
    text.includes("resend account verification")
  ) {
    return "candidate account verification required by Workday";
  }
  return null;
}

async function workdaySignInFailureReason(page, options = {}) {
  const allowGeneric = options.allowGeneric !== false;
  if (!page.url || typeof page.url !== "function") return null;
  const url = String(page.url()).toLowerCase();
  if (!url.includes("myworkdayjobs.com")) return null;
  if (await workdayAccountVerificationReason(page)) return null;
  const fields = await scrapeFields(page);
  if (!workdaySignInFieldSet(fields)) return null;
  const text = norm(await currentPageText(page));
  if (!text || !text.includes("sign in")) return null;
  const explicitPatterns = new Map([
    ["wrong email address or password", "candidate account sign-in rejected by Workday: wrong email address or password"],
    ["your account might be locked", "candidate account sign-in rejected by Workday: account might be locked"],
    ["account might be locked", "candidate account sign-in rejected by Workday: account might be locked"],
    ["invalid username or password", "candidate account sign-in rejected by Workday: invalid username or password"],
    ["incorrect email or password", "candidate account sign-in rejected by Workday: incorrect email or password"],
  ]);
  for (const [pattern, reason] of explicitPatterns.entries()) {
    if (text.includes(pattern)) return reason;
  }
  if (text.includes("please enter a valid email") || text.includes("please enter your password")) {
    return "candidate account sign-in rejected by Workday: sign-in form remained invalid after automation";
  }
  if (!allowGeneric) return null;
  return "candidate account sign-in rejected by Workday";
}

async function workdayCreateAccountFailureReason(page) {
  if (!page.url || typeof page.url !== "function") return null;
  const url = String(page.url()).toLowerCase();
  if (!url.includes("myworkdayjobs.com")) return null;
  const fields = await scrapeFields(page);
  const passwordCount = (fields || []).filter((field) => String(field.type || "").toLowerCase() === "password").length;
  const labels = (fields || []).map((field) => norm(field.label || ""));
  const hasEmail = labels.some((label) => label.includes("email") || label.includes("username"));
  if (!hasEmail || passwordCount < 2) return null;
  const text = norm(await currentPageText(page));
  if (!text || !text.includes("create account")) return null;
  if (text.includes("please check the box to continue")) {
    return "candidate account creation blocked by required privacy consent checkbox";
  }
  if (text.includes("passwords do not match")) {
    return "candidate account creation rejected by Workday: passwords do not match";
  }
  if (text.includes("password requirements") && text.includes("error")) {
    return "candidate account creation rejected by Workday: password requirements not satisfied";
  }
  return "candidate account creation blocked by Workday";
}

function pageDidNotAdvance(stepBefore, stepAfter, fieldsBeforeNext, fieldsAfterNext) {
  if (fieldsAfterNext !== fieldsBeforeNext) return false;
  if (stepBefore && stepAfter && stepAfter !== stepBefore) return false;
  return Boolean(fieldsAfterNext || stepBefore || stepAfter);
}

async function detectSubmissionConfirmation(page) {
  const state = await page.evaluate(() => ({
    url: window.location.href,
    title: document.title || "",
    text: (document.body && document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60000),
  })).catch(() => null);
  if (!state) return null;
  const rawText = [state.url, state.title, state.text].join(" ").toLowerCase();
  const localizedPatterns = ["\u7533\u8bf7\u5df2\u63d0\u4ea4", "\u60a8\u7684\u7533\u8bf7\u5df2\u6210\u529f\u63d0\u4ea4", "\u63d0\u4ea4\u6210\u529f"];
  if (localizedPatterns.some((pattern) => rawText.includes(pattern))) {
    return `matched localized submission confirmation at ${state.url || "current page"}`;
  }
  const text = norm(rawText);
  const patterns = [
    "thank you for applying",
    "thanks for applying",
    "thanks so much for applying",
    "application success",
    "application submitted",
    "application has been submitted",
    "successfully submitted",
    "application received",
    "we have received your application",
    "received your application",
    "your application has been received",
    "we ll be in touch",
    "we will be in touch",
  ];
  const matched = patterns.find((pattern) => text.includes(pattern));
  return matched ? `matched '${matched}' at ${state.url || "current page"}` : null;
}

async function detectEmailVerificationRequest(page) {
  const state = await page.evaluate(() => ({
    url: window.location.href,
    title: document.title || "",
    text: (document.body && document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60000),
  })).catch(() => null);
  if (!state) return null;
  const text = norm([state.url, state.title, state.text].join(" "));
  const patterns = [
    "security code",
    "verification code",
    "enter the code",
    "copy and paste this code",
    "email verification",
    "verify your email",
    "one time code",
    "one time password",
  ];
  const matched = patterns.find((pattern) => text.includes(pattern));
  return matched ? `matched '${matched}' at ${state.url || "current page"}` : null;
}

async function detectSubmissionProcessingError(page) {
  const state = await page.evaluate(() => ({
    url: window.location.href,
    title: document.title || "",
    text: (document.body && document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60000),
    recaptcha: Boolean(document.querySelector([
      'iframe[src*="recaptcha"]',
      'iframe[src*="captcha"]',
      'iframe[src*="hcaptcha"]',
      'iframe[src*="challenges.cloudflare.com"]',
      'iframe[src*="arkoselabs"]',
      'iframe[src*="funcaptcha"]',
      'iframe[title*="cloudflare" i]',
      'iframe[title*="challenge" i]',
      'iframe[title*="verification" i]',
      'iframe[title*="captcha" i]',
      '.g-recaptcha',
      '.cf-turnstile',
      '[name="cf-turnstile-response"]',
      '[class*="turnstile" i]',
      '[id*="turnstile" i]',
      '[class*="recaptcha" i]',
      '[class*="captcha" i]',
      '[id*="captcha" i]',
    ].join(","))),
  })).catch(() => null);
  if (!state) return null;
  const text = norm([state.url, state.title, state.text].join(" "));
  const patterns = [
    "flagged as possible spam",
    "application was flagged as spam",
    "too many requests",
    "rate limited",
    "rate limit",
    "http 429",
    "status 429",
    "your form needs corrections",
    "missing entry for required field",
    "there was an error processing your application",
    "error processing your application",
    "please try again",
    "please complete the recaptcha",
    "captcha verification failed",
    "captcha token expired",
    "invalid captcha",
    "verify you are human",
    "cf turnstile",
  ];
  const matched = patterns.find((pattern) => text.includes(pattern));
  if (matched) return `matched '${matched}' at ${state.url || "current page"}` + (state.recaptcha ? " with recaptcha present" : "");
  return state.recaptcha ? `captcha present at ${state.url || "current page"}` : null;
}

function isRetryableCaptchaError(error) {
  const text = norm(error || "");
  if (!text) return false;
  if (text.includes("possible spam")) return false;
  if (text.includes("flagged as spam")) return false;
  if (text.includes("too many requests")) return false;
  if (["rate limit", "rate-limit", "rate limited", "rate-limited", "http 429", "status 429"].some((marker) => text.includes(marker))) return false;
  return [
    "captcha present at ",
    "please complete the recaptcha",
    "captcha verification failed",
    "captcha token expired",
    "invalid captcha",
    "verify you are human",
    "cf turnstile",
  ].some((marker) => text.includes(marker));
}

function captchaRecoveryFailure(captchaResult) {
  const status = String(captchaResult && captchaResult.status || "unknown");
  const detail = String(captchaResult && captchaResult.detail || "no solver detail");
  return "captcha recovery failed: " + status + " (" + detail + ")";
}

function isAmbientCaptchaPresence(error) {
  return norm(error || "").startsWith("captcha present at ");
}

async function waitForSubmissionOutcome(page, timeoutMs = 35000) {
  let elapsed = 0;
  const interval = 1000;
  while (elapsed < timeoutMs) {
    await page.waitForTimeout(interval);
    elapsed += interval;
    const confirmation = await detectSubmissionConfirmation(page);
    if (confirmation) return { confirmation, verification: null, processingError: null };
    const verification = await detectEmailVerificationRequest(page);
    if (verification) return { confirmation: null, verification, processingError: null };
    const processingError = await detectSubmissionProcessingError(page);
    if (processingError && !isAmbientCaptchaPresence(processingError)) {
      return { confirmation: null, verification: null, processingError };
    }
  }
  return { confirmation: null, verification: null, processingError: null };
}

async function writeEvidence(page, filename, label, detail) {
  const directory = __dirname || ".";
  const out = path.join(directory, filename);
  const state = (await page.evaluate(() => ({
    url: window.location.href,
    title: document.title || "",
    text: (document.body && document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60000),
  })).catch(() => ({ url: "", title: "", text: "" }))) || { url: "", title: "", text: "" };
  let screenshot = "not captured";
  try {
    const screenshotPath = out.replace(/\.txt$/, ".png");
    await page.screenshot({ path: screenshotPath, fullPage: true });
    screenshot = screenshotPath;
  } catch (e) {
    screenshot = "not captured";
  }
  const text = [
    `${label}: ${detail || ""}`,
    `url: ${state.url || ""}`,
    `title: ${state.title || ""}`,
    `screenshot: ${screenshot}`,
    "",
    "page_text_head:",
    (state.text || "").slice(0, 4000),
    "",
    "page_text_tail:",
    (state.text || "").slice(-4000),
  ].join("\n");
  fs.writeFileSync(out, text);
}

async function writeReviewEvidence(page, reviewItems) {
  const directory = __dirname || ".";
  const out = path.join(directory, "review-required.txt");
  const state = (await page.evaluate(() => ({
    url: window.location.href,
    title: document.title || "",
    text: (document.body && document.body.innerText || "").replace(/\s+/g, " ").trim().slice(0, 60000),
  })).catch(() => ({ url: "", title: "", text: "" }))) || { url: "", title: "", text: "" };
  let screenshot = "not captured";
  try {
    const screenshotPath = path.join(directory, "review-required.png");
    await page.screenshot({ path: screenshotPath, fullPage: true });
    screenshot = screenshotPath;
  } catch (e) {
    screenshot = "not captured";
  }
  const lines = [
    "review_count: " + (reviewItems || []).length,
    "url: " + (state.url || ""),
    "title: " + (state.title || ""),
    "screenshot: " + screenshot,
    "",
    "review_items:",
    ...(reviewItems || []).map((item) => JSON.stringify({
      label: item && item.label || "",
      reason: item && item.reason || "",
      sensitive: !!(item && item.sensitive),
      blocking: item && item.blocking !== false,
    })),
    "",
    "page_text_head:",
    (state.text || "").slice(0, 8000),
    "",
    "page_text_tail:",
    (state.text || "").slice(-8000),
  ];
  fs.writeFileSync(out, lines.join("\n"));
  return out;
}

async function emailVerificationCode(requestedAfterMs = Date.now()) {
  // Shared code files can retain a syntactically valid code from another ATS
  // session. File-backed values must be written after this verification page.
  for (const key of [
    "JOB_AGENT_EMAIL_VERIFICATION_CODE",
    "JOB_AGENT_GREENHOUSE_SECURITY_CODE",
    "JOB_AGENT_SECURITY_CODE",
  ]) {
    const value = String(process.env[key] || "").trim();
    if (value) return value;
  }
  const codeFile = String(process.env.JOB_AGENT_EMAIL_VERIFICATION_CODE_FILE || "").trim();
  if (!codeFile) return null;
  const rawWait = Number(process.env.JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS || "120");
  const waitMs = Math.max(0, Number.isFinite(rawWait) ? rawWait * 1000 : 120000);
  const deadline = Date.now() + waitMs;
  while (true) {
    try {
      const stats = fs.statSync(codeFile);
      const value = String(fs.readFileSync(codeFile, "utf8") || "").trim();
      if (value && stats.mtimeMs >= requestedAfterMs) return value;
    } catch (_) {}
    if (Date.now() >= deadline) return null;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

async function fillEmailVerificationCode(page, code) {
  if (!/^[A-Za-z0-9][A-Za-z0-9 -]{3,20}$/.test(String(code || "").trim())) return false;
  const characters = Array.from(String(code).trim());
  // React-owned OTP inputs need native user-style updates. Direct DOM value
  // assignment can look filled while the final submit button remains disabled.
  try {
    const cells = page.locator('input[id^="security-input-"]');
    if ((await cells.count()) >= characters.length) {
      for (let index = 0; index < characters.length; index++) {
        await cells.nth(index).fill(characters[index]);
      }
      await cells.nth(characters.length - 1).press("Tab").catch(() => {});
      const values = await Promise.all(characters.map((_, index) => cells.nth(index).inputValue()));
      if (values.every((value, index) => value === characters[index])) return true;
    }
  } catch (_) {}
  return Boolean(await page.evaluate((codeValue) => {
    const visible = (node) => {
      if (!node) return false;
      if (node.offsetParent) return true;
      const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
      return rects && rects.length > 0;
    };
    const labelFor = (control) => {
      const parts = [
        control.id || "",
        control.name || "",
        control.getAttribute("aria-label") || "",
        control.getAttribute("placeholder") || "",
        control.getAttribute("autocomplete") || "",
      ];
      if (control.id) {
        document.querySelectorAll("label").forEach((label) => {
          if (label.htmlFor === control.id || label.getAttribute("for") === control.id) {
            parts.push(label.textContent || "");
          }
        });
      }
      const wrapper = control.closest("label,[data-automation-id^='formField-'],.field,.form-field,.application-question");
      if (wrapper) parts.push(wrapper.textContent || "");
      return parts.join(" ").replace(/\s+/g, " ").trim().toLowerCase();
    };
    const candidates = Array.from(document.querySelectorAll("input, textarea"))
      .filter((node) => visible(node))
      .filter((node) => !["hidden", "submit", "button", "file", "checkbox", "radio"].includes((node.getAttribute("type") || "").toLowerCase()))
      .map((node) => {
        const text = labelFor(node);
        let score = 0;
        if (/security\s+code|verification\s+code|confirmation\s+code|one[-\s]?time\s+(code|password)|email\s+code/.test(text)) score += 100;
        if (/\bcode\b/.test(text)) score += 25;
        if ((node.getAttribute("autocomplete") || "").toLowerCase() === "one-time-code") score += 100;
        return { node, score };
      })
      .filter((item) => item.score > 0)
      .sort((left, right) => right.score - left.score);
    const setValue = (target, value) => {
      const proto = target.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor && descriptor.set) descriptor.set.call(target, value);
      else target.value = value;
      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.dispatchEvent(new Event("change", { bubbles: true }));
    };
    const singleCharacterInputs = Array.from(document.querySelectorAll("input"))
      .filter((node) => visible(node))
      .filter((node) => node.maxLength === 1 || /security-input-\d+/i.test(node.id || ""));
    singleCharacterInputs.sort((left, right) => {
      const leftIndex = Number((String(left.id || "").match(/(\d+)$/) || [])[1]);
      const rightIndex = Number((String(right.id || "").match(/(\d+)$/) || [])[1]);
      return leftIndex - rightIndex;
    });
    if (singleCharacterInputs.length >= String(codeValue).length) {
      Array.from(String(codeValue)).forEach((character, index) => setValue(singleCharacterInputs[index], character));
      return Array.from(String(codeValue)).every((character, index) => singleCharacterInputs[index].value === character);
    }
    const target = candidates[0] && candidates[0].node;
    if (!target) return false;
    setValue(target, String(codeValue).trim());
    return true;
  }, String(code).trim()).catch(() => false));
}

function capMonsterConfig() {
  const enabled = /^(1|true|yes|y|on)$/i.test(String(process.env.CAPMONSTER_SOLVE_CAPTCHA || "").trim());
  const apiKey = String(process.env.CAPMONSTER_API_KEY || "").trim();
  const proxyPort = Number(process.env.CAPMONSTER_PROXY_PORT || 0);
  const proxy = process.env.CAPMONSTER_PROXY_TYPE && process.env.CAPMONSTER_PROXY_ADDRESS && proxyPort
    ? {
        proxyType: process.env.CAPMONSTER_PROXY_TYPE,
        proxyAddress: process.env.CAPMONSTER_PROXY_ADDRESS,
        proxyPort,
        ...(process.env.CAPMONSTER_PROXY_LOGIN ? { proxyLogin: process.env.CAPMONSTER_PROXY_LOGIN } : {}),
        ...(process.env.CAPMONSTER_PROXY_PASSWORD ? { proxyPassword: process.env.CAPMONSTER_PROXY_PASSWORD } : {}),
      }
    : null;
  const rawMinScore = Number(process.env.CAPMONSTER_RECAPTCHA_MIN_SCORE || 0.3);
  return {
    enabled: enabled && Boolean(apiKey),
    apiKey,
    pollIntervalMs: Math.max(1000, Number(process.env.CAPMONSTER_POLL_INTERVAL_SECONDS || 3) * 1000),
    timeoutMs: Math.max(15000, Number(process.env.CAPMONSTER_TIMEOUT_SECONDS || 120) * 1000),
    proxy,
    recaptchaMinScore: Math.min(0.9, Math.max(0.1, Number.isFinite(rawMinScore) ? rawMinScore : 0.3)),
    hcaptchaTaskType: ["HCaptchaTask", "HCaptchaTaskProxyless"].includes(String(process.env.CAPMONSTER_HCAPTCHA_TASK_TYPE || "").trim())
      ? String(process.env.CAPMONSTER_HCAPTCHA_TASK_TYPE || "").trim()
      : "HCaptchaTaskProxyless",
  };
}

function capMonsterPost(endpoint, payload) {
  const body = JSON.stringify(payload);
  return new Promise((resolve, reject) => {
    const request = https.request(
      {
        hostname: "api.capmonster.cloud",
        path: endpoint,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (response) => {
        let raw = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => { raw += chunk; });
        response.on("end", () => {
          try {
            const parsed = JSON.parse(raw || "{}");
            if (Number(parsed.errorId || 0) !== 0) {
              const code = parsed.errorCode || "UNKNOWN_ERROR";
              const description = parsed.errorDescription || "CapMonster request failed";
              reject(new Error(code + ": " + description));
              return;
            }
            resolve(parsed);
          } catch (e) {
            reject(new Error("CapMonster returned invalid JSON"));
          }
        });
      }
    );
    request.on("error", reject);
    request.setTimeout(30000, () => request.destroy(new Error("CapMonster request timed out")));
    request.write(body);
    request.end();
  });
}

async function discoverCaptcha(page) {
  return await page.evaluate(() => {
    const attr = (node, name) => node && node.getAttribute ? node.getAttribute(name) : "";
    const visibleCaptchaFrame = (frame) => {
      if (!frame || !frame.src) return false;
      if (/[?&]size=invisible(?:&|$)/.test(frame.src)) return false;
      const style = window.getComputedStyle ? window.getComputedStyle(frame) : null;
      const rect = frame.getBoundingClientRect ? frame.getBoundingClientRect() : { width: 0, height: 0 };
      return rect.width >= 40 && rect.height >= 40 && !(style && (style.display === "none" || style.visibility === "hidden"));
    };
    const keyFromUrl = (raw) => {
      try {
        const url = new URL(raw, window.location.href);
        return url.searchParams.get("k") || url.searchParams.get("sitekey") || url.searchParams.get("render") || "";
      } catch (e) {
        return "";
      }
    };
    const urlParam = (raw, name) => {
      try {
        const url = new URL(raw, window.location.href);
        return url.searchParams.get(name) || "";
      } catch (e) {
        return "";
      }
    };
    const cookieValue = (name) => {
      const found = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith(name + "="));
      return found || "";
    };
    const scriptText = Array.from(document.querySelectorAll("script:not([src])")).map((script) => script.textContent || "").join("\\n");
    const currentURL = window.location.href;
    const dataDomeFrame = Array.from(document.querySelectorAll("iframe[src*='captcha-delivery.com'], iframe[src*='geo.captcha-delivery.com']")).find((frame) => frame.src);
    const dataDomeScript = Array.from(document.querySelectorAll("script[src*='captcha-delivery.com']")).find((script) => script.src);
    const dataDomeCaptchaUrl = currentURL.includes("captcha-delivery.com") ? currentURL : (dataDomeFrame ? dataDomeFrame.src : (dataDomeScript ? dataDomeScript.src : ""));
    if (dataDomeCaptchaUrl || /\\bdatadome\\b/i.test(document.body && document.body.innerText || "")) {
      const ddmCid = (scriptText.match(/cid\\s*[:=]\\s*['"]([^'"]+)['"]/) || [])[1] || "";
      const datadomeCookie = cookieValue("datadome") || (ddmCid ? "datadome=" + ddmCid : "");
      if (dataDomeCaptchaUrl && datadomeCookie) {
        return { kind: "datadome", websiteURL: currentURL, captchaUrl: dataDomeCaptchaUrl, datadomeCookie, datadomeVersion: "new", userAgent: navigator.userAgent };
      }
    }
    const findSiteKey = (selector) => {
      const node = document.querySelector(selector);
      return node ? attr(node, "data-sitekey") || attr(node, "sitekey") || "" : "";
    };
    const turnstileNode = document.querySelector(".cf-turnstile[data-sitekey], [data-sitekey][class*='turnstile' i], [data-sitekey][id*='turnstile' i]");
    const turnstileKey = turnstileNode ? attr(turnstileNode, "data-sitekey") || attr(turnstileNode, "sitekey") || "" : "";
    if (turnstileKey) {
      return {
        kind: "turnstile",
        websiteURL: window.location.href,
        websiteKey: turnstileKey,
        pageAction: attr(turnstileNode, "data-action") || attr(turnstileNode, "action") || "",
        data: attr(turnstileNode, "data-cdata") || attr(turnstileNode, "cdata") || "",
        userAgent: navigator.userAgent,
      };
    }
    const turnstileFrame = Array.from(document.querySelectorAll("iframe[src*='challenges.cloudflare.com']")).find((frame) => frame.src);
    const turnstileFrameKey = turnstileFrame ? keyFromUrl(turnstileFrame.src) : "";
    if (turnstileFrameKey) {
      return {
        kind: "turnstile",
        websiteURL: window.location.href,
        websiteKey: turnstileFrameKey,
        pageAction: urlParam(turnstileFrame.src, "action"),
        data: urlParam(turnstileFrame.src, "cData") || urlParam(turnstileFrame.src, "cdata"),
        userAgent: navigator.userAgent,
      };
    }
    const hcaptchaNode = document.querySelector(".h-captcha[data-sitekey], [data-sitekey][class*='h-captcha' i], [data-sitekey][id*='h-captcha' i], [data-sitekey][class*='hcaptcha' i], [data-sitekey][id*='hcaptcha' i]");
    const hcaptchaKey = hcaptchaNode ? attr(hcaptchaNode, "data-sitekey") || attr(hcaptchaNode, "sitekey") || "" : "";
    if (hcaptchaKey) {
      return {
        kind: "hcaptcha",
        websiteURL: currentURL,
        websiteKey: hcaptchaKey,
        invisible: attr(hcaptchaNode, "data-size") === "invisible",
        data: attr(hcaptchaNode, "data-rqdata") || attr(hcaptchaNode, "rqdata") || "",
        callback: attr(hcaptchaNode, "data-callback") || "",
        userAgent: navigator.userAgent,
        cookies: document.cookie || "",
      };
    }
    const hcaptchaFrame = Array.from(document.querySelectorAll("iframe[src*='hcaptcha.com']")).find((frame) => frame.src);
    const hcaptchaFrameKey = hcaptchaFrame ? keyFromUrl(hcaptchaFrame.src) : "";
    if (hcaptchaFrameKey) {
      return { kind: "hcaptcha", websiteURL: currentURL, websiteKey: hcaptchaFrameKey, userAgent: navigator.userAgent, cookies: document.cookie || "" };
    }
    const funToken = document.querySelector("#verification-token, #FunCaptcha-Token, input[name='fc-token'], input[name='verification-token']");
    const funTokenValue = funToken ? String(funToken.value || attr(funToken, "value") || "") : "";
    const funParams = Object.fromEntries(funTokenValue.split("|").map((item) => item.split("=")).filter((item) => item.length >= 2));
    const funFrame = Array.from(document.querySelectorAll("iframe[src*='arkoselabs.com'], iframe[src*='funcaptcha.com']")).find((frame) => frame.src);
    const funPublicKey = funParams.pk || findSiteKey("[data-pkey], [data-pk], [data-public-key], [data-fc-public-key]") || (funFrame ? urlParam(funFrame.src, "public_key") || urlParam(funFrame.src, "pk") : "");
    let funSubdomain = "";
    try {
      funSubdomain = funParams.surl ? new URL(decodeURIComponent(funParams.surl), currentURL).hostname : (funFrame ? new URL(funFrame.src, currentURL).hostname : "");
    } catch (e) {
      funSubdomain = "";
    }
    const funBlobNode = document.querySelector("[data-blob], [data-fc-blob]");
    const funBlob = funBlobNode ? attr(funBlobNode, "data-blob") || attr(funBlobNode, "data-fc-blob") : "";
    if (funPublicKey) return { kind: "funcaptcha", websiteURL: currentURL, websitePublicKey: funPublicKey, funcaptchaApiJSSubdomain: funSubdomain, data: funBlob ? JSON.stringify({ blob: funBlob }) : "", userAgent: navigator.userAgent };
    const geetestNode = document.querySelector("[data-gt], [data-geetest-gt]");
    const geetestGt = geetestNode ? attr(geetestNode, "data-gt") || attr(geetestNode, "data-geetest-gt") : ((scriptText.match(/["']gt["']\\s*:\\s*["']([^"']+)["']/) || [])[1] || "");
    const geetestChallenge = geetestNode ? attr(geetestNode, "data-challenge") || attr(geetestNode, "data-geetest-challenge") : ((scriptText.match(/["']challenge["']\\s*:\\s*["']([^"']+)["']/) || [])[1] || "");
    if (geetestGt) return { kind: "geetest", websiteURL: currentURL, gt: geetestGt, challenge: geetestChallenge, version: 3, userAgent: navigator.userAgent };
    const enterpriseFrame = Array.from(document.querySelectorAll("iframe[src*='recaptcha/enterprise']")).find(visibleCaptchaFrame);
    const enterpriseFrameKey = enterpriseFrame ? keyFromUrl(enterpriseFrame.src) : "";
    const enterpriseFramePayload = enterpriseFrame ? urlParam(enterpriseFrame.src, "s") : "";
    const enterpriseScript = Array.from(document.querySelectorAll("script[src*='recaptcha/enterprise']")).find((script) => script.src);
    const enterpriseRenderKey = enterpriseScript ? keyFromUrl(enterpriseScript.src) : "";
    const enterpriseNode = document.querySelector(".g-recaptcha[data-sitekey], [data-sitekey][class*='recaptcha' i], [data-sitekey][id*='recaptcha' i]");
    const enterprisePayload = {};
    if (enterpriseNode) {
      Array.from(enterpriseNode.attributes || []).forEach((attribute) => {
        if (attribute.name.startsWith("data-") && attribute.name !== "data-sitekey") {
          enterprisePayload[attribute.name.slice(5)] = attribute.value;
        }
      });
    }
    const greenhouseEnterpriseKey = window.ENV && window.ENV.GOOGLE_RECAPTCHA_INVISIBLE_KEY;
    const greenhouseEnterpriseEndpoint = String(window.ENV && window.ENV.GOOGLE_RECAPTCHA_ENDPOINT || "");
    if (enterpriseFramePayload) enterprisePayload.s = enterpriseFramePayload;
    if (enterpriseFrameKey) return { kind: "recaptchaV2Enterprise", websiteURL: currentURL, websiteKey: enterpriseFrameKey, enterprisePayload, invisible: /[?&]size=invisible(?:&|$)/.test(enterpriseFrame.src), userAgent: navigator.userAgent };
    const isAshby = /(^|\\.)ashbyhq\\.com$/i.test(window.location.hostname);
    const v3Action = isAshby ? "job_apply" : "verify";
    const v3MinScore = isAshby ? 0.7 : null;
    if (enterpriseRenderKey && enterpriseRenderKey !== "explicit") return { kind: "recaptchaV3Enterprise", websiteURL: currentURL, websiteKey: enterpriseRenderKey, pageAction: v3Action, minScore: v3MinScore, userAgent: navigator.userAgent };
    const recaptchaV3Script = Array.from(document.querySelectorAll("script[src*='recaptcha/api.js?render=']")).find((script) => script.src);
    const recaptchaV3Key = recaptchaV3Script ? keyFromUrl(recaptchaV3Script.src) : "";
    if (recaptchaV3Key && recaptchaV3Key !== "explicit") return { kind: "recaptchaV3", websiteURL: currentURL, websiteKey: recaptchaV3Key, pageAction: v3Action, minScore: v3MinScore, userAgent: navigator.userAgent };
    const recaptchaNode = document.querySelector(".g-recaptcha[data-sitekey], [data-sitekey][class*='recaptcha' i], [data-sitekey][id*='recaptcha' i]");
    const recaptchaKey = recaptchaNode ? attr(recaptchaNode, "data-sitekey") || attr(recaptchaNode, "sitekey") || "" : "";
    if (recaptchaKey) {
      return {
        kind: "recaptchaV2",
        websiteURL: window.location.href,
        websiteKey: recaptchaKey,
        invisible: attr(recaptchaNode, "data-size") === "invisible",
        callback: attr(recaptchaNode, "data-callback"),
        userAgent: navigator.userAgent,
        cookies: document.cookie || "",
        recaptchaDataSValue: attr(recaptchaNode, "data-s") || "",
      };
    }
    const recaptchaFrame = Array.from(document.querySelectorAll("iframe[src*='recaptcha']")).find(visibleCaptchaFrame);
    const recaptchaFrameKey = recaptchaFrame ? keyFromUrl(recaptchaFrame.src) : "";
    if (recaptchaFrameKey) {
      return {
        kind: "recaptchaV2",
        websiteURL: window.location.href,
        websiteKey: recaptchaFrameKey,
        invisible: false,
        callback: "",
        userAgent: navigator.userAgent,
        cookies: document.cookie || "",
        recaptchaDataSValue: urlParam(recaptchaFrame.src, "s"),
      };
    }
    if (greenhouseEnterpriseKey && greenhouseEnterpriseEndpoint.includes("recaptcha/enterprise")) {
      return { kind: "recaptchaV3Enterprise", websiteURL: currentURL, websiteKey: greenhouseEnterpriseKey, pageAction: "apply_to_job", minScore: 0.7, userAgent: navigator.userAgent };
    }
    return null;
  }).catch(() => null);
}

function capMonsterTaskFor(challenge, cfg) {
  if (!challenge || !challenge.websiteURL) return null;
  if (challenge.kind === "turnstile") {
    if (!challenge.websiteKey) return null;
    const task = {
      type: "TurnstileTask",
      websiteURL: challenge.websiteURL,
      websiteKey: challenge.websiteKey,
    };
    if (challenge.userAgent) task.userAgent = challenge.userAgent;
    if (challenge.pageAction) task.pageAction = challenge.pageAction;
    if (challenge.data) task.data = challenge.data;
    if (cfg.proxy) Object.assign(task, cfg.proxy);
    return task;
  }
  if (challenge.kind === "hcaptcha") {
    if (!challenge.websiteKey) return null;
    const task = {
      type: cfg.hcaptchaTaskType || "HCaptchaTaskProxyless",
      websiteURL: challenge.websiteURL,
      websiteKey: challenge.websiteKey,
    };
    if (challenge.invisible) task.isInvisible = true;
    if (challenge.data) task.data = challenge.data;
    if (challenge.userAgent) {
      task.userAgent = challenge.userAgent;
      task.fallbackToActualUA = false;
    }
    if (challenge.cookies) task.cookies = challenge.cookies;
    if (task.type === "HCaptchaTask" && cfg.proxy) Object.assign(task, cfg.proxy);
    return task;
  }
  if (challenge.kind === "recaptchaV2") {
    if (!challenge.websiteKey) return null;
    const task = {
      type: "RecaptchaV2Task",
      websiteURL: challenge.websiteURL,
      websiteKey: challenge.websiteKey,
    };
    if (challenge.invisible) task.isInvisible = true;
    if (challenge.userAgent) task.userAgent = challenge.userAgent;
    if (challenge.cookies) task.cookies = challenge.cookies;
    if (challenge.recaptchaDataSValue) task.recaptchaDataSValue = challenge.recaptchaDataSValue;
    return task;
  }
  if (challenge.kind === "recaptchaV2Enterprise") {
    if (!challenge.websiteKey) return null;
    const task = {
      type: "RecaptchaV2EnterpriseTaskProxyless",
      websiteURL: challenge.websiteURL,
      websiteKey: challenge.websiteKey,
    };
    if (challenge.enterprisePayload && Object.keys(challenge.enterprisePayload).length) task.enterprisePayload = challenge.enterprisePayload;
    if (challenge.pageAction) task.pageAction = challenge.pageAction;
    if (challenge.invisible) task.isInvisible = true;
    if (challenge.userAgent) task.userAgent = challenge.userAgent;
    return task;
  }
  if (challenge.kind === "recaptchaV3" || challenge.kind === "recaptchaV3Enterprise") {
    if (!challenge.websiteKey) return null;
    const task = {
      type: challenge.kind === "recaptchaV3Enterprise" ? "RecaptchaV3EnterpriseTask" : "RecaptchaV3TaskProxyless",
      websiteURL: challenge.websiteURL,
      websiteKey: challenge.websiteKey,
      pageAction: challenge.pageAction || "verify",
      minScore: typeof challenge.minScore === "number" ? challenge.minScore : cfg.recaptchaMinScore,
    };
    if (challenge.userAgent) task.userAgent = challenge.userAgent;
    return task;
  }
  if (challenge.kind === "funcaptcha") {
    if (!challenge.websitePublicKey) return null;
    const task = {
      type: "FunCaptchaTask",
      websiteURL: challenge.websiteURL,
      websitePublicKey: challenge.websitePublicKey,
    };
    if (challenge.funcaptchaApiJSSubdomain) task.funcaptchaApiJSSubdomain = challenge.funcaptchaApiJSSubdomain;
    if (challenge.data) task.data = challenge.data;
    if (challenge.userAgent) task.userAgent = challenge.userAgent;
    if (cfg.proxy) Object.assign(task, cfg.proxy);
    return task;
  }
  if (challenge.kind === "geetest") {
    if (!challenge.gt) return null;
    const task = {
      type: "GeeTestTask",
      websiteURL: challenge.websiteURL,
      gt: challenge.gt,
      version: Number(challenge.version || 3),
    };
    if (challenge.challenge) task.challenge = challenge.challenge;
    if (challenge.geetestApiServerSubdomain) task.geetestApiServerSubdomain = challenge.geetestApiServerSubdomain;
    if (challenge.userAgent) task.userAgent = challenge.userAgent;
    if (cfg.proxy) Object.assign(task, cfg.proxy);
    return task;
  }
  if (challenge.kind === "datadome") {
    if (!cfg.proxy || !challenge.captchaUrl || !challenge.datadomeCookie) return null;
    return {
      type: "CustomTask",
      class: "DataDome",
      websiteURL: challenge.websiteURL,
      userAgent: challenge.userAgent,
      metadata: {
        captchaUrl: challenge.captchaUrl,
        datadomeCookie: challenge.datadomeCookie,
        datadomeVersion: challenge.datadomeVersion || "new",
      },
      ...cfg.proxy,
    };
  }
  return null;
}

function capMonsterTasksFor(challenge, cfg) {
  if (!challenge || challenge.kind !== "hcaptcha" || !challenge.websiteURL || !challenge.websiteKey) {
    const task = capMonsterTaskFor(challenge, cfg);
    if (!task) return [];
    const tasks = [task];
    if (challenge && challenge.kind === "recaptchaV2") {
      ["NoCaptchaTaskProxyless", "RecaptchaV2TaskProxyless"].forEach((taskType) => {
        if (taskType !== task.type) tasks.push({ ...task, type: taskType });
      });
    }
    if (challenge && challenge.kind === "turnstile" && task.type !== "TurnstileTaskProxyless") {
      tasks.push({ ...task, type: "TurnstileTaskProxyless" });
    }
    return tasks;
  }
  const taskTypes = ["HCaptchaTaskProxyless", "HCaptchaTask"];
  const configuredType = cfg.hcaptchaTaskType || "HCaptchaTaskProxyless";
  if (taskTypes.includes(configuredType)) {
    taskTypes.splice(taskTypes.indexOf(configuredType), 1);
    taskTypes.unshift(configuredType);
  }
  return taskTypes.map((taskType) => {
    const task = {
      type: taskType,
      websiteURL: challenge.websiteURL,
      websiteKey: challenge.websiteKey,
    };
    if (challenge.invisible) task.isInvisible = true;
    if (challenge.data) task.data = challenge.data;
    if (challenge.userAgent) {
      task.userAgent = challenge.userAgent;
      task.fallbackToActualUA = false;
    }
    if (challenge.cookies) task.cookies = challenge.cookies;
    if (task.type === "HCaptchaTask" && cfg.proxy) Object.assign(task, cfg.proxy);
    return task;
  });
}

function isCapMonsterTaskTypeError(error) {
  const detail = String(error && error.message ? error.message : error);
  return detail.includes("ERROR_TASK_NOT_SUPPORTED")
    || detail.includes("Task type is not supported")
    || detail.includes("typed incorrectly");
}

async function solveCapMonsterTask(task, cfg) {
  const created = await capMonsterPost("/createTask", { clientKey: cfg.apiKey, task });
  const taskId = created.taskId;
  if (!taskId) throw new Error("CapMonster did not return taskId");
  const deadline = Date.now() + cfg.timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, cfg.pollIntervalMs));
    const result = await capMonsterPost("/getTaskResult", { clientKey: cfg.apiKey, taskId });
    if (result.status === "ready") return result.solution || {};
    if (result.status && result.status !== "processing") throw new Error("Unexpected CapMonster task status: " + result.status);
  }
  throw new Error("CapMonster task " + taskId + " was not ready before timeout");
}

async function injectCaptchaSolution(page, challenge, solution) {
  if (challenge.kind === "datadome") {
    const domains = solution && solution.domains;
    if (!domains || typeof domains !== "object") return false;
    const cookies = [];
    for (const [domain, payload] of Object.entries(domains)) {
      const domainCookies = payload && payload.cookies;
      if (!domainCookies || typeof domainCookies !== "object") continue;
      for (const [name, value] of Object.entries(domainCookies)) {
        if (name && value) cookies.push({ name, value: String(value), domain, path: "/" });
      }
    }
    if (!cookies.length) return false;
    await page.context().addCookies(cookies);
    return true;
  }
  if (challenge.kind === "geetest") {
    if (!solution || (!solution.validate && !solution.seccode)) return false;
    return Boolean(await page.evaluate((solution) => {
      const values = {
        geetest_challenge: solution.challenge || "",
        geetest_validate: solution.validate || "",
        geetest_seccode: solution.seccode || "",
      };
      const setValue = (name, value) => {
        if (!value) return false;
        let node = document.querySelector(`input[name="${name}"], textarea[name="${name}"]`);
        if (!node) {
          node = document.createElement("input");
          node.type = "hidden";
          node.name = name;
          document.body.appendChild(node);
        }
        const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
        if (descriptor && descriptor.set) descriptor.set.call(node, value);
        else node.value = value;
        node.dispatchEvent(new Event("input", { bubbles: true }));
        node.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      };
      let injected = false;
      Object.entries(values).forEach(([name, value]) => { injected = setValue(name, value) || injected; });
      return injected;
    }, solution).catch(() => false));
  }
  const token = solution && (solution.gRecaptchaResponse || solution.token || solution.recaptchaResponse || solution.cf_clearance);
  if (!token) return false;
  return Boolean(await page.evaluate(({ challenge, token }) => {
    let captchaApiIntercepted = false;
    try {
      const target = challenge.kind === "recaptchaV3Enterprise"
        ? window.grecaptcha && window.grecaptcha.enterprise
        : String(challenge.kind || "").startsWith("recaptcha")
          ? window.grecaptcha
          : null;
      if (target) {
        const assign = (name, value) => {
          try { target[name] = value; } catch (e) {}
          if (target[name] !== value) {
            try { Object.defineProperty(target, name, { configurable: true, value }); } catch (e) {}
          }
          return target[name] === value;
        };
        const solved = () => Promise.resolve(token);
        const response = () => token;
        const reset = () => {};
        if (typeof target.execute === "function") {
          captchaApiIntercepted = assign("execute", solved) || captchaApiIntercepted;
        }
        if (typeof target.getResponse === "function") {
          captchaApiIntercepted = assign("getResponse", response) || captchaApiIntercepted;
        }
        if (typeof target.reset === "function") {
          assign("reset", reset);
        }
      }
    } catch (e) {}
    const selectors = challenge.kind === "turnstile"
      ? ["textarea[name='cf-turnstile-response']", "input[name='cf-turnstile-response']"]
      : challenge.kind === "hcaptcha"
        ? ["textarea[name='h-captcha-response']", "textarea[name='hcaptcha-response']", "input[name='h-captcha-response']", "input[name='hcaptcha-response']"]
      : challenge.kind === "funcaptcha"
        ? ["#verification-token", "#FunCaptcha-Token", "input[name='fc-token']", "input[name='verification-token']"]
        : ["textarea[name='g-recaptcha-response']", "input[name='g-recaptcha-response']"];
    const setValue = (node) => {
      if (!node) return false;
      const proto = node.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
      if (descriptor && descriptor.set) descriptor.set.call(node, token);
      else node.value = token;
      node.dispatchEvent(new Event("input", { bubbles: true }));
      node.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    };
    let injected = false;
    selectors.forEach((selector) => {
      document.querySelectorAll(selector).forEach((node) => { injected = setValue(node) || injected; });
    });
    if (!injected && String(challenge.kind || "").startsWith("recaptcha")) {
      const textarea = document.createElement("textarea");
      textarea.name = "g-recaptcha-response";
      textarea.style.display = "none";
      document.body.appendChild(textarea);
      injected = setValue(textarea);
    }
    if (!injected && challenge.kind === "hcaptcha") {
      const textarea = document.createElement("textarea");
      textarea.name = "h-captcha-response";
      textarea.style.display = "none";
      document.body.appendChild(textarea);
      injected = setValue(textarea);
    }
    if (!injected && challenge.kind === "funcaptcha") {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "fc-token";
      document.body.appendChild(input);
      injected = setValue(input);
    }
    if (challenge.callback && typeof window[challenge.callback] === "function") {
      try { window[challenge.callback](token); } catch (e) {}
    }
    return injected || captchaApiIntercepted;
  }, { challenge, token }).catch(() => false));
}

function captchaVisionConfig() {
  const enabled = /^(1|true|yes|on)$/i.test(String(process.env.CAPTCHA_VISION_FALLBACK || "0").trim());
  const apiKey = String(process.env.OPENAI_API_KEY || process.env.LLM_API_KEY || "").trim();
  const model = String(process.env.CAPTCHA_VISION_MODEL || process.env.LLM_MODEL_ID || "gpt-4.1-mini").trim();
  const baseUrl = String(process.env.LLM_BASE_URL || "https://api.openai.com/v1").replace(/\/+$/, "");
  const rounds = Math.min(12, Math.max(1, Number(process.env.CAPTCHA_VISION_MAX_ROUNDS || 8) || 8));
  return { enabled: enabled && Boolean(apiKey) && Boolean(model), apiKey, model, baseUrl, rounds };
}

function postVisionJson(endpoint, apiKey, payload) {
  const body = JSON.stringify(payload);
  const url = new URL(endpoint);
  return new Promise((resolve, reject) => {
    const request = https.request(
      {
        protocol: url.protocol,
        hostname: url.hostname,
        port: url.port || undefined,
        path: url.pathname + url.search,
        method: "POST",
        headers: {
          "Authorization": "Bearer " + apiKey,
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (response) => {
        let raw = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => { raw += chunk; });
        response.on("end", () => {
          try {
            const parsed = JSON.parse(raw);
            if ((response.statusCode || 500) >= 400) {
              reject(new Error((parsed.error && parsed.error.message) || "vision API HTTP " + response.statusCode));
              return;
            }
            resolve(parsed);
          } catch (error) {
            reject(new Error("vision API returned invalid JSON"));
          }
        });
      }
    );
    request.setTimeout(90000, () => request.destroy(new Error("vision API request timed out")));
    request.on("error", reject);
    request.write(body);
    request.end();
  });
}

function parseVisionClicks(raw, width, height) {
  const match = String(raw || "").match(/\{[\s\S]*\}/);
  if (!match) throw new Error("vision response did not contain JSON");
  const payload = JSON.parse(match[0]);
  let clicks = payload.clicks || payload.target_points || payload.targets;
  if (!Array.isArray(clicks) && Array.isArray(payload.objects)) {
    const objects = payload.objects.filter((item) => item && typeof item === "object");
    const selected = objects.filter((item) => item.matches === true || item.selected === true);
    clicks = selected.length ? selected : objects;
  }
  if (!Array.isArray(clicks) && payload.x != null && payload.y != null) clicks = [{ x: payload.x, y: payload.y }];
  if (!Array.isArray(clicks)) throw new Error("vision response did not contain clicks");
  const valid = clicks.slice(0, 12).map((click) => Array.isArray(click)
    ? ({ x: Number(click[0]), y: Number(click[1]) })
    : ({ x: Number(click && click.x), y: Number(click && click.y) }))
    .filter((click) => Number.isFinite(click.x) && Number.isFinite(click.y) && click.x >= 0 && click.x <= width && click.y >= 0 && click.y <= height);
  if (!valid.length) throw new Error("vision response had no in-bounds clicks");
  return valid;
}

function parseComplexImageClicks(solution, width, height) {
  const raw = solution && (solution.clicks || solution.coordinates || solution.objects || solution.answer || solution.answers);
  if (!Array.isArray(raw)) return [];
  const parsed = [];
  const grid = [];
  for (const item of raw) {
    if (typeof item === "boolean" || typeof item === "number") {
      grid.push(Boolean(item));
      continue;
    }
    const candidate = Array.isArray(item) && item.length >= 2
      ? { x: item[0], y: item[1] }
      : (item && typeof item === "object" && item.center && typeof item.center === "object" ? item.center : item);
    if (!candidate || typeof candidate !== "object") continue;
    const x = Number(candidate.x);
    const y = Number(candidate.y);
    if (Number.isFinite(x) && Number.isFinite(y) && x >= 0 && x <= width && y >= 0 && y <= height) parsed.push({ x, y });
  }
  if (parsed.length) return parsed.slice(0, 12);
  if (!grid.length) return [];
  let columns = 3;
  let rows = 3;
  const gridText = String(solution && solution.metadata && solution.metadata.Grid || "");
  const match = gridText.match(/(\d+)\s*[xX]\s*(\d+)/);
  if (match) {
    columns = Math.max(1, Number(match[1]));
    rows = Math.max(1, Number(match[2]));
  } else if ([4, 9, 16].includes(grid.length)) {
    columns = rows = Math.sqrt(grid.length);
  }
  return grid.slice(0, columns * rows).map((selected, index) => {
    if (!selected) return null;
    const column = index % columns;
    const row = Math.floor(index / columns);
    return { x: (column + 0.5) * width / columns, y: (row + 0.5) * height / rows };
  }).filter(Boolean).slice(0, 12);
}

async function solveComplexImageClicksWithCapMonster(image, instruction, width, height) {
  const cfg = capMonsterConfig();
  if (!cfg.enabled) return [];
  const solution = await solveCapMonsterTask({
    type: "ComplexImageTask",
    class: String(process.env.CAPMONSTER_COMPLEX_IMAGE_CLASS || "recognition"),
    imagesBase64: [image.toString("base64")],
    metadata: { Task: instruction },
  }, cfg);
  return parseComplexImageClicks(solution, width, height);
}

async function hcaptchaResponse(page) {
  return String(await page.evaluate(() => window.hcaptcha && typeof window.hcaptcha.getResponse === "function" ? (window.hcaptcha.getResponse() || "") : "").catch(() => ""));
}

async function visibleHcaptchaChallengeFrame(page) {
  for (const frame of page.frames()) {
    if (!String(frame.url()).includes("frame=challenge")) continue;
    try {
      const element = await frame.frameElement();
      if (await element.isVisible()) return { frame, element };
    } catch (error) {}
  }
  return null;
}

async function solveHcaptchaWithVision(page) {
  const cfg = captchaVisionConfig();
  if (!cfg.enabled) return { status: "unsupported", detail: "hcaptcha vision fallback disabled or missing API key" };
  try {
    await page.evaluate(() => {
      if (!window.hcaptcha || typeof window.hcaptcha.execute !== "function") return false;
      window.hcaptcha.execute();
      return true;
    });
    await page.waitForTimeout(4000);
    for (let round = 1; round <= cfg.rounds; round++) {
      if (await hcaptchaResponse(page)) return { status: "solved", detail: "hcaptcha vision fallback in " + (round - 1) + " rounds" };
      let visible = await visibleHcaptchaChallengeFrame(page);
      if (!visible) {
        await page.waitForTimeout(1500);
        if (await hcaptchaResponse(page)) return { status: "solved", detail: "hcaptcha vision fallback in " + (round - 1) + " rounds" };
        continue;
      }
      const canvas = visible.frame.locator("canvas").first();
      const canvasBox = await canvas.boundingBox();
      const header = visible.frame.locator(".challenge-header, .challenge-prompt").first();
      const headerBox = await header.boundingBox().catch(() => null);
      if (!canvasBox || !canvasBox.width || !canvasBox.height) continue;
      const imageTop = Math.max(canvasBox.y, headerBox ? headerBox.y + headerBox.height + 10 : canvasBox.y + 120);
      const box = {
        x: canvasBox.x,
        y: imageTop,
        width: canvasBox.width,
        height: Math.max(0, canvasBox.y + canvasBox.height - imageTop),
      };
      if (!box.height) continue;
      const image = await page.screenshot({ clip: box });
      const instruction = String(await visible.frame.locator(".challenge-prompt").innerText({ timeout: 3000 }).catch(() => "Select every image that satisfies the visible instruction.")).trim();
      const prompt = `Instruction: ${JSON.stringify(instruction)}. Translate it exactly if needed. Inspect the entire image despite camouflage, ` +
        "identify every distinct depicted item including vehicles and machinery, and apply the instruction exactly. " +
        "Treat each picture/card as the real-world object it represents, not as a lightweight photo or card. Return ONLY JSON " +
        `with {\"objects\":[{\"name\":\"item\",\"x\":number,\"y\":number,\"matches\":boolean}],` +
        `\"clicks\":[{\"x\":number,\"y\":number}],\"confidence\":number,\"reason\":\"short\"}. ` +
        `Coordinates are pixels in the full supplied image (${Math.round(box.width)}x${Math.round(box.height)}). ` +
        "Include every target that must be selected, in click order. Do not include markdown.";
      let clicks = [];
      try {
        clicks = await solveComplexImageClicksWithCapMonster(image, instruction, box.width, box.height);
        if (clicks.length) console.log("hCaptcha CapMonster ComplexImage: clicking " + clicks.length + " target(s)");
      } catch (error) {}
      if (!clicks.length) {
        const response = await postVisionJson(cfg.baseUrl + "/chat/completions", cfg.apiKey, {
          model: cfg.model,
          messages: [{ role: "user", content: [
            { type: "text", text: prompt },
            { type: "image_url", image_url: { url: "data:image/png;base64," + image.toString("base64"), detail: "high" } },
          ] }],
          temperature: 0,
          max_tokens: 300,
        });
        const content = response && response.choices && response.choices[0] && response.choices[0].message && response.choices[0].message.content;
        clicks = parseVisionClicks(content, box.width, box.height);
      }
      console.log("hCaptcha vision round " + round + ": clicking " + clicks.length + " target(s)");
      for (const click of clicks) {
        try {
          await page.mouse.click(box.x + click.x, box.y + click.y);
          await page.waitForTimeout(400);
        } catch (error) {
          break;
        }
      }
      try {
        const verify = visible.frame.getByRole("button", { name: /^(verify|next)$/i }).last();
        if (await verify.count() && await verify.isVisible()) await verify.click({ force: true, timeout: 5000 });
      } catch (error) {}
      await page.waitForTimeout(3500);
    }
    if (await hcaptchaResponse(page)) return { status: "solved", detail: "hcaptcha vision fallback" };
    return { status: "error", detail: "hcaptcha vision fallback exhausted rounds" };
  } catch (error) {
    return { status: "error", detail: "hcaptcha vision fallback failed: " + (error && error.message ? error.message : String(error)) };
  }
}

async function solveCaptchaIfConfigured(page) {
  const cfg = capMonsterConfig();
  const visionCfg = captchaVisionConfig();
  const challenge = await discoverCaptcha(page);
  if (!challenge || typeof challenge !== "object" || !challenge.kind) return { status: "none", detail: "no supported CAPTCHA detected" };
  if (challenge.kind === "hcaptcha") {
    if (visionCfg.enabled) return solveHcaptchaWithVision(page);
    return {
      status: "unsupported",
      detail: "hcaptcha is not supported by CapMonster token tasks; vision fallback disabled or missing API key",
    };
  }
  if (!cfg.enabled && !visionCfg.enabled) return { status: "skipped", detail: "disabled" };
  if (!cfg.enabled) return { status: "skipped", detail: "CapMonster disabled" };
  const tasks = capMonsterTasksFor(challenge, cfg);
  if (!tasks.length) return { status: "unsupported", detail: challenge.kind || "unknown" };
  try {
    const errors = [];
    let solution = null;
    for (let index = 0; index < tasks.length; index++) {
      const task = tasks[index];
      try {
        solution = await solveCapMonsterTask(task, cfg);
        break;
      } catch (error) {
        errors.push(String(task.type || "unknown") + ": " + (error && error.message ? error.message : String(error)));
        if (!isCapMonsterTaskTypeError(error) || index >= tasks.length - 1) {
          throw new Error(challenge.kind === "hcaptcha" && errors.length ? errors.join("; ") : (error && error.message ? error.message : String(error)));
        }
      }
    }
    if (!solution) throw new Error("CapMonster did not return a solution");
    const injected = await injectCaptchaSolution(page, challenge, solution);
    const detail = challenge.kind + " at " + challenge.websiteURL + (solution.userAgent ? " (solution userAgent returned)" : "");
    return {
      status: injected ? "solved" : "solution_not_injected",
      detail,
    };
  } catch (e) {
    if (challenge.kind === "hcaptcha") {
      const vision = await solveHcaptchaWithVision(page);
      if (vision.status === "solved") return vision;
      return { status: vision.status, detail: "CapMonster token API: " + (e.message || String(e)) + "; vision fallback: " + vision.detail };
    }
    return { status: "error", detail: e.message || String(e) };
  }
}

function captchaResultBlocksSubmission(captchaResult) {
  const status = String(captchaResult && captchaResult.status || "").trim().toLowerCase();
  return Boolean(status) && !["none", "skipped", "solved"].includes(status);
}

const WORK_LABEL_PATTERNS = {
  title: ["job title", "position title", "role title"],
  company: ["company", "employer", "organization"],
  start_month: ["start date month"],
  start_year: ["start date year"],
  start_date: ["start date", "from date"],
  end_month: ["end date month", "to date month"],
  end_year: ["end date year", "to date year"],
  end_date: ["end date", "to date"],
  description: ["description", "responsibilities"],
  location: ["location", "city"],
};
const EDU_LABEL_PATTERNS = {
  school: ["school", "university", "institution", "college"],
  degree: ["degree"],
  field: ["field of study", "major"],
  start_month: ["start date month"],
  start_year: ["start date year"],
  start_date: ["start date", "from date"],
  end_month: ["end date month"],
  end_year: ["end date year"],
  end_date: ["end date", "graduation"],
  gpa: ["gpa"],
};

function verifyRuntimeResumeFile() {
  const sourceDir = String(CFG.resumeSourceDir || "").trim();
  const requiredResume = String(CFG.requiredResumePdf || "").trim();
  if (!CFG.resumeFile) {
    if (sourceDir || requiredResume) {
      throw new Error("missing required PDF resume upload path");
    }
    return;
  }
  const resumePath = path.resolve(__dirname, CFG.resumeFile);
  if (path.extname(resumePath).toLowerCase() !== ".pdf") {
    throw new Error("resume upload must be an existing PDF: " + resumePath);
  }
  if (!fs.existsSync(resumePath) || !fs.statSync(resumePath).isFile()) {
    throw new Error("resume upload PDF does not exist: " + resumePath);
  }
  const packageDir = path.resolve(__dirname);
  const relativeToPackage = path.relative(packageDir, resumePath);
  if (relativeToPackage && !relativeToPackage.startsWith("..") && !path.isAbsolute(relativeToPackage)) {
    throw new Error("resume upload PDF must be an original external path, not package-local: " + resumePath);
  }
  if (sourceDir) {
    const sourcePath = path.resolve(sourceDir);
    const relativeToSource = path.relative(sourcePath, resumePath);
    if (!relativeToSource || relativeToSource.startsWith("..") || path.isAbsolute(relativeToSource)) {
      throw new Error("resume upload PDF must come from required resume source dir: " + resumePath + "; expected under: " + sourcePath);
    }
  }
  if (requiredResume) {
    const requiredResumePath = path.resolve(requiredResume);
    if (path.extname(requiredResumePath).toLowerCase() !== ".pdf" || !fs.existsSync(requiredResumePath) || !fs.statSync(requiredResumePath).isFile()) {
      throw new Error("required resume PDF is not an existing PDF: " + requiredResumePath);
    }
    if (resumePath !== requiredResumePath) {
      throw new Error("resume upload PDF does not match required path: " + resumePath + "; expected: " + requiredResumePath);
    }
  }
  CFG.resumeFile = resumePath;
}

async function main() {
  verifyRuntimeResumeFile();
  let browser = null;
  try {
  const headlessOverride = norm(process.env.BROWSER_HEADLESS || "");
  const effectiveHeadless = ["0", "false", "no", "off"].includes(headlessOverride)
    ? false
    : (["1", "true", "yes", "on"].includes(headlessOverride) ? true : CFG.headless);
  const launchOptions = { headless: effectiveHeadless, args: ["--disable-blink-features=AutomationControlled"] };
  if (process.env.BROWSER_CHANNEL) launchOptions.channel = String(process.env.BROWSER_CHANNEL).trim();
  browser = await chromium.launch(launchOptions);
  const context = typeof browser.newContext === "function"
    ? await browser.newContext(browserContextOptions())
    : browser;
  await installBrowserFingerprintMitigation(context);
  const page = await context.newPage();
  if (page.setDefaultTimeout) page.setDefaultTimeout(10000);
  const applicationUrl = runtimeApplicationUrl(CFG.applicationUrl);
  if (applicationUrl) await page.goto(applicationUrl);
  const ats = detectATS(applicationUrl || CFG.applicationUrl);
	  await openApplicationFormIfNeeded(page);
	  let applicationFormReady = await waitForApplicationFormContext(page);
	  if (!applicationFormReady) {
	    applicationFormReady = await recoverApplicationFormFromJobPage(page, applicationUrl);
	  }
	  if (!applicationFormReady) {
    const review = [{
      label: "Application form",
      reason: "no visible job-application form was found",
      sensitive: false,
      blocking: true,
    }];
    console.log(APPLICATION_FORM_UNAVAILABLE_LINE_PREFIX + " no visible job-application form was found");
    console.log("Review item: " + JSON.stringify(review[0]));
    const artifact = await writeReviewEvidence(page, review);
    if (artifact) console.log("Review evidence: " + artifact);
    console.log("Autofill stats: filled=0 review=1");
	    console.log("Submit gate: automatic submission not performed because blocking review fields remain or the final Submit control is unavailable.");
	    return;
	  }
	  await installApplicationNavigationGuard(page, applicationUrl);

  const allFilled = [];
  const allReview = [];
  const sectionReport = [];
  const appendUniqueFilled = (target, items) => {
    const seen = new Set(target.map((item) => `${item.label || ""}\u0000${item.action || ""}`));
    for (const item of items || []) {
      const key = `${item.label || ""}\u0000${item.action || ""}`;
      if (!seen.has(key)) {
        target.push(item);
        seen.add(key);
      }
    }
  };
  const parsedPasses = Number(process.env.JOB_AGENT_SELF_HEAL_PASSES || "3");
  const selfHealPasses = Math.min(5, Math.max(1, Number.isFinite(parsedPasses) ? Math.floor(parsedPasses) : 3));
  let pages = 0;
	  let repeatedWorkdaySignInPages = 0;
	  let lastWorkdaySignInSignature = "";
	  let repeatedWorkdaySignInFillPages = 0;
		  let lastWorkdaySignInFillSignature = "";
		  while (pages < CFG.maxPages) {
		    await restoreApplicationContextIfExternal(page, applicationUrl);
		    await restoreWorkdayApplicationFromCandidateHome(page, applicationUrl);
		    await installApplicationNavigationGuard(page, applicationUrl);
		    pages++;
		    const fieldsAtPageStart = await ensureApplicationFieldsReady(page);
		    if (
		      pages > 1
		      && lastWorkdaySignInFillSignature
		      && String(page.url && page.url() || "").toLowerCase().includes("myworkdayjobs.com")
		      && !meaningfulApplicationFields(fieldsAtPageStart).length
		      && await restoreWorkdayApplicationFromCandidateHome(page, applicationUrl)
		    ) {
		      pages--;
		      continue;
		    }
    const accountVerificationReasonAtPageStart = await workdayAccountVerificationReason(page);
    if (accountVerificationReasonAtPageStart) {
      allReview.push({
        label: "Candidate account verification",
        reason: accountVerificationReasonAtPageStart,
        sensitive: false,
        blocking: true,
      });
      break;
    }
    if (workdaySignInFieldSet(fieldsAtPageStart)) {
      const signInSignature = formFieldSignature(fieldsAtPageStart);
      if (signInSignature && signInSignature === lastWorkdaySignInSignature) {
        repeatedWorkdaySignInPages++;
      } else {
        repeatedWorkdaySignInPages = 1;
        lastWorkdaySignInSignature = signInSignature;
      }
      const explicitSignInReason = await workdaySignInFailureReason(page, { allowGeneric: false });
      if (explicitSignInReason || repeatedWorkdaySignInPages > 1) {
        if (await openWorkdayCreateAccountFromSignInIfAvailable(page, { requireFailure: false })) {
          repeatedWorkdaySignInPages = 0;
          lastWorkdaySignInSignature = "";
          continue;
        }
        allReview.push({
          label: "Candidate account sign-in",
          reason: explicitSignInReason
            || await workdaySignInFailureReason(page)
            || "candidate account sign-in rejected by Workday",
          sensitive: false,
          blocking: true,
        });
        break;
      }
    } else {
      repeatedWorkdaySignInPages = 0;
      lastWorkdaySignInSignature = "";
    }
    const stepBefore = await currentApplicationStep(page);
	    console.log(`Autofill progress: page ${pages} (${stepBefore || "application entry"})`);
	    let res = await fillPage(page, CFG.profile);
	    if (await restoreApplicationContextIfExternal(page, applicationUrl)) {
	      await installApplicationNavigationGuard(page, applicationUrl);
	      res = await fillPage(page, CFG.profile);
	    }
	    const fieldsBeforeNext = formFieldSignature(await ensureApplicationFieldsReady(page));
    const pageFilled = [...res.filled];

    // Repeatable multi-entry sections (Simplify fills work history + education).
    if (await hasSectionHeading(page, "work (experience|history)|employment")) {
      const r = await fillRepeatableSection(page, CFG.profile.work_history || [], mapWorkField, "experience", WORK_LABEL_PATTERNS);
      if (r.length) sectionReport.push({ section: "work_history", entries: r });
    }
    if (await hasSectionHeading(page, "education|academic")) {
      const r = await fillRepeatableSection(page, CFG.profile.education || [], mapEduField, "education", EDU_LABEL_PATTERNS);
      if (r.length) sectionReport.push({ section: "education", entries: r });
    }
    let requiredFindings = await auditRequiredFields(page);
    appendUniqueFilled(pageFilled, await repairInvalidRequiredFields(page, requiredFindings, CFG.profile));
    requiredFindings = await auditRequiredFields(page);
    appendRequiredAudit(res.review, requiredFindings, pageFilled);

    for (let attempt = 1; attempt < selfHealPasses; attempt++) {
      const previousBlocking = res.review.filter((item) => item.blocking !== false).length;
	      if (!previousBlocking) break;
	      await page.waitForTimeout(750);
	      await restoreApplicationContextIfExternal(page, applicationUrl);
	      await installApplicationNavigationGuard(page, applicationUrl);
	      const retry = await fillPage(page, CFG.profile);
      appendUniqueFilled(pageFilled, retry.filled);
      let retryRequiredFindings = await auditRequiredFields(page);
      appendUniqueFilled(pageFilled, await repairInvalidRequiredFields(page, retryRequiredFindings, CFG.profile));
      retryRequiredFindings = await auditRequiredFields(page);
      appendRequiredAudit(retry.review, retryRequiredFindings, pageFilled);
      const nextBlocking = retry.review.filter((item) => item.blocking !== false).length;
      res = retry;
      if (!retry.filled.length && nextBlocking >= previousBlocking) break;
    }
    const signInFillSignature = workdaySignInFillSignature(page, pageFilled);
    if (signInFillSignature) {
      if (signInFillSignature === lastWorkdaySignInFillSignature) {
        repeatedWorkdaySignInFillPages++;
      } else {
        repeatedWorkdaySignInFillPages = 1;
        lastWorkdaySignInFillSignature = signInFillSignature;
      }
      if (repeatedWorkdaySignInFillPages > 1) {
        const signInReason = await workdaySignInFailureReason(page);
        if (await openWorkdayCreateAccountFromSignInIfAvailable(page, { requireFailure: false })) {
          repeatedWorkdaySignInFillPages = 0;
          lastWorkdaySignInFillSignature = "";
          continue;
        }
        allReview.push({
          label: "Candidate account sign-in",
          reason: signInReason
            || "candidate account sign-in did not advance after filling email and password",
          sensitive: false,
          blocking: true,
        });
        break;
      }
    } else {
      repeatedWorkdaySignInFillPages = 0;
      lastWorkdaySignInFillSignature = "";
    }
	    let finalRequiredFindings = await auditRequiredFields(page);
	    appendUniqueFilled(pageFilled, await repairInvalidRequiredFields(page, finalRequiredFindings, CFG.profile));
	    finalRequiredFindings = await auditRequiredFields(page);
	    res.review = retainUnresolvedControlReviews(res.review, finalRequiredFindings);
	    appendRequiredAudit(res.review, finalRequiredFindings, pageFilled);
	    res.review = filterSuccessfulReadbackReviews(res.review, pageFilled);
	    appendUniqueFilled(allFilled, pageFilled);
	    allReview.push(...res.review);
    if (res.review.some((item) => item.blocking !== false)) break;

    const next = await findNextButton(page);
    if (next) {
      try {
        await clickButton(page, next);
	        await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
	        await page.waitForTimeout(2500);
	        await restoreApplicationContextIfExternal(page, applicationUrl);
	        await installApplicationNavigationGuard(page, applicationUrl);
	        const accountVerificationReasonAfterNext = await workdayAccountVerificationReason(page);
        if (accountVerificationReasonAfterNext) {
          allReview.push({
            label: "Candidate account verification",
            reason: accountVerificationReasonAfterNext,
            sensitive: false,
            blocking: true,
          });
          break;
        }
        const signInReasonAfterNext = await workdaySignInFailureReason(page, { allowGeneric: false });
        if (signInReasonAfterNext) {
          if (await openWorkdayCreateAccountFromSignInIfAvailable(page, { requireFailure: false })) {
            continue;
          }
          allReview.push({
            label: "Candidate account sign-in",
            reason: signInReasonAfterNext,
            sensitive: false,
            blocking: true,
          });
          break;
        }
        let stepAfter = await currentApplicationStep(page);
        let fieldsAfterNextList = await scrapeFields(page);
        let fieldsAfterNext = formFieldSignature(fieldsAfterNextList);
        if (pageDidNotAdvance(stepBefore, stepAfter, fieldsBeforeNext, fieldsAfterNext)) {
          try {
            if (next.autofillId) {
              await page.locator(attrSelector("data-job-agent-button-index", next.autofillId)).first().evaluate((node) => node.click());
              await page.waitForTimeout(2500);
            }
          } catch (_) {}
          stepAfter = await currentApplicationStep(page);
          fieldsAfterNextList = await scrapeFields(page);
          fieldsAfterNext = formFieldSignature(fieldsAfterNextList);
          if (pageDidNotAdvance(stepBefore, stepAfter, fieldsBeforeNext, fieldsAfterNext)) {
            const signInReason = await workdaySignInFailureReason(page, { allowGeneric: false });
            const createAccountReason = await workdayCreateAccountFailureReason(page);
            if (!signInReason && !createAccountReason && workdaySignInFieldSet(fieldsAfterNextList)) {
              continue;
            }
            const reason = signInReason || createAccountReason || (
              stepBefore
                ? `Save and Continue did not advance the Workday page: ${stepBefore}`
                : "Save and Continue did not advance the Workday page"
            );
            allReview.push({
              label: String(reason || "").startsWith("candidate account creation")
                ? "Candidate account creation"
                : (stepBefore || "Candidate account sign-in"),
              reason,
              sensitive: false,
              blocking: true,
            });
            break;
          }
        }
      } catch (e) {
        console.log("Could not advance to next page: " + e.message);
        break;
      }
      continue;
    }
    break; // reached final page (no Next button)
  }

	  for (const blocker of CFG.profile.submission_blockers || []) {
    allReview.push({
      label: String(blocker),
      reason: "package truthfulness gate",
      sensitive: false,
	      blocking: true,
	    });
		  }
		  await restoreApplicationContextIfExternal(page, applicationUrl);
		  await installApplicationNavigationGuard(page, applicationUrl);
		  allReview.splice(0, allReview.length, ...filterSuccessfulReadbackReviews(allReview, allFilled));
		  const blockingReview = allReview.filter((item) => item.blocking !== false);
  const captchaResult = blockingReview.length
    ? { status: "skipped", detail: "blocking review fields present" }
    : await solveCaptchaIfConfigured(page);
  let submit = await findSubmitButton(page);
  if (await isJobPageApplyButton(page, submit)) submit = null;
  console.log("=== Simplify-style autofill report ===");
  console.log("Detected ATS: " + ats);
  console.log("Pages filled: " + pages);
  console.log("Filled fields (" + allFilled.length + "):");
  allFilled.forEach((f) => {
    const rb = " | readback=" + readbackStatus(f.readback);
    console.log("  - [" + f.action + "] " + displayText(f.label) + (f.action === "upload" ? " -> file selected" : "") + rb);
  });
  if (sectionReport.length) {
    console.log("Repeatable sections:");
    sectionReport.forEach((s) => {
      console.log("  - " + s.section + ":");
      s.entries.forEach((e) =>
        console.log("      entry#" + e.entry + " " + e.field + " [" + e.label + "] readback=" + readbackStatus(e.readback))
      );
    });
  }
  console.log("Review-required (" + allReview.length + "):");
  allReview.forEach((r) => console.log("  - " + displayText(r.label) + " (" + displayText(r.reason) + ")"));
  allReview.forEach((r) =>
    console.log("Review item: " + JSON.stringify({
      label: r && r.label || "",
      reason: r && r.reason || "",
      sensitive: !!(r && r.sensitive),
      blocking: r && r.blocking !== false,
    }))
  );
  if (blockingReview.some((item) => item && item.reason === "candidate account creation required")) {
    console.log(CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX + " configured candidate account password is missing");
  }
  if (blockingReview.some((item) => String(item && item.reason || "").startsWith("candidate account sign-in rejected by Workday"))) {
    console.log(CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX + " configured candidate account credentials were rejected by Workday");
  }
  if (blockingReview.some((item) => String(item && item.reason || "").startsWith("candidate account verification required by Workday"))) {
    console.log(CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX + " candidate account verification is required by Workday");
  }
  console.log("CapMonster CAPTCHA: " + captchaResult.status + " (" + captchaResult.detail + ")");
  console.log("Final submit button present: " + (submit ? submit.text : "none"));
  console.log("Autofill stats: filled=" + allFilled.length + " review=" + allReview.length);
	  if (blockingReview.length) {
	    const reviewArtifact = await writeReviewEvidence(page, blockingReview);
	    if (reviewArtifact) console.log("Review evidence: " + reviewArtifact);
	  }
	  if (
	    !String(process.env.JOB_AGENT_SUBMIT_COMPLETE || "1").trim().toLowerCase().match(/^(0|false|no|off)$/) &&
	    blockingReview.length === 0 &&
	    submit &&
	    captchaResultBlocksSubmission(captchaResult)
	  ) {
	    const processingError = "captcha blocked automatic submission: " + captchaResult.status + " (" + captchaResult.detail + ")";
	    await writeEvidence(page, "submission-processing-error.txt", "processing_error", processingError);
	    console.log(SUBMISSION_PROCESSING_ERROR_LINE_PREFIX + " " + processingError);
	    return;
	  }
	  if (!submit && allFilled.length === 0 && allReview.length === 0) {
    const processingError = await detectSubmissionProcessingError(page);
    if (processingError) {
      await writeEvidence(page, "submission-processing-error.txt", "processing_error", processingError);
      console.log(SUBMISSION_PROCESSING_ERROR_LINE_PREFIX + " " + processingError);
      return;
    }
  }
  if (!String(process.env.JOB_AGENT_SUBMIT_COMPLETE || "1").trim().toLowerCase().match(/^(0|false|no|off)$/) && blockingReview.length === 0 && submit) {
    let verificationRequestedAt = Date.now();
    try {
      await waitBeforeSubmit(page);
      await clickButton(page, submit);
    } catch (e) {
      await writeEvidence(page, "submission-click-unconfirmed.txt", "submit_clicked_unconfirmed", "click failed: " + (e && e.message ? e.message : String(e)));
      console.log(SUBMIT_CLICKED_UNCONFIRMED_LINE_PREFIX + " click failed: " + (e && e.message ? e.message : String(e)));
      return;
    }
    await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
    let outcome = await waitForSubmissionOutcome(page);
    let confirmation = outcome.confirmation;
    if (confirmation) {
      await writeEvidence(page, "submission-confirmation.txt", "confirmation", confirmation);
      console.log(SUBMITTED_LINE_PREFIX + " " + confirmation);
      return;
    }
    let verification = outcome.verification;
    let processingError = outcome.processingError;
    for (let retryNumber = 1; retryNumber <= CAPTCHA_RECOVERY_ATTEMPTS && isRetryableCaptchaError(processingError); retryNumber++) {
      const retryCaptcha = await solveCaptchaIfConfigured(page);
      console.log("CapMonster CAPTCHA retry " + retryNumber + ": " + retryCaptcha.status + " (" + retryCaptcha.detail + ")");
      if (retryCaptcha.status !== "solved") {
        processingError = captchaRecoveryFailure(retryCaptcha);
        break;
      }
      const retrySubmit = await findSubmitButton(page);
      if (!retrySubmit) break;
      verificationRequestedAt = Date.now();
      await waitBeforeSubmit(page);
      await clickButton(page, retrySubmit);
      await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
      outcome = await waitForSubmissionOutcome(page);
      confirmation = outcome.confirmation;
      if (confirmation) {
        await writeEvidence(page, "submission-confirmation.txt", "confirmation", confirmation);
        console.log(SUBMITTED_LINE_PREFIX + " " + confirmation);
        return;
      }
      verification = outcome.verification;
      processingError = outcome.processingError;
      if (verification) break;
    }
    const code = await emailVerificationCode(verificationRequestedAt);
    if (code && await fillEmailVerificationCode(page, code)) {
      verification = verification || "code field found on page";
      console.log("Email verification code entered: " + verification);
      const verificationCaptcha = await solveCaptchaIfConfigured(page);
      console.log("CapMonster CAPTCHA for verification submit: " + verificationCaptcha.status + " (" + verificationCaptcha.detail + ")");
      const resubmit = await findSubmitButton(page);
      if (resubmit && ["solved", "none", "skipped"].includes(verificationCaptcha.status)) {
        await waitBeforeSubmit(page);
        await clickButton(page, resubmit);
        await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
          const resubmittedOutcome = await waitForSubmissionOutcome(page);
          const resubmittedConfirmation = resubmittedOutcome.confirmation;
          if (resubmittedConfirmation) {
            await writeEvidence(page, "submission-confirmation.txt", "confirmation", resubmittedConfirmation);
            console.log(SUBMITTED_LINE_PREFIX + " " + resubmittedConfirmation);
            return;
          }
          const processingError = resubmittedOutcome.processingError;
          if (processingError) {
            await writeEvidence(page, "submission-processing-error.txt", "processing_error", processingError);
            console.log(SUBMISSION_PROCESSING_ERROR_LINE_PREFIX + " " + processingError);
            return;
          }
          await writeEvidence(page, "submission-click-unconfirmed.txt", "submit_clicked_unconfirmed", "verification code submitted");
          console.log(SUBMIT_CLICKED_UNCONFIRMED_LINE_PREFIX + " verification code submitted");
          return;
        }
    }
    if (verification) {
      await writeEvidence(page, "email-verification-required.txt", "email_verification", verification);
      console.log(EMAIL_VERIFICATION_REQUIRED_LINE_PREFIX + " " + verification);
      return;
    }
    processingError = processingError || await detectSubmissionProcessingError(page);
    if (processingError) {
      await writeEvidence(page, "submission-processing-error.txt", "processing_error", processingError);
      console.log(SUBMISSION_PROCESSING_ERROR_LINE_PREFIX + " " + processingError);
      return;
    }
    await writeEvidence(page, "submission-click-unconfirmed.txt", "submit_clicked_unconfirmed", "clicked " + submit.text);
    console.log(SUBMIT_CLICKED_UNCONFIRMED_LINE_PREFIX + " clicked " + submit.text);
    return;
  }
  console.log("Submit gate: automatic submission not performed because blocking review fields remain or the final Submit control is unavailable.");

  } finally {
    if (browser) await browser.close();
  }
}

main().catch((error) => {
  console.error("Runtime autofill failed: " + (error && error.message ? error.message : String(error)));
  process.exit(1);
});
"""
