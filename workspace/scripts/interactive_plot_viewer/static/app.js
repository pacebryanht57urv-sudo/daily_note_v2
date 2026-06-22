"use strict";

const state = {
  currentPath: "",
  selectedPath: "",
  variables: [],
  traces: [],
  nextTraceId: 1,
  editingTraceId: null,
  palette: ["#0f766e", "#be123c", "#2563eb", "#ca8a04", "#7c3aed", "#15803d", "#c2410c", "#0369a1"],
};

const elements = {
  statusText: document.getElementById("statusText"),
  refreshButton: document.getElementById("refreshButton"),
  exportPngButton: document.getElementById("exportPngButton"),
  exportSvgButton: document.getElementById("exportSvgButton"),
  upButton: document.getElementById("upButton"),
  currentPath: document.getElementById("currentPath"),
  fileList: document.getElementById("fileList"),
  selectedFile: document.getElementById("selectedFile"),
  xVar: document.getElementById("xVar"),
  xComponent: document.getElementById("xComponent"),
  xMode: document.getElementById("xMode"),
  xIndex: document.getElementById("xIndex"),
  yVar: document.getElementById("yVar"),
  yComponent: document.getElementById("yComponent"),
  yMode: document.getElementById("yMode"),
  yIndex: document.getElementById("yIndex"),
  traceName: document.getElementById("traceName"),
  normalizeMode: document.getElementById("normalizeMode"),
  yOffset: document.getElementById("yOffset"),
  xShift: document.getElementById("xShift"),
  xAxisScale: document.getElementById("xAxisScale"),
  yAxisScale: document.getElementById("yAxisScale"),
  addTraceButton: document.getElementById("addTraceButton"),
  cancelEditButton: document.getElementById("cancelEditButton"),
  clearButton: document.getElementById("clearButton"),
  traceList: document.getElementById("traceList"),
  toast: document.getElementById("toast"),
};

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 4200);
}

async function apiGet(path) {
  const response = await fetch(path);
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || `Request failed: ${path}`);
  }
  return payload;
}

async function apiPost(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || `Request failed: ${path}`);
  }
  return payload;
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function variableLabel(variable) {
  const shape = variable.shape.join(" x ");
  const complex = variable.complex ? ", complex" : "";
  const usable = variable.usable ? "" : ", unsupported";
  return `${variable.name} [${shape}] ${variable.dtype}${complex}${usable}`;
}

function selectedVariable(select) {
  return state.variables.find((item) => item.name === select.value);
}

function variableSpec(kind) {
  const prefix = kind === "x" ? "x" : "y";
  const select = elements[`${prefix}Var`];
  const name = select.value;
  if (!name) return null;
  return {
    name,
    component: elements[`${prefix}Component`].value,
    mode: elements[`${prefix}Mode`].value,
    index: Number(elements[`${prefix}Index`].value || 0),
  };
}

function syncModeControls() {
  for (const prefix of ["x", "y"]) {
    const variable = selectedVariable(elements[`${prefix}Var`]);
    const needs2d = variable && variable.usable && variable.ndim === 2;
    const isComplex = variable && variable.complex;
    elements[`${prefix}Mode`].disabled = !needs2d;
    elements[`${prefix}Index`].disabled = !needs2d;
    elements[`${prefix}Component`].disabled = !isComplex;
  }
}

async function loadStatus() {
  const status = await apiGet("/api/status");
  const h5 = status.h5py ? "h5py ready" : "h5py missing";
  elements.statusText.textContent = `${status.data_root} | numpy ${status.numpy} | ${h5}`;
}

async function loadDirectory(path = state.currentPath) {
  const payload = await apiGet(`/api/list?path=${encodeURIComponent(path || "")}`);
  state.currentPath = payload.path || "";
  elements.currentPath.textContent = state.currentPath ? `/${state.currentPath}` : "/";
  elements.upButton.disabled = !state.currentPath;
  renderFileList(payload);
}

