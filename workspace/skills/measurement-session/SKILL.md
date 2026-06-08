---
name: measurement-session
description: Use for onsite scientific measurement sessions involving instruments, sweeps, scope traces, spectra, PID/feedback settings, PyRPL/Red Pitaya, laser/photodetector readout, microcavity large scans, or other data-acquisition workflows where Codex must protect live instrument configuration, reuse acquisition scripts, run the fixed microcavity large-scan Q flow when a cavity is coupled, judge data validity with the user, and maintain the appropriate external records.
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

## Fixed Microcavity Large-Scan Q Flow

When the user says a microcavity is coupled, e.g. "c6 耦合好了", treat it as permission to run the current configured large-scan Q pipeline. Do not rediscover the whole workspace, reread old cavity folders, or rewrite scripts.

Treat each run as a fresh measurement, not a "remeasure" branch. The user may manually delete old data before the run. Do not preserve old or known-wrong data in `evidence/` unless the user explicitly asks; `evidence/` is for the current accepted run's processing trace.
Do not check whether old data has been deleted before starting acquisition; the user owns that cleanup. After acquisition, only verify that the new timestamped raw `.npz` and metadata `.json` really exist in the current cavity's `Q/` folder.

Use a fast onsite mode for consecutive cavities on the same die. Once the current die has been verified, cache its measurement context for the rest of that die:

- current `chip / die`;
- design radius and gap rows;
- current monitor-power convention;
- expected design FSR for candidate-search sanity only;
- active acquisition parameters and hard gates.

For the next cavity on the same die, do not reread this skill, `AGENTS.md`, git status, old sibling result folders, or the design matrix unless something changed. A one-line confirmation such as "using cached die2-3 context: R=65 um, monitor=10 uW" is enough.

Escalate out of fast mode only when one of these is true:

- the user starts a new die/chip, changes monitor power, scan range, trigger settings, or instrument channel mapping;
- acquisition or restore fails;
- CH2/CH3 saturation gate fails or is borderline;
- the script reports an unknown die/design row or requires a manual reference FSR;
- FSR candidates do not include a plausible full-FSR branch near the design expectation;
- family count, depth-filtered point count, fit residual, or Q-fit success count changes unexpectedly compared with adjacent cavities on the same die;
- the mode-family algorithm or plotting code was just edited;
- the user explicitly asks whether a plot or family assignment "looks right".

The script context is now in Git, while records and data are external:

- Scripts: `workspace/scripts/microcavity_large_scan/`
- Campaign entry: `experiments/<campaign>/`
- Legacy session, if explicitly needed: `experiments/<campaign>/session.md`
- Results: `experiments/<campaign>/results/<chip>/<die>/<cavity>/`
- Figures: `experiments/<campaign>/figures/measurement/<chip>/<die>/<cavity>/`

For `<chip> / <current die> / cX`, use the fixed workflow below unless the user explicitly gives different scan settings. The current die is the die most recently stated by the user, for example `die1-1` or `die1-2`. The current default chip/campaign may come from `DAILY_NOTE_CHIP` and `DAILY_NOTE_CAMPAIGN`; chip7 / `wafer_measuement/Batch_260515` is only the current lab default, not a hard workflow boundary.

Before acquisition/analysis for a new die, verify that die's design row from the external session/design matrix, including cavity radius and the gap rows when available. Do not reuse the previous die's reference FSR or gap sequence. For chip7, the design FSR helper is only an initial expectation and FSR-candidate search range; the formal folding FSR must come from the accepted full-FSR candidate in the new data. For non-chip7 data, require an explicit `--disk-fsr-mhz` or an equivalent design helper before acquisition through the wrapper.

Prefer the standardized wrapper over manually chaining scripts. It owns acquisition, the new-file gate, processing, automatic multi-family fallback, Q fitting, and final `Q/` folder standardization.

The standardized wrapper may overlap analysis with post-scan cleanup: once the timestamped raw `.npz` and initial metadata `.json` are saved, it can start processing / dispersion / Q fitting while `acquire_large_scan.py` continues restoring the laser wavelength, fine-scan arc factor, oscilloscope idle state, and post-scan emission/current cycle. This is allowed only as a wrapper-level concurrency optimization. Final standardization, onsite verdict, and `cavity_card.html` updates must still wait until the acquisition process exits and the full acquisition gates pass, including fine-scan restore and post-scan emission cycle.

