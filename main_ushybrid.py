"""
USHybrid AI -- main_ushybrid.py
US S&P 500 (US500) spread betting main loop.
Mon-Fri, US cash session 14:30-21:00 UTC only.
No entries 14:30-14:45 UTC (open volatility) or after 20:45 UTC.
Force close at 20:45 UTC. No overnight positions, ever.

PAPER_TRADING_MODE = True until demo account is verified.
"""

import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# ── Config ────────────────────────────────────────────────────────────────────

PAPER_TRADING_MODE = True
VERSION            = "1.3.1"
CANDLE_INTERVAL    = 300      # 5-minute candle loop (seconds)
POSITION_INTERVAL  = 30       # position monitoring (seconds)
HEARTBEAT_INTERVAL = 240      # emit a liveness log at least this often, even when idle
DASHBOARD_INTERVAL = 15       # push live top-line state to the dashboard this often
BASE_DIR           = Path(__file__).resolve().parent
LOG_DIR            = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SHUTDOWN_FLAG      = LOG_DIR / "shutdown.flag"
LIFT_FLAG          = LOG_DIR / "confidence_lift.json"   # manual Morgan confidence lift (live)

# US session boundaries (minute-of-day, UTC)
_PRE_MARKET_START = 13 * 60 + 30   # 13:30
_MARKET_OPEN      = 14 * 60 + 30   # 14:30
_FORCE_CLOSE      = 20 * 60 + 45   # 20:45
_SESSION_END      = 21 * 60        # 21:00

# ── Env / logging setup ───────────────────────────────────────────────────────

_ENV_PATH = BASE_DIR / ".env"
# Capital.com / Anthropic credentials: prefer this system's own .env, then the
# template original's .env, then a known-good sibling (all Albion systems share the
# one Capital.com demo account). Fixes the yfinance fallback when a freshly-cloned
# hybrid has no own .env -- TideTraderAI/.env carries only Kraken keys, no CAPITALCOM_.
_ENV_CANDIDATES = [
    _ENV_PATH,
    BASE_DIR.parent / "USTraderAI" / ".env",
    BASE_DIR.parent / "USTraderAI" / ".env",
    BASE_DIR.parent / "GoldTraderAI" / ".env",
]
for _cand in _ENV_CANDIDATES:
    if _cand.exists():
        load_dotenv(dotenv_path=_cand)
        break
else:
    load_dotenv()

# ─── ALBION STANDING RULE: ALL LOG TIMESTAMPS ARE UTC ────────────────────────
# Force Python's logging to emit %(asctime)s in UTC, not BST/local. Without this
# line, logging defaults to local time and every log line is +1h vs the UTC CSV
# artefacts (phantom_trades.csv etc.) — the exact BST/UTC mismatch that caused a
# misread on 11 Jul 2026. Never interpret an Albion log timestamp as local time;
# confirm UTC before analysing. (Baked in per Nick's directive, 12 Jul 2026.)
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "ushybrid.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("USHybrid.Main")

# ── Internal imports ──────────────────────────────────────────────────────────

from agent_brain_us     import get_trading_decision, format_decision_for_display
from calendar_us        import check_calendar, is_hard_blocked, get_calendar_context
from data_feed_us       import (
    USDataFeed, get_session_phase, is_market_open, minutes_until_next_open,
)
from capitalcom_connector import CapitalComConnector
from notifier_us        import (
    notify_system_startup, notify_system_shutdown,
    notify_trade_opened, notify_trade_closed_win, notify_trade_closed_loss,
    notify_kill_switch_triggered, notify_kill_switch_reset,
    notify_calendar_block, notify_daily_summary, notify_system_error,
)
from paper_trader_us    import PaperTraderUS
from performance_us     import (
    get_performance_context, get_perf_dashboard_dict, invalidate_cache,
    generate_milestone_review,
)
from pre_checks_us      import run_all_pre_checks, run_individual_pre_checks
from strategy_us        import should_force_close, DEFAULT_GBPUSD
import performance_us
import regime_us   # market-regime reader (System 4 Review, 18 Jul 2026)

US_EPIC     = "US500"
GBPUSD_EPIC = "GBPUSD"

# Minimum Arthur confidence (System 4 Review, Change 1). Bidirectional sweep
# (24 Jul 2026, Nick's direct order): the bull/bear asymmetry is REMOVED -- a single
# symmetric floor of 50 applies regardless of regime, for LONG and SHORT alike. On this
# EXIT manager min_conf is context/display for Arthur only (it gates no mechanical entry).
ARTHUR_MIN_CONFIDENCE_BULL = 50
ARTHUR_MIN_CONFIDENCE_BEAR = 50

