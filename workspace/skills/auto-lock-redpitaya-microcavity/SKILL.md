---
name: auto-lock-redpitaya-microcavity
description: Use for the Red Pitaya/PyRPL + TOPTICA DLC PRO microcavity transmission auto-lock workflow, especially when the user asks to lock the current mode, continuing the external locking session, judging apparent linewidth/lockpoint/PID handoff quality, or recording lock attempts.
---

# Auto Lock Red Pitaya Microcavity

## Scope

Use this skill only for the current Red Pitaya/PyRPL + TOPTICA DLC PRO + microcavity transmission locking setup.

In a new thread, first read:

```text
$DAILY_NOTE_DATA_ROOT/experiments/2026-05-22/auto_lock_redpitaya_microcavity/session.md
```

Then use this skill as the operational shortcut. It works together with:

- `workspace/skills/measurement-session/SKILL.md` for one group, one judgment, one record.
- `workspace/skills/scientific-plotting/SKILL.md` for readable lock traces and summary figures.
- `workspace/skills/git-collaboration/SKILL.md` before committing or syncing rule/session changes.

Recommended user prompt for reuse:

```text
继续使用 workspace/skills/auto-lock-redpitaya-microcavity/SKILL.md 里的 Red Pitaya + TOPTICA 微腔锁模流程；如果我说“锁一下/锁模”，默认当前还没锁住，直接走当前模式快速锁定流程。
```

Default command semantics:

- 当用户说“锁一下”“锁模”“把这个模式锁上”时，默认用户给的是未锁定状态；不要先绕去验证“是不是已经锁住”。
- 默认执行下面的 **Current-Mode Fast Lock Workflow**。先把 PC piezo 设为 `75 V`，再做必要的扫频、PC 居中、PID handoff 和 2 s 稳定性确认。
- 不默认重跑大范围 ARC/PC 预调，不默认扫多轮 trace，不默认保存 monitor 文件。

## System Map

Default signal and control meanings:

| Channel | Meaning |
|---|---|
| `in1` / scope `CH1` | Photodetector transmission signal |
| `out2` / scope `CH2` | Red Pitaya control voltage sent to the laser control input |
| `asg0` | Ramp sweep output to `out2` |
| `asg1` | Trigger helper, not physically output |
| `pid0.input` | `in1` |
| `pid0.output_direct` | `out2` |

Default operational path:

```text
workspace/scripts/redpitaya_microcavity_lock/src/lock/current_mode_fast_lock.py
```

Key dependencies reused by the locking workflow:

- `workspace/scripts/redpitaya_microcavity_lock/src/dashboard/microcavity_control_panel.py`: local dashboard for stable Q/lock operations. It exposes current-mode lock, selected-mode dry-run, move-to-target, Q table selection, and safe-off buttons while delegating hardware work to the existing scripts.
- `workspace/scripts/redpitaya_microcavity_lock/src/lock/current_mode_fast_lock.py`: default TOPTICA one-command current-mode lock; PC starts at 75 V, ASG sweep amplitude defaults to 1.0 V, bounded low-width ARC exception, PC centering, fixed negative-I PID handoff, final 2 s live monitor.
- `workspace/scripts/redpitaya_microcavity_lock/src/lock/weiyuan_current_mode_lock.py`: micro-source current-mode lock; initialize active LD set current to 260 mA, center the dip by tuning LD set current, then reuse the same fixed negative-I PID handoff.
- `workspace/scripts/redpitaya_microcavity_lock/src/lock/lock_best_q_mode.py`: after Q fitting, read `Q/best_lock_candidate.json` or fall back to `Q/q_by_mode.csv`, move TOPTICA to the highest-Q0 fitted mode, set PC to 75 V before wavelength movement, then run `current_mode_fast_lock.py`.
- `workspace/scripts/redpitaya_microcavity_lock/src/lock/lock_common.py`: shared bridge/TOPTICA/trace-analysis helpers used by the lock scripts.
- `workspace/scripts/redpitaya_microcavity_lock/src/bridge/pyrpl_live_bridge.py`: local HTTP bridge for Red Pitaya/PyRPL control. It supports the default `--headless` mode for dashboard-only operation and optional GUI mode for PyRPL-native debugging.
- `workspace/scripts/redpitaya_microcavity_lock/src/drivers/toptica_laser_adapter.py`: TOPTICA serial/TCP adapter. In serial current-mode locking, reuse one COM session during the run and use the short serial timeout path; do not open/close COM3 for every PC/ARC readback.

