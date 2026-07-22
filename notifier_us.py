"""
USHybrid AI -- notifier_us.py  (Percival)
Pushover push notifications. All failures are silent.
Loads credentials from same .env as TideTrader (shared Pushover account).
All notifications prefixed [US500].
"""

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

_ENV_PATH = BASE_DIR / ".env"
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH)
else:
    _TIDE_ENV = BASE_DIR.parent / "TideTraderAI" / ".env"
    if _TIDE_ENV.exists():
        load_dotenv(dotenv_path=_TIDE_ENV)
    else:
        load_dotenv()

log = logging.getLogger("USHybrid.Percival")

_PUSHOVER_API = "https://api.pushover.net/1/messages.json"
_USER         = os.getenv("PUSHOVER_USER_KEY",  "")
_TOKEN        = os.getenv("PUSHOVER_API_TOKEN", "")

_P_NORMAL = 0
_P_HIGH   = 1


def _send(title: str, message: str, priority: int = _P_NORMAL) -> None:
    if not _USER or not _TOKEN:
        log.debug("Pushover not configured -- skipping: %s", title)
        return
    try:
        resp = requests.post(
            _PUSHOVER_API,
            data={
                "token":    _TOKEN,
                "user":     _USER,
                "title":    title,
                "message":  message,
                "priority": priority,
            },
            timeout=5,
        )
        if resp.status_code == 200:
            log.debug("Notification sent: %s", title)
        else:
            log.warning("Pushover HTTP %d for: %s", resp.status_code, title)
    except Exception as exc:
        log.warning("Pushover notification failed (%s): %s", title, exc)


# ── Public notification functions ─────────────────────────────────────────────

def notify_trade_opened(
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    stake: float,
    session_phase: str = "",
) -> None:
    _send(
        title   = "[US500] Trade Opened -- USHybrid AI",
        message = (
            f"{direction} opened at {entry_price:,.1f}\n"
            f"Stop: {stop_loss:,.1f} | Target: {take_profit:,.1f}\n"
            f"Stake: £{stake:.4f}/pt | Session: {session_phase}"
        ),
    )


def notify_trade_closed_win(
    direction: str,
    exit_price: float,
    pnl_pts: float,
    pnl_gbp: float,
    capital: float,
    reason: str,
) -> None:
    _send(
        title   = "[US500] Trade WON -- USHybrid AI",
        message = (
            f"{direction} closed at {exit_price:,.1f}\n"
            f"Points: +{pnl_pts:.1f} | P&L: +£{pnl_gbp:.2f}\n"
            f"Capital: £{capital:.2f} | Reason: {reason}"
        ),
    )


def notify_trade_closed_loss(
    direction: str,
    exit_price: float,
    pnl_pts: float,
    pnl_gbp: float,
    capital: float,
    reason: str,
) -> None:
    _send(
        title   = "[US500] Trade Lost -- USHybrid AI",
        message = (
            f"{direction} closed at {exit_price:,.1f}\n"
            f"Points: {pnl_pts:.1f} | P&L: -£{abs(pnl_gbp):.2f}\n"
            f"Capital: £{capital:.2f} | Reason: {reason}"
        ),
    )


def notify_kill_switch_triggered(
    tier: int,
    reason: str,
    wait_hours: int,
    daily_pnl: float,
    capital: float,
) -> None:
    _send(
        title   = f"[US500] KILL SWITCH Tier {tier} -- USHybrid AI",
        message = (
            f"{reason}\n"
            f"Daily P&L: £{daily_pnl:+.2f}\n"
            f"Auto-resume in {wait_hours}h | Capital: £{capital:.2f}"
        ),
        priority = _P_HIGH,
    )


def notify_kill_switch_reset(tier: int, wait_hours: int, capital: float) -> None:
    _send(
        title   = "[US500] Trading Resuming -- USHybrid AI",
        message = (
            f"Kill switch reset after {wait_hours}h cooldown (Tier {tier}).\n"
            f"Capital: £{capital:.2f}. Watching for US500 setups."
        ),
    )


def notify_tier3_urgent() -> None:
    _send(
        title   = "[US500] URGENT: Manual Review Required",
        message = (
            "USHybrid AI kill switch triggered 3x in 48 hours.\n"
            "System paused 24 hours. Please review performance logs."
        ),
        priority = _P_HIGH,
    )


def notify_system_startup(capital: float, mode: str = "PAPER") -> None:
    _send(
        title   = f"[US500] USHybrid AI Started ({mode})",
        message = (
            f"USHybrid AI is live.\n"
            f"Capital: £{capital:.2f} | Mode: {mode}\n"
            f"Trading S&P 500 via Capital.com spread betting."
        ),
    )


def notify_system_shutdown(capital: float) -> None:
    _send(
        title   = "[US500] USHybrid AI Shutdown",
        message = (
            f"USHybrid AI stopped cleanly.\n"
            f"Final capital: £{capital:.2f}"
        ),
    )


def notify_calendar_block(event_name: str, mins_remaining: int) -> None:
    _send(
        title   = "[US500] Calendar Block Active",
        message = (
            f"Trading paused: {event_name}\n"
            f"{mins_remaining} min remaining. Will resume automatically."
        ),
    )


def notify_daily_summary(
    date_str: str,
    trades: int,
    pnl_gbp: float,
    capital: float,
    win_rate: float,
) -> None:
    _send(
        title   = f"[US500] Daily Summary {date_str}",
        message = (
            f"Trades: {trades} | P&L: £{pnl_gbp:+.2f}\n"
            f"Win rate: {win_rate:.1f}% | Capital: £{capital:.2f}"
        ),
    )


def notify_milestone_review(milestone_num: int) -> None:
    _send(
        title   = f"[US500] Milestone Review #{milestone_num} -- Arthur",
        message = (
            f"Arthur has completed his {milestone_num * 50}-trade review.\n"
            f"Check logs/arthur_review_{milestone_num:02d}.txt for insights."
        ),
    )


def notify_system_error(error_msg: str) -> None:
    _send(
        title   = "[US500] System Error -- USHybrid AI",
        message = f"Error detected:\n{error_msg[:200]}",
        priority = _P_HIGH,
    )
