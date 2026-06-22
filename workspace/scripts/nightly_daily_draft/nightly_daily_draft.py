#!/usr/bin/env python
"""Create conservative campaign-local daily drafts from file timestamps.

This script writes only Markdown daily index notes under:

    <DATA_ROOT>/experiments/<campaign>/daily/YYYY-MM-DD.md

It does not inspect binary data content, and it does not infer experimental
conclusions from timestamps.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path


AUTO_START = "<!-- auto-daily-start -->"
AUTO_END = "<!-- auto-daily-end -->"
SESSION_RE = re.compile(r"20\d{6}_\d{6}_[^\\/]+")
IGNORED_SUFFIXES = {".tmp", ".bak", ".log", ".err"}


@dataclass
class FileEvent:
    path: Path
    relative_to_campaign: Path
    mtime: datetime
    size: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate conservative daily drafts from experiment file timestamps."
    )
    parser.add_argument(
        "--data-root",
        default=os.environ.get("DAILY_NOTE_DATA_ROOT"),
        help="External data root. Defaults to DAILY_NOTE_DATA_ROOT.",
    )
    parser.add_argument(
        "--experiments-root",
        help="Override experiments root. Defaults to <data-root>/experiments.",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Date to scan, in YYYY-MM-DD. Defaults to today.",
    )
    parser.add_argument(
        "--min-files",
        type=int,
        default=1,
        help="Minimum updated files in one campaign before writing a draft.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned output paths and summaries without writing files.",
    )
    return parser.parse_args()


def day_bounds(day_text: str) -> tuple[datetime, datetime]:
    try:
        day = date.fromisoformat(day_text)
    except ValueError as exc:
        raise SystemExit(f"ERROR: invalid --date {day_text!r}; expected YYYY-MM-DD") from exc
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def path_has_part(path: Path, part: str) -> bool:
    return any(piece.lower() == part.lower() for piece in path.parts)


def is_ignored_file(path: Path) -> bool:
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return True
    if path.name.lower() in {"thumbs.db", "desktop.ini"}:
        return True
    if path_has_part(path, "__pycache__"):
        return True
    if path_has_part(path, "daily"):
        return True
    return False


def find_existing_campaign_roots(experiments_root: Path) -> list[Path]:
    roots: list[Path] = []
    for daily_dir in experiments_root.rglob("daily"):
        if daily_dir.is_dir():
            roots.append(daily_dir.parent.resolve())
    roots.sort(key=lambda item: len(str(item)), reverse=True)
    return roots


def campaign_for_path(path: Path, experiments_root: Path, known_roots: list[Path]) -> Path:
    resolved = path.resolve()
    for root in known_roots:
        if resolved == root or root in resolved.parents:
            return root

    try:
        rel = resolved.relative_to(experiments_root.resolve())
    except ValueError:
        return experiments_root.resolve()
    if not rel.parts:
        return experiments_root.resolve()
    return (experiments_root / rel.parts[0]).resolve()


def collect_events(
    experiments_root: Path, known_roots: list[Path], start: datetime, end: datetime
) -> dict[Path, list[FileEvent]]:
    grouped: dict[Path, list[FileEvent]] = defaultdict(list)
    for path in experiments_root.rglob("*"):
        if not path.is_file() or is_ignored_file(path):
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if not (start <= mtime < end):
            continue
        campaign_root = campaign_for_path(path, experiments_root, known_roots)
        try:
            rel = path.relative_to(campaign_root)
        except ValueError:
            rel = path.relative_to(experiments_root)
        grouped[campaign_root].append(
            FileEvent(path=path, relative_to_campaign=rel, mtime=mtime, size=path.stat().st_size)
        )
    return grouped


def object_key(rel: Path) -> str:
    parts = rel.parts
    if len(parts) >= 4 and parts[0] == "results" and parts[1].startswith("chip"):
        return "/".join(parts[:4])
    if len(parts) >= 2 and parts[0] == "devices":
        return "/".join(parts[:2])
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return rel.as_posix()


def campaign_label(campaign_root: Path, experiments_root: Path) -> str:
    try:
        return campaign_root.relative_to(experiments_root).as_posix()
    except ValueError:
        return campaign_root.name


def summarize_activity(label: str, events: list[FileEvent]) -> list[str]:
    names = " ".join(event.relative_to_campaign.as_posix().lower() for event in events[:200])
    suffixes = Counter(event.path.suffix.lower() or "<none>" for event in events)
    lines: list[str] = []

    if "sensitivity" in names:
        sessions = sorted({match.group(0) for event in events for match in SESSION_RE.finditer(str(event.path))})
        if sessions:
            lines.append(f"检测到 {len(sessions)} 组 `sensitivity` session 更新。")
        else:
            lines.append("检测到 `sensitivity` 相关 raw/processed/figures 更新。")
    if "best_lock_candidate" in names:
        count = sum(1 for event in events if event.path.name == "best_lock_candidate.json")
        lines.append(f"检测到 {count} 个 `best_lock_candidate.json` 更新。")
    if "interactive_q" in names or "q_trend" in names or "q_by_mode" in names:
        lines.append("检测到 Q review / Q trend / family table 相关结果刷新。")
    if "cavity_card" in names:
        lines.append("检测到 `cavity_card.html` 展示页刷新。")
    if "ky_prm" in names or label.lower().startswith("pd_comparison"):
        lines.append("检测到 PD comparison 相关 raw 数据、metadata 或交互图更新。")
    if "ultrasound" in names or label.lower().startswith("calibrations"):
        lines.append("检测到 calibration 相关 raw/processed/figure/metadata 更新。")
    if "continuous_scan" in names or "triangular_scan" in names:
        lines.append("检测到自动耦合扫描优化状态文件更新。")

    if not lines:
        top_ext = ", ".join(f"`{ext}` x{count}" for ext, count in suffixes.most_common(4))
        lines.append(f"检测到一批实验文件更新，主要类型：{top_ext}。")

    return lines


def top_objects(events: list[FileEvent], limit: int = 10) -> list[tuple[str, int]]:
    counts = Counter(object_key(event.relative_to_campaign) for event in events)
    return counts.most_common(limit)


def time_range(events: list[FileEvent]) -> tuple[str, str]:
    ordered = sorted(events, key=lambda item: item.mtime)
    return ordered[0].mtime.strftime("%H:%M"), ordered[-1].mtime.strftime("%H:%M")


def build_daily_block(day_text: str, campaign_root: Path, experiments_root: Path, events: list[FileEvent]) -> str:
    label = campaign_label(campaign_root, experiments_root)
    first, last = time_range(events)
    suffixes = Counter(event.path.suffix.lower() or "<none>" for event in events)
    ext_text = ", ".join(f"`{ext}` x{count}" for ext, count in suffixes.most_common(6))
    objects = top_objects(events)
    activity_lines = summarize_activity(label, events)

    lines = [
        AUTO_START,
        "",
        f"根据文件时间戳自动补记：当天 `{label}` 下检测到 {len(events)} 个文件更新，时间约 {first}-{last}。",
        "以下只作为活动索引，不直接判断实验结论或数据有效性。",
        "",
        "## 文件活动",
    ]
    for line in activity_lines:
        lines.append(f"- {line}")
    lines.append(f"- 主要文件类型：{ext_text}。")

    if objects:
        lines.extend(["", "## 涉及对象"])
        for obj, count in objects:
            lines.append(f"- `{obj}`：{count} 个文件更新")

    lines.extend(
        [
            "",
            "## 待确认",
            "- 这些文件更新分别对应现场采集、后处理、补图还是批量刷新？",
            "- 哪些结果应进入正式 README、cavity card 或候选比较？",
            "- 是否存在无效、探索性或需要排除的数据组？",
            "",
            AUTO_END,
            "",
        ]
    )
    return "\n".join(lines)


def upsert_auto_block(path: Path, day_text: str, block: str) -> str:
    title = f"# {day_text}\n\n"
    if not path.exists():
        return title + block

    text = path.read_text(encoding="utf-8")
    if AUTO_START in text and AUTO_END in text:
        pattern = re.compile(
            re.escape(AUTO_START) + r".*?" + re.escape(AUTO_END) + r"\n?",
            flags=re.DOTALL,
        )
        return pattern.sub(block, text).rstrip() + "\n"

    if text.strip():
        return text.rstrip() + "\n\n" + block
    return title + block


def main() -> int:
    args = parse_args()
    if not args.data_root:
        print("ERROR: set DAILY_NOTE_DATA_ROOT or pass --data-root.", file=sys.stderr)
        return 2

    data_root = Path(args.data_root).expanduser().resolve()
    experiments_root = (
        Path(args.experiments_root).expanduser().resolve()
        if args.experiments_root
        else data_root / "experiments"
    )
    if not experiments_root.exists() or not experiments_root.is_dir():
        print(f"ERROR: experiments root not found: {experiments_root}", file=sys.stderr)
        return 2

    start, end = day_bounds(args.date)
    known_roots = find_existing_campaign_roots(experiments_root)
    grouped = collect_events(experiments_root, known_roots, start, end)
    wrote = 0

    for campaign_root, events in sorted(grouped.items(), key=lambda item: str(item[0]).lower()):
        if len(events) < args.min_files:
            continue
        daily_path = campaign_root / "daily" / f"{args.date}.md"
        block = build_daily_block(args.date, campaign_root, experiments_root, sorted(events, key=lambda item: item.mtime))
        new_text = upsert_auto_block(daily_path, args.date, block)

        rel_path = daily_path.relative_to(data_root) if data_root in daily_path.parents else daily_path
        if args.dry_run:
            print(f"[dry-run] would write {rel_path} ({len(events)} updated files)")
            continue

        daily_path.parent.mkdir(parents=True, exist_ok=True)
        daily_path.write_text(new_text, encoding="utf-8", newline="\n")
        print(f"wrote {rel_path} ({len(events)} updated files)")
        wrote += 1

    if wrote == 0 and not args.dry_run:
        print(f"No daily drafts written for {args.date}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
