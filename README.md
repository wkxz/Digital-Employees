# 迷你数字员工 · 经营周报员工

锐软科技赛题“迷你数字员工——可审核、可交付的岗位业务 Agent”，场景 A：经营周报。

员工读取门店销售明细（Excel/CSV）和周会纪要（Word/Markdown），汇总分析、找出异常波动及原因，生成带图表和结论的《经营周报.docx》，
经自动质检和人工审核后交付；被退回时按意见返工，缺材料或工具故障时停下来说明原因，不编造、不崩溃。

**核心原则：模型只负责分析和写作；状态流转、准入、数字计算、质检、能否送审和交付全部由程序判定。**

## 快速开始

```bash
make install        # 安装依赖（需要 uv）
make test           # 单元测试 + 39 条用例 + 真实模型调用路径测试（共 66 项，全部离线）
make demo           # 命令行跑一条正常任务，停在“待审核”
make ui             # Web 界面 http://localhost:8501
```

不装 make 时：`uv sync`，然后 `uv run mini ...`、`uv run streamlit run app/streamlit_app.py`。
模拟数据是 2026 年第 44 周，Makefile 已设置 `MINI_TODAY=2026-11-03`；直接调用 `mini` 时也请设置这个环境变量，否则会提示“统计周期尚未结束”。

### 命令行

```bash
export MINI_TODAY=2026-11-03
uv run mini new --period 2026-W44 --file sales_file=data/samples/销售明细_2026W44.xlsx \
                --file minutes_file=data/samples/周会纪要_2026W44.docx
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

然后：

```bash
uv run mini check-model     # 检查连通性、密钥、工具调用、JSON 输出和响应时间
uv run mini cases           # 用真实模型跑全部用例（6 条依赖 Mock 注入错误的用例自动跳过）
```

改回离线 Mock：`.env` 中设 `MINI_MODEL=mock`。单元测试（`make test`）始终使用 Mock，另有一组测试通过本地假 OpenAI 服务覆盖真实模型的 HTTP 调用路径。
用例中 `report_contains` 检查事实与程序生成的文字，任何模型都须满足；`report_contains_mock` 是 Mock 的固定措辞，真实模型下不检查。

## 目录

```
employees/weekly_report/
  employee.yaml        员工结构：表单、准入、步骤、开放工具、质检规则、上限、报告章节（业务人员改这里）
  SKILL.md             员工做法：岗位职责、异常分析、撰写要求、注意事项（Agent Skills 格式）
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
  render.py            占位符替换 + Word / Markdown 生成
  runner.py            用例批量运行器