Before starting acquisition:

- Set TOPTICA PC piezo voltage to `75 V` and read back both `voltage-set` and `voltage-act`.
- Read and remember the current laser wavelength. The cleanup target is this initial wavelength, not a fixed `1550 nm`, unless the user explicitly says otherwise.
- Use CH1 rising trigger at `1 V`, total oscilloscope window `20 s`, with `10 s` before trigger and `10 s` after trigger.
- Use `200 kSa/s` onsite raw acquisition and uncompressed `.npz` storage.
- Cycle TOPTICA emission/current before the formal scan. After waveform readout, first restore the laser wavelength, fine-scan arc factor, and oscilloscope fine-scan idle state; only then cycle emission/current once more. Use `2 s` off and `2 s` on-settle for each cycle unless the user changes it.

```powershell
cd <repo-root>

python workspace\scripts\microcavity_large_scan\acquire_large_scan.py --campaign <campaign> --chip <chip> --die <current-die> --cavity cX --start-nm 1530 --stop-nm 1570 --speed-nm-s 2 --sample-rate-hz 200000 --record-seconds 20 --storage-format npz --restore-wavelength-mode initial --cycle-emission-before-scan --cycle-emission-after-scan --emission-off-seconds 2 --emission-on-settle-seconds 2 --output-dir "<campaign-root>\results\<chip>\<current-die>\cX\Q"

python workspace\scripts\microcavity_large_scan\process_large_scan.py "<campaign-root>\results\<chip>\<current-die>\cX\Q\large_scan_<timestamp>_1530-1570nm.npz" --chip <chip> --die <current-die> --cavity cX --nominal-width-samples 50

python workspace\scripts\microcavity_large_scan\fit_large_scan_dispersion.py "<campaign-root>\results\<chip>\<current-die>\cX\Q\large_scan_<timestamp>_1530-1570nm_dip_table.csv" --chip <chip> --die <current-die> --cavity cX --depth-threshold 0.4

python workspace\scripts\microcavity_large_scan\fit_large_scan_q.py --data-path "<campaign-root>\results\<chip>\<current-die>\cX\Q\large_scan_<timestamp>_1530-1570nm.npz" --family-points-csv "<campaign-root>\results\<chip>\<current-die>\cX\Q\large_scan_<timestamp>_dispersion_auto_centered_family_points.csv" --chip <chip> --die <current-die> --cavity cX --depth-threshold 0.4
```

Normal onsite command:

```powershell
python workspace\scripts\microcavity_large_scan\large_scan_flow.py --campaign <campaign> --chip <chip> --die <current-die> --cavity cX
```

Use the explicit commands above only when debugging one pipeline stage.

After acquisition, get `<timestamp>` from the new metadata or file name in that cavity's results directory only. Do not scan other cavities to infer it.

When the large scan is finished and the setup is returned to fine-scan mode, restore TOPTICA to the initial wavelength read before moving to `1530 nm`, then restore the fine-scan arc factor and oscilloscope idle state. Treat this as part of the normal cleanup path; if the laser cannot return to the initial wavelength, report the failure instead of silently continuing.

Before processing or fitting, run the acquisition gates on the new acquisition only. Do not infer the timestamp from other cavities or from stable root names such as `raw.npz`; use the new metadata/file name from this acquisition.

- `acquisition.json` must show emission/current cycle before scan, plus a post-scan emission/current cycle after fine-scan state restoration.
- `acquisition.json` must show restore target = initial wavelength and restore readback close to target.
- `acquisition.json` / waveform header must show CH1 trigger window `x_start=-10 s`, `x_stop=+10 s` within tolerance.
- CH2 transmission and CH3 MZI arrays must not show obvious flat top/bottom; more than about `1%` of samples within a tiny tolerance of channel min/max is invalid.
- PC piezo readback should remain consistent with `75 V`; if it was not checked before acquisition, stop and report the missing precondition.

If any gate fails, stop immediately. Mark the data group invalid, report the exact failed gate, and do not run family assignment or Q fitting unless the user explicitly asks to keep it as exploratory.

