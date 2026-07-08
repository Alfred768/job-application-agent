# 基于 HelloAgents 的求职申请 Agent 设计

日期：2026-07-08
基座：`hello-agents`
依据：`docs/superpowers/specs/2026-07-08-job-application-agent-peas-design.md`

## 0. Upstream 基座来源

本项目实际拉取并参考的公开仓库：

- `https://github.com/datawhalechina/hello-agents`

该 upstream 仓库的根目录主要是教程、章节材料和共创项目集合；可直接复用的 `hello_agents` Python 框架包位于：

```text
Co-creation-projects/lcyting-StockSage-agent/HelloAgents Optimized/hello_agents
```

当前项目已将这个可复用框架包引入到：

```text
src/hello_agents
```

并在此基础上新增：

- `hello_agents.agents.plan_solve_agent`
- `hello_agents.agents.job_application_agent`
- `hello_agents.career.models`
- `hello_agents.tools.builtin.career.*`

因此后续实现不再以自建的散函数为中心，而是以 HelloAgents 的 `Agent + ToolRegistry + Tool` 为基座。

## 1. 设计目标

以 `hello-agents` 为基础框架，构建一个面向个人求职申请的 Agent。它需要完成从岗位感知、JD 分析、简历选择与定制、申请材料生成、表单填写，到最终人工确认提交前暂停的闭环。

这个 Agent 的核心目标不是“批量海投”，而是“高质量、可追踪、合规、安全的人机协同申请”。

边界：

- 不做 LinkedIn 未授权爬取。
- 不做 LinkedIn 自动投递。
- 不绕过 ATS 或招聘网站限制。
- 浏览器表单可以自动填写，但普通网页最终 Submit 必须人工确认。
- 所有简历与表单答案必须基于真实用户资料，不编造经历。

## 2. PEAS 到 HelloAgents 的映射

| PEAS 元素 | 求职 Agent 含义 | HelloAgents 对应层 |
| --- | --- | --- |
| Performance | 匹配度、简历相关性、真实性、合规性、完成度、可追踪性 | `agents/job_application_agent.py` 的目标函数与评估策略 |
| Environment | Job API/RSS、ATS 页面、本地简历库、LLM API、SQLite 状态库、用户确认 | `tools/builtin/career/*` + `core/message.py` |
| Actuators | 搜索岗位、解析 JD、评分、选简历、生成材料、填写表单、上传文件、记录状态 | `tools` 层的 Tool 实现 |
| Sensors | 岗位源、JD 文本、简历文本、DOM 表单字段、历史申请记录、用户反馈 | `tools` 返回的 Observation message |

对应到你给的 Agent 图：

```text
Environment
  -> Sensors / Perception
  -> Thought
  -> Planning
  -> Tool Selection
  -> Actuators
  -> Environment State Change
```

求职 Agent 的运行循环：

1. Perception：读取岗位、JD、简历、申请历史、页面表单。
2. Thought：判断岗位是否值得申请，缺哪些关键词，是否有合规风险。
3. Planning：生成申请计划，例如“选 Agent Engineer 简历 -> 改 Summary -> 生成 review packet -> 打开 ATS”。
4. Tool Selection：调用具体工具，例如 `JDParserTool`、`ResumeSelectorTool`、`FormFillTool`。
5. State Change：保存 application record，生成文件，填写表单，等待用户确认。

## 3. 推荐目录结构

在原始 `hello-agents` 结构上增加 career 领域工具和求职 Agent 实现：

```text
hello-agents/
├── hello_agents/
│   ├── core/
│   │   ├── agent.py
│   │   ├── llm.py
│   │   ├── message.py
│   │   ├── config.py
│   │   └── exceptions.py
│   │
│   ├── agents/
│   │   ├── simple_agent.py
│   │   ├── react_agent.py
│   │   ├── reflection_agent.py
│   │   ├── plan_solve_agent.py
│   │   └── job_application_agent.py
│   │
│   ├── tools/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── chain.py
│   │   ├── async_executor.py
│   │   └── builtin/
│   │       ├── calculator.py
│   │       ├── search.py
│   │       └── career/
│   │           ├── job_sources.py
│   │           ├── jd_parser.py
│   │           ├── fit_scorer.py
│   │           ├── resume_indexer.py
│   │           ├── resume_tailor.py
│   │           ├── document_exporter.py
│   │           ├── form_inspector.py
│   │           ├── form_filler.py
│   │           ├── application_tracker.py
│   │           └── compliance.py
│   │
│   └── career/
│       ├── models.py
│       ├── prompts.py
│       ├── policies.py
│       └── storage.py
```

