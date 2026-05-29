# chip7 / die1-2 / c1 large-scan summary

- Geometry: `R = 125 um`, gap `0.90 um`.
- Throughput: `P_out = 6.0 uW`; input monitor `1 uW`, using measured splitter ratio about `100:1`, so `P_in ~= 100 uW`.
- Inferred throughput / single-ended insertion loss: `6.0% / 6.11 dB`, assuming two symmetric coupling ends.
- Acquisition: `1530-1570 nm`, `2 nm/s`, CH1 rising trigger at `1 V`, `20 s = 10 s pre + 10 s post`, `500 kSa/s`, `npz-compressed`.
- Raw data: `results/chip7/die1-2/c1/large_scan_20260529_222227_1530-1570nm.npz`.
- Preparation image: `figures/measurement/chip7/die1-2/c1/c1??.jpg`.
## Validity

Raw-voltage flat-top gate passed. CH2/CH3 showed no visible clipped plateau in the accepted acquisition. This is not a visible clipped plateau.

Processing found `812` total dips and `86` dips with `depth > 0.2`. MZI extrema count was `249039`. Main FSR candidates were `102.46 GHz` and `205.36 GHz`.

Q fitting succeeded for `74/74` assigned modes.

## Family Summary

`deep_lower`, `deep_upper`, and `side_mid` are folded-frequency branch labels, not lower or higher wavelength labels. `side_mid` shows `Tmin/platform` increasing with wavelength, but the gap trend argues against using that slope alone as an overcoupling criterion; Q0/Q1 are therefore kept unswapped and the branch assignment is marked ambiguous.

| family | N | D1 / FSR | `n_g` | fit rms | most-likely Q0 | median Q0 | Q1 nearest 1550 nm | nearest wavelength | note |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `deep_lower` | 25 | 204.600 GHz | 1.8656 | 0.887 GHz | 2.006M | 2.403M | 5.572M | 1549.686 nm |  |
| `deep_upper` | 25 | 205.610 GHz | 1.8565 | 1.319 GHz | 2.262M | 2.258M | 5.947M | 1549.562 nm | residual? |
| `side_mid` | 24 | 204.531 GHz | 1.8663 | 0.753 GHz | 0.650M | 0.673M | 5.660M | 1550.411 nm | Tmin/platform rises with wavelength; coupling branch ambiguous; Q0/Q1 not swapped. |

## Judgment

This group is valid for Q comparison and `n_g` tracking, but not for precise D2 interpretation. The side branch is no longer labeled as overcoupled from `Tmin` slope alone; its branch assignment is ambiguous. In the unswapped convention, the side-branch median Q1 is `5.660M` near 1550 nm and the median Q0 is `0.673M`, so cross-gap comparisons should explicitly state which branch is being tracked.

## Output Files

- Process summary: `large_scan_20260529_222227_1530-1570nm_process_summary.json`
- Dispersion summary: `large_scan_20260529_222227_1530-1570nm_dispersion_fit_summary.json`
- Q summary: `large_scan_20260529_222227_1530-1570nm_large_scan_q_summary.json`
- Q table: `large_scan_20260529_222227_1530-1570nm_large_scan_q_by_family.csv`
- Raw CH2/CH3 figure: `large_scan_20260529_222227_1530-1570nm_ch2_ch3_raw.png`
- Dispersion figure: `large_scan_20260529_222227_1530-1570nm_dispersion_auto_centered_depth_gt_0p2.png`
- Q trend figure: `large_scan_20260529_222227_1530-1570nm_large_scan_q_trends.png`
