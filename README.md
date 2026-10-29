# 迷你数字员工 · 经营周报员工

锐软科技赛题“迷你数字员工——可审核、可交付的岗位业务 Agent”，场景 A：经营周报。

员工读取门店销售明细（Excel/CSV）和周会纪要（Word/Markdown），汇总分析、找出异常波动及原因，生成带图表和结论的《经营周报.docx》，
经自动质检和人工审核后交付；被退回时按意见返工，缺材料或工具故障时停下来说明原因，不编造、不崩溃。

**核心原则：模型只负责分析和写作；状态流转、准入、数字计算、质检、能否送审和交付全部由程序判定。**

## 快速开始

```bash
make install        # 安装依赖（需要 uv）
make test           # 单元测试 + 40 条用例 + 现场变更预演 + 真实模型调用路径测试（共 116 项，全部离线）
make demo           # 命令行跑一条正常任务，停在“待审核”
make ui             # Web 界面 http://localhost:8501
```

不装 make 时：`uv sync`，然后 `uv run mini ...`、`uv run streamlit run app/streamlit_app.py`。
官方数据（`data/official_a/`，青禾烘焙 2026-08-31 至 2026-11-01）最后一周是第 44 周。统计周期是否可用按**数据实际覆盖的日期**判断，
不看系统日期；数据不含该周时暂停并写明“销售数据实际覆盖 X 至 Y”。Makefile 设置的 `MINI_TODAY=2026-11-03` 只影响报告落款日期。

### 命令行

```bash
export MINI_TODAY=2026-11-03
uv run mini new --period 2026-W44 --file sales_file=data/official_a/销售明细.xlsx \
                --file minutes_file=data/official_a/周会纪要_2026W44.docx
# 只上传 CSV 也可以：区域、品类、销售目标取自 employees/weekly_report/master.xlsx
uv run mini review <任务ID> --changes "华东区下滑原因没说清楚，请结合周会纪要补充"
uv run mini review <任务ID> --approve
uv run mini events <任务ID>             # 回放事件日志
uv run mini status                      # 全部任务
uv run mini supplement <任务ID> --file minutes_file=...   # 补充材料
uv run mini retry <任务ID> --clear-faults                 # 暂停后从断点继续
uv run mini resume                      # 程序被中断后恢复所有未完成任务
uv run mini transitions                 # 状态流转表
uv run mini tools                       # 工具清单与入参 Schema
```

### 接入真实模型

在项目根目录的 `.env` 中填写（`.env` 已加入 `.gitignore`，不会提交；没有该文件时 `cp .env.example .env`）：

```ini
MINI_MODEL=openai_compatible
MINI_BASE_URL=https://api.deepseek.com          # 服务商的 OpenAI 兼容地址
MINI_API_KEY=sk-...
MINI_MODEL_NAME=deepseek-chat                    # 需支持 Function Calling
```

推荐 **deepseek-flash**：迁移到官方数据后的 33 条真实模型用例全部通过（约 53 万 token），一份周报约 1.3 万～1.4 万 token、模型耗时约 20 秒；
与 gemini-3.8-flash-high 的对比（速度、token、原因归属是否谨慎）见 `reports/real_model_report.md`。

然后：

```bash
uv run mini check-model     # 检查连通性、密钥、工具调用、JSON 输出和响应时间
uv run mini cases           # 用真实模型跑全部用例（7 条依赖 Mock 注入错误的用例自动跳过）
```

改回离线 Mock：`.env` 中设 `MINI_MODEL=mock`。单元测试（`make test`）始终使用 Mock，另有一组测试通过本地假 OpenAI 服务覆盖真实模型的 HTTP 调用路径。
用例中 `report_contains` 检查事实与程序生成的文字，任何模型都须满足；`report_contains_mock` 是 Mock 的固定措辞，真实模型下不检查。

## 目录

