# chip7 / die1-2 / c4 large-scan summary

- Geometry: `R = 125 um`, gap `0.95 um`.
- Throughput: `P_out = 3.7 uW`; input monitor `1 uW`, using measured splitter ratio about `100:1`, so `P_in ~= 100 uW`.
- Inferred throughput / single-ended insertion loss: `3.7% / 7.16 dB`, assuming two symmetric coupling ends.
- Acquisition: `1530-1570 nm`, `2 nm/s`, CH1 rising trigger at `1 V`, `20 s = 10 s pre + 10 s post`, `500 kSa/s`, `npz-compressed`.
- Raw data: `results/chip7/die1-2/c4/large_scan_20260529_225256_1530-1570nm.npz`.
- Preparation image: `figures/measurement/chip7/die1-2/c4/c4??.jpg`.
## Validity

Raw-voltage flat-top gate passed. CH2 longest near-maximum visual run was `4` samples and longest near-minimum run was `7` samples; CH3 longest near-maximum and near-minimum runs were both `2` samples. This is not a visible clipped plateau.

Processing found `695` total dips and `84` dips with `depth > 0.2`. MZI extrema count was `249009`. Main FSR candidates were `102.33 GHz` and `205.24 GHz`.

Q fitting succeeded for `74/74` assigned modes.

## Family Summary

`deep_lower`, `deep_upper`, and `side_mid` are folded-frequency branch labels, not lower or higher wavelength labels. `side_mid` shows `Tmin/platform` increasing with wavelength, but the gap trend argues against using that slope alone as an overcoupling criterion; Q0/Q1 are therefore kept unswapped and the branch assignment is marked ambiguous.

| family | N | D1 / FSR | `n_g` | fit rms | most-likely Q0 | median Q0 | Q1 nearest 1550 nm | nearest wavelength | note |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| `deep_lower` | 25 | 204.264 GHz | 1.8687 | 0.474 GHz | 2.283M | 2.185M | 5.658M | 1549.734 nm |  |
| `deep_upper` | 25 | 205.368 GHz | 1.8587 | 1.367 GHz | 2.453M | 2.173M | 8.163M | 1549.610 nm | residual? |
| `side_mid` | 24 | 204.194 GHz | 1.8693 | 0.478 GHz | 0.777M | 0.739M | 6.019M | 1550.457 nm | Tmin/platform rises with wavelength; coupling branch ambiguous; Q0/Q1 not swapped. |

## Judgment

This group is valid for Q comparison and `n_g` tracking, but not for precise D2 interpretation. The side branch is no longer labeled as overcoupled from `Tmin` slope alone; its branch assignment is ambiguous. In the unswapped convention, the side-branch median Q1 is `6.019M` near 1550 nm and the median Q0 is `0.739M`, so cross-gap comparisons should explicitly state which branch is being tracked.

## Output Files

- Process summary: `large_scan_20260529_225256_1530-1570nm_process_summary.json`
- Dispersion summary: `large_scan_20260529_225256_1530-1570nm_dispersion_fit_summary.json`
- Q summary: `large_scan_20260529_225256_1530-1570nm_large_scan_q_summary.json`
- Q table: `large_scan_20260529_225256_1530-1570nm_large_scan_q_by_family.csv`
- Raw CH2/CH3 figure: `large_scan_20260529_225256_1530-1570nm_ch2_ch3_raw.png`
- Dispersion figure: `large_scan_20260529_225256_1530-1570nm_dispersion_auto_centered_depth_gt_0p2.png`
- Q trend figure: `large_scan_20260529_225256_1530-1570nm_large_scan_q_trends.png`
