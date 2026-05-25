---
name: measurement-session
description: Use for onsite scientific measurement sessions involving instruments, sweeps, scope traces, spectra, PID/feedback settings, PyRPL/Red Pitaya, laser/photodetector readout, or other data-acquisition workflows where Codex must protect live instrument configuration, reuse acquisition scripts, judge data validity with the user, and maintain session.md records.
---

# Measurement Session

## Core Rule

Treat measurement work as a sequence of data groups, not a stream of facts.

For each new group:

1. State the hypothesis or purpose of the group.
2. Confirm the controlled variables, scanned variables, instrument state, and readout channel.
3. Acquire or process data using existing scripts first.
4. Inspect the result before recording it as evidence.
5. Ask 1-2 targeted questions if interpretation, safety, or reproducibility is unclear.
6. Record the group in `session.md` only after it is judged useful or explicitly marked invalid.

Do not just accumulate files. Each accepted group needs a short judgment: what it shows, what it does not prove, and what should happen next.

## Script Reuse

Before writing new code:

- Search existing session scripts with `rg --files` and `rg`.
- Reuse or lightly patch existing acquisition/plotting scripts.
- Keep instrument-level logic stable; express experiment differences through parameters, metadata, and output directories.
- Prefer one stable scope acquisition script, one stable spectrum acquisition script, and parameterized sweep scripts.

If new code is unavoidable, name it by instrument/workflow rather than by one-off idea, and record why existing scripts were insufficient.

## Instrument Config Safety

Never let automation write directly into a user's manual GUI/debug configuration when the software auto-saves state.

For PyRPL/Red Pitaya:

- Treat `global_config`, `new1234`, `new123456`, and similar GUI configs as user-owned.
- Copy the GUI config to a temporary runtime config before backend operations.
- Run scripts against the temporary config.
- Prefer `gui=False` for backend acquisition.
- At the end, set risky outputs such as `pid*.output_direct`, `asg*.output_direct`, or similar control outputs to `off` when safe and appropriate.
- Delete the temporary config and its `.bak` file.
- Do not hand-open temporary configs in the GUI.

If PyRPL GUI freezes or emits Qt/pyqtgraph errors, do not repeatedly relaunch it. Inspect configs, processes, and logs first; backend access may still work.

## Data Validity

Separate:

- **Fact:** instrument settings, file paths, numeric readouts, waveform/spectrum features.
- **Observation:** peaks, dips, oscillation, drift, baseline changes.
- **Interpretation:** likely mechanism or model.
- **Limit:** what the data cannot prove.
- **Next check:** the smallest test that would distinguish alternatives.

Mark data as invalid or exploratory when:

- The lock point changed unintentionally.
- The wrong channel was read.
- Outputs were not in the intended state.
- The GUI/config state was corrupted.
- The sweep changed more than the declared variable.
- The result contradicts a basic sanity check and has not been resolved.

Invalid data may stay in `session.md` only as a caution, not as evidence for a conclusion.

## Measurement Records

For each accepted group, write:

- Time/context.
- Sample or optical mode, if applicable.
- Instrument platform overview and port mapping when relevant: device/system photos, physical input/output ports, software channels, signal meaning, and control direction.
- Instrument config name and whether it was copied to a runtime config.
- Controlled variables and scanned variables.
- Readout channel and units.
- File paths for raw data, metadata, and figures.
- A concise table of key numbers.
- A short judgment and open questions.

Use embedded image links for representative figures in `session.md`. Keep raw large data out of git; record paths instead.

## Feedback And Locking

When feedback/PID is involved:

- Confirm the lock point before sweeping parameters.
- Check whether the measured channel is the error signal, detector signal, control signal, or a derived quantity.
- Record whether the final application reads the detector channel or the control channel.
- Test feedback direction when a parameter does not lock; flipping the sign may be appropriate, but record it.
- Do not infer sensitivity from reduced noise alone. If feedback suppresses noise, it may also suppress low-frequency signal in the same readout channel.
- To evaluate sensitivity, compare signal amplitude, noise PSD, and SNR under a known excitation or calibrated modulation.

## Session Hygiene

Measurement sessions can become long. Prefer this rhythm:

- Capture one coherent group.
- Plot or summarize it immediately.
- Decide whether it is meaningful.
- Write the accepted conclusion or invalidation into `session.md`.

When earlier exploratory sections become distracting, do not delete them silently. Either ask the user to confirm deletion, or move/label them as exploratory and not used for final conclusions.