```
employees/weekly_report/       场景 A 经营周报员工（青禾烘焙官方数据与《周报格式要求》）
  employee.yaml        员工结构：表单、数据结构与主数据关联、准入、步骤、开放工具、质检规则、上限、报告章节（业务人员改这里）
  SKILL.md             员工做法：岗位职责、口径、异常分析、撰写要求、篇幅、注意事项（Agent Skills 格式）
  master.xlsx          兜底主数据：门店信息、商品信息、销售目标（表单“主数据”上传的、或销售明细 xlsx 中的同名工作表优先）
src/mini_employee/
  states.py            状态、流转表、闸门条件
  store.py             SQLite：任务表 + 只追加的事件表；唯一的状态变更入口 transition()
  orchestrator.py      编排器：准入 → 步骤执行 → 质检 → 修正 → 审核 → 交付；断点恢复
  tools/server.py      MCP 工具服务（FastMCP），7 个工具
  tools/gateway.py     工具网关：白名单、Schema 校验、重试、超时、故障注入、留痕
  tools/data.py        数据读取、清洗、KPI 与异常识别（所有数字在这里算）
  llm/agent.py         Agent 步骤：模型 ⇄ 工具循环，轮次 / 时长上限，输出 Schema 校验
  llm/mock.py          离线 Mock 模型
  qc/rules.py          质检规则（可插拔）
  qc/engine.py         质检引擎、程序修正、检验记录
  validate.py          员工配置校验（mini check-config；加载配置时自动执行）
  render.py            占位符替换 + Word / Markdown / HTML / PDF 生成
  runner.py            用例批量运行器
app/streamlit_app.py   Web 界面：发起任务 / 任务中心 / 审核台 / 事件回放 / 故障注入 / 员工配置
cases/                 40 条自测用例（YAML），全部基于官方数据：A01–A07（含公开样例 A-01～A-05、上传主数据），其余为故障、审核流转、缺材料等系统行为
data/official_a/       官方数据：销售明细（xlsx 4 个工作表 / csv）、第 42–44 周周会纪要、格式要求、公开样例用例
data/fixtures/         用例变体数据，由 scripts/make_fixtures.py 从官方数据生成（make data）
docs/技术报告.md        技术报告草稿
```

## 设计要点

| 赛题要求 | 实现 |
|---|---|
| 流程与状态流转表，非法流转被拒绝 | `states.py` 12 个状态、`store.transition()` 查表 + 触发方校验 + 闸门；拒绝写 `transition_rejected` 事件 |
| 能否送审 / 交付由程序判定 | 只有 `QC --qc_pass-->` 能到待审核，闸门直接读 `qc_report.json` 最后一轮：须全部通过且检的是当前产物（哈希一致）；只有审核人 `approve` 能交付，且审核的文件哈希 = 交付文件哈希、质检记录通过 |
| 退回意见被采纳 | 意见由程序按配置拆成待办（章节 + 关键词），只重做受影响的步骤；新增 `feedback_addressed` 质检逐条校验，不通过不能再次送审 |
| 缺材料暂停、补充后继续 | 准入或执行中发现缺材料 → 待补材料，说明缺什么；补充后重新准入，从断点继续 |
| 员工能力写成配置 | `employee.yaml` + `SKILL.md`，界面“员工配置”页可直接编辑 |
| 轮次与时长上限 | 每步 8 轮 / 180 秒，任务 600 秒，超限暂停转人工（理由见下） |
| ≥3 个工具，明确入参出参，只能调开放的工具 | 7 个 MCP 工具，JSON Schema 入参；模型每步只看到该步开放的工具，越权调用被拒绝并留痕 |
| 工具失败重试、暂停、不编造 | 网关指数退避重试 3 次，耗尽后暂停并提示“第几步、什么原因、已完成 x/y 步”；数字只能用占位符引用 |
| ≥3 类自动质检，可修正，有上限，有记录 | 结构完整、数据一致性、异常归因有据、纪要说法与数据核对、敏感信息、字数（+ 审核意见已采纳）；程序能确定性修正的先修，其余交模型，最多 2 轮；检验记录随交付包保存 |
| 加分：事件流与断点恢复 | 全部事件只追加写入 SQLite，状态变更与对应事件在同一事务内写入；步骤输出落盘并记录哈希，重启后跳过已完成步骤；界面可回放 |
| 加分：工程加固 | 写文件工具带幂等键，重试不产生重复文件；越权调用留痕；乐观锁防重复点击；路径限定在任务目录 |

### 上限取值理由

| 上限 | 取值 | 理由 |
|---|---|---|
| 单步模型轮次 | 8 | 实测（deepseek-flash 全量 44 条用例，81 个模型步骤）：纪要和常用占位符直接放进输入后，80% 的步骤 1 轮完成、其余 2 轮；早先 gemini-3.8-flash 实测 99 个步骤结果相近。8 轮是防循环的上限，第 8 轮收回工具、要求直接给出结果 |
| 单步时长 | 180 秒 | 实测单次调用：deepseek-flash 中位 8 秒、P90 12 秒、最长 48 秒；gemini-3.8-flash 中位 23 秒、最长 57 秒。按较慢的模型最多 3 次慢调用计 |
| 任务总时长 | 600 秒 | 含 2 轮自动修正和一次返工，仍远低于人工写周报的时间 |
| 工具重试 | 3 次，退避 1/2/4 秒 | 覆盖瞬时网络抖动，不长时间阻塞 |
| 质检自动修正 | 2 轮 | 第 1 轮通常能修好数字和结构；2 轮仍不通过说明问题不在文字层面，应转人工 |
| 返工 | 3 次 | 超过则提示审核人与发起人线下沟通 |

