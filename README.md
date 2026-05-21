# Daily Note v2

这是一个面向科研工作的本地记录与上下文系统。当前重点不是做一个复杂 app，而是把每天的实验、加工、测量、判断、AI 建议和后续计划沉淀成可追溯的 Markdown 资产。

核心目标：

- 让自然语言记录成为主要交互方式。
- 把事实记录、现场判断、AI 建议和待确认事项分开。
- 把实验 session、图片证据、参数和后续测试计划组织到同一目录中。
- 支持后续生成日总结、周总结和组会材料。
- 优先沉淀轻量 workflow、项目 skill 和 session 约定，而不是继续扩展旧桌面 app。

## 当前定位

本仓库是 `daily_note_v2` 的工作区快照，当前主要包含：

- 项目协作规则：`AGENTS.md`
- 轻量 workflow 入口：`workflow/`
- 项目内可复用 skills：`workspace/skills/`
- 实验和微加工 session：`workspace/experiments/`
- 配置示例：`config.local.example.json`

需要注意：当前仓库中的 `workflow/` 仍处于 v2 轻量工具包建设阶段，不应把它理解为完整稳定的自动化系统。现阶段更可靠的主线是：用自然语言记录现场过程，由 Codex 整理成结构化 session，并把关键图片保存为 session 资产。

## 目录结构

```text
.
|-- AGENTS.md
|-- README.md
|-- config.local.example.json
|-- workflow/
|   |-- README.md
|   |-- __init__.py
|   `-- __main__.py
`-- workspace/
    |-- experiments/
    |   |-- README.md
    |   `-- 2026-05-20/
    |       `-- deep_si_etch_pku_changping/
    |           |-- session.md
    |           `-- images/
    `-- skills/
        |-- daily-summary/
        `-- microfabrication-session/
```

## 记录原则

日常记录遵循四类信息分离：

- 事实记录：实际发生的操作、参数、测量结果。
- 现场判断：当时对原因、风险、趋势的判断。
- AI 建议：AI 给出的解释、下一步建议或风险提醒。
- 待确认事项：尚不确定、需要复查或后续测量的信息。

当信息缺口会影响计算、归类、参数有效性或后续决策时，应在现场即时追问，而不是等到晚上总结时统一补。

## 微加工 session

微加工、器件加工、ICP、深硅刻蚀、除胶、裂片等现场记录，遵循：

[workspace/skills/microfabrication-session/SKILL.md](workspace/skills/microfabrication-session/SKILL.md)

典型 session 结构：

```text
workspace/experiments/YYYY-MM-DD/<process-name>/
  session.md
  images/
```

`session.md` 应记录：

- 基本信息和样品命名规则。
- recipe、气压、功率、流量、温度、cycle 数。
- 标定片和正式样品的区别。
- 装载方式、压环、载片、固定介质和异常风险。
- 加工过程中的阶段观察。
- 后处理、除胶、裂片结果。
- 后续测试计划与关注点。
- 待确认事项。

现场图片、显微图和加工后照片应保存到 session 的 `images/` 目录中，并在 `session.md` 中引用。图片不应只留在聊天附件、Lark 缓存或临时路径里，因为后续周总结和组会材料需要图文并茂。

示例：

[workspace/experiments/2026-05-20/deep_si_etch_pku_changping/session.md](workspace/experiments/2026-05-20/deep_si_etch_pku_changping/session.md)

## 日总结与周总结

日总结 skill 位于：

[workspace/skills/daily-summary/SKILL.md](workspace/skills/daily-summary/SKILL.md)

后续总结应优先读取：

- `workspace/notes/`：每日自然语言原始记录。
- `workspace/experiments/`：实验、测量和加工 session。
- `workspace/skills/`：项目内总结和记录方法。

周总结和组会材料应从 session 的 `images/` 中选图，而不是回聊天记录中查找图片。

## 不提交的内容

`.gitignore` 默认排除：

- 本地密钥和机器配置：`config.local.json`
- 运行索引和状态：`workspace/indexes/`
- 每日原始 notes：`workspace/notes/`
- 生成报告：`workspace/reports/daily/`
- 临时聊天附件：`.codex-remote-attachments/`
- 原始大数据和本地临时文件

原则上，仓库中保存的是轻量、可复盘、可汇报的上下文资产；原始大数据只记录路径或编号，不默认同步进仓库。

## 与 Codex 协作

在这个项目里，Codex 的默认职责是：

- 读取项目上下文和已有规则。
- 帮助把自然语言记录整理成 Markdown session。
- 对关键缺失信息即时追问。
- 区分事实、判断、建议和待确认事项。
- 保存重要图片到 session 目录。
- 在生成正式记录前做短收口：确认关键照片、可测 chip/die 数量、下一步是测量还是继续加工。
- 给出少量基于记录的下一步建议。

如果是中等以上复杂度的功能开发，应先写清楚计划、修改范围、复用模块、测试方式和停止条件，再进入执行。

## 当前状态

当前仓库已经包含一个完整的深硅刻蚀加工 session 示例，展示了：

- Bosch 深硅刻蚀参数记录。
- SiO2 消耗速率标定。
- Si 刻蚀速率估算。
- 正式样品装载和硅油污染异常记录。
- 230 cycle 后停止刻蚀的现场决策。
- 除胶、裂片和 chip/die 命名体系。
- 图片资产化保存。

下一步更适合继续完善：

- chip/die 级测试表。
- 测腔性能记录模板。
- 周总结图文生成流程。
- `workflow/` 中稳定可用的 session 和 report CLI。
