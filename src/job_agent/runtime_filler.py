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
6. Upload the tailored resume on Resume/CV file fields.
7. STOP before the final Submit button (Simplify never auto-submits either;
   the human clicks Submit).

The generated script is self-contained Node.js + Playwright.
"""

from __future__ import annotations

import json
from typing import Any


def render_runtime_autofill_script(
    profile: dict[str, Any],
    resume_file: str | None = None,
    application_url: str | None = None,
    max_pages: int = 12,
    headless: bool = True,
) -> str:
    """Render a generic runtime autofill script for the given profile.

    Args:
        profile: approved profile facts (name, email, phone, links, location,
            cover_letter, education, work_history, and ``answers`` bank).
        resume_file: path to the tailored resume file for Resume/CV uploads.
        application_url: application page URL to open.
        max_pages: safety cap on multi-page navigation.
        headless: run headless when True (CI/verification); False for manual use.
    """
    payload = {
        "profile": profile,
        "resumeFile": resume_file,
        "applicationUrl": application_url,
        "maxPages": max_pages,
        "headless": headless,
    }
    return _TEMPLATE.replace("__AUTOFILL_PAYLOAD__", json.dumps(payload, ensure_ascii=False))


_TEMPLATE = r"""const { chromium } = require("playwright");
const CFG = __AUTOFILL_PAYLOAD__;

const SENSITIVE = [
  "sponsor", "sponsorship", "visa", "authorization", "authorized",
  "disability", "veteran", "gender", "ethnicity", "race", "salary",
  "relocation", "start date", "legal", "attestation", "eeo", "demographic",
  "clearance", "citizen",
];

const NEXT_PATTERNS = /^\s*(next|continue|->|→|step\s|\d+\s*\/\s*\d+|forward)\b/i;
const SUBMIT_PATTERNS = /(submit|apply|send\s+application|complete\s+application|finish|submit\s+application)\b/i;

