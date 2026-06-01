"""Test several TOPTICA ARC factors and compare lock outcomes.

For each ARC factor:
1. write laser1.dl.pc.external_input.factor,
2. capture a downsweep pre-lock trace,
3. plot only the selected downsweep with width and lock-point markers,
4. run the existing second-scale PID lock sweep using the measured lock point.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from suggest_arc_factor import (
    RESULTS_DIR,
    analyze_apparent_width,
    bridge_get,
    configure_prelock_sweep,
    make_plot,
    read_arc_factor,
)
from tune_arc_fullwidth_center import read_pc, write_arc_factor


SCRIPT_DIR = Path(__file__).resolve().parent


def parse_float_list(text: str) -> list[float]:
    return [float(item) for item in text.split(",") if item.strip()]


def remove_file(path_text: str | None) -> bool:
    if not path_text:
        return False
    path = Path(path_text)
    if not path.exists():
        return False
    for _ in range(5):
        try:
            path.unlink()
            return True
        except PermissionError:
            time.sleep(0.2)
    return False


def compact_capture(capture: dict[str, object]) -> dict[str, object]:
    return {
        key: capture.get(key)
        for key in ("ok", "n", "input1", "input2", "duration", "trigger_source")
        if key in capture
    }


def best_i_summary(lock_summary: dict[str, object]) -> dict[str, object] | None:
    best: tuple[float, dict[str, object]] | None = None
    for item in lock_summary.get("i_summaries") or []:
        score = abs(float(item["target_error_mean"])) + float(item["ch1_std"])
        if best is None or score < best[0]:
            best = (score, item)
    return best[1] if best else None


def row_score(row: dict[str, object]) -> float | None:
    lock_summary = ((row.get("lock_result") or {}).get("summary") or {})
    best_i = best_i_summary(lock_summary)
    if best_i is None:
        return None
    return abs(float(best_i["target_error_mean"])) + float(best_i["ch1_std"])


def delete_lock_artifacts(lock_result: dict[str, object]) -> None:
    summary = lock_result.get("summary")
    if isinstance(summary, dict):
        remove_file(summary.get("csv_path"))
        remove_file(summary.get("png_path"))
    remove_file(lock_result.get("summary_path"))
    lock_result["artifacts_deleted"] = True


def run_lock_script(
    *,
    tag: str,
    target: float,
    probe_center: float,
    base: str,
    monitor_seconds: float,
) -> dict[str, object]:
    tag_path = RESULTS_DIR / tag
    tag_path.parent.mkdir(parents=True, exist_ok=True)
    before = set(tag_path.parent.glob(f"{tag_path.name}_*_summary.json"))
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "run_seconds_pid_lock_sweep.py"),
        "--base",
        base,
        "--tag",
        tag,
        "--target",
        repr(target),
        "--auto-probe",
        "--probe-center",
        repr(probe_center),
        "--probe-step",
        "0.02",
        "--probe-span",
        "0.15",
        "--probe-min-ch1-change",
        "0.006",
        "--probe-target-tolerance",
        "0.010",
        "--handoff-tolerance",
        "0.010",
        "--start",
        repr(probe_center),
        "--stop-limit",
        repr(probe_center - 0.15),
        "--step",
        "-0.003",
        "--approach-dwell",
        "0.25",
        "--approach-samples",
        "3",
        "--drop-threshold",
        "0.010",
        "--local-drop-threshold",
        "0.002",
        "--p-list=-0.000488,-0.000976,-0.001953,-0.003906,-0.007812",
        "--i-list=-1,-5,-10,-50,-100",
        "--monitor-seconds",
        repr(monitor_seconds),
        "--monitor-interval",
        "0.25",
    ]
    completed = subprocess.run(cmd, cwd=SCRIPT_DIR, text=True, capture_output=True)
    after = set(tag_path.parent.glob(f"{tag_path.name}_*_summary.json"))
    new_summaries = sorted(after - before, key=lambda p: p.stat().st_mtime)
    summary: dict[str, object] = {
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    if new_summaries:
        summary_path = new_summaries[-1]
        summary["summary_path"] = str(summary_path)
        summary["summary"] = json.loads(summary_path.read_text(encoding="utf-8"))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--host", default="192.168.1.104")
    parser.add_argument("--arc-list", default="3.2625,4.35,5.8")
    parser.add_argument("--tag", default="arc_factor_lock_series")
    parser.add_argument("--sweep-frequency", type=float, default=50.0)
    parser.add_argument("--sweep-amplitude", type=float, default=0.5)
    parser.add_argument("--duration", type=float, default=0.067108864)
    parser.add_argument("--settle-seconds", type=float, default=0.8)
    parser.add_argument("--monitor-seconds", type=float, default=3.0)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_tag = f"{args.tag}_{stamp}"
    run_dir = RESULTS_DIR / run_tag
    run_dir.mkdir(parents=True, exist_ok=True)
    arc_values = parse_float_list(args.arc_list)
    rows = []
    traces: dict[int, dict[str, object]] = {}

    initial_arc = read_arc_factor(args.host)
    initial_pc = read_pc(args.host)

    for index, arc in enumerate(arc_values, start=1):
        arc_label = f"arc{index:02d}_{arc:.4g}".replace(".", "p")
        arc_tag = f"{run_tag}_{arc_label}"
        print(f"\n=== ARC {index}/{len(arc_values)}: {arc:.6g} ===", flush=True)
        write_arc_factor(args.host, arc)
        time.sleep(args.settle_seconds)
        arc_info = read_arc_factor(args.host)
        pc_info = read_pc(args.host)

        configure_prelock_sweep(
            args.base, args.duration, args.sweep_frequency, args.sweep_amplitude
        )
        time.sleep(args.settle_seconds)
        capture = bridge_get(
            args.base, "/scope/single", {"tag": arc_tag, "timeout": 8, "plot": "false"}
        )
        if not capture.get("ok"):
            rows.append(
                {
                    "arc_factor": arc,
                    "arc_info": arc_info,
                    "pc_info": pc_info,
                    "capture": capture,
                    "ok": False,
                    "failure": "scope_capture_failed",
                }
            )
            continue

        with np.load(Path(capture["path"])) as data:
            t = np.asarray(data["t"], dtype=float)
            ch1 = np.asarray(data["ch1"], dtype=float)
            ch2 = np.asarray(data["ch2"], dtype=float)
        width_analysis = analyze_apparent_width(t, ch1, ch2, 0.75)
        lock_analysis = analyze_apparent_width(t, ch1, ch2, 0.25)
        traces[index] = {"t": t, "ch1": ch1, "ch2": ch2}
        removed_prelock_npz = remove_file(capture.get("path"))
        removed_legacy_plot = remove_file(capture.get("plot_path"))

        row: dict[str, object] = {
            "arc_index": index,
            "arc_label": arc_label,
            "arc_factor": arc,
            "arc_info": arc_info,
            "pc_info": pc_info,
            "capture": compact_capture(capture),
            "prelock_npz_deleted_after_analysis": removed_prelock_npz,
            "legacy_lockpoint_plot_deleted": removed_legacy_plot,
            "width_analysis": width_analysis,
            "lockpoint_analysis": lock_analysis,
        }
        if not width_analysis.get("ok") or not lock_analysis.get("ok"):
            row["ok"] = False
            row["failure"] = "prelock_analysis_failed"
            rows.append(row)
            print("prelock analysis failed; skip PID lock", flush=True)
            continue

        lock_chosen = lock_analysis["chosen"]
        target = float(lock_analysis["transmission_lock"])
        probe_center = float(lock_chosen["capture_side_sweep_voltage"])
        row["target_ch1"] = target
        row["probe_center_out2"] = probe_center
        row["width_capture_out2_v"] = float(
            width_analysis["chosen"]["capture_width_out2_v"]
        )
        row["width_full_out2_v"] = float(
            width_analysis["chosen"]["quarter_full_width_out2_v"]
        )
        row["lock_width_capture_out2_v"] = float(lock_chosen["capture_width_out2_v"])

        print(
            "prelock "
            f"target={target:.6f} probe_center={probe_center:+.6f} "
            f"width={row['width_capture_out2_v']:.6f}",
            flush=True,
        )
        lock_result = run_lock_script(
            tag=f"{run_tag}/{arc_label}_lock",
            target=target,
            probe_center=probe_center,
            base=args.base,
            monitor_seconds=args.monitor_seconds,
        )
        row["lock_result"] = lock_result
        row["ok"] = lock_result.get("returncode") == 0
        rows.append(row)

    best_index: int | None = None
    best_score: float | None = None
    for row in rows:
        if not row.get("ok"):
            continue
        score = row_score(row)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best_score = score
            best_index = int(row["arc_index"])

    final_prelock_plot_path: str | None = None
    if best_index is not None and best_index in traces:
        final_row = next(row for row in rows if row.get("arc_index") == best_index)
        trace = traces[best_index]
        final_prelock_plot = run_dir / "final_prelock_downsweep_width_lock.png"
        make_plot(
            final_prelock_plot,
            np.asarray(trace["t"], dtype=float),
            np.asarray(trace["ch1"], dtype=float),
            np.asarray(trace["ch2"], dtype=float),
            final_row["width_analysis"],
            final_row["arc_info"],
            None,
            final_row["lockpoint_analysis"],
        )
        final_prelock_plot_path = str(final_prelock_plot)
        final_row["final_prelock_plot_path"] = final_prelock_plot_path

    for row in rows:
        if row.get("arc_index") == best_index:
            continue
        lock_result = row.get("lock_result")
        if isinstance(lock_result, dict):
            delete_lock_artifacts(lock_result)

    summary = {
        "ok": True,
        "run_tag": run_tag,
        "run_dir": str(run_dir),
        "initial_arc": initial_arc,
        "initial_pc": initial_pc,
        "arc_values": arc_values,
        "selected_arc_index": best_index,
        "selected_score": best_score,
        "final_prelock_plot_path": final_prelock_plot_path,
        "rows": rows,
    }
    summary_path = run_dir / "series_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
