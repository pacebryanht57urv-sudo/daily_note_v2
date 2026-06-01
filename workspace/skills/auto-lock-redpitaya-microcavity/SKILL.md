---
name: auto-lock-redpitaya-microcavity
description: Use for the Red Pitaya/PyRPL + TOPTICA DLC PRO microcavity transmission auto-lock workflow, especially when continuing the locking session from the external data root, running fast_lock_with_pretune.py, judging apparent linewidth/lockpoint/PID handoff quality, or recording lock attempts.
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
继续使用 workspace/skills/auto-lock-redpitaya-microcavity/SKILL.md 里的 Red Pitaya + TOPTICA 微腔锁模流程，先读当前 session，再按里面的主线脚本操作。
```

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

Main script:

```text
workspace/scripts/redpitaya_microcavity_lock/fast_lock_with_pretune.py
```

Key dependencies reused by the main script:

- `workspace/scripts/redpitaya_microcavity_lock/pyrpl_live_bridge.py`: local HTTP bridge for PyRPL GUI-visible Red Pitaya control.
- `workspace/scripts/redpitaya_microcavity_lock/suggest_arc_factor.py`: scope capture, down-sweep dip detection, apparent width, lockpoint, prelock plot.
- `workspace/scripts/redpitaya_microcavity_lock/tune_arc_fullwidth_center.py`: TOPTICA ARC factor and PC piezo read/write helpers.

Auxiliary diagnostics:

- `workspace/scripts/redpitaya_microcavity_lock/run_seconds_pid_lock_sweep.py`: static probe, direction diagnosis, manual P/I sweep.
- `workspace/scripts/redpitaya_microcavity_lock/monitor_pid_state.py`: independent post-lock monitor.

## Before Running

Check the minimum safe context before touching live outputs:

1. Remind the user to check branch, status, and sync state for important session or skill changes.
2. Confirm the PyRPL live bridge is already running and reachable at `http://127.0.0.1:7870`.
3. Confirm the target is the current optical mode; never reuse an old lockpoint after polarization, coupling, PC piezo, ARC factor, or mode selection changed.
4. Prefer the existing scripts over new code. Do not create another one-off locking script unless the current workflow cannot express the needed test.
5. Treat GUI/debug configs such as `global_config`, `new1234`, `new123456`, and similar PyRPL configs as user-owned.

Use the TOPTICA SDK environment for the main locking script when needed:

```powershell
C:\Users\win10\toptica_lasersdk_venv\Scripts\python.exe workspace\scripts\redpitaya_microcavity_lock\fast_lock_with_pretune.py ...
```

## Main Workflow

Use this rhythm for each lock attempt:

1. Configure ASG sweep and acquire a fresh scope trace of the current mode.
2. Analyze only the down-sweep segment by default.
3. Use platform-drop 1/4 apparent full width to decide whether ARC/PC pretuning is acceptable.
4. Tune PC piezo first when the dip center is not near `Out2 = 0`.
5. Tune ARC factor only after the dip is centered enough.
6. Recompute the lockpoint from the fresh sweep using dip-rise 1/4.
7. Turn off ASG, initialize PID, set `pid0.ival`, set PID target, then enable P/I.
8. Monitor for a short window, inspect saturation and first-frame handoff transient separately.
9. Judge the data with the user before writing it into the external `session.md`.

Default command template:

```powershell
C:\Users\win10\toptica_lasersdk_venv\Scripts\python.exe workspace\scripts\redpitaya_microcavity_lock\fast_lock_with_pretune.py `
  --tag <meaningful_tag> `
  --max-pretune-iterations 10 `
  --max-fractional-step 0.50 `
  --monitor-seconds 5 `
  --p 0.01 `
  --i 10
```

Use `--initial-ival <value>` only when the automatic handoff direction is known to be wrong or has just caused saturation.

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
target_full_width = 0.10 V
max_full_width = 0.24 V
```

Interpretation:

- Keep `min_full_width = 0.08 V` so high-Q modes are not rejected just because they cannot be made wider.
- Keep `target_full_width = 0.10 V` as the ARC tuning reference.
- Allow `max_full_width = 0.24 V` so low-Q modes are not forced narrower by pushing ARC factor too high.

PID lockpoint uses dip-rise 1/4:

```text
T_lock = T_min + 0.25 * (T_platform - T_min)
```

Do not reuse the platform-drop 1/4 level as the PID setpoint.

`initial_ival` is a handoff starting control voltage, not a measured physical parameter. If omitted, the script uses:

```text
lock_center <= 0 -> +platform_ival
lock_center > 0  -> -platform_ival
```

where `platform_ival` defaults to `0.95 V`.

## Failure Rules

Mark a lock attempt invalid or exploratory, not evidence, when any of these occur:

- `abs(CH2/out2) > 0.98 V`.
- `abs(pid0.ival) > 3.8 V`.
- PID handoff uses the wrong direction and drives toward saturation.
- The lock point changed because the optical mode, coupling, polarization, ARC factor, or PC piezo state changed unexpectedly.
- ASG or PID outputs are not in the intended state.
- The wrong channel is read, or CH1/CH2 meanings are uncertain.
- The result contradicts a basic sanity check and has not been resolved.

When saturation guard triggers, stop the attempt, disable risky PID output as the script does, and record it as a failure case only if it teaches a parameter or direction lesson.

## Monitor And Recording

For handoff monitor, inspect both:

- Full monitor including the first frame.
- Stable segment after removing the first handoff frame.

Judge at least:

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
