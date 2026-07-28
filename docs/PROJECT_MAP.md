# Project Map

这份地图用于“按问题加载上下文”，避免每个 Agent 都从头阅读数千行 CLI 和浏览器运行时代码。

## 运行主链

```text
ops/daily.local.json
  -> Pipeline Agent Core: prepare_application_cohort Tool
  -> Environment adapters: source_config.py / jobs.py
  -> Per-application Agent Core: core/runtime.py::run_loop / job_application_agent.py
       -> AgentThought -> ToolCall -> ToolResult -> new Observation -> MemoryUpdate
       <- Short-term Memory: core/memory.py
       <- Long-term Memory: job_agent/memory.py / db.py / profile_vector_store.py
  -> Policy Gate: career/policies.py
  -> Execution Agent Core:
       -> parallel runtime_package_inspect + resume_provenance_inspect
       -> concurrent_read_join -> browser_execute
       -> ats_observe_page -> ats_fill_fields
       -> ats_advance_page | ats_stop_page_navigation
       -> ats_submit_application | ats_stop_before_submit
       -> terminal_outcome_router
  -> Execution: core/execution.py / job_agent/execution.py
  -> Tool Use: tools/builtin/career / runtime_filler.py / python_runtime.py
  -> execution-audit.json + job-agent.db
  -> Recovery Agent Core: recovery_executor.py -> recovery-execution.json
  -> Repair Agent Core: codex_repair_agent -> isolated verification
  -> Agent Evaluation: career/evaluation.py -> evaluation-metrics.json
  -> agent-runtime-trace.json + applications/*/agent-trajectory.json
  -> daily_sop.py
       -> 增量审计 -> Policy Gate -> repair_orchestrator.py (隔离异步 repair)
       -> recover 命令 -> historical audit -> recovery-execution.json
       -> repair 命令 -> current-audit rebuilt request -> verified single-job retry
          -> --refresh-request-only 仅刷新历史 request，不启动 Codex
       -> APPLICATION_LEDGER.csv + RUN_SUMMARY.md
```

当前架构契约、依赖方向和模块映射见
`docs/architecture/runtime-agent-architecture.md`。

## 目录边界

| 路径 | 性质 | 默认是否读取 |
| --- | --- | --- |
| `src/job_agent/` | 当前产品实现 | 按问题读取 |
| `src/hello_agents/` | Agent Core、策略执行契约和 career tools | 只在 Agent/tool 接口问题时读取 |
| `tests/` | 当前行为契约 | 与目标源码一起读取 |
| `ops/` | 每日 SOP 配置和任务模板 | 每日执行必读 |
| `docs/` | 当前说明、工具和架构 | 按索引读取 |
| `profiles/` | 私密候选人事实、答案和向量库 | 只验证结构，不在回复中泄露内容 |
| `examples/` | 虚构离线测试数据 | 不得用于真实投递 |
| `output/` | 可删除重建的运行产物 | 只读最新运行或指定事故 |

## 模块所有权

