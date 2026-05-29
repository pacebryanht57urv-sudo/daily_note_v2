# chip7 / die1-1 / c7 large-scan summary

- Geometry: `R = 125 um`, gap `0.85 um`.
- Throughput: `P_out = 2.5 uW`; input monitor `1 uW`, using measured splitter ratio about `100:1`, so `P_in ~= 100 uW`.
- Inferred throughput / single-ended insertion loss: `2.5% / 8.01 dB`, assuming two symmetric coupling ends.
- Raw data: `results/chip7/die1-1/c7/large_scan_20260529_213414_1530-1570nm` with the original stored extension.
- Preparation image folder: `figures/measurement/chip7/die1-1/c7/`.

## Validity

This accepted fit uses the updated Q-branch convention: `Tmin/platform` slope alone is not used as an overcoupling criterion, and Q0/Q1 are not swapped from that slope.

Processing found `524` total dips and `87` dips with `depth > 0.2`. MZI extrema count was `249025`. Main FSR candidates were `102.29 GHz` and `205.12 GHz`.

Q fitting succeeded for `74/74` assigned modes.

## Family Summary

`deep_lower`, `deep_upper`, and `side_mid` are folded-frequency branch labels, not lower or higher wavelength labels.

| family | N | D1 / FSR | `n_g` | fit rms | most-likely Q0 | median Q0 | Q1 nearest 1550 nm | nearest wavelength | note |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `deep_lower` | 25 | 204.201 GHz | 1.8693 | 0.490 GHz | 2.440M | 2.288M | 3.585M | 1550.109 nm |  |
| `deep_upper` | 25 | 205.088 GHz | 1.8612 | 0.515 GHz | 2.304M | 2.246M | 4.366M | 1549.990 nm |  |
| `side_mid` | 24 | 204.062 GHz | 1.8705 | 0.615 GHz | 0.576M | 0.583M | 4.371M | 1549.192 nm | Tmin/platform rises with wavelength; coupling branch ambiguous; Q0/Q1 not swapped. |

## Judgment

This group is valid for Q comparison and `n_g` tracking, with the limitations implied by the residuals above. The side branch is no longer labeled as overcoupled from `Tmin` slope alone; its branch assignment is ambiguous, with unswapped median Q0 `0.583M` and Q1 near 1550 nm `4.371M`.

## Output Files

- Process summary: `large_scan_20260529_213414_1530-1570nm_process_summary.json`
- Dispersion summary: `large_scan_20260529_213414_1530-1570nm_dispersion_fit_summary.json`
- Q summary: `large_scan_20260529_213414_1530-1570nm_large_scan_q_summary.json`
- Q table: `large_scan_20260529_213414_1530-1570nm_large_scan_q_by_family.csv`
- Raw CH2/CH3 figure: `large_scan_20260529_213414_1530-1570nm_ch2_ch3_raw.png`
- Dispersion figure: `large_scan_20260529_213414_1530-1570nm_dispersion_auto_centered_depth_gt_0p2.png`
- Q trend figure: `large_scan_20260529_213414_1530-1570nm_large_scan_q_trends.png`
