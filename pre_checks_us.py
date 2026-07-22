"""
USHybrid AI -- pre_checks_us.py  (Lancelot)
Hard filter guardian. Runs before Arthur is ever called.
US S&P 500 (US500) specific: UTC session aware, US kill limits.

Check order (spec):
  1. Kill switch
  2. Daily loss limit (GBP 60 = 6% of GBP 1000)
  3. Consecutive losses
  4. Cooldown period
  5. Market open (14:30-21:00 UTC only)
  6. Too close to close (block after 20:45 UTC)
  7. US open volatility (block 14:30-14:45 UTC)
  8. Daily trend filter (daily SSL sets direction)
  9. SSL agreement (1h SSL agrees with direction)
  10. RSI confirming (1h RSI at least 52 LONG, at most 48 SHORT)
  11. Momentum strong (5m TMO above +0.3 or below -0.3)
  12. Not choppy
  13. Candle confirmed (last 5m candle green/red)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import pandas as pd

from data_feed_us import get_session_phase, US_OPEN, CORE, LATE, PRE_MARKET, CLOSED

log = logging.getLogger("USHybrid.Lancelot")

# ── Thresholds ────────────────────────────────────────────────────────────────

DAILY_LOSS_LIMIT_GBP    = 60.0   # 6% of GBP 1000 -- hard stop for the day
MAX_CONSECUTIVE_LOSSES  = 5      # kill switch after this many in a row
COOLDOWN_MINUTES        = 30     # wait after a loss before next entry
MIN_TMO_FOR_ENTRY       = 0.3    # 5m TMO must exceed this magnitude
CHOPPY_RSI_THRESHOLD    = 5.0    # RSI within 5 of 50 = choppy
CHOPPY_TMO_THRESHOLD    = 0.5    # TMO within 0.5 of zero = choppy
CHOPPY_SIGNALS_REQUIRED = 2      # block if this many choppy signals

# Session time boundaries (minute-of-day, UTC)
_MARKET_OPEN   = 14 * 60 + 30    # 14:30
_MARKET_CLOSE  = 21 * 60         # 21:00
_NO_NEW_ENTRY  = 20 * 60 + 45    # 20:45 -- too close to close
_US_OPEN_END   = 14 * 60 + 45    # 14:45 -- opening volatility window ends


def _now_minute(ts_utc: Optional[datetime] = None) -> int:
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    ts_utc = ts_utc.astimezone(timezone.utc)
    return ts_utc.hour * 60 + ts_utc.minute


# ── Result builders ───────────────────────────────────────────────────────────

def _pass() -> dict:
    return {"passed": True, "reason": None}


def _fail(reason: str, block_direction: str = "BOTH") -> dict:
    log.info("  PRE-CHECK FAILED: %s", reason)
    return {"passed": False, "reason": reason, "block_direction": block_direction, "decision": "STAY_OUT"}


def _trigger_kill_switch(account, reason: str) -> dict:
    """Tiered kill switch. 1st trigger = 6h wait; 2nd = 12h; 3rd+ = 24h."""
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)
    history = [
        t for t in account.kill_history
        if datetime.fromisoformat(t).replace(tzinfo=timezone.utc) > cutoff
    ]
    history.append(now.isoformat())
    count = len(history)
    if count == 1:
        tier, wait_hours = 1, 6
    elif count == 2:
        tier, wait_hours = 2, 12
    else:
        tier, wait_hours = 3, 24
    account.kill_history       = history
    account.kill_switch_active = True
    account.kill_switch_reason = reason
    account.kill_switch_tier   = tier
    account.kill_switch_until  = (now + timedelta(hours=wait_hours)).isoformat()
    log.warning("KILL SWITCH (Tier %d) -- %s | auto-resume in %dh", tier, reason, wait_hours)
    result = _fail(reason)
    result["kill_switch_triggered"] = True
    result["kill_tier"] = tier
    return result


# ── Safety checks ─────────────────────────────────────────────────────────────

def check_kill_switch(account) -> dict:
    if account.kill_switch_active:
        return _fail(f"KILL SWITCH ACTIVE -- {account.kill_switch_reason or 'triggered'}")
    return _pass()


def check_daily_loss_limit(account) -> dict:
    daily_pnl = account.daily_pnl_gbp
    if daily_pnl <= -DAILY_LOSS_LIMIT_GBP:
        reason = (
            f"Daily loss limit hit (GBP {daily_pnl:.2f} / "
            f"limit GBP -{DAILY_LOSS_LIMIT_GBP:.2f})"
        )
        return _trigger_kill_switch(account, reason)
    return _pass()


def check_consecutive_losses(account) -> dict:
    n = account.consecutive_losses
    if n >= MAX_CONSECUTIVE_LOSSES:
        return _trigger_kill_switch(account, f"{n} consecutive losses")
    return _pass()


def check_kill_switch_reset(account) -> bool:
    """Auto-reset kill switch after the tier wait period. Returns True if reset."""
    if not account.kill_switch_active:
        return False
    if not account.kill_switch_until:
        return False
    until = datetime.fromisoformat(account.kill_switch_until)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) < until:
        return False
    tier = account.kill_switch_tier
    account.kill_switch_active = False
    account.kill_switch_reason = ""
    account.kill_switch_until  = None
    account.consecutive_losses = 0
    msg = f"Kill switch reset (Tier {tier}) -- resuming"
    if tier >= 3:
        msg += ". Manual review recommended."
    log.info(msg)
    return True


def check_cooldown(account) -> dict:
    """Block entries for COOLDOWN_MINUTES after a losing trade."""
    last_loss_time = account.last_loss_time
    if not last_loss_time:
        return _pass()
    try:
        if isinstance(last_loss_time, str):
            last_loss = datetime.fromisoformat(last_loss_time)
        else:
            last_loss = last_loss_time
        if last_loss.tzinfo is None:
            last_loss = last_loss.replace(tzinfo=timezone.utc)
        minutes_since = (datetime.now(timezone.utc) - last_loss).total_seconds() / 60
        if minutes_since < COOLDOWN_MINUTES:
            remaining = int(COOLDOWN_MINUTES - minutes_since)
            return _fail(f"Cooldown active -- {remaining} min remaining after last loss.")
    except Exception as exc:
        log.warning("Cooldown check error: %s", exc)
    return _pass()


# ── US session-time checks ────────────────────────────────────────────────────

def check_market_open(ts_utc: Optional[datetime] = None) -> dict:
    """Only allow entries while the US cash session is open (14:30-21:00 UTC)."""
    t = _now_minute(ts_utc)
    if _MARKET_OPEN <= t < _MARKET_CLOSE and (ts_utc or datetime.now(timezone.utc)).weekday() < 5:
        return _pass()
    return _fail("US market closed -- entries only 14:30-21:00 UTC, Mon-Fri.")


def check_near_close(ts_utc: Optional[datetime] = None) -> dict:
    """Block new entries after 20:45 UTC -- force close is imminent."""
    t = _now_minute(ts_utc)
    if t >= _NO_NEW_ENTRY:
        return _fail("Too close to close -- no new entries after 20:45 UTC (force close pending).")
    return _pass()


def check_us_open_volatility(ts_utc: Optional[datetime] = None) -> dict:
    """Block the first 15 minutes after the US open (14:30-14:45 UTC)."""
    t = _now_minute(ts_utc)
    if _MARKET_OPEN <= t < _US_OPEN_END:
        return _fail("US open volatility -- no entries in first 15 min (14:30-14:45 UTC).")
    return _pass()


# ── Quality checks ────────────────────────────────────────────────────────────

def check_daily_trend_filter(bar_1d: Optional[pd.Series], direction: str) -> dict:
    """
    Daily SSL sets the bias for the day.
    Daily BULL: LONG only. Daily BEAR: SHORT only. NaN: both allowed.
    """
    if bar_1d is None:
        return _pass()
    ssl_1d = bar_1d.get("ssl_bull")
    if pd.isna(ssl_1d):
        return _pass()
    if ssl_1d and direction == "SHORT":
        return _fail(
            "Daily SSL is BULL -- only LONG entries allowed today. "
            f"Proposed direction {direction} blocked.",
            block_direction="SHORT",
        )
    if not ssl_1d and direction == "LONG":
        return _fail(
            "Daily SSL is BEAR -- only SHORT entries allowed today. "
            f"Proposed direction {direction} blocked.",
            block_direction="LONG",
        )
    return _pass()


def check_ssl_agreement(bar_1h: pd.Series, bar_5m: pd.Series, direction: str = "BOTH") -> dict:
    """SSL alignment.

    Change 2 (System 4 Review, 18 Jul 2026): for a LONG, allow a bull-market pullback
    DIP entry -- PASS if EITHER 1h SSL is BULL (strong trend alignment) OR 1h SSL is
    BEAR but 1h RSI > 45 (a pullback, not a reversal). This lets the best LONG setups
    (dips within the uptrend) through while still blocking genuine reversals (RSI <= 45).
    For SHORT/BOTH the original 1h/5m agreement requirement is retained.
    """
    ssl_1h = bar_1h.get("ssl_bull")
    ssl_5m = bar_5m.get("ssl_bull")
    if pd.isna(ssl_1h) or pd.isna(ssl_5m):
        return _fail("SSL Cloud data unavailable")
    if direction == "LONG":
        if ssl_1h:
            return _pass()
        rsi_1h = bar_1h.get("rsi")
        if pd.notna(rsi_1h) and rsi_1h > 45:
            return _pass()   # pullback dip within the uptrend, not a reversal
        return _fail(
            f"1h SSL is BEAR and 1h RSI {'N/A' if pd.isna(rsi_1h) else f'{rsi_1h:.1f}'} "
            f"<= 45 -- reversal, not a dip. No LONG.", block_direction="LONG")
    if ssl_1h != ssl_5m:
        d1h = "BULL" if ssl_1h else "BEAR"
        d5m = "BULL" if ssl_5m else "BEAR"
        return _fail(f"SSL conflict -- 1h={d1h} but 5m={d5m}. Market in transition.")
    return _pass()


def check_1h_rsi_confirms(bar_1h: pd.Series, direction: str) -> dict:
    """1h RSI at least 52 for LONG, at most 48 for SHORT.

    Relaxed from 55/45 to 52/48 (Fix 3, backtest-validated v1.0.6): the wider
    band lets marginal-but-valid setups through without hurting win rate or
    drawdown on the ^GSPC 5m sample.
    """
    rsi_1h = bar_1h.get("rsi")
    if pd.isna(rsi_1h):
        return _pass()
    if direction == "LONG" and rsi_1h < 52:
        return _fail(f"1h RSI is {rsi_1h:.1f} -- need at least 52 for LONG entry.", block_direction="LONG")
    if direction == "SHORT" and rsi_1h > 48:
        return _fail(f"1h RSI is {rsi_1h:.1f} -- need at most 48 for SHORT entry.", block_direction="SHORT")
    return _pass()


def check_5m_tmo_momentum(bar_1h: pd.Series, bar_5m: pd.Series, direction: str = "BOTH") -> dict:
    """5m TMO must show meaningful momentum: > +0.3 for LONG, < -0.3 for SHORT.

    Change 2 (System 4 Review): keyed off the proposed DIRECTION, not the 1h SSL, so a
    bull-market dip entry (1h SSL briefly BEAR, direction LONG) is evaluated as a LONG
    (needs 5m TMO > +0.3, i.e. the dip turning back up) rather than as a SHORT."""
    tmo_5m = bar_5m.get("tmo_main")
    if pd.isna(tmo_5m):
        return _pass()
    d = direction if direction in ("LONG", "SHORT") else ("LONG" if bar_1h.get("ssl_bull") else "SHORT")
    if d == "LONG" and tmo_5m < MIN_TMO_FOR_ENTRY:
        return _fail(
            f"LONG setup but 5m TMO only {tmo_5m:.3f} -- need >{MIN_TMO_FOR_ENTRY} (dip turning up).",
            block_direction="LONG",
        )
    if d == "SHORT" and tmo_5m > -MIN_TMO_FOR_ENTRY:
        return _fail(
            f"SHORT setup but 5m TMO only {tmo_5m:.3f} -- need <-{MIN_TMO_FOR_ENTRY} for momentum.",
            block_direction="SHORT",
        )
    return _pass()


def check_choppy_market(bar_1h: pd.Series, bar_5m: pd.Series) -> dict:
    """Block if RSI and TMO both near their midpoints -- market is directionless."""
    choppy = []
    rsi_5m = bar_5m.get("rsi")
    if pd.notna(rsi_5m) and abs(rsi_5m - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy.append(f"5m RSI near 50 ({rsi_5m:.1f})")
    tmo_5m = bar_5m.get("tmo_main")
    if pd.notna(tmo_5m) and abs(tmo_5m) <= CHOPPY_TMO_THRESHOLD:
        choppy.append(f"5m TMO near zero ({tmo_5m:.3f})")
    rsi_1h = bar_1h.get("rsi")
    if pd.notna(rsi_1h) and abs(rsi_1h - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy.append(f"1h RSI near 50 ({rsi_1h:.1f})")
    if len(choppy) >= CHOPPY_SIGNALS_REQUIRED:
        return _fail(
            f"Choppy market: {', '.join(choppy)}. No clear direction -- best trade is no trade."
        )
    return _pass()


def check_candle_confirmed(bar_1h: pd.Series, bar_5m: pd.Series, direction: str = "BOTH") -> dict:
    """Last 5m candle must be green for LONG, red for SHORT.

    Change 2 (System 4 Review): keyed off the proposed DIRECTION, not the 1h SSL, so a
    LONG dip entry requires a GREEN confirmation candle even when the 1h SSL is a
    pullback BEAR."""
    open_price  = bar_5m.get("open")
    close_price = bar_5m.get("close")
    if pd.isna(open_price) or pd.isna(close_price):
        return _pass()
    d = direction if direction in ("LONG", "SHORT") else ("LONG" if bar_1h.get("ssl_bull") else "SHORT")
    candle_green = close_price >= open_price
    if d == "LONG" and not candle_green:
        return _fail("LONG setup but last 5m candle is RED -- waiting for green confirmation.",
                     block_direction="LONG")
    if d == "SHORT" and candle_green:
        return _fail("SHORT setup but last 5m candle is GREEN -- waiting for red confirmation.",
                     block_direction="SHORT")
    return _pass()


# ── Master runner ─────────────────────────────────────────────────────────────

def run_all_pre_checks(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    account,
    current_trade=None,
    bar_1d: Optional[pd.Series] = None,
    proposed_direction: str = "BOTH",
) -> dict:
    """
    Run all pre-checks in order. Returns on first failure.
    Arthur is only called if this returns passed=True.
    """
    log.info("--- Lancelot running pre-checks ---")

    safety_checks = [
        ("Kill switch",         lambda: check_kill_switch(account)),
        ("Daily loss limit",    lambda: check_daily_loss_limit(account)),
        ("Consecutive losses",  lambda: check_consecutive_losses(account)),
        ("Cooldown period",     lambda: check_cooldown(account)),
        ("Market open",         lambda: check_market_open()),
        ("Not near close",      lambda: check_near_close()),
        ("US open volatility",  lambda: check_us_open_volatility()),
    ]
    for name, fn in safety_checks:
        result = fn()
        if not result["passed"]:
            log.info("  [FAIL] %s -- %s", name, result["reason"])
            return result
        log.info("  [PASS] %s", name)

    if current_trade is None:
        direction = proposed_direction
        ssl_1h    = bar_1h.get("ssl_bull")
        if direction == "BOTH":
            direction = "LONG" if ssl_1h else "SHORT"

        quality_checks = [
            ("Daily trend filter",  lambda: check_daily_trend_filter(bar_1d, direction)),
            ("SSL agreement",       lambda: check_ssl_agreement(bar_1h, bar_5m, direction)),
            ("1h RSI confirming",   lambda: check_1h_rsi_confirms(bar_1h, direction)),
            ("Momentum strong",     lambda: check_5m_tmo_momentum(bar_1h, bar_5m, direction)),
            ("Not choppy",          lambda: check_choppy_market(bar_1h, bar_5m)),
            ("Candle confirmed",    lambda: check_candle_confirmed(bar_1h, bar_5m, direction)),
        ]
        for name, fn in quality_checks:
            result = fn()
            if not result["passed"]:
                log.info("  [FAIL] %s -- %s", name, result["reason"])
                return result
            log.info("  [PASS] %s", name)

    log.info("  All pre-checks passed -- ready for Arthur")
    return _pass()


def run_individual_pre_checks(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    account,
    current_trade=None,
    bar_1d: Optional[pd.Series] = None,
    proposed_direction: str = "BOTH",
) -> dict:
    """Run each check individually for dashboard display. Returns dict of name -> bool/None."""
    ssl_1h    = bar_1h.get("ssl_bull")
    direction = proposed_direction if proposed_direction in ("LONG", "SHORT") else ("LONG" if ssl_1h else "SHORT")
    checks    = {}
    checks["Kill Switch OK"]        = check_kill_switch(account)["passed"]
    checks["Daily Loss OK"]         = check_daily_loss_limit(account)["passed"]
    checks["Consecutive Losses OK"] = check_consecutive_losses(account)["passed"]
    checks["Cooldown OK"]           = check_cooldown(account)["passed"]
    checks["Market Open OK"]        = check_market_open()["passed"]
    checks["Not Near Close OK"]     = check_near_close()["passed"]
    checks["Past US Open OK"]       = check_us_open_volatility()["passed"]
    if current_trade is None:
        checks["Daily Trend OK"]    = check_daily_trend_filter(bar_1d, direction)["passed"]
        checks["SSL Aligned"]       = check_ssl_agreement(bar_1h, bar_5m, direction)["passed"]
        checks["1h RSI Confirming"] = check_1h_rsi_confirms(bar_1h, direction)["passed"]
        checks["Momentum Strong"]   = check_5m_tmo_momentum(bar_1h, bar_5m, direction)["passed"]
        checks["Not Choppy"]        = check_choppy_market(bar_1h, bar_5m)["passed"]
        checks["Candle Confirmed"]  = check_candle_confirmed(bar_1h, bar_5m, direction)["passed"]
    else:
        for k in ["Daily Trend OK", "SSL Aligned", "1h RSI Confirming",
                  "Momentum Strong", "Not Choppy", "Candle Confirmed"]:
            checks[k] = None
    return checks


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    import types
    log.info("Lancelot self-test (US500)")
    account_ok = types.SimpleNamespace(
        kill_switch_active=False, kill_switch_reason="",
        kill_switch_tier=0, kill_switch_until=None,
        kill_history=[], daily_pnl_gbp=-5.0,
        consecutive_losses=1, last_loss_time=None,
    )
    bar_1h = pd.Series({
        "ssl_bull": True, "rsi": 62.0, "macd_histogram": 12.0,
        "tmo_main": 2.1, "tmo_smooth": 1.5, "chande_mo": 45.0,
        "money_flow": 100.0, "open": 7480.0, "close": 7500.0,
    })
    bar_5m = pd.Series({
        "ssl_bull": True, "rsi": 58.0, "macd_histogram": 5.0,
        "tmo_main": 0.8, "tmo_smooth": 0.5, "chande_mo": 30.0,
        "money_flow": 80.0, "open": 7490.0, "close": 7500.0,
    })
    result = run_all_pre_checks(bar_1h, bar_5m, account_ok, proposed_direction="LONG")
    log.info("Result: %s", "PASSED" if result["passed"] else f"FAILED -- {result['reason']}")
    log.info("Lancelot self-test complete.")
