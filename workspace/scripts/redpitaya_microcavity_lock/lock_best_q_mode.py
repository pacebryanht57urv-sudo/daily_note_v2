"""Move to the highest-Q0 mode of a cavity and run the current-mode fast lock."""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lock_common import set_param
from toptica_laser_adapter import move_to_wavelength, write_pc_voltage


DEFAULT_LOCK_SCRIPT = SCRIPT_DIR / "current_mode_fast_lock.py"


def finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def candidate_from_row(row: dict[str, str], metric: str) -> dict[str, object]:
    return {
        "selection_metric": metric,
        "selection_metric_value": finite_float(row.get(metric)),
        "family": row.get("family", ""),
        "family_label": row.get("family_label", row.get("family", "")),
        "mode_number": int(float(row.get("mode_number", 0))),
        "wavelength_nm": finite_float(row.get("wavelength_nm")),
        "Q0": finite_float(row.get("Q0")),
        "Q1": finite_float(row.get("Q1")),
        "QL": finite_float(row.get("QL")),
        "depth": finite_float(row.get("depth")),
        "transmission": finite_float(row.get("transmission")),
        "linewidth_loaded_mhz": finite_float(row.get("linewidth_loaded_mhz")),
        "sample_index": int(float(row.get("sample_index", 0))),
        "time_s": finite_float(row.get("time_s")),
        "fit_status": row.get("fit_status", ""),
        "coupling_note": row.get("coupling_note", ""),
    }


def select_candidate_from_q_table(q_table: Path, metric: str) -> dict[str, object]:
    candidates: list[tuple[float, dict[str, object]]] = []
    with q_table.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("fit_status") != "ok":
                continue
            score = finite_float(row.get(metric))
            wavelength_nm = finite_float(row.get("wavelength_nm"))
            if score is None or wavelength_nm is None:
                continue
            candidates.append((score, candidate_from_row(row, metric)))
    if not candidates:
        raise RuntimeError(f"No valid lock candidate found in {q_table}")
    return max(candidates, key=lambda item: item[0])[1]


def resolve_q_dir(args: argparse.Namespace) -> Path:
    if args.q_dir:
        return Path(args.q_dir)
    if args.cavity_dir:
        cavity_dir = Path(args.cavity_dir)
        q_dir = cavity_dir / "Q"
        return q_dir if q_dir.exists() else cavity_dir
    raise ValueError("Pass --cavity-dir or --q-dir")


def manual_wavelength_candidate(args: argparse.Namespace) -> tuple[Path, dict[str, object], str] | None:
    if args.wavelength_nm is None:
        return None
    q_dir = resolve_q_dir(args)
    candidate = {
        "selection_metric": "manual_wavelength",
        "selection_metric_value": float(args.wavelength_nm),
        "family": "manual",
        "family_label": "manual",
        "mode_number": None,
        "wavelength_nm": float(args.wavelength_nm),
        "Q0": None,
        "Q1": None,
        "QL": None,
        "depth": None,
        "fit_status": "manual",
        "coupling_note": "manual wavelength target from dashboard",
    }
    return q_dir, candidate, "manual_wavelength"