function renderFileList(payload) {
  elements.fileList.innerHTML = "";

  for (const dir of payload.dirs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "file-row";
    button.innerHTML = `<span>DIR</span><span class="file-name"></span>`;
    button.querySelector(".file-name").textContent = dir.name;
    button.addEventListener("click", () => loadDirectory(dir.path).catch(reportError));
    elements.fileList.appendChild(button);
  }

  for (const file of payload.files) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `file-row${file.path === state.selectedPath ? " active" : ""}`;
    const suffix = file.suffix.replace(".", "").toUpperCase();
    button.innerHTML = `<span>${suffix}</span><span><span class="file-name"></span><span class="file-meta"></span></span>`;
    button.querySelector(".file-name").textContent = file.name;
    button.querySelector(".file-meta").textContent = ` ${formatBytes(file.size)}`;
    button.addEventListener("click", () => inspectFile(file.path, {resetForm: true}).catch(reportError));
    elements.fileList.appendChild(button);
  }

  if (!payload.dirs.length && !payload.files.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "No .npz or .mat files in this directory.";
    elements.fileList.appendChild(empty);
  }
}

async function inspectFile(path, options = {}) {
  const payload = await apiGet(`/api/inspect?path=${encodeURIComponent(path)}`);
  state.selectedPath = path;
  state.variables = payload.variables.filter((item) => item.usable);
  elements.selectedFile.textContent = path;
  if (options.resetForm) {
    elements.traceName.value = "";
    clearEditMode();
  }
  populateVariableSelects();
  await loadDirectory(state.currentPath);
}

function populateVariableSelects() {
  const usable = state.variables;

  function fill(select, includeIndex) {
    select.innerHTML = "";
    if (includeIndex) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "index";
      select.appendChild(option);
    }
    for (const variable of usable) {
      const option = document.createElement("option");
      option.value = variable.name;
      option.textContent = variableLabel(variable);
      select.appendChild(option);
    }
  }

  fill(elements.xVar, true);
  fill(elements.yVar, false);
  const yGuess = usable.find((item) => /^(y|trace|voltage|transmission|power|q)$/i.test(item.name)) || usable[0];
  const xGuess = usable.find((item) => /^(x|time|freq|frequency|wavelength)$/i.test(item.name));
  elements.yVar.value = yGuess ? yGuess.name : "";
  elements.xVar.value = xGuess ? xGuess.name : "";
  syncModeControls();
}

function applySpecToControls(kind, spec) {
  const prefix = kind === "x" ? "x" : "y";
  if (!spec || !spec.name) {
    elements[`${prefix}Var`].value = "";
  } else {
    elements[`${prefix}Var`].value = spec.name;
  }
  elements[`${prefix}Component`].value = spec && spec.component ? spec.component : "real";
  elements[`${prefix}Mode`].value = spec && spec.mode ? spec.mode : "row";
  elements[`${prefix}Index`].value = spec && Number.isFinite(Number(spec.index)) ? String(spec.index) : "0";
  syncModeControls();
}

function setEditMode(traceId) {
  state.editingTraceId = traceId;
  elements.addTraceButton.textContent = "Update Trace";
  elements.cancelEditButton.hidden = false;
  renderTraces();
}

function clearEditMode() {
  state.editingTraceId = null;
  elements.addTraceButton.textContent = "Add Trace";
  elements.cancelEditButton.hidden = true;
  renderTraces();
}

function normalize(values, mode) {
  const y = values.slice();
  if (mode === "none") return y;
  const finite = y.filter(Number.isFinite);
  if (!finite.length) return y;
  if (mode === "max") {
    const scale = Math.max(...finite.map((value) => Math.abs(value)));
    return scale ? y.map((value) => value / scale) : y;
  }
  if (mode === "minmax") {
    const min = Math.min(...finite);
    const max = Math.max(...finite);
    const span = max - min;
    return span ? y.map((value) => (value - min) / span) : y;
  }
  if (mode === "zscore") {
    const mean = finite.reduce((sum, value) => sum + value, 0) / finite.length;
    const variance = finite.reduce((sum, value) => sum + (value - mean) ** 2, 0) / finite.length;
    const sigma = Math.sqrt(variance);
    return sigma ? y.map((value) => (value - mean) / sigma) : y;
  }
  return y;
}

function axisType(axis) {
  const value = axis === "x" ? elements.xAxisScale.value : elements.yAxisScale.value;
  return value === "log" ? "log" : "linear";
}

