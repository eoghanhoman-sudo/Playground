#!/usr/bin/env python3
"""
Personal Daily Organiser
========================
Runs at 6:45am to start your day with:
  - Apple Calendar  (iCloud CalDAV)  → today's events + tomorrow look-ahead
  - Apple Reminders (iCloud CalDAV)  → due today / overdue
  - Weather         (Open-Meteo)     → forecast for your location, no API key needed
  - Weekly review                    → Friday only: summary of the week ahead
  - macOS notification               → brief summary pushed to Notification Centre
  - Email                            → full plan delivered to your inbox

Setup: copy .env.example → .env and fill in your details.

Cron (runs 6:45am every day):
  45 6 * * * cd /path/to/daily_schedule && python schedule.py >> ~/daily_plan.log 2>&1
"""

import os
import sys
import datetime
import platform
import smtplib
import subprocess
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

import anthropic
import caldav
import requests
from icalendar import Calendar as iCal
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config — all values come from .env
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
APPLE_ID           = os.getenv("APPLE_ID", "")
APPLE_APP_PASSWORD = os.getenv("APPLE_APP_PASSWORD", "")

TIMEZONE           = os.getenv("TIMEZONE", "Europe/London")
DEPARTURE_TIME     = os.getenv("DEPARTURE_TIME", "07:00")
COMMUTE_MINUTES    = int(os.getenv("COMMUTE_MINUTES", "30"))

# Open-Meteo — no API key, just set your coordinates
# Find yours at: https://www.latlong.net/
LATITUDE           = os.getenv("LATITUDE", "")
LONGITUDE          = os.getenv("LONGITUDE", "")

# Optional: limit to specific calendar names (comma-separated). Empty = all.
CALENDAR_FILTER    = os.getenv("CALENDAR_FILTER", "")

# macOS notification (auto-detected; set to "false" to disable)
NOTIFY_MAC         = os.getenv("NOTIFY_MAC", "true").lower() != "false"

# Email delivery — leave EMAIL_TO blank to skip
EMAIL_TO           = os.getenv("EMAIL_TO", "")
EMAIL_FROM         = os.getenv("EMAIL_FROM", "")
EMAIL_PASSWORD     = os.getenv("EMAIL_PASSWORD", "")    # app-specific password
EMAIL_SMTP_HOST    = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
EMAIL_SMTP_PORT    = int(os.getenv("EMAIL_SMTP_PORT", "587"))

ICLOUD_URL = "https://caldav.icloud.com"


# ---------------------------------------------------------------------------
# Weather — Open-Meteo (free, no key)
# ---------------------------------------------------------------------------

# WMO weather interpretation codes → human-readable description + emoji
WMO_WEATHER = {
    0:  ("Clear sky", "☀️"),
    1:  ("Mainly clear", "🌤️"),  2: ("Partly cloudy", "⛅"),  3: ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"),         48: ("Icy fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Moderate drizzle", "🌦️"), 55: ("Heavy drizzle", "🌧️"),
    61: ("Light rain", "🌧️"),    63: ("Moderate rain", "🌧️"),   65: ("Heavy rain", "🌧️"),
    71: ("Light snow", "🌨️"),    73: ("Moderate snow", "❄️"),   75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Light showers", "🌦️"), 81: ("Moderate showers", "🌧️"), 82: ("Violent showers", "⛈️"),
    85: ("Snow showers", "🌨️"),  86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),  96: ("Thunderstorm + hail", "⛈️"), 99: ("Thunderstorm + hail", "⛈️"),
}


def get_weather() -> dict | None:
    """
    Fetch today's forecast from Open-Meteo. Returns a structured dict or None.
    No API key needed — just LATITUDE and LONGITUDE in .env.
    """
    if not LATITUDE or not LONGITUDE:
        return None

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":   LATITUDE,
                "longitude":  LONGITUDE,
                "current":    "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
                "hourly":     "temperature_2m,precipitation_probability,weather_code",
                "daily":      "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,sunrise,sunset",
                "timezone":   TIMEZONE,
                "forecast_days": 2,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"  ⚠  Weather unavailable: {exc}")
        return None


