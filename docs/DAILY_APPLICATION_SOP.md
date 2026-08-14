# 每日求职投递 SOP

目标：每天用同一套输入、同一组阶段门和同一种审计方式完成投递。Agent 不需要记住长命令，也不需要重新阅读整个代码库。

## 0. 一次性设置

本地配置是 `ops/daily.local.json`，它不会进入 git。首次使用时可从模板创建：

```bash
cp ops/daily.example.json ops/daily.local.json
```

必须逐项确认：

- `source_config`：当天使用的唯一岗位源配置。
- `profile`：真实候选人档案，不能指向 `examples/profile.json`。
- `sensitive_kb`：已明确批准的敏感答案库。
- `profile_vector_db`：从 profile 和 approved sensitive answers 建立的长期检索索引；它不是
  人工直接写入答案的权威源。
- `database`：持续复用的 SQLite 跟踪库，不能每天新建来绕过去重。
- `resume_source_dir` 或 `required_resume_pdf`：原始 PDF 的唯一来源。
- `limit`：每个准备批次最多生成的岗位数；当前配置为 100，脚本硬限制为 100。
  这是批次尝试上限，不是确认提交数量保证；资格、去重、反垃圾和真实答案门仍然生效。
- `daily_submit_target`：本地自然日内页面确认提交数的绝对下限；当前为 100。实际目标取
  该下限与 `ceil(本轮原始导入数 * min_confirmed_submission_rate)` 的较大值。只有数据库
  中写入 `submitted_at` 的确认成功才计数，填写完成、点击未确认、阻塞和失败均不计数。
- `empty_wake_minutes`：空候选后由外部调度启动新批次的间隔，默认 15 分钟。
- `submit_complete`：是否允许在无阻塞项时点击最终提交。
- `require_gmail_token`：是否把 Gmail OAuth token 设为执行前硬门槛。
- `evaluation`：每轮考核目标。`imported_cohort_target` 是样本规模门槛；
  `confirmation_rate_denominator` 必须为 `raw_imported`；
  `min_confirmed_submission_rate` 严格以本轮原始导入岗位为分母，默认 80%；
  `min_terminal_audit_coverage` 默认要求 100% 完整终态审计。最终合格执行岗位的确认率只作
  诊断；原始导入目标也不能作为绕过资格、去重或安全门的理由。
- `auto_repair`：受控 coding repair 策略。`enabled` 决定是否自动启动隔离修复，
  `max_cycles` 限制同一修复阶段在最近一次已验证修复之后的连续逻辑失败轮数，且脚本硬上限为 5；
  已验证修复经过后续浏览器执行后，新暴露的指纹进入新的有界修复阶段，但 artifact cycle 仍全局递增。
  `combobox_no_progress_seconds` 是单个下拉字段的无进展上限，
  `retry_after_verified_repair` 决定验证通过后是否只重试受影响岗位。

所有路径相对于项目根目录；`${RESUME_SOURCE_DIR}` 这类值从 `.env` 读取。不要把密钥或候选人答案写进 SOP 配置。

## 1. 开始一天：恢复上下文

```bash
.venv/bin/python scripts/daily_sop.py check
.venv/bin/python scripts/daily_sop.py report
```

第一条命令只做本地预检和投递台账刷新，不访问岗位网站。任何 `ERROR` 都必须先解决。`WARN`
要在当日日报中说明是否影响当前来源。
隔离 Codex 的二进制和只读远程 `codex exec` 探针也在预检范围内；`codex login status`
只显示本地认证方式，不能单独证明 refresh token 可用。自动化单次 exec 优先使用
`CODEX_API_KEY`，未配置时可兼容已有 `OPENAI_API_KEY`，但密钥不会持久化，也不会进入修复
工作区或子 Shell。若机器级 Codex 配置选中了 custom model provider，启动器会在继续保留
`--ignore-user-config` 的前提下，只投影所选 provider 的 model、base URL、wire API、
认证模式和 `env_key` 名称，并注入该 `env_key` 对应的单个环境变量。custom provider 缺少
密钥时必须在启动 Codex 前报告 `repair_agent_provider_key_missing`，不得回退到 ChatGPT
登录或默认 OpenAI 端点。认证、配置、网络或限流不可用显示为 `automatic repair` WARN，
因为它只停用独立 repair lane，不应阻断其他岗位。后续可修复缺陷会保留为
`repair_unavailable`，不会消耗 coding repair 周期。
命令启动时也会清理 `output/daily/.tmp/` 中超过 24 小时、且没有活跃进程的本项目残留。
同时会从 SQLite 重新生成 `output/daily/APPLICATION_LEDGER.csv`。Agent 必须先读取该台账，
再进入准备阶段。