说明：

- `core/`：保持通用框架，不放求职业务逻辑。
- `agents/`：放不同 Agent 推理模式，求职 Agent 是一个业务 Agent。
- `tools/`：所有能改变外部状态的动作都必须通过 Tool。
- `tools/builtin/career/`：求职领域工具集。
- `career/`：领域模型、prompt、策略、存储适配，不直接执行工具动作。

## 4. Core 层设计

### 4.1 `core/agent.py`

定义 Agent 基类：

```python
class Agent:
    def __init__(self, llm, tools, config):
        self.llm = llm
        self.tools = tools
        self.config = config

    def run(self, task: str) -> AgentResult:
        raise NotImplementedError
```

对求职 Agent 的要求：

- `run()` 接收自然语言任务，例如“帮我申请这个岗位”。
- 基类只负责生命周期，不负责 JD 解析、简历修改或表单填写。
- 每次工具调用必须产生 message 记录，方便审计。

### 4.2 `core/llm.py`

统一 LLM 接口：

```python
class HelloAgentsLLM:
    def complete(self, messages, response_schema=None) -> LLMResponse:
        ...
```

求职 Agent 使用 LLM 的地方：

- JD 结构化解析。
- 简历改写计划。
- Cover letter 生成。
- Screening question 草稿。
- 合规/真实性自检。

不应该让 LLM 直接：

- 操作浏览器。
- 写入数据库。
- 点击 Submit。
- 直接改原始简历文件。

### 4.3 `core/message.py`

建议消息类型：

```python
class MessageRole:
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    OBSERVATION = "observation"
    SAFETY_GATE = "safety_gate"
```

求职 Agent 需要额外记录：

- `JobObservation`：岗位/JD 感知结果。
- `ResumeObservation`：简历模板与文本。
- `FormObservation`：ATS 表单结构。
- `SafetyGateMessage`：合规、真实性、敏感字段、提交确认等闸门。

### 4.4 `core/config.py`

求职 Agent 配置项：

```text
OPENAI_API_KEY
LLM_PROVIDER
LLM_MODEL_ID
LLM_BASE_URL
RESUME_SOURCE_DIR
OUTPUT_DIR
DATABASE_PATH
JOB_SOURCE_CONFIG_PATH
BROWSER_HEADLESS
AUTO_SUBMIT_ALLOWLIST
```

其中 `AUTO_SUBMIT_ALLOWLIST` 默认必须为空。只有明确允许自动提交的官方 API/ATS adapter 才能加入。CLI 默认使用 deterministic mode；只有用户显式传入 `--use-llm` 时，才通过 `HelloAgentsLLM` 使用 `OPENAI_API_KEY` / `LLM_*` 配置调用 OpenAI-compatible API。

`--use-llm` 当前用于生成 `LLM Review Notes`，属于 Thought / Planning 的辅助审阅层。它不能覆盖 `TruthfulnessCheckTool`、`SensitiveFieldDetectorTool` 或 `SubmitGateTool` 的安全结论。

### 4.5 `core/exceptions.py`

建议异常体系：

```python
class HelloAgentsError(Exception): ...
class ToolExecutionError(HelloAgentsError): ...
class SafetyGateError(HelloAgentsError): ...
class ComplianceBlockedError(SafetyGateError): ...
class TruthfulnessCheckError(SafetyGateError): ...
class HumanApprovalRequired(SafetyGateError): ...
class MissingProfileFactError(SafetyGateError): ...
```

求职 Agent 中，以下情况必须抛出或返回 safety gate：

- 试图自动点击普通网页 Submit。
- 试图抓取禁止自动访问的网站。
- 生成内容包含未证实经历。
- 表单需要敏感信息但没有用户确认答案。

## 5. Agents 层设计

### 5.1 为什么使用 Plan-and-Solve + ReAct 混合模式

求职申请不是单步任务，它包含：

- 感知外部环境。
- 分析岗位。
- 多工具调用。
- 生成文件。
- 浏览器表单填写。
- 中途人工确认。

因此建议 `JobApplicationAgent` 以 `PlanAndSolveAgent` 为主，局部工具执行采用 ReAct loop。

```text
Plan-and-Solve:
  先生成整体申请计划

ReAct:
  每一步根据 observation 选择下一个 tool
```

### 5.2 `agents/job_application_agent.py`

核心职责：

