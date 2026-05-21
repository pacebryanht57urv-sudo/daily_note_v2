# Workflow v2

`workflow/` 是科研记录工作流 v2 的轻量工具包入口。

目标是逐步把核心能力从旧桌面 app 中抽离出来：先保证记录可以稳定写入 Markdown，再继续接入无 UI 监听、日总结、实验 session 和测量分析。

旧 Tk 桌面 app 已归档到 `legacy/v1_app/`，后续默认不要读取或修改 legacy 代码，除非任务明确要求迁移某个能力。

## 当前入口

### 统一 CLI

推荐优先使用：

```powershell
python -m workflow record "记录：今天测试了 Red Pitaya PID"
python -m workflow record "提问：为什么锁模后噪声变大？"
python -m workflow record "新增待办任务：整理不同 PID 参数下的噪声谱"
python -m workflow record "器件测量：P=0.1 mW" --date 2026-05-16 --time 14:30
```

`record` 会解析飞书风格文本命令，然后写入 `workspace/notes/YYYY/MM/YYYY-MM-DD.md`。

也可以启动一个最小无 UI 监听模式，从标准输入逐行读取消息：

```powershell
python -m workflow listen --stdin
```

启动后每行输入一条 `记录：...` / `提问：...` / `新增待办任务：...`，程序会持续写入 notes。这个模式只用于验证监听链路，尚未接入飞书 API。

启动无 UI 飞书远程记录：

```powershell
python -m workflow listen --feishu
```

它会读取项目根目录的 `config.local.json`、`config.json`、`.env` 或环境变量中的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，通过飞书 WebSocket 接收文本消息，去重后写入 `workspace/notes/`。如果不想让机器人回“已记录”，加 `--no-reply`。

当飞书消息是 `提问：...` / `问题：...` / `求助：...` 时，listener 会继续读取 `OPENAI_API_KEY`、`OPENAI_MODEL`、`OPENAI_BASE_URL`，把当天 Markdown 记录作为上下文发给远端 API，并把回答回到飞书；同时在 Markdown 中追加一条 `AI问题解答` 简答记录。没有配置 `OPENAI_API_KEY` 时，只记录问题，不生成回答。

需要承接上一轮回答时，用显式追问：

```text
追问：那如果胶厚只剩 4 um，还能继续刻蚀 100 脉冲吗？
```

`追问：...` 会额外带上当天 Markdown 中最近的 `问题求助` 和 `AI问题解答`，用于承接上一轮判断。

`追问 xxx` 这种省略冒号的写法也支持。

更自然的方式是直接在飞书里回复机器人上一条 AI 回答。listener 会根据飞书的 `parent_id/root_id` 找到上一轮问题和回答，把这条回复自动当作追问处理。

生成某一天的日总结 prompt：

```powershell
python -m workflow report daily --date 2026-05-15 --output tmp/daily-2026-05-15.prompt.md
```

这个命令只打包 Markdown 记录和 daily-summary skill 路径，不调用模型、不生成 Word。

创建实验 session 目录：

```powershell
python -m workflow session create red-pitaya-pid-noise --date 2026-05-18 --title "Red Pitaya PID 噪声谱测量"
```

默认会创建 `workspace/experiments/YYYYMMDD-name/`，并包含 `data/`、`scripts/`、`figures/`、`results/` 和 `README.md`。

### 底层模块

`workflow.notes`：Markdown 写入核心。

```powershell
python -m workflow.notes add --text "测试记录"
python -m workflow.notes add --kind todo --text "测量 Q 值"
python -m workflow.notes add --kind question --text "为什么锁定后噪声变大"
```

`workflow.listener`：远程文本命令解析与本地写入。

```powershell
python -m workflow.listener.commands "记录：测试"
python -m workflow.listener.record "记录：测试"
python -m workflow.listener.service --stdin
python -m workflow.listener.service --feishu
```

后续无 UI 飞书 listener 会复用这一层：收消息、解析文本、写入 `workspace/notes/`。

`workflow.reports`：报告生成前的 prompt 打包。

```powershell
python -m workflow.reports --date 2026-05-15
```

`workflow.sessions`：实验 session 目录创建。

```powershell
python -m workflow.sessions red-pitaya-pid-noise --date 2026-05-18
```
