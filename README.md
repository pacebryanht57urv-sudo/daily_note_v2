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

## NAS Data Deployment

推荐把代码和数据分开管理：代码通过 Git 同步，实验数据和报告通过 Synology NAS 或本机外部数据根管理。不要把 `.git`、`.SynologyWorkingDirectory`、`tmp`、原始数据、结果图和报告提交到 Git。

### Lab Measurement Computer

实验室测量电脑负责连接仪器、采集数据，并把数据上传到 NAS。一个常见配置是使用 Synology Drive Client：

```text
Task type: Sync Task
Direction: Upload local changes to Synology Drive Server
Local root example: D:\daily_note_v2
NAS root example: /<share-name>/daily_note_v2
```

建议只同步数据和报告目录，例如：

```text
D:\daily_note_v2\workspace\experiments
D:\daily_note_v2\weekly_report        optional
```

不要同步这些目录：

```text
D:\daily_note_v2\.git
D:\daily_note_v2\.SynologyWorkingDirectory
D:\daily_note_v2\tmp
D:\daily_note_v2\workspace\scripts
D:\daily_note_v2\workspace\skills
```

测量电脑的用户级环境变量示例：

```powershell
[Environment]::SetEnvironmentVariable("DAILY_NOTE_DATA_ROOT", "D:\daily_note_v2\workspace", "User")
[Environment]::SetEnvironmentVariable("DAILY_NOTE_CAMPAIGN", "wafer_measuement\Batch_260515", "User")
[Environment]::SetEnvironmentVariable("DAILY_NOTE_CHIP", "chip7", "User")
```

### Office Analysis Computer

办公室电脑可以不安装 Synology Drive Client，也不必把大数据下载到本地。推荐通过 Tailscale 进入同一虚拟网络，再用 SMB 把 NAS 共享文件夹映射成本地盘符：

```text
Windows Explorer -> This PC -> Map network drive
Drive letter: Z:
Folder: \\<nas-tailscale-name>\<share-name>
Reconnect at sign-in: enabled
```

如果不用 MagicDNS，也可以用占位 IP 形式：

```text
\\<nas-tailscale-ip>\<share-name>
```

办公室电脑的环境变量示例取决于 NAS 共享目录结构。如果映射后的 `Z:` 下直接能看到 `workspace/experiments`，设置：

```powershell
[Environment]::SetEnvironmentVariable("DAILY_NOTE_DATA_ROOT", "Z:\workspace", "User")
```

如果映射后的 `Z:` 下还有一层项目目录，例如 `Z:\daily_note_v2\workspace\experiments`，设置：

```powershell
[Environment]::SetEnvironmentVariable("DAILY_NOTE_DATA_ROOT", "Z:\daily_note_v2\workspace", "User")
```

然后按需设置默认 campaign 和 chip：

```powershell
[Environment]::SetEnvironmentVariable("DAILY_NOTE_CAMPAIGN", "wafer_measuement\Batch_260515", "User")
[Environment]::SetEnvironmentVariable("DAILY_NOTE_CHIP", "chip7", "User")
```

在办公室电脑上，Git 仓库仍建议 clone 到本地磁盘；脚本和 Codex 从本地仓库运行，数据通过 `DAILY_NOTE_DATA_ROOT` 指向 NAS 映射盘。这样写周报或复查 Q 结果时可以直接读取 NAS 上的标准化 `Q/` 文件，而不会把代码、缓存和大体积原始数据混在同一个同步机制里。

## Microcavity Q Measurement Quickstart

这套大扫测 Q workflow 面向现场测量：采集大扫、自动找 dips、选择 full-FSR、分族、拟合色散和 Q，并生成固定格式的单腔 HTML 名片。第一次使用时，先只做 dry run，不连接仪器：

```powershell
python workspace\scripts\microcavity_large_scan\large_scan_flow.py --help
python workspace\scripts\microcavity_large_scan\large_scan_flow.py --chip chip7 --campaign wafer_measuement/Batch_260515 --die die1-1 --cavity c1 --dry-run
```