def load_candidate(args: argparse.Namespace) -> tuple[Path, dict[str, object], str]:
    manual = manual_wavelength_candidate(args)
    if manual is not None:
        return manual

    q_dir = resolve_q_dir(args)
    if not q_dir.exists():
        raise FileNotFoundError(q_dir)

    candidate_json = Path(args.candidate_json) if args.candidate_json else q_dir / "best_lock_candidate.json"
    if candidate_json.exists():
        payload = json.loads(candidate_json.read_text(encoding="utf-8"))
        candidate_key = args.candidate_key
        candidate = payload.get(candidate_key)
        if not payload.get("ok"):
            reason = payload.get("reason", "candidate payload is marked ok=false")
            raise RuntimeError(f"No valid lock candidate in {candidate_json}: {reason}")
        if not isinstance(candidate, dict):
            raise RuntimeError(f"Invalid {candidate_key} payload in {candidate_json}")
        return q_dir, candidate, f"{candidate_json}#{candidate_key}"

    prefixed = sorted(q_dir.glob("*_best_lock_candidate.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in prefixed:
        payload = json.loads(path.read_text(encoding="utf-8"))
        candidate = payload.get(args.candidate_key)
        if payload.get("ok") and isinstance(candidate, dict):
            return q_dir, candidate, f"{path}#{args.candidate_key}"

    q_table = Path(args.q_table) if args.q_table else q_dir / "q_by_mode.csv"
    if not q_table.exists():
        raise FileNotFoundError(f"Missing {candidate_json} and {q_table}")
    return q_dir, select_candidate_from_q_table(q_table, args.metric), str(q_table)


def safe_off(base: str) -> None:
    for param, value in (
        ("pid0.p", 0),
        ("pid0.i", 0),
        ("pid0.output_direct", "off"),
        ("asg0.output_direct", "off"),
    ):
        set_param(base, param, value)


def move_laser_to_wavelength(
    host: str,
    port: str,
    connection: str,
    target_nm: float,
    *,
    timeout_s: float,
    tolerance_nm: float,
) -> dict[str, object]:
    result = move_to_wavelength(
        connection=connection,
        host=host,
        port=port,
        target_nm=target_nm,
        timeout_s=timeout_s,
        tolerance_nm=tolerance_nm,
    )
    if "after_readback_nm" not in result and "after_read_nm" in result:
        result["after_readback_nm"] = result["after_read_nm"]
    return result


def parse_lock_stdout(stdout: str) -> dict[str, object] | None:
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError:
        return None


def run_lock_script(args: argparse.Namespace, lock_extra_args: list[str]) -> tuple[int, dict[str, object] | None, str, str]:
    command = [
        sys.executable,
        str(args.lock_script),
        "--base",
        args.base,
        "--host",
        args.host,
        "--laser-connection",
        args.laser_connection,
        "--laser-port",
        args.laser_port,
        "--pc-start-v",
        f"{args.pc_start_v:g}",
        *lock_extra_args,
    ]
    proc = subprocess.run(
        command,
        cwd=SCRIPT_DIR.parents[2],
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode, parse_lock_stdout(proc.stdout), proc.stdout, proc.stderr


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cavity-dir", help="Cavity directory containing Q/, for example .../die1-1/c1")
    parser.add_argument("--q-dir", help="Q directory containing best_lock_candidate.json or q_by_mode.csv")
    parser.add_argument("--candidate-json")
    parser.add_argument("--candidate-key", default="candidate", choices=("candidate", "nearest_1550_best_q_candidate"))
    parser.add_argument("--q-table")
    parser.add_argument("--metric", default="Q0")
    parser.add_argument("--wavelength-nm", type=float, help="Manual target wavelength; bypasses candidate JSON/Q table selection")
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--host", default="192.168.1.104")
    parser.add_argument("--laser-connection", choices=["tcp", "serial"], default="tcp")
    parser.add_argument("--laser-port", default="COM3")
    parser.add_argument("--pc-start-v", type=float, default=75.0)
    parser.add_argument("--wavelength-timeout-s", type=float, default=90.0)
    parser.add_argument("--wavelength-tolerance-nm", type=float, default=0.01)
    parser.add_argument("--lock-script", type=Path, default=DEFAULT_LOCK_SCRIPT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--move-only", action="store_true", help="Safe-off, set PC, move wavelength, then stop before PID lock")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args, lock_extra_args = parser.parse_known_args(argv)
    try:
        q_dir, candidate, candidate_source = load_candidate(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "failure": {"stage": "load_candidate", "error": str(exc)}}, indent=2), flush=True)
        return 2
    wavelength_nm = finite_float(candidate.get("wavelength_nm"))
    if wavelength_nm is None:
        raise SystemExit(f"Candidate from {candidate_source} has no valid wavelength_nm")

    lock_command = [
        sys.executable,
        str(args.lock_script),
        "--base",
        args.base,
        "--host",
        args.host,
        "--laser-connection",
        args.laser_connection,
        "--laser-port",
        args.laser_port,
        "--pc-start-v",
        f"{args.pc_start_v:g}",
        *lock_extra_args,
    ]

    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "q_dir": str(q_dir),
                    "candidate_source": candidate_source,
                    "candidate": candidate,
                    "planned": {
                        "safe_off": True,
                        "pc_start_v_before_wavelength_move": args.pc_start_v,
                        "wavelength_target_nm": wavelength_nm,
                        "lock_command": lock_command,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 0

    summary: dict[str, Any] = {
        "ok": False,
        "q_dir": str(q_dir),
        "candidate_source": candidate_source,
        "candidate": candidate,
    }
    try:
        safe_off(args.base)
        pc = write_pc_voltage(
            connection=args.laser_connection,
            host=args.host,
            port=args.laser_port,
            value=args.pc_start_v,
        )
        move = move_laser_to_wavelength(
            args.host,
            args.laser_port,
            args.laser_connection,
            wavelength_nm,
            timeout_s=args.wavelength_timeout_s,
            tolerance_nm=args.wavelength_tolerance_nm,
        )
        summary["prelock"] = {"pc_start": pc, "wavelength_move": move}
        if not move["ok"]:
            summary["failure"] = {"stage": "move_wavelength", "move": move}
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
            return 2

        if args.move_only:
            summary["ok"] = True
            summary["move_only"] = True
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
            return 0

        return_code, lock_summary, stdout, stderr = run_lock_script(args, lock_extra_args)
        summary["lock_returncode"] = return_code
        summary["lock"] = lock_summary
        if stderr.strip():
            summary["lock_stderr"] = stderr.strip()
        summary["ok"] = return_code == 0 and bool(lock_summary and lock_summary.get("ok"))
        if not summary["ok"]:
            summary["failure"] = {"stage": "lock", "stdout": stdout[-2000:]}
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 0 if summary["ok"] else 3
    except Exception as exc:
        try:
            safe_off(args.base)
        except Exception:
            pass
        summary["failure"] = {"stage": "exception", "error": repr(exc)}
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
