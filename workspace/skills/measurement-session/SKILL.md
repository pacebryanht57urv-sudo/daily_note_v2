---
name: measurement-session
description: Use for onsite scientific measurement sessions involving instruments, sweeps, scope traces, spectra, PID/feedback settings, PyRPL/Red Pitaya, laser/photodetector readout, microcavity large scans, or other data-acquisition workflows where Codex must protect live instrument configuration, reuse acquisition scripts, run the fixed chip7 large-scan fast path when a cavity is coupled, judge data validity with the user, and maintain session.md records.
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

## Shared Platform Sessions

When another student or collaborator uses the same measurement platform, start a separate session for their data instead of appending to the current user's active experiment session. This is especially important when the same instruments and workflow are reused but the sample owner, device purpose, chip family, or analysis question differs.

Default behavior:

- Create or use a separate session directory under `workspace/experiments/<date>/`, named by the collaborator/project/sample, for example `liu_jianfei_platform_measurement`.
- Put that collaborator's `session.md`, figures, results, and lightweight summaries under that separate session directory.
- Reuse the stable acquisition, processing, plotting, saturation-check, and cleanup workflow from the current platform scripts when appropriate, but keep output paths and records in the collaborator's session.
- Do not write collaborator measurements into the current chip7/four-inch-sample mainline session unless the user explicitly says the data belong to that session.
- Record platform reuse explicitly: copied/reused scripts, instrument settings, channel mapping, power calibration, and any differences from the current mainline workflow.

## Fixed Large-Scan Fast Path

When the user says a chip7 cavity is coupled, e.g. "c6 耦合好了", treat it as permission to run the current four-inch-sample large-scan pipeline. Do not rediscover the whole workspace, reread old cavity folders, or rewrite scripts.

Use this narrow context only:

- `workspace/experiments/2026-05-28/four_inch_sample_formal_measurement/session.md`
- `workspace/experiments/2026-05-28/four_inch_sample_formal_measurement/scripts/`
- `workspace/experiments/2026-05-28/four_inch_sample_formal_measurement/results/chip7/<die>/<cavity>/`
- `workspace/experiments/2026-05-28/four_inch_sample_formal_measurement/figures/measurement/chip7/<die>/<cavity>/`

For `chip7 / <current die> / cX`, use the fixed workflow below unless the user explicitly gives different scan settings. The current die is the die most recently stated by the user, for example `die1-1` or `die1-2`.

```powershell
cd workspace\experiments\2026-05-28\four_inch_sample_formal_measurement

python scripts\acquire_large_scan.py --chip chip7 --die <current-die> --cavity cX --start-nm 1530 --stop-nm 1570 --speed-nm-s 2 --sample-rate-hz 500000 --record-seconds 20 --storage-format npz-compressed

python scripts\process_large_scan.py results\chip7\<current-die>\cX\large_scan_<timestamp>_1530-1570nm.npz --chip chip7 --die <current-die> --cavity cX --nominal-width-samples 500

python scripts\fit_large_scan_dispersion.py results\chip7\<current-die>\cX\large_scan_<timestamp>_1530-1570nm_dip_table.csv --chip chip7 --die <current-die> --cavity cX --depth-threshold 0.2 --reference-fsr-mhz 204900

python scripts\fit_large_scan_q.py --data-path results\chip7\<current-die>\cX\large_scan_<timestamp>_1530-1570nm.npz --family-points-csv results\chip7\<current-die>\cX\large_scan_<timestamp>_1530-1570nm_dispersion_auto_centered_family_points.csv --chip chip7 --die <current-die> --cavity cX --depth-threshold 0.2
```

After acquisition, get `<timestamp>` from the new metadata or file name in that cavity's results directory only. Do not scan other cavities to infer it.

When the large scan is finished and the setup is returned to fine-scan mode, restore TOPTICA to `1550 nm` as the fine-scan center wavelength, then restore the fine-scan arc factor and oscilloscope idle state. Treat this as part of the normal cleanup path; if the laser cannot return to `1550 nm`, report the failure instead of silently continuing.

Before processing or fitting, run a raw-voltage saturation check on the new acquisition only. Inspect CH2 transmission and CH3 MZI arrays from the new `.npz`/raw file; if either channel has an obvious flat top/bottom, or more than about `1%` of samples sit within a tiny tolerance of the channel min/max, stop immediately. Mark that data group invalid because of voltage saturation, report the saturated channel and fraction, and do not run family assignment or Q fitting unless the user explicitly asks to keep it as exploratory.

Before or after fitting, inspect that cavity's figure folder only:

- `figures/measurement/chip7/<die>/<cavity>/`

Read the microscope/coupling photos already placed there, typically `.jpg`, `.jpeg`, `.png`, or `.tif`. Do not search sibling cavity folders. Embed the representative cavity photo(s) in that cavity's `session.md` subsection together with the throughput / single-ended insertion-loss table. If no photo is present, record that the cavity photo is missing instead of silently skipping the preparation evidence.