| 问题 | 首先读取 | 对应测试 |
| --- | --- | --- |
| Agent 分层、结构化契约和闭环状态机 | `hello_agents/core/contracts.py`, `perception.py`, `memory.py`, `runtime.py::run_loop` | `test_agent_architecture.py`, `test_hello_agents_career.py` |
| 脱敏轨迹、跨阶段 Observation handoff 和连续性 | `hello_agents/core/trace.py`, `job_agent/cli.py`, `execution.py`, `daily_sop.py` | `test_execution.py`, `test_cli.py`, `test_daily_sop.py` |
| Simple/ReAct/Plan-and-Solve/Reflection、会话和 Tool 编排 | `hello_agents/agents/`, `core/conversation*.py`, `tools/chain.py`, `async_executor.py` | `test_hello_agents_base.py` |
| 每轮 Agent 考核、指标、阈值和历史 | `hello_agents/core/contracts.py`, `core/runtime.py`, `career/evaluation.py`, `daily_sop.py` | `test_evaluation.py`, `test_agent_architecture.py`, `test_daily_sop.py` |
| 反垃圾、CAPTCHA、邮箱、账户、缺失事实和未确认提交恢复 | `hello_agents/career/recovery.py`, `job_agent/recovery_executor.py`, `job_agent/execution.py`, `daily_sop.py` | `test_recovery.py`, `test_recovery_executor.py`, `test_execution.py`, `test_daily_sop.py` |
| 职业策略门和 Tool 副作用 | `hello_agents/career/policies.py`, `core/execution.py`, `tools/base.py` | `test_agent_architecture.py` |
| SQLite Agent 历史记忆 | `job_agent/memory.py` | `test_agent_architecture.py` |
| 每日编排、执行日统计、连续目标、阶段状态和日报 | `daily_sop.py` | `test_daily_sop.py` |
| 隔离 coding repair、custom provider 投影、认证预检、增量启动、验证、提升和 scoped retry | `repair_orchestrator.py`, `daily_sop.py` | `test_repair_orchestrator.py`, `test_daily_sop.py` |
| 每日 `check/prepare/execute/recover/repair/report/run` 命令路由 | `daily_sop.py` 的 `main()` 附近 | `test_daily_sop.py` |
| 产品 CLI 命令参数和路由 | `cli.py` 的目标 command 附近 | `test_cli.py`, `test_cli_llm.py` |
| 环境变量和基础配置 | `config.py` | `test_config.py` |
| 岗位源配置 | `source_config.py` | `test_source_config.py` |
| API/RSS 导入与去重 | `jobs.py` | `test_jobs.py` |
| JD 解析、匹配分数、候选人筛选 | `jd_analysis.py`, `scoring.py`, `candidate_screening.py`, `shortlist.py` | 同名测试 |
| PDF 简历索引和选择 | `resumes.py` | `test_resumes.py`, `test_pdf_resume_upload.py` |
| 档案、答案库、向量检索 | `profile.py`, `application_answers.py`, `sensitive_kb.py`, `profile_vector_store.py` | 同名测试 |
| 字段语义和表单计划 | `field_semantics.py`, `forms.py` | `test_field_semantics.py`, `test_forms.py` |
| LLM 非敏感答案 | `llm_answer_resolver.py` | `test_llm_answer_resolver.py` |
| 浏览器脚本生成 | `runtime_filler.py` | `test_runtime_filler.py` |
| Python 浏览器运行时 | `python_runtime.py` | `test_python_runtime*.py` |
| ATS 特例 | `ats_adapters.py` | `test_ats_adapters.py` |
| Chrome 连接 | `chrome_runtime.py` | 相关 runtime/CLI 测试 |
| 执行、策略预检、终态和审计 | `execution.py`, `career/policies.py`, `runners.py` | `test_execution.py`, `test_agent_architecture.py`, `test_runners.py` |
| Gmail 验证 | `gmail_verification.py` | `test_gmail_verification.py` |
| CAPTCHA | `capmonster.py` | `test_capmonster.py` |
| 数据库去重和投递状态 | `db.py` | `test_db.py` |
| 报告和文档导出 | `reports.py`, `document_export.py` | 同名测试 |

## 先证据、后代码

出现真实投递问题时按下面顺序定位，不要先修改浏览器逻辑：

1. 最新 `RUN_SUMMARY.md`：确认阶段和终态。
2. `pipeline-manifest.json`：确认输入数量、输出路径、简历策略和 LLM 模式。
3. `resume-preflight*.json`：确认实际 PDF 路径、大小和哈希。
4. `execution-audit*.json`：确认结构化状态，不依赖控制台印象。
5. `agent-runtime-trace.json`：确认闭环阶段和 prepare/execute handoff 连续。
6. 对应包下的 `agent-trajectory.json`、`review-required.txt` 或确认页面证据。
7. 再根据上表进入一个目标模块和它的测试。

## 变更边界

- 每个修复先锁定一个失败状态和一条最短复现路径。
- 共用字段解析或共用终态变更需要扩大测试范围；单一 ATS 适配器问题保持在适配器边界。
- 不把候选人专属事实硬编码进运行时代码。个人事实进入 `profiles/` 或 sensitive KB。
- 不把临时来源文件名、当天日期或某次输出目录写进产品逻辑。
- 自动修复输入只能来自已脱敏的审计字段标签、原因和重复字段，不得把真实档案、敏感答案、
  简历、数据库或本地配置复制进 repair workspace。