- 接收用户目标。
- 调用岗位感知工具。
- 调用 JD 解析与评分工具。
- 决定是否值得申请。
- 选择简历模板。
- 生成简历改写计划。
- 生成 review packet。
- 启动表单填写计划。
- 在 Submit 前停止。

推荐状态：

```python
class JobApplicationState:
    task_id: str
    job: JobPosting | None
    jd_analysis: JDAnalysis | None
    fit_score: FitScore | None
    selected_resume: ResumeTemplate | None
    resume_edit_plan: ResumeEditPlan | None
    generated_documents: list[GeneratedDocument]
    form_plan: FormFillPlan | None
    safety_gates: list[SafetyGate]
    status: ApplicationStatus
```

推荐主流程：

```text
1. perceive_job()
2. analyze_jd()
3. score_fit()
4. plan_application()
5. prepare_documents()
6. prepare_form_fill()
7. pause_for_human_review()
8. record_outcome()
```

### 5.3 Agent 人格 Prompt

系统人格：

```text
You are a careful personal career operations agent.
Your job is to help the user apply to relevant roles with high quality,
truthful materials, clear tracking, and strict compliance boundaries.
You optimize for fit, accuracy, and user control, not application volume.
You never invent user experience.
You never submit an ordinary browser application without explicit user confirmation.
```

中文解释：

- 野心：主动找好岗位。
- 谨慎：敏感信息必须确认。
- 真实：不编经历。
- 合规：不绕过平台规则。
- 可解释：每个决定都给理由。

## 6. Tools 层设计

### 6.1 Tool 基类

`tools/base.py` 应统一所有工具接口：

```python
class Tool:
    name: str
    description: str
    input_schema: type
    output_schema: type

    def run(self, input_data):
        raise NotImplementedError
```

每个 Tool 必须：

- 明确输入输出 schema。
- 不读取未声明的全局状态。
- 返回结构化结果。
- 失败时返回可审计错误。
- 对会改变环境的动作写入 tracker。

### 6.2 Tool Registry

`tools/registry.py` 负责注册和查找工具：

```python
registry.register(JobSearchTool())
registry.register(JDParserTool())
registry.register(FitScorerTool())
registry.register(ResumeIndexerTool())
registry.register(ResumeSelectorTool())
registry.register(ResumeTailorTool())
registry.register(DocumentExporterTool())
registry.register(FormInspectorTool())
registry.register(FormFillerTool())
registry.register(ApplicationTrackerTool())
registry.register(ComplianceGateTool())
```

求职 Agent 不应该直接 import 每个工具，而应该通过 registry 按能力选择。

### 6.3 Tool Chain

`tools/chain.py` 管理固定流程：

```text
JDReviewChain:
  ImportJobTool -> JDParserTool -> FitScorerTool -> ResumeSelectorTool -> ReviewPacketTool

ResumePreparationChain:
  ResumeIndexerTool -> ResumeSelectorTool -> ResumeTailorTool -> DocumentExporterTool -> TruthfulnessCheckTool

ApplicationFormChain:
  FormInspectorTool -> SensitiveFieldDetectorTool -> FormFillerTool -> SubmitGateTool
```

### 6.4 Async Executor

`tools/async_executor.py` 用于并发做安全的只读任务：

- 同时拉取多个 Job API。
- 同时解析多个 JD。
- 同时生成多个岗位 review packet。

不建议异步执行：

- 最终表单填写。
- 文件覆盖写入。
- 数据库状态切换。
- 任何 Submit 动作。

## 7. Career Builtin Tools 设计

### 7.1 `job_sources.py`

工具：

- `ManualJDImportTool`
- `RSSJobSourceTool`
- `GreenhouseJobSourceTool`
- `LeverJobSourceTool`
- `RemotiveJobSourceTool`

当前已实现：

- `ManualJDImportTool`：接收用户提供的 JD 文本，标准化为 `Job`。
- `RSSJobSourceTool`：接收公开 RSS/Atom XML 或 URL，标准化为 `Job` 列表，并保留 `source_url` / `apply_url` 作为出处和后续申请入口。
- `GreenhouseJobSourceTool`：接收 Greenhouse Job Board API 的公开 jobs JSON，标准化为 `Job` 列表。
- `LeverJobSourceTool`：接收 Lever Postings API 的公开 postings JSON，标准化为 `Job` 列表。
- `RemotiveJobSourceTool`：接收 Remotive Remote Jobs API 的公开 jobs JSON，标准化为 `Job` 列表，并保留 Remotive 回链。

