"""Read-only discovery for two Thorlabs MDT693B controllers."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

from mdt693b import discover_ports


DEFAULT_EXPERIMENT_DIR = (
    Path(__file__).resolve().parents[2] / "experiments" / "auto_coupling_mdt693b"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ports",
        nargs="+",
        default=["COM6", "COM7"],
        help="Serial ports to query. Default: COM6 COM7.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_EXPERIMENT_DIR / "raw",
        help="Output directory for JSON discovery record.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows = discover_ports(args.ports)
    payload = {
        "timestamp": timestamp,
        "purpose": "read-only MDT693B discovery for computer-assisted coupling",
        "port_mapping_expected": {
            "COM6": "left translation stage",
            "COM7": "right translation stage",
        },
        "serial_settings": {
            "baudrate": 115200,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
            "flow_control": "none",
            "terminator": "CRLF",
        },
        "rows": rows,
    }
    port_tag = "_".join(port.upper().replace(":", "") for port in args.ports)
    out_path = args.out_dir / f"mdt693b_discovery_{port_tag}_{timestamp}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nSaved: {out_path}")
    return 0 if all(row.get("ok") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
