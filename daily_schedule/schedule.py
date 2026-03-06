#!/usr/bin/env python3
"""
Personal Daily Organiser
========================
Runs by 7am to help you plan your day using:
  - Apple Calendar  (iCloud CalDAV → VEVENT)
  - Apple Reminders (iCloud CalDAV → VTODO)
  - Claude API      (claude-opus-4-6) → personalised plan

Setup: copy .env.example → .env and fill in your details.

Cron example (runs at 6:45am every morning):
  45 6 * * * cd /path/to/daily_schedule && python schedule.py
"""

import os
import sys
import datetime
from zoneinfo import ZoneInfo

import anthropic
import caldav
from icalendar import Calendar as iCal
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
APPLE_ID           = os.getenv("APPLE_ID", "")            # your iCloud email
APPLE_APP_PASSWORD = os.getenv("APPLE_APP_PASSWORD", "")  # app-specific password

# Your timezone (IANA format)
TIMEZONE           = os.getenv("TIMEZONE", "Europe/London")

# How long your commute takes (minutes)
COMMUTE_MINUTES    = int(os.getenv("COMMUTE_MINUTES", "30"))

# You leave home at this time each morning
DEPARTURE_TIME     = os.getenv("DEPARTURE_TIME", "07:00")

# Optional: comma-separated Apple Calendar names to include (empty = all)
# e.g. "Personal,Family,Health"
CALENDAR_FILTER    = os.getenv("CALENDAR_FILTER", "")

ICLOUD_URL = "https://caldav.icloud.com"


# ---------------------------------------------------------------------------
# iCloud connection
# ---------------------------------------------------------------------------

def connect_icloud() -> caldav.Principal:
    if not APPLE_ID or not APPLE_APP_PASSWORD:
        raise EnvironmentError(
            "APPLE_ID and APPLE_APP_PASSWORD must be set. "
            "Generate an app-specific password at appleid.apple.com → Security."
        )
    client = caldav.DAVClient(
        url=ICLOUD_URL,
        username=APPLE_ID,
        password=APPLE_APP_PASSWORD,
    )
    return client.principal()


# ---------------------------------------------------------------------------
# Apple Calendar – events
# ---------------------------------------------------------------------------

def get_apple_events(principal: caldav.Principal) -> list[dict]:
    """
    Fetch today's timed events from all (or filtered) Apple Calendars.
    iCloud exposes both Calendar and Reminders as CalDAV objects;
    we only want VEVENT components here.
    """
    tz    = ZoneInfo(TIMEZONE)
    today = datetime.date.today()
    start = datetime.datetime.combine(today, datetime.time.min, tzinfo=tz)
    end   = datetime.datetime.combine(today, datetime.time.max, tzinfo=tz)

    filter_names = (
        {n.strip() for n in CALENDAR_FILTER.split(",") if n.strip()}
        if CALENDAR_FILTER else set()
    )

    events = []
    for cal in principal.calendars():
        cal_name = cal.name or ""
        if filter_names and cal_name not in filter_names:
            continue

        try:
            raw_events = cal.date_search(start=start, end=end, expand=True)
        except Exception:
            # Some iCloud pseudo-calendars (Reminders lists, etc.) reject date_search
            continue

        for ev in raw_events:
            try:
                parsed = iCal.from_ical(ev.data)
            except Exception:
                continue

            for component in parsed.walk():
                if component.name != "VEVENT":
                    continue

                dtstart = component.get("DTSTART")
                dtend   = component.get("DTEND")
                if dtstart is None:
                    continue

                start_val = dtstart.dt
                end_val   = dtend.dt if dtend else start_val

                # All-day events are date objects; timed events are datetimes
                is_all_day = isinstance(start_val, datetime.date) and not isinstance(
                    start_val, datetime.datetime
                )

                if not is_all_day:
                    # Normalise to local tz
                    if start_val.tzinfo is None:
                        start_val = start_val.replace(tzinfo=tz)
                    else:
                        start_val = start_val.astimezone(tz)
                    if end_val.tzinfo is None:
                        end_val = end_val.replace(tzinfo=tz)
                    else:
                        end_val = end_val.astimezone(tz)

                events.append({
                    "summary":     str(component.get("SUMMARY", "(No title)")),
                    "start":       start_val,
                    "end":         end_val,
                    "location":    str(component.get("LOCATION", "") or "").strip(),
                    "description": str(component.get("DESCRIPTION", "") or "").strip()[:150],
                    "all_day":     is_all_day,
                    "calendar":    cal_name,
                })

    # Sort by start time; all-day events first
    events.sort(key=lambda e: (
        0 if e["all_day"] else 1,
        e["start"] if not e["all_day"] else datetime.datetime.min,
    ))
    return events


def format_events(events: list[dict]) -> str:
    if not events:
        return "Nothing in the calendar today."

    lines = []
    for ev in events:
        if ev["all_day"]:
            time_str = "All day"
        else:
            s = ev["start"]
            e = ev["end"]
            dur = int((e - s).total_seconds() / 60)
            time_str = f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')} ({dur} min)"

        line = f"• {time_str}  {ev['summary']}"
        if ev["location"]:
            line += f"  📍 {ev['location']}"
        if ev["calendar"]:
            line += f"  [{ev['calendar']}]"
        if ev["description"]:
            line += f"\n  └ {ev['description']}"

        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apple Reminders – todos
