"""Second-scale PID lock attempt with early drop capture and large I ramp."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import matplotlib.pyplot as plt

from data_paths import RESULTS_DIR


def bridge_get(base: str, path: str, params: dict[str, object] | None = None) -> dict:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def set_param(base: str, param: str, value: object) -> object:
    result = bridge_get(base, "/set", {"param": param, "value": str(value)})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result.get("after")


def get_value(base: str, param: str) -> object:
    result = bridge_get(base, "/get", {"param": param})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result["value"]


def sample(base: str) -> dict[str, float]:
    return {
        "ch1": float(get_value(base, "scope.voltage_in1")),
        "ch2": float(get_value(base, "scope.voltage_in2")),
        "p": float(get_value(base, "pid0.p")),
        "i_gain": float(get_value(base, "pid0.i")),
        "ival": float(get_value(base, "pid0.ival")),
    }


def configure_scope(base: str, duration: float) -> None:
    set_param(base, "scope.input1", "in1")
    set_param(base, "scope.input2", "out2")
    set_param(base, "scope.duration", duration)
    set_param(base, "scope.rolling_mode", "true")


def monitor(
    base: str, seconds: float, interval: float, run_start: float | None = None
) -> list[dict[str, float]]:
    rows = []
    start = time.time()
    index = 0
    while time.time() - start < seconds:
        row = {"t_s": time.time() - start, "index": index}
        if run_start is not None:
            row["elapsed_s"] = time.time() - run_start
        row.update(sample(base))
        rows.append(row)
        index += 1
        time.sleep(interval)
    return rows


def summarize(rows: list[dict[str, float]], target: float) -> dict[str, object]:
    if not rows:
        return {"ok": False}
    ch1 = [row["ch1"] for row in rows]
    ch2 = [row["ch2"] for row in rows]
    ival = [row["ival"] for row in rows]
    centered = [value - target for value in ch1]
    crossings = 0
    for left, right in zip(centered, centered[1:]):
        if left == 0 or right == 0 or left * right < 0:
            crossings += 1
    ptp = max(ch1) - min(ch1)
    std = statistics.pstdev(ch1) if len(ch1) > 1 else 0.0
    mean = statistics.fmean(ch1)
    ch2_mean = statistics.fmean(ch2)
    ival_end = ival[-1]
    return {
        "ok": True,
        "n": len(rows),
        "ch1_mean": mean,
        "ch1_min": min(ch1),
        "ch1_max": max(ch1),
        "ch1_ptp": ptp,
        "ch1_std": std,
        "target_error_mean": mean - target,
        "target_crossings": crossings,
        "ch2_mean": ch2_mean,
        "ch2_min": min(ch2),
        "ch2_max": max(ch2),
        "ch2_ptp": max(ch2) - min(ch2),
        "ival_start": ival[0],
        "ival_end": ival_end,
        "ival_drift": ival_end - ival[0],
        "periodic_or_large": ptp > 0.20 or std > 0.07 or (crossings >= 6 and ptp > 0.04),
        "platform_like": mean > target + 0.15,
        "too_deep": mean < target - 0.05,
        "out2_saturated": abs(ch2_mean) > 0.95 or max(abs(v) for v in ch2) > 0.98,
        "ival_saturated": abs(ival_end) > 3.8,
    }


def is_problem(summary: dict[str, object]) -> bool:
    return bool(
        summary.get("periodic_or_large")
        or summary.get("platform_like")
        or summary.get("too_deep")
        or summary.get("out2_saturated")
        or summary.get("ival_saturated")
    )


def mean_at_ival(args: argparse.Namespace, requested_ival: float) -> dict[str, object]:
    set_param(args.base, "pid0.ival", requested_ival)
    time.sleep(args.approach_dwell)
    readings = [sample(args.base) for _ in range(args.approach_samples)]
    ch1 = statistics.fmean(row["ch1"] for row in readings)
    ch2 = statistics.fmean(row["ch2"] for row in readings)
    readback_ival = float(get_value(args.base, "pid0.ival"))
    return {
        "requested_ival": requested_ival,
        "readback_ival": readback_ival,
        "ch1_mean": ch1,
        "ch2_mean": ch2,
        "error_to_target": ch1 - args.target,
    }


def bidirectional_probe(
    args: argparse.Namespace, run_start: float
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows: list[dict[str, object]] = []
    center = args.probe_center if args.probe_center is not None else args.start
    probe_step = abs(args.probe_step)

    set_param(args.base, "asg0.output_direct", "off")
    set_param(args.base, "pid0.p", 0)
    set_param(args.base, "pid0.i", 0)
    set_param(args.base, "pid0.output_direct", "out2")

    probes = [
        ("center", center),
        ("plus", center + probe_step),
        ("minus", center - probe_step),
    ]
    measured: dict[str, dict[str, object]] = {}
    for label, requested in probes:
        row = mean_at_ival(args, requested)
        row.update(
            {
                "phase": "probe",
                "probe_point": label,
                "elapsed_s": time.time() - run_start,
                "action": "measure",
            }
        )
        rows.append(row)
        measured[label] = row
        print(
            f"probe {label:>6s} ival={row['readback_ival']:+.5f} "
            f"ch1={row['ch1_mean']:.6f} err={row['error_to_target']:+.6f}",
            flush=True,
        )

    center_row = measured["center"]
    center_error = float(center_row["error_to_target"])
    plus_delta = float(measured["plus"]["ch1_mean"]) - float(center_row["ch1_mean"])
    minus_delta = float(measured["minus"]["ch1_mean"]) - float(center_row["ch1_mean"])
    max_abs_delta = max(abs(plus_delta), abs(minus_delta))
    probe_inconclusive = False
    if abs(center_error) <= args.probe_target_tolerance:
        selected_direction = 0.0
        reason = "center already near target"
    elif max_abs_delta < args.probe_min_ch1_change:
        selected_direction = 0.0
        probe_inconclusive = True
        reason = "bidirectional probe did not produce a significant CH1 change"
    else:
        if center_error > 0:
            # We are above the lock transmission; move toward the side where CH1 falls.
            selected_direction = 1.0 if plus_delta < minus_delta else -1.0
            reason = "center is above target; choose the side with stronger CH1 decrease"
        else:
            # We are deeper than target; move toward the side where CH1 rises.
            selected_direction = 1.0 if plus_delta > minus_delta else -1.0
            reason = "center is below target; choose the side with stronger CH1 increase"

    selected_start = float(center_row["readback_ival"])
    if selected_direction == 0.0:
        selected_step = 0.0
        selected_stop = selected_start
    else:
        selected_step = selected_direction * abs(args.step)
        selected_stop = selected_start + selected_direction * abs(args.probe_span)

    summary = {
        "center_ival": selected_start,
        "center_ch1": float(center_row["ch1_mean"]),
        "center_error_to_target": center_error,
        "plus_ch1": float(measured["plus"]["ch1_mean"]),
        "minus_ch1": float(measured["minus"]["ch1_mean"]),
        "plus_delta_ch1": plus_delta,
        "minus_delta_ch1": minus_delta,
        "max_abs_delta_ch1": max_abs_delta,
        "probe_min_ch1_change": args.probe_min_ch1_change,
        "probe_inconclusive": probe_inconclusive,
        "selected_direction": selected_direction,
        "selected_start": selected_start,
        "selected_step": selected_step,
        "selected_stop_limit": selected_stop,
        "reason": reason,
    }
    rows[-1]["action"] = "select_direction"
    rows[-1].update(summary)
    print(
        f"probe decision start={selected_start:+.5f} step={selected_step:+.5f} "
        f"stop={selected_stop:+.5f} reason={reason}",
        flush=True,
    )
    return rows, summary


def approach_until_drop(
    args: argparse.Namespace, run_start: float
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows: list[dict[str, object]] = []
    set_param(args.base, "asg0.output_direct", "off")
    set_param(args.base, "pid0.p", 0)
    set_param(args.base, "pid0.i", 0)
    set_param(args.base, "pid0.output_direct", "out2")
    set_param(args.base, "pid0.ival", args.start)

    current = args.start
    baseline: float | None = None
    last_ch1: float | None = None
    direction = 1.0 if args.step > 0 else -1.0
    while (args.stop_limit - current) * direction >= 0:
        time.sleep(args.approach_dwell)
        readings = [sample(args.base) for _ in range(args.approach_samples)]
        ch1 = statistics.fmean(row["ch1"] for row in readings)
        ch2 = statistics.fmean(row["ch2"] for row in readings)
        readback_ival = float(get_value(args.base, "pid0.ival"))
        if baseline is None:
            baseline = ch1
        local_drop = 0.0 if last_ch1 is None else last_ch1 - ch1
        total_drop = baseline - ch1
        row: dict[str, object] = {
            "phase": "approach",
            "elapsed_s": time.time() - run_start,
            "requested_ival": current,
            "readback_ival": readback_ival,
            "ch1_mean": ch1,
            "ch2_mean": ch2,
            "drop_from_baseline": total_drop,
            "local_drop": local_drop,
            "error_to_target": ch1 - args.target,
            "action": "measure",
        }
        rows.append(row)
        print(
            f"approach ival={readback_ival:+.5f} ch1={ch1:.6f} "
            f"drop={total_drop:.6f} local={local_drop:.6f}",
            flush=True,
        )
        if total_drop >= args.drop_threshold and local_drop >= args.local_drop_threshold:
            row["action"] = "stop_on_clear_drop"
            return rows, {
                "stop_reason": "clear transmission drop",
                "final_ival": readback_ival,
                "final_ch1": ch1,
                "final_ch2": ch2,
                "drop_from_baseline": total_drop,
            }
        if abs(ch1 - args.target) <= args.handoff_tolerance:
            row["action"] = "stop_near_target"
            return rows, {
                "stop_reason": "near target",
                "final_ival": readback_ival,
                "final_ch1": ch1,
                "final_ch2": ch2,
                "drop_from_baseline": total_drop,
            }
        last_ch1 = ch1
        current = readback_ival + args.step
        set_param(args.base, "pid0.ival", current)

    return rows, {
        "stop_reason": "stop limit reached",
        "final_ival": float(get_value(args.base, "pid0.ival")),
        "final_ch1": float(get_value(args.base, "scope.voltage_in1")),
        "final_ch2": float(get_value(args.base, "scope.voltage_in2")),
    }


def add_rows(
    all_rows: list[dict[str, object]],
    phase: str,
    setting: float | str,
    rows: list[dict[str, float]],
    summary: dict[str, object],
    elapsed_offset: float,
) -> float:
    if len(rows) >= 2:
        dt = rows[1]["t_s"] - rows[0]["t_s"]
    else:
        dt = 0.0
    for row in rows:
        merged: dict[str, object] = {"phase": phase, "setting": setting}
        merged.update(row)
        if "elapsed_s" not in merged:
            merged["elapsed_s"] = elapsed_offset + float(row["t_s"])
        all_rows.append(merged)
    summary_row: dict[str, object] = {"phase": f"{phase}_summary", "setting": setting}
    summary_row.update(summary)
    if rows:
        summary_row["elapsed_s"] = rows[-1].get(
            "elapsed_s", elapsed_offset + float(rows[-1]["t_s"])
        )
    all_rows.append(summary_row)
    if not rows:
        return elapsed_offset
    return float(rows[-1].get("elapsed_s", elapsed_offset + float(rows[-1]["t_s"]))) + max(
        float(dt), 0.0
    )


def parse_float_list(text: str) -> list[float]:
    return [float(item) for item in text.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--tag", default="seconds_pid_lock_sweep")
    parser.add_argument("--target", type=float, required=True)
    parser.add_argument("--scope-duration", type=float, default=2.147483648)
    parser.add_argument("--start", type=float, default=-0.03)
    parser.add_argument("--stop-limit", type=float, default=-0.30)
    parser.add_argument("--step", type=float, default=-0.005)
    parser.add_argument("--approach-dwell", type=float, default=0.45)
    parser.add_argument("--approach-samples", type=int, default=3)
    parser.add_argument("--drop-threshold", type=float, default=0.015)
    parser.add_argument("--local-drop-threshold", type=float, default=0.004)
    parser.add_argument("--handoff-tolerance", type=float, default=0.008)
    parser.add_argument("--auto-probe", action="store_true")
    parser.add_argument("--probe-center", type=float)
    parser.add_argument("--probe-step", type=float, default=0.006)
    parser.add_argument("--probe-span", type=float, default=0.08)
    parser.add_argument("--probe-target-tolerance", type=float, default=0.008)
    parser.add_argument("--probe-min-ch1-change", type=float, default=0.005)
    parser.add_argument("--p-list", default="-0.000488,-0.000976,-0.001953,-0.003906,-0.007812")
    parser.add_argument("--i-list", default="-1,-5,-10,-50,-100,-500,-1000")
    parser.add_argument("--monitor-seconds", type=float, default=4.0)
    parser.add_argument("--monitor-interval", type=float, default=0.25)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{args.tag}_{stamp}"
    csv_path = RESULTS_DIR / f"{tag}.csv"
    png_path = RESULTS_DIR / f"{tag}.png"
    summary_path = RESULTS_DIR / f"{tag}_summary.json"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, object]] = []
    p_summaries: list[dict[str, object]] = []
    i_summaries: list[dict[str, object]] = []
    selected_p: float | None = None
    selected_i: float | None = None
    best_i_score: float | None = None
    failure: dict[str, object] | None = None
    elapsed_offset = 0.0
    probe_summary: dict[str, object] | None = None
    approach_summary: dict[str, object] = {"stop_reason": "not_started"}

    configure_scope(args.base, args.scope_duration)
    run_start = time.time()

    try:
        if args.auto_probe:
            probe_rows, probe_summary = bidirectional_probe(args, run_start)
            all_rows.extend(probe_rows)
            if probe_rows:
                elapsed_offset = float(probe_rows[-1]["elapsed_s"])
            if probe_summary.get("probe_inconclusive"):
                failure = {
                    "stage": "probe",
                    "reason": probe_summary["reason"],
                    "center_ival": probe_summary["center_ival"],
                    "center_ch1": probe_summary["center_ch1"],
                    "target": args.target,
                }
                approach_summary = {
                    "stop_reason": "probe inconclusive",
                    "final_ival": probe_summary["center_ival"],
                    "final_ch1": probe_summary["center_ch1"],
                }
                set_param(args.base, "pid0.i", 0)
                set_param(args.base, "pid0.p", 0)
                set_param(args.base, "pid0.output_direct", "off")
                raise StopIteration
            args.start = float(probe_summary["selected_start"])
            selected_step = float(probe_summary["selected_step"])
            if abs(selected_step) > 0:
                args.step = selected_step
                args.stop_limit = float(probe_summary["selected_stop_limit"])

        approach_rows, approach_summary = approach_until_drop(args, run_start)
        all_rows.extend(approach_rows)
        if approach_rows:
            elapsed_offset = float(approach_rows[-1]["elapsed_s"])

        set_param(args.base, "pid0.setpoint", args.target)
        set_param(args.base, "pid0.i", 0)
        set_param(args.base, "pid0.output_direct", "out2")

        best_p_score: float | None = None
        for requested_p in parse_float_list(args.p_list):
            readback_p = float(set_param(args.base, "pid0.p", requested_p))
            set_param(args.base, "pid0.i", 0)
            rows = monitor(args.base, args.monitor_seconds, args.monitor_interval, run_start)
            summary = summarize(rows, args.target)
            summary["requested_p"] = requested_p
            summary["readback_p"] = readback_p
            p_summaries.append(summary)
            elapsed_offset = add_rows(all_rows, "p_test", requested_p, rows, summary, elapsed_offset)
            print(
                f"P req={requested_p:+.6g} read={readback_p:+.6g} "
                f"mean={summary['ch1_mean']:.6f} ptp={summary['ch1_ptp']:.6f} "
                f"cross={summary['target_crossings']} problem={is_problem(summary)}",
                flush=True,
            )
            if is_problem(summary):
                if selected_p is not None:
                    set_param(args.base, "pid0.p", selected_p)
                break
            score = abs(float(summary["target_error_mean"])) + float(summary["ch1_std"])
            if best_p_score is None or score < best_p_score:
                best_p_score = score
                selected_p = readback_p

        if selected_p is None:
            failure = {"stage": "p_test", "reason": "no non-problematic P found"}
            set_param(args.base, "pid0.p", 0)
            set_param(args.base, "pid0.i", 0)
            set_param(args.base, "pid0.output_direct", "off")
        else:
            set_param(args.base, "pid0.p", selected_p)
            for requested_i in parse_float_list(args.i_list):
                readback_i = float(set_param(args.base, "pid0.i", requested_i))
                rows = monitor(args.base, args.monitor_seconds, args.monitor_interval, run_start)
                summary = summarize(rows, args.target)
                summary["requested_i"] = requested_i
                summary["readback_i"] = readback_i
                i_summaries.append(summary)
                elapsed_offset = add_rows(
                    all_rows, "i_test", requested_i, rows, summary, elapsed_offset
                )
                print(
                    f"I req={requested_i:+.6g} read={readback_i:+.6g} "
                    f"mean={summary['ch1_mean']:.6f} ptp={summary['ch1_ptp']:.6f} "
                    f"err={summary['target_error_mean']:+.6f} "
                    f"sat_out={summary['out2_saturated']} sat_ival={summary['ival_saturated']} "
                    f"problem={is_problem(summary)}",
                    flush=True,
                )
                if is_problem(summary):
                    failure = {
                        "stage": "i_test",
                        "requested_i": requested_i,
                        "readback_i": readback_i,
                        "reason": "problem or saturation guard triggered",
                    }
                    set_param(args.base, "pid0.i", 0)
                    set_param(args.base, "pid0.p", 0)
                    set_param(args.base, "pid0.output_direct", "off")
                    break
                score = abs(float(summary["target_error_mean"])) + float(summary["ch1_std"])
                if best_i_score is None or score < best_i_score:
                    best_i_score = score
                    selected_i = readback_i
            if failure is None and selected_i is not None:
                set_param(args.base, "pid0.i", selected_i)
    except StopIteration:
        pass
    finally:
        configure_scope(args.base, args.scope_duration)

    fieldnames = sorted({key for row in all_rows for key in row.keys()})
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    plt.rcParams.update(
        {
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
        }
    )
    fig, axes = plt.subplots(2, 1, figsize=(15, 8), sharex=True)
    probe_rows = [row for row in all_rows if row.get("phase") == "probe"]
    if probe_rows:
        for axis, y_key in ((axes[0], "ch1_mean"), (axes[1], "ch2_mean")):
            axis.plot(
                [float(row["elapsed_s"]) for row in probe_rows],
                [float(row[y_key]) for row in probe_rows],
                marker="D",
                linestyle="",
                markersize=5,
                color="tab:red",
                label="probe: bidirectional ival check",
            )

    approach_rows = [row for row in all_rows if row.get("phase") == "approach"]
    if approach_rows:
        for axis, y_key in ((axes[0], "ch1_mean"), (axes[1], "ch2_mean")):
            axis.plot(
                [float(row["elapsed_s"]) for row in approach_rows],
                [float(row[y_key]) for row in approach_rows],
                marker="^",
                linestyle="-",
                markersize=3,
                color="tab:purple",
                label="approach: ival stepping",
            )

    for phase, marker, cmap_name, prefix in (
        ("p_test", "o", "Blues", "P"),
        ("i_test", "s", "Greens", "I"),
    ):
        phase_rows = [row for row in all_rows if row.get("phase") == phase]
        settings = []
        for row in phase_rows:
            setting = row.get("setting")
            if setting not in settings:
                settings.append(setting)
        cmap = plt.get_cmap(cmap_name)
        for index, setting in enumerate(settings):
            rows = [row for row in phase_rows if row.get("setting") == setting]
            color_scale = 0.35 + 0.55 * index / max(len(settings) - 1, 1)
            color = cmap(color_scale)
            label = f"{prefix}={float(setting):+.4g}" if setting is not None else phase
            axes[0].plot(
                [float(row["elapsed_s"]) for row in rows],
                [float(row.get("ch1", row.get("ch1_mean"))) for row in rows],
                marker=marker,
                linestyle="-",
                markersize=3,
                color=color,
                label=label,
            )
            axes[1].plot(
                [float(row["elapsed_s"]) for row in rows],
                [float(row.get("ch2", row.get("ch2_mean"))) for row in rows],
                marker=marker,
                linestyle="-",
                markersize=3,
                color=color,
                label=label,
            )
    axes[0].axhline(args.target, color="tab:orange", linestyle="--", label="target")
    axes[0].set_ylabel("CH1 / transmission (V)")
    axes[1].set_ylabel("CH2 / Out2 (V)")
    axes[1].set_xlabel("Elapsed time from first ival change (s)")
    for ax in axes:
        ax.grid(True, alpha=0.3)
    fig.suptitle("Second-scale PID lock sweep")
    handles = []
    labels = []
    seen = set()
    for ax in axes:
        ax_handles, ax_labels = ax.get_legend_handles_labels()
        for handle, label in zip(ax_handles, ax_labels):
            if label in seen:
                continue
            seen.add(label)
            handles.append(handle)
            labels.append(label)
    if handles:
        fig.legend(
            handles,
            labels,
            loc="center left",
            bbox_to_anchor=(1.01, 0.5),
            borderaxespad=0,
            frameon=True,
        )
    fig.subplots_adjust(right=0.78, top=0.92, hspace=0.18)
    fig.tight_layout()
    fig.savefig(png_path, dpi=300, bbox_inches="tight")
    plt.close(fig)

    summary = {
        "target": args.target,
        "probe": probe_summary,
        "approach": approach_summary,
        "p_summaries": p_summaries,
        "i_summaries": i_summaries,
        "selected_p": selected_p,
        "selected_i": selected_i,
        "failure": failure,
        "csv_path": str(csv_path),
        "png_path": str(png_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