第二条命令读取 `output/daily/latest.json`，重新生成上一次 `RUN_SUMMARY.md`。第一次运行还没有 latest 时可跳过。

开始条件：

- 岗位源、真实档案、敏感答案库均为有效 JSON。
- 至少有一份非空原始 PDF 简历。
- 跟踪数据库完整且持续复用。
- 投递台账已生成，且数据库中已有提交、阻塞和处理中记录均可见。
- 需要 LLM 时 API 凭证存在。
- Gmail、CAPTCHA 和 Chromium 与本地策略一致。
- 没有未解释的 `execution-attempt.json`。

## 2. 准备阶段：不打开浏览器

```bash
.venv/bin/python scripts/daily_sop.py prepare
```

脚本自动创建：

```text
output/daily/YYYY-MM-DD/HHMMSS/
  run-state.json
  pipeline-manifest.json
  jobs.json
  shortlist.json
  candidate-screening.json       # 仅有被筛除岗位时
  prior-terminal-outcomes.json   # 仅命中既有终态时
  evaluation-metrics.json        # 每轮量化考核与目标判定
  agent-runtime-trace.json       # 全流程 Agent Core 轨迹与 handoff 连续性
  applications/
    */agent-trajectory.json      # 单岗位 prepare/execute/recovery/repair/evaluation
  RUN_SUMMARY.md
```

`run-state.json` 记录配置文件哈希、档案/答案/岗位源输入哈希、明确的提交模式、命令、阶段和时间；`latest.json` 指向创建时间最新的运行。历史 `recover`/`repair` 只更新所选运行自身，不能因旧运行的 `updated_at` 较新而让该指针倒退。后续 Agent 不再猜测“上次执行到哪里”。准备后若配置、真实档案、敏感答案或指定简历发生变化，`execute` 会拒绝混用旧包，必须重新准备。

准备阶段验收：

- manifest 的 `prepared` 不超过配置的 `limit`。
- 每个准备批次内，同一家公司只保留一个岗位，避免对同一家公司重复投递；若候选池不足，
  仍按分数取其他公司岗位补足。
- 同一天内已经由更早批次取得真实页面确认提交的公司，会在后续准备批次中继续排除，避免跨批次重复。
  仅准备但未执行、或执行后没有确认成功的旧岗位标记为未投递并释放同公司新岗位；
  原始终态仍保留在数据库和审计中，具体 URL 继续受 submitted/terminal Recovery 门保护。
- 短名单默认按「创业公司 → 中型公司 → 大型公司」的顺序排序，在每个公司层级内再按匹配
  分数排序。
- `candidate-screening.json` 没有被拒绝却进入准备队列的候选人。
- 每个包的职位、公司和申请 URL 对应。
- 每个包使用配置允许的外部原始 PDF；不得出现包内生成的简历。
- `prior-terminal-outcomes.json` 中的终态不会被当作新机会重投。
- `APPLICATION_LEDGER.csv` 中已经确认提交的规范化申请 URL 不会进入新准备包。
- `submit_gate` 和配置的 `submit_complete` 符合当天授权。

任一项不符合时停止，不执行浏览器。修复输入或代码后新建一次 `prepare`，不要手改生成包掩盖问题。

若 `prepared=0`，本轮不是完成态。脚本将：

- 把 `run-state.json` 和 `latest.json` 的阶段写为 `waiting_for_candidates`；
- 按 `empty_wake_minutes` 记录未来的 `next_wake_at`；
- 生成日报后退出，不在 Goal 或进程内部长时间 sleep；
- 由外部定时器在 `next_wake_at` 启动新的 `run --execute`，重新导入和筛选来源。

空候选表示当前来源中的岗位均被分数、候选人筛选、去重、冷却或熔断门挡住，不表示任务
已经完成，也不授权降低门槛或绕过数据库。

## 3. 执行阶段：明确授权浏览器副作用

```bash
.venv/bin/python scripts/daily_sop.py execute
```

`execute` 默认不止执行一批：当前批次写出完整终态审计后，会继续准备合格新候选并追踪
`daily_submit_target`。只有单批诊断或明确交接时使用
`.venv/bin/python scripts/daily_sop.py execute --one-batch`。无完整审计时仍立即停止，
不能用下一批掩盖异常。

脚本从 latest 指针恢复刚才的准备目录，先再次预检，再调用批量执行器。执行器在打开任何浏览器之前写入并验证 `resume-preflight.json`；简历路径、扩展名、存在性或来源不一致时整批停止。
每个生成运行时还会形成带 `SUBMIT` 或 `WRITE` 副作用的结构化 ToolCall，并经过职业
Policy Gate；LinkedIn 自动访问、未验证档案、重复/受保护终态、错误简历来源或缺失确认要求
会在浏览器启动前分流。浏览器内部的字段门和确认检测仍是靠近环境的第二道强制边界。

