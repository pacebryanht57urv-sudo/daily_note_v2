# chip7 / die1-2 / c9 invalid large-scan note

- Time: 2026-05-29 23:21:42.
- Geometry: `R = 125 um`, gap `1.00 um`.
- Throughput: `P_out = 3.8 uW`; input monitor `1 uW`, using measured splitter ratio about `100:1`, so `P_in ~= 100 uW`.
- Inferred throughput / single-ended insertion loss: `3.8% / 7.10 dB`, assuming two symmetric coupling ends.
- Acquisition attempted: `1530-1570 nm`, `2 nm/s`, CH1 rising trigger at `1 V`, `20 s = 10 s pre + 10 s post`, `500 kSa/s`, `npz-compressed`.
- Raw data: `results/chip7/die1-2/c9/large_scan_20260529_232142_1530-1570nm.npz`.
- Preparation image: `figures/measurement/chip7/die1-2/c9/c9薄膜.jpg`.

## Invalidity

Raw-voltage flat-top gate failed. CH2 was visibly saturated near the channel maximum: near-maximum fraction was `0.9999428`, with the longest near-maximum run lasting `6173776` samples. CH3 did not show the same saturation pattern.

## Judgment

This acquisition is invalid for formal Q, FSR, `n_g`, or family comparison. Processing, family assignment, and Q fitting were intentionally not run. c9 should be reacquired only after reducing the detector/scope saturation risk.
