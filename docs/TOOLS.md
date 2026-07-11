# 求职 Agent 工具清单

这个项目以 `hello_agents` 的 `Agent + ToolRegistry + ToolChain` 为基座。每个 Tool 都只负责一个可审计动作，Agent 负责编排，不让大模型直接操作文件、数据库或浏览器。

## 感知层：Sensors / Perception

| Tool | 作用 | 来源与边界 |
| --- | --- | --- |
| `ManualJDImportTool` | 导入用户粘贴的 JD | 本地输入；不联网 |
| `RSSJobSourceTool` | 读取 RSS/Atom 岗位 feed | 公开 feed；保留来源 |
| `GreenhouseJobSourceTool` | 读取 Greenhouse public Job Board API | 公开 board endpoint |
| `LeverJobSourceTool` | 读取 Lever public postings API | 公开 postings endpoint |
| `RemotiveJobSourceTool` | 读取 Remotive public Remote Jobs API | 公开 API |
| `FormSnapshotScriptTool` | 读取 ATS 页面表单元数据 | Playwright；只读 DOM，不填写、不上传、不提交 |
| `ResumeIndexerTool` | 索引本地 DOCX/PDF 简历模板 | 只读 `RESUME_SOURCE_DIR` |

项目不包含 LinkedIn 未授权抓取或 LinkedIn 自动投递。LinkedIn 岗位只能通过用户提供的链接/JD，或经授权的官方接口接入。

## 思考与规划层：Thought / Planning

| Tool | 作用 |
| --- | --- |
| `JDParserTool` | 将 JD 解析为职位、公司、地点、技能、职责与风险 |
| `FitScorerTool` | 根据候选人资料和偏好计算岗位匹配度 |
| `ResumeSelectorTool` | 从本地岗位模板中选择最匹配的一份 |
| `ResumeTailorTool` | 生成关键词覆盖计划和基于证据的简历草稿 |
| `TruthfulnessCheckTool` | 检查改写是否超出候选人事实库 |
| `SensitiveFieldDetectorTool` | 标记身份、工签、薪资、人口统计、法律声明等敏感字段 |

LLM 可以帮助解析、排序、改写和生成草稿，但不能新增工作经历、教育、数字成果、身份信息或授权状态。所有材料都要经过真实性闸门。

## 执行层：Actuators

| Tool | 作用 | 提交策略 |
| --- | --- | --- |
| `DocumentExporterTool` | 将 Markdown 草稿导出为 DOCX | 生成新文件，不修改原始简历 |
| `FormFillerTool` | 将已批准 profile facts 映射到低风险表单字段 | 敏感字段进入人工复核 |
| `FormFillScriptTool` | 生成 Playwright 填表和简历上传脚本 | 不生成 Submit 点击动作 |
| `ApplicationPackageTool` | 生成 review packet、简历、表单脚本和清单 | 供用户审阅 |
| `ApplicationTrackerTool` | 写入 SQLite 岗位和申请状态 | 记录来源、材料、状态和审计信息 |
| `SubmitGateTool` | 统一控制最终投递边界 | 普通浏览器申请固定为 `blocked_pending_human_confirmation` |

“自动投递”在本项目中被拆成两段：Agent 自动打开页面、填写低风险字段、上传用户批准的简历；最终 Submit 由用户确认。只有未来接入明确允许自动提交的官方 API/适配器，且进入显式 allowlist 后，才可以改变这个闸门。

## 本地简历接入

你的当前英文简历目录是：

```text
/Users/wugaoyi/Learning/求职/英文简历(最新)
```

在本地 `.env` 中设置：

```bash
RESUME_SOURCE_DIR=/Users/wugaoyi/Learning/求职/英文简历(最新)
```

公开仓库不会保存这个目录里的 PDF/DOCX。运行 `job-agent resumes index "$RESUME_SOURCE_DIR"` 后，Agent 会按文件名识别 Agent Engineer、SDE、MLE、ML Infra、AI Algorithm Engineer 和 Data Scientist 等岗位轨道。

## 推荐调用顺序

```text
Job Source -> JDParser -> FitScorer -> ResumeSelector
           -> ResumeTailor -> TruthfulnessCheck -> DocumentExporter
           -> FormSnapshot -> SensitiveFieldDetector -> FormFiller
           -> Human Review -> SubmitGate -> ApplicationTracker
```

完整的 PEAS、环境假设和状态转换见：

- `docs/superpowers/specs/2026-07-08-job-application-agent-peas-design.md`
- `docs/architecture/hello-agents-job-application-agent.md`
