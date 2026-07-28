# 求职 Agent 工具清单

这个项目以 `AgentCore + ToolRegistry + ControlledExecution` 为运行基座。每个 Tool
只负责一个可审计动作；Agent Core 选择工具和参数，Policy Gate 在执行前授权，
ControlledExecution 负责调用并返回结构化结果。LLM 不能直接操作文件、数据库或浏览器。

## Agent 推理策略

| 组件 | 作用 | Tool 边界 |
| --- | --- | --- |
| `SimpleAgent` | 单轮 LLM 推理 | 不调用 Tool |
| `PlanAndSolveAgent` | 生成有界计划并逐步求解 | 显式 Tool Plan 交给 Agent Core |
| `ReActAgent` | Thought -> Action -> Observation 循环 | Action 必须转换为 `ToolCall` 并经过策略门 |
| `ReflectionAgent` | 在限定轮数内评审和改进结果 | 反思过程不产生隐式环境副作用 |
| `ToolChain` | 顺序组合有依赖关系的 Tool | 每步经过 `ControlledExecution` |
| `AsyncToolExecutor` | 并行调用独立只读 Tool | 只允许 `OBSERVE` 和 `READ` |
| `ConversationManager` | 历史、分支和显式持久化 | 不替代档案或长期事实库 |

`JobApplicationAgent.create_reasoning_strategy()` 可创建共享当前安全门和执行器的策略实例。

## 感知层：Sensors / Perception

| Tool | 作用 | 来源与边界 |
| --- | --- | --- |
| `ManualJDImportTool` | 导入用户粘贴的 JD | 本地输入；不联网 |
| `RSSJobSourceTool` | 读取 RSS/Atom 岗位 feed | 公开 feed；保留来源 |
| `GreenhouseJobSourceTool` | 读取 Greenhouse public Job Board API | 公开 board endpoint |
| `LeverJobSourceTool` | 读取 Lever public postings API | 公开 postings endpoint |
| `AshbyJobSourceTool` | 读取 Ashby public Job Board API | 公开 board endpoint |
| `RemotiveJobSourceTool` | 读取 Remotive public Remote Jobs API | 公开 API |
| `FormSnapshotScriptTool` | 读取 ATS 页面表单元数据 | Playwright；只读 DOM，不填写、不上传、不提交 |
| `ResumeIndexerTool` | 索引本地原始 PDF 简历 | 只读 `RESUME_SOURCE_DIR`，不修改文件 |

项目不包含 LinkedIn 未授权抓取或 LinkedIn 自动投递。LinkedIn 岗位只能通过用户提供的链接/JD，或经授权的官方接口接入。

## 思考与规划层：Thought / Planning

| Tool | 作用 |
| --- | --- |
| `JDParserTool` | 将 JD 解析为职位、公司、地点、技能、职责与风险 |
| `FitScorerTool` | 根据候选人资料和偏好计算岗位匹配度 |
| `ResumeSelectorTool` | 从本地原始 PDF 中选择最匹配的一份并保持原文件不变 |
| `SensitiveFieldDetectorTool` | 标记身份、工签、薪资、人口统计、法律声明等敏感字段 |

LLM 可以帮助解析、排序和回答未知的非敏感筛选问题，但不能新增工作经历、教育、数字成果、身份信息或授权状态。当前真实投递链路不生成或改写简历，只选择并原样上传已有 PDF。

## 执行层：Actuators

