"""Chip7 design helpers for large-scan analysis defaults."""

from __future__ import annotations

from dataclasses import dataclass

C_M_PER_S = 299_792_458.0
DEFAULT_NG_ESTIMATE = 2.0


@dataclass(frozen=True)
class DieDesign:
    radius_um: float
    gap_rows_um: tuple[float, float, float]
    edge_pitch_um: float = 30.0
    topology: str = "dual_side"

    def expected_fsr_mhz(self, ng: float = DEFAULT_NG_ESTIMATE) -> float:
        radius_m = self.radius_um * 1e-6
        return C_M_PER_S / (ng * 2.0 * 3.141592653589793 * radius_m) / 1e6

    def gap_for_cavity(self, cavity: str) -> float:
        index = int(cavity.lower().lstrip("c"))
        if index < 1 or index > 9:
            raise ValueError(f"Unsupported chip7 cavity {cavity!r}; expected c1-c9")
        return self.gap_rows_um[(index - 1) // 3]


CHIP7_DIE_DESIGNS: dict[str, DieDesign] = {
    "die1-1": DieDesign(125.0, (0.75, 0.80, 0.85)),
    "die1-2": DieDesign(125.0, (0.90, 0.95, 1.00)),
    "die1-3": DieDesign(105.0, (0.75, 0.80, 0.85)),
    "die1-4": DieDesign(105.0, (0.90, 0.95, 1.00)),
    "die2-1": DieDesign(85.0, (0.75, 0.80, 0.85)),
    "die2-2": DieDesign(85.0, (0.90, 0.95, 1.00)),
    "die2-3": DieDesign(65.0, (0.75, 0.80, 0.85)),
    "die2-4": DieDesign(65.0, (0.90, 0.95, 1.00)),
    "die3-1": DieDesign(125.0, (0.75, 0.80, 0.85), edge_pitch_um=127.0),
    "die3-2": DieDesign(125.0, (0.90, 0.95, 1.00), edge_pitch_um=127.0),
    "die3-3": DieDesign(105.0, (0.75, 0.80, 0.85), edge_pitch_um=127.0),
    "die3-4": DieDesign(105.0, (0.90, 0.95, 1.00), edge_pitch_um=127.0),
    "die4-1": DieDesign(125.0, (0.75, 0.80, 0.85), edge_pitch_um=127.0, topology="same_side"),
    "die4-2": DieDesign(125.0, (0.90, 0.95, 1.00), edge_pitch_um=127.0, topology="same_side"),
    "die4-3": DieDesign(105.0, (0.75, 0.80, 0.85), edge_pitch_um=127.0, topology="same_side"),
    "die4-4": DieDesign(105.0, (0.90, 0.95, 1.00), edge_pitch_um=127.0, topology="same_side"),
}


def chip7_design_for_die(die: str) -> DieDesign:
    try:
        return CHIP7_DIE_DESIGNS[die.lower()]
    except KeyError as exc:
        known = ", ".join(sorted(CHIP7_DIE_DESIGNS))
        raise ValueError(f"Unknown chip7 die {die!r}; known dies: {known}") from exc


def expected_chip7_fsr_mhz(die: str, ng: float = DEFAULT_NG_ESTIMATE) -> float:
    return chip7_design_for_die(die).expected_fsr_mhz(ng=ng)
