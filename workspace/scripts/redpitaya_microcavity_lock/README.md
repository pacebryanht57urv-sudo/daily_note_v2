# Red Pitaya Microcavity Lock Quickstart

这个文件夹提供 Red Pitaya / PyRPL + TOPTICA DLC PRO 的微腔锁模控制面板和自动锁模脚本。目标是让没有 Codex 的电脑也能直接按固定流程启动。

## 适用范围

可独立使用的主线功能：

- 打开本地 dashboard：`http://127.0.0.1:7880/`
- 检查 RP host
- 启动 / 停止 PyRPL bridge
- 恢复扫频 / 关闭 PID
- 锁定当前已经手动选中的模式
- 根据已有 `Q/best_lock_candidate.json` 或 `Q/q_by_mode.csv` 移动到目标模式并锁定
- 在 `TOPTICA Q / Lock`、`微源光子 Lock`、`RP spectrum / debug` 三种 dashboard 模式之间切换

仍依赖完整仓库其它脚本的功能：

- `Large-Scan Q` 区域的大扫采集、resume、standardize 和 card 刷新依赖 `workspace/scripts/microcavity_large_scan/`。
- 如果只把本文件夹单独复制到另一台电脑，先不要使用这些大扫按钮。

## 文件清单

最小锁模包至少包含：

```text
redpitaya_microcavity_lock/
  launch_pyrpl_bridge_try.bat
  config.local.example.json
  microcavity_control_panel.py
  pyrpl_live_bridge.py
  current_mode_fast_lock.py
  lock_best_q_mode.py
  lock_common.py
  toptica_laser_adapter.py
  weiyuan_laser_adapter.py
  data_paths.py
```

## Python 环境

推荐使用两个 venv，避免 PyRPL/Qt 和 TOPTICA 控制依赖互相污染。

### 1. PyRPL / dashboard 环境

```powershell
py -3.10 -m venv %USERPROFILE%\pyrpl_bridge_venv
%USERPROFILE%\pyrpl_bridge_venv\Scripts\python.exe -m pip install --upgrade pip
%USERPROFILE%\pyrpl_bridge_venv\Scripts\python.exe -m pip install -r requirements-pyrpl.txt
```

这个环境用于启动：

- `microcavity_control_panel.py`
- `pyrpl_live_bridge.py`

### 2. TOPTICA / laser-control 环境

```powershell
py -3.10 -m venv %USERPROFILE%\toptica_lasersdk_venv
%USERPROFILE%\toptica_lasersdk_venv\Scripts\python.exe -m pip install --upgrade pip
%USERPROFILE%\toptica_lasersdk_venv\Scripts\python.exe -m pip install -r requirements-toptica.txt
```

如果 `toptica-lasersdk` 不能从 pip 获取，就按实验室或 TOPTICA 官方 SDK 安装方式装到这个 venv 里。串口控制至少需要 `pyserial`。

## 本机配置

日常配置使用本文件夹下的：

```text
config.local.json
```

第一次双击 `launch_pyrpl_bridge_try.bat` 时，如果这个文件不存在，脚本会自动从 `config.local.example.json` 复制一份，并用记事本打开。改完保存、关闭记事本后，dashboard 会继续启动。

典型配置如下：

```json
{
  "rp_hostname": "RP-f0f213",
  "default_laser_type": "weiyuan",
  "weiyuan_port": "COM5",
  "toptica_port": "COM3",
  "toptica_host": "192.168.1.104",
  "toptica_python": "%USERPROFILE%\\toptica_lasersdk_venv\\Scripts\\python.exe",
  "bridge_base": "http://127.0.0.1:7870",
  "listen_host": "127.0.0.1",
  "listen_port": 7880,
  "scope_type": "none",
  "scope_resource": "TCPIP::192.168.1.8::INSTR",
  "auto_start_bridge": true,
  "open_pyrpl_gui": false
}
```

仍然可以用环境变量临时覆盖少数字段，例如：

```bat
set PYTHON_EXE=%USERPROFILE%\pyrpl_bridge_venv\Scripts\python.exe
set TOPTICA_PYTHON_EXE=%USERPROFILE%\toptica_lasersdk_venv\Scripts\python.exe
set RP_HOSTNAME=RP-f0cb0d
set TOPTICA_HOST=192.168.1.104
set MICROCAVITY_CONTROL_PORT=7880
set DAILY_NOTE_DATA_ROOT=D:\daily_note_data
set PYRPL_BRIDGE_AUTO_START=1
set PYRPL_BRIDGE_GUI=0
```

这些环境变量只适合临时调试；长期本机设置优先写入 `config.local.json`。

常见修改：

