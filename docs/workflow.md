# LangGraph 编排图

由 `uv run mini graph` 生成。任务编排图的边由状态流转表推出（`workflow.exits()`），与状态机一一对应；步骤图的边来自 `employee.yaml` 中步骤的 `after`。实线为固定边，虚线为条件边。

## 任务编排图

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	created(created)
	admission(admission)
	running(running)
	rework(rework)
	qc(qc)
	auto_fix(auto_fix)
	wait_input(wait_input)
	suspended(suspended)
	review(review)
	__end__([<p>__end__</p>]):::last
	__start__ -.-> __end__;
	__start__ -.-> admission;
	__start__ -.-> auto_fix;
	__start__ -.-> created;
	__start__ -.-> qc;
	__start__ -.-> review;
	__start__ -.-> rework;
	__start__ -.-> running;
	__start__ -.-> suspended;
	__start__ -.-> wait_input;
	admission -.-> __end__;
	admission -.-> running;
	admission -.-> suspended;
	admission -.-> wait_input;
	auto_fix -.-> __end__;
	auto_fix -.-> qc;
	auto_fix -.-> suspended;
	created -.-> __end__;
	created -.-> admission;
	qc -.-> __end__;
	qc -.-> auto_fix;
	qc -.-> review;
	qc -.-> suspended;
	review -.-> __end__;
	review -.-> qc;
	review -.-> rework;
	review -.-> suspended;
	rework -.-> __end__;
	rework -.-> qc;
	rework -.-> suspended;
	rework -.-> wait_input;
	running -.-> __end__;
	running -.-> qc;
	running -.-> suspended;
	running -.-> wait_input;
	suspended -.-> __end__;
	suspended -.-> admission;
	suspended -.-> auto_fix;
	suspended -.-> created;
	suspended -.-> qc;
	suspended -.-> rework;
	suspended -.-> running;
	wait_input -.-> __end__;
	wait_input -.-> admission;
	admission -.-> admission;
	auto_fix -.-> auto_fix;
	created -.-> created;
	qc -.-> qc;
	review -.-> review;
	rework -.-> rework;
	running -.-> running;
	suspended -.-> suspended;
	wait_input -.-> wait_input;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## 步骤图（running / rework 节点内部，按 employee.yaml 中步骤的 after 构建，同一层的步骤并行执行）

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	load(load)
	kpi(kpi)
	minutes(minutes)
	charts(charts)
	analysis(analysis)
	draft(draft)
	render(render)
	finish(finish)
	__end__([<p>__end__</p>]):::last
	__start__ --> load;
	__start__ --> minutes;
	analysis --> draft;
	charts --> render;
	draft --> render;
	kpi --> analysis;
	kpi --> charts;
	load --> kpi;
	minutes --> analysis;
	render --> finish;
	finish --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## 模型步骤子图

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	model(model)
	tools(tools)
	parse(parse)
	__end__([<p>__end__</p>]):::last
	__start__ --> model;
	model -.-> parse;
	model -.-> tools;
	parse -.-> __end__;
	parse -.-> model;
	tools --> model;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
