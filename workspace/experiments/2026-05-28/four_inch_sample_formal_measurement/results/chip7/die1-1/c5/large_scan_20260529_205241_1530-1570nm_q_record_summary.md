# chip7 / die1-1 / c5 large-scan summary

- Geometry: `R = 125 um`, gap `0.80 um`.
- Throughput: `P_out = 3.5 uW`; input monitor `1 uW`, using measured splitter ratio about `100:1`, so `P_in ~= 100 uW`.
- Inferred throughput / single-ended insertion loss: `3.5% / 7.28 dB`, assuming two symmetric coupling ends.
- Raw data: `results/chip7/die1-1/c5/large_scan_20260529_205241_1530-1570nm` with the original stored extension.
- Preparation image folder: `figures/measurement/chip7/die1-1/c5/`.

## Validity

This accepted fit uses the updated Q-branch convention: `Tmin/platform` slope alone is not used as an overcoupling criterion, and Q0/Q1 are not swapped from that slope.

Processing found `732` total dips and `76` dips with `depth > 0.2`. MZI extrema count was `249030`. Main FSR candidates were `102.28 GHz` and `68.07 GHz`.

Q fitting succeeded for `25/25` assigned modes.

## Family Summary

`deep_lower`, `deep_upper`, and `side_mid` are folded-frequency branch labels, not lower or higher wavelength labels.

| family | N | D1 / FSR | `n_g` | fit rms | most-likely Q0 | median Q0 | Q1 nearest 1550 nm | nearest wavelength | note |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `deep_lower` | 25 | 204.359 GHz | 1.8678 | 0.407 GHz | 0.584M | 0.586M | 2.318M | 1549.792 nm |  |

## Judgment

This group is valid for Q comparison and `n_g` tracking, with the limitations implied by the residuals above. No continuous side branch was available in this fit.

## Output Files

- Process summary: `large_scan_20260529_205241_1530-1570nm_process_summary.json`
- Dispersion summary: `large_scan_20260529_205241_1530-1570nm_dispersion_fit_summary.json`
- Q summary: `large_scan_20260529_205241_1530-1570nm_large_scan_q_summary.json`
- Q table: `large_scan_20260529_205241_1530-1570nm_large_scan_q_by_family.csv`
- Raw CH2/CH3 figure: `large_scan_20260529_205241_1530-1570nm_ch2_ch3_raw.png`
- Dispersion figure: `large_scan_20260529_205241_1530-1570nm_dispersion_auto_centered_depth_gt_0p2.png`
- Q trend figure: `large_scan_20260529_205241_1530-1570nm_large_scan_q_trends.png`