执行阶段只允许使用准备阶段生成的 `batch-summary.json`。不要重新拼接 `pipeline run-execute`，也不要把另一次运行的包混入本次目录。

生产运行目录下的浏览器执行只能由本 SOP 的 `execute` 或 `run --execute` 授权。
`pipeline run-execute` 与 `applications execute-batch` 仅用于内部受控调用、离线 fixture
或诊断；它们直接指向 `output/daily/` 时会拒绝执行。`check` 和 `report` 还会校验阶段与
完整审计的一致性：若发现 `progress.complete=true` 但运行仍是 `prepared`、
`prepared_empty`、`waiting_for_candidates` 或 `executing`，必须报错并先做审计对账，不能
静默生成日报或把运行当作未执行。

每个岗位的自动恢复顺序固定为：

1. 依据真实档案、approved sensitive KB、本人简历文本和受约束的非敏感 LLM 回答填写字段；
   动态下拉若在规划时尚未暴露选项，会在控件打开后把真实可见选项重新交给受控生成器；
   生成结果必须回映射到页面原始选项并通过读回校验，不能提交页面不存在的自由文本；
2. 对动态字段、读回失败和可恢复的普通填写错误执行最多
   `JOB_AGENT_SELF_HEAL_PASSES` 次有界重检，即使同页另有人工阻塞字段也继续修复可恢复项；
   CAPTCHA 或服务端校验后的重检会先验证并复用与批准答案匹配的已提交 combobox 值，
   包括未暴露 ARIA `combobox` role 的 Greenhouse 控件；不匹配或无法读回时仍重新受控填写；
   每个 combobox 同时受 `auto_repair.combobox_no_progress_seconds` 限制，超过后立即生成
   明确的 no-progress blocker，不再耗满岗位级超时；
3. 若仍有 blocking review，保存结构化 `review_items` 和 `review-required.txt` 并停止提交；
4. 只有 blocking review 全部解决后，才调用 CapMonster 处理检测到且受支持的 CAPTCHA；
5. 点击提交后，只有明确的 CAPTCHA challenge/token 错误才允许一次有界恢复；
6. `possible spam`、HTTP 429 或服务器限流即使与残留 CAPTCHA 元素同时存在，也立即记录
   终态，不再求解或重复提交。

CapMonster 只能处理受支持的 CAPTCHA。它不能解除 HTTP 429、服务器限流、账户封禁，也
不能替代候选人回答事实、法律、授权、薪资或人口统计字段。solver unsupported/error 和
`captcha recovery failed` 都属于 `submission_processing_error`，不得污染公司或 ATS
反垃圾冷却。

批次执行器为每个岗位设置独立超时和异常边界。一个岗位进入字段阻塞、反垃圾终态、超时
或运行时失败后，必须输出 `Application N/M terminal: <status>; continuing.`，立即更新
`execution-audit.json`，然后继续下一个岗位；不得等待整批结束后才写审计，也不得让单个
岗位的普通异常中断后续岗位。

如果整个执行进程被系统强制终止，而 `run-state.json` 仍为 `executing` 且规范
`execution-audit.json` 的 `progress.complete=false`，使用：

```bash
.venv/bin/python scripts/daily_sop.py execute \
  --run-dir output/daily/YYYY-MM-DD/HHMMSS \
  --resume-incomplete
```

续跑会保留全部已有终态，把第一个未记录岗位保守分流为
`submit_clicked_unconfirmed`（中断后结果未知，不再点击），只执行其后的未记录岗位，并继续
原子更新同一个规范审计文件。`--resume-incomplete` 只能用于 `executing` 阶段，不能与
`--retry` 组合，也不会传递 `--retry-prior-terminal-outcome`。

一次性完整执行可使用：

```bash
.venv/bin/python scripts/daily_sop.py run --execute
```

`run` 不带 `--execute` 时只完成预检和准备。`run --execute` 与默认 `execute` 都会在每个
批次获得完整终态审计后重新统计当天确认提交数；未达到 `daily_submit_target` 时继续准备
下一批。当前没有候选时
写入 `next_wake_at` 后退出，由外部调度恢复。异常批次没有完整审计时必须停止，不能用下一批
掩盖。`--execute` 是明确的浏览器和最终提交授权，不能由 Agent 自行补上。

### 3.1 受控自动修复

审计中的普通字段/运行时错误会生成
`repair/repair-request-cycle-NN.json`，并把运行阶段写成 `needs_repair`。该状态不能作为
Goal achieved。反垃圾、CAPTCHA、邮箱验证、账户、点击未确认、站点处理错误以及需要新增
候选人事实的问题不会生成 coding repair。

