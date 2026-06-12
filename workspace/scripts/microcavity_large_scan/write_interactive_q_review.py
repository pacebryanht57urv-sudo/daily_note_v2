#!/usr/bin/env python3
"""Write a lightweight interactive Q / dispersion HTML review for one cavity."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Iterable

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fit_large_scan_dispersion import (  # noqa: E402
    assigned_labels_by_sample,
    build_common_rows,
    choose_common_origin,
    display_labels_by_depth,
    normalize_transmission_with_baseline,
    read_large_scan_data,
    representative_side_panel_rows,
)


COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("cavity_dir", type=Path, help="Cavity result directory containing Q/.")
    parser.add_argument("--output", type=Path, default=None, help="Defaults to Q/interactive_q.html.")
    parser.add_argument("--trace-step", type=int, default=5, help="Keep every Nth point in dense traces.")
    parser.add_argument("--max-one-fsr-points", type=int, default=5000)
    parser.add_argument("--max-local-points", type=int, default=400, help="Maximum points embedded per local Q window.")
    return parser.parse_args(list(argv))


def latest_evidence_dir(q_dir: Path) -> Path:
    dirs = [path for path in (q_dir / "evidence").glob("processing_*") if path.is_dir()]
    if not dirs:
        raise FileNotFoundError(f"No processing_* evidence directory under {q_dir / 'evidence'}")
    return max(dirs, key=lambda path: path.stat().st_mtime)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def numeric_row(row: dict[str, str]) -> dict[str, float | str]:
    item: dict[str, float | str] = {}
    for key, value in row.items():
        if value is None or value == "":
            item[key] = value or ""
            continue
        try:
            item[key] = float(value)
        except ValueError:
            item[key] = value
    return item


def decimate(points: list, *, step: int, max_points: int | None = None) -> list:
    if step > 1 and len(points) > step:
        points = points[::step] + ([] if points[-1] == points[::step][-1] else [points[-1]])
    if max_points and len(points) > max_points:
        stride = math.ceil(len(points) / max_points)
        points = points[::stride] + ([] if points[-1] == points[::stride][-1] else [points[-1]])
    return points


def load_family_data(q_dir: Path) -> tuple[dict[str, list[dict[str, float]]], dict[str, str]]:
    rows = [numeric_row(row) for row in read_csv_rows(q_dir / "family_points.csv")]
    families: dict[str, list[dict[str, float]]] = {}
    labels: dict[str, str] = {}
    for row in rows:
        family = str(row["family"])
        labels[family] = str(row.get("family_label") or family)
        families.setdefault(family, []).append(row)  # type: ignore[arg-type]
    for family_rows in families.values():
        family_rows.sort(key=lambda item: float(item["sample_index"]))
    return families, labels


def color_map(labels: Iterable[str]) -> dict[str, str]:
    return {label: COLORS[index % len(COLORS)] for index, label in enumerate(labels)}


def one_fsr_payload(
    q_dir: Path,
    families: dict[str, list[dict[str, float]]],
    fits: list[dict[str, object]],
    *,
    trace_step: int,
    max_points: int,
) -> tuple[list[list[float]], list[dict[str, object]], float, int, list[str], list[str]]:
    origin = choose_common_origin(families, fits)
    if origin is None:
        return [], [], 1.0, 0, [], []
    _origin_family, origin_row, common_d1_mhz = origin
    common_rows = build_common_rows(
        [row for rows in families.values() for row in rows],
        origin_mhz=float(origin_row["relative_freq_mhz"]),
        common_d1_mhz=common_d1_mhz,
    )
    assigned = assigned_labels_by_sample(families)
    selected_mode, side_rows, target_labels, plotted_labels = representative_side_panel_rows(common_rows, assigned)
    if len(side_rows) < 2:
        return [], [], common_d1_mhz / 1000.0, selected_mode, sorted(target_labels), sorted(plotted_labels)

    _time_s, _trigger, trans_raw, _mzi_raw = read_large_scan_data(q_dir / "raw.npz")
    trans_norm, _baseline = normalize_transmission_with_baseline(trans_raw)
    control = sorted(
        (
            int(float(row["sample_index"])),
            float(row["common_mode_number"]) * common_d1_mhz + float(row["common_folded_mhz"]),
        )
        for row in common_rows
    )
    control_samples = np.array([item[0] for item in control], dtype=float)
    control_unfolded = np.array([item[1] for item in control], dtype=float)
    side_samples = np.array([int(float(row["sample_index"])) for row in side_rows], dtype=int)
    pad = max(10_000, int(0.15 * (int(side_samples.max()) - int(side_samples.min()))))
    lo = max(0, int(side_samples.min()) - pad)
    hi = min(len(trans_norm) - 1, int(side_samples.max()) + pad)
    sample_grid = np.arange(lo, hi + 1)
    unfolded = np.interp(sample_grid, control_samples, control_unfolded)
    trace_mode = np.rint(unfolded / common_d1_mhz).astype(int)
    trace_folded = unfolded - trace_mode * common_d1_mhz
    mask = np.abs(trace_folded) <= common_d1_mhz / 2.0
    trace = [
        [float(x) / 1000.0, float(y)]
        for x, y in zip(trace_folded[mask], trans_norm[sample_grid][mask], strict=False)
    ]
    trace = decimate(trace, step=trace_step, max_points=max_points)

    labels_by_family = display_labels_by_depth(families)
    colors = color_map(labels_by_family.values())
    markers: list[dict[str, object]] = []
    for row in side_rows:
        sample = int(float(row["sample_index"]))
        label_row = assigned.get(sample)
        if label_row is None:
            continue
        label, assigned_row = label_row
        markers.append(
            {
                "family": str(row.get("family", "")),
                "family_label": label,
                "color": colors.get(label, "#111111"),
                "mode_number": int(float(assigned_row.get("mode_number_centered", row["common_mode_number"]))),
                "x_GHz": float(row["common_folded_mhz"]) / 1000.0,
                "norm_T": float(assigned_row["norm_transmission"]),
                "depth": float(assigned_row["depth_1_minus_norm"]),
                "wavelength_nm": float(assigned_row["wavelength_nm_linear"]),
            }
        )
    return trace, markers, common_d1_mhz / 1000.0, selected_mode, sorted(target_labels), sorted(plotted_labels)


def sample_to_wavelength_interpolator(families: dict[str, list[dict[str, float]]]):
    pairs = sorted(
        (int(float(row["sample_index"])), float(row["wavelength_nm_linear"]))
        for rows in families.values()
        for row in rows
    )
    samples = np.array([item[0] for item in pairs], dtype=float)
    wavelengths = np.array([item[1] for item in pairs], dtype=float)
    return samples, wavelengths


def local_windows(
    q_dir: Path,
    families: dict[str, list[dict[str, float]]],
    labels: dict[str, str],
    colors: dict[str, str],
    *,
    trace_step: int,
    max_points: int,
) -> list[dict[str, object]]:
    q_rows = read_csv_rows(q_dir / "q_by_mode.csv")
    _time_s, _trigger, trans_raw, _mzi_raw = read_large_scan_data(q_dir / "raw.npz")
    trans_norm, _baseline = normalize_transmission_with_baseline(trans_raw)
    control_samples, control_wavelengths = sample_to_wavelength_interpolator(families)
    locals_payload: list[dict[str, object]] = []
    for index, row in enumerate(q_rows):
        if row.get("fit_status") != "ok":
            continue
        family = row["family"]
        label = labels.get(family, family)
        center = int(float(row["sample_index"]))
        half = int(float(row.get("half_window_samples") or 1200))
        lo = max(0, center - half)
        hi = min(len(trans_norm) - 1, center + half)
        sample_grid = np.arange(lo, hi + 1)
        wavelengths = np.interp(sample_grid, control_samples, control_wavelengths)
        data = [[float(x), float(y)] for x, y in zip(wavelengths, trans_norm[sample_grid], strict=False)]
        data = decimate(data, step=trace_step, max_points=max_points)
        locals_payload.append(
            {
                "id": index,
                "label": f"{label} mu={int(float(row['mode_number']))} {float(row['wavelength_nm']):.3f} nm",
                "family": family,
                "family_label": label,
                "color": colors.get(label, "#111111"),
                "mode_number": int(float(row["mode_number"])),
                "wavelength_nm": float(row["wavelength_nm"]),
                "depth": float(row["depth"]),
                "QL_M": float(row["QL"]) / 1e6,
                "Q0_M": float(row["Q0"]) / 1e6,
                "Q1_M": float(row["Q1"]) / 1e6,
                "transmission": float(row["transmission"]),
                "linewidth_MHz": float(row["linewidth_loaded_mhz"]),
                "fit_status": row.get("fit_status", ""),
                "coupling_note": row.get("coupling_note", ""),
                "local_data": data,
            }
        )
    return locals_payload


def dispersion_payload(fits: list[dict[str, object]], labels: dict[str, str], colors: dict[str, str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for fit in fits:
        if "quadratic" not in fit:
            continue
        family = str(fit["name"])
        label = labels.get(family, family)
        quadratic = fit["quadratic"]
        offset = float(quadratic["offset_mhz"])
        d1_corr = float(quadratic["d1_correction_mhz"])
        d2 = float(quadratic["d2_mhz_per_mode2"])
        points = []
        modes = []
        for point in fit.get("points", []):
            mode = int(point["mode_number"])
            modes.append(mode)
            y = (float(point["folded_freq_mhz"]) - d1_corr * mode) / 1000.0
            fit_y = (offset + 0.5 * d2 * mode * mode) / 1000.0
            points.append(
                {
                    "mode_number": mode,
                    "dint_GHz": y,
                    "fit_GHz": fit_y,
                    "residual_MHz": float(point.get("residual_mhz", 0.0)),
                    "wavelength_nm": float(point["wavelength_nm"]),
                }
            )
        if not modes:
            continue
        xs = np.linspace(min(modes), max(modes), 160)
        fit_curve = [[float(x), float((offset + 0.5 * d2 * x * x) / 1000.0)] for x in xs]
        out.append(
            {
                "family": family,
                "family_label": label,
                "color": colors.get(label, "#111111"),
                "D1_GHz": float(quadratic["effective_d1_mhz"]) / 1000.0,
                "D2_MHz": d2,
                "rms_MHz": float(quadratic["rms_residual_mhz"]),
                "points": points,
                "fit_curve": fit_curve,
            }
        )
    return out


def q_trend_payload(locals_payload: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "family": item["family"],
            "family_label": item["family_label"],
            "color": item["color"],
            "mode_number": item["mode_number"],
            "wavelength_nm": item["wavelength_nm"],
            "QL_M": item["QL_M"],
            "Q0_M": item["Q0_M"],
            "Q1_M": item["Q1_M"],
            "transmission": item["transmission"],
            "depth": item["depth"],
            "linewidth_MHz": item["linewidth_MHz"],
        }
        for item in locals_payload
    ]


def html_template(payload: dict[str, object]) -> str:
    payload_js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{payload['title']}</title>
<style>
  body {{ margin:0; font-family:Arial, sans-serif; background:#fff; color:#000; }}
  #wrap {{ padding:14px 18px 24px; max-width:1540px; margin:auto; }}
  h1 {{ margin:0 0 4px; font-size:26px; font-weight:650; color:#000; }}
  #sub {{ color:#000; margin-bottom:12px; font-size:14px; }}
  #grid {{ display:grid; grid-template-columns:1.05fr .95fr; gap:14px; }}
  .panel {{ background:#fff; border:1px solid #000; border-radius:6px; padding:10px; min-width:0; }}
  .panel h2 {{ font-size:18px; margin:0 0 8px; color:#000; }}
  .canvasBox {{ position:relative; height:380px; }}
  .small .canvasBox {{ height:315px; }}
  canvas {{ width:100%; height:100%; display:block; }}
  #tooltip {{ position:fixed; display:none; pointer-events:none; background:#fff; border:1px solid #000; border-radius:4px; padding:6px 8px; font-size:13px; box-shadow:0 2px 6px rgba(0,0,0,.25); z-index:10; white-space:nowrap; color:#000; }}
  .selectBox {{ position:absolute; display:none; border:1.5px dashed #000; background:rgba(0,0,0,.08); pointer-events:none; }}
  .controls {{ display:flex; gap:10px; align-items:center; margin-bottom:8px; flex-wrap:wrap; color:#000; font-size:14px; }}
  select {{ font-family:Arial, sans-serif; font-size:14px; padding:4px 6px; max-width:680px; border:1px solid #000; background:#fff; color:#000; }}
  #metrics {{ display:grid; grid-template-columns:repeat(5,minmax(88px,1fr)); gap:8px; margin:8px 0 6px; }}
  .metric {{ background:#fff; border:1px solid #000; border-radius:5px; padding:7px 8px; color:#000; }}
  .metric b {{ display:block; font-size:12px; color:#000; font-weight:500; }}
  .metric span {{ font-size:16px; color:#000; }}
  .legend {{ display:flex; gap:12px; flex-wrap:wrap; font-size:13px; margin:2px 0 8px; color:#000; }}
  .sw {{ display:inline-block; width:16px; height:4px; vertical-align:middle; margin-right:5px; }}
  .footer {{ margin-top:10px; color:#000; font-size:13px; }}
  @media(max-width:1050px) {{ #grid {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div id="wrap">
  <h1 id="title"></h1>
  <div id="sub"></div>
  <div class="legend" id="familyLegend"></div>
  <div id="grid">
    <div class="panel">
      <h2>Selected one-FSR raw trace with mode families</h2>
      <div class="canvasBox"><canvas id="onefsr"></canvas><div class="selectBox"></div></div>
    </div>
    <div class="panel">
      <h2>Local Q window</h2>
      <div class="controls"><label>family:</label><select id="localFamily"></select><label>resonance:</label><select id="localResonance"></select></div>
      <div id="metrics"></div>
      <div class="canvasBox"><canvas id="local"></canvas><div class="selectBox"></div></div>
    </div>
    <div class="panel small">
      <h2>D2 fit</h2>
      <div class="controls"><label>family:</label><select id="dispSelect"></select></div>
      <div class="canvasBox"><canvas id="dispersion"></canvas><div class="selectBox"></div></div>
    </div>
    <div class="panel small">
      <h2>Q trend vs wavelength</h2>
      <div class="controls"><label>metric:</label><select id="qMetric"><option value="Q0_M">Q0</option><option value="Q1_M">Q1</option><option value="QL_M">Q total / loaded</option><option value="transmission">Tmin / platform</option></select></div>
      <div class="canvasBox"><canvas id="qtrend"></canvas><div class="selectBox"></div></div>
    </div>
  </div>
  <div class="footer">Generated from standardized Q outputs. No re-analysis was run.</div>
</div>
<div id="tooltip"></div>
<script>
const payload={payload_js};
const tooltip=document.getElementById('tooltip');
document.getElementById('title').textContent=payload.title;
document.getElementById('sub').textContent=payload.subtitle;
function range(v,p=.05){{let mn=Math.min(...v),mx=Math.max(...v);if(mn===mx){{mn-=1;mx+=1}}let q=(mx-mn)*p;return[mn-q,mx+q]}}
function niceTicks(a,b,n=6){{const span=Math.abs(b-a)||1,raw=span/n,pow=Math.pow(10,Math.floor(Math.log10(raw))),mult=raw/pow,step=(mult<1.5?1:mult<3?2:mult<7?5:10)*pow,start=Math.ceil(a/step)*step,out=[];for(let v=start;v<=b+1e-9;v+=step)out.push(v);return out}}
function tickText(v,span){{const a=Math.abs(span);const d=a<0.01?5:a<0.1?4:a<1?3:a<10?2:1;return v.toFixed(d)}}
function metric(label,value){{return`<div class="metric"><b>${{label}}</b><span>${{value}}</span></div>`}}
class Chart{{
  constructor(canvas,opts){{this.canvas=canvas;this.box=canvas.parentElement;this.sel=this.box.querySelector('.selectBox');this.ctx=canvas.getContext('2d');this.opts=opts;this.pad={{l:82,r:24,t:18,b:60}};this.initView={{...opts.view}};this.view={{...opts.view}};this.drag=false;this.pan=false;this.start=null;this.last=null;this.bind();this.resize()}}
  W(){{return this.box.clientWidth}} H(){{return this.box.clientHeight}}
  sx(x){{return this.pad.l+(x-this.view.x0)/(this.view.x1-this.view.x0)*(this.W()-this.pad.l-this.pad.r)}}
  sy(y){{return this.H()-this.pad.b-(y-this.view.y0)/(this.view.y1-this.view.y0)*(this.H()-this.pad.t-this.pad.b)}}
  ix(px){{return this.view.x0+(px-this.pad.l)/(this.W()-this.pad.l-this.pad.r)*(this.view.x1-this.view.x0)}}
  iy(py){{return this.view.y0+(this.H()-this.pad.b-py)/(this.H()-this.pad.t-this.pad.b)*(this.view.y1-this.view.y0)}}
  inPlot(x,y){{return x>=this.pad.l&&x<=this.W()-this.pad.r&&y>=this.pad.t&&y<=this.H()-this.pad.b}}
  resize(){{const dpr=window.devicePixelRatio||1;this.canvas.width=Math.round(this.W()*dpr);this.canvas.height=Math.round(this.H()*dpr);this.ctx.setTransform(dpr,0,0,dpr,0,0);this.draw()}}
  setOpts(opts){{this.opts=opts;this.initView={{...opts.view}};this.view={{...opts.view}};this.draw()}}
  bind(){{window.addEventListener('resize',()=>this.resize());this.canvas.addEventListener('dblclick',()=>{{this.view={{...this.initView}};this.draw()}});this.canvas.addEventListener('wheel',e=>{{e.preventDefault();const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,cx=this.ix(mx),cy=this.iy(my),f=e.deltaY<0?.82:1.22;this.view.x0=cx-(cx-this.view.x0)*f;this.view.x1=cx+(this.view.x1-cx)*f;this.view.y0=cy-(cy-this.view.y0)*f;this.view.y1=cy+(this.view.y1-cy)*f;this.draw()}});this.canvas.addEventListener('mousedown',e=>{{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;if(!this.inPlot(mx,my))return;this.drag=true;this.pan=!!e.shiftKey;this.start={{x:mx,y:my}};this.last={{x:e.clientX,y:e.clientY}};if(!this.pan)this.updateSel(mx,my)}});window.addEventListener('mouseup',e=>{{if(!this.drag)return;if(!this.pan&&this.start){{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,xA=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,this.start.x)),xB=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,mx)),yA=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,this.start.y)),yB=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,my));if(Math.abs(xB-xA)>8&&Math.abs(yB-yA)>8){{this.view={{x0:Math.min(this.ix(xA),this.ix(xB)),x1:Math.max(this.ix(xA),this.ix(xB)),y0:Math.min(this.iy(yA),this.iy(yB)),y1:Math.max(this.iy(yA),this.iy(yB))}};this.draw()}}}}this.drag=false;this.pan=false;this.start=null;this.sel.style.display='none'}});window.addEventListener('mousemove',e=>{{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;if(this.drag&&this.pan){{const dx=e.clientX-this.last.x,dy=e.clientY-this.last.y;this.last={{x:e.clientX,y:e.clientY}};const xs=this.view.x1-this.view.x0,ys=this.view.y1-this.view.y0;this.view.x0-=dx/(this.W()-this.pad.l-this.pad.r)*xs;this.view.x1-=dx/(this.W()-this.pad.l-this.pad.r)*xs;this.view.y0+=dy/(this.H()-this.pad.t-this.pad.b)*ys;this.view.y1+=dy/(this.H()-this.pad.t-this.pad.b)*ys;this.draw();return}}if(this.drag&&!this.pan){{this.updateSel(mx,my);return}}this.hover(e,mx,my)}})}}
  updateSel(mx,my){{const x1=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,this.start.x)),y1=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,this.start.y)),x2=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,mx)),y2=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,my));this.sel.style.left=Math.min(x1,x2)+'px';this.sel.style.top=Math.min(y1,y2)+'px';this.sel.style.width=Math.abs(x2-x1)+'px';this.sel.style.height=Math.abs(y2-y1)+'px';this.sel.style.display='block'}}
  hover(e,mx,my){{let best=null;for(const s of this.opts.series){{if(s.visible===false)continue;for(const p of s.data){{const x=p[0],y=p[1];if(x<this.view.x0||x>this.view.x1)continue;const d=Math.abs(this.sx(x)-mx)+Math.abs(this.sy(y)-my)*.35;if(!best||d<best.d)best={{s,x,y,d,extra:p[2]}}}}}}if(best&&this.inPlot(mx,my)){{tooltip.style.display='block';tooltip.style.left=e.clientX+14+'px';tooltip.style.top=e.clientY+12+'px';tooltip.innerHTML=`<b style="color:#000">${{best.s.name}}</b><br>${{this.opts.xLabel}}: ${{best.x.toFixed(4)}}<br>${{this.opts.yLabel}}: ${{best.y.toFixed(4)}}${{best.extra?'<br>'+best.extra:''}}`}}else tooltip.style.display='none'}}
  draw(){{const c=this.ctx;c.clearRect(0,0,this.W(),this.H());c.fillStyle='#fff';c.fillRect(0,0,this.W(),this.H());c.strokeStyle='#000';c.lineWidth=1.2;c.strokeRect(this.pad.l,this.pad.t,this.W()-this.pad.l-this.pad.r,this.H()-this.pad.t-this.pad.b);const xt=niceTicks(this.view.x0,this.view.x1,5),yt=niceTicks(this.view.y0,this.view.y1,6);c.strokeStyle='#000';c.lineWidth=.45;c.textAlign='center';c.textBaseline='top';c.font='14px Arial';for(const v of xt){{const x=this.sx(v);c.beginPath();c.moveTo(x,this.pad.t);c.lineTo(x,this.H()-this.pad.b);c.stroke();c.fillStyle='#000';c.fillText(tickText(v,this.view.x1-this.view.x0),x,this.H()-this.pad.b+9)}}c.textAlign='right';c.textBaseline='middle';for(const v of yt){{const y=this.sy(v);c.beginPath();c.moveTo(this.pad.l,y);c.lineTo(this.W()-this.pad.r,y);c.stroke();c.fillStyle='#000';c.fillText(v.toFixed(this.opts.yDigits??2),this.pad.l-9,y)}}c.save();c.font='18px Arial';c.textAlign='center';c.textBaseline='bottom';c.fillStyle='#000';c.fillText(this.opts.xLabel,(this.pad.l+this.W()-this.pad.r)/2,this.H()-10);c.restore();c.save();c.translate(22,(this.pad.t+this.H()-this.pad.b)/2);c.rotate(-Math.PI/2);c.textAlign='center';c.textBaseline='middle';c.font='18px Arial';c.fillStyle='#000';c.fillText(this.opts.yLabel,0,0);c.restore();for(const s of this.opts.series){{if(s.visible===false)continue;c.strokeStyle=s.lineColor||s.color||'#000';c.fillStyle=s.color||'#000';c.lineWidth=s.width||2;if(s.type==='points'||s.type==='linepoints'){{if(s.type==='linepoints'){{c.beginPath();let st=false;for(const p of s.data){{const x=p[0],y=p[1];if(x<this.view.x0||x>this.view.x1||y<this.view.y0-5||y>this.view.y1+5)continue;if(!st){{c.moveTo(this.sx(x),this.sy(y));st=true}}else c.lineTo(this.sx(x),this.sy(y))}}c.stroke()}}for(const p of s.data){{const x=p[0],y=p[1];if(x<this.view.x0||x>this.view.x1||y<this.view.y0||y>this.view.y1)continue;c.beginPath();c.arc(this.sx(x),this.sy(y),s.r||4.5,0,Math.PI*2);c.fill();if(this.opts.labels){{c.font='12px Arial';c.fillStyle='#000';c.fillText(String(p[3]??''),this.sx(x)+7,this.sy(y)-7);c.fillStyle=s.color||'#000'}}}}}}else{{c.beginPath();let st=false;for(const p of s.data){{const x=p[0],y=p[1];if(x<this.view.x0||x>this.view.x1||y<this.view.y0-5||y>this.view.y1+5)continue;if(!st){{c.moveTo(this.sx(x),this.sy(y));st=true}}else c.lineTo(this.sx(x),this.sy(y))}}c.stroke()}}}}}}
}}
const familyLegend=document.getElementById('familyLegend');familyLegend.innerHTML=payload.familyOrder.map(lab=>`<span><span class="sw" style="background:${{payload.familyColors[lab]||'#000'}}"></span>${{lab}}</span>`).join('');
const oneY=range(payload.oneFSRTrace.map(p=>p[1]),.08);new Chart(document.getElementById('onefsr'),{{xLabel:'offset within one FSR (GHz)',yLabel:'normalized CH2',yDigits:2,view:{{x0:-payload.fsr_GHz/2,x1:payload.fsr_GHz/2,y0:Math.max(0,oneY[0]),y1:Math.min(1.15,oneY[1])}},series:[{{name:'selected one-FSR raw trace',color:'#000',lineColor:'#000',data:payload.oneFSRTrace,width:1.6}},...payload.familyOrder.map(lab=>{{const pts=payload.modeMarkers.filter(m=>m.family_label===lab);return{{name:lab,color:payload.familyColors[lab]||'#000',type:'points',r:5,data:pts.map(m=>[m.x_GHz,m.norm_T,`${{m.family_label}}<br>mu=${{m.mode_number}}<br>${{m.wavelength_nm.toFixed(3)}} nm<br>depth=${{m.depth.toFixed(3)}}`,m.mode_number])}}}})]}});
let dispChart=null;const dispSelect=document.getElementById('dispSelect');payload.dispersion.forEach((f,i)=>{{const o=document.createElement('option');o.value=i;o.textContent=`${{f.family_label}}: D1=${{f.D1_GHz.toFixed(3)}} GHz, D2=${{f.D2_MHz.toFixed(1)}} MHz, rms=${{f.rms_MHz.toFixed(1)}} MHz`;dispSelect.appendChild(o)}});function renderDisp(){{const f=payload.dispersion[+dispSelect.value];const ys=f.points.map(p=>p.dint_GHz).concat(f.fit_curve.map(p=>p[1])),xs=f.points.map(p=>p.mode_number);const r=range(ys,.08);const opts={{xLabel:'mode number',yLabel:'Dint after fitted D1 (GHz)',yDigits:2,labels:true,view:{{x0:Math.min(...xs)-1,x1:Math.max(...xs)+1,y0:r[0],y1:r[1]}},series:[{{name:f.family_label+' fit',color:'#000',lineColor:'#000',data:f.fit_curve,width:2}},{{name:f.family_label+' points',color:f.color,type:'points',r:4.8,data:f.points.map(p=>[p.mode_number,p.dint_GHz,`${{f.family_label}}<br>${{p.wavelength_nm.toFixed(3)}} nm<br>residual=${{p.residual_MHz.toFixed(1)}} MHz<br>D1=${{f.D1_GHz.toFixed(3)}} GHz<br>D2=${{f.D2_MHz.toFixed(1)}} MHz`,p.mode_number])}}]}}; if(dispChart) dispChart.setOpts(opts); else dispChart=new Chart(document.getElementById('dispersion'),opts)}}dispSelect.addEventListener('change',renderDisp);renderDisp();
let qChart=null;const qMetric=document.getElementById('qMetric');function renderQ(){{const key=qMetric.value;const label={{Q0_M:'Q0 (M)',Q1_M:'Q1 (M)',QL_M:'Q total / loaded (M)',transmission:'Tmin / platform'}}[key];const xs=payload.qTrend.map(p=>p.wavelength_nm),ys=payload.qTrend.map(p=>p[key]);const series=payload.familyOrder.map(lab=>{{const pts=payload.qTrend.filter(p=>p.family_label===lab).sort((a,b)=>a.wavelength_nm-b.wavelength_nm);return{{name:lab,color:payload.familyColors[lab]||'#000',type:'linepoints',r:5,width:1.5,data:pts.map(p=>[p.wavelength_nm,p[key],`${{p.family_label}}<br>mu=${{p.mode_number}}<br>${{label}}=${{Number(p[key]).toFixed(3)}}<br>Q0=${{p.Q0_M.toFixed(3)}} M<br>Q1=${{p.Q1_M.toFixed(3)}} M<br>QL=${{p.QL_M.toFixed(3)}} M<br>depth=${{p.depth.toFixed(3)}}`,p.mode_number])}}}});const opts={{xLabel:'wavelength (nm)',yLabel:label,yDigits:key==='transmission'?2:2,labels:false,view:{{x0:Math.min(...xs)-.4,x1:Math.max(...xs)+.4,y0:range(ys,.08)[0],y1:range(ys,.08)[1]}},series}}; if(qChart) qChart.setOpts(opts); else qChart=new Chart(document.getElementById('qtrend'),opts)}}qMetric.addEventListener('change',renderQ);renderQ();
let localChart=null;const localFamily=document.getElementById('localFamily');const localResonance=document.getElementById('localResonance');const localFamilies=payload.familyOrder.filter(lab=>payload.locals.some(x=>x.family_label===lab));localFamilies.forEach(lab=>{{const o=document.createElement('option');o.value=lab;o.textContent=lab;localFamily.appendChild(o)}});function populateLocalResonance(){{const lab=localFamily.value;localResonance.innerHTML='';payload.locals.forEach((l,i)=>{{if(l.family_label!==lab)return;const o=document.createElement('option');o.value=i;o.textContent=`mu=${{l.mode_number}}, ${{l.wavelength_nm.toFixed(3)}} nm, QL=${{l.QL_M.toFixed(3)}} M`;localResonance.appendChild(o)}})}}function renderLocal(){{const l=payload.locals[+localResonance.value];if(!l)return;document.getElementById('metrics').innerHTML=metric('family',l.family_label)+metric('QL',l.QL_M.toFixed(3)+' M')+metric('Q0',l.Q0_M.toFixed(3)+' M')+metric('Q1',l.Q1_M.toFixed(3)+' M')+metric('depth',l.depth.toFixed(3));const xs=l.local_data.map(p=>p[0]),ys=l.local_data.map(p=>p[1]);const opts={{xLabel:'wavelength (nm)',yLabel:'normalized T',yDigits:2,view:{{x0:Math.min(...xs),x1:Math.max(...xs),y0:range(ys,.08)[0],y1:range(ys,.08)[1]}},series:[{{name:l.label,color:'#000',lineColor:'#000',data:l.local_data,width:1.6}},{{name:'fit center',color:l.color,type:'points',r:5,data:[[l.wavelength_nm,1-l.depth,`${{l.fit_status}}<br>${{l.coupling_note}}`]]}}]}};if(localChart)localChart.setOpts(opts);else localChart=new Chart(document.getElementById('local'),opts)}}localFamily.addEventListener('change',()=>{{populateLocalResonance();renderLocal()}});localResonance.addEventListener('change',renderLocal);populateLocalResonance();renderLocal();
</script>
</body>
</html>
"""


