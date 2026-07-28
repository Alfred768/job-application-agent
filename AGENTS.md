# Job Application Agent Operating Contract

本文件是所有新 Agent 进入项目后的第一入口。不要先通读整个仓库，也不要把历史设计文档当成当天的执行指令。

## 1. 先判断任务类型

- 每日投递：先读 `docs/DAILY_APPLICATION_SOP.md`，运行
  `.venv/bin/python scripts/daily_sop.py check`，再查看最新日报。SOP 会为每条命令隔离并
  自动回收项目临时目录；发现异常中断残留时运行
  `.venv/bin/python scripts/daily_sop.py cleanup`。
- 故障排查：先运行 `.venv/bin/python scripts/daily_sop.py report`，只读取最新
  `RUN_SUMMARY.md`、`pipeline-manifest.json`、`execution-audit*.json` 和对应测试。
  排查本身不授权重投。
- 代码修改：先读 `docs/PROJECT_MAP.md`，只打开与目标对应的模块和测试。跨会话任务要从
  `ops/CURRENT_TASK.example.md` 建立本地 `ops/CURRENT_TASK.md`，每完成一个阶段就更新。
  涉及 Agent 分层、工具调用或安全门时，再读
  `docs/architecture/runtime-agent-architecture.md`；不得让 LLM、Tool 或浏览器入口
  绕过 `Policy Gate -> Controlled Execution`。
  `JobApplicationAgent` 的工具工作流必须使用 Agent Core 的统一
  `Perception -> Thought -> Action -> Observation -> Memory Update` 闭环；每轮 Thought
  只能选择当前有界 Plan 中的 ToolCall，并且下一轮必须以上一轮的新 Observation 为输入。
  Thought 只保存可审计的决策摘要、反思和自我批判，不保存或要求隐藏推理过程。
  ReAct、ToolChain 和异步执行器都必须生成结构化 ToolCall；异步执行只允许只读副作用。
  并发分支必须共享父 Observation、记录共同 `parallel_group_id`，并由显式 Join Observation
  回到串行链；不能把某个并发分支结果直接当作全部环境状态。

## 2. 默认读取顺序

1. `AGENTS.md`
2. `docs/DAILY_APPLICATION_SOP.md` 或 `docs/PROJECT_MAP.md`
3. `output/daily/APPLICATION_LEDGER.csv`（每日投递必须读取；不存在时先运行 `check`）
4. `ops/CURRENT_TASK.md`（若存在）
5. 最新一次 `output/daily/**/RUN_SUMMARY.md`（仅每日投递或故障排查）
6. 与当前问题直接相关的源码和测试

除非任务明确需要，否则不要加载：

- 旧的 `output/live-*`：历史运行证据，只在回溯特定事故时读取。
- 整个 `src/job_agent/cli.py` 或整个 `python_runtime.py`：先通过
  `docs/PROJECT_MAP.md` 定位，再读取目标函数附近内容。

## 3. 每日投递的唯一配置与入口

- 本地唯一配置：`ops/daily.local.json`（已被 git 忽略）。
- 可提交模板：`ops/daily.example.json`。
- 唯一入口：`.venv/bin/python scripts/daily_sop.py <command>`。
- 单批准备上限：`ops/daily.local.json` 的 `limit`，当前为 100，脚本硬限制为 100。
  该值不绕过资格、去重、反垃圾、候选人事实或最终提交确认门。
- 每日确认目标：`ops/daily.local.json` 的 `daily_submit_target`，当前为 100。只有带
  `submitted_at` 的页面确认成功计数；批次阻塞或失败时继续新候选，但没有完整终态审计时
  必须停止。跨午夜运行按最新一次 `execution_attempt.finished_at` 的本地日期统计和考核，
  不按准备目录日期归属。
- 最新运行指针：`output/daily/latest.json`。
- 每次运行状态：`output/daily/YYYY-MM-DD/<run-id>/run-state.json`。
- 每次运行结论：同目录下的 `RUN_SUMMARY.md`。
- 每轮考核：同目录下的 `evaluation-metrics.json`；80% 确认提交目标以执行后未被安全跳过的
  最终合格岗位为分母，原始导入到确认提交率只作漏斗监测，不得驱动绕过资格、去重或安全门。
