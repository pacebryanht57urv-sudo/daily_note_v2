---
name: microcavity-large-scan-q
description: Use for chip/die microcavity large-scan Q measurements, fixed 1530-1570 nm acquisition, FSR/D2 onsite review, per-cavity card refresh, output-power/insertion-loss pass, multi-family die README close-out, and global-mu family comparison in the wafer measurement campaign.
---

# Microcavity Large-Scan Q

## Scope

Use this skill when the user says a microcavity/cavity is coupled or asks to measure `cX`, review FSR/D2, refresh large-scan Q outputs, write a cavity card, close out a die README, or update the campaign daily for chip/die microcavity large-scan measurements.

Do not rediscover the full workspace during consecutive cavities on the same die. Cache the current `campaign / chip / die`, design radius/gaps, power convention, acquisition settings, and expected FSR after the die context is verified.

## Standard Command

For the current formal workflow, prefer the wrapper:

```powershell
python workspace\scripts\microcavity_large_scan\large_scan_flow.py --campaign <campaign> --chip <chip> --die <die> --cavity cX
```

Use explicit stage commands only when debugging acquisition, processing, dispersion, or Q fitting.

If acquisition succeeded but analysis, plotting, standardization, or card refresh failed, do not rescan by default. Resume from the latest raw data:

```powershell
python workspace\scripts\microcavity_large_scan\large_scan_flow.py --campaign <campaign> --chip <chip> --die <die> --cavity cX --resume-existing-raw
```

If analysis outputs already exist and only the formal `Q/` layout or card refresh is missing, standardize only:

```powershell
python workspace\scripts\microcavity_large_scan\large_scan_flow.py --campaign <campaign> --chip <chip> --die <die> --cavity cX --standardize-only
```

Default onsite settings:

- scan range: 1530-1570 nm
- scan speed: 2 nm/s
- oscilloscope: 200 kSa/s, 20 s total, CH1 rising trigger at 1 V, 10 s before and 10 s after trigger
- TOPTICA PC piezo: set/read back 75 V
- restore laser to initial wavelength after scan
- cycle emission/current before scan and after fine-scan state restoration

## Acquisition Gates

Before accepting a run, verify the new acquisition only:

- `acquisition.json` shows pre-scan and post-scan emission/current cycles.
- restore target is the initial wavelength and readback is close.
- waveform header shows the CH1 trigger-centered 20 s window.
- CH2/CH3 are not saturated or clipped.
- PC piezo readback is consistent with 75 V.
- new timestamped raw `.npz` and metadata `.json` exist in that cavity's `Q/` folder.

If any gate fails, stop. Mark the group invalid and do not run family/Q fitting unless the user explicitly asks to keep it exploratory.

## Fast Onsite Review

The wrapper still produces full Q outputs and refreshes `cavity_card.html`, but live review during consecutive cavity measurements should inspect and report only:

- selected full-FSR candidate and whether half-FSR aliases were rejected;
- family continuity and max mode gap;
- D2 and fit residual consistency with neighboring cavities;
- whether an alias-boundary split needs manual merging.

If FSR and dispersion are normal, stop review there. Do not summarize Q trend, mode spectra, linewidths, Q medians, or local line shapes unless the user asks, the wrapper fails, or FSR/D2 raises a concern.

## Family Assignment

Formal folding must use the accepted full-FSR candidate from `process_summary.json`, not the design estimate, unless the user explicitly overrides it.

For family assignment:

- try the continuous-FSR graph splitter;
- if it misses visible branches, use the multi-branch per-mode-bin fallback;
- auto-center each family and prune only obvious D2-fit outliers;
- extend families only when nearby unassigned dips follow the fitted frequency curve and preserve continuous spacing;
- when two internal branches are one physical sequence split across an FSR boundary, merge them and preserve the pre-merge evidence.

Internal keys such as `upper_branch`, `middle_branch`, and `lower_branch` are script keys only. User-facing records and plots should expose `family_label` such as `mode1/mode2/mode3`. `q_by_mode.csv` should include both `family` and `family_label`; figures such as `mode_spectra.png` should show display labels in titles.