function norm(s) {
  return (s || "")
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function isSensitive(label) {
  const n = norm(label);
  return SENSITIVE.some((kw) => n.includes(norm(kw)));
}

const PLACEHOLDER_ANSWERS = new Set(["needs review", "n/a", "tbd", "na", ""]);

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

// Match a field label against the pre-filled, user-approved sensitive-answer
// knowledge base (profile.sensitive_answers). Each entry has patterns + an
// approved answer; only approved entries are used.
function matchSensitive(label) {
  const kb = (CFG.profile && CFG.profile.sensitive_answers) || {};
  const n = norm(label);
  for (const entry of Object.values(kb)) {
    if (!entry || !entry.approved || !entry.answer) continue;
    const pats = (entry.patterns || []).map(norm);
    if (pats.some((p) => p && n.includes(p))) return String(entry.answer);
  }
  return null;
}

function mapTextValue(label, profile) {
  const n = norm(label);
  if (!n) return null;
  if (n.includes("email") || n.includes("e-mail")) return profile.email || null;
  if (n.includes("phone") || n.includes("mobile") || n.includes("telephone")) return profile.phone || null;
  if (n.includes("linkedin")) return profile.linkedin || null;
  if (n.includes("github")) return profile.github || null;
  if (n.includes("portfolio")) return profile.portfolio || profile.website || null;
  if (n.includes("website") || n.includes("personal site") || n.includes("homepage")) return profile.website || profile.portfolio || null;
  if (n.includes("cover letter")) return profile.cover_letter || null;
  if (n.includes("location") || n.includes("city") || n.includes("address")) return profile.location || profile.city || null;
  if (n.includes("first name")) return profile.first_name || (profile.name ? profile.name.split(" ")[0] : null);
  if (n.includes("last name")) return profile.last_name || (profile.name ? profile.name.split(" ").slice(1).join(" ") : null);
  if (n.includes("full name") || n.includes("your name") || n.includes("name")) return profile.name || null;
  return null;
}

async function scrapeFields(page) {
  return page.evaluate(() => {
    const labelFor = (control) => {
      if (control.id) {
        const explicit = document.querySelector(`label[for="${CSS.escape(control.id)}"]`);
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
      return control.getAttribute("aria-label") || control.getAttribute("placeholder") || control.name || "";
    };
    const groupLabelFor = (control) => {
      const fs = control.closest("fieldset");
      if (fs) {
        const legend = fs.querySelector("legend");
        if (legend && legend.textContent) return legend.textContent.trim();
      }
      return control.getAttribute("aria-label") || control.getAttribute("name") || "";
    };
    const isSkippable = (t) => ["hidden", "submit", "button", "image"].includes(t);

    const out = [];
    const radiosByName = {};
    document.querySelectorAll("input, textarea, select").forEach((c) => {
      const t = (c.type || c.tagName).toLowerCase();
      if (isSkippable(t)) return;
      if (!c.offsetParent) return; // only visible controls on the current page
      if (t === "radio") {
        const name = c.name || c.id || c.value;
        if (!radiosByName[name]) {
          radiosByName[name] = {
            kind: "radiogroup", type: "radio",
            label: groupLabelFor(c), name, required: false, options: [],
          };
        }
        radiosByName[name].options.push({ id: c.id, value: c.value, label: labelFor(c) });
        if (c.required) radiosByName[name].required = true;
        return;
      }
      const tag = c.tagName.toLowerCase();
      const options = tag === "select"
        ? Array.from(c.options).map((o) => o.textContent.trim()).filter(Boolean)
        : [];
      out.push({
        kind: "single", tag, type: (c.getAttribute("type") || tag).toLowerCase(),
        label: labelFor(c), id: c.id || "", name: c.name || "",
        required: Boolean(c.required), options, value: c.value || "",
      });
    });
    Object.values(radiosByName).forEach((g) => out.push(g));
    return out;
  });
}

function fieldSelector(f) {
  return null;
} // legacy placeholder, unused

function selectorFor(f) {
  if (f.id) return '[id="' + f.id + '"]';
  if (f.name) return '[name="' + f.name + '"]';
  return null;
}

function planField(f, profile, ctx) {
  const answers = profile.answers || {};
  const label = f.label;

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

  // Radio group: map the QUESTION (legend) to an answer, then pick the option.
  if (f.kind === "radiogroup") {
    const ans = findAnswer(label, answers) || matchSensitive(label);
    if (ans == null) {
      return { action: "skip", reason: "no approved answer for screening question", sensitive: isSensitive(label) };
    }
    const want = norm(String(ans));
    const opt = f.options.find((o) => norm(o.label) === want || norm(o.value) === want);
    if (opt) return { action: "check", optionId: opt.id, optionValue: opt.value, groupName: f.name };
    return { action: "skip", reason: "no radio option matches saved answer", sensitive: isSensitive(label) };
  }

  // File upload
  if (f.type === "file") {
    const ln = norm(label);
    if ((ln.includes("resume") || ln.includes("cv")) && CFG.resumeFile) {
      return { action: "upload", value: CFG.resumeFile };
    }
    return { action: "skip", reason: "file field not resume/cv or no resume configured" };
  }
  // Select (dropdown)
  if (f.tag === "select") {
    const ans = findAnswer(label, answers) || matchSensitive(label);
    if (ans && f.options.some((o) => norm(o) === norm(ans))) {
      return { action: "select", value: ans };
    }
    return { action: "skip", reason: "no matching option / answer", sensitive: isSensitive(label) };
  }
  // Checkbox (e.g. consent / yes-no screening)
  if (f.type === "checkbox") {
    const ans = findAnswer(label, answers);
    if (ans != null) {
      const want = norm(String(ans));
      if (want === "yes" || want === "true" || want === "1") return { action: "check", value: String(ans) };
      return { action: "skip", reason: "saved answer is negative for checkbox" };
    }
    return { action: "skip", reason: "consent/checkbox needs explicit review", sensitive: isSensitive(label) };
  }
  // Text / email / tel / textarea
  const ans = findAnswer(label, answers) || matchSensitive(label);
  if (ans != null) {
    // A saved answer-bank / knowledge-base entry is the user's explicit
    // approval, so fill it even for sensitive screening questions.
    return { action: "fill", value: String(ans) };
  }
  const mapped = mapTextValue(label, profile);
  if (mapped) {
    if (isSensitive(label)) return { action: "skip", reason: "sensitive field needs review", sensitive: true };
    return { action: "fill", value: mapped };
  }
  return { action: "skip", reason: "unmapped field" };
}

async function applyFill(page, f, plan) {
  if (plan.action === "check" && plan.optionId) {
    await page.locator('[id="' + plan.optionId + '"]').first().check();
    return;
  }
  if (plan.action === "check" && plan.groupName && plan.optionValue) {
    // radio without id: target by name + value
    await page.locator('[name="' + plan.groupName + '"][value="' + plan.optionValue + '"]').first().check();
    return;
  }
  const sel = selectorFor(f);
  if (plan.action === "fill") {
    if (!sel) throw new Error("no selector");
    await page.locator(sel).first().fill(plan.value);
  } else if (plan.action === "select") {
    if (!sel) throw new Error("no selector");
    await page.locator(sel).first().selectOption({ label: plan.value });
  } else if (plan.action === "upload") {
    if (!sel) throw new Error("no selector");
    await page.locator(sel).first().setInputFiles(plan.value);
  } else if (plan.action === "check") {
    if (!sel) throw new Error("no selector");
    await page.locator(sel).first().check();
  }
}

async function fillPage(page, profile) {
  const fields = await scrapeFields(page);
  const ctx = {
    hasWork: await hasSectionHeading(page, "work (experience|history)|employment"),
    hasEdu: await hasSectionHeading(page, "education|academic"),
  };
  const filled = [];
  const review = [];
  for (const f of fields) {
    const plan = planField(f, profile, ctx);
    if (plan.action === "skip") {
      // fields handled by the repeatable-section filler are not review-required
      if (plan.reason && plan.reason.indexOf("handled by repeatable") === 0) continue;
      review.push({ label: f.label, reason: plan.reason || "skipped", sensitive: !!plan.sensitive });
      continue;
    }
    try {
      await applyFill(page, f, plan);
      // Read back the actual DOM value to self-verify the fill took effect.
      let readback = null;
      try {
        if (plan.action === "fill" || plan.action === "select") {
          const sel = selectorFor(f);
          if (sel) readback = await page.locator(sel).first().inputValue();
        } else if (plan.action === "check" && plan.optionId) {
          readback = await page.locator('[id="' + plan.optionId + '"]').first().isChecked();
        } else if (plan.action === "check") {
          const sel = selectorFor(f);
          if (sel) readback = await page.locator(sel).first().isChecked();
        }
      } catch (e) { readback = "readback-error"; }
      filled.push({ label: f.label, action: plan.action, value: plan.value, readback });
    } catch (e) {
      review.push({ label: f.label, reason: "fill error: " + e.message, sensitive: !!plan.sensitive });
    }
  }
  return { filled, review };
}

async function findNextButton(page) {
  const btns = await page.evaluate(() =>
    Array.from(document.querySelectorAll("button, input[type='button'], a"))
      .filter((b) => b.offsetParent) // visible only (multi-page: hidden pages skipped)
      .map((b) => ({ text: (b.textContent || b.value || "").trim(), id: b.id, tag: b.tagName.toLowerCase(), name: b.name || "" }))
      .filter((b) => b.text)
  );
  for (const b of btns) {
    if (NEXT_PATTERNS.test(b.text) && !SUBMIT_PATTERNS.test(b.text)) {
      return b;
    }
  }
  return null;
}

async function findSubmitButton(page) {
  const btns = await page.evaluate(() =>
    Array.from(document.querySelectorAll("button, input[type='submit'], a"))
      .filter((b) => b.offsetParent) // visible only
      .map((b) => ({ text: (b.textContent || b.value || "").trim(), id: b.id, tag: b.tagName.toLowerCase() }))
      .filter((b) => b.text)
  );
  return btns.find((b) => SUBMIT_PATTERNS.test(b.text)) || null;
}

async function clickButton(page, b) {
  if (b.id) { await page.locator("#" + b.id).first().click(); return; }
  await page.getByText(b.text, { exact: false }).first().click();
}

// --- ATS provider detection (Simplify adapts per provider) ---
function detectATS(url) {
  const u = (url || "").toLowerCase();
  if (u.includes("greenhouse") || u.includes("boards-api.greenhouse")) return "greenhouse";
  if (u.includes("lever.co") || u.includes("jobs.lever")) return "lever";
  if (u.includes("ashbyhq") || u.includes("ashby")) return "ashby";
  if (u.includes("workday") || u.includes("myworkdayjobs")) return "workday";
  if (u.includes("icims")) return "icims";
  if (u.includes("taleo")) return "taleo";
  if (u.includes("smartrecruiters")) return "smartrecruiters";
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
  let last = null;
  for (const f of fields) {
    if (f.kind === "radiogroup" || f.type === "file" || f.type === "radio" || f.type === "checkbox") continue;
    const n = norm(f.label);
    if (patterns.some((p) => n.includes(norm(p)))) last = f;
  }
  if (!last) return null;
  const sel = selectorFor(last);
  if (!sel) return null;
  try {
    if (last.tag === "select") await page.locator(sel).first().selectOption({ label: String(value) });
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
    if (btn.id) await page.locator('[id="' + btn.id + '"]').first().click();
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
    const entry = entries[i];
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

const WORK_LABEL_PATTERNS = {
  title: ["job title", "position title", "role title"],
  company: ["company", "employer", "organization"],
  start_date: ["start date", "from date"],
  end_date: ["end date", "to date"],
  description: ["description", "responsibilities"],
  location: ["location", "city"],
};
const EDU_LABEL_PATTERNS = {
  school: ["school", "university", "institution", "college"],
  degree: ["degree"],
  field: ["field of study", "major"],
  start_date: ["start date", "from date"],
  end_date: ["end date", "graduation"],
  gpa: ["gpa"],
};

(async () => {
  const browser = await chromium.launch({ headless: CFG.headless });
  const page = await browser.newPage();
  if (CFG.applicationUrl) await page.goto(CFG.applicationUrl);
  const ats = detectATS(CFG.applicationUrl);

  const allFilled = [];
  const allReview = [];
  const sectionReport = [];
  let pages = 0;
  while (pages < CFG.maxPages) {
    pages++;
    const res = await fillPage(page, CFG.profile);
    allFilled.push(...res.filled);
    allReview.push(...res.review);

    // Repeatable multi-entry sections (Simplify fills work history + education).
    if (await hasSectionHeading(page, "work (experience|history)|employment")) {
      const r = await fillRepeatableSection(page, CFG.profile.work_history || [], mapWorkField, "experience", WORK_LABEL_PATTERNS);
      if (r.length) sectionReport.push({ section: "work_history", entries: r });
    }
    if (await hasSectionHeading(page, "education|academic")) {
      const r = await fillRepeatableSection(page, CFG.profile.education || [], mapEduField, "education", EDU_LABEL_PATTERNS);
      if (r.length) sectionReport.push({ section: "education", entries: r });
    }

    const next = await findNextButton(page);
    if (next) {
      try {
        await clickButton(page, next);
        await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
      } catch (e) {
        console.log("Could not advance to next page: " + e.message);
        break;
      }
      continue;
    }
    break; // reached final page (no Next button)
  }

  const submit = await findSubmitButton(page);
  console.log("=== Simplify-style autofill report ===");
  console.log("Detected ATS: " + ats);
  console.log("Pages filled: " + pages);
  console.log("Filled fields (" + allFilled.length + "):");
  allFilled.forEach((f) => {
    const rb = (f.readback === null || f.readback === undefined) ? "" : " | readback=" + JSON.stringify(f.readback);
    console.log("  - [" + f.action + "] " + f.label + (f.action === "upload" ? " -> " + f.value : "") + rb);
  });
  if (sectionReport.length) {
    console.log("Repeatable sections:");
    sectionReport.forEach((s) => {
      console.log("  - " + s.section + ":");
      s.entries.forEach((e) =>
        console.log("      entry#" + e.entry + " " + e.field + " [" + e.label + "] readback=" + JSON.stringify(e.readback))
      );
    });
  }
  console.log("Review-required (" + allReview.length + "):");
  allReview.forEach((r) => console.log("  - " + r.label + " (" + r.reason + ")"));
  console.log("Final submit button present: " + (submit ? submit.text : "none"));
  console.log("Submit gate: STOPPED before final Submit. The human clicks Submit (Simplify never auto-submits either).");

  await browser.close();
})();
"""