启用 `auto_repair.enabled` 后，SOP 会：

1. 持续读取增量 `execution-audit.json`；首个可修复指纹出现后立即在独立线程和隔离副本中
   启动 Codex，浏览器继续处理后续岗位，主工作区在批次退出前不提升代码；
2. 先把修复请求标记为 `REPAIR` 副作用并经过同一职业 Policy Gate；非可修复终态、真实
   浏览器验证或真实提交请求会直接拒绝；
3. 只复制产品源码、测试、虚构 examples 和允许的操作文档到临时隔离工作区；不会复制
   `profiles/`、简历、数据库、`.env`、本地配置或历史运行输出；
4. 用 `codex exec --ephemeral --sandbox workspace-write` 执行有时限的非交互修复；
5. 拒绝白名单外修改、符号链接和主工作区在修复期间发生的同文件变化；
6. 运行隔离区目标测试，再用修复启动前冻结的可信测试副本运行完整测试，最后运行
   `examples verify-offline`；修复 Agent 不能通过删除或削弱旧测试获得通过；
7. 浏览器批次退出后再次检查主工作区哈希，仅在全部通过后提升代码，记录
   `repair_verified`，并从原 batch 生成只含修复指纹关联岗位、带
   `repair_verified=true` 和 `retry_scope=single_application` 的 retry batch；SOP 随后在主进程
   从原始规范化岗位、当前已批准 profile/sensitive KB 和已提升代码重建这些包，避免旧包内嵌
   的旧运行时代码或旧事实快照进入重试，同时不向隔离 Repair Agent 暴露私密事实；
8. 最近一次已验证修复之后连续达到 `max_cycles` 仍未通过时记录 `repair_exhausted` 并返回失败，
   不把命令成功退出误报为投递完成；尚未真正启动 Codex 的 `exhausted` 门检和基础设施失败
   都不消耗逻辑周期。

活动浏览器进程不会热改源码。验证后的重试会启动新的执行子进程，因此加载的是已提升代码；
SQLite 继续保护已确认提交，非修复终态不会被带入 retry batch。

Codex 认证/配置/网络/限流错误属于 repair 基础设施不可用：记录独立 attempt 编号和
`repair_unavailable`，但不增加逻辑 `max_cycles` 计数，也不立即重复同一个确定性失败。
若外部中断使运行残留在 `repairing`，先确认对应 Repair 进程已经退出，再运行
`repair --recover-interrupted`；它会记录基础设施中断并保留当前审计范围，不消耗 coding
repair cycle，也不会自动打开浏览器。
修复环境恢复后运行：

```bash
.venv/bin/python scripts/daily_sop.py repair \
  --run-dir output/daily/YYYY-MM-DD/HHMMSS
```

该命令先从当前完整审计重新生成脱敏 fingerprints 和 scoped targets；旧 request 只在当前
审计不可用或不完整时兜底。完整审计若确认只剩候选人事实等非代码阻塞，会持久化空 repair
范围、废止旧指针并回到 `executed_with_blockers`，不检查 Codex readiness。其他重建结果会在 Codex readiness 检查前写入新的
`repair-request-refresh-NN-cycle-NN.json`，同步更新 state/manifest 指针；因此即使认证仍
不可用，当前完整范围也会保留，同时不启动 Codex、不新增 repair attempt、不消耗逻辑
cycle。新的 refresh/attempt 文件都不覆盖旧 request/result，且默认不打开浏览器。若
Codex 判断代码已经包含修复且没有产生 diff，SOP 仍会运行受影响测试、冻结全量测试和离线
验证；全部通过时记录 `already_fixed_verified` 并生成同样受限的 scoped retry，而不是
`repair_agent_made_no_changes` 失败。若需要在验证后立即执行唯一的 scoped retry，必须显式
增加 `--retry-verified`。已经处于 `repair_verified` 的运行也可用同一参数恢复已保存的
retry batch，不会重跑 Codex 或原始整批。

只需要刷新历史 request 范围、不希望检查 Codex 或启动修复时，运行：

```bash
.venv/bin/python scripts/daily_sop.py repair \
  --run-dir output/daily/YYYY-MM-DD/HHMMSS \
  --refresh-request-only
```

该模式不做 readiness 网络探测，不创建 repair attempt，不消耗 cycle，也不启动浏览器。

### 3.2 非代码阻塞恢复

反垃圾、CAPTCHA、邮箱验证、账户、缺少候选人事实、点击未确认和站点处理错误不进入
Codex Repair，但每条执行审计必须包含 `recovery_plan`。SOP 会把其中的自动动作转换为
结构化 ToolCall，逐项经过 Policy Gate 和 ControlledExecution，并把 `verified`、
`pending`、`waiting_for_user` 或 `failed` 结果写入 `recovery-execution.json` 和对应审计
记录。该计划记录恢复策略、自动动作、候选人动作、需要的证据、冷却时间、单岗位重试范围
和重试条件：

