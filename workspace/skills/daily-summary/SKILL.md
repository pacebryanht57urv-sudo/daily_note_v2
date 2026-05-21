---
name: daily-summary
description: Use when the user asks to generate, write, or produce a daily work summary, daily report, daily note summary, today summary, or 日总结/今日总结/工作总结. Reads the day's Markdown notes and produces a structured summary.
---

# Daily Work Summary Skill

## Instructions

Read the user's daily Markdown note file and generate a structured Chinese daily summary.

### Input

1. Find the note file at `workspace/notes/<YYYY>/<MM>/<YYYY-MM-DD>.md` for today (or the requested date).
2. Read the full content of that Markdown file.

### Output Format

Generate a Markdown document with these second-level headings (`## `):

```
## 今日概览
3-5 句话概括今天主要做了什么。

## 今日加工流程
仅当今日 Markdown 出现 Fab/器件加工内容时输出。用表格说明今日涉及的光刻胶/材料、工艺用途、样品对象、目标、风险，以及实际执行流程。

## 过程测量与工艺判断
仅当今日 Markdown 出现加工过程测量时输出。用表格整理刻蚀前后深度、胶厚、氧化硅厚度、电阻/导通、显微观察等数据和推导判断。

## 关键进展
按项目/任务归纳实质进展。

## 问题与阻塞
列出未解决点、异常现象、需要补充的信息。

## AI 建议与执行记录
只保留今日 AI 问答、Codex 执行、建议中真正有用的部分。

## 明日建议
3-5 条可执行的下步行动。
```

### Rules

- Only summarize information that actually appears in the Markdown. Do not fabricate experimental results, literature conclusions, or completed tasks.
- Language: Chinese, clear and concise, suitable for research group records.
- If the Markdown contains Fab/process records such as 器件加工、光刻、涂胶、曝光、显影、坚膜、ICP、刻蚀、深硅、镀膜、lift-off、溶脱、除胶、晶圆、样品、台阶仪、椭偏仪、胶厚、氧化硅厚度、电阻 or 导通测试, add `## 今日加工流程` and, when measurements exist, `## 过程测量与工艺判断`.
- In `今日加工流程`, prefer Markdown tables:
  - 加工概要表 fields: 涉及光刻胶/材料, 工艺用途, 样品对象, 今日目标, 关键风险.
  - 工艺流程表 fields: 时间/阶段, 工艺步骤, 材料/参数, 设备/载具, 观察现象, 判断/下一步.
- In `过程测量与工艺判断`, prefer a measurement table with fields: 测量对象, 操作/轮次, 刻蚀前, 刻蚀后, 增量/消耗, 推导指标, 判断.
- Preserve units exactly. Do not turn nm, um, μm, ℃, Pa, sccm, W, mJ/cm², rpm, min, s or 脉冲 into unitless numbers.
- Do simple calculations when the Markdown provides enough data, and show the calculation briefly, e.g. `2024 - 1870.31 = 153.69 nm，约 3.07 nm/脉冲`.
- Mark inferred content with `推断` or `可能`. For missing cells, write `未记录`.
- Do not copy standard SOP parameters into the day summary unless the Markdown says they were used that day.
- If there is no Fab/process content, do not output empty Fab tables.
- If the note file does not exist, report: "今日还没有可整理的 Markdown 记录。"
- If the file exists but is empty, report the same.
- 不要自己保存文件，app 会负责保存。