## Before Running

Check the minimum safe context before touching live outputs:

1. Remind the user to check branch, status, and sync state for important session or skill changes.
2. Confirm the PyRPL live bridge is already running and reachable at `http://127.0.0.1:7870`.
3. Confirm the target is the current optical mode; never reuse an old lockpoint after polarization, coupling, PC piezo, ARC factor, or mode selection changed.
4. Prefer the existing scripts over new code. Do not create another one-off locking script unless the current workflow cannot express the needed test.
5. Treat GUI/debug configs such as `global_config`, `new1234`, `new123456`, and similar PyRPL configs as user-owned.

## Main Workflow

### Local Dashboard

When using `launch_pyrpl_bridge_try.bat`, the batch file starts the local dashboard first:

```text
http://127.0.0.1:7880/
```

Before first use on a new computer, run `workspace/scripts/redpitaya_microcavity_lock/install_microcavity_control.bat`. The installer checks for a valid Python + PyRPL `0.9.8.0` runtime and otherwise creates a managed user-level environment under `%LOCALAPPDATA%\MicrocavityControl\envs\...`. It writes the selected runtime to the ignored local file `runtime.local.json`. Use `install_microcavity_control.bat -Reset -ForceManaged` to rebuild the managed runtime from scratch.

The launcher reads `runtime.local.json` and starts dashboard/bridge scripts with that runtime. Do not recreate a package-local `.venv` as the default deployment path. The dashboard and bridge status should show the live Python/PyRPL path so environment mix-ups can be diagnosed quickly.

The batch file reads `workspace/scripts/redpitaya_microcavity_lock/config.local.json` for machine-specific defaults. On first run, it copies `config/config.local.example.json` to `config.local.json` and opens it in Notepad; the user should edit RP hostname, laser type, COM ports, and whether to open the PyRPL GUI there. Do not hard-code a user's local RP hostname or COM port into the batch file.

The dashboard is the preferred entry point for the PyRPL bridge:

- First choose `Experiment mode`:
  - `TOPTICA Q / Lock`: TOPTICA large-scan Q, Q-table target selection, selected-mode movement, and TOPTICA current-mode lock.
  - `微源光子 Lock`: micro-source serial temperature/current controls and current-mode lock by LD-current centering; TOPTICA large-scan/Q-table controls are hidden.
  - `RP spectrum / debug`: RP bridge/status/safe-off only for spectrum, scope, or temporary debugging.
- `RP bridge action -> Check RP host`: resolve the RP hostname before starting PyRPL.
- `.bat` normally starts the dashboard with `--auto-start-bridge`, so the dashboard starts a managed headless PyRPL bridge automatically.
- `RP bridge action -> Start / restart headless bridge`: start or restart the dashboard-managed bridge without opening the PyRPL Qt GUI.
- `RP bridge action -> Start / restart GUI bridge`: restart the bridge with the PyRPL Qt GUI, only when native PyRPL debugging is needed.
- `RP bridge action -> Refresh bridge status`: check whether the bridge is reachable.
- `RP bridge action -> Stop bridge`: stop only the bridge process that was started by this dashboard.
- Use bare RP hostnames such as `RP-f0cb0d` before fixed IPs or `.local` names when possible. On this Windows setup, `.local` may resolve through a virtual adapter; the bare hostname avoids that failure mode.
- When the RP host is `RP-f0cb0d`, the dashboard starts the spectrum analyzer display with `external_gain_db = 23 dB`, matching the calibrated bias-tee + amplifier RF path. The fixed RP high-Z correction of `6.0206 dB` remains enabled by default.

Use it only for stable automated microcavity operations:

- `Lock current mode`: user has already selected the optical mode; runs `current_mode_fast_lock.py`.
- In `微源光子 Lock` experiment mode, `Lock current mode` runs `weiyuan_current_mode_lock.py` instead of the TOPTICA script. It does not use PC or ARC.
- `Selected mode`: choose a cavity directory, then pick highest `Q0`, nearest 1550 nm, or a row/manual wavelength.
- `Dry-run target`: verify the selected candidate and planned wavelength without touching hardware.
- `Move to target wavelength`: safe-off RP outputs, set PC to 75 V, move TOPTICA to the selected wavelength, and stop before PID lock.
- `Restore sweep / PID off`: after a lock attempt or manual interruption, disable PID, restore the 50 Hz, 1 V pre-lock ramp sweep on `out2`, set scope input/trigger for the current-mode sweep view, and request PyRPL scope continuous run.
- `Safe off PID/ASG`: turn off the risky RP outputs through the live bridge.

Keep exploratory experiments such as PD comparison, ultrasound sensitivity scans, or report plotting out of this dashboard until those workflows become stable enough to automate.

### Current-Mode Fast Lock Workflow

Use this as the default rhythm for each simple user lock request:

Run the one-command script, then inspect the final 2 s monitor summary:

```powershell
C:\Users\win10\toptica_lasersdk_venv\Scripts\python.exe workspace\scripts\redpitaya_microcavity_lock\src\lock\current_mode_fast_lock.py
```

Internally it follows this rhythm:

1. Set TOPTICA PC piezo to `75 V` as the fixed starting point for every user-requested lock.
2. Configure ASG sweep with `amplitude = 1.0 V`, force PyRPL scope `run_continuous = true`, and acquire a fresh scope trace of the current mode.
3. Analyze only the down-sweep segment by default.
4. Check apparent full width with only the lower limit:
   - accept if `full_width >= 0.08 V`;
   - no upper width cap for the normal lock workflow;
   - do not tune ARC just because the mode is wide.
5. Keep ARC unchanged unless the width is below the lower limit or the user explicitly asks for ARC/PC pretuning.
6. Tune only PC when the dip minimum is not near `Out2 = 0`:
   - use the existing proportional centering rule, such as `pc_next = pc + 0.8 * arc_factor * dip_out2`;
   - repeat sweep/centering until `abs(dip_out2) <= 0.03 V` or a small iteration cap is reached.
7. Recompute the PID setpoint from the fresh centered sweep using dip-rise 1/4.
8. Turn off ASG sweep, zero PID `p`/`i`, reset `pid0.inputfilter = 0`, set `pid0.setpoint`, set `pid0.ival = +1 V`, set `pid0.output_direct = out2`.
9. Enable PID with `p = 0.01`, `i = -1`; with `pid0.ival = +1 V`, negative integral direction is the fixed correct direction for this setup.
10. Increase the integral magnitude directly to `i = -100`.
11. Do not auto-flip the integral direction during the normal current-mode workflow.
12. Monitor for 2 s in memory/terminal only. Do not save monitor files for this quick confirmation.
13. If CH1 stays near target and `out2`/`ival` do not saturate, leave PID on and report the lock state concisely.

The script defaults to printing a JSON summary only. It deletes temporary scope captures unless `--keep-captures` is passed, and it writes a summary file only if `--save-summary` is passed.

### Best-Q Mode Lock Workflow

After a cavity's large-scan Q fitting is complete, use this when the user asks to lock the highest-Q mode from that cavity:

```powershell
C:\Users\win10\toptica_lasersdk_venv\Scripts\python.exe workspace\scripts\redpitaya_microcavity_lock\src\lock\lock_best_q_mode.py --cavity-dir <.../dieX-Y/cZ>
```

The Q fitting step writes `Q/best_lock_candidate.json` with the highest fitted `Q0` mode as `candidate`. The same manifest also records `nearest_1550_best_q_candidate`, chosen as the valid fitted mode closest to 1550.0 nm, with highest `Q0` as the tie-breaker. The best-Q lock script uses `candidate` first, and only falls back to sorting `Q/q_by_mode.csv` when the manifest is missing. Its order is:

1. Read best lock candidate: family, mode number, wavelength, Q0/Q1/QL/depth.
2. Safe-off RP PID and ASG outputs.
3. Set TOPTICA PC piezo to `75 V` before moving wavelength.
4. Move TOPTICA to the candidate wavelength.
5. Run `current_mode_fast_lock.py` and report the final 2 s monitor.

Successful reference state from 2026-06-13:

```text
ARC factor unchanged at 25
PC centering: dip_out2 -0.401 V -> -0.088 V -> -0.017 V
target_ch1 ~= 61.86 mV
i = -1 locked
final i ~= -100, p = 0.01
2 s monitor: CH1 mean ~= 62.06 mV, CH1 p-p ~= 2.08 mV,
              CH2 mean ~= -0.490 V, CH2 p-p ~= 7.57 mV,
              ival drift ~= 7.32 mV, no saturation
```

## Decision Rules

Do not treat apparent linewidth on the `Out2` axis as the optical cavity's true Q or frequency FWHM. It is the current control-coordinate capture width.

Pretune width uses platform-drop 1/4:

```text
T_width = T_platform - 0.25 * (T_platform - T_min)
        = T_min + 0.75 * (T_platform - T_min)
```

Current default window:

```text
min_full_width = 0.08 V
max_full_width = none for the default current-mode fast lock
```

Interpretation:

- Keep `min_full_width = 0.08 V` as the only normal acceptance threshold.
- Do not reject or retune a normal lock attempt because the apparent width is above a previous upper bound.

PID lockpoint uses dip-rise 1/4:

```text
T_lock = T_min + 0.25 * (T_platform - T_min)
```

Do not reuse the platform-drop 1/4 level as the PID setpoint.

For DC transmission locking, always clear the PID input filter before handoff:

```text
pid0.inputfilter = 0
```

The lock setpoint is defined on raw `in1` transmission. A stale nonzero PyRPL `pid0.inputfilter` changes the signal that PID compares to the setpoint and can make the trace pass through the apparent raw lockpoint without stopping.

`initial_ival` is a handoff starting control voltage, not a measured physical parameter. The default current-mode lock sets it to:

```text
+1.0 V
```

Then the script uses the empirically fixed direction `i < 0`. It does not auto-test or auto-flip the integral sign in the normal current-mode workflow.

## Failure Rules

Mark a lock attempt invalid or exploratory, not evidence, when any of these occur:

- `abs(CH2/out2) > 0.98 V`.
- `abs(pid0.ival) > 3.8 V`.
- The fixed negative integral direction cannot survive the 2 s `|i| = 100` check.
- The lock point changed because the optical mode, coupling, polarization, ARC factor, or PC piezo state changed unexpectedly.
- ASG or PID outputs are not in the intended state.
- The wrong channel is read, or CH1/CH2 meanings are uncertain.
- The result contradicts a basic sanity check and has not been resolved.

Do not auto-flip integral direction in the normal workflow. With `ival = +1 V`, keep `i < 0`; stop only if the final high-gain check saturates or cannot hold the target.

## Monitor And Recording

The default current-mode lock uses only an in-memory 2 s final monitor and does not save monitor files. Judge at least:

```text
CH1 mean vs target
CH1 RMS fluctuation
CH1 peak-to-peak
CH2/out2 peak-to-peak
pid0.ival drift
out2/ival saturation flags
periodic oscillation or slow drift
```

Write one accepted group into the external `session.md` with this shape:

```text
### <date/time or tag>: <short purpose>

Purpose:
Facts:
- command / script
- result directory
- ARC factor, PC voltage, full width, dip Out2, target CH1, lock Out2
- monitor summary, including stable segment after first-frame removal

Observation:
Interpretation:
Limit:
Next check:
```

Keep raw large data, figures, tabular run artifacts, and `session.md` out of git by default. Commit only code/workflow changes; record external result directory names instead of committing CSV/JSON outputs. Do not overstate 5 s short locks as long-term stability or measurement SNR proof.

For the default current-mode fast lock, the final 2 s stability check should be printed or inspected live only; do not create extra monitor files unless the user asks for evidence capture.