这些 job source tools 属于 Sensors / Perception 层的合规岗位感知工具。它们只读取公开 feed/API，不做 LinkedIn scraping，也不绕过招聘网站访问规则。

当前 CLI 已提供两条公开 feed 路径：

- `job-agent jobs import-rss`：只把 feed 标准化为 JSON 岗位池。
- `job-agent jobs import-greenhouse`：把 Greenhouse 公开 Job Board API 响应标准化为 JSON 岗位池。
- `job-agent jobs import-lever`：把 Lever 公开 Postings API 响应标准化为 JSON 岗位池。
- `job-agent jobs import-remotive`：把 Remotive 公开 Remote Jobs API 响应标准化为 JSON 岗位池。
- `job-agent jobs import-sources`：从 `sources.json` 合并多个公开 source，生成统一 JSON 岗位池。
- `job-agent jobs shortlist`：对标准化岗位池执行 fit scoring，按分数降序输出短名单，保留 `applications prepare` 所需的标准岗位字段。
- `job-agent jobs review-rss`：把 feed 中每个岗位转换为 JD 文本，交给 `JobApplicationAgent` 生成 review packet。
- `job-agent jobs review-greenhouse`：把 Greenhouse 岗位列表交给 `JobApplicationAgent` 批量生成 review packet。
- `job-agent jobs review-lever`：把 Lever 岗位列表交给 `JobApplicationAgent` 批量生成 review packet。
- `job-agent jobs review-remotive`：把 Remotive 岗位列表交给 `JobApplicationAgent` 批量生成 review packet。
- `job-agent jobs review-sources`：从 `sources.json` 获取多个来源岗位，并批量交给 `JobApplicationAgent` 生成 review packet。
- `job-agent applications prepare`：从标准化 `jobs.json` 中选择一个岗位，生成 application package，并在提供表单 snapshot/profile 时生成 guarded form-fill script。
- `job-agent applications prepare-shortlist`：从短名单或标准化岗位 JSON 批量生成 application package，每个岗位一个目录，并输出 `batch-summary.json` 作为审计索引。
- `job-agent applications build-batch-runner`：从 `batch-summary.json` 生成 guarded Playwright runner，顺序执行每个 `fill-form.js`，但不生成任何最终提交动作。

输入：

```python
class JobSearchInput:
    query: str
    location: str | None
    remote: bool | None
    limit: int
```

输出：

```python
class JobPosting:
    title: str
    company: str
    location: str | None
    source: str
    source_url: str | None
    apply_url: str | None
    raw_jd: str
```

### 7.2 `jd_parser.py`

工具：

- `JDParserTool`

职责：

- 抽取 required skills。
- 抽取 preferred skills。
- 抽取职责。
- 判断 seniority。
- 判断 role family。
- 标记 location/work authorization 风险。

LLM 输出必须是结构化 JSON，不允许散文输出直接进入状态库。

### 7.3 `fit_scorer.py`

工具：

- `FitScorerTool`

评分维度：

- role family match
- skill match
- seniority match
- location/remote match
- evidence strength
- application complexity
- compliance risk

输出：

```python
class FitScore:
    score: int
    role_track: str
    reasons: list[str]
    matched_skills: list[str]
    missing_keywords: list[str]
    risks: list[str]
    recommendation: Literal["prepare", "review", "skip"]
```

### 7.4 `resume_indexer.py`

工具：

- `ResumeIndexerTool`
- `ResumeSelectorTool`

职责：

- 扫描 `RESUME_SOURCE_DIR`。
- 从可解析 DOCX/PDF 中提取 `parsed_text`，作为后续 tailored resume draft 的基础文本。
- 识别七类模板：
  - Agent Engineer
  - SDE
  - MLE
  - ML Infra
  - AI Algorithm Engineer
  - Data Scientist
  - Unity ML Infrastructure
- 将模板与岗位 role track 匹配。

### 7.5 `resume_tailor.py`

工具：

- `ResumeTailorTool`
- `ResumeDraftTool`
- `TruthfulnessCheckTool`

允许修改：

- Summary。
- Skills 顺序。
- 少量 bullet 的关键词表达。
- 项目展示顺序。

禁止修改：

- 学历。
- 公司。
- 日期。
- 论文状态。
- 未证实技能。
- 未证实业务指标。

当前已实现：