function hasNonPositiveValues(axis) {
  const key = axis === "x" ? "x" : "y";
  return state.traces.some((trace) => (
    trace.visible !== false && trace[key].some((value) => Number.isFinite(value) && value <= 0)
  ));
}

function warnForLogScale() {
  const warnings = [];
  if (axisType("x") === "log" && hasNonPositiveValues("x")) {
    warnings.push("X");
  }
  if (axisType("y") === "log" && hasNonPositiveValues("y")) {
    warnings.push("Y");
  }
  if (warnings.length) {
    showToast(`${warnings.join(" and ")} log scale hides zero or negative values.`);
  }
}

async function buildTraceFromForm(existingTrace = null) {
  if (!state.selectedPath) {
    showToast("Choose a data file first.");
    return null;
  }
  const ySpec = variableSpec("y");
  if (!ySpec || !ySpec.name) {
    showToast("Choose a Y variable.");
    return null;
  }
  const payload = await apiPost("/api/trace", {
    path: state.selectedPath,
    x: variableSpec("x"),
    y: ySpec,
  });

  const normalizeMode = elements.normalizeMode.value;
  const xShift = Number(elements.xShift.value || 0);
  const yOffset = Number(elements.yOffset.value || 0);
  const color = existingTrace
    ? existingTrace.color
    : state.palette[(state.nextTraceId - 1) % state.palette.length];
  const name = elements.traceName.value.trim()
    || `${state.selectedPath.split("/").pop()} : ${ySpec.name}`;
  const trace = {
    id: existingTrace ? existingTrace.id : state.nextTraceId++,
    visible: existingTrace ? existingTrace.visible : true,
    color,
    name,
    x: payload.x.map((value) => value + xShift),
    y: normalize(payload.y, normalizeMode).map((value) => value + yOffset),
    meta: {
      path: state.selectedPath,
      x: variableSpec("x"),
      y: ySpec,
      normalizeMode,
      xShift,
      yOffset,
      points: payload.points,
      originalPoints: payload.original_points,
      downsampled: payload.downsampled,
    },
  };
  if (payload.downsampled) {
    showToast(`Trace downsampled from ${payload.original_points} to ${payload.points} points.`);
  }
  return trace;
}

async function addOrUpdateTrace() {
  const editingTrace = state.editingTraceId === null
    ? null
    : state.traces.find((trace) => trace.id === state.editingTraceId);
  const trace = await buildTraceFromForm(editingTrace);
  if (!trace) return;

  if (editingTrace) {
    state.traces = state.traces.map((item) => (item.id === trace.id ? trace : item));
    clearEditMode();
  } else {
    state.traces.push(trace);
  }
  renderTraces();
  renderPlot();
}

async function editTrace(trace) {
  await inspectFile(trace.meta.path, {resetForm: false});
  elements.traceName.value = trace.name;
  elements.normalizeMode.value = trace.meta.normalizeMode || "none";
  elements.xShift.value = String(trace.meta.xShift || 0);
  elements.yOffset.value = String(trace.meta.yOffset || 0);
  applySpecToControls("x", trace.meta.x);
  applySpecToControls("y", trace.meta.y);
  setEditMode(trace.id);
}

function renderTraces() {
  elements.traceList.innerHTML = "";
  if (!state.traces.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "No traces added yet.";
    elements.traceList.appendChild(empty);
    return;
  }

  for (const trace of state.traces) {
    const item = document.createElement("div");
    item.className = `trace-item${trace.id === state.editingTraceId ? " editing" : ""}`;
    item.innerHTML = `
      <span class="trace-swatch"></span>
      <span>
        <span class="trace-title"></span>
        <span class="file-meta"></span>
      </span>
      <span class="trace-actions">
        <button type="button" data-action="edit">Edit</button>
        <button type="button" data-action="toggle">${trace.visible ? "Hide" : "Show"}</button>
        <button type="button" data-action="remove">X</button>
      </span>
    `;
    item.querySelector(".trace-swatch").style.background = trace.color;
    item.querySelector(".trace-title").textContent = trace.name;
    item.querySelector(".file-meta").textContent = ` ${trace.meta.points} pts`;
    item.querySelector('[data-action="edit"]').addEventListener("click", () => {
      editTrace(trace).catch(reportError);
    });
    item.querySelector('[data-action="toggle"]').addEventListener("click", () => {
      trace.visible = !trace.visible;
      renderTraces();
      renderPlot();
    });
    item.querySelector('[data-action="remove"]').addEventListener("click", () => {
      state.traces = state.traces.filter((itemTrace) => itemTrace.id !== trace.id);
      if (state.editingTraceId === trace.id) {
        clearEditMode();
      }
      renderTraces();
      renderPlot();
    });
    elements.traceList.appendChild(item);
  }
}

