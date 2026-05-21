# 科研工作流

这是一个每日科研记录与任务上下文系统。当前项目正在从 v1 桌面 app 过渡到 v2 轻量 workflow 工具包。

- v1 桌面 app 已复制封存到 `legacy/v1_app/`，用于展示工作流建立过程。
- `app/` 暂时保留并仍可运行，但视为过渡/legacy 代码；除非确有必要，后续不再把新能力加到旧 app 中。
- v2 默认开发入口是 `workflow/`、`workspace/skills/` 和 `workspace/experiments/`。
- `workspace/` 仍是长期事实来源，保存记录、实验代码、文献启发和报告草稿。

## 启动

```powershell
python run_daily_note.pyw
```

或双击 `run_daily_note.bat`。

## 目录

```text
workflow/               v2 轻量工具包入口，后续承载 listener、reports、measurement
legacy/v1_app/          v1 桌面 app 归档展示目录，默认不再开发
app/                    过渡保留的旧桌面 app，暂时仍可运行
workspace/notes/         每日 Markdown 记录
workspace/experiments/   实验采集、绘图、分析代码
workspace/literature/    文献启发
workspace/reports/       日总结、周报、组会材料
workspace/indexes/       飞书索引和状态
config.local.json        本机配置和密钥，不建议同步
AGENTS.md                项目边界和 agent 规则
```

## 使用原则

- 记录和实验产物以 `workspace/` 为核心，不绑定旧 app。
- 新能力优先做成 v2 CLI/后台服务/skill，而不是继续扩展 Tk UI。
- 旧 app 只作为临时查看器和 v1 展示材料，除非迁移需要，不主动读取 legacy 代码。
- 测量和分析代码优先沉淀到 `workspace/experiments/` 的 session 目录。

## Codex 协作

需要把“高智慧规划”和“低成本执行”分开时，使用双线程流程：`gpt-5.5` 负责输出计划，`gpt-5.3-codex` 负责按计划执行。具体提示词和操作步骤见 `docs/codex-two-thread-workflow.md`。

飞书用于外场快速输入和提问。常用命令：

```text
记录：内容
想法：内容
杂事：内容
新增待办任务：内容
提问：问题
关联待办：关键词 内容
```

## 飞书 CLI listener

启动或重启后台监听：

```powershell
.\scripts\start-feishu-cli-listener.ps1 -Restart
```

安装当前用户登录后自动启动：

```powershell
.\scripts\install-feishu-cli-listener-startup.ps1
```

在飞书里发送：

```text
系统状态
当前 session
最近 session
切换 session：关键词
恢复上一个 session
我判断：内容
问AI：问题
结束当前 session
```