- `ResumeTailorTool`：生成 auditable edit plan。
- `ResumeDraftTool`：基于 base resume text 生成 `tailored-resume.md` 草稿，只插入 supported JD keywords，并把 unsupported keywords 放入 review-required 区域。
- `applications prepare` / `prepare-shortlist`：当用户没有显式传入 `--resume` 时，使用 `--resume-source-dir` 中与 JD role track 匹配且带 `parsed_text` 的模板生成 `tailored-resume.md`。
- `TruthfulnessCheckTool`：阻止 unsupported keywords 被当作事实经历写入。

输出：

```python
class ResumeEditPlan:
    base_template_id: str
    target_job_id: str
    summary_edits: list[TextEdit]
    skill_order: list[str]
    bullet_edits: list[TextEdit]
    truthfulness_risks: list[str]
```

### 7.6 `document_exporter.py`

工具：

- `ApplicationPackageTool`
- `DocxRenderTool`
- `PDFExportTool`
- `ResumeQualityCheckTool`

职责：

- 当前已实现：生成本地 application package，包含 `review.md`、`jd-analysis.json`、`resume-edit-plan.json`、`submit-gate.txt`。
- 从模板生成定制 DOCX。
- 导出 PDF。
- 检查页数是否为 1 页。
- 检查是否存在明显空文件、缺失文本、渲染失败。

阶段边界：

- 当前 package exporter 只生成安全的 review artifacts，不直接改写原始简历。
- DOCX/PDF 真正改写属于下一阶段，需要在 `TruthfulnessCheckTool` 通过或用户确认后执行。

### 7.7 `form_inspector.py`

工具：

- `FormInspectorTool`
- `FormSnapshotScriptTool`
- `SensitiveFieldDetectorTool`
- 当前已实现：从 JSON form snapshot 归一化字段，并识别敏感字段。
- 当前已实现：生成 guarded Playwright snapshot script，只读取 ATS 表单字段 metadata，不填写、不上传、不提交。

职责：

- 读取 ATS 表单 DOM。
- 识别 label、placeholder、required、select options、radio groups。
- 标记敏感字段。

敏感字段：

- sponsorship
- work authorization
- EEO/demographic
- disability
- veteran status
- salary
- relocation
- start date
- legal attestation

### 7.8 `form_filler.py`

工具：

- `FormFillerTool`
- `FormFillScriptTool`
- `FileUploadTool`
- `SubmitGateTool`
- 当前已实现：根据 approved profile facts 生成安全的 fill plan，并列出 review-required 字段。
- 当前已实现：生成 guarded Playwright form-fill script，只填写低风险且高置信字段，可上传 approved Resume/CV file，不生成点击提交动作。

策略：

- 低风险字段自动填。
- 敏感字段需要确认。
- 未知字段需要确认。
- 上传简历允许自动执行。
- 普通网页最终 Submit 不允许自动执行。

`FormFillScriptTool` 是执行器层的中间产物生成器。它可以把 `FormFillPlan` 转成可审计的 Playwright 脚本，自动打开申请页面、填写低风险字段，并在 Resume/CV file 字段上传用户明确提供或 agent 生成的简历文件；脚本必须把敏感字段输出为 review list，并在最终提交前停住。

`SubmitGateTool` 必须是强制工具，不能只靠 prompt：

```python
class SubmitGateTool:
    def run(self, form_state):
        return HumanApprovalRequired("Final Submit remains manual.")
```

### 7.9 `application_tracker.py`

工具：

- `ApplicationTrackerTool`

记录：

- job source
- fit score
- selected resume
- generated files
- form fill status
- user review status
- submitted/skipped/needs manual work

### 7.10 `compliance.py`

工具：

- `SourcePolicyCheckTool`
- `ComplianceGateTool`
- `RateLimitPolicyTool`

职责：

- 检查是否为允许来源。
- 阻止 LinkedIn 自动爬取/自动投递。
- 阻止绕过 bot protection。
- 阻止非白名单 auto-submit。

## 8. 消息流设计

一次完整申请任务的消息流：

```text
UserMessage:
  "帮我申请这个岗位..."

SystemMessage:
  JobApplicationAgent personality and safety policy

ToolCall:
  ManualJDImportTool

Observation:
  JobPosting(title, company, raw_jd)

ToolCall:
  JDParserTool

Observation:
  JDAnalysis(required_skills, responsibilities, seniority)

ToolCall:
  FitScorerTool

Observation:
  FitScore(score, reasons, recommendation)

AssistantMessage:
  "这个岗位适合申请，推荐使用 Agent Engineer 简历模板。"

ToolCall:
  ResumeSelectorTool

ToolCall:
  ResumeTailorTool

ToolCall:
  TruthfulnessCheckTool

SafetyGateMessage:
  "No unsupported claims detected."

ToolCall:
  FormInspectorTool

ToolCall:
  FormFillerTool

SafetyGateMessage:
  "Final Submit remains manual. Please review and submit."
```