For FSR selection, do not fold by the design estimate when the new data provides a clear full-FSR candidate. `process_large_scan.py` should compute FSR candidates first, then choose the highest-scoring candidate near the expected full FSR. Half-FSR candidates can be reported but must not be used for formal folding. `fit_large_scan_dispersion.py` must read the formal FSR from `process_summary.json` unless the user explicitly supplies a different reference FSR.

For family assignment, the formal automatic path is now:

- try the continuous-FSR graph splitter;
- if it only finds one branch while per-mode-bin assignment finds multiple visible branches, automatically use the multi-branch per-mode-bin assignment;
- auto-center each family and prune only obvious D2-fit outliers using residual gates;
- extend each accepted family with adjacent unassigned dips only when they are close to that family's fitted frequency curve, keep mode spacing continuous, and do not collide with another accepted family;
- use the resulting `*_dispersion_auto_centered_family_points.csv` for Q fitting.

After processing/fitting an accepted run, standardize the cavity's `Q/` folder. The root is for formal display and daily use:

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

Do not leave processing summaries, raw-health figures, fit examples, or dip tables in the `Q/` root. Do not keep old known-wrong measurements as `evidence/`; the user is responsible for deleting old data before a fresh run, and Codex should only preserve the current accepted run's processing evidence unless explicitly asked otherwise.

Cavity cards and user-facing summaries must read display data only from the `Q/` root: `dispersion.png`, `d2_fit.png`, `q_trend.png`, `mode_spectra.png`, `family_points.csv`, and `q_by_mode.csv`. Do not use files under `Q/evidence/processing_*` as display figures; those are for traceability and debugging only.

Formal large-scan display figures have a content contract, not just a filename contract:

- `dispersion.png` must show the common-coordinate family map on the left and a representative one-FSR panel on the right.
- The right one-FSR panel must include the normalized CH2 spectrum trace plus the assigned family markers. A point-only panel with x-axis `1 - depth` is an exploratory fallback, not an acceptable formal display when `Q/raw.npz` or the timestamped raw scan exists.
- `d2_fit.png` must show each accepted family in its own family-centered D2 panel and must also include the same representative one-FSR spectrum panel.
- The representative one-FSR panel should choose the nearest mode bin that contains all assigned families; if no single bin contains all families, the title must list the missing labels.
- After any plotting or family-assignment code change, or after any escalation caused by family count, spacing, residual, or missing branches, visually inspect `Q/dispersion.png`, `Q/d2_fit.png`, `Q/q_trend.png`, and `Q/mode_spectra.png` before updating `cavity_card.html`.
- If a formal figure loses the right-side spectrum trace, stop and fix the plotting/data-path issue before recording the cavity card.

Before or after fitting, identify the representative cavity photo from the die's known figure folder or the cavity's figure folder only:

- `figures/measurement/<chip>/<die>/<cavity>/`
- `figures/measurement/<chip>/<die>/cX.jpg`

Do not search sibling cavity folders. Do not open or visually inspect microscope photos during fast mode unless the user asks or the file is missing/ambiguous; use the known path in the Markdown. If no photo is present, record that the cavity photo is missing instead of silently skipping the preparation evidence.

After fitting in fast mode, extract record numbers from machine-readable outputs only:

- `Q/evidence/processing_*/process_summary.json`
- `Q/evidence/processing_*/dispersion_summary.json`
- `Q/evidence/processing_*/q_summary.json`
- `Q/q_by_mode.csv`

Do not open generated plots in fast mode just to write Markdown. The summary can be written from JSON/CSV values and figure paths. Keep plot files linked in the record for later review.

Open or visually inspect generated plots only when an escalation trigger is present. If escalation is needed, inspect only the new files for that cavity:

- `Q/evidence/processing_*/raw_health.png`
- `Q/dispersion.png`
- `Q/d2_fit.png`
- `Q/q_trend.png`
- `Q/mode_spectra.png`

