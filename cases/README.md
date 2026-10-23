# 自测用例

每个 `*.yaml` 是一条用例，`make cases` 或 `uv run mini cases` 批量运行，结果写入 `reports/case_report.md`；
`uv run pytest` 也会把全部用例作为测试运行。

## 格式

```yaml
id: N01
name: 用例名称
category: 正常 | 边界 | 缺材料 | 不合规 | 审核流转 | 故障 | 断点恢复
env: {MINI_TODAY: "2027-01-12"}          # 可选：覆盖环境变量
crash_after: analysis                     # 可选：在该步骤完成后强制结束进程，再断点恢复
input:
  period: 2026-W44
  fields: {focus_region: 华东}            # 其他表单字段
  files: {sales_file: data/samples/销售明细_2026W44.xlsx, minutes_file: ...}
  faults: [{tool: search_minutes, mode: timeout, times: 2}]   # 故障注入
steps:                                    # 依次执行
  - expect: {state: PENDING_REVIEW, report_contains: [...]}
  - review: {decision: changes, comments: "..."}   # 或 decision: approve
  - supplement: {files: {minutes_file: ...}}
  - retry: {clear_faults: true}
  - recheck: {}
  - cancel: {}
  - illegal: {event: approve, actor: initiator}     # 断言该操作被拒绝且状态不变
  - compare_with_clean_run: {}                      # 断言结果与一次跑完的任务一致
```

`expect` 支持：`state`、`version`、`pause_contains`、`report_contains`、`report_not_contains`、`events`、
`event_min`、`step_runs`（某步骤执行次数）、`qc_failed_seen`、`qc_all_blocking_passed`、`deliverable_files`、
`kpis`（与独立核算的基准值比对，金额误差 ≤ 0.01）、`anomaly_labels`、`state_path`。

## 故障注入

| tool | mode | 效果 |
|---|---|---|
| 任一工具名 | timeout / error | 该工具调用失败（可重试错误）；`times: n` 表示只失败前 n 次 |
| 任一工具名 | missing | 该工具报缺材料 |
| llm | timeout | 模型接口不可用 |
| mock | direction_error / hardcode_number / forbidden_tool / loop_tools / bad_json_once / stubborn | Mock 模型的典型错误，见 `src/mini_employee/llm/mock.py` |

## 企业用例

企业下发 5 条公开样例用例后，按上述格式放到 `cases/enterprise/`，运行 `uv run mini cases cases/enterprise`。