- 每轮考核必须通过 Agent Core 注册的 `job_application_round` evaluator，记录 evaluation
  ID、轮次、总状态、指标和建议，并生成只读 `agent_evaluation` Observation。考核器不得
  调用 LLM、Tool 或浏览器，也不得直接改变准备、提交、恢复或 repair 决策。
- 统一运行轨迹：同目录下的 `agent-runtime-trace.json` 汇总 Pipeline、真实浏览器、
  Recovery、Repair 和 Evaluation；每个申请包的 `agent-trajectory.json` 保存同一
  `agent_runtime_id` 的分阶段脱敏轨迹。`prepare` 最后的 Observation ID 必须原样成为
  `execute` 第一轮输入，其脱敏 payload 也必须相等；报告中的 continuity 不得出现
  `disconnected`。
- 空候选唤醒间隔：`ops/daily.local.json` 的 `empty_wake_minutes`；状态为
  `waiting_for_candidates` 时由外部调度在 `next_wake_at` 启动新批次，不在 Goal 内
  长时间 sleep。
- 全量投递台账：`output/daily/APPLICATION_LEDGER.csv`，由 SQLite 自动生成，记录确认投递
  时间、公司、岗位、状态、申请 URL 和 application ID。
- 手动刷新台账：`.venv/bin/python scripts/daily_sop.py ledger`。
- 项目临时根目录：`output/daily/.tmp/`；正常或异常返回时自动删除当前命令的临时目录。
- 手动清理入口：`.venv/bin/python scripts/daily_sop.py cleanup`，只清理带本项目所有权
  标记且没有活跃进程的目录。
- 受控自动修复：`ops/daily.local.json` 的 `auto_repair`。字段/运行时故障进入
  `needs_repair`，在隔离副本中修复并完成目标测试、全量测试和离线验证后，才允许提升代码
  和重试受影响岗位。全量测试使用修复启动前冻结的测试副本，不能由修复 Agent 改写。
- `execute` 默认在当前批次完整审计后继续准备新候选，直到达到每日确认目标或进入
  `waiting_for_candidates`；仅在明确需要单批诊断时使用 `execute --one-batch`。
- `check` 会用只读、无工具的远程 `codex exec` 验证隔离 Codex 的真实可用性，不能只相信
  `codex login status`。自动化优先使用单次 `CODEX_API_KEY`，未配置时可从已有
  `OPENAI_API_KEY` 注入该次 exec。选中 custom Codex provider 时，必须投影其 base URL、
  wire API 和 `env_key`，不得回退到 ChatGPT auth 或默认 OpenAI 端点；任何密钥都不得传给
  修复工作区或子 Shell。
  repair 认证/配置/网络/限流不可用只记录
  `repair_unavailable` 警告并保留 scoped request，不阻断无关岗位，也不消耗 coding repair
  周期。`repair` 必须在 readiness 前从当前完整审计重建并持久化 request，旧 request
  只作兜底；readiness 失败不得让旧窄范围继续作为当前指针。恢复认证后使用
  `scripts/daily_sop.py repair --run-dir <run>`；该命令默认不打开
  浏览器，只有显式 `--retry-verified` 才执行已验证的单岗位 retry batch。
  只刷新 request 范围时使用 `repair --refresh-request-only`；该模式不得检查 Codex
  readiness、创建 repair attempt 或消耗 cycle。
- 历史完整审计中的 Recovery Plan 使用
  `scripts/daily_sop.py recover --run-dir <run>` 回放。该命令会从当前审计重新规划并写入
  `recovery-execution.json`，默认不打开浏览器；只有显式 `--retry-verified` 且恢复证据
  完整时才执行生成的单岗位 retry batch。

不要临时拼接一条新的 `pipeline run-execute` 命令。需要改变来源、上限、分数、简历策略或
提交模式时，先改 `ops/daily.local.json`，再执行 `check`，让状态文件记录配置哈希。

## 4. 不可违背的事实与安全边界

- 真实投递只使用 `profiles/` 下的真实档案；`examples/` 仅用于离线测试。
- 简历只能从配置指定的原始 PDF 或 PDF 目录选择并原样上传，不生成或改写经历。
- 敏感、法律、授权、薪资、人口统计等答案只能来自已批准的 sensitive KB。
- LLM 只能补充未知的非敏感问题，并且只能依据候选人已保存的事实。
- 配置中的 `profile` JSON 和其中的 `answers` 是普通事实与自定义答案的权威源；
  `sensitive_kb` 是敏感、法律、身份和授权答案的权威源。`profile_vector_db` 只是由批准事实
  生成的长期检索索引，不得绕过 JSON 权威源直接加入未经批准的答案；`database` 只记录投递
  历史和终态。
