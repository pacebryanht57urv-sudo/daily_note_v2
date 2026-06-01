# 科研 workflow 仓库规则

## 核心定位

本仓库只保存代码和 workflow：采集/处理/分析脚本、Codex skills、协作规则、README 和配置示例。实验记录、图片、报告、notes、文献摘录、原始数据和生成结果不再作为 Git 资产；它们应放在群晖或本机外部数据根目录中。

默认外部数据根由 `DAILY_NOTE_DATA_ROOT` 指定。脚本可以继续接受显式输入路径和 `--output-dir`；如果需要自动寻找默认数据目录但没有设置 `DAILY_NOTE_DATA_ROOT`，应给出清晰错误，不得默认写入本仓库。

## 最小目录边界

- `workspace/scripts/`：可复用的采集、处理、绘图、拟合和仪器控制脚本。
- `workspace/skills/`：项目内可复用的记录、总结、测量、绘图和 Git 协作 skill。
- `AGENTS.md`、`README.md`：项目级规则和入口说明。
- `config.local.example.json`：可提交的本地配置示例。
- 外部数据根或群晖：`experiments/`、`notes/`、`reports/`、`literature/`、`data/` 等记录和数据目录。

`workspace/experiments/`、`workspace/reports/`、`workspace/notes/`、`workspace/literature/`、`workspace/data/` 在本仓库内默认忽略。它们可以在本机存在，作为迁移前缓存或群晖挂载点，但不得进入 Git。

## 科研级严谨协作原则

- Codex 不能为了迎合用户而直接接受观点；应先检查假设、反例、风险和替代解释，再给出判断。
- 功能开发必须优先判断是否减少用户心智负担；不能因为“可以实现”就增加命令、状态、后台服务或维护成本。
- 日常记录必须区分事实记录、现场判断、AI 建议和待确认事项，不能把观察、推测和建议混写成同一类结论。
- 当 Codex 无法确定一条记录应放入哪个 section 时，必须先向用户追问；不得擅自归类。
- 科研记录以凝练、可追溯、可复盘为目标；自然语言是主交互，外部 Markdown 记录是真实状态。

## 默认记录方式

- 新建实验、测量或加工 session 时，记录目录应建在 `DAILY_NOTE_DATA_ROOT` 下的外部 `experiments/YYYY-MM-DD/<session-name>/`，而不是 Git 仓库内。
- 每个外部 session 可按需包含 `session.md`、`figures/`、`images/`、`results/` 和外部数据路径说明。
- 密集现场记录阶段，Codex 默认先在对话中轻量归类和必要追问；待用户明确说“整理一下 / 写进 session / 今天收口 / 落盘”，或一段内容明显结束后，再批量更新外部 `session.md`。
- 每采完一组新测量数据后，Codex 应先和用户一起判断规律、有效性、异常点和下一步意义；确认有效或明确标记无效后，再写入外部记录。
- 原始大数据、运行产物、索引、密钥配置、仪器 autosave 状态不得提交到 Git。

## 脚本与数据路径

- 大扫测量脚本位于 `workspace/scripts/microcavity_large_scan/`。
- Red Pitaya / PyRPL 微腔锁模脚本位于 `workspace/scripts/redpitaya_microcavity_lock/`。
- 现场采集、绘图、扫参等重复代码应优先复用已有脚本；实验差异通过参数、元数据和外部结果目录表达。
- 脚本默认输出必须落到显式 `--output-dir` 或 `DAILY_NOTE_DATA_ROOT` 派生目录。不要根据脚本所在位置构造 repo 内 `results/`。

## Skill 使用

- 示波器/频谱仪/锁模/PID/Red Pitaya/PyRPL 等测量现场记录，优先读取 `workspace/skills/measurement-session/SKILL.md`。
- Red Pitaya/PyRPL + TOPTICA DLC PRO + 微腔透射自动锁模流程，优先读取 `workspace/skills/auto-lock-redpitaya-microcavity/SKILL.md`。
- 科研绘图、数据图、锁模过程图、报告图或组会图，优先读取 `workspace/skills/scientific-plotting/SKILL.md`。
- 微加工/器件加工/ICP/深硅刻蚀/除胶/裂片等现场加工记录，优先读取 `workspace/skills/microfabrication-session/SKILL.md`。
- 每日总结、周总结和组会材料从外部 notes、experiments、literature 和 reports 中读取，不从运行日志或索引中推断事实。

## Git 协作防漏规则

- 在任何实验、测量、加工、报告或规则修改开始前，Codex 应先确认 `git status`、当前分支和远程同步状态。
- 完整 workflow、`AGENTS.md`、`README.md`、`workspace/skills/` 或脚本重构等重要改动，默认从最新 `main` 新建分支；现场连续分支应明确是否为堆叠分支。
- 涉及 Git 协作、分支、PR、跨终端同步或提交前检查时，优先读取 `workspace/skills/git-collaboration/SKILL.md`。
- 提交前必须确认 staged 内容只包含代码、workflow、skills、规则和配置示例；不得包含 session、图片、报告、`.npz`、`.mat`、结果 `.csv` 或结果 `.json`。
- PR 合并后，提醒用户删除已合并远程分支，并在本地 `git switch main`、`git pull`、`git branch -d <branch>` 清理。

## AI 协作与执行策略

- Codex 默认独立完成分析、实现、记录和总结；只有用户明确要求“交给 opencode”“生成给 opencode 的方案”或“多 agent 协作”时，才启用多 AI 协作流程。
- 需要交给 opencode 时，Codex 输出短计划，必须包含 `Goal`、`Scope`、`Reuse`、`Steps`、`Checks`、`Stop`。
- 关键主线功能、高风险重构、安全相关改动，或用户明确要求 review 时，Codex 应做审查。

## 执行安全与边界

- 严谨性优先于节省 token；必要的上下文读取、核查、测试和追问不应省略。
- 读取、测试和修改必须服务于用户目标，不顺手重构或扩大无关范围。
- 新能力优先沉淀为自然语言记录约定、session 模板或项目 skill；只有当重复操作稳定且确实减少心智负担时，才考虑生成小工具。
- 遇到限流、模型服务错误、编码错误时，先停止并说明原因，不连续重试。

