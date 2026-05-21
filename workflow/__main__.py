import argparse
import sys

from workflow.listener import record, service
from workflow.reports import daily_prompt
from workflow.sessions import create as session_create
from workflow.sessions import natural as session_natural
from workflow.sessions import start as session_start


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser(
        "record",
        help="write a remote-style text command into workspace notes",
    )
    record_parser.add_argument("message", help="text command, e.g. 记录：测试")
    record_parser.add_argument("--notes-dir", default="workspace/notes", help="notes root directory")
    record_parser.add_argument("--date", default=None, help="target note date in YYYY-MM-DD")
    record_parser.add_argument("--time", default=None, help="entry time in HH:MM")

    listen_parser = subparsers.add_parser(
        "listen",
        help="run a minimal no-UI listener",
    )
    listen_parser.add_argument("--stdin", action="store_true", help="read one text command per line from stdin")
    listen_parser.add_argument("--feishu", action="store_true", help="receive Feishu text messages through WebSocket")
    listen_parser.add_argument("--feishu-cli-event", action="store_true", help="receive Feishu events through lark-cli")
    listen_parser.add_argument("--stdin-event", action="store_true", help="read lark-cli-style NDJSON events from stdin")
    listen_parser.add_argument("--notes-dir", default="workspace/notes", help="notes root directory")
    listen_parser.add_argument("--index-dir", default="workspace/indexes", help="listener state directory")
    listen_parser.add_argument("--experiments-dir", default="workspace/experiments", help="experiments root directory")
    listen_parser.add_argument("--project-dir", default=".", help="directory containing config.local.json or .env")
    listen_parser.add_argument("--date", default=None, help="target note date in YYYY-MM-DD; stdin mode only")
    listen_parser.add_argument("--no-reply", action="store_true", help="do not send confirmation messages to Feishu")
    listen_parser.add_argument("--event-key", default="im.message.receive_v1", help="Feishu CLI EventKey")
    listen_parser.add_argument("--as", dest="as_identity", default="bot", help="lark-cli identity: bot, user, or auto")
    listen_parser.add_argument("--timeout", default=None, help="optional lark-cli timeout, e.g. 60s")
    listen_parser.add_argument("--max-events", type=int, default=None, help="optional lark-cli max event count")
    listen_parser.add_argument("--no-structured", action="store_true", help="only write raw log and session inbox")
    listen_parser.add_argument("--no-daily-fallback", action="store_true", help="fail instead of writing daily note when no session is active")
    listen_parser.add_argument("--no-auto-start-session", action="store_true", help="do not create a session from a start-like message")
    listen_parser.add_argument("--no-llm-classifier", action="store_true", help="use rule-based section guessing only")
    listen_parser.add_argument("--min-classifier-confidence", type=float, default=0.55, help="minimum confidence for applying LLM actions")

    report_parser = subparsers.add_parser(
        "report",
        help="build report-generation prompts",
    )
    report_subparsers = report_parser.add_subparsers(dest="report_command", required=True)
    daily_parser = report_subparsers.add_parser("daily", help="build a daily-summary prompt")
    daily_parser.add_argument("--date", default=None, help="target note date in YYYY-MM-DD")
    daily_parser.add_argument("--notes-dir", default="workspace/notes", help="notes root directory")
    daily_parser.add_argument(
        "--skill-path",
        default=str(daily_prompt.DEFAULT_SKILL_PATH),
        help="daily-summary skill path",
    )
    daily_parser.add_argument("--output", default=None, help="write prompt to this Markdown file")

    session_parser = subparsers.add_parser(
        "session",
        help="create experiment session directories",
    )
    session_subparsers = session_parser.add_subparsers(dest="session_command", required=True)
    create_session_parser = session_subparsers.add_parser("create", help="create an experiment session")
    create_session_parser.add_argument("name", help="session name, e.g. red-pitaya-pid-noise")
    create_session_parser.add_argument("--date", default=None, help="session date in YYYY-MM-DD")
    create_session_parser.add_argument(
        "--experiments-dir",
        default="workspace/experiments",
        help="experiments root directory",
    )
    create_session_parser.add_argument("--title", default=None, help="README title; defaults to name")
    create_session_parser.add_argument("--activate", action="store_true", help="make this the current active session")
    create_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")
    start_session_parser = session_subparsers.add_parser("start", help="start a session from natural language")
    start_session_parser.add_argument("description", help="natural-language session description")
    start_session_parser.add_argument("--date", default=None, help="session date in YYYY-MM-DD")
    start_session_parser.add_argument("--experiments-dir", default="workspace/experiments", help="experiments root directory")
    start_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")
    start_session_parser.add_argument("--title", default=None, help="override generated title")
    start_session_parser.add_argument("--name", default=None, help="override generated directory slug")
    start_session_parser.add_argument("--no-activate", action="store_true", help="create without making it active")
    current_session_parser = session_subparsers.add_parser("current", help="show the current active session")
    current_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")
    resume_session_parser = session_subparsers.add_parser("resume", help="resume the last closed session")
    resume_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")
    recent_session_parser = session_subparsers.add_parser("recent", help="list recent sessions")
    recent_session_parser.add_argument("--experiments-dir", default="workspace/experiments", help="experiments root directory")
    recent_session_parser.add_argument("--limit", type=int, default=5, help="number of sessions to show")
    switch_session_parser = session_subparsers.add_parser("switch", help="switch to a recent session by keyword or number")
    switch_session_parser.add_argument("query", help="session keyword or recent-session number")
    switch_session_parser.add_argument("--experiments-dir", default="workspace/experiments", help="experiments root directory")
    switch_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")
    activate_session_parser = session_subparsers.add_parser("activate", help="make a session directory active")
    activate_session_parser.add_argument("session_path", help="session directory path")
    activate_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")
    add_session_parser = session_subparsers.add_parser("add", help="append a natural-language note")
    add_session_parser.add_argument("text", help="natural-language note")
    add_session_parser.add_argument("--section", default="auto", help="target section, or auto")
    add_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")
    answer_session_parser = session_subparsers.add_parser("answer", help="save a useful AI answer")
    answer_session_parser.add_argument("text", help="answer or advice to save")
    answer_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")
    close_session_parser = session_subparsers.add_parser("close", help="close the active session")
    close_session_parser.add_argument("--summary", default=None, help="optional conclusion text")
    close_session_parser.add_argument("--index-dir", default="workspace/indexes", help="workflow state directory")

    args = parser.parse_args()

    if args.command == "record":
        record.main(
            [
                args.message,
                "--notes-dir",
                args.notes_dir,
                *(["--date", args.date] if args.date else []),
                *(["--time", args.time] if args.time else []),
            ],
            prog="python -m workflow record",
        )
        return

    if args.command == "report":
        if args.report_command == "daily":
            daily_prompt.main(
                [
                    *(["--date", args.date] if args.date else []),
                    "--notes-dir",
                    args.notes_dir,
                    "--skill-path",
                    args.skill_path,
                    *(["--output", args.output] if args.output else []),
                ],
                prog="python -m workflow report daily",
            )
            return

    if args.command == "session":
        if args.session_command == "create":
            session_create.main(
                [
                    args.name,
                    *(["--date", args.date] if args.date else []),
                    "--experiments-dir",
                    args.experiments_dir,
                    *(["--title", args.title] if args.title else []),
                    *(["--activate"] if args.activate else []),
                    "--index-dir",
                    args.index_dir,
                ],
                prog="python -m workflow session create",
            )
            return
        if args.session_command == "start":
            session_start.main(
                [
                    args.description,
                    *(["--date", args.date] if args.date else []),
                    "--experiments-dir",
                    args.experiments_dir,
                    "--index-dir",
                    args.index_dir,
                    *(["--title", args.title] if args.title else []),
                    *(["--name", args.name] if args.name else []),
                    *(["--no-activate"] if args.no_activate else []),
                ],
                prog="python -m workflow session start",
            )
            return
        if args.session_command == "current":
            session_natural.main(
                [
                    "current",
                    "--index-dir",
                    args.index_dir,
                ],
                prog="python -m workflow session",
            )
            return
        if args.session_command == "resume":
            session_natural.main(
                [
                    "resume",
                    "--index-dir",
                    args.index_dir,
                ],
                prog="python -m workflow session",
            )
            return
        if args.session_command == "recent":
            session_natural.main(
                [
                    "recent",
                    "--experiments-dir",
                    args.experiments_dir,
                    "--limit",
                    str(args.limit),
                ],
                prog="python -m workflow session",
            )
            return
        if args.session_command == "switch":
            session_natural.main(
                [
                    "switch",
                    args.query,
                    "--experiments-dir",
                    args.experiments_dir,
                    "--index-dir",
                    args.index_dir,
                ],
                prog="python -m workflow session",
            )
            return
        if args.session_command == "activate":
            session_natural.main(
                [
                    "activate",
                    args.session_path,
                    "--index-dir",
                    args.index_dir,
                ],
                prog="python -m workflow session",
            )
            return
        if args.session_command == "add":
            session_natural.main(
                [
                    "add",
                    args.text,
                    "--section",
                    args.section,
                    "--index-dir",
                    args.index_dir,
                ],
                prog="python -m workflow session",
            )
            return
        if args.session_command == "answer":
            session_natural.main(
                [
                    "answer",
                    args.text,
                    "--index-dir",
                    args.index_dir,
                ],
                prog="python -m workflow session",
            )
            return
        if args.session_command == "close":
            session_natural.main(
                [
                    "close",
                    *(["--summary", args.summary] if args.summary else []),
                    "--index-dir",
                    args.index_dir,
                ],
                prog="python -m workflow session",
            )
            return

    if args.command == "listen":
        service.main(
            [
                *(["--stdin"] if args.stdin else []),
                *(["--feishu"] if args.feishu else []),
                *(["--feishu-cli-event"] if args.feishu_cli_event else []),
                *(["--stdin-event"] if args.stdin_event else []),
                "--notes-dir",
                args.notes_dir,
                "--index-dir",
                args.index_dir,
                "--experiments-dir",
                args.experiments_dir,
                "--project-dir",
                args.project_dir,
                *(["--date", args.date] if args.date else []),
                *(["--no-reply"] if args.no_reply else []),
                "--event-key",
                args.event_key,
                "--as",
                args.as_identity,
                *(["--timeout", args.timeout] if args.timeout else []),
                *(["--max-events", str(args.max_events)] if args.max_events is not None else []),
                *(["--no-structured"] if args.no_structured else []),
                *(["--no-daily-fallback"] if args.no_daily_fallback else []),
                *(["--no-auto-start-session"] if args.no_auto_start_session else []),
                *(["--no-llm-classifier"] if args.no_llm_classifier else []),
                "--min-classifier-confidence",
                str(args.min_classifier_confidence),
            ],
            prog="python -m workflow listen",
        )
        return

    print(f"error: unknown command {args.command}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