- 主 Agent 的 ToolCall 必须记录真实副作用并经过 Policy Gate；Tool 自身声明的副作用高于
  Plan 声明时以 Tool 为准。真实浏览器必须作为 `browser_execute` ToolCall 执行，其
  ToolResult 形成新 Observation 后，再由 `terminal_outcome_router` 分流终态；独立 repair
  必须作为 `codex_repair_agent` ToolCall 执行。两者都不得只做一次门检查后绕开 Core。
- 生产 Python Playwright 内部的页面观察、字段填写、Next 和 Submit 必须分别作为同一
  JobApplicationAgent 的 `ats_*` ToolCall。Next/Submit 各自必须在执行与停止候选间选择；
  选择停止不得调用浏览器回调。旧 Node/Chrome 路径只可用于兼容、fixture 或独立诊断。
- 禁止 LinkedIn 抓取和 LinkedIn 自动投递。
- 是否最终提交由 `ops/daily.local.json` 的 `submit_complete` 明确决定；不要依赖记忆或
  隐含环境默认值。
- SQLite 跟踪库是去重、已投递和终态的事实源。不得为了增加数量绕过数据库记录。
- `APPLICATION_LEDGER.csv` 是 SQLite 的只读可见镜像，不得手工修改后当作去重依据，也不得
  删除或更换数据库来规避已有记录。
- 新 application 使用规范化申请 URL 作为数据库唯一键，自动忽略常见 tracking 参数；
  没有 URL 时才使用规范化公司与岗位。数据库返回已有 application ID 时必须复用。
- `email_verification_required`、`submission_blocked_by_anti_spam`、
  `submit_clicked_unconfirmed`、`submission_processing_error`、
  `candidate_account_required` 都是终态分流，不是立即重试信号。
- 未解决根因时禁止 `--retry`，也禁止新建输出目录来规避重试保护。
- 自动修复不得复制或读取 `profiles/`、私密简历、数据库、`.env` 或本地
  `ops/daily.local.json`，不得使用真实网站验证。只允许修改 `src/job_agent/`、相关测试、
  `AGENTS.md`、当前 SOP/项目地图和配置模板；越界修改或主工作区并发变化必须拒绝提升。
- 修复启动器只允许从机器级 Codex 配置投影当前选中的 model、provider、base URL、wire API
  和 `env_key` 名称；对应密钥只注入单次 `codex exec`。不得把全局 Codex 配置、认证缓存或
  密钥复制进修复工作区，隔离 Agent 的子 Shell 必须保持 `inherit=none`。
- 反垃圾、CAPTCHA、邮箱验证、账户、点击未确认、站点处理错误和缺少候选人事实都不是
  coding repair 候选，也不得进入修复后的自动重试批次。
- 上述状态必须进入 Agent Core 的结构化 Recovery Plan，而不是只报告失败：按状态执行
  tenant 冷却、受支持 CAPTCHA 单次求解、Gmail code/link、账户登录/创建/验证、候选人事实
  请求或提交结果核验。每个自动动作必须通过 Policy Gate 和 ControlledExecution，并写入
  `recovery-execution.json`；缺少已配置的外部适配器或候选人授权时保持 `pending` 或
  `waiting_for_user`，不得伪造完成。计划中的动作和证据全部满足后，只允许恢复关联岗位；
  不得重放原批次。
- 受保护终态的恢复执行必须携带 `recovery_verified=true` 和
  `retry_scope=single_application`。点击未确认在证明首次点击没有创建申请前不得再次点击。
- 禁止直接递归清空系统 `$TMPDIR`、`/tmp` 或通用 `playwright-*` 目录；其他程序可能
  正在使用。只能通过每日 SOP 的 `cleanup` 清理本项目拥有的临时目录。
- 不提交 `.env`、`profiles/`、私密简历、OAuth token、运行输出或候选人密码。

## 5. 每日阶段门

1. `check` 必须通过并刷新投递台账，任何 `ERROR` 都停止。
2. `prepare` 前先读台账；准备阶段只导入、去重、筛选和生成包，不打开浏览器。
3. 执行前检查 manifest、候选人筛选结果、应用数量和简历来源。
4. `execute` 才允许浏览器填写和配置允许的最终提交；完整批次后默认继续追每日目标，
   `--one-batch` 才明确停止。
