---
name: scientific-plotting
description: Use whenever creating, updating, or regenerating scientific figures for experiments, measurements, reports, talks, session.md records, or data analysis; ensures plots are readable in meeting-room projection and labels do not obscure data.
---

# Scientific Plotting

## Core Rule

Every new figure should be readable when projected in a meeting room. Prioritize clear communication over packing everything inside the axes.

This skill applies to new plots only unless the user asks to revise old figures.

## Default Figure Style

- Use large fonts by default:
  - title: 18-22 pt
  - axis labels: 16-18 pt
  - tick labels: 13-15 pt
  - legend/text annotations: 13-15 pt
- Use sufficiently thick lines and markers:
  - line width: at least 2 px for main traces
  - marker size: at least 5-7 px when markers matter
- Use high enough output resolution:
  - PNG: at least 180-300 dpi for reports or slides
  - SVG/PDF is preferred when the downstream document supports vector figures
- Keep color choices distinguishable on a projector and in grayscale when possible.

## Labels And Legends

- Do not let legends, text boxes, or parameter labels cover important curves, peaks, dips, ticks, grid labels, or annotations.
- If a legend or parameter description is long, place it outside the plotting axes, usually on the right or below the plot.
- Put detailed run parameters in the caption, side panel, or session text instead of inside the data area.
- Prefer concise labels in the figure and fuller explanations in `session.md`.

## Layout Checks

Before saving a figure:

1. Check that axis labels and tick labels are not clipped.
2. Check that the legend does not overlap the plotted signal or key annotations.
3. Check that long labels wrap or move outside the axes.
4. Check that the figure still works at slide scale, not only when zoomed in.
5. If there are multiple stages or parameter groups, use color bands or separate panels with a clear external legend.

## Matplotlib Defaults

When using Matplotlib, start from settings like:

```python
plt.rcParams.update({
    "font.size": 14,
    "axes.titlesize": 20,
    "axes.labelsize": 17,
    "xtick.labelsize": 14,
    "ytick.labelsize": 14,
    "legend.fontsize": 13,
    "lines.linewidth": 2.2,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
})
```

For long legends:

```python
fig, ax = plt.subplots(figsize=(10, 6), constrained_layout=True)
ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), borderaxespad=0)
```

If the external legend needs more room, enlarge the figure instead of shrinking fonts.

## Measurement Figures

For scope traces, spectra, PID sweeps, and lock timelines:

- Clearly state what each channel means, not only `CH1` or `CH2`.
- Put key numeric settings outside the axes when they are long.
- If showing lock stages, mark stages with readable color bands and a legend outside the data area.
- When plotting an accepted data group for external `session.md`, save both the raw data path and the representative figure path under `$DAILY_NOTE_DATA_ROOT` or a user-provided external output directory. Do not save generated figures into tracked repo paths.