# ---------------------------------------------------------------------------

def get_apple_reminders(principal: caldav.Principal) -> list[dict]:
    """
    Fetch incomplete reminders that are due today or overdue.
    Reminders appear as VTODO components in iCloud CalDAV.
    """
    tz    = ZoneInfo(TIMEZONE)
    today = datetime.date.today()

    reminders = []
    for cal in principal.calendars():
        try:
            todos = cal.todos(include_completed=False)
        except Exception:
            continue

        for todo in todos:
            try:
                parsed = iCal.from_ical(todo.data)
            except Exception:
                continue

            for component in parsed.walk():
                if component.name != "VTODO":
                    continue

                due_prop = component.get("DUE") or component.get("DTSTART")
                if due_prop is None:
                    continue

                due_val = due_prop.dt
                due_date = due_val if isinstance(due_val, datetime.date) and not isinstance(
                    due_val, datetime.datetime
                ) else due_val.date() if isinstance(due_val, datetime.datetime) else due_val

                if due_date > today:
                    continue  # Future reminder, skip

                summary  = str(component.get("SUMMARY", "(No title)"))
                priority = int(component.get("PRIORITY", 0) or 0)
                # iCal priority: 1–4 high, 5 medium, 6–9 low, 0 undefined
                overdue  = due_date < today

                reminders.append({
                    "summary":  summary,
                    "due_date": due_date,
                    "priority": priority,
                    "overdue":  overdue,
                    "calendar": cal.name or "",
                })

    # Sort: overdue first, then by priority (1 = highest)
    reminders.sort(key=lambda r: (not r["overdue"], r["priority"] if r["priority"] else 99))
    return reminders


def format_reminders(reminders: list[dict]) -> str:
    if not reminders:
        return "No reminders due today."

    lines = []
    for r in reminders:
        prefix = "🔴 OVERDUE" if r["overdue"] else "📌"
        due_str = f" [{r['due_date']}]" if r["overdue"] else ""
        lines.append(f"{prefix}  {r['summary']}{due_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude – personal day planner
# ---------------------------------------------------------------------------

def generate_plan(events_text: str, reminders_text: str) -> None:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    tz       = ZoneInfo(TIMEZONE)
    now      = datetime.datetime.now(tz)
    today    = now.strftime("%A, %d %B %Y")
    cur_time = now.strftime("%H:%M")

    # Work out arrival time from departure + commute
    dep_h, dep_m = map(int, DEPARTURE_TIME.split(":"))
    dep_dt  = now.replace(hour=dep_h, minute=dep_m, second=0, microsecond=0)
    arr_dt  = dep_dt + datetime.timedelta(minutes=COMMUTE_MINUTES)
    arrival = arr_dt.strftime("%H:%M")

    prompt = f"""Today is {today}. Current time: {cur_time} ({TIMEZONE}).

I leave home at {DEPARTURE_TIME} each morning and arrive after a {COMMUTE_MINUTES}-minute commute (arriving ~{arrival}).

━━━ APPLE CALENDAR – TODAY ━━━
{events_text}

━━━ APPLE REMINDERS – DUE TODAY / OVERDUE ━━━
{reminders_text}

You are my personal daily organiser. Focus entirely on my personal life — \
not work tasks, but things like health, family, social plans, errands, personal goals, \
and making sure I actually enjoy my day.

Please structure your response exactly like this:

## Morning Snapshot
A warm, brief (2–3 sentence) summary of today. How full is the day? \
What's the overall feel of it?

## Today's Plan
A practical time-blocked plan from when I arrive (~{arrival}) through the evening. \
Cover:
- All calendar events with natural buffers before/after
- Sensible spots for any reminders/errands
- Meal times (flag if an event clashes with lunch or dinner)
- Some downtime or personal time if the day allows

## Don't Forget
The top 3 things I absolutely must not let slip today, each with a one-liner on why.

## Personal Notes
Any gentle observations: is the day overloaded? Is there something worth doing for \
myself (exercise, an early night, time with someone)? Flag any overdue reminders \
that deserve attention this morning before the day kicks off.
"""

    header = f"  YOUR DAY – {today}  "
    bar    = "─" * len(header)

    print(f"\n┌{bar}┐")
    print(f"│{header}│")
    print(f"└{bar}┘\n")

    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=2000,
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
    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    print("🍎  Fetching your day from iCloud …\n")

    try:
        principal = connect_icloud()
    except EnvironmentError as exc:
        print(f"❌  {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"❌  Could not connect to iCloud: {exc}")
        sys.exit(1)

    # -- Calendar events --
    events_text = ""
    try:
        print("  → Calendar: fetching events …")
        events = get_apple_events(principal)
        events_text = format_events(events)
        print(f"  → Calendar: {len(events)} event(s) today")
    except Exception as exc:
        events_text = "[Calendar unavailable]"
        print(f"  ⚠  Calendar error: {exc}")

    # -- Reminders --
    reminders_text = ""
    try:
        print("  → Reminders: fetching due items …")
        reminders = get_apple_reminders(principal)
        reminders_text = format_reminders(reminders)
        print(f"  → Reminders: {len(reminders)} item(s) due today")
    except Exception as exc:
        reminders_text = "[Reminders unavailable]"
        print(f"  ⚠  Reminders error: {exc}")

    print("  → Claude: building your plan …")
    generate_plan(events_text, reminders_text)


if __name__ == "__main__":
    main()
