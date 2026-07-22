"""
USHybrid AI -- calendar_us.py  (Guinevere)
US economic calendar with hard blocks around high-impact US events.
Fourth system in the Blackpool Trading Desk suite (S&P 500 via Capital.com, epic US500).

Hard block: 30 min either side of FOMC decisions, NFP, US CPI, US GDP (advance).
Soft context: US market open, Fed speakers, earnings season -- passed to Arthur.

All times are UTC.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("USHybrid.Guinevere")

HARD_BLOCK_MINUTES = 30

# ── Federal Reserve (FOMC) rate decisions ─────────────────────────────────────
# All at 19:00 UTC (14:00 ET) on the announcement day.
FOMC_DATES_2026 = [
    "2026-01-29", "2026-03-19", "2026-05-07", "2026-06-18",
    "2026-07-30", "2026-09-17", "2026-11-05", "2026-12-10",
]

# US data releases (NFP, CPI, GDP) are all released at 08:30 ET = 13:30 UTC.
US_DATA_HOUR_UTC = 13
US_DATA_MIN_UTC = 30

# US cash open: 09:30 ET = 14:30 UTC (soft volatility flag).
US_OPEN_HOUR_UTC = 14
US_OPEN_MIN_UTC = 30

# Months with heavy earnings season overlap (soft context).
EARNINGS_MONTHS = {1, 4, 7, 10}  # January, April, July, October

# Placeholder hook for scheduled Fed speakers (soft context).
# Populate with "YYYY-MM-DD HH:MM" UTC strings to flag as they are scheduled.
FED_SPEAKER_DATES = [
    # "2026-07-08 17:00",
]


def _fixed_utc(date_str: str, hour: int, minute: int) -> datetime:
    """Build a tz-aware UTC datetime from a date string plus a fixed UTC time."""
    return datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=hour, minute=minute, tzinfo=timezone.utc
    )


def _fomc_utc(date_str: str) -> datetime:
    """FOMC announcement at 19:00 UTC (14:00 ET)."""
    return _fixed_utc(date_str, 19, 0)


def _us_data_utc(date_str: str) -> datetime:
    """US data release at 13:30 UTC (08:30 ET) -- NFP, CPI, GDP."""
    return _fixed_utc(date_str, US_DATA_HOUR_UTC, US_DATA_MIN_UTC)


def _first_friday(year: int, month: int) -> datetime:
    """First Friday of the given month (NFP release day)."""
    d = datetime(year, month, 1)
    while d.weekday() != 4:  # Monday=0 .. Friday=4
        d += timedelta(days=1)
    return d


def _second_wednesday(year: int, month: int) -> datetime:
    """
    Second Wednesday of the given month.
    APPROXIMATION: US CPI is typically released mid-month on a Wednesday
    (usually the 2nd or 3rd Wednesday). We use the 2nd Wednesday as a
    reasonable, dynamically computed proxy for the standard CPI release day.
    """
    d = datetime(year, month, 1)
    while d.weekday() != 2:  # Wednesday=2
        d += timedelta(days=1)
    return d + timedelta(days=7)


def _last_wednesday(year: int, month: int) -> datetime:
    """Last Wednesday of the given month (GDP advance release day)."""
    # Move to the last day of the month, then walk back to a Wednesday.
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != 2:  # Wednesday=2
        d -= timedelta(days=1)
    return d


# ── Hard event builders ───────────────────────────────────────────────────────

def _get_fomc_events(year: Optional[int] = None) -> list:
    events = []
    for d in FOMC_DATES_2026:
        if year is not None and not d.startswith(f"{year}-"):
            continue
        try:
            events.append({
                "name":     "Federal Reserve (FOMC) Rate Decision",
                "datetime": _fomc_utc(d),
                "impact":   "HARD_BLOCK",
                "source":   "Fed",
                "date_str": d,
            })
        except Exception:
            pass
    return events


def _get_nfp_events(year: int) -> list:
    """Non-Farm Payrolls: first Friday of each month at 13:30 UTC."""
    events = []
    for month in range(1, 13):
        dt = _first_friday(year, month)
        events.append({
            "name":     "US Non-Farm Payrolls",
            "datetime": dt.replace(hour=US_DATA_HOUR_UTC, minute=US_DATA_MIN_UTC, tzinfo=timezone.utc),
            "impact":   "HARD_BLOCK",
            "source":   "US",
            "date_str": dt.strftime("%Y-%m-%d"),
        })
    return events


def _get_cpi_events(year: int) -> list:
    """US CPI: (approx) second Wednesday of each month at 13:30 UTC."""
    events = []
    for month in range(1, 13):
        dt = _second_wednesday(year, month)
        events.append({
            "name":     "US CPI Release",
            "datetime": dt.replace(hour=US_DATA_HOUR_UTC, minute=US_DATA_MIN_UTC, tzinfo=timezone.utc),
            "impact":   "HARD_BLOCK",
            "source":   "US",
            "date_str": dt.strftime("%Y-%m-%d"),
        })
    return events


def _get_gdp_events(year: int) -> list:
    """US GDP (advance): last Wednesday of Jan, Apr, Jul, Oct at 13:30 UTC."""
    events = []
    for month in (1, 4, 7, 10):
        dt = _last_wednesday(year, month)
        events.append({
            "name":     "US GDP (Advance)",
            "datetime": dt.replace(hour=US_DATA_HOUR_UTC, minute=US_DATA_MIN_UTC, tzinfo=timezone.utc),
            "impact":   "HARD_BLOCK",
            "source":   "US",
            "date_str": dt.strftime("%Y-%m-%d"),
        })
    return events


def _get_all_hard_events(year: int) -> list:
    return (
        _get_fomc_events(year)
        + _get_nfp_events(year)
        + _get_cpi_events(year)
        + _get_gdp_events(year)
    )


# ── Soft context builders ─────────────────────────────────────────────────────

def _get_fed_speaker_events() -> list:
    events = []
    for s in FED_SPEAKER_DATES:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            events.append({
                "name":     "Fed Speaker (scheduled)",
                "datetime": dt,
                "impact":   "SOFT",
                "source":   "Fed",
                "date_str": dt.strftime("%Y-%m-%d"),
            })
        except Exception:
            pass
    return events


def _get_soft_events(now_utc: datetime) -> list:
    """
    Soft (non-blocking) context events for Arthur.
    Includes the US cash open volatility flag for today/tomorrow plus any
    scheduled Fed speakers.
    """
    events = list(_get_fed_speaker_events())
    for day_offset in (0, 1):
        day = (now_utc + timedelta(days=day_offset)).date()
        open_dt = datetime(
            day.year, day.month, day.day,
            US_OPEN_HOUR_UTC, US_OPEN_MIN_UTC, tzinfo=timezone.utc,
        )
        events.append({
            "name":     "US Market Open (volatility)",
            "datetime": open_dt,
            "impact":   "SOFT",
            "source":   "US",
            "date_str": open_dt.strftime("%Y-%m-%d"),
        })
    return events


# ── Public API ────────────────────────────────────────────────────────────────

def check_calendar(now_utc: Optional[datetime] = None) -> dict:
    """
    Main calendar check. Returns:
      hard_block      : bool -- True means DO NOT TRADE
      block_reason    : str  -- why if hard_block
      upcoming_events : list -- events in next 24h
      calendar_summary: str  -- plain English for Arthur
      next_fomc       : str  -- next FOMC decision line
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    hard_events = _get_all_hard_events(now_utc.year)
    # Include next-year events near a year boundary so upcoming/next-FOMC work.
    if now_utc.month == 12:
        hard_events += _get_all_hard_events(now_utc.year + 1)
    soft_events = _get_soft_events(now_utc)

    # Check hard block window
    hard_block = False
    block_reason = ""
    block_name = ""
    for ev in hard_events:
        ev_time = ev["datetime"]
        delta_secs = (ev_time - now_utc).total_seconds()
        window = HARD_BLOCK_MINUTES * 60
        if -window <= delta_secs <= window:
            hard_block = True
            block_name = ev["name"]
            if delta_secs >= 0:
                mins = int(delta_secs / 60)
                block_reason = (
                    f"{ev['name']} in {mins} minutes "
                    f"({ev['datetime'].strftime('%H:%M UTC')}, {ev['date_str']}) -- "
                    f"hard block {HARD_BLOCK_MINUTES} min either side"
                )
            else:
                mins = int(-delta_secs / 60)
                block_reason = (
                    f"{ev['name']} released {mins} minutes ago -- "
                    f"volatility window active, {HARD_BLOCK_MINUTES - mins} min remaining"
                )
            log.warning("GUINEVERE HARD BLOCK: %s", block_reason)
            break

    # Upcoming events in next 24 hours
    upcoming = []
    window_24h = timedelta(hours=24)
    for ev in hard_events + soft_events:
        delta = ev["datetime"] - now_utc
        if timedelta(0) <= delta <= window_24h:
            mins_away = int(delta.total_seconds() / 60)
            upcoming.append({
                "name":      ev["name"],
                "time_utc":  ev["datetime"].strftime("%H:%M UTC"),
                "date":      ev["date_str"],
                "impact":    ev["impact"],
                "mins_away": mins_away,
            })
    upcoming.sort(key=lambda x: x["mins_away"])

    # Next FOMC decision
    future_fomc = sorted(
        [ev for ev in hard_events if ev["source"] == "Fed" and ev["datetime"] > now_utc],
        key=lambda x: x["datetime"],
    )
    next_fomc = future_fomc[0] if future_fomc else None
    next_fomc_str = ""
    if next_fomc:
        delta = next_fomc["datetime"] - now_utc
        days = delta.days
        next_fomc_str = (
            f"Next FOMC: {next_fomc['date_str']} at 19:00 UTC "
            f"({days} days away)"
        )

    # Earnings season flag (soft context)
    earnings_flag = now_utc.month in EARNINGS_MONTHS

    # Session-aware display (Snag 13): when the US cash session is currently
    # open, lead with "session active -- closes in X" rather than listing
    # tomorrow's Market Open (today's is in the past, so it read as ~1350 min
    # away, i.e. as if the market were shut). DISPLAY-ONLY: this string feeds
    # calendar_summary -> state["calendar"] (dashboard Guinevere card + Archie
    # Brief). Arthur's context comes from get_calendar_context(), which is
    # unchanged -- so trading behaviour is unaffected.
    session_active = False
    mins_to_close = 0
    try:
        from data_feed_us import get_session_phase
        if get_session_phase(now_utc) in ("US_OPEN", "CORE", "LATE"):
            session_active = True
            close_dt = now_utc.replace(hour=20, minute=45, second=0, microsecond=0)
            mins_to_close = max(0, int((close_dt - now_utc).total_seconds() / 60))
    except Exception:
        session_active = False

    # Calendar summary for Arthur
    if hard_block:
        summary = f"HARD BLOCK ACTIVE: {block_reason}"
    elif session_active:
        summary = (
            f"US session active -- closes in {mins_to_close} min "
            f"(force close 20:45 UTC). {next_fomc_str}"
        )
    elif upcoming:
        ev_list = "; ".join(
            f"{u['name']} in {u['mins_away']} min" for u in upcoming[:3]
        )
        summary = f"Upcoming: {ev_list}. {next_fomc_str}"
    else:
        summary = f"Calendar clear -- no events in next 24h. {next_fomc_str}"

    if earnings_flag:
        summary += " [earnings season]"

    return {
        "hard_block":       hard_block,
        "block_reason":     block_reason,
        "block_name":       block_name,
        "upcoming_events":  upcoming,
        "calendar_summary": summary,
        "next_fomc":        next_fomc_str,
        "earnings_season":  earnings_flag,
    }


