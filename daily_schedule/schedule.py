#!/usr/bin/env python3
"""
Daily Schedule Automation
=========================
Runs by 7am to organise your day using:
  - Microsoft Outlook (via Graph API)  → today's meetings
  - Todoist (REST API)                 → task prioritisation
  - Claude API (claude-opus-4-6)       → intelligent schedule + priorities

Setup: see .env.example for required credentials.
Cron example (runs at 6:45am daily):
  45 6 * * 1-5 cd /path/to/daily_schedule && python schedule.py
"""

import os
import sys
import json
import datetime
import threading
from zoneinfo import ZoneInfo

import anthropic
import requests
import msal
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration (set via .env or environment variables)
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
TODOIST_API_TOKEN   = os.getenv("TODOIST_API_TOKEN", "")
AZURE_CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "")
AZURE_TENANT_ID     = os.getenv("AZURE_TENANT_ID", "common")

# Your local timezone, e.g. "Europe/London", "America/New_York"
TIMEZONE            = os.getenv("TIMEZONE", "Europe/London")

# How many minutes your door-to-desk commute takes
COMMUTE_MINUTES     = int(os.getenv("COMMUTE_MINUTES", "30"))

# Optional: a note about your working situation, e.g. "hybrid - Tue/Thu in office"
WORKING_PATTERN     = os.getenv("WORKING_PATTERN", "")

# Where to persist the MS auth token between runs (avoids re-login every day)
TOKEN_CACHE_FILE    = os.path.expanduser("~/.daily_schedule_ms_cache.json")

GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES   = ["Calendars.Read", "User.Read"]


# ---------------------------------------------------------------------------
# Microsoft Graph – Outlook calendar
# ---------------------------------------------------------------------------

def _load_token_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_FILE):
        with open(TOKEN_CACHE_FILE) as f:
            cache.deserialize(f.read())
    return cache


def _save_token_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        with open(TOKEN_CACHE_FILE, "w") as f:
            f.write(cache.serialize())


def get_ms_access_token() -> str:
    """
    Returns a valid Microsoft Graph access token.
    Uses cached tokens when available; falls back to the device-code flow
    (opens a short browser login) when a fresh grant is needed.
    """
    if not AZURE_CLIENT_ID:
        raise EnvironmentError(
            "AZURE_CLIENT_ID is not set. "
            "Register an app in Azure Portal → App Registrations and add it to .env"
        )

    cache = _load_token_cache()
    app = msal.PublicClientApplication(
        AZURE_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}",
        token_cache=cache,
    )

    # Try silent refresh first (uses cached refresh token)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_token_cache(cache)
            return result["access_token"]

    # Interactive device-code flow – user opens browser once, then it's cached
    flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
    if "user_code" not in flow:
        raise Exception(f"Failed to start device flow: {flow.get('error_description')}")

    print("\n── Microsoft Login Required ─────────────────────────────────")
    print(flow["message"])
    print("─────────────────────────────────────────────────────────────\n")

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise Exception(f"Authentication failed: {result.get('error_description')}")

    _save_token_cache(cache)
    return result["access_token"]