# ── Graceful shutdown ─────────────────────────────────────────────────────────

_SHUTDOWN = False

def _handle_signal(sig, frame):
    global _SHUTDOWN
    log.info("Shutdown signal received (%s)", sig)
    _SHUTDOWN = True

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Account state ─────────────────────────────────────────────────────────────

class AccountState:
    """Holds live trading account state passed to pre-checks."""

    def __init__(self, capital: float) -> None:
        self.capital_gbp        = capital
        self.daily_pnl_gbp      = 0.0
        self.consecutive_losses = 0
        self.last_loss_time     = None
        self.kill_switch_active = False
        self.kill_switch_tier   = 0
        self.kill_switch_until  = None
        self.kill_switch_reason = ""
        self.kill_history       = []

    def record_trade(self, pnl_gbp: float) -> None:
        self.daily_pnl_gbp += pnl_gbp
        self.capital_gbp = round(self.capital_gbp + pnl_gbp, 2)
        if pnl_gbp < 0:
            self.consecutive_losses += 1
            self.last_loss_time = datetime.now(timezone.utc)
        else:
            self.consecutive_losses = 0

    def reset_daily(self) -> None:
        self.daily_pnl_gbp = 0.0


# ── GBP/USD rate ──────────────────────────────────────────────────────────────

_gbpusd_cache = {"rate": DEFAULT_GBPUSD, "at": 0.0}


def _get_gbpusd(ig: CapitalComConnector) -> float:
    """Live GBP/USD from Capital.com, cached 60s, with a safe fallback."""
    now = time.monotonic()
    if now - _gbpusd_cache["at"] < 60:
        return _gbpusd_cache["rate"]
    try:
        if ig is not None and ig.connected:
            data = ig.get_price(GBPUSD_EPIC)
            if data:
                # Excalibur rounds mid to 1dp (fine for the index, too coarse for
                # FX) -- recompute from bid/ask to keep GBPUSD precision.
                bid, ask = data.get("bid"), data.get("ask")
                if bid and ask:
                    rate = (float(bid) + float(ask)) / 2
                else:
                    rate = float(data.get("mid") or 0)
                if 0.5 < rate < 3.0:   # sanity band
                    _gbpusd_cache.update(rate=rate, at=now)
                    return rate
    except Exception:
        pass
    _gbpusd_cache["at"] = now
    return _gbpusd_cache["rate"]


# ── Dashboard push (best-effort) ──────────────────────────────────────────────

DASHBOARD_URL = "http://localhost:5044/api/update"

_dash_first_ok:  bool  = False
_dash_fail_count: int  = 0
_dash_last_warn: float = 0.0


def _dashboard_push_ok(kind: str, phase: str, price: float, status: str, http) -> None:
    global _dash_first_ok
    if not _dash_first_ok:
        _dash_first_ok = True
        log.info("Dashboard connected -- first %s push OK | phase=%s us500=%.1f status=%s HTTP %s",
                 kind, phase, price, status, http)
    else:
        log.debug("Dashboard %s push | phase=%s us500=%.1f status=%s HTTP %s",
                  kind, phase, price, status, http)


def _dashboard_push_warn(exc: Exception) -> None:
    global _dash_fail_count, _dash_last_warn
    _dash_fail_count += 1
    now = time.monotonic()
    if now - _dash_last_warn > 60:
        log.warning("Dashboard push failing (%d so far): %s -- is dashboard_us.py running on :5044?",
                    _dash_fail_count, exc)
        _dash_last_warn = now


def _serialise_trade(trade):
    if trade is None:
        return None
    if hasattr(trade, "__dict__"):
        return {k: str(v) for k, v in trade.__dict__.items()}
    return trade


def _safe_float(v):
    try:
        f = float(v)
        return None if f != f else f  # NaN check (NaN != NaN)
    except (TypeError, ValueError):
        return None


def _indicator_snapshot(bar) -> dict:
    if bar is None:
        return {}
    return {
        "ssl_bull":   bool(bar.get("ssl_bull", False)),
        "rsi":        _safe_float(bar.get("rsi")),
        "macd":       _safe_float(bar.get("macd")),
        "tmo_main":   _safe_float(bar.get("tmo_main")),
        "chande_mo":  _safe_float(bar.get("chande_mo")),
        "money_flow": _safe_float(bar.get("money_flow")),
    }


