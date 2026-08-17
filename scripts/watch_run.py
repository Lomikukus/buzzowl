#!/usr/bin/env python3
"""
Watch an agent run live — polls GET /runs/{id} every 2 s and prints each
new tool call as it arrives. Works with both Pi (:8001) and Hermes (:8002).

Usage:
    python scripts/watch_run.py <run_id>                  # default port 8001 (Pi)
    python scripts/watch_run.py <run_id> --port 8002      # Hermes
    python scripts/watch_run.py <run_id> --host localhost --port 8001

Quick start (trigger + watch in one step):
    python scripts/watch_run.py --trigger \
        --task "Research Siemens — 2025 strategy and financials" \
        --subject Siemens --port 8001
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Load token from .env if present (same key used by all services)
def _load_token() -> str:
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("AGENT_SERVICE_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("AGENT_SERVICE_TOKEN", "")

_TOKEN = _load_token()

TOOL_ICONS = {
    "web_search":      "🔍",
    "fetch_page":      "📄",
    "search_kb":       "🗄 ",
    "get_client":      "👤",
    "search_clients":  "👥",
    "write_document":  "💾",
}

STATUS_COLOR = {
    "queued":   "\033[90m",   # grey
    "running":  "\033[33m",   # yellow
    "done":     "\033[32m",   # green
    "failed":   "\033[31m",   # red
    "timeout":  "\033[31m",
    "cancelled":"\033[90m",
}
RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"


def _headers() -> dict:
    h = {}
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    return h


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", **_headers()},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _fmt_tool_call(tc: dict, idx: int) -> str:
    tool = tc.get("tool", tc.get("name", "?"))
    icon = TOOL_ICONS.get(tool, "⚙ ")
    ts   = tc.get("ts", "")[-8:]  # HH:MM:SS
    args = tc.get("args", tc.get("arguments", {}))

    # Summarise args for readability
    if tool == "web_search":
        detail = args.get("query", "") if isinstance(args, dict) else str(args)
    elif tool == "fetch_page":
        url = args.get("url", "") if isinstance(args, dict) else str(args)
        detail = url[:80]
    elif tool == "write_document":
        t = args.get("type", "?") if isinstance(args, dict) else "?"
        title = args.get("title", "") if isinstance(args, dict) else ""
        detail = f"[{t}] {title[:60]}"
    elif tool in ("search_kb", "search_clients"):
        q = args.get("query", args.get("partial_name", "")) if isinstance(args, dict) else str(args)
        detail = q[:60]
    elif tool == "get_client":
        detail = args.get("name", "") if isinstance(args, dict) else str(args)
    else:
        detail = str(args)[:60]

    result_summary = tc.get("result", "")[:80] if tc.get("result") else ""

    line = f"  {DIM}{ts}{RESET}  {icon} {BOLD}{tool}{RESET}  {detail}"
    if result_summary:
        line += f"\n         {DIM}→ {result_summary}{RESET}"
    return line


def watch(base_url: str, run_id: int, poll_interval: float = 2.0) -> None:
    url = f"{base_url}/runs/{run_id}"
    seen = 0
    last_status = ""

    print(f"\n{BOLD}Watching run #{run_id}{RESET}  {DIM}{url}{RESET}\n")

    while True:
        try:
            data = _get(url)
        except urllib.error.URLError as e:
            print(f"  {DIM}[poll error: {e}]{RESET}")
            time.sleep(poll_interval)
            continue

        status   = data.get("status", "?")
        tcs      = data.get("tool_calls", [])
        output   = data.get("output", {})
        error    = data.get("error")

        # Print status change header
        if status != last_status:
            color = STATUS_COLOR.get(status, "")
            print(f"{color}{BOLD}[{status.upper()}]{RESET}", end="")
            if status == "running":
                subject = data.get("subject", "")
                print(f"  {subject}", end="")
            print()
            last_status = status

        # Print new tool calls
        new_tcs = tcs[seen:]
        for i, tc in enumerate(new_tcs):
            print(_fmt_tool_call(tc, seen + i))
        seen += len(new_tcs)

        # Done / failed
        if status in ("done", "failed", "timeout", "cancelled"):
            print()
            if output:
                print(f"{BOLD}Output:{RESET}")
                for k, v in output.items():
                    print(f"  {k}: {v}")
            if error:
                print(f"{STATUS_COLOR['failed']}Error:{RESET} {error}")
            print(f"\n{DIM}Total tool calls seen: {seen}{RESET}\n")
            break

        time.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch an agent run live")
    parser.add_argument("run_id", nargs="?", type=int, help="Run ID to watch")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8001, help="8001=Pi, 8002=Hermes")
    parser.add_argument("--interval", type=float, default=2.0, help="Poll interval in seconds")
    # Trigger + watch
    parser.add_argument("--trigger", action="store_true", help="Trigger a new run then watch it")
    parser.add_argument("--task", default="", help="Task string (with --trigger)")
    parser.add_argument("--subject", default="", help="Subject (with --trigger)")
    parser.add_argument("--org-id", type=int, default=1, dest="org_id")
    parser.add_argument("--brain", default="", help="openrouter | ollama (with --trigger)")
    parser.add_argument("--model", default="", help="Model override (with --trigger)")
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"

    if args.trigger:
        if not args.task:
            print("--task is required with --trigger", file=sys.stderr)
            sys.exit(1)
        payload: dict = {
            "task": args.task,
            "org_id": args.org_id,
            "subject": args.subject or args.task[:40],
        }
        if args.brain:
            payload["brain"] = args.brain
        if args.model:
            payload["model"] = args.model
        try:
            resp = _post(f"{base_url}/runs", payload)
        except Exception as e:
            print(f"Failed to trigger run: {e}", file=sys.stderr)
            sys.exit(1)
        run_id = resp["run_id"]
        print(f"Triggered run #{run_id}")
    elif args.run_id is not None:
        run_id = args.run_id
    else:
        parser.print_help()
        sys.exit(1)

    watch(base_url, run_id, args.interval)


if __name__ == "__main__":
    main()