def format_weather(data: dict | None) -> str:
    if not data:
        return "[Weather unavailable — set LATITUDE and LONGITUDE in .env]"

    tz  = ZoneInfo(TIMEZONE)
    now = datetime.datetime.now(tz)

    cur = data.get("current", {})
    cur_code  = cur.get("weather_code", 0)
    cur_desc, cur_emoji = WMO_WEATHER.get(cur_code, ("Unknown", "🌡️"))
    cur_temp  = cur.get("temperature_2m", "?")
    cur_feel  = cur.get("apparent_temperature", "?")
    cur_wind  = cur.get("wind_speed_10m", "?")
    cur_rain  = cur.get("precipitation", 0)

    daily = data.get("daily", {})
    # daily has lists for today [0] and tomorrow [1]
    today_max   = daily.get("temperature_2m_max", [None, None])[0]
    today_min   = daily.get("temperature_2m_min", [None, None])[0]
    today_code  = daily.get("weather_code",        [0,    0   ])[0]
    today_rain  = daily.get("precipitation_sum",   [0,    0   ])[0]
    sunrise_str = daily.get("sunrise",             ["",   ""  ])[0]
    sunset_str  = daily.get("sunset",              ["",   ""  ])[0]

    tom_max  = daily.get("temperature_2m_max", [None, None])[1]
    tom_min  = daily.get("temperature_2m_min", [None, None])[1]
    tom_code = daily.get("weather_code",        [0,    0   ])[1]
    tom_desc, tom_emoji = WMO_WEATHER.get(tom_code, ("Unknown", "🌡️"))

    # Find the hour index for departure time
    dep_h    = int(DEPARTURE_TIME.split(":")[0])
    hourly   = data.get("hourly", {})
    h_times  = hourly.get("time", [])
    h_probs  = hourly.get("precipitation_probability", [])
    h_codes  = hourly.get("weather_code", [])

    dep_weather = ""
    for i, t in enumerate(h_times):
        if t.endswith(f"T{dep_h:02d}:00"):
            dep_code     = h_codes[i] if i < len(h_codes) else 0
            dep_desc, dep_emoji = WMO_WEATHER.get(dep_code, ("", ""))
            dep_precip   = h_probs[i] if i < len(h_probs) else 0
            dep_weather  = f"At {DEPARTURE_TIME}: {dep_emoji} {dep_desc}, {dep_precip}% rain chance"
            break

    # Find peak rain probability for the day (hourly, today only)
    today_str = now.strftime("%Y-%m-%d")
    today_probs = [
        h_probs[i] for i, t in enumerate(h_times)
        if t.startswith(today_str) and i < len(h_probs)
    ]
    max_rain_prob = max(today_probs) if today_probs else 0

    today_desc, today_emoji = WMO_WEATHER.get(today_code, ("Unknown", "🌡️"))

    lines = [
        f"Now:      {cur_emoji} {cur_desc}, {cur_temp}°C (feels {cur_feel}°C), wind {cur_wind} km/h",
    ]
    if dep_weather:
        lines.append(dep_weather)
    lines += [
        f"Today:    High {today_max}°C / Low {today_min}°C — {today_emoji} {today_desc}",
    ]
    if today_rain and today_rain > 0:
        lines.append(f"          Rain: {today_rain:.1f}mm expected, up to {max_rain_prob}% chance")
    if sunrise_str and sunset_str:
        try:
            sr = datetime.datetime.fromisoformat(sunrise_str).strftime("%H:%M")
            ss = datetime.datetime.fromisoformat(sunset_str).strftime("%H:%M")
            lines.append(f"          Sunrise {sr} · Sunset {ss}")
        except Exception:
            pass
    lines.append(f"Tomorrow: High {tom_max}°C / Low {tom_min}°C — {tom_emoji} {tom_desc}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# iCloud connection
# ---------------------------------------------------------------------------

def connect_icloud() -> caldav.Principal:
    if not APPLE_ID or not APPLE_APP_PASSWORD:
        raise EnvironmentError(
            "APPLE_ID and APPLE_APP_PASSWORD must be set. "
            "Generate an app-specific password at appleid.apple.com → Security."
        )
    client = caldav.DAVClient(url=ICLOUD_URL, username=APPLE_ID, password=APPLE_APP_PASSWORD)
    return client.principal()


# ---------------------------------------------------------------------------
# Apple Calendar — events
# ---------------------------------------------------------------------------

def _parse_vevent(component, cal_name: str, tz: ZoneInfo) -> dict | None:
    """Parse a VEVENT iCal component into a plain dict. Returns None to skip."""
    dtstart = component.get("DTSTART")
    dtend   = component.get("DTEND")
    if dtstart is None:
        return None

    start_val = dtstart.dt
    end_val   = (dtend.dt if dtend else start_val)

    is_all_day = isinstance(start_val, datetime.date) and not isinstance(start_val, datetime.datetime)

    if not is_all_day:
        start_val = start_val.replace(tzinfo=tz) if start_val.tzinfo is None else start_val.astimezone(tz)
        end_val   = end_val.replace(tzinfo=tz)   if end_val.tzinfo is None   else end_val.astimezone(tz)

    return {
        "summary":     str(component.get("SUMMARY", "(No title)")),
        "start":       start_val,
        "end":         end_val,
        "location":    str(component.get("LOCATION",    "") or "").strip(),
        "description": str(component.get("DESCRIPTION", "") or "").strip()[:150],
        "all_day":     is_all_day,
        "calendar":    cal_name,
    }


def _fetch_events_for_range(
    principal: caldav.Principal,
    start: datetime.datetime,
    end: datetime.datetime,
    tz: ZoneInfo,
    filter_names: set[str],
) -> list[dict]:
    events = []
    for cal in principal.calendars():
        cal_name = cal.name or ""
        if filter_names and cal_name not in filter_names:
            continue
        try:
            raw = cal.date_search(start=start, end=end, expand=True)
        except Exception:
            continue
        for ev in raw:
            try:
                parsed = iCal.from_ical(ev.data)
            except Exception:
                continue
            for component in parsed.walk():
                if component.name == "VEVENT":
                    item = _parse_vevent(component, cal_name, tz)
                    if item:
                        events.append(item)

    events.sort(key=lambda e: (
        0 if e["all_day"] else 1,
        e["start"] if not e["all_day"] else datetime.datetime.min,
    ))
    return events


def get_apple_events(principal: caldav.Principal) -> tuple[list[dict], list[dict]]:
    """Returns (today_events, tomorrow_events)."""
    tz    = ZoneInfo(TIMEZONE)
    today = datetime.date.today()
    tom   = today + datetime.timedelta(days=1)

    filter_names = {n.strip() for n in CALENDAR_FILTER.split(",") if n.strip()} if CALENDAR_FILTER else set()

    def day_range(d: datetime.date):
        return (
            datetime.datetime.combine(d, datetime.time.min, tzinfo=tz),
            datetime.datetime.combine(d, datetime.time.max, tzinfo=tz),
        )

    today_events = _fetch_events_for_range(principal, *day_range(today), tz, filter_names)
    tom_events   = _fetch_events_for_range(principal, *day_range(tom),   tz, filter_names)
    return today_events, tom_events


def get_week_events(principal: caldav.Principal) -> list[tuple[datetime.date, list[dict]]]:
    """Fetch the next 7 days of events (used on Fridays for weekly review)."""
    tz    = ZoneInfo(TIMEZONE)
    today = datetime.date.today()

    filter_names = {n.strip() for n in CALENDAR_FILTER.split(",") if n.strip()} if CALENDAR_FILTER else set()

    result = []
    for offset in range(1, 8):  # next 7 days
        d = today + datetime.timedelta(days=offset)
        start = datetime.datetime.combine(d, datetime.time.min, tzinfo=tz)
        end   = datetime.datetime.combine(d, datetime.time.max, tzinfo=tz)
        events = _fetch_events_for_range(principal, start, end, tz, filter_names)
        result.append((d, events))
    return result


def format_events(events: list[dict]) -> str:
    if not events:
        return "Nothing in the calendar."

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


def format_week_events(week: list[tuple[datetime.date, list[dict]]]) -> str:
    lines = []
    for d, events in week:
        day_label = d.strftime("%A %-d %b")
        if not events:
            lines.append(f"{day_label}: —")
        else:
            timed = [e for e in events if not e["all_day"]]
            allday = [e for e in events if e["all_day"]]
            summaries = []
            for e in allday:
                summaries.append(e["summary"])
            for e in timed:
                summaries.append(f"{e['start'].strftime('%H:%M')} {e['summary']}")
            lines.append(f"{day_label}: " + " · ".join(summaries))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Apple Reminders — VTODO
# ---------------------------------------------------------------------------

def get_apple_reminders(principal: caldav.Principal) -> list[dict]:
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
                due_val  = due_prop.dt
                due_date = due_val.date() if isinstance(due_val, datetime.datetime) else due_val
                if due_date > today:
                    continue

                reminders.append({
                    "summary":  str(component.get("SUMMARY", "(No title)")),
                    "due_date": due_date,
                    "priority": int(component.get("PRIORITY", 0) or 0),
                    "overdue":  due_date < today,
                    "calendar": cal.name or "",
                })

    reminders.sort(key=lambda r: (not r["overdue"], r["priority"] if r["priority"] else 99))
    return reminders


def format_reminders(reminders: list[dict]) -> str:
    if not reminders:
        return "No reminders due today."
    lines = []
    for r in reminders:
        prefix  = "🔴 OVERDUE" if r["overdue"] else "📌"
        due_str = f" [{r['due_date']}]" if r["overdue"] else ""
        lines.append(f"{prefix}  {r['summary']}{due_str}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude — plan generation
# ---------------------------------------------------------------------------

def generate_plan(
    events_text: str,
    tom_events_text: str,
    reminders_text: str,
    weather_text: str,
    week_text: str | None,
) -> str:
    """Stream the plan to stdout and return the full text for notifications/email."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    tz        = ZoneInfo(TIMEZONE)
    now       = datetime.datetime.now(tz)
    today     = now.strftime("%A, %-d %B %Y")
    cur_time  = now.strftime("%H:%M")
    is_friday = now.weekday() == 4

    dep_h, dep_m = map(int, DEPARTURE_TIME.split(":"))
    arr_time = (
        now.replace(hour=dep_h, minute=dep_m, second=0, microsecond=0)
        + datetime.timedelta(minutes=COMMUTE_MINUTES)
    ).strftime("%H:%M")

    weekly_section = ""
    if is_friday and week_text:
        weekly_section = f"""
━━━ WEEK AHEAD ━━━
{week_text}
"""

    prompt = f"""Today is {today}. Current time: {cur_time} ({TIMEZONE}).
I leave home at {DEPARTURE_TIME} and arrive after a {COMMUTE_MINUTES}-minute commute (~{arr_time}).

━━━ WEATHER ━━━
{weather_text}

━━━ APPLE CALENDAR – TODAY ━━━
{events_text}

━━━ TOMORROW ━━━
{tom_events_text}

━━━ REMINDERS – DUE TODAY / OVERDUE ━━━
{reminders_text}
{weekly_section}
You are my personal daily organiser, focused entirely on my personal life — \
health, family, social plans, errands, hobbies, and general wellbeing. \
Not work. Help me have a good, well-organised day.

Structure your response exactly as follows:

## Morning Snapshot
2–3 warm sentences: what kind of day is this? Busy or spacious? \
Anything to look forward to? Note if the weather affects anything.

## Today's Plan
Practical time-blocked plan from my arrival (~{arr_time}) through the evening. \
Include all calendar events with natural buffers, sensible slots for reminders, \
meal times (flag if anything clashes), and some personal breathing room where possible.

## Don't Forget
Exactly 3 bullet points — the things I must not let slip today, each with a one-liner reason.

## Personal Note
1–2 short paragraphs: gentle observations about today. \
Is there a window for exercise, rest, or quality time with someone? \
Call out any overdue reminders that deserve attention this morning. \
If it's Friday, briefly preview the week ahead and flag anything to prepare for.
"""

    header = f"  YOUR DAY – {today}  "
    bar    = "─" * len(header)
    print(f"\n┌{bar}┐")
    print(f"│{header}│")
    print(f"└{bar}┘\n")

    parts = []
    with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=2000,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            parts.append(text)

    print("\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_mac_notification(title: str, subtitle: str, message: str) -> None:
    """Push a macOS Notification Centre alert. Silent on non-Mac or if disabled."""
    if not NOTIFY_MAC or platform.system() != "Darwin":
        return
    script = (
        f'display notification "{message}" '
        f'with title "{title}" subtitle "{subtitle}"'
    )
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def send_email(subject: str, body: str) -> None:
    """Email the plan. Skipped silently if EMAIL_TO is not configured."""
    if not EMAIL_TO or not EMAIL_FROM or not EMAIL_PASSWORD:
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
            smtp.send_message(msg)
        print(f"  ✉  Plan emailed to {EMAIL_TO}")
    except Exception as exc:
        print(f"  ⚠  Email failed: {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not ANTHROPIC_API_KEY:
        print("❌  ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    print("🍎  Fetching your day …\n")

    # -- iCloud --
    try:
        principal = connect_icloud()
    except EnvironmentError as exc:
        print(f"❌  {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"❌  iCloud connection failed: {exc}")
        sys.exit(1)

    # -- Calendar events (today + tomorrow) --
    events_text     = "[Calendar unavailable]"
    tom_events_text = "[Unavailable]"
    try:
        print("  → Calendar: fetching events …")
        today_evs, tom_evs = get_apple_events(principal)
        events_text     = format_events(today_evs)
        tom_events_text = format_events(tom_evs)
        print(f"  → Calendar: {len(today_evs)} today, {len(tom_evs)} tomorrow")
        event_count = len(today_evs)
    except Exception as exc:
        print(f"  ⚠  Calendar error: {exc}")
        event_count = 0

    # -- Weekly review (Fridays only) --
    week_text = None
    if datetime.date.today().weekday() == 4:
        try:
            print("  → Weekly review: fetching next 7 days …")
            week_data = get_week_events(principal)
            week_text = format_week_events(week_data)
            print("  → Weekly review: ready")
        except Exception as exc:
            print(f"  ⚠  Weekly review error: {exc}")

    # -- Reminders --
    reminders_text = "[Reminders unavailable]"
    try:
        print("  → Reminders: fetching due items …")
        reminders = get_apple_reminders(principal)
        reminders_text = format_reminders(reminders)
        print(f"  → Reminders: {len(reminders)} due today")
    except Exception as exc:
        print(f"  ⚠  Reminders error: {exc}")

    # -- Weather --
    weather_text = "[Weather unavailable]"
    try:
        print("  → Weather: fetching forecast …")
        weather_data = get_weather()
        weather_text = format_weather(weather_data)
        print("  → Weather: ready")
    except Exception as exc:
        print(f"  ⚠  Weather error: {exc}")

    # -- Claude plan --
    print("  → Claude: building your plan …")
    plan_text = generate_plan(
        events_text, tom_events_text, reminders_text, weather_text, week_text
    )

    # -- macOS notification --
    tz       = ZoneInfo(TIMEZONE)
    now      = datetime.datetime.now(tz)
    day_name = now.strftime("%A")
    # Use first non-blank line of the plan as the notification body
    notif_body = next((l.strip() for l in plan_text.splitlines() if l.strip() and not l.startswith("#")), "")
    send_mac_notification(
        title    = f"Your Day – {day_name}",
        subtitle = f"{event_count} event{'s' if event_count != 1 else ''} today",
        message  = notif_body[:200],
    )

    # -- Email --
    tz      = ZoneInfo(TIMEZONE)
    subject = f"Your Day – {datetime.datetime.now(tz).strftime('%A, %-d %B %Y')}"
    send_email(subject, plan_text)


if __name__ == "__main__":
    main()