5. `report` 是当日收尾依据。没有审计文件，不得声称完成投递。
6. `report` 后确认台账已包含本轮全部终态及已确认投递时间。
7. 命令退出时自动回收当前临时工作区；异常中断残留由下一次 SOP 或 `cleanup` 回收。

若 `prepare` 得到 `prepared=0`，本轮必须保持 `waiting_for_candidates`，写入
`next_wake_at` 后退出并交给外部调度。空候选不是完成态，不授权降低筛选、绕过去重或
在 Goal 内等待数十分钟。

字段和 CAPTCHA 的处理顺序固定为：先对可恢复字段做有界自愈；仍有 blocking review
时保存具体 `review_items` 并以字段阻塞分流；只有字段全部解决后才允许 CapMonster
处理受支持 CAPTCHA。CapMonster 不能解除 HTTP 429、服务器限流、账户封禁或替代候选人
事实。solver 失败不得记为公司级 anti-spam。明确的 CAPTCHA challenge/token 错误最多
恢复一次；`possible spam`、HTTP 429 和服务器限流即使与 CAPTCHA 元素同时存在也必须
立即终态。

批次中的每个岗位必须独立终止：字段阻塞、反垃圾、超时或普通运行时异常都要立即增量写入
`execution-audit.json` 并继续下一个岗位，不能让单个岗位占住或中断整批。

字段/运行时审计命中 combobox、可复现表单导航或通用非敏感 unmapped-field 等可修复指纹
时，隔离 repair 必须在增量审计出现后启动；但要求候选人原创内容、偏好、身份或批准事实的
字段即使原始原因为 `unmapped field`，也必须进入 `candidate_fact_resolution`，不得进入
coding repair。后续岗位继续执行；候选修复只能在浏览器批次退出且主工作区哈希未变化后
提升。阶段必须依次记录
`needs_repair -> repairing -> repair_verified`，失败则记录 `repair_failed` 或
`repair_exhausted`。`needs_repair` 不是完成态。只有 `repair_verified` 才能生成仅含该指纹
关联岗位且带 `repair_verified=true`、`retry_scope=single_application` 的 retry batch；
不得用原始整批重试，也不得把反垃圾等终态带回。repair 基础设施失败记录尝试编号但不占用
逻辑修复周期，也不得用同一个确定性 401 重复耗尽 `max_cycles`。

手动恢复 retained repair 时必须先从当前完整审计重建 fingerprints，旧 request 仅在审计
不可用或无法产生范围时兜底。若当前代码已修好且 Codex 不产生 diff，仍必须完成受影响测试、
冻结全量测试和离线验证；全部通过后记录 `already_fixed_verified`，不得记为
`repair_agent_made_no_changes` 失败。

完成的定义不是“命令返回 0”，而是：

- `submitted` 有确认记录；
- 所有其他状态都有清楚的终态分类和下一步；
- `RUN_SUMMARY.md` 已生成；
- `evaluation-metrics.json` 已生成，并如实记录样本规模、终态审计覆盖、确认提交率和未确认点击；
- `agent-runtime-trace.json` 已生成；所有实际执行岗位都有 `agent-trajectory.json`，且
  prepare/execute handoff 为 `continuous`，未执行岗位只能是 `not_executed`；
- 没有对同一终态进行盲目重试。

吞吐保护：反垃圾冷却按时间窗口和 ATS tenant 隔离；普通表单/适配器失败按配置窗口内
连续相同状态触发公司或 tenant/adapter 熔断。有限批次先覆盖不同公司，再用同公司的
其他岗位补足。两者都不得累计终身封死候选池。

## 6. 代码修改完成标准

- 修改 Agent 主循环时必须验证每轮 Observation/Thought/Action/ToolResult/MemoryUpdate 的
  ID 连续性、Policy Decision 绑定关系和新 Observation 回流，不能只验证最终字符串输出。
- 先运行受影响模块的测试，再运行完整测试。
- 涉及投递链路时，额外运行
  `.venv/bin/job-agent examples verify-offline --out-dir output/offline-verify-sop`。
- 不使用真实网站验证代码改动，除非用户明确授权一次真实投递。
- 保留用户已有的未提交改动，不重置、不清理、不覆盖无关文件。
- 如果改变命令、配置、状态或安全边界，同步更新本文件、每日 SOP、配置模板和测试。