正式测量前必须确认：

- `DAILY_NOTE_DATA_ROOT` 指向外部数据根，例如群晖映射盘或本机数据盘，不指向 Git 仓库。
- `DAILY_NOTE_CAMPAIGN` 可选，用来给默认 campaign 命名，例如 `wafer_measuement/Batch_260515`。
- `DAILY_NOTE_CHIP` 可选，用来给默认 chip/sample 命名，例如 `chip7`。
- 仪器连接参数正确：TOPTICA 串口、示波器 VISA resource、示波器通道映射。
- 对非 chip7 数据，运行 wrapper 时显式传入 `--disk-fsr-mhz`；chip7 会使用仓库内的设计 FSR helper 作为初始参考。

设置当前 PowerShell 会话的示例：

```powershell
$env:DAILY_NOTE_DATA_ROOT = "D:\daily_note_data"
$env:DAILY_NOTE_CAMPAIGN = "wafer_measuement\Batch_260515"
$env:DAILY_NOTE_CHIP = "chip7"
```

耦合好一个腔后，现场测 Q 的常用入口是：

```powershell
python workspace\scripts\microcavity_large_scan\large_scan_flow.py --die die1-1 --cavity c1
```

如果是其他 chip 或 campaign，显式写清楚：

```powershell
python workspace\scripts\microcavity_large_scan\large_scan_flow.py --campaign my_campaign --chip my_chip --die dieA --cavity c1 --disk-fsr-mhz 205000 --radius-um 125 --gap-um 0.9
```

成功后，正式结果固定在：

```text
<DATA_ROOT>/experiments/<campaign>/results/<chip>/<die>/<cavity>/Q/
```

其中 `Q/` 根目录保存日常查看文件，`Q/evidence/processing_*` 保存可追溯的处理证据：

```text
Q/
  raw.npz
  acquisition.json
  dispersion.png
  d2_fit.png
  family_points.csv
  q_by_mode.csv
  q_trend.png
  mode_spectra.png
  evidence/
    processing_YYYYMMDD_HHMMSS/
      dip_table.csv
      process_summary.json
      dispersion_summary.json
      q_summary.json
      q_fit_examples.png
      raw_health.png
```

单腔名片由脚本生成：

```powershell
python workspace\scripts\microcavity_large_scan\write_cavity_card.py --chip chip7 --die die1-1 --cavity c1
```

名片固定为左侧身份/参数表、中间 Q trend、右侧 sensitivity 图或占位。不要手动把色散图、family map 或 one-FSR 图塞进第三列。

## Office Analysis Commands

办公室电脑不需要连接仪器。只要能通过群晖映射盘或同步目录读取外部数据根，就可以设置：

```powershell
$env:DAILY_NOTE_DATA_ROOT = "Z:\daily_note_data"
```

然后直接读取标准化的 `Q/` 文件做周总结、die 级比较或补图。若只分析已有数据，优先传显式路径，避免误用默认 campaign：

```powershell
python workspace\scripts\microcavity_large_scan\fit_large_scan_q.py --data-path "Z:\...\Q\raw.npz" --family-points-csv "Z:\...\Q\family_points.csv"
```

`config.local.example.json` 提供了本机配置字段示例，方便人和 Codex 对齐默认路径与仪器参数；当前大扫脚本的可靠入口仍是环境变量和命令行参数。

## Collaboration Rules

- 不提交实验记录、图片、报告、原始数据、`.npz`、`.mat`、结果 `.csv` 或结果 `.json`。
- 记录中只引用外部数据路径、样品编号、数据编号和关键结论。
- 改脚本或 workflow 前先检查 `git status --short --branch`。
- 提交前检查 `git diff --cached --stat`，确认 staged 内容只包含代码、skills、规则和配置示例。