function renderPlot() {
  if (!window.Plotly) {
    document.getElementById("plot").innerHTML = `
      <div class="plot-missing">
        <h2>Plotly is not loaded</h2>
        <p>Restart the server with --download-plotly or pass --plotly-js PATH.</p>
      </div>
    `;
    return;
  }

  const traces = state.traces.map((trace) => ({
    x: trace.x,
    y: trace.y,
    name: trace.name,
    type: "scatter",
    mode: "lines",
    visible: trace.visible ? true : "legendonly",
    line: {color: trace.color, width: 2.2},
    hovertemplate: "x=%{x:.6g}<br>y=%{y:.6g}<extra>%{fullData.name}</extra>",
  }));

  const layout = {
    margin: {l: 72, r: 24, t: 26, b: 60},
    paper_bgcolor: "#ffffff",
    plot_bgcolor: "#ffffff",
    font: {family: "Segoe UI, Arial, sans-serif", size: 14, color: "#172026"},
    xaxis: {
      type: axisType("x"),
      title: {text: "x", font: {size: 17}},
      tickfont: {size: 14},
      showgrid: true,
      gridcolor: "#e5ecef",
      zerolinecolor: "#cbd5da",
    },
    yaxis: {
      type: axisType("y"),
      title: {text: "y", font: {size: 17}},
      tickfont: {size: 14},
      showgrid: true,
      gridcolor: "#e5ecef",
      zerolinecolor: "#cbd5da",
    },
    legend: {
      orientation: "v",
      x: 1,
      xanchor: "right",
      y: 1,
      bgcolor: "rgba(255,255,255,0.86)",
      bordercolor: "#d8e0e4",
      borderwidth: 1,
      font: {size: 13},
    },
  };

  const config = {
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ["lasso2d", "select2d"],
  };

  Plotly.react("plot", traces, layout, config);
}

function clearTraces() {
  state.traces = [];
  clearEditMode();
  renderTraces();
  renderPlot();
}

function exportPlot(format) {
  if (!window.Plotly) {
    showToast("Plotly is not loaded; export is unavailable.");
    return;
  }
  const options = {
    format,
    filename: `comparison_${new Date().toISOString().replace(/[:.]/g, "-")}`,
    width: 1600,
    height: 950,
    scale: 2,
  };
  Plotly.downloadImage("plot", options).catch(reportError);
}

function reportError(error) {
  console.error(error);
  showToast(error.message || String(error));
}

function bindEvents() {
  elements.refreshButton.addEventListener("click", () => {
    Promise.all([loadStatus(), loadDirectory()]).catch(reportError);
  });
  elements.exportPngButton.addEventListener("click", () => exportPlot("png"));
  elements.exportSvgButton.addEventListener("click", () => exportPlot("svg"));
  elements.upButton.addEventListener("click", () => {
    const parts = state.currentPath.split("/").filter(Boolean);
    parts.pop();
    loadDirectory(parts.join("/")).catch(reportError);
  });
  for (const element of [elements.xVar, elements.yVar]) {
    element.addEventListener("change", syncModeControls);
  }
  for (const element of [elements.xAxisScale, elements.yAxisScale]) {
    element.addEventListener("change", () => {
      warnForLogScale();
      renderPlot();
    });
  }
  elements.addTraceButton.addEventListener("click", () => addOrUpdateTrace().catch(reportError));
  elements.cancelEditButton.addEventListener("click", clearEditMode);
  elements.clearButton.addEventListener("click", clearTraces);
}

async function boot() {
  bindEvents();
  renderTraces();
  renderPlot();
  await loadStatus();
  await loadDirectory("");
}

boot().catch(reportError);
