#!/usr/bin/env python3
"""
scripts/add_telegram_recipient.py

Add a new Telegram recipient to TELEGRAM_CHAT_ID in .env.

Usage:
    python scripts/add_telegram_recipient.py

The person must message @WhisperKW_bot first before their chat ID can be discovered.
"""

import os
import re
import sys
from pathlib import Path

import requests

ENV_FILE = Path(__file__).parent.parent / ".env"


def load_env() -> dict[str, str]:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(env: dict[str, str]) -> None:
    lines = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in env:
                    lines.append(f'{key}="{env.pop(key)}"')
                    continue
            lines.append(line)
    # Append any new keys not already in the file
    for k, v in env.items():
        lines.append(f'{k}="{v}"')
    ENV_FILE.write_text("\n".join(lines) + "\n")


def get_recent_updates(token: str) -> list[dict]:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        print(f"  Error fetching updates: {e}")
        return []


def main() -> None:
    env = load_env()
    token = env.get("TELEGRAMBOT", "")
    if not token:
        print("ERROR: TELEGRAMBOT not found in .env")
        sys.exit(1)

    current_ids = [cid.strip() for cid in env.get("TELEGRAM_CHAT_ID", "").split(",") if cid.strip()]

    print("=== Buzzowl — Add Telegram Recipient ===\n")
    print("Step 1: The person must open Telegram and send any message to @WhisperKW_bot")
    input("Press Enter once they have done that...")

    print("\nFetching recent messages from the bot...")
    updates = get_recent_updates(token)

    if not updates:
        print("\nNo messages found. Make sure the person sent a message to @WhisperKW_bot and try again.")
        sys.exit(1)

    # Collect unique senders not already in the list
    seen: dict[str, dict] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or msg.get("from") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id or chat_id in current_ids:
            continue
        name_parts = [chat.get("first_name", ""), chat.get("last_name", ""), chat.get("title", "")]
        name = " ".join(p for p in name_parts if p).strip() or f"ID {chat_id}"
        seen[chat_id] = {"name": name, "type": chat.get("type", "private")}

    if not seen:
        print("\nNo new senders found — everyone who messaged the bot is already in the list.")
        sys.exit(0)

    print(f"\nFound {len(seen)} new sender(s):\n")
    choices = list(seen.items())
    for i, (chat_id, info) in enumerate(choices, 1):
        type_label = f"[{info['type']}]" if info["type"] != "private" else ""
        print(f"  {i}. {info['name']} {type_label}  (ID: {chat_id})")

    print("\nEnter the number(s) to add, comma-separated (e.g. 1,3), or 'all':")
    raw = input("> ").strip()

    if raw.lower() == "all":
        to_add = choices
    else:
        indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()]
        to_add = [choices[i] for i in indices if 0 <= i < len(choices)]

    if not to_add:
        print("Nothing selected. Exiting.")
        sys.exit(0)

    for chat_id, info in to_add:
        current_ids.append(chat_id)
        print(f"  + Added: {info['name']} ({chat_id})")

    env["TELEGRAM_CHAT_ID"] = ",".join(current_ids)
    save_env(env)

    print(f"\n.env updated. {len(to_add)} recipient(s) added.")
    print(f"Total recipients: {len(current_ids)}")

    # Send a welcome message to new recipients
    for chat_id, info in to_add:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "👋 You've been added to *Buzzowl* notifications. You'll receive research updates and alerts here.",
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            print(f"  Welcome message sent to {info['name']}")
        except Exception as e:
            print(f"  Could not send welcome message to {info['name']}: {e}")


if __name__ == "__main__":
    main()