| Tool | 作用 | 提交策略 |
| --- | --- | --- |
| `FormFillerTool` | 将已批准 profile facts 映射到低风险表单字段 | 未批准敏感字段形成阻塞项 |
| `FormFillScriptTool` | 生成 Playwright 填表和简历上传脚本 | 静态脚本不提交；live runtime 无阻塞项时自动提交 |
| `ApplicationPackageTool` | 生成 review packet、PDF 选择证据、表单脚本和清单 | 供执行前审阅 |
| `ApplicationTrackerTool` | 写入 SQLite 岗位和申请状态 | 记录来源、材料、状态和审计信息 |
| `SubmitGateTool` | 统一控制最终投递边界 | 必填项真实完成且无阻塞项时使用 `automatic_submission_enabled` |
| `prepare_application_cohort` | 导入、筛选、去重并准备一个有界 cohort | Pipeline Agent Core 的生产入口 |
| `runtime_package_builder` | 选择批准 PDF 并生成受保护运行文件 | 延续同一岗位上一 Observation |
| `runtime_package_inspect` / `resume_provenance_inspect` | 并发读取运行包和简历来源 | 从同一 Observation 分支，Join 后才能继续 |
| `browser_execute` | 持有一个生产 Python Playwright 会话并返回终态 | 外层 `WRITE` 动作经过职业 Policy Gate |
| `ats_observe_page` | 将实时 ATS 字段结构返回原 Agent Core | `OBSERVE`；不保存页面正文或字段值 |
| `ats_fill_fields` | 执行一次有界字段填写或自愈 | `WRITE`；只回传计数和阻塞摘要 |
| `ats_advance_page` / `ats_stop_page_navigation` | 由 Core 选择继续下一页或停止 | 两候选计划只执行一项 |
| `ats_submit_application` / `ats_stop_before_submit` | 由 Core 选择最终提交或停止 | Submit 候选仍需事实、敏感字段、简历和确认门 |
| `terminal_outcome_router` | 消费浏览器 ToolResult 并决定完成/恢复分流 | 只读，不产生第二次提交 |
| `job_application_recovery` | 执行一个有界 Recovery Action | 每动作独立 AgentRound |
| `codex_repair_agent` | 启动一个隔离、脱敏、离线验证的修复周期 | `REPAIR` 副作用，不接触真实网站 |

## 示例 Tool

`CalculatorTool` 和 `SearchTool` 用于验证通用 Agent 策略、参数解析和策略控制，不在
`JobApplicationAgent` 默认注册表中。前者使用受限 AST 运算，后者访问公开搜索端点；
经 ReAct、ToolChain 或异步执行器调用时仍必须经过 Policy Gate。

“自动投递”在本项目中包含完整闭环：Agent 自动打开页面、填写低风险字段、上传用户批准的简历，并在所有必填项已真实解决且无阻塞复核项时自动点击最终 Submit。遇到未解决项时，运行时先重复识别动态字段、复用 profile 中已有的明确敏感事实、调用已配置的 CAPTCHA 求解器、等待并填写邮箱验证码、重试提交并轮询确认结果；只有穷尽这些路径后才记录 blocked、verification-required、processing-error 或 clicked-unconfirmed 状态。`JOB_AGENT_SUBMIT_COMPLETE=0` 可显式关闭自动提交。

每日生产路径使用 Python Playwright，并把每次实时 `ats_*` 动作交还同一
`JobApplicationAgent` 的 ToolRegistry 和 ControlledExecution。生成的 Node Playwright、
Chrome 连接和静态表单脚本只属于兼容、fixture 或独立诊断路径，不能代表每日统一 Core 已执行。

## 本地简历接入

在本地 `.env` 中设置私有 PDF 简历目录：

```bash
RESUME_SOURCE_DIR=/absolute/path/to/pdf-resumes
```

公开仓库不会保存这个目录里的 PDF。每日 SOP 会在执行前检查目录、文件类型、大小和上传来源；需要手动索引时运行 `job-agent resumes index "$RESUME_SOURCE_DIR"`。

## 推荐调用顺序

```text
Job Source -> JDParser -> FitScorer -> ResumeSelector
           -> CandidateScreening -> ApplicationPackage
           -> RuntimeFormInspection -> SensitiveFieldDetector -> FormFiller
           -> Blocking Review Check -> SubmitGate -> Automatic Submit -> ApplicationTracker
```

每日实际调用顺序以 `docs/DAILY_APPLICATION_SOP.md` 为准。层间契约、策略门和修复反馈路径见
`docs/architecture/runtime-agent-architecture.md`。

`examples verify-offline` 除了虚构 manifest 和 execution audit，还会运行只读 evaluator，
生成 `evaluation-metrics.json` 与 `agent-runtime-trace.json`，用于离线检查同一
`agent_runtime_id` 的 prepare/execute handoff；它不会访问真实岗位网站。