1. 反垃圾或 HTTP 429 只冷却受影响公司或 ATS tenant，继续其他公司；冷却后先做查重和
   只读可用性检查。
2. CAPTCHA 只对检测到且受支持的新 challenge 使用已配置 solver，最多一次；unsupported
   转候选人交互，不伪造 token。
3. 邮箱验证优先用只读 Gmail token 查找请求时间之后的 code/link；缺少授权时请求候选人。
4. 候选人账户优先用外部凭证存储完成登录、创建和验证；密码和值不得写入审计。
5. 缺少候选人事实时列出字段标签，请候选人批准并更新 profile/sensitive KB，随后只重建
   该岗位。
6. 点击未确认先核对页面证据、门户、邮箱和 application ID；已有确认时只更新 SQLite，
   未证明首次点击失败前不再次点击。

教育经历（包括高中名称和毕业年份），过往/当前任职、承包或投递面试经历，其他 offer
及其截止日期，亲属关系与利益冲突，雇主限制与便利需求，母语/本地文字法定姓名，以及
产品/地点/工作方向等个人偏好和明确的每周到岗承诺必须视为候选人事实。
优先使用规范化后与表单字段精确对应的 profile answer，或该字段适用且已批准的 sensitive KB；
当已保存答案与表单选项语义兼容时，允许使用受控 closest-match 回退，约束条件为：
否定极性一致、不选 "Other"/"Select"/"None" 等占位选项、达到置信度阈值、
不把简历缺失解释为 `No`，也不从一般搬迁、远程偏好或低频到岗意愿推导固定到岗承诺。
缺少可匹配事实时保持 `waiting_for_user`。运行时明确返回
`candidate fact needs explicit approved answer` 时必须走同一
`candidate_fact_resolution`，不能误分流到 coding repair。

Gmail 查询、SQLite reconciliation、证据检查和 tenant 冷却有内置执行器；账户、
CAPTCHA 或浏览器恢复只有在对应外部适配器和所需上下文已配置时才能完成，否则保持
`pending`。需要候选人事实或授权的动作必须是 `waiting_for_user`，LLM 不得代填。

历史完整审计如果只有 `recovery_plan`、没有 `recovery-execution.json`，运行：

```bash
.venv/bin/python scripts/daily_sop.py recover \
  --run-dir output/daily/YYYY-MM-DD/HHMMSS
```

`recover` 会使用当前分类规则重新生成每条 Recovery Plan，通过 Agent Core 回放自动动作，
更新原审计并写入 `recovery-execution.json`。默认不打开浏览器，也不会重投；只有恢复证据
已经完整并显式增加 `--retry-verified` 时，才执行生成的单岗位 retry batch。
如果当前回放没有任何 `retry_ready` 目标，SOP 会清除 state/manifest 中上一轮的
`recovery_retry_batch` 指针，防止已经提交或仍缺确认的历史目标被后续 Repair 合并回来。
存在多次 execution attempt 时，Recovery 按 application ID 合并全部完整审计，以最后一次
终态覆盖同一岗位，同时保留只出现在早期批次的岗位；重新规划后的 recovery 注解写回该岗位
最后出现的原始 attempt 审计，不把合并视图覆写成某一次浏览器执行的证据。
对于 `candidate_fact_resolution`，候选人补全答案后，`recover` 会从当前 profile 或
approved sensitive KB 验证原阻塞字段，在原运行的 `recovery/` 子目录重建一个新申请包，
保留旧包和旧审计，并记录事实源哈希。只有新包通过事实门时才会生成带
`recovery_verified=true`、`retry_scope=single_application` 的恢复批次；仍缺答案时继续
保持 `waiting_for_user`。普通事实必须与原阻塞字段精确对应，且相对于旧申请包确实是新增
或变更的已批准答案；`N/A` 等占位值、旧包中已经存在但未解决阻塞的答案，以及同时包含
非候选人事实阻塞的申请都不能借此恢复通道重试。

普通自定义答案写入 `ops/daily.local.json` 中 `profile` 指向文件的 `answers`；敏感、法律、
身份、授权和人口统计答案写入 `sensitive_kb` 并设为 `approved: true`。配置中的
`profile_vector_db` 是这些批准事实的派生检索索引，`database` 则是投递历史库，二者都不是
直接批准候选人答案的位置。非敏感开放题允许运行时 LLM 依据已批准档案事实和候选人本人
简历文本合理生成；要求候选人原创且明确禁止 AI 的问题仍进入 `candidate_fact_resolution`，
Repair Agent 不得编写答案。