After fitting, inspect only the new files for that cavity:

- `<stem>_process_summary.json`
- `<stem>_dispersion_fit_summary.json`
- `<stem>_large_scan_q_summary.json`
- `<stem>_large_scan_q_by_family.csv`
- `<stem>_ch2_ch3_raw.png`
- `<stem>_dispersion_families_depth_gt_0p2.png`
- `<stem>_large_scan_q_trends.png`

Then update `session.md` in that cavity subsection. Record:

- Acquisition facts: CH1 rising trigger at `1 V`, 20 s window, 500 kSa/s actual sample rate, `.npz` path.
- Processing facts: total dips, `depth > 0.2` dips, FSR candidates near half-FSR and full FSR.
- One table row per fitted family with D1/FSR, `n_g`, fit rms, most-likely Q0, median Q0, Q1 nearest 1550 nm, nearest wavelength, and coupling-branch note.
- A short judgment: whether this group is valid for Q comparison, whether dispersion is clean enough for D2 interpretation, and what limitation matters.

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

Use embedded image links for representative figures in `session.md`. Keep raw large data out of git; record paths instead.

Prefer compact horizontal tables in `session.md` when a record contains comparable photos, powers, or result figures. For example:

- Put die-level identification and left/right facet photos under the die subsection, not under a specific cavity.
- For array-cavity dies, put cavity-level microscope/phone photos in one die-level cavity overview table laid out like the real array. Use the per-cavity folder such as `figures/measurement/<chip>/<die>/<cavity>/`, but do not create a long repeated subsection for every cavity.
- Keep per-cavity rows in one die-level status table: cavity, gap, valid/skipped state, throughput / single-ended insertion loss, data state, group-index values `n_g` by family, summary link, and one short note. If only output power is known, record it temporarily in the note and leave throughput / insertion loss pending until the input-monitor power is known.
- Keep Q0/Q1/loaded-linewidth details, raw acquisition figures, dispersion-family figures, long file lists, and full per-family Q tables in the per-cavity summary file instead of expanding them in `session.md`.
- In `session.md`, embed at most the compact Q-trend figure per measured cavity. Prefer a three-panel vertical Q trend (`Q0`, `Q1`, `Tmin/platform`) and omit the loaded-linewidth subplot unless linewidth is the main question. For array-cavity dies, lay Q-trend figures out in the real array row order: three cavities with the same gap in one horizontal table row, using a full-width fixed-layout table and images that fill their cells, such as `style="width:100%; display:block;"`.
- Put short die-level judgments below the table in one concise paragraph instead of several repeated cavity paragraphs.

For chip/die microcavity measurements, use this hierarchy:

1. The user loads one die on the measurement stage.
2. Before writing any gap values for that die, verify the die row in the session's design matrix or layout summary. Do not copy the previous die's gap sequence by pattern.
3. Record die-level identification and left/right facet observations once in the die subsection.
4. For each cavity, record the cavity photo, throughput / single-ended insertion loss, acquisition state, `n_g`, summary link, and short judgment as one row in the die-level overview.
5. Treat one cavity as complete only after the die-level row is updated and the per-cavity summary contains the full evidence chain.

For the current large-scan acquisition workflow, the oscilloscope window is centered on the CH1 trigger: CH1 rising edge at 1 V, total window 20 s, with 10 s before trigger and 10 s after trigger. Treat scans using different trigger level or pre/post window as invalid for formal comparison unless the user explicitly asks to keep them as exploratory.

## Microcavity Large-Scan Summaries

For accepted microcavity large-scan analyses, always close the data group with comparison-ready evidence. In array-cavity sessions, put the full evidence in the per-cavity summary and keep only a compact comparison row plus the compact Q-trend figure in `session.md`.

In the per-cavity summary, record one table row per assigned mode family. Include:

- Family display label.
- Effective D1 or FSR from the family fit.
- Group index `n_g = c / (D1 * 2πR)` when the cavity radius is known; if radius is missing, ask before filling `n_g`.
- Most-likely Q0, using the peak of the successful-mode `log10(Q0)` distribution.
- Median Q0.
- Q1 for the mode closest to 1550 nm.
- The actual nearest wavelength used for that Q1.
- Any coupling-branch note. Do not infer overcoupling or swap Q0/Q1 from `Tmin`/platform wavelength slope alone; if `Tmin` rises with wavelength but gap trends or other evidence do not confirm overcoupling, mark the branch assignment as ambiguous and keep Q0/Q1 unswapped.

Keep the branch-name explanation near the table when labels such as `deep_lower` or `deep_upper` are used: these labels describe the folded-frequency branch within each mode bin, not lower or higher wavelength.

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
