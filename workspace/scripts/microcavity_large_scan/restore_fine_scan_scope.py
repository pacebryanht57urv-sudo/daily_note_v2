#!/usr/bin/env python3
"""Restore the R&S RTE oscilloscope and TOPTICA DLC PRO to fine-scan idle."""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from acquire_large_scan import RohdeSchwarzRte, TopticaDlcPro


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-resource", default="TCPIP::192.168.1.8::INSTR")
    parser.add_argument("--visa-backend", default="@py")
    parser.add_argument("--laser-port", default="COM3")
    parser.add_argument("--trigger-channel", type=int, default=4)
    parser.add_argument("--trigger-level-v", type=float, default=2.5)
    parser.add_argument("--trigger-slope", choices=["positive", "negative"], default="negative")
    parser.add_argument("--trigger-mode", choices=["auto", "normal"], default="auto")
    parser.add_argument("--time-scale-s-per-div", type=float, default=1e-3)
    parser.add_argument("--arc-factor-v-per-v", type=float, default=25.0)
    parser.add_argument("--fine-center-nm", type=float, default=1550.0)
    parser.add_argument("--scope-only", action="store_true")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    laser = None
    if not args.scope_only:
        laser = TopticaDlcPro(args.laser_port)
        try:
            laser.move_to_wavelength(args.fine_center_nm, timeout_s=90.0)
            readback = laser.wavelength_nm()
            print(f"TOPTICA wavelength restored to {readback:.9f} nm for fine scan.")
            laser.configure_fine_scan_arc_factor(args.arc_factor_v_per_v)
            print(f"TOPTICA arc factor enabled at {args.arc_factor_v_per_v:g} V/V.")
        finally:
            laser.close()

    scope = RohdeSchwarzRte(args.scope_resource, visa_backend=args.visa_backend)
    try:
        print(f"Scope: {scope.idn()}")
        scope.configure_fine_scan_idle(
            trigger_channel=args.trigger_channel,
            trigger_level_v=args.trigger_level_v,
            trigger_slope=args.trigger_slope,
            trigger_mode=args.trigger_mode,
            time_scale_s_per_div=args.time_scale_s_per_div,
        )
        print(
            "Restored fine-scan scope state: "
            f"1 ms/div, CH1 off, CH{args.trigger_channel} on, "
            f"CH{args.trigger_channel} {args.trigger_slope} edge {args.trigger_mode} trigger "
            f"at {args.trigger_level_v:g} V."
        )
        return 0
    finally:
        scope.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
