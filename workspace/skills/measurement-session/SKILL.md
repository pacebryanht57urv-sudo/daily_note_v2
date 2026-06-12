---
name: measurement-session
description: Use for general onsite scientific measurement sessions involving instruments, sweeps, scope traces, spectra, PID/feedback settings, PyRPL/Red Pitaya, laser/photodetector readout, or other data-acquisition workflows where Codex must protect live instrument configuration, reuse acquisition scripts, judge data validity with the user, and maintain appropriate external records. For chip/die microcavity large-scan Q measurement, use the dedicated microcavity-large-scan-q skill instead.
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
6. Record the group only after it is judged useful or explicitly marked invalid. In content-first microcavity campaigns, the primary record is the per-cavity card plus `Q/` outputs; `session.md` is legacy unless the user explicitly asks to update it.

Do not just accumulate files. Each accepted group needs a short judgment: what it shows, what it does not prove, and what should happen next.

## Dense Spectrum Data Policy

For dense scope/spectrum captures such as Red Pitaya/PyRPL, oscilloscope FFT, spectrum-analyzer traces, or other high-point-count readouts, keep one full-resolution machine-readable source of truth per accepted group.

- Use raw `.npz` plus metadata `.json` as the default full-trace record when the acquisition script can save structured arrays and settings.
- For PyRPL/Red Pitaya spectrum captures, the default raw `.npz` must contain only the frequency axis and two `input1` vertical arrays: `input1_vpk2` from `spectrumanalyzer.single()` and `input1_dbm_per_hz` matching the current corrected dBm/Hz display. Do not save `input2`, `cross_real`, `cross_imag`, or parallel unit-converted copies unless the user explicitly requests those channels or a specific export.
- Treat dBm, dBm/Hz, V/√Hz, 50 Ω equivalent power, and similar quantities as analysis/view-layer conversions derived from the raw trace plus metadata such as RBW and load assumption. Do not store four unit-converted full traces by default.
- For RP/PyRPL dBm and dBm/Hz live display, apply the fixed RP high-Z input correction of `6.0206 dB` by default when converting the 1 MΩ RP input voltage to a 50 Ω matched-power display. For external RF-chain gain, prefer the spectrum analyzer GUI field `external_gain_db`; keep this as display/metadata state and do not bake corrected unit traces into the raw `.npz`.
- Generate user-facing figures directly from the raw `.npz` and metadata; do not create a full-length processed `.csv` just to draw plots.
- Put only compact summaries in processed records by default: key marker values, band medians, units, calibration assumptions, and file paths.
- Export a full processed `.csv` only when the user explicitly asks for spreadsheet/Origin use, when another tool cannot read `.npz`, or when a formal downstream analysis script requires CSV input.
- When replacing a measurement, overwrite or refresh the current accepted group's canonical raw/metadata/figures and record the source timestamp in metadata; do not keep parallel duplicate full traces unless the user asks to compare retakes.

## Script Reuse

Before writing new code:

- Search existing session scripts with `rg --files` and `rg`.
- When records/data live under ignored external or local experiment folders, use `rg -uuu` or explicit paths. Do not conclude that a design matrix or session record is missing from a normal `rg` search that obeys `.gitignore`.
- Reuse or lightly patch existing acquisition/plotting scripts.
- Keep instrument-level logic stable; express experiment differences through parameters, metadata, and output directories.
- Prefer one stable scope acquisition script, one stable spectrum acquisition script, and parameterized sweep scripts.

If new code is unavoidable, name it by instrument/workflow rather than by one-off idea, and record why existing scripts were insufficient.

## Shared Platform Sessions

When another student or collaborator uses the same measurement platform, start a separate session for their data instead of appending to the current user's active experiment session. This is especially important when the same instruments and workflow are reused but the sample owner, device purpose, chip family, or analysis question differs.

Default behavior:

- Create or use a separate session directory under `$DAILY_NOTE_DATA_ROOT/experiments/<date>/`, named by the collaborator/project/sample, for example `liu_jianfei_platform_measurement`.
- Put that collaborator's `session.md`, figures, results, and lightweight summaries under that separate session directory.
- Reuse the stable acquisition, processing, plotting, saturation-check, and cleanup workflow from the current platform scripts when appropriate, but keep output paths and records in the collaborator's session.
- Do not write collaborator measurements into the current campaign unless the user explicitly says the data belong to that campaign.
- Record platform reuse explicitly: copied/reused scripts, instrument settings, channel mapping, power calibration, and any differences from the current mainline workflow.

## Microcavity Large-Scan Q Route

For chip/die microcavity large-scan Q measurement, fixed 1530-1570 nm acquisition, FSR/D2 live review, per-cavity cards, power/loss pass, multi-family die README close-out, or global-μ family comparison, use `workspace/skills/microcavity-large-scan-q/SKILL.md`. Do not load the detailed microcavity rules during unrelated measurement sessions.

Write `daily/YYYY-MM-DD.md` only at a natural progress boundary: after a small batch such as several cavities on one die, at task switch, or at end-of-day. Daily notes are time indexes only; they should state what advanced and which workflow rule changed, not duplicate the evidence chain.

If acquisition fails, stop and report the exact failure. Do not retry repeatedly or broaden into unrelated folders.

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

Use embedded image links for representative figures in external `session.md`. Keep raw large data and generated figures out of git; record external paths instead.

Prefer compact horizontal tables in `session.md` when a record contains comparable photos, powers, or result figures. For example:

- Put die-level identification and left/right facet photos under the die subsection, not under a specific cavity.
- For array-cavity dies, put cavity-level microscope/phone photos in one die-level cavity overview table laid out like the real array. Use the per-cavity folder such as `figures/measurement/<chip>/<die>/<cavity>/`, but do not create a long repeated subsection for every cavity.
- Keep per-cavity rows in one die-level status table: cavity, gap, valid/skipped state, throughput / single-ended insertion loss, data state, group-index values `n_g` by family, summary link, and one short note. If only output power is known, record it temporarily in the note and leave throughput / insertion loss pending until the input-monitor power is known.
- Keep Q0/Q1/loaded-linewidth details, raw acquisition figures, dispersion-family figures, long file lists, and full per-family Q tables in the per-cavity summary file instead of expanding them in `session.md`.
- In `session.md`, keep the compact Q-trend figure per measured cavity because it is the most direct performance summary. For array-cavity dies, lay Q-trend figures out in the real array row order: three cavities with the same gap in one horizontal table row, using a full-width fixed-layout table and images that fill their cells, such as `style="width:100%; display:block;"`.
- Keep the global raw CH2/CH3 min-max envelope and local dip mosaic in the per-cavity result folder and list them in the per-cavity summary. Use the global envelope for acquisition health checks such as saturation, baseline drift, MZI behavior, and obvious missing/extra dips. Use the local dip mosaic for inspecting the real full-resolution normalized line shape and `Tmin/platform` behavior within each depth-ordered mode family. Do not replace the `session.md` Q-trend overview with these raw-data diagnostic figures unless the user explicitly asks.
- Put short die-level judgments below the table in one concise paragraph instead of several repeated cavity paragraphs.

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
- For content-first campaigns, write accepted conclusions into the per-cavity card and standardized result files, not a long live `session.md`.
- Add or update `daily/YYYY-MM-DD.md` only at a small-batch boundary, task switch, or end-of-day; keep it as a time index rather than a full evidence record.

When earlier exploratory sections become distracting, do not delete them silently. Either ask the user to confirm deletion, or move/label them as exploratory and not used for final conclusions.