## 9. 状态机设计

```text
NEW_JOB
  -> JD_PARSED
  -> SCORED
  -> SKIPPED
  -> MATERIALS_PLANNED
  -> MATERIALS_GENERATED
  -> FORM_INSPECTED
  -> FORM_FILLED
  -> WAITING_USER_SUBMIT
  -> SUBMITTED
  -> NEEDS_MANUAL_WORK
  -> FAILED
```

关键约束：

- `WAITING_USER_SUBMIT -> SUBMITTED` 只能由用户确认触发。
- `FAILED` 必须保存错误原因。
- `SKIPPED` 必须保存跳过理由。
- 所有状态变化写入 tracker。

## 10. 与当前 MVP 的迁移关系

当前仓库已有 `src/job_agent/*` MVP 模块。迁移到 HelloAgents 时，不需要推倒重来：

| 当前模块 | HelloAgents 目标位置 |
| --- | --- |
| `src/job_agent/config.py` | `hello_agents/core/config.py` + career config |
| `src/job_agent/models.py` | `hello_agents/career/models.py` |
| `src/job_agent/jobs.py` | `hello_agents/tools/builtin/career/job_sources.py` |
| `src/job_agent/scoring.py` | `hello_agents/tools/builtin/career/fit_scorer.py` |
| `src/job_agent/resumes.py` | `hello_agents/tools/builtin/career/resume_indexer.py` |
| `src/job_agent/reports.py` | `hello_agents/tools/builtin/career/application_tracker.py` 或 review packet tool |
| `src/job_agent/forms.py` | `hello_agents/tools/builtin/career/form_filler.py` |
| `src/job_agent/cli.py` | CLI adapter，调用 `JobApplicationAgent` |

推荐迁移步骤：

1. 先创建 `hello_agents/core` 基础框架。
2. 把当前 dataclass models 移到 `hello_agents/career/models.py`。
3. 把当前 scoring/resume/jobs/forms 封装成 Tool。
4. 实现 `ToolRegistry`。
5. 实现 `JobApplicationAgent`。
6. 让 CLI 从直接调用函数改为调用 Agent。
7. 保留所有现有测试，再新增 Agent loop 测试。

## 11. 第一版 JobApplicationAgent 的最小可用能力

MVP v1 不需要一次做完所有工具。第一版 HelloAgents 求职 Agent 应该包含：

- `ManualJDImportTool`
- `RSSJobSourceTool`
- `GreenhouseJobSourceTool`
- `LeverJobSourceTool`
- `RemotiveJobSourceTool`
- `FitScorerTool`
- `ResumeIndexerTool`
- `ResumeSelectorTool`
- `ReviewPacketTool`
- `SubmitGateTool`
- `ApplicationTrackerTool`

最小任务：

```text
用户提供 JD 文本
  -> Agent 解析岗位
  -> Agent 评分
  -> Agent 选择简历方向
  -> Agent 生成 review packet
  -> Agent 明确提示 Submit 需要人工确认
```

这会把当前 MVP 功能真正包进 HelloAgents 的 Agent/Tool/message 框架。

## 12. 后续扩展

第二阶段：

- 增加 Greenhouse/Lever source tools。
- 增加 LLM JD parser。
- 增加 resume edit plan。
- 增加 DOCX/PDF exporter。

第三阶段：

- 增加 browser form inspector。
- 增加 browser form filler。
- 增加 sensitive-field review UI。

第四阶段：

- 增加 follow-up tracker。
- 增加 email outcome ingestion。
- 增加面试准备 Agent。

## 13. 设计结论

基于 PEAS 文档，求职 Agent 最适合被设计成一个 `PlanAndSolve + ReAct` 混合 Agent：

- `PlanAndSolve` 负责整体申请路径。
- `ReAct` 负责每一步根据 observation 选择工具。
- `ToolRegistry` 保证工具调用可控。
- `SafetyGate` 保证合规、真实性和人工确认。
- `ApplicationTracker` 保证状态可追踪。

核心原则：

```text
LLM 负责思考与生成计划。
Tool 负责可审计动作。
SafetyGate 负责边界。
User 负责最终确认。
```