- 换 RP：改 `rp_hostname`，优先用裸 hostname，例如 `RP-f0cb0d`，再考虑固定 IP。
- 做 TOPTICA 大扫/Q/锁模：dashboard 里选择 `TOPTICA Q / Lock`。
- 做微源光子当前模式锁模：dashboard 里选择 `微源光子 Lock`，默认串口为 `COM5`。
- 只调试 RP 频谱仪或 scope：dashboard 里选择 `RP spectrum / debug`，只保留 RP bridge 与安全关闭。
- 换 TOPTICA 串口：改 `toptica_port`，或在 `TOPTICA Q / Lock` 模式下改 `Laser COM / port`。
- 换 TOPTICA TCP：dashboard 里把 laser 类型改成 `TOPTICA TCP/IP`，再填 `Laser host / IP`。
- 换微源光子控制器：改 `weiyuan_port`。微源串口为 `9600 8N1`，dashboard 默认从机地址为厂家工具里的通用地址 `255`；如果现场设备要求固定地址，也可以在 dashboard 里改成 `1`。同一时间不能被厂家 `ModuleMonitorTool` 和 dashboard 同时占用。
- 默认启动方式是 `dashboard + headless bridge`，不打开 PyRPL 原生 GUI。需要 PyRPL 原生窗口时，把 `open_pyrpl_gui` 改成 `true`，或在 dashboard 的 `RP bridge action` 里选择 `Start / restart GUI bridge`。

## 启动流程

1. 双击或运行：

   ```powershell
   .\launch_pyrpl_bridge_try.bat
   ```

2. `.bat` 默认会启动 dashboard，并由 dashboard 自动拉起 headless PyRPL bridge。

   如果是第一次运行，`.bat` 会先生成并打开 `config.local.json`；改完保存、关闭记事本后再继续。

3. 如果需要手动重连，先在 `RP bridge action` 里选择 `Check RP host`；确认 RP host 能解析后，选择 `Start / restart headless bridge`。需要 PyRPL 原生窗口时，选择 `Start / restart GUI bridge`。

4. Bridge 启动时会读取 `config.local.json` 里的 `photodetector` 和 `rp_frontend`：
   - `photodetector.scope_response_v_per_w` 用来初始化 PyRPL scope 的光功率响应系数；
   - `rp_frontend.rf_path.external_gain_db` 用来初始化 PyRPL 频谱仪的 `external_gain_db`，即 dBm/dBmHz 显示时需要反扣的前端 RF 增益。
   - 如果没有写这些新字段，旧逻辑仍会保留 `RP-f0cb0d -> 23 dB` 的兼容默认值。

5. 先选 `Experiment mode`。`TOPTICA Q / Lock` 会显示大扫、选模和 Q 表；`微源光子 Lock` 只显示微源串口控制和当前模式锁模；`RP spectrum / debug` 只显示 RP 相关安全控制。

6. 点击 `Refresh status` 检查当前模式需要的仪器状态。

选择 `微源光子` 时，dashboard 会显示一个小控制区，可以读取控制器状态、设置 TEC 温度、设置 LD 电流，并可一键把 LD 设定电流写为 `260 mA`。

在 `微源光子` 模式下点击 `Lock current mode` 会运行 `weiyuan_current_mode_lock.py`：先把 active LD set current 初始化为 `260 mA`，再用 RP 的 1 V 三角扫频找当前模式 dip，并调 LD set current 让 dip 靠近 `out2 = 0`；居中后关闭扫频，设置 PID setpoint、`ival = +1 V`，并用固定负积分方向锁模。

7. 手动把激光器调到目标模式附近后，点击 `Lock current mode`。

## 锁模默认动作

`Lock current mode` 会调用：

```text
current_mode_fast_lock.py
```

默认流程：

- PC piezo 先回到 `75 V`
- RP 用 `1 V` 幅值扫频
- 只要求模式 apparent width 大于下限
- 调 PC 让 dip 接近 `Out2 = 0`
- 关闭扫频后打开 PID
- `pid0.ival = +1 V`，固定使用负积分方向
- 锁住后把积分增益加到 `|I| = 100`
- 只做 2 s 内存监控，不默认保存监控文件

## 常见问题

### 页面默认没有 cavity directory

这是刻意设计的。dashboard 不应该默认显示某台电脑上的本地实验路径。需要用已有 Q 数据锁定最高 Q 模式时，再手动 Browse 到对应 cavity 文件夹。

### `.local` 能 ping 但 bridge 连不上

在某些 Windows/VPN/虚拟网卡配置下，`RP-xxxx.local` 可能解析到错误网卡。优先尝试裸 hostname，例如：

```text
RP-f0cb0d
```

再尝试固定 IP。

### 只想先看 dashboard，不想连接 RP

把 `config.local.json` 里的：

```json
"auto_start_bridge": false
```

然后再运行 bat。这样只启动 dashboard，不自动连接 RP。

### 需要关闭 RP 输出

点击 `Safe off PID/ASG`。这会通过 bridge 关闭 PID 和 ASG 的危险输出。

## 移植建议

如果要给其他人长期使用，建议下一步把本文件夹打成一个独立包，并补上：

- `requirements-pyrpl.txt`
- `requirements-toptica.txt`
- `config.local.example.json`
- 一个不包含大扫按钮的纯锁模 dashboard 模式

当前版本已经能按本 README 手动配置和启动，但还不是严格意义上的 pip package。