`retry_allowed` 不是立即重试授权。恢复动作和证据全部满足后，执行请求还必须带
`recovery_verified=true` 和 `retry_scope=single_application` 通过 Policy Gate；不得重放
原批次。

## 4. 收尾阶段：以审计为准

```bash
.venv/bin/python scripts/daily_sop.py report
```

完成后必须同时存在：

- `execution-audit.json`：隐私安全的结构化执行记录；执行中通过 `progress` 显示计划数、
  已终态数、剩余数和是否完成。
- `resume-preflight.json`：实际简历路径、大小与哈希证据。
- `RUN_SUMMARY.md`：当日数量、每个岗位终态、结构化 Recovery Plans 和下一步。
- `recovery-execution.json`：每个受保护终态的实际恢复动作、证据和是否满足单岗位重试条件。
- `evaluation-metrics.json`：可机读的每轮考核记录，包含导入漏斗、终态审计覆盖、
  作为 80% 主目标的原始导入确认率、诊断用最终合格岗位确认率和点击未确认数。
- `agent-runtime-trace.json`：Pipeline、浏览器、Recovery、Repair、Evaluation 的统一索引，
  并统计每岗位 prepare/execute Observation handoff。实际执行岗位必须为 `continuous`。
- `applications/*/agent-trajectory.json`：同一 `agent_runtime_id` 的脱敏 AgentRound；
  Tool 参数、候选人答案、页面正文和凭证不得写入该文件。

日报的 `Efficiency` 区域分别记录 prepare 活跃时间、execute 活跃时间、有效工作时间、
等待时间和本批确认成功率。确认成功率只以 `submitted / total` 计算，不把“填写完成”、
点击未确认或前一天的提交算入本批成功。

`Agent Evaluation` 区域和 `evaluation-metrics.json` 是每轮考核依据：
`raw_import_to_confirmed_rate = 当日页面确认提交数 / 本轮原始导入岗位数`，该值必须达到
`min_confirmed_submission_rate`。`confirmed_submission_rate_final_eligible` 继续保留，
但只作执行质量诊断，不再缩小 80% 主目标的分母。`submitted` 只计页面确认并写入数据库的
投递。每轮还要求完整终态审计，并将 `submit_clicked_unconfirmed` 维持为 0。
单轮导入数达到 `imported_cohort_target` 前，样本规模状态为 `insufficient_cohort`；该状态
不授权降低资格、去重、反垃圾或候选人事实门。

考核由 Agent Core 中注册的 `job_application_round` evaluator 执行。结构化文件继续保留
`counts`、`rates`、`targets` 和 `assessment`，并新增 `agent_core`，记录 evaluation ID、
轮次、总体状态、建议和时间。Core 同时保存有界考核历史并产生只读
`agent_evaluation` Observation，供下一轮规划参考。考核器不调用 LLM、Tool 或浏览器，
考核建议也不能自动放宽任何执行门。

真实浏览器不是 CLI 直接副作用。每个岗位先执行 `browser_execute` ToolCall，经
Policy Gate 和 ControlledExecution 获得结构化终态 ToolResult；该结果形成新 Observation，
下一轮 `terminal_outcome_router` 必须消费它并选择完成、Recovery 或记录终态。跨命令执行
通过 `agent_handoff` 原样恢复准备阶段最后一个已脱敏 Observation 的 ID 与 payload，而不是
创建无关联会话。
执行前的 runtime package 与 resume provenance 检查从同一 Observation 并发执行，只有
`concurrent_read_join` 能成为浏览器动作输入。生产 Python Playwright 会话中的实时字段观察、
填表、Next 和 Submit 也必须作为同一 Agent Core 的 `ats_*` ToolCall；Next/Submit 必须从
“执行/停止”两个候选中选择，不能先点击再补记轨迹。旧 Node/Chrome 脚本只用于兼容或离线
fixture，不是每日生产入口。
Playwright 启动或首次导航失败且尚未产生任何 `ats_*` 轮次时，可用全新浏览器会话重试一次；
一旦出现页面观察、填写、Next 或 Submit 轮次，整份申请不得自动重启。审计只记录分类后的
故障码和有界重试次数，不保存原始异常中的页面内容。

运行目录日期只表示准备时间。跨午夜批次的 `daily_target`、日报和考核会读取最新
`execution_attempt.finished_at`，按实际执行本地日期查询 SQLite；结构化指标同时记录
`accounting.local_date`、时区和 `date_source=execution_finished_at`。

状态处理：

