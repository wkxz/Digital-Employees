UV ?= uv
RUN = $(UV) run
# 模拟数据为 2026 年第 44 周，演示时把“今天”固定在该周之后
export MINI_TODAY ?= 2026-11-03

.PHONY: install data test cases ui demo transitions check-model check-config clean

install:            ## 安装依赖（需要 uv：https://docs.astral.sh/uv/）
	$(UV) sync

data:               ## 重新生成模拟数据
	$(RUN) python scripts/make_sample_data.py

test:               ## 单元测试 + 全部用例
	$(RUN) pytest -q

cases:              ## 运行自测用例并生成报告 reports/case_report.md
	$(RUN) mini cases

ui:                 ## 启动 Web 界面 http://localhost:8501
	$(RUN) streamlit run app/streamlit_app.py

demo:               ## 命令行跑一遍正常任务（发起 → 质检 → 待审核）
	$(RUN) mini new --period 2026-W44 \
	  --file sales_file=data/samples/销售明细_2026W44.xlsx \
	  --file minutes_file=data/samples/周会纪要_2026W44.docx

check-model:        ## 检查 .env 中的模型配置
	$(RUN) mini check-model

check-config:       ## 检查员工配置（现场改完 employee.yaml / SKILL.md 后先跑）
	$(RUN) mini check-config

transitions:        ## 打印状态流转表
	$(RUN) mini transitions

clean:              ## 清空任务数据
	rm -rf runs
