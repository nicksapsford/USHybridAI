"""
USHybrid AI -- strategy_us.py
US S&P 500 (US500) spread betting strategy mechanics.
Points-based trailing stop. P&L = points_moved * stake_per_point.

Currency model:
  - US500 is priced in USD index points (~7,500 in 2026).
  - The spread bet stake is denominated in GBP per point (£0.67/pt), so
    pnl_gbp = net_points * stake  falls straight out in GBP and the
    £20 max risk (30pt * £0.67) is exact.
  - pnl_usd is the same P&L expressed in USD using the live GBPUSD rate,
    recorded for reporting alongside the USD entry/exit index levels.

All session times are UTC. Force close at 20:45 UTC -- never hold overnight.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("USHybrid.Strategy")

# ── Confirmed settings ────────────────────────────────────────────────────────

TRAILING_STOP_POINTS   = 30.0    # trailing stop in index points
TAKE_PROFIT_POINTS     = 45.0    # scalp target (System 4 Review, 18 Jul 2026): 200 was
                                 # never hit; ~46pt = 90th-pct observed move; 1.5:1 R:R.
                                 # Backtest-provisional, 4-week review.
STAKE_PER_POINT_GBP    = 0.67    # £0.67 per index point
MAX_RISK_PER_TRADE_GBP = 20.0    # 30pt * £0.67 ~= £20.10 (2% of £1000)
CAPITAL_SPREAD_POINTS  = 0.6     # Capital.com US500 real spread confirmed 0.6pt (18 Jul:
                                 # Sell 7,455.5 / Buy 7,456.1) -- was 0.4
FORCE_CLOSE_HOUR       = 20      # UTC
FORCE_CLOSE_MIN        = 45      # UTC -- 20:45 UTC hard close
DEFAULT_GBPUSD         = 1.27    # fallback if the live rate is unavailable


# ── Trade record ──────────────────────────────────────────────────────────────

# Recalibrated for the 30pt stop / 45pt target profile (System 4 Review, 18 Jul
# 2026) at £0.67/pt: Step1 ~12pt (developing), Step2 ~24pt (halfway), Step3 ~36pt
# (approaching target).
PROFIT_LADDER = [
    {"trigger_gbp": 8.00,  "floor_gbp": 6.00},
    {"trigger_gbp": 16.00, "floor_gbp": 13.00},
    {"trigger_gbp": 24.00, "floor_gbp": 20.00},
]


@dataclass
class USTrade:
    """
    A single US500 spread bet trade.
    Sizing: fixed £0.67/point (30pt stop => ~£20 risk).
    P&L: net_points * stake_per_point (GBP), converted to USD for reporting.
    """
    direction:     str
    entry_price:   float          # USD index level
    stop_pts:      float = TRAILING_STOP_POINTS
    entry_time:    object = field(default=None)
    session_phase: str    = field(default="")
    gbpusd_rate:   float  = DEFAULT_GBPUSD

    def __post_init__(self):
        self.stake       = STAKE_PER_POINT_GBP
        self.trail_best  = self.entry_price
        if self.direction == "LONG":
            self.stop_loss   = self.entry_price - self.stop_pts
            self.take_profit = self.entry_price + TAKE_PROFIT_POINTS
        else:
            self.stop_loss   = self.entry_price + self.stop_pts
            self.take_profit = self.entry_price - TAKE_PROFIT_POINTS
        self.exit_price  = None
        self.exit_time   = None
        self.exit_reason = None
        self.pnl_pts     = None
        self.pnl_gbp     = None
        self.pnl_usd     = None
        if self.entry_time is None:
            self.entry_time = datetime.now(timezone.utc)

    def apply_profit_ladder(self, price: float):
        """Profit Protection Ladder (Variant 2). Tighten the stop to guarantee a
        minimum GBP floor as floating profit builds -- additive to the trailing stop,
        only ever tightens, never triggers on a floating loss. The trailing stop and
        the force close are unaffected (whichever stop is tighter wins). Idempotent,
        so it re-applies correctly on restart. Returns a dict describing a NEWLY
        triggered rung (for logging), else None."""
        if not PROFIT_LADDER or self.stake <= 0:
            return None
        if not hasattr(self, "ladder_step"):     # lazy init (also survives reload)
            self.ladder_step, self.ladder_floor_gbp = 0, 0.0
        pts = (price - self.entry_price) if self.direction == "LONG" else (self.entry_price - price)
        float_gbp = pts * self.stake
        if float_gbp <= 0:                       # never engage on a floating loss
            return None
        idx, floor = 0, 0.0
        for i, s in enumerate(PROFIT_LADDER, start=1):
            if float_gbp >= s["trigger_gbp"]:
                idx, floor = i, s["floor_gbp"]
        if idx == 0:
            return None
        new_rung = idx > self.ladder_step
        if new_rung:
            self.ladder_step = idx
            self.ladder_floor_gbp = floor
        if self.ladder_floor_gbp <= 0:
            return None
        floor_pts = self.ladder_floor_gbp / self.stake
        stop_before = self.stop_loss
        if self.direction == "LONG":
            floor_stop = round(self.entry_price + floor_pts, 2)
            if floor_stop > self.stop_loss:      # tighten only
                self.stop_loss = floor_stop
        else:
            floor_stop = round(self.entry_price - floor_pts, 2)
            if floor_stop < self.stop_loss:      # tighten only
                self.stop_loss = floor_stop
        if new_rung:
            log.info("  PROFIT LADDER step %d: float GBP %.2f -> floor GBP %.2f | stop %.2f -> %.2f",
                     idx, float_gbp, self.ladder_floor_gbp, stop_before, self.stop_loss)
            return {"step": idx, "floor_gbp": self.ladder_floor_gbp,
                    "trigger_float_gbp": round(float_gbp, 2),
                    "stop_before": round(stop_before, 2), "stop_after": self.stop_loss}
        return None

    def update_trailing_stop(self, price: float) -> bool:
        """Move stop in favour of the trade as price moves our way."""
        if self.direction == "LONG" and price > self.trail_best:
            self.trail_best = price
            new_sl = price - self.stop_pts
            if new_sl > self.stop_loss:
                self.stop_loss = new_sl
                log.info("  Trailing stop moved UP to %.1f (price=%.1f)", self.stop_loss, price)
                return True
        elif self.direction == "SHORT" and price < self.trail_best:
            self.trail_best = price
            new_sl = price + self.stop_pts
            if new_sl < self.stop_loss:
                self.stop_loss = new_sl
                log.info("  Trailing stop moved DOWN to %.1f (price=%.1f)", self.stop_loss, price)
                return True
        return False

    def check_exit(self, price: float) -> Optional[str]:
        """Check stop loss and take profit. Returns exit reason or None."""
        if self.direction == "LONG":
            if price <= self.stop_loss:   return "STOP_LOSS"
            if price >= self.take_profit: return "TAKE_PROFIT"
        else:
            if price >= self.stop_loss:   return "STOP_LOSS"
            if price <= self.take_profit: return "TAKE_PROFIT"
        return None

    def close(self, price: float, reason: str, gbpusd_rate: Optional[float] = None) -> None:
        """Record exit. P&L is net of the Capital.com spread cost."""
        self.exit_price  = price
        self.exit_time   = datetime.now(timezone.utc)
        self.exit_reason = reason
        if gbpusd_rate and gbpusd_rate > 0:
            self.gbpusd_rate = gbpusd_rate
        raw_pts       = (price - self.entry_price) if self.direction == "LONG" else (self.entry_price - price)
        self.pnl_pts  = raw_pts - CAPITAL_SPREAD_POINTS
        self.pnl_gbp  = round(self.pnl_pts * self.stake, 2)
        self.pnl_usd  = round(self.pnl_gbp * self.gbpusd_rate, 2)

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def points_from_entry(self) -> Optional[float]:
        """Signed points moved from entry (positive = our favour)."""
        if not self.is_open:
            return self.pnl_pts
        return None

    def summary(self) -> str:
        if self.is_open:
            return (
                f"[OPEN {self.direction}] entry={self.entry_price:.1f} "
                f"stop={self.stop_loss:.1f} target={self.take_profit:.1f} "
                f"stake=£{self.stake:.2f}/pt"
            )
        sign = "WIN " if (self.pnl_gbp or 0) >= 0 else "LOSS"
        return (
            f"[{sign} {self.direction}] "
            f"entry={self.entry_price:.1f} exit={self.exit_price:.1f} "
            f"pts={self.pnl_pts:+.1f} P&L=£{self.pnl_gbp:+.2f} (${self.pnl_usd:+.2f}) "
            f"reason={self.exit_reason}"
        )


# ── Strategy helpers ──────────────────────────────────────────────────────────

def calculate_stake(stop_distance: float = TRAILING_STOP_POINTS) -> float:
    """Return stake per point. Fixed at £0.67/pt for US500."""
    return STAKE_PER_POINT_GBP


def calculate_stop_loss(entry_price: float, direction: str,
                        stop_pts: float = TRAILING_STOP_POINTS) -> float:
    if direction == "LONG":
        return entry_price - stop_pts
    return entry_price + stop_pts


def calculate_take_profit(entry_price: float, direction: str,
                          tp_pts: float = TAKE_PROFIT_POINTS) -> float:
    if direction == "LONG":
        return entry_price + tp_pts
    return entry_price - tp_pts


def should_force_close(ts_utc=None) -> bool:
    """Return True at 20:45 UTC or later -- force close all positions."""
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    ts_utc = ts_utc.astimezone(timezone.utc)
    return (ts_utc.hour == FORCE_CLOSE_HOUR and ts_utc.minute >= FORCE_CLOSE_MIN) or ts_utc.hour > FORCE_CLOSE_HOUR


def open_trade(direction: str, price: float, session_phase: str = "",
               stop_pts: float = TRAILING_STOP_POINTS,
               gbpusd_rate: float = DEFAULT_GBPUSD) -> USTrade:
    """Create and log a new USTrade."""
    trade = USTrade(
        direction     = direction,
        entry_price   = price,
        stop_pts      = stop_pts,
        entry_time    = datetime.now(timezone.utc),
        session_phase = session_phase,
        gbpusd_rate   = gbpusd_rate,
    )
    log.info(
        ">>> TRADE OPENED | %s | entry=%.1f | stake=£%.2f/pt | "
        "stop=%.1f | target=%.1f | session=%s",
        direction, price, trade.stake,
        trade.stop_loss, trade.take_profit, session_phase,
    )
    return trade


def close_trade(trade: USTrade, price: float, reason: str,
                gbpusd_rate: Optional[float] = None) -> USTrade:
    """Close a trade and log the result."""
    trade.close(price, reason, gbpusd_rate)
    sign = "WIN " if trade.pnl_gbp >= 0 else "LOSS"
    log.info(
        "<<< TRADE CLOSED | [%s %s] | pts=%+.1f | P&L=£%+.2f ($%+.2f) | reason=%s",
        sign, trade.direction, trade.pnl_pts, trade.pnl_gbp, trade.pnl_usd, reason,
    )
    return trade


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Strategy self-test (US500)")
    t = open_trade("LONG", 7500.0, "CORE", gbpusd_rate=1.27)
    log.info("%s", t.summary())
    log.info("Stake: £%.2f/pt | Max risk: £%.2f", t.stake, t.stake * t.stop_pts)
    t.update_trailing_stop(7540.0)
    log.info("After +40pt move: stop=%.1f", t.stop_loss)
    exit_reason = t.check_exit(7509.0)
    log.info("Check exit at 7509: %s", exit_reason)
    close_trade(t, 7510.0, "TRAIL_STOP", gbpusd_rate=1.27)
    log.info("%s", t.summary())
    log.info("Force close at 20:45 UTC: %s", should_force_close())