## Standard Q Folder

Accepted runs should be standardized as:

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

The `Q/` root is for formal display and daily use. `Q/evidence/` is for traceability and debugging. Cavity cards and README links should read formal display files from the `Q/` root.

Formal display contract:

- `dispersion.png`: common-coordinate family map plus representative one-FSR spectrum panel.
- `d2_fit.png`: one panel per accepted family plus representative one-FSR spectrum panel.
- right-side one-FSR panel must show normalized CH2 spectrum and assigned family markers, not a point-only fallback.
- `mode_spectra.png`: local full-resolution normalized line shapes with display label and mode index in each panel title.

## Power And Cards

Power convention: 1% input monitor port fixed at 1 uW, so input-side power is 100 uW.

For each cavity:

- measure out-coupled power after coupling;
- total throughput = `P_out / 100 uW`;
- equivalent single-ended insertion loss = `-10 log10(sqrt(throughput))` under symmetric input/output coupling;
- create or refresh a `cavity_card.html` even if the cavity is skipped, damaged, low-power, or no formal Q is available.

When the user provides a die-level output-power pass, immediately write the powers to a die-level source table such as `output_power_log.csv` in the die result directory and create or refresh all `c1`-`c9` cards with `--output-power-uw` before starting Q scans. Do not wait until die close-out to backfill insertion loss. The large-scan wrapper preserves an existing card throughput field when refreshing Q results, so early power cards are safe to create before formal Q data exists.

Treat output power/throughput as preparation evidence and Q scan data as a separate data group.

## Die Close-Out

At die close-out, use:

```text
workspace/skills/microcavity-large-scan-q/templates/die_readme_multi_family.md
```

Before writing numeric tables by hand, generate the machine summary:

```powershell
python workspace\scripts\microcavity_large_scan\summarize_die_large_scan.py --campaign <campaign> --chip <chip> --die <die>
```

This writes `die_summary.json`, `die_cavity_summary.csv`, and `die_family_summary.csv` in the die result directory. Use these files as the source of numeric table values; use prose for interpretation and caveats.

If a reviewed unified-family map already exists, pass it as JSON to generate aligned `global μ=0` candidates:

```json
{
  "Family A": {"c2": "mode2", "c5": "mode1"},
  "Family B": {"c2": "mode1", "c5": "mode2"}
}
```

```powershell
python workspace\scripts\microcavity_large_scan\summarize_die_large_scan.py --campaign <campaign> --chip <chip> --die <die> --family-map-json <family_map.json>
```

The unified alignment defaults to `--alignment-target-nm 1550`, so it prefers a compact same-longitudinal-order cluster near 1550 nm instead of an arbitrary tighter cluster at the scan edge.

For multi-family dies, do not compare local `mode1/mode2/...` labels directly across cavities. Build unified families (`Family A/B/C/...`) using:

- FSR / D1 / `n_g`;
- D2;
- same-cavity one-FSR relative branch order and offset;
- mode continuity and residuals;
- Q/depth only as secondary evidence.

Same-cavity relative branch structure is a hard guard against swapping nearby families with similar FSR/D2.

Family comparison tables should include globally aligned `mu=0` wavelength for each unified family. Choose an integer local-mode shift for each cavity/family so the same longitudinal order is compared across cavities. Do not simply copy each cavity's local `mode_number_centered=0`, and do not use the mode nearest 1550 nm used for `Q1@1550`. Include `对齐 local μ` when it is not zero.

README structure:

- design information and data state;
- unified mode-family map;
- photos;
- one compact per-cavity entry/status table combining gap, state, out-coupled power, throughput/loss, card, dispersion, D2 fit, and Q trend;
- one horizontal table per unified family;
- extra/sparse/unreliable branches;
- gap-group observations;
- die-level judgment and open caveats.

Do not keep a separate plot-entry table plus a separate power/loss table. Do not add a wide per-cavity Q summary table when family tables already carry the cross-cavity Q/D2 comparison.

Write `daily/YYYY-MM-DD.md` only at a natural boundary, such as die close-out or task switch. Daily notes are a time index and should not duplicate the full evidence chain.