def _push_dashboard(
    stanley:    PaperTraderUS,
    account:    AccountState,
    decision:   dict = None,
    pre_checks: dict = None,
    phase:      str  = "",
    us_level:   float = 0.0,
    gbpusd:     float = DEFAULT_GBPUSD,
    calendar_summary: str = "",
    connector_status: str = "yahoo",
    panel_mode: str = "pre_checks",
    trend_1d:   str = "NEUTRAL",
    trend_1h:   str = "NEUTRAL",
    signal_5m:  str = "NEUTRAL",
    indicators_1d: dict = None,
    indicators_1h: dict = None,
    indicators_5m: dict = None,
) -> None:
    """Push latest state to dashboard via HTTP POST (separate process)."""
    try:
        import requests
        perf = get_perf_dashboard_dict()
        payload = {
            "mode":          "PAPER" if PAPER_TRADING_MODE else "LIVE",
            "version":       VERSION,
            "epic":          US_EPIC,
            "phase":         phase,
            "us_level":      us_level,
            "gbpusd_rate":   gbpusd,
            "connector_status": connector_status,
            "capital":       stanley.capital_gbp,
            "daily_pnl":     account.daily_pnl_gbp,
            "total_trades":  stanley.total_trades,
            "win_rate":      stanley.win_rate,
            "in_trade":      stanley.in_trade,
            "current_trade": _serialise_trade(stanley.current_trade),
            "decision":      decision,
            "panel_mode":    panel_mode,
            "checklist":     (decision or {}).get("checklist", {}),
            "pre_checks":    pre_checks,
            "trend_1d":      trend_1d,
            "trend_1h":      trend_1h,
            "signal_5m":     signal_5m,
            "indicators_1d": indicators_1d or {},
            "indicators_1h": indicators_1h or {},
            "indicators_5m": indicators_5m or {},
            "perf":          perf,
            "calendar":      calendar_summary,
            "kill_switch":   account.kill_switch_active,
            "kill_tier":     account.kill_switch_tier,
            "updated_at":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        resp = requests.post(
            DASHBOARD_URL,
            data=json.dumps(payload, default=str),
            headers={"Content-Type": "application/json"},
            timeout=2,
        )
        _dashboard_push_ok("full", phase, us_level, connector_status, resp.status_code)
    except Exception as exc:
        _dashboard_push_warn(exc)


def _push_dashboard_live(
    stanley: PaperTraderUS,
    account: AccountState,
    ig:      CapitalComConnector,
    feed:    USDataFeed,
    now_utc: datetime,
) -> None:
    """
    Lightweight, frequent push of the always-known top-line fields (live price,
    phase, connector status, capital, P&L, open position). Runs every loop tick
    in ALL session phases so the dashboard never sits on its 0.0 / -- defaults.
    """
    try:
        import requests
        phase = get_session_phase(now_utc)
        price = _get_price(ig, feed)
        gbpusd = _get_gbpusd(ig)
        connector_status = "capitalcom" if (ig is not None and ig.connected) else "yahoo"
        payload = {
            "mode":             "PAPER" if PAPER_TRADING_MODE else "LIVE",
            "version":          VERSION,
            "epic":             US_EPIC,
            "phase":            phase,
            "us_level":         price,
            "gbpusd_rate":      gbpusd,
            "connector_status": connector_status,
            "capital":          stanley.capital_gbp,
            "daily_pnl":        account.daily_pnl_gbp,
            "total_trades":     stanley.total_trades,
            "win_rate":         stanley.win_rate,
            "in_trade":         stanley.in_trade,
            "current_trade":    _serialise_trade(stanley.current_trade),
            "kill_switch":      account.kill_switch_active,
            "kill_tier":        account.kill_switch_tier,
            "perf":             get_perf_dashboard_dict(),   # keep confidence exposed in ALL market states
            "updated_at":       now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        resp = requests.post(
            DASHBOARD_URL,
            data=json.dumps(payload, default=str),
            headers={"Content-Type": "application/json"},
            timeout=2,
        )
        _dashboard_push_ok("live", phase, price, connector_status, resp.status_code)
    except Exception as exc:
        _dashboard_push_warn(exc)


# ── Core candle tick ──────────────────────────────────────────────────────────

def _ssl(bar):
    """LONG/SHORT/None from a bar's ssl_bull flag (None or NaN -> None)."""
    if bar is None:
        return None
    v = bar.get("ssl_bull")
    if v is None or (isinstance(v, float) and v != v):
        return None
    return "LONG" if bool(v) else "SHORT"


def ssl_agreement(bar_1d, bar_1h, bar_5m):
    """Daily + 1h + 5m SSL must ALL agree -> LONG/SHORT, else None. USHybrid's
    Lancelot 3-timeframe entry signal (bidirectional -- unlike the LONG_ONLY original)."""
    d, h, m = _ssl(bar_1d), _ssl(bar_1h), _ssl(bar_5m)
    if d is not None and d == h == m:
        return d
    return None


def _major_event_within(now_utc, minutes):
    """True if a HARD_BLOCK calendar event is within `minutes` ahead (entry look-ahead)."""
    try:
        for ev in check_calendar(now_utc).get("upcoming_events", []):
            if ev.get("impact") == "HARD_BLOCK" and 0 <= ev.get("mins_away", 1e9) <= minutes:
                return True
    except Exception:
        pass
    return False


def run_candle_tick(
    feed:    USDataFeed,
    stanley: PaperTraderUS,
    account: AccountState,
    ig:      CapitalComConnector,
) -> None:
    """
    Called once every 5 minutes during a trading session.
    Gathers indicators, runs pre-checks, calls Arthur, acts on decision.
    """
    now_utc    = datetime.now(timezone.utc)
    phase      = get_session_phase(now_utc)
    us_price   = _get_price(ig, feed)
    gbpusd     = _get_gbpusd(ig)
    connector_status = "capitalcom" if (ig is not None and ig.connected) else "yahoo"

    log.info("--- CANDLE TICK | %s | phase=%s | US500=%.1f | GBPUSD=%.4f ---",
             now_utc.strftime("%H:%M:%S UTC"), phase, us_price, gbpusd)

    # Calendar check
    hard_blocked, block_reason, event_name, mins_remain = is_hard_blocked(now_utc)
    cal_context = get_calendar_context(now_utc)
    cal_summary = check_calendar(now_utc).get("calendar_summary", "")

    if hard_blocked:
        log.warning("CALENDAR HARD BLOCK: %s (%d min remaining)", block_reason, mins_remain)
        notify_calendar_block(event_name or block_reason, mins_remain)
        if not stanley.in_trade:
            _push_dashboard(stanley, account, phase=phase, us_level=us_price, gbpusd=gbpusd,
                            calendar_summary=cal_summary, connector_status=connector_status)
            return

    # Refresh data
    try:
        feed.refresh()
    except Exception as exc:
        log.error("Data refresh failed: %s", exc)
        return

    bar_1d = feed.latest_bar("1d")
    bar_1h = feed.latest_bar("1h")
    bar_5m = feed.latest_bar("5m")

    if bar_1h is None or bar_5m is None:
        log.warning("Insufficient indicator data -- skipping tick")
        return

    perf_context = get_performance_context()

    sig_1h = feed.composite_signal("1h")
    sig_5m = feed.composite_signal("5m")
    trend_1d = "LONG" if bar_1d.get("ssl_bull") else "SHORT"

    # USHybrid is BIDIRECTIONAL (unlike the LONG_ONLY original USTrader). The daily
    # SSL sets the session direction; the 3-timeframe SSL agreement below is the
    # actual Lancelot entry signal, in either direction.
    _ssl_1d = bar_1d.get("ssl_bull")
    proposed_direction = "NEUTRAL" if (_ssl_1d is None or _ssl_1d != _ssl_1d) else trend_1d

    # Regime is CONTEXT for Arthur's prompt only now -- no longer raises/lowers the
    # confidence bar (bidirectional sweep, 24 Jul 2026). Single symmetric floor for
    # LONG and SHORT. On this EXIT manager min_conf is display-only (gates no entry).
    regime = regime_us.get_regime()
    min_conf = ARTHUR_MIN_CONFIDENCE_BULL

    ind_1d = _indicator_snapshot(bar_1d)
    ind_1h = _indicator_snapshot(bar_1h)
    ind_5m = _indicator_snapshot(bar_5m)

    # ── HYBRID DECISION ───────────────────────────────────────────────────
    # USHybrid: Arthur manages the EXIT only. Entry is LANCELOT-only -- pre-checks +
    # 3-timeframe SSL agreement (BIDIRECTIONAL) + a 60-min calendar look-ahead. Arthur
    # is NEVER consulted on entry; no confidence/RSI entry gate and no Morgan SHORT gate
    # (fully bidirectional, 24 Jul 2026). No phantom logging in this system.
    if stanley.in_trade:
        # EXIT MANAGEMENT -- Arthur decides HOLD or EXIT only.
        decision = get_trading_decision(
            bar_1h=bar_1h, bar_5m=bar_5m, current_price=us_price,
            session_phase=phase, bar_1d=bar_1d, current_trade=stanley.current_trade,
            calendar_context=cal_context, perf_context=perf_context,
            regime=regime, min_confidence=min_conf,
        )
        action = decision.get("decision", "HOLD")
        if action != "EXIT":                 # Arthur only manages exits in this system
            action = "HOLD"
            decision["decision"] = "HOLD"
        log.info(format_decision_for_display(decision))
        _push_dashboard(stanley, account, decision=decision,
                        phase=phase, us_level=us_price, gbpusd=gbpusd, calendar_summary=cal_summary,
                        connector_status=connector_status, panel_mode="arthur",
                        trend_1d=trend_1d, trend_1h=sig_1h, signal_5m=sig_5m,
                        indicators_1d=ind_1d, indicators_1h=ind_1h, indicators_5m=ind_5m)
        if action == "EXIT":
            _close_trade(stanley, account, ig, us_price, "ARTHUR_EXIT", gbpusd)
        else:
            log.info("Arthur says HOLD -- maintaining position")
        return

    # ── FLAT: LANCELOT ENTRY (no Arthur) ─────────────────────────────────
    checks = run_all_pre_checks(
        bar_1h=bar_1h, bar_5m=bar_5m, account=account,
        current_trade=None, bar_1d=bar_1d, proposed_direction=proposed_direction,
    )
    individual_checks = run_individual_pre_checks(
        bar_1h=bar_1h, bar_5m=bar_5m, account=account,
        current_trade=None, bar_1d=bar_1d, proposed_direction=proposed_direction,
    )

    def _push_flat(panel="pre_checks", decision=None):
        _push_dashboard(stanley, account, decision=decision, pre_checks=individual_checks,
                        phase=phase, us_level=us_price, gbpusd=gbpusd, calendar_summary=cal_summary,
                        connector_status=connector_status, panel_mode=panel,
                        trend_1d=trend_1d, trend_1h=sig_1h, signal_5m=sig_5m,
                        indicators_1d=ind_1d, indicators_1h=ind_1h, indicators_5m=ind_5m)

    # NOTE (Job 2, 24 Jul 2026): a Type-1 hybrid's ENTRY is Lancelot-only and must fire
    # REGARDLESS of Morgan -- so there is NO Morgan hard-block on entry here (the earlier
    # three-zone hard-block was removed as architecturally wrong for Type-1). Morgan <30
    # instead sharpens Arthur's EXIT posture (see the in_trade branch above, which passes
    # the critical-Morgan context into Arthur's exit decision). Type-2 hybrids (Gold/Oil,
    # where Arthur gates entry) KEEP their entry hard-block.

    if not checks["passed"]:
        log.info("Pre-checks FAILED: %s", checks.get("reason"))
        _push_flat()
        if checks.get("kill_switch_triggered"):
            account.kill_switch_active = True
            tier = checks.get("kill_tier", 1)
            account.kill_switch_tier = tier
            wait_hours = {1: 6, 2: 12}.get(tier, 24)
            account.kill_switch_until = None
            notify_kill_switch_triggered(
                tier=tier, reason=checks.get("reason", ""), wait_hours=wait_hours,
                daily_pnl=account.daily_pnl_gbp, capital=stanley.capital_gbp)
        elif checks.get("kill_switch_reset"):
            account.kill_switch_active = False
            notify_kill_switch_reset(tier=account.kill_switch_tier, wait_hours=0,
                                     capital=stanley.capital_gbp)
            account.kill_switch_tier = 0
        return

    # 3-timeframe SSL agreement = the Lancelot entry signal (bidirectional).
    ssl_dir = ssl_agreement(bar_1d, bar_1h, bar_5m)
    if ssl_dir not in ("LONG", "SHORT"):
        log.info("No 3-TF SSL agreement -- no entry")
        _push_flat()
        return

    # Fully bidirectional (24 Jul 2026, Nick's direct order): the Morgan SHORT gate was
    # removed -- SHORT entries now proceed identically to LONG entries. (The three-zone
    # Morgan HARD BLOCK above, which suspends ALL new entries below 30, is unaffected.)

    # Calendar look-ahead -- HARD BLOCK a new entry if a major event is within 60 min.
    if _major_event_within(now_utc, 60):
        log.info("Entry blocked -- major calendar event within 60 min")
        _push_flat()
        return

    # ENTER immediately -- Lancelot only, no Arthur.
    log.info("LANCELOT ENTRY -- %s (pre-checks passed + Daily/1h/5m SSL agreed)", ssl_dir)
    _open_trade(stanley, account, ig, ssl_dir, us_price, phase, gbpusd)
    _push_flat(panel="arthur", decision={
        "decision": "ENTER_" + ssl_dir, "confidence": None,
        "reasoning": "Lancelot entry: pre-checks passed and Daily/1h/5m SSL all agreed %s. No Arthur on entry." % ssl_dir,
    })

    # Milestone review every 50 trades
    if stanley.total_trades > 0 and stanley.total_trades % 50 == 0:
        from paper_trader_us import TRADES_LOG
        milestone = stanley.total_trades // 50
        generate_milestone_review(TRADES_LOG, milestone)


# ── Position monitoring ───────────────────────────────────────────────────────

def monitor_open_position(
    stanley:  PaperTraderUS,
    account:  AccountState,
    ig:       CapitalComConnector,
    feed:     USDataFeed,
) -> None:
    """Called every 30 seconds while in a position. Trailing stop + force close."""
    if not stanley.in_trade:
        return

    now_utc  = datetime.now(timezone.utc)
    us_price = _get_price(ig, feed)
    gbpusd   = _get_gbpusd(ig)

    if should_force_close(now_utc):
        log.warning("Force close at 20:45 UTC -- closing all positions")
        _close_trade(stanley, account, ig, us_price, "FORCE_CLOSE_2045", gbpusd)
        return

    reason = stanley.monitor_trade(us_price, gbpusd)
    if reason:
        trade = stanley.trade_history[-1] if stanley.trade_history else None
        _handle_closed_trade(account, trade)
        log.info("Position auto-closed: %s | price=%.1f", reason, us_price)
        invalidate_cache()


# ── Open / close helpers ──────────────────────────────────────────────────────

def _open_trade(stanley, account, ig, direction, price, phase, gbpusd):
    trade = stanley.open_trade(direction, price, phase, gbpusd_rate=gbpusd)
    if PAPER_TRADING_MODE:
        log.info("[PAPER] OPEN %s | entry=%.1f | stop=%.1f | target=%.1f | stake=£%.2f/pt",
                 direction, price, trade.stop_loss, trade.take_profit, trade.stake)
    else:
        try:
            ig.open_position(
                epic=US_EPIC, direction="BUY" if direction == "LONG" else "SELL",
                size=trade.stake, stop_distance=trade.stop_pts,
            )
            log.info("[LIVE] OPEN %s via Capital.com | entry=%.1f", direction, price)
        except Exception as exc:
            log.error("Capital.com open_position failed: %s -- position tracked paper only", exc)
            notify_system_error(f"Capital.com open failed: {exc}")

    notify_trade_opened(
        direction=direction, entry_price=price,
        stop_loss=trade.stop_loss, take_profit=trade.take_profit,
        stake=trade.stake, session_phase=phase,
    )
    log.info("Trade opened: %s", trade.summary())


def _close_trade(stanley, account, ig, price, reason, gbpusd):
    trade = stanley.close_trade(price, reason, gbpusd)
    if trade is None:
        return
    _handle_closed_trade(account, trade)
    invalidate_cache()

    if not PAPER_TRADING_MODE:
        try:
            positions = ig.get_open_positions()
            for pos in positions:
                ig.close_position(
                    deal_id=pos.get("dealId"),
                    direction="SELL" if trade.direction == "LONG" else "BUY",
                    size=trade.stake,
                )
            log.info("[LIVE] Position closed via Capital.com | reason=%s", reason)
        except Exception as exc:
            log.error("Capital.com close_position failed: %s", exc)
            notify_system_error(f"Capital.com close failed: {exc}")

    if trade.pnl_gbp >= 0:
        notify_trade_closed_win(
            direction=trade.direction, exit_price=price,
            pnl_pts=trade.pnl_pts, pnl_gbp=trade.pnl_gbp,
            capital=account.capital_gbp, reason=reason,
        )
    else:
        notify_trade_closed_loss(
            direction=trade.direction, exit_price=price,
            pnl_pts=trade.pnl_pts, pnl_gbp=trade.pnl_gbp,
            capital=account.capital_gbp, reason=reason,
        )


def _handle_closed_trade(account: AccountState, trade) -> None:
    if trade is None:
        return
    account.record_trade(trade.pnl_gbp)
    log.info("Trade result: %s%+.2f GBP | capital=£%.2f",
             "+" if trade.pnl_gbp >= 0 else "", trade.pnl_gbp, account.capital_gbp)


# ── Price getter ──────────────────────────────────────────────────────────────

def _get_price(ig: CapitalComConnector, feed: USDataFeed) -> float:
    """Get current US500 price -- Capital.com first, yfinance fallback."""
    try:
        if ig is not None and ig.connected:
            price_data = ig.get_price(US_EPIC)
            if price_data:
                return price_data.get("mid", 0.0)
    except Exception:
        pass
    try:
        df = feed.get("5m")
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
    except Exception:
        pass
    return 0.0


# ── Daily summary ─────────────────────────────────────────────────────────────

_last_summary_date: str = ""


def _maybe_send_daily_summary(stanley: PaperTraderUS, account: AccountState) -> None:
    global _last_summary_date
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")
    if today == _last_summary_date:
        return
    t = now_utc.hour * 60 + now_utc.minute
    if t >= _SESSION_END:   # 21:00 UTC US close
        notify_daily_summary(
            date_str=today, trades=stanley.total_trades,
            pnl_gbp=account.daily_pnl_gbp, capital=stanley.capital_gbp,
            win_rate=stanley.win_rate,
        )
        account.reset_daily()
        _last_summary_date = today
        log.info("Daily summary sent for %s", today)


# ── Main loop ─────────────────────────────────────────────────────────────────

def _apply_confidence_lift() -> None:
    """Apply a pending manual confidence lift (logs/confidence_lift.json) in-process
    so a Gaius/dashboard lift takes effect LIVE -- Morgan's persisted baseline is
    otherwise cached in this process until restart. Written by the dashboard
    /api/lift-confidence endpoint (or Gaius --lift); consumed here via the existing
    set_confidence() and the flag deleted. Does not change the confidence algorithm."""
    import json
    try:
        if not LIFT_FLAG.exists():
            return
        data = json.loads(LIFT_FLAG.read_text(encoding="utf-8"))
        val = max(0.0, min(100.0, float(data.get("confidence", 50.0))))
        reason = data.get("reason") or "CONFIDENCE LIFT -- manual override"
        prior = performance_us.get_confidence()
        performance_us.set_confidence(val, reason=reason)
        LIFT_FLAG.unlink(missing_ok=True)
        log.warning("Morgan CONFIDENCE LIFT applied live: %.1f -> %.1f (%s)", prior, val, reason)
    except Exception as _exc:
        log.warning("Confidence lift apply failed: %s", _exc)
        try:
            LIFT_FLAG.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> None:
    global _SHUTDOWN
    log.info("=" * 70)
    log.info("  USHybrid AI v%s", VERSION)
    log.info("  US S&P 500 (US500) Spread Betting -- Capital.com")
    log.info("  Blackpool Trading Desk -- system 4 of 4")
    log.info("  Mode: %s", "PAPER TRADING" if PAPER_TRADING_MODE else "LIVE TRADING")
    log.info("=" * 70)

    # Capital.com connector -- always connect for live price data, even in paper
    # mode. PAPER_TRADING_MODE only controls whether trades are sent live below.
    ig = CapitalComConnector()
    try:
        ig.connect()
        ig_connected = True
        log.info("Capital.com connected")
    except Exception as exc:
        log.error("Capital.com connection failed: %s -- yfinance fallback", exc)
        ig_connected = False

    feed = USDataFeed(connector=ig if ig_connected else None)
    try:
        feed.initialise()
    except Exception as exc:
        log.warning("Initial data load partial: %s -- will retry", exc)

    # NOTE: USHybrid has NO phantom logging. Phantom tracks Arthur STAY-OUT decisions
    # on entry; this system never asks Arthur to gate entry (Lancelot enters), so there
    # is nothing to phantom-log. Morgan still learns from actual trade outcomes.

    # Restore Morgan's confidence from the CSV history so it survives a restart.
    try:
        _saved_conf = performance_us.load_confidence()
        if _saved_conf is not None:
            performance_us.set_confidence(_saved_conf, reason='restore')
            log.info("Morgan: confidence restored to %.1f from CSV on startup", _saved_conf)
        else:
            log.info("Morgan: no saved confidence found -- starting from baseline 50")
    except Exception as _exc:
        log.warning("Morgan confidence restore failed: %s", _exc)

    # Apply any confidence lift requested while the engine was down (Step 4).
    _apply_confidence_lift()

    stanley = PaperTraderUS()
    account = AccountState(capital=stanley.capital_gbp)
    stanley.print_status()

    notify_system_startup(
        capital=stanley.capital_gbp,
        mode="PAPER" if PAPER_TRADING_MODE else "LIVE",
    )

    SHUTDOWN_FLAG.unlink(missing_ok=True)

    log.info("USHybrid AI is running. Ctrl+C to stop.")
    log.info("Dashboard: http://localhost:5044  (start dashboard_us.py separately)")

    last_candle_tick    = 0.0
    last_position_check = 0.0
    last_heartbeat      = 0.0
    last_dashboard_push = 0.0
    _force_close_done   = False

    import random
    # Stagger Capital.com API calls across systems (shared demo Z6CJSM) to avoid 429s
    STARTUP_DELAY_SECONDS = 30
    _delay = STARTUP_DELAY_SECONDS + random.uniform(0, 10)  # jitter avoids re-sync
    log.info("Staggering Capital.com requests -- waiting %.0fs before main loop", _delay)
    time.sleep(_delay)

    while not _SHUTDOWN:
        try:
            now     = time.monotonic()
            now_utc = datetime.now(timezone.utc)
            t_min   = now_utc.hour * 60 + now_utc.minute

            # Dashboard shutdown flag -- leave on disk for Galahad (see main_albiontrader pattern)
            if SHUTDOWN_FLAG.exists():
                log.info("Shutdown requested via dashboard -- stopping (flag left for watchdog)")
                break

            # Apply a pending manual confidence lift live (Gaius intervention Step 4).
            _apply_confidence_lift()

            # Live dashboard push (all phases, every ~15s)
            if (now - last_dashboard_push) >= DASHBOARD_INTERVAL:
                _push_dashboard_live(stanley, account, ig, feed, now_utc)
                last_dashboard_push = now

            # Liveness heartbeat
            if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                log.info("Heartbeat -- alive | %s UTC | phase=%s | in_trade=%s",
                         now_utc.strftime("%H:%M"), get_session_phase(now_utc), stanley.in_trade)
                last_heartbeat = now

            # Skip weekends
            if now_utc.weekday() >= 5:
                log.debug("Weekend -- idle")
                _interruptible_sleep(HEARTBEAT_INTERVAL)
                continue

            # Outside the US session window entirely (before 13:30 or after 21:00 UTC)
            if t_min < _PRE_MARKET_START or t_min >= _SESSION_END:
                mins = minutes_until_next_open()
                sleep_sec = max(60, min(mins * 60, HEARTBEAT_INTERVAL)) if mins else HEARTBEAT_INTERVAL
                log.info("Market closed (%s UTC) -- next US open in %s min",
                         now_utc.strftime("%H:%M"), mins if mins else "?")
                _maybe_send_daily_summary(stanley, account)
                _interruptible_sleep(sleep_sec)
                _force_close_done = False
                continue

            # Force close at 20:45 UTC
            if t_min >= _FORCE_CLOSE:
                if stanley.in_trade and not _force_close_done:
                    price  = _get_price(ig, feed)
                    gbpusd = _get_gbpusd(ig)
                    log.warning("20:45 UTC force close triggered")
                    _close_trade(stanley, account, ig, price, "FORCE_CLOSE_2045", gbpusd)
                    _force_close_done = True
                _interruptible_sleep(30)
                continue

            # Position monitoring every 30 seconds
            if stanley.in_trade and (now - last_position_check) >= POSITION_INTERVAL:
                monitor_open_position(stanley, account, ig, feed)
                last_position_check = now

            # Candle tick every 5 minutes (during the trading session)
            if is_market_open() and (now - last_candle_tick) >= CANDLE_INTERVAL:
                run_candle_tick(feed, stanley, account, ig)
                last_candle_tick = now
            elif not is_market_open():
                # PRE_MARKET (13:30-14:30): warming up, no candle ticks / entries yet.
                _interruptible_sleep(30)
                continue

            _interruptible_sleep(5)

        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.error("Main loop error: %s", exc, exc_info=True)
            notify_system_error(str(exc)[:200])
            time.sleep(30)

    # Shutdown
    log.info("")
    log.info("=" * 70)
    log.info("  USHybrid AI -- Shutdown")
    log.info("=" * 70)
    if stanley.in_trade:
        log.warning("Position still open at shutdown -- closing paper record")
        price  = _get_price(ig, feed)
        gbpusd = _get_gbpusd(ig)
        _close_trade(stanley, account, ig, price, "SHUTDOWN", gbpusd)
    stanley.print_status()
    notify_system_shutdown(stanley.capital_gbp)
    log.info("USHybrid AI stopped cleanly.")


def _interruptible_sleep(seconds: float) -> None:
    """Sleep that responds to _SHUTDOWN flag."""
    end = time.monotonic() + seconds
    while not _SHUTDOWN and time.monotonic() < end:
        time.sleep(min(1, end - time.monotonic()))


if __name__ == "__main__":
    main()