def get_outlook_events() -> list[dict]:
    """Fetch calendar events for today from Microsoft Graph."""
    token = get_ms_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Prefer": f'outlook.timezone="{TIMEZONE}"',
    }

    tz        = ZoneInfo(TIMEZONE)
    today     = datetime.date.today()
    start_dt  = datetime.datetime.combine(today, datetime.time.min, tzinfo=tz)
    end_dt    = datetime.datetime.combine(today, datetime.time.max, tzinfo=tz)

    resp = requests.get(
        f"{GRAPH_ENDPOINT}/me/calendarView",
        headers=headers,
        params={
            "$select": (
                "subject,start,end,location,bodyPreview,"
                "isOnlineMeeting,showAs,sensitivity"
            ),
            "startDateTime": start_dt.isoformat(),
            "endDateTime":   end_dt.isoformat(),
            "$orderby":      "start/dateTime",
            "$top":          "50",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("value", [])


def format_outlook_events(events: list[dict]) -> str:
    if not events:
        return "No meetings scheduled today."

    lines = []
    for ev in events:
        raw_start = ev["start"].get("dateTime", ev["start"].get("date", ""))
        raw_end   = ev["end"].get("dateTime",   ev["end"].get("date",   ""))

        if "T" in raw_start:
            # Graph returns local time because of the Prefer header
            start_dt = datetime.datetime.fromisoformat(raw_start)
            end_dt   = datetime.datetime.fromisoformat(raw_end)
            duration = int((end_dt - start_dt).total_seconds() / 60)
            time_str = f"{start_dt.strftime('%H:%M')}–{end_dt.strftime('%H:%M')} ({duration} min)"
        else:
            time_str = "All day"

        location = ev.get("location", {}).get("displayName", "").strip()
        online   = ev.get("isOnlineMeeting", False)
        show_as  = ev.get("showAs", "busy")          # free | tentative | busy | oof | workingElsewhere
        subject  = ev.get("subject", "(No title)")
        preview  = (ev.get("bodyPreview") or "").strip()[:120]

        line = f"• {time_str}  {subject}"
        if online:
            line += " [Online]"
        elif location:
            line += f" @ {location}"
        if show_as == "tentative":
            line += " ⚠ Tentative"
        if preview:
            line += f"\n  └ {preview}"

        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Todoist
# ---------------------------------------------------------------------------

def get_todoist_tasks() -> list[dict]:
    """Fetch tasks due today or overdue from Todoist."""
    if not TODOIST_API_TOKEN:
        raise EnvironmentError("TODOIST_API_TOKEN is not set.")

    resp = requests.get(
        "https://api.todoist.com/rest/v2/tasks",
        headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}"},
        params={"filter": "today | overdue"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def format_todoist_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "No tasks due today."

    priority_label = {4: "🔴 P1", 3: "🟠 P2", 2: "🟡 P3", 1: "⬜ P4"}

    # Sort: highest priority first, then by due time
    def sort_key(t):
        p = -(t.get("priority", 1))
        due = t.get("due") or {}
        time_str = due.get("datetime") or due.get("date") or "9999"
        return (p, time_str)

    sorted_tasks = sorted(tasks, key=sort_key)
    lines = []
    for task in sorted_tasks:
        p     = task.get("priority", 1)
        label = priority_label.get(p, "⬜")
        due   = task.get("due") or {}
        due_time = ""
        if due.get("datetime"):
            dt = datetime.datetime.fromisoformat(due["datetime"].replace("Z", "+00:00"))
            due_time = f" [due {dt.strftime('%H:%M')}]"
        elif due.get("date"):
            if due["date"] < str(datetime.date.today()):
                due_time = f" [OVERDUE: {due['date']}]"

        lines.append(f"{label} {task['content']}{due_time}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude – schedule generation
# ---------------------------------------------------------------------------

def generate_schedule(events_text: str, tasks_text: str) -> None:
    """Stream a Claude-generated daily schedule to stdout."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    tz      = ZoneInfo(TIMEZONE)
    now     = datetime.datetime.now(tz)
    today   = now.strftime("%A, %d %B %Y")
    cur_time = now.strftime("%H:%M")

    pattern_note = f"\nMy working pattern: {WORKING_PATTERN}" if WORKING_PATTERN else ""

    prompt = f"""Today is {today}. Current time: {cur_time} ({TIMEZONE}).{pattern_note}
My commute door-to-desk takes approximately {COMMUTE_MINUTES} minutes.

━━━ OUTLOOK CALENDAR – TODAY ━━━
{events_text}

━━━ TODOIST – DUE TODAY / OVERDUE ━━━
{tasks_text}

Please produce my daily organiser. Structure it exactly as follows:

## Day at a Glance
A 2-3 sentence overview of what today looks like (meeting load, available focus time, key pressure points).

## Commute & Arrival
Based on the first in-person commitment (if any), what time should I leave home?
If everything is online or there are no meetings, note that I have flexibility.

## Time-Blocked Schedule
A realistic hour-by-hour plan from morning until end of day.
Include:
- Buffer time between meetings (at least 5 min)
- Suggested focus blocks for deep work
- Lunch (flag if a meeting is cutting into it)
- Where to slot the Todoist tasks

## Top 3 Priorities
The three most important things I must get done today, with a one-line reason each.

## Flags & Watch-outs
Any conflicts, tight transitions, overdue tasks that need attention, or things I should prepare in advance.
"""

    header = f"  DAILY ORGANISER – {today}  "
    bar    = "─" * len(header)

    print(f"\n┌{bar}┐")
    print(f"│{header}│")
    print(f"└{bar}┘\n")

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)

    print("\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("🗓  Fetching your day …\n")

    errors = []

    # -- Outlook --
    events_text = ""
    try:
        print("  → Outlook: connecting …")
        events = get_outlook_events()
        events_text = format_outlook_events(events)
        print(f"  → Outlook: {len(events)} event(s) found")
    except EnvironmentError as exc:
        msg = f"Outlook skipped – {exc}"
        print(f"  ⚠  {msg}")
        events_text = f"[{msg}]"
        errors.append(msg)
    except Exception as exc:
        msg = f"Outlook error – {exc}"
        print(f"  ⚠  {msg}")
        events_text = "[Calendar unavailable]"
        errors.append(msg)

    # -- Todoist --
    tasks_text = ""
    try:
        print("  → Todoist: fetching tasks …")
        tasks = get_todoist_tasks()
        tasks_text = format_todoist_tasks(tasks)
        print(f"  → Todoist: {len(tasks)} task(s) found")
    except EnvironmentError as exc:
        msg = f"Todoist skipped – {exc}"
        print(f"  ⚠  {msg}")
        tasks_text = f"[{msg}]"
        errors.append(msg)
    except Exception as exc:
        msg = f"Todoist error – {exc}"
        print(f"  ⚠  {msg}")
        tasks_text = "[Tasks unavailable]"
        errors.append(msg)

    if not ANTHROPIC_API_KEY:
        print("\n❌  ANTHROPIC_API_KEY is not set. Cannot generate schedule.")
        sys.exit(1)

    # -- Claude schedule --
    print("  → Claude: generating your schedule …")
    generate_schedule(events_text, tasks_text)

    if errors:
        print("⚠  Some integrations were unavailable:")
        for e in errors:
            print(f"   • {e}")


if __name__ == "__main__":
    main()
