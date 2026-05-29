# chip7 / die1-1 / c1 large-scan summary

- Geometry: `R = 125 um`, gap `0.75 um`.
- Throughput: `P_out = 5.0 uW`; input monitor `1 uW`, using measured splitter ratio about `100:1`, so `P_in ~= 100 uW`.
- Inferred throughput / single-ended insertion loss: `5.0% / 6.51 dB`, assuming two symmetric coupling ends.
- Raw data: `results/chip7/die1-1/c1/large_scan_20260528_222935_1530-1570nm` with the original stored extension.
- Preparation image folder: `figures/measurement/chip7/die1-1/c1/`.

## Validity

This accepted fit uses the updated Q-branch convention: `Tmin/platform` slope alone is not used as an overcoupling criterion, and Q0/Q1 are not swapped from that slope.

Processing found `133` total dips and `83` dips with `depth > 0.2`. MZI extrema count was `248475`. Main FSR candidates were `102.23 GHz` and `204.90 GHz`.

Q fitting succeeded for `74/74` assigned modes.

## Family Summary

`deep_lower`, `deep_upper`, and `side_mid` are folded-frequency branch labels, not lower or higher wavelength labels.

| family | N | D1 / FSR | `n_g` | fit rms | most-likely Q0 | median Q0 | Q1 nearest 1550 nm | nearest wavelength | note |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `deep_lower` | 25 | 204.069 GHz | 1.8705 | 0.998 GHz | 1.772M | 1.800M | 2.254M | 1549.933 nm |  |
| `deep_upper` | 25 | 204.928 GHz | 1.8626 | 1.252 GHz | 1.988M | 1.939M | 2.807M | 1549.806 nm | residual? |
| `side_mid` | 24 | 204.040 GHz | 1.8707 | 0.429 GHz | 0.493M | 0.479M | 2.936M | 1550.651 nm | Tmin/platform rises with wavelength; coupling branch ambiguous; Q0/Q1 not swapped. |

## Judgment

This group is valid for Q comparison and `n_g` tracking, with the limitations implied by the residuals above. The side branch is no longer labeled as overcoupled from `Tmin` slope alone; its branch assignment is ambiguous, with unswapped median Q0 `0.479M` and Q1 near 1550 nm `2.936M`.

## Output Files

- Process summary: `large_scan_20260528_222935_1530-1570nm_process_summary.json`
- Dispersion summary: `large_scan_20260528_222935_1530-1570nm_dispersion_fit_summary.json`
- Q summary: `large_scan_20260528_222935_1530-1570nm_large_scan_q_summary.json`
- Q table: `large_scan_20260528_222935_1530-1570nm_large_scan_q_by_family.csv`
- Raw CH2/CH3 figure: `large_scan_20260528_222935_1530-1570nm_ch2_ch3_raw.png`
- Dispersion figure: `large_scan_20260528_222935_1530-1570nm_dispersion_auto_centered_depth_gt_0p2.png`
- Q trend figure: `large_scan_20260528_222935_1530-1570nm_large_scan_q_trends.png`