（实测数据见 `reports/real_model_report.md`；更换模型后用 `mini check-model` 和一轮 `mini cases` 重新校准。）

## 现场变更预案

改完 `employee.yaml` / `SKILL.md` 先运行 `uv run mini check-config`：规则名、工具名、章节 id、`skill_sections`、字段类型、模板里引用的字段等写错会逐条列出（界面“员工配置”页保存时自动做同样的校验，有错误不写盘）。配置有错误时发起任务会被直接拒绝，不会白跑模型步骤。

| 可能的变更 | 改哪里 |
|---|---|
| 新增质检规则，如“整体经营必须提到客单价” | `employee.yaml` 的 `quality_checks` 加 `keyword_required`（已有注释示例），不改代码 |
| 表单加字段，如“报告接收人” | `employee.yaml` 的 `form`（建议 `required: false`）；报告中展示则在 `report.title` 或 `footer` 中引用 `{字段key}` |
| 表单加字段并要求报告体现，如“重点关注品类” | `form` 中该字段加 `guide`（怎么写）和 `section`（写在哪一章）；模型照做，程序自动加一条质检核对该章节提到了填写的值 |
| 交付格式加 Markdown / HTML / PDF | `report.output_formats: [docx, md, html, pdf]`（docx 保持在首位，审核核对的是它的哈希）；HTML 单文件内嵌图表，PDF 内置中文字体、不依赖 Word |
| 新增报告章节，如“风险提示” | `report.sections` 加一行 `{id: risks, title: 七、风险提示, kind: bullets, guide: "..."}`；撰写与修正的输出 Schema 随配置变化 |
| 改异常阈值 / 摘要条数 | `analysis.anomaly_threshold`；`SKILL.md` 的撰写要求 + `length` 规则参数 |
| 新增一类分析维度 | `analysis.dimensions`（region / store / category / rc） |
| 全新质检逻辑 | `qc/rules.py` 写一个函数并 `@rule` 注册，再在配置中引用 |

## 场景 A 口径（按官方《数据说明》《周报格式要求》）

| 项目 | 做法 |
|---|---|
| 数据读取 | xlsx 读“销售明细”工作表；明细只有门店编码、商品编码时按编码关联“门店信息”“商品信息”补齐区域、店型、开业日期、品类；“销售目标”按周次 + 区域取目标 |
| 主数据 | 按工作表取用，优先级：表单“主数据”上传的文件 > 销售明细 xlsx 中的同名工作表 > 员工配置自带的 `master.xlsx`；门店新开、目标调整时只上传变化的工作表即可；数据质量说明的“口径”写明各表来源 |
| 去重 | 有记录 ID 时按记录 ID 去重（官方数据 2026-10-21 重复上传 29 条，实收 5,629 元），在数据质量说明中写明 |
| 退货 | 销量、实收同为负数视为退货，保留并直接冲减实收；数据质量说明写退货笔数、金额和最大一笔 |
| 指标 | 实收（原价 − 折扣）、环比、毛利率、外卖占比、目标达成率、实收前 5 名单品、区域×品类毛利率变动 ≥ 5 个百分点 |
| 异常 | 区域、门店实收环比超过 ±10% 全部列出；门店与所在区域同向时作为带动门店合并；同店型同向的独立门店合并为“写字楼店（3 家）” |
| 缺数 | 开业后无数据的门店日逐一列出，原因从纪要中找：停业写“属正常停业，不是数据缺失”，系统故障时纪要中的手工台账金额不计入实收 |
| 纪要 | 识别正文样式的“一、二、”章节、“华南（陈某）：”发言、行动项表格（负责人、截止日期）；纪要周次与统计周期不一致时暂停 |
| 篇幅 | Word 正文不超过 3 页：两张图并排、表格 9 号字；程序按版面估算页数（已用 Word 实际分页校准），超出时自动压缩一轮 |
| 金额格式 | 元、取整数（573,635 元）；比例 1 位小数；比例的变动写“个百分点” |

## 待企业确认的问题

1. 主数据的维护方式：目前门店新开、目标调整时由发起人在表单“主数据”中上传（可只含变化的工作表），系统自带的 `master.xlsx` 兜底，是否需要改为由管理员统一维护；
2. 周的定义（当前：ISO 周，周一至周日）；
3. 是否有公司周报 Word 模板；
4. 输入含个人隐私时：直接拒绝（当前）还是脱敏后继续（配置 `pii_policy: redact`：销售明细的隐私列读取时剔除、不落盘，纪要中的手机号 / 身份证号打码后再交给模型，数据质量说明中写明）；
5. 上传错误文件类型时：直接拒绝（当前）还是暂停让发起人重新上传。

企业数据列名与官方数据不同时：差异加到 `admission.column_aliases`；新用例按 `cases/README.md` 格式添加。
