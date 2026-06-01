# Daily Note

这是一个科研 workflow 仓库。当前边界是：Git 只保存代码、工作流规则、项目 skills、配置示例和说明文档；实验记录、图片、报告、原始数据和生成结果放在群晖或本机外部数据根目录，不进入 Git。

默认外部数据根由环境变量 `DAILY_NOTE_DATA_ROOT` 指定。现场脚本仍支持显式传入 `--output-dir` 或输入文件路径；如果脚本需要自动寻找默认结果目录但没有设置 `DAILY_NOTE_DATA_ROOT`，应直接报错，避免把数据写回仓库。

## Repository Contents

```text
.
|-- AGENTS.md
|-- README.md
|-- config.local.example.json
`-- workspace/
    |-- scripts/
    |   |-- microcavity_large_scan/
    |   `-- redpitaya_microcavity_lock/
    `-- skills/
        |-- auto-lock-redpitaya-microcavity/
        |-- daily-summary/
        |-- git-collaboration/
        |-- measurement-session/
        |-- microfabrication-session/
        `-- scientific-plotting/
```

## Data Boundary

- `workspace/scripts/`：采集、处理、分析和仪器 workflow 代码。
- `workspace/skills/`：Codex 可复用的记录、总结、测量和绘图规则。
- `AGENTS.md`：项目级协作规则。
- `config.local.example.json`：可提交的配置示例。
- 外部数据根：实验 session、图片、结果、报告、notes、文献摘录和原始数据。

仓库默认忽略 `workspace/experiments/`、`workspace/reports/`、`workspace/notes/`、`workspace/literature/` 和 `workspace/data/`。这些目录可以作为本机临时工作区或群晖同步挂载点使用，但不作为 Git 资产。

## Current Scripts

大扫测量脚本位于：

```powershell
workspace\scripts\microcavity_large_scan\
```

Red Pitaya / PyRPL 微腔锁模脚本位于：

```powershell
workspace\scripts\redpitaya_microcavity_lock\
```

推荐在测量电脑上设置：

```powershell
$env:DAILY_NOTE_DATA_ROOT = "Z:\daily_note_data"
```

其中 `Z:\daily_note_data` 可以是群晖映射盘、本机数据盘或其他不在 Git 仓库内的目录。

## Collaboration Rules

- 不提交实验记录、图片、报告、原始数据、`.npz`、`.mat`、结果 `.csv` 或结果 `.json`。
- 记录中只引用外部数据路径、样品编号、数据编号和关键结论。
- 改脚本或 workflow 前先检查 `git status --short --branch`。
- 提交前检查 `git diff --cached --stat`，确认 staged 内容只包含代码、skills、规则和配置示例。