| 状态 | 含义 | 当日动作 |
| --- | --- | --- |
| `submitted` | 已得到提交确认 | 结束，不再操作 |
| `autofill_completed_blocked` / `completed` | 表单已处理但仍有阻塞项 | 读取该包的 `review-required.txt` |
| `email_verification_required` | 邮箱验证码链路未完成 | 修复 OAuth、查询条件或验证码交付后再决定重试 |
| `submission_blocked_by_anti_spam` | 页面明确显示 spam 或频率限制 | 在配置的冷却窗口内停止该公司或 ATS 租户，不立即重试 |
| `submit_clicked_unconfirmed` | 点击过但未确认结果 | 先用保存的页面证据 reconcile，禁止再次点击 |
| `submission_processing_error` | 站点处理失败 | 检查页面证据和适配器，不盲重投 |
| `candidate_account_required` | 账户创建或登录未闭环 | 先修复密码/验证流程 |
| `failed` / timeout | 运行时失败 | 用审计定位一个根因和最短复现 |
| `needs_repair` / `repairing` | 已识别可修复代码指纹 | 等待受控隔离修复，不能声明完成 |
| `repair_unavailable` | Codex 认证或 repair 基础设施不可用，未消耗逻辑周期 | 恢复环境后运行 scoped `repair`，默认不打开浏览器 |
| `repair_verified` | 代码已通过全部离线验证 | 只运行审计记录的 scoped retry batch |
| `repair_failed` / `repair_exhausted` | 修复未通过或达到上限 | 保持终态，读取 repair result 后再诊断 |
| `skipped` | 数据库判定重复、已投或已有终态 | 保持跳过 |

当日完成定义：

- 当天数据库确认提交数达到
  `max(daily_submit_target, ceil(本轮原始导入数 * min_confirmed_submission_rate))`；
- `raw_import_to_confirmed_rate` 达到配置的 80% 主目标；
- 每个准备岗位都有一个审计终态；
- 每个非 `submitted` 状态都有明确分流；
- 日报已生成；
- 投递台账已刷新并包含本轮最终状态；
- 没有同日盲重试；
- 没有把“进程退出码为 0”误报成“全部已投递”。

## 5. 投递台账与防重复

唯一的人类可读投递台账是：

```text
output/daily/APPLICATION_LEDGER.csv
```

每次 `check`、`prepare`、`execute`、`report`、`ledger` 或 `run` 都会从配置指定的 SQLite
数据库刷新该文件。需要只刷新台账时运行：

```bash
.venv/bin/python scripts/daily_sop.py ledger
```

台账按最近更新时间倒序，包含：

- `submitted_at_utc`：只在页面确认投递成功后写入；
- `company`、`role`：公司与岗位；
- `status`：当前提交、阻塞、失败或待处理状态；
- `application_url`：实际申请地址；
- `first_recorded_at_utc`、`last_updated_at_utc`；
- `application_id`：执行包必须绑定并复用的数据库 ID；
- `legacy_duplicate_of_application_id`：旧数据中已识别出的历史重复记录。

防重复不是依赖 Agent 记忆或手工检查 CSV。数据库会为每条 application 生成唯一键：

- 有申请 URL：规范化 URL，并移除 `utm_*`、`ref`、`source` 等 tracking 参数；
- Greenhouse 自有页面的 `gh_jid` 与官方 `/jobs/<requisition>` URL 归为同一岗位；
- Lever 的岗位页与同一 requisition 的 `/apply` URL 归为同一岗位；
- 没有申请 URL：使用规范化的公司与岗位；
- 同公司同岗位但 requisition URL 不同，仍视为可能不同的真实职位；
- 命中已有唯一键时复用原 application ID，不创建新的投递记录。

CSV 是数据库的只读镜像。不要手改 CSV、删除旧行、更换数据库，或新建数据库规避去重。
历史重复记录不会自动删除，而是在最后一列标记其对应的主记录。

## 6. 临时目录回收

`check`、`prepare`、`execute`、`repair`、`report` 和 `run` 都在
`output/daily/.tmp/job-agent-<command>-*/` 中使用独立临时工作区，并将该路径传给
Python、Node 和 Playwright。命令正常完成、返回错误或抛出异常时都会立即回收当前工作区。

进程被强制终止时可能来不及执行回收。下一次 SOP 命令会自动删除超过 24 小时且没有活跃
所有者进程的残留；需要立即清理所有非活跃项目临时目录时运行：

```bash
.venv/bin/python scripts/daily_sop.py cleanup
```

也可以只清理达到指定时长的残留：

```bash
.venv/bin/python scripts/daily_sop.py cleanup --older-than-hours 24
```

清理器只删除同时满足以下条件的目录：

- 位于配置的 `output_root/.tmp/` 内；
- 目录名以 `job-agent-` 开头；
- 包含与当前项目和输出根目录一致的所有权标记；
- 标记中的所有者 PID 已不再运行；
- 已达到 `--older-than-hours` 指定的时长。

