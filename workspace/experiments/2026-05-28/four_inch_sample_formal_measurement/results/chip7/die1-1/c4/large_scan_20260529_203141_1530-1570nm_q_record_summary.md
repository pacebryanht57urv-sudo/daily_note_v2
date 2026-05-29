# chip7 / die1-1 / c4 large-scan summary

- Geometry: `R = 125 um`, gap `0.80 um`.
- Throughput: `P_out = 1.5 uW`; input monitor `1 uW`, using measured splitter ratio about `100:1`, so `P_in ~= 100 uW`.
- Inferred throughput / single-ended insertion loss: `1.5% / 9.12 dB`, assuming two symmetric coupling ends.
- Raw data: `results/chip7/die1-1/c4/large_scan_20260529_203141_1530-1570nm` with the original stored extension.
- Preparation image folder: `figures/measurement/chip7/die1-1/c4/`.

## Validity

This accepted fit uses the updated Q-branch convention: `Tmin/platform` slope alone is not used as an overcoupling criterion, and Q0/Q1 are not swapped from that slope.

Processing found `324` total dips and `85` dips with `depth > 0.2`. MZI extrema count was `249072`. Main FSR candidates were `102.80 GHz` and `206.11 GHz`.

Q fitting succeeded for `74/74` assigned modes.

## Family Summary

`deep_lower`, `deep_upper`, and `side_mid` are folded-frequency branch labels, not lower or higher wavelength labels.

| family | N | D1 / FSR | `n_g` | fit rms | most-likely Q0 | median Q0 | Q1 nearest 1550 nm | nearest wavelength | note |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `deep_lower` | 25 | 205.191 GHz | 1.8603 | 0.037 GHz | 2.167M | 1.937M | 2.447M | 1549.826 nm |  |
| `deep_upper` | 25 | 206.064 GHz | 1.8524 | 0.012 GHz | 1.735M | 1.782M | 3.046M | 1549.702 nm |  |
| `side_mid` | 24 | 205.120 GHz | 1.8609 | 0.009 GHz | 0.497M | 0.475M | 3.250M | 1550.545 nm | Tmin/platform rises with wavelength; coupling branch ambiguous; Q0/Q1 not swapped. |

## Judgment

This group is valid for Q comparison and `n_g` tracking, with the limitations implied by the residuals above. The side branch is no longer labeled as overcoupled from `Tmin` slope alone; its branch assignment is ambiguous, with unswapped median Q0 `0.475M` and Q1 near 1550 nm `3.250M`.

## Output Files

- Process summary: `large_scan_20260529_203141_1530-1570nm_process_summary.json`
- Dispersion summary: `large_scan_20260529_203141_1530-1570nm_dispersion_fit_summary.json`
- Q summary: `large_scan_20260529_203141_1530-1570nm_large_scan_q_summary.json`
- Q table: `large_scan_20260529_203141_1530-1570nm_large_scan_q_by_family.csv`
- Raw CH2/CH3 figure: `large_scan_20260529_203141_1530-1570nm_ch2_ch3_raw.png`
- Dispersion figure: `large_scan_20260529_203141_1530-1570nm_dispersion_auto_centered_depth_gt_0p2.png`
- Q trend figure: `large_scan_20260529_203141_1530-1570nm_large_scan_q_trends.png`