app/streamlit_app.py   Web 界面：发起任务 / 任务中心 / 审核台 / 事件回放 / 故障注入 / 员工配置
cases/                 39 条自测用例（YAML），其中 U01–U07 基于用户提供的数据（data/user_cases/）
data/samples/          主样例数据（模拟）；data/fixtures/ 用例变体数据
scripts/make_sample_data.py  模拟数据生成器
docs/技术报告.md        技术报告草稿
```

## 设计要点

| 赛题要求 | 实现 |
|---|---|
| 流程与状态流转表，非法流转被拒绝 | `states.py` 12 个状态、`store.transition()` 查表 + 触发方校验 + 闸门；拒绝写 `transition_rejected` 事件 |
| 能否送审 / 交付由程序判定 | 只有 `QC --qc_pass-->` 能到待审核（闸门读检验记录）；只有审核人 `approve` 能交付，且审核的文件哈希 = 交付文件哈希 |
| 退回意见被采纳 | 意见由程序按配置拆成待办（章节 + 关键词），只重做受影响的步骤；新增 `feedback_addressed` 质检逐条校验，不通过不能再次送审 |
| 缺材料暂停、补充后继续 | 准入或执行中发现缺材料 → 待补材料，说明缺什么；补充后重新准入，从断点继续 |
| 员工能力写成配置 | `employee.yaml` + `SKILL.md`，界面“员工配置”页可直接编辑 |
| 轮次与时长上限 | 每步 8 轮 / 120 秒，任务 600 秒，超限暂停转人工（理由见下） |
| ≥3 个工具，明确入参出参，只能调开放的工具 | 7 个 MCP 工具，JSON Schema 入参；模型每步只看到该步开放的工具，越权调用被拒绝并留痕 |
| 工具失败重试、暂停、不编造 | 网关指数退避重试 3 次，耗尽后暂停并提示“第几步、什么原因、已完成 x/y 步”；数字只能用占位符引用 |
| ≥3 类自动质检，可修正，有上限，有记录 | 结构完整、数据一致性、异常归因有据、敏感信息、字数（+ 审核意见已采纳）；程序能确定性修正的先修，其余交模型，最多 2 轮；检验记录随交付包保存 |
| 加分：事件流与断点恢复 | 全部事件只追加写入 SQLite；步骤输出落盘并记录哈希，重启后跳过已完成步骤；界面可回放 |
| 加分：工程加固 | 写文件工具带幂等键，重试不产生重复文件；越权调用留痕；乐观锁防重复点击；路径限定在任务目录 |

### 上限取值理由

| 上限 | 取值 | 理由 |
|---|---|---|
| 单步模型轮次 | 8 | 实测（gemini-3.8-flash，3 轮共 99 个模型步骤）：纪要和常用占位符直接放进输入后，90% 的步骤 1 轮完成、其余 2 轮；8 轮是防循环的上限，第 8 轮收回工具、要求直接给出结果 |
| 单步时长 | 180 秒 | 实测单次调用中位 23 秒、P90 33 秒、最长 57 秒；按最多 3 次慢调用计 |
| 任务总时长 | 600 秒 | 含 2 轮自动修正和一次返工，仍远低于人工写周报的时间 |
| 工具重试 | 3 次，退避 1/2/4 秒 | 覆盖瞬时网络抖动，不长时间阻塞 |
| 质检自动修正 | 2 轮 | 第 1 轮通常能修好数字和结构；2 轮仍不通过说明问题不在文字层面，应转人工 |
| 返工 | 3 次 | 超过则提示审核人与发起人线下沟通 |

（实测数据见 `reports/real_model_report.md`；更换模型后用 `mini check-model` 和一轮 `mini cases` 重新校准。）

## 现场变更预案

| 可能的变更 | 改哪里 |
|---|---|
| 新增质检规则，如“整体经营必须提到客单价” | `employee.yaml` 的 `quality_checks` 加 `keyword_required`（已有注释示例），不改代码 |
| 表单加字段，如“报告接收人” | `employee.yaml` 的 `form`；报告中展示则在 `report.title` 或 `footer` 中引用 |
| 交付格式加 Markdown | `report.output_formats: [docx, md]` |
| 改异常阈值 / 摘要条数 | `analysis.anomaly_threshold`；`SKILL.md` 的撰写要求 + `length` 规则参数 |
| 新增一类分析维度 | `analysis.dimensions`（region / store / category / rc） |
| 全新质检逻辑 | `qc/rules.py` 写一个函数并 `@rule` 注册，再在配置中引用 |

## 待企业确认的问题

1. 周报读者与最关心的 3 个指标；
2. “异常波动”口径（当前：环比 ±15%，区域 / 门店 / 品类 / 区域×品类）；
3. 周的定义（当前：ISO 周，周一至周日）；未营业门店算 0 还是排除；
4. 是否有公司周报模板；
5. 输入含个人隐私时：直接拒绝（当前）还是脱敏后继续（配置 `pii_policy: redact`）；
6. 上传错误文件类型时：直接拒绝（当前）还是暂停让发起人重新上传。

样例数据下发后：列名差异加到 `admission.column_aliases`；5 条公开用例按 `cases/README.md` 格式放到 `cases/enterprise/`。