def build_payload(cavity_dir: Path, *, trace_step: int, max_one_fsr_points: int, max_local_points: int) -> dict[str, object]:
    q_dir = cavity_dir / "Q"
    evidence_dir = latest_evidence_dir(q_dir)
    dispersion_summary = json.loads((evidence_dir / "dispersion_summary.json").read_text(encoding="utf-8"))
    families, labels = load_family_data(q_dir)
    ordered_labels = []
    for family, label in labels.items():
        if label not in ordered_labels:
            ordered_labels.append(label)
    colors = color_map(ordered_labels)
    fits = [fit for fit in dispersion_summary.get("auto_centered_fits", []) if "quadratic" in fit]
    one_trace, markers, fsr_ghz, selected_mode, target_labels, plotted_labels = one_fsr_payload(
        q_dir,
        families,
        fits,
        trace_step=trace_step,
        max_points=max_one_fsr_points,
    )
    locals_payload = local_windows(q_dir, families, labels, colors, trace_step=trace_step, max_points=max_local_points)
    parts = cavity_dir.parts[-3:]
    title = " / ".join(parts) + " Q interactive review"
    return {
        "title": title,
        "subtitle": (
            f"One-FSR panel uses common-coordinate representative mode m={selected_mode:+d}; "
            "left-drag box zooms, Shift+drag pans, wheel zooms, double click resets."
        ),
        "oneFSRTrace": one_trace,
        "modeMarkers": markers,
        "selectedMode": selected_mode,
        "targetLabels": target_labels,
        "plottedLabels": plotted_labels,
        "locals": locals_payload,
        "dispersion": dispersion_payload(fits, labels, colors),
        "qTrend": q_trend_payload(locals_payload),
        "fsr_GHz": fsr_ghz,
        "familyOrder": ordered_labels,
        "familyColors": colors,
    }


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    cavity_dir = args.cavity_dir.resolve()
    output = args.output or (cavity_dir / "Q" / "interactive_q.html")
    payload = build_payload(
        cavity_dir,
        trace_step=max(1, args.trace_step),
        max_one_fsr_points=args.max_one_fsr_points,
        max_local_points=args.max_local_points,
    )
    output.write_text(html_template(payload), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
