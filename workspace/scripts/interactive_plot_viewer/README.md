# Interactive NPZ/MAT Comparison Viewer

Local browser tool for comparing multiple traces from `.npz` and `.mat` files.

## Run

Use an external data root. The viewer refuses to default to a repository data
directory.

```powershell
python workspace\scripts\interactive_plot_viewer\server.py --data-root "D:\daily_note_data" --download-plotly
```

Or use the project default:

```powershell
$env:DAILY_NOTE_DATA_ROOT = "D:\daily_note_data"
python workspace\scripts\interactive_plot_viewer\server.py
```

Then open:

```text
http://127.0.0.1:8765/
```

For double-click startup on Windows, run:

```text
workspace\scripts\interactive_plot_viewer\start_interactive_plot_viewer.bat
```

The batch file stops existing `interactive_plot_viewer/server.py` Python
servers, asks you to choose a data folder, downloads Plotly to the user cache if
needed, and opens the browser. If `DAILY_NOTE_DATA_ROOT` is set, that folder is
preselected in the chooser.

`--download-plotly` stores Plotly in the user cache, not in this repository.
After that cache exists, the flag is optional. On an offline machine, copy a
local `plotly.min.js` somewhere outside the repository and pass:

```powershell
python workspace\scripts\interactive_plot_viewer\server.py --data-root "D:\daily_note_data" --plotly-js "D:\tools\plotly.min.js"
```

## What It Supports

- Browse `.npz` and `.mat` files under the configured external data root.
- Inspect numeric variables and their shape/dtype.
- Plot one-dimensional arrays directly.
- Plot rows, columns, row means, or column means from two-dimensional arrays.
- Overlay multiple traces from multiple files.
- Edit an added trace by loading its settings back into the right-side controls.
- Switch the X and Y axes independently between linear and log scale.
- Apply simple display transforms per added trace:
  - no normalization
  - divide by max absolute value
  - min-max normalization
  - z-score
  - x shift
  - y offset
- Export the current interactive Plotly figure as PNG or SVG from the browser.

## MATLAB Files

Regular MATLAB `.mat` files are read through `scipy.io.loadmat`.

MATLAB v7.3 files require `h5py`. If `h5py` is not installed, the viewer reports
that clearly instead of guessing.

## Data Boundary

This tool reads from `--data-root` or `DAILY_NOTE_DATA_ROOT`. It does not write
figures, data, caches, or plot configs into this repository.