def is_hard_blocked(now_utc: Optional[datetime] = None) -> tuple:
    """
    Quick check. Returns (hard_block: bool, reason: str, event_name: str, mins_remaining: int).
    """
    result = check_calendar(now_utc)
    if result["hard_block"]:
        return True, result["block_reason"], result["block_name"] or "US Event", HARD_BLOCK_MINUTES
    return False, "", "", 0


def upcoming_events(now_utc: Optional[datetime] = None) -> list:
    """Return the list of events (hard + soft) in the next 24 hours."""
    return check_calendar(now_utc)["upcoming_events"]


def get_calendar_context(now_utc: Optional[datetime] = None) -> str:
    """Return formatted calendar context string for Arthur."""
    cal = check_calendar(now_utc)
    lines = [
        "US ECONOMIC CALENDAR",
        f"  Status: {'HARD BLOCK ACTIVE' if cal['hard_block'] else 'Clear'}",
    ]
    if cal["block_reason"]:
        lines.append(f"  Block reason: {cal['block_reason']}")
    if cal["upcoming_events"]:
        lines.append("  Upcoming (next 24h):")
        for ev in cal["upcoming_events"][:5]:
            impact_str = "[HARD BLOCK]" if ev["impact"] == "HARD_BLOCK" else "[soft]"
            lines.append(
                f"    {ev['name']} -- {ev['time_utc']} "
                f"(in {ev['mins_away']} min) {impact_str}"
            )
    if cal["next_fomc"]:
        lines.append(f"  {cal['next_fomc']}")
    if cal["earnings_season"]:
        lines.append("  NOTE: Earnings season active -- expect single-name driven volatility.")
    lines.append(
        "  NOTE: US cash open 14:30 UTC -- watch for US500 volatility."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Guinevere self-test (USHybrid AI -- US500)")
    now = datetime.now(timezone.utc)
    log.info("Current UTC: %s", now.strftime("%Y-%m-%d %H:%M"))

    cal = check_calendar(now)
    log.info("Hard block: %s", cal["hard_block"])
    log.info("Summary: %s", cal["calendar_summary"])

    blocked, reason, name, mins = is_hard_blocked(now)
    log.info("is_hard_blocked -> %s (%s)", blocked, name or "none")

    if cal["upcoming_events"]:
        log.info("Upcoming events (next 24h):")
        for ev in cal["upcoming_events"]:
            log.info("  %s at %s in %d min %s",
                     ev["name"], ev["time_utc"], ev["mins_away"], ev["impact"])
    else:
        log.info("No events in next 24h.")

    log.info("--- Next few hard events overall ---")
    future = sorted(
        [e for e in _get_all_hard_events(now.year) + _get_all_hard_events(now.year + 1)
         if e["datetime"] > now],
        key=lambda x: x["datetime"],
    )
    for ev in future[:6]:
        log.info("  %s -- %s", ev["date_str"], ev["name"])

    log.info("\n%s", get_calendar_context(now))
    log.info("Guinevere self-test complete.")