未标记目录、符号链接、仍在使用的目录和 macOS 共用系统临时目录都会保留。不要用
`rm -rf $TMPDIR` 或通配符删除通用 `playwright-*` 目录。

## 7. 受控重试

普通运行时问题只有根因已经处理，才允许显式：

```bash
.venv/bin/python scripts/daily_sop.py execute \
  --run-dir output/daily/YYYY-MM-DD/HHMMSS \
  --retry
```

重试会生成带编号的新审计与简历预检文件，不覆盖第一次证据。以下情况不能用重试解决：

- 只是希望提高当天投递数量；
- 站点仍处于反滥用冷却；
- 邮箱、账户或 CAPTCHA 配置尚未变化；
- 点击提交后还未 reconcile 确认证据；
- 还不知道第一次失败的具体状态。

coding repair 和 Recovery 不使用上面的原始整批入口。coding repair 使用
`repair --retry-verified` 读取 `scoped_retry_batch`；受保护终态只有
`recovery-execution.json` 已验证且 batch 项携带 `recovery_verified=true`、
`retry_scope=single_application` 时才能通过 `recover --retry-verified` 进入执行。二者均
不得手工改写 batch 扩大范围。

默认反滥用冷却由 `.env` 中的 `JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS=24`
控制。公司以冷却窗口内最新一次成功或反滥用终态为准；共享 ATS host 只统计该窗口内的
记录，并由 `JOB_AGENT_ANTI_SPAM_HOST_COOLDOWN_THRESHOLD` 控制阈值。Greenhouse、
Lever 和 Ashby 按公司或 board 隔离。CAPTCHA solver 的 unsupported/error 属于
`submission_processing_error`，不能作为整个公司或 ATS 的反滥用证据。

普通失败使用独立熔断，不记为反垃圾。默认配置为：

- `JOB_AGENT_FAILURE_CIRCUIT_BREAKER_HOURS=6`；
- `JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD=2`。
- `JOB_AGENT_NETWORK_HEALTH_CIRCUIT_THRESHOLD=3`。

在时间窗口内，同公司或同 ATS tenant/adapter 连续两次出现相同的
`application_form_unavailable`、`autofill_completed_blocked`、`autofill_failed`、
`autofill_timed_out` 或 `submission_processing_error` 后，只暂停对应范围并转向其他
来源。执行器在当前批次每个终态增量落盘后立即更新连续计数；达到阈值时，后续同范围岗位
仍生成完整 Agent 轨迹和 `skipped_policy_denied` 终态，但
`JobApplicationPolicyGate` 必须以 `failure_circuit_breaker_active` 拒绝
`browser_execute`，不再打开浏览器。不同失败状态、后续确认成功或窗口过期会关闭熔断；
不得借此重投已有终态。只有已生成的精确单岗位 retry 同时携带 `retry=true`、
`retry_scope=single_application` 和 `repair_verified=true` 或 `recovery_verified=true` 时，才可
解除该目标的旧普通失败熔断；反垃圾冷却和当前批次全局网络健康熔断仍不可绕过。

跨公司浏览器或网络故障另有批次级健康熔断。连续达到阈值的网络故障会写入
`batch_health.network` 和每岗位的 `network_health_observation`，后续岗位仍经过 Agent
Core，但由 Policy Gate 拒绝新的 `browser_execute`，不再创建浏览器会话。每个网络终态
都会生成 `batch_network_health_recovery` Recovery Plan，先保留脱敏故障证据、执行有界
冷却，再等待只读健康检查；没有 `network_health_rechecked` 证据时不得自动重试。CAPTCHA、
点击未确认、候选人事实缺失和表单入口不可用仍分别进入各自的 Recovery Plan，不会被网络
熔断误分类。

## 8. 改代码后的额外门槛

真实投递链路改动后，先执行目标测试和完整测试，再执行离线端到端验证：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/pytest -q
.venv/bin/job-agent examples verify-offline \
  --out-dir output/offline-verify-sop
```

离线验证使用虚构数据和假的浏览器模块，不接触真实岗位网站。通过后重新执行每日 `check`；只有用户明确授权时才进入真实 `execute`。

## 9. Agent 交接格式

每次交接只报告以下内容，不粘贴长控制台输出：

```text
Run:
Phase:
Config hash:
Imported / shortlisted / prepared:
Submitted:
Other terminal states:
Root cause already resolved:
Next exact command:
Evidence files:
Application ledger:
```

跨会话代码任务使用 `ops/CURRENT_TASK.md` 保存目标、授权范围、验收标准、当前证据和下一条精确命令。不要依赖聊天记忆。