For Q large-scan measurements in the current content-first workflow, update the per-cavity formal files and `cavity_card.html` immediately after the analysis is judged valid or explicitly skipped/invalid. The cavity card is the required per-cavity user-facing record and is HTML, not Markdown. Generate or refresh measured cavity cards with `workspace/scripts/microcavity_large_scan/write_cavity_card.py`; do not hand-write ad hoc card HTML during live measurement. The fixed card layout is: left identity/summary table, middle `Q/q_trend.png`, right sensitivity placeholder or sensitivity figure. Do not replace the third column with dispersion, family-map, D2-fit, or one-FSR figures. Do not create or maintain old-style per-cavity Markdown summaries unless the user explicitly asks for an export. Do not update die-level README / die-level comparison tables after each individual cavity; wait until the whole die is measured or the user explicitly says to close out that die, then update die-level summaries in one batch. Do not use `session.md` as the main live record unless the user explicitly asks; it is legacy/history for this campaign. Record in the per-cavity card or machine-readable files:

- Acquisition facts: CH1 rising trigger at `1 V`, 20 s window, 200 kSa/s actual sample rate, `.npz` path.
- Processing facts: total dips, `depth > 0.4` dips used for formal family assignment, FSR candidates near half-FSR and full FSR.
- One table row per fitted family with D1/FSR, `n_g`, fit rms, most-likely Q0, median Q0, Q1 nearest 1550 nm, nearest wavelength, and coupling-branch note.
- A short judgment: whether this group is valid for Q comparison, whether dispersion is clean enough for D2 interpretation, and what limitation matters.

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

For chip/die microcavity measurements, use this hierarchy:

1. The user loads one die on the measurement stage.
2. Before writing any gap values for that die, verify the die row in the session's design matrix or layout summary. Do not copy the previous die's gap sequence by pattern.
3. Record die-level identification and left/right facet observations once in the die subsection.
4. For each cavity during active measurement, record the accepted result only in the per-cavity card, standardized `Q/` files, and processing evidence.
5. Update die-level README / overview rows only at die close-out, after all measured/skipped cavities on that die have been judged, so the cross-cavity comparison is internally consistent.

For the current large-scan acquisition workflow, the oscilloscope window is centered on the CH1 trigger: CH1 rising edge at 1 V, total window 20 s, with 10 s before trigger and 10 s after trigger. Treat scans using different trigger level or pre/post window as invalid for formal comparison unless the user explicitly asks to keep them as exploratory.

## Microcavity Large-Scan Summaries

For accepted microcavity large-scan analyses, always close the data group with comparison-ready evidence. In content-first campaigns, put the formal display in `cavity_card.html` and the standardized `Q/` root, with processing evidence under `Q/evidence/processing_*`. If a cavity is skipped or invalid, still update its `cavity_card.html` with the skip/invalid reason and remove stale plots from the card display. In array-cavity sessions that still use `session.md`, keep only a compact comparison row plus the compact Q-trend figure there.

In the per-cavity card or summary, record one table row per assigned mode family. Include:

- Family display label.
- Effective D1 or FSR from the family fit.
- Group index `n_g = c / (D1 * 2πR)` when the cavity radius is known; if radius is missing, ask before filling `n_g`.
- Most-likely Q0, using the peak of the successful-mode `log10(Q0)` distribution.
- Median Q0.
- Q1 for the mode closest to 1550 nm.
- The actual nearest wavelength used for that Q1.
- Any coupling-branch note. Do not infer overcoupling or swap Q0/Q1 from `Tmin`/platform wavelength slope alone; if `Tmin` rises with wavelength but gap trends or other evidence do not confirm overcoupling, mark the branch assignment as ambiguous and keep Q0/Q1 unswapped.

For user-facing records and figures, name mode families by coupling depth: `mode1` is the branch with the lower median `Tmin/platform` or larger median `1 - normalized transmission`; `mode2` is the shallower branch, and so on. Do not use labels such as `deep_lower`, `deep_upper`, or `side_mid` in `session.md`, per-cavity summaries, plot legends, or spoken interpretation, because those names are legacy internal assignment keys. If scripts keep legacy internal keys for compatibility, explicitly treat them as internal-only and map them to depth-ordered `modeN` display names before recording. When two legacy keys point to the same physical dip sequence, merge them under the same `modeN` and record that the automatic assignment duplicated one physical mode rather than creating two independent families.

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
