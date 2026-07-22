"""
USHybrid AI -- backtest_us.py  (Backtest Bench)

S&P 500 (US500) spread betting backtest engine. Validates the LIVE entry gate
(Lancelot / pre_checks_us) against real Yahoo Finance history and specifically
answers one question: does relaxing the 1-hour RSI veto (Fix 3) help?

Strategy  : SSL Cloud + RSI + MACD + TMO + Chande MO + Money Flow
Timeframes: Daily (trend filter) + 1h (confirmation + RSI veto) + 5m (entry timing)
Market    : S&P 500, Yahoo ticker ^GSPC (USHybrid's live Yahoo fallback US_TICKER)

US500 mechanics (matched to strategy_us / pre_checks_us):
  Spread betting P&L = points_moved x stake_per_point
  Stake per point    = GBP 0.67/pt  (fixed; 30pt stop => ~GBP 20 risk = 2% of GBP 1000)
  Trailing stop      = 30 points
  Take-profit ceiling= 200 points
  Spread cost        = 0.6 pt/trade (constant across configs, for realism only)
  No overnight       = force close at 20:45 UTC

Session model (LIVE boundaries from data_feed_us.get_session_phase, all UTC):
  PRE_MARKET 13:30-14:30  -- warming up, no entries
  US_OPEN    14:30-14:45  -- opening volatility, no entries
  CORE       14:45-20:00  -- main trading window (entries allowed here)
  LATE       20:00-20:45  -- manage open trades only, no new entries
  CLOSED     otherwise    -- force close at 20:45

The two configurations differ ONLY in the 1h RSI veto thresholds:
  BASELINE (live) : LONG needs 1h RSI >= 55,  SHORT needs 1h RSI <= 45
  RELAXED  (Fix 3): LONG needs 1h RSI >= 52,  SHORT needs 1h RSI <= 48

Output:
  logs/backtest_us_results.txt  -- full side-by-side detail + verdict
  logs/backtest_us_trades.csv   -- every trade across both configs
"""

import ast
import csv
import logging
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("USHybrid.Backtest")

# ── Constants (matched to live strategy_us / pre_checks_us) ────────────────────

STARTING_CAPITAL_GBP = 1000.0
STAKE_PER_POINT_GBP  = 0.67      # fixed GBP/pt stake (30pt stop => ~GBP 20 risk)
STOP_POINTS          = 30.0      # trailing stop distance in index points
TP_POINTS            = 200.0     # take-profit ceiling in points
SPREAD_COST_PTS      = 0.6       # constant per-trade spread cost (realism only)
LOGS_DIR             = Path(__file__).parent / "logs"

US_TICKER = "^GSPC"              # Yahoo Finance S&P 500 (USHybrid live fallback)

# Live entry-gate momentum / choppiness thresholds (mirror pre_checks_us)
MIN_TMO_FOR_ENTRY       = 0.3
CHOPPY_RSI_THRESHOLD    = 5.0
CHOPPY_TMO_THRESHOLD    = 0.5
CHOPPY_SIGNALS_REQUIRED = 2

# ── Session phase constants (mirror data_feed_us, all UTC) ─────────────────────

PRE_MARKET = "PRE_MARKET"
US_OPEN    = "US_OPEN"
CORE       = "CORE"
LATE       = "LATE"
CLOSED     = "CLOSED"

_PRE_MARKET_START = 13 * 60 + 30   # 810
_US_OPEN_START    = 14 * 60 + 30   # 870
_CORE_START       = 14 * 60 + 45   # 885
_LATE_START       = 20 * 60        # 1200
_FORCE_CLOSE      = 20 * 60 + 45   # 1245  -- CLOSED begins; force close all

# Only CORE takes new entries (matches pre_checks_us intent: CORE tradeable,
# LATE manage-only, US_OPEN volatility block, near-close block after 20:45).
TRADEABLE_PHASES = {CORE}

# The two RSI-veto configurations under test.
CONFIGS = [
    # (name, long_rsi_min, short_rsi_max)
    ("BASELINE", 55.0, 45.0),
    ("RELAXED",  52.0, 48.0),
]


# ── Indicator functions (verbatim from data_feed_us / the FTSE template) ───────

def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _calc_macd(series: pd.Series,
               fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({
        "macd":      macd_line,
        "signal":    signal_line,
        "histogram": macd_line - signal_line,
    })


def _calc_ssl_cloud(df: pd.DataFrame, period: int = 10) -> pd.DataFrame:
    sma_high = df["high"].rolling(period).mean()
    sma_low  = df["low"].rolling(period).mean()
    hlv = pd.Series(
        np.where(df["close"] > sma_high, 1,
        np.where(df["close"] < sma_low,  -1, np.nan)),
        index=df.index,
    ).ffill()
    ssl_up   = np.where(hlv < 0, sma_low,  sma_high)
    ssl_down = np.where(hlv < 0, sma_high, sma_low)
    return pd.DataFrame({
        "ssl_up":   ssl_up,
        "ssl_down": ssl_down,
        "ssl_bull": ssl_up > ssl_down,
    }, index=df.index)


def _calc_tmo(df: pd.DataFrame, length: int = 14, calc_length: int = 5) -> pd.DataFrame:
    mom    = np.sign(df["close"] - df["open"]).rolling(length).sum()
    main   = mom.ewm(span=calc_length, adjust=False).mean()
    smooth = main.ewm(span=calc_length, adjust=False).mean()
    return pd.DataFrame({"tmo_main": main, "tmo_smooth": smooth}, index=df.index)


def _calc_chande(df: pd.DataFrame, period: int = 20) -> pd.Series:
    diff   = df["close"].diff()
    up_sum = diff.clip(lower=0).rolling(period).sum()
    dn_sum = (-diff.clip(upper=0)).rolling(period).sum()
    denom  = (up_sum + dn_sum).replace(0, np.nan)
    return (100 * (up_sum - dn_sum) / denom).rename("chande_mo")


def _calc_money_flow(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, np.nan)
    mfv = tp * vol * np.sign(df["close"] - df["open"])
    return (mfv.rolling(period).sum() / vol.rolling(period).sum().replace(0, np.nan)
            ).rename("money_flow")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate all 6 indicators on a full OHLCV DataFrame (verbatim suite)."""
    if df.empty:
        return df
    df = df.copy()
    df["rsi"]            = _calc_rsi(df["close"])
    macd_df              = _calc_macd(df["close"])
    df["macd"]           = macd_df["macd"]
    df["macd_signal"]    = macd_df["signal"]
    df["macd_histogram"] = macd_df["histogram"]
    ssl_df               = _calc_ssl_cloud(df)
    df["ssl_up"]         = ssl_df["ssl_up"]
    df["ssl_down"]       = ssl_df["ssl_down"]
    df["ssl_bull"]       = ssl_df["ssl_bull"]
    tmo_df               = _calc_tmo(df)
    df["tmo_main"]       = tmo_df["tmo_main"]
    df["tmo_smooth"]     = tmo_df["tmo_smooth"]
    df["chande_mo"]      = _calc_chande(df)
    df["money_flow"]     = _calc_money_flow(df)
    return df


# ── Session phase logic (mirror data_feed_us.get_session_phase, UTC) ───────────

def get_session_phase(ts_utc) -> str:
    """Return the US session phase for a UTC-aware pandas Timestamp."""
    if ts_utc.weekday() >= 5:
        return CLOSED
    t = ts_utc.hour * 60 + ts_utc.minute
    if   t < _PRE_MARKET_START: return CLOSED
    elif t < _US_OPEN_START:    return PRE_MARKET
    elif t < _CORE_START:       return US_OPEN
    elif t < _LATE_START:       return CORE
    elif t < _FORCE_CLOSE:      return LATE
    else:                       return CLOSED


def _is_force_close(ts_utc) -> bool:
    """Force close at/after 20:45 UTC (mirror strategy_us.should_force_close)."""
    t = ts_utc.hour * 60 + ts_utc.minute
    return t >= _FORCE_CLOSE


# ── Higher-timeframe lookup helpers ────────────────────────────────────────────

def _last_bar_at(df: pd.DataFrame, ts) -> Optional[pd.Series]:
    """Most recent completed higher-timeframe bar at or before ts (no lookahead)."""
    mask = df.index <= ts
    if not mask.any():
        return None
    return df[mask].iloc[-1]


# ── LIVE entry gate (faithful port of pre_checks_us quality checks) ────────────
#
# Live order for a new entry (run_all_pre_checks, quality section):
#   direction  = LONG if 1h ssl_bull else SHORT   (proposed_direction BOTH)
#   1. Daily trend filter (daily SSL bias)
#   2. SSL agreement (1h SSL == 5m SSL)
#   3. 1h RSI confirming        <-- the tested veto (thresholds parameterised)
#   4. 5m TMO momentum (|tmo| > 0.3 aligned)
#   5. Not choppy
#   6. Candle confirmed (5m green for LONG / red for SHORT)

def _candidate_direction(bar_1h: pd.Series) -> Optional[str]:
    ssl_1h = bar_1h.get("ssl_bull")
    if pd.isna(ssl_1h):
        return None
    return "LONG" if ssl_1h else "SHORT"


def _daily_trend_ok(bar_1d: Optional[pd.Series], direction: str) -> bool:
    if bar_1d is None:
        return True
    ssl_1d = bar_1d.get("ssl_bull")
    if pd.isna(ssl_1d):
        return True
    if ssl_1d and direction == "SHORT":
        return False
    if (not ssl_1d) and direction == "LONG":
        return False
    return True


def _ssl_agreement_ok(bar_1h: pd.Series, bar_5m: pd.Series) -> bool:
    ssl_1h = bar_1h.get("ssl_bull")
    ssl_5m = bar_5m.get("ssl_bull")
    if pd.isna(ssl_1h) or pd.isna(ssl_5m):
        return False
    return bool(ssl_1h) == bool(ssl_5m)


def _rsi_confirms(bar_1h: pd.Series, direction: str,
                  long_min: float, short_max: float) -> bool:
    """Parameterised 1h RSI veto. NaN passes (matches live)."""
    rsi_1h = bar_1h.get("rsi")
    if pd.isna(rsi_1h):
        return True
    if direction == "LONG":
        return rsi_1h >= long_min
    return rsi_1h <= short_max


def _tmo_momentum_ok(bar_1h: pd.Series, bar_5m: pd.Series) -> bool:
    ssl_bull = bar_1h.get("ssl_bull")
    tmo_5m   = bar_5m.get("tmo_main")
    if pd.isna(ssl_bull) or pd.isna(tmo_5m):
        return True
    if ssl_bull and tmo_5m < MIN_TMO_FOR_ENTRY:
        return False
    if (not ssl_bull) and tmo_5m > -MIN_TMO_FOR_ENTRY:
        return False
    return True


def _not_choppy(bar_1h: pd.Series, bar_5m: pd.Series) -> bool:
    choppy = 0
    rsi_5m = bar_5m.get("rsi")
    if pd.notna(rsi_5m) and abs(rsi_5m - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy += 1
    tmo_5m = bar_5m.get("tmo_main")
    if pd.notna(tmo_5m) and abs(tmo_5m) <= CHOPPY_TMO_THRESHOLD:
        choppy += 1
    rsi_1h = bar_1h.get("rsi")
    if pd.notna(rsi_1h) and abs(rsi_1h - 50) <= CHOPPY_RSI_THRESHOLD:
        choppy += 1
    return choppy < CHOPPY_SIGNALS_REQUIRED


def _candle_confirmed(bar_1h: pd.Series, bar_5m: pd.Series) -> bool:
    ssl_bull    = bar_1h.get("ssl_bull")
    open_price  = bar_5m.get("open")
    close_price = bar_5m.get("close")
    if pd.isna(ssl_bull) or pd.isna(open_price) or pd.isna(close_price):
        return True
    candle_green = close_price >= open_price
    if ssl_bull and not candle_green:
        return False
    if (not ssl_bull) and candle_green:
        return False
    return True


def evaluate_entry(bar_5m: pd.Series, bar_1h: Optional[pd.Series],
                   bar_1d: Optional[pd.Series],
                   long_min: float, short_max: float) -> Optional[str]:
    """
    Run the full live entry gate for one 5m bar. Returns the trade direction
    to open ("LONG"/"SHORT") or None if any check blocks entry.
    """
    if bar_1h is None:
        return None
    direction = _candidate_direction(bar_1h)
    if direction is None:
        return None
    if not _daily_trend_ok(bar_1d, direction):
        return None
    if not _ssl_agreement_ok(bar_1h, bar_5m):
        return None
    if not _rsi_confirms(bar_1h, direction, long_min, short_max):
        return None
    if not _tmo_momentum_ok(bar_1h, bar_5m):
        return None
    if not _not_choppy(bar_1h, bar_5m):
        return None
    if not _candle_confirmed(bar_1h, bar_5m):
        return None
    return direction


# ── Spread-bet trade record (mirror strategy_us.USTrade) ───────────────────────

@dataclass
class USBacktestTrade:
    direction:   str
    entry_price: float
    entry_time:  object = field(default=None)
    session:     str    = field(default="")

    def __post_init__(self):
        self.stop_pts   = STOP_POINTS
        self.stake      = STAKE_PER_POINT_GBP
        self.trail_best = self.entry_price
        if self.direction == "LONG":
            self.stop_loss   = self.entry_price - self.stop_pts
            self.take_profit = self.entry_price + TP_POINTS
        else:
            self.stop_loss   = self.entry_price + self.stop_pts
            self.take_profit = self.entry_price - TP_POINTS
        self.exit_price  = None
        self.exit_time   = None
        self.exit_reason = None
        self.pnl_pts     = None
        self.pnl_gbp     = None
        self._cost_gbp   = 0.0
        self._net_pnl    = 0.0
        self._peak_pts   = 0.0

    def update_trailing_stop(self, price: float) -> None:
        if self.direction == "LONG" and price > self.trail_best:
            self.trail_best = price
            new_sl = price - self.stop_pts
            if new_sl > self.stop_loss:
                self.stop_loss = new_sl
        elif self.direction == "SHORT" and price < self.trail_best:
            self.trail_best = price
            new_sl = price + self.stop_pts
            if new_sl < self.stop_loss:
                self.stop_loss = new_sl

    def close(self, price: float, reason: str) -> None:
        self.exit_price  = price
        self.exit_reason = reason
        self.pnl_pts     = (price - self.entry_price) if self.direction == "LONG" \
                           else (self.entry_price - price)
        self.pnl_gbp     = self.pnl_pts * self.stake
        self._cost_gbp   = SPREAD_COST_PTS * self.stake
        self._net_pnl    = self.pnl_gbp - self._cost_gbp


# ── Yahoo Finance data fetching ────────────────────────────────────────────────

def _yf_download(ticker: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(ticker, period=period, interval=interval,
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0].lower() for col in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={"adj close": "close"})
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = df["volume"].fillna(0.0)
    df.sort_index(inplace=True)
    return df.dropna(subset=["close"])


def fetch_historical_data() -> tuple:
    """Fetch and enrich all three timeframes. Returns (df_1d, df_1h, df_5m)."""
    log.info("Fetching S&P 500 (^GSPC) history from Yahoo Finance...")

    log.info("  ^GSPC 1d (2y)...")
    df_1d = add_indicators(_yf_download(US_TICKER, "1d", "2y"))
    if df_1d.empty:
        raise RuntimeError("No 1d data returned for ^GSPC")
    log.info("    %d candles  [%s -> %s]", len(df_1d),
             df_1d.index[0].strftime("%Y-%m-%d"),
             df_1d.index[-1].strftime("%Y-%m-%d"))

    log.info("  ^GSPC 1h (90d)...")
    df_1h = add_indicators(_yf_download(US_TICKER, "1h", "90d"))
    if df_1h.empty:
        raise RuntimeError("No 1h data returned for ^GSPC")
    log.info("    %d candles  [%s -> %s]", len(df_1h),
             df_1h.index[0].strftime("%Y-%m-%d %H:%M"),
             df_1h.index[-1].strftime("%Y-%m-%d %H:%M"))

    log.info("  ^GSPC 5m (60d)...")
    df_5m = _yf_download(US_TICKER, "5m", "60d")
    if len(df_5m) < 200:
        log.warning("    Only %d 5m bars -- trying 15m fallback...", len(df_5m))
        df_5m = _yf_download(US_TICKER, "15m", "60d")
    if df_5m.empty:
        raise RuntimeError("No intraday (5m/15m) data returned for ^GSPC")
    df_5m = add_indicators(df_5m)
    log.info("    %d candles  [%s -> %s]", len(df_5m),
             df_5m.index[0].strftime("%Y-%m-%d %H:%M"),
             df_5m.index[-1].strftime("%Y-%m-%d %H:%M"))

    return df_1d, df_1h, df_5m


# ── Single backtest run (one RSI-veto configuration) ───────────────────────────

def run_single_backtest(name: str, long_min: float, short_max: float,
                        df_1d: pd.DataFrame, df_1h: pd.DataFrame,
                        df_5m: pd.DataFrame) -> tuple:
    """Replay df_5m bar by bar under one RSI config. Returns (stats, trades)."""
    log.info("")
    log.info("=" * 60)
    log.info("  %s  |  LONG 1h RSI >= %.0f  |  SHORT 1h RSI <= %.0f",
             name, long_min, short_max)
    log.info("=" * 60)

    capital       = STARTING_CAPITAL_GBP
    equity_peak   = capital
    max_dd_gbp    = 0.0
    current_trade: Optional[USBacktestTrade] = None
    completed     = []

    for ts, bar in df_5m.iterrows():
        phase = get_session_phase(ts)

        # ── Force close at/after 20:45 UTC (never hold overnight) ──────────────
        if current_trade is not None and _is_force_close(ts):
            current_trade.close(float(bar["close"]), "SESSION_CLOSE")
            current_trade.exit_time = ts
            capital += current_trade._net_pnl
            completed.append(current_trade)
            current_trade = None
            equity_peak = max(equity_peak, capital)
            max_dd_gbp  = max(max_dd_gbp, equity_peak - capital)
            continue

        # ── Manage an open trade: trailing stop, SL/TP ─────────────────────────
        if current_trade is not None:
            high, low = float(bar["high"]), float(bar["low"])
            exit_reason = exit_px = None
            if current_trade.direction == "LONG":
                current_trade.update_trailing_stop(high)
                current_trade._peak_pts = max(current_trade._peak_pts,
                                              high - current_trade.entry_price)
                if low <= current_trade.stop_loss:
                    exit_reason, exit_px = "STOP_LOSS", current_trade.stop_loss
                elif high >= current_trade.take_profit:
                    exit_reason, exit_px = "TAKE_PROFIT", current_trade.take_profit
            else:
                current_trade.update_trailing_stop(low)
                current_trade._peak_pts = max(current_trade._peak_pts,
                                              current_trade.entry_price - low)
                if high >= current_trade.stop_loss:
                    exit_reason, exit_px = "STOP_LOSS", current_trade.stop_loss
                elif low <= current_trade.take_profit:
                    exit_reason, exit_px = "TAKE_PROFIT", current_trade.take_profit

            if exit_reason:
                current_trade.close(exit_px, exit_reason)
                current_trade.exit_time = ts
                capital += current_trade._net_pnl
                completed.append(current_trade)
                current_trade = None
                equity_peak = max(equity_peak, capital)
                max_dd_gbp  = max(max_dd_gbp, equity_peak - capital)
            continue   # never seek entries while a trade is active

        # ── Entry gate (CORE only) ─────────────────────────────────────────────
        if phase not in TRADEABLE_PHASES:
            continue

        bar_1h = _last_bar_at(df_1h, ts)
        bar_1d = _last_bar_at(df_1d, ts)
        direction = evaluate_entry(bar, bar_1h, bar_1d, long_min, short_max)
        if direction is None:
            continue

        current_trade = USBacktestTrade(
            direction   = direction,
            entry_price = float(bar["close"]),
            entry_time  = ts,
            session     = phase,
        )

    # ── Close any trade still open at end of data ──────────────────────────────
    if current_trade is not None:
        current_trade.close(float(df_5m.iloc[-1]["close"]), "BACKTEST_END")
        current_trade.exit_time = df_5m.index[-1]
        capital += current_trade._net_pnl
        completed.append(current_trade)
        equity_peak = max(equity_peak, capital)
        max_dd_gbp  = max(max_dd_gbp, equity_peak - capital)

    stats = _compute_stats(name, long_min, short_max, completed,
                           capital, max_dd_gbp, df_5m)
    log.info("  %s done: %d trades | win %.1f%% | net GBP %+.2f",
             name, stats["total"], stats["win_rate"], stats["net_pnl"])
    return stats, completed


# ── Statistics ─────────────────────────────────────────────────────────────────

def _dir_stats(subset: list) -> dict:
    wins = [t for t in subset if t._net_pnl > 0]
    return {
        "count":    len(subset),
        "wins":     len(wins),
        "win_rate": len(wins) / len(subset) * 100 if subset else 0.0,
        "net_pnl":  sum(t._net_pnl for t in subset),
    }


def _compute_stats(name: str, long_min: float, short_max: float,
                   completed: list, final_capital: float,
                   max_dd_gbp: float, df_5m: pd.DataFrame) -> dict:
    total  = len(completed)
    wins   = [t for t in completed if t._net_pnl > 0]
    losses = [t for t in completed if t._net_pnl <= 0]

    net_pnl   = sum(t._net_pnl for t in completed)
    win_rate  = len(wins) / total * 100 if total else 0.0
    avg_win   = sum(t._net_pnl for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss  = sum(t._net_pnl for t in losses) / len(losses) if losses else 0.0

    # Max consecutive losing trades
    max_consec = streak = 0
    for t in completed:
        if t._net_pnl <= 0:
            streak += 1
            max_consec = max(max_consec, streak)
        else:
            streak = 0

    exit_counts: dict = defaultdict(int)
    for t in completed:
        exit_counts[t.exit_reason or "UNKNOWN"] += 1

    by_session = {
        ph: _dir_stats([t for t in completed if t.session == ph])
        for ph in (CORE, LATE)
    }

    period_days = max((df_5m.index[-1] - df_5m.index[0]).days, 1)

    return {
        "name":            name,
        "long_min":        long_min,
        "short_max":       short_max,
        "total":           total,
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        win_rate,
        "net_pnl":         net_pnl,
        "final_capital":   final_capital,
        "avg_win":         avg_win,
        "avg_loss":        avg_loss,
        "max_consec_loss": max_consec,
        "max_dd_gbp":      max_dd_gbp,
        "exit_counts":     dict(exit_counts),
        "long":            _dir_stats([t for t in completed if t.direction == "LONG"]),
        "short":           _dir_stats([t for t in completed if t.direction == "SHORT"]),
        "by_session":      by_session,
        "period_days":     period_days,
    }


# ── Verdict ────────────────────────────────────────────────────────────────────

def decide_verdict(baseline: dict, relaxed: dict) -> tuple:
    """
    IMPLEMENT if RELAXED win rate >= 50% AND trade count increases
    AND no significant drawdown increase; else DEFER.

    'No significant drawdown increase' = peak-to-trough GBP drawdown does not
    worsen by more than 20%, and max consecutive losses rise by at most 1.
    """
    wr_ok      = relaxed["win_rate"] >= 50.0
    more_trades = relaxed["total"] > baseline["total"]

    base_dd = baseline["max_dd_gbp"]
    dd_limit = base_dd * 1.20 if base_dd > 0 else 1e-9 + baseline["max_dd_gbp"]
    dd_ok = (relaxed["max_dd_gbp"] <= max(dd_limit, base_dd + 1e-9)) and \
            (relaxed["max_consec_loss"] <= baseline["max_consec_loss"] + 1)

    implement = wr_ok and more_trades and dd_ok
    reasons = [
        f"RELAXED win rate {relaxed['win_rate']:.1f}% "
        f"{'>=' if wr_ok else '<'} 50%  [{'PASS' if wr_ok else 'FAIL'}]",
        f"Trade count {baseline['total']} -> {relaxed['total']} "
        f"({'increases' if more_trades else 'does NOT increase'})  "
        f"[{'PASS' if more_trades else 'FAIL'}]",
        f"Max drawdown GBP {base_dd:.2f} -> GBP {relaxed['max_dd_gbp']:.2f}, "
        f"consec loss {baseline['max_consec_loss']} -> {relaxed['max_consec_loss']} "
        f"[{'PASS' if dd_ok else 'FAIL'}]",
    ]
    return implement, reasons


# ── Output: side-by-side table ─────────────────────────────────────────────────

def _fmt_row(label: str, a, b, width: int = 22) -> str:
    return f"  {label:<30} {str(a):>{width}} {str(b):>{width}}"


def build_comparison_lines(baseline: dict, relaxed: dict,
                           implement: bool, reasons: list,
                           df_5m: pd.DataFrame) -> list:
    p_start = df_5m.index[0].strftime("%Y-%m-%d")
    p_end   = df_5m.index[-1].strftime("%Y-%m-%d")
    p_days  = max((df_5m.index[-1] - df_5m.index[0]).days, 1)
    sep = "=" * 76

    L = []
    L.append(sep)
    L.append("  USHYBRID AI -- S&P 500 BACKTEST  |  1h RSI VETO: BASELINE vs RELAXED")
    L.append(f"  Data    : ^GSPC 5m  ({p_start} to {p_end}, {p_days} days)")
    L.append(f"  Account : GBP {STARTING_CAPITAL_GBP:.0f}  |  stake GBP {STAKE_PER_POINT_GBP:.2f}/pt"
             f"  |  stop {STOP_POINTS:.0f}pt  |  TP {TP_POINTS:.0f}pt"
             f"  |  spread {SPREAD_COST_PTS:.1f}pt")
    L.append(f"  Entries : CORE only (14:45-20:00 UTC) | force close 20:45 UTC")
    L.append(sep)
    L.append(_fmt_row("", "BASELINE (55/45)", "RELAXED (52/48)"))
    L.append("  " + "-" * 74)
    L.append(_fmt_row("Total trades", baseline["total"], relaxed["total"]))
    L.append(_fmt_row("Overall win rate",
                      f"{baseline['win_rate']:.1f}%", f"{relaxed['win_rate']:.1f}%"))
    L.append(_fmt_row("  LONG trades / win rate",
                      f"{baseline['long']['count']} / {baseline['long']['win_rate']:.1f}%",
                      f"{relaxed['long']['count']} / {relaxed['long']['win_rate']:.1f}%"))
    L.append(_fmt_row("  SHORT trades / win rate",
                      f"{baseline['short']['count']} / {baseline['short']['win_rate']:.1f}%",
                      f"{relaxed['short']['count']} / {relaxed['short']['win_rate']:.1f}%"))
    for ph in (CORE, LATE):
        b = baseline["by_session"][ph]
        r = relaxed["by_session"][ph]
        L.append(_fmt_row(f"  {ph} trades / win rate",
                          f"{b['count']} / {b['win_rate']:.1f}%",
                          f"{r['count']} / {r['win_rate']:.1f}%"))
    L.append(_fmt_row("Net P&L",
                      f"GBP {baseline['net_pnl']:+.2f}", f"GBP {relaxed['net_pnl']:+.2f}"))
    L.append(_fmt_row("Final capital",
                      f"GBP {baseline['final_capital']:.2f}",
                      f"GBP {relaxed['final_capital']:.2f}"))
    L.append(_fmt_row("Avg winner",
                      f"GBP {baseline['avg_win']:+.2f}", f"GBP {relaxed['avg_win']:+.2f}"))
    L.append(_fmt_row("Avg loser",
                      f"GBP {baseline['avg_loss']:+.2f}", f"GBP {relaxed['avg_loss']:+.2f}"))
    L.append(_fmt_row("Max consecutive losses",
                      baseline["max_consec_loss"], relaxed["max_consec_loss"]))
    L.append(_fmt_row("Max drawdown (peak-trough)",
                      f"GBP {baseline['max_dd_gbp']:.2f}", f"GBP {relaxed['max_dd_gbp']:.2f}"))
    L.append(_fmt_row("Exits SL / TP / close",
                      f"{baseline['exit_counts'].get('STOP_LOSS',0)}"
                      f" / {baseline['exit_counts'].get('TAKE_PROFIT',0)}"
                      f" / {baseline['exit_counts'].get('SESSION_CLOSE',0)}",
                      f"{relaxed['exit_counts'].get('STOP_LOSS',0)}"
                      f" / {relaxed['exit_counts'].get('TAKE_PROFIT',0)}"
                      f" / {relaxed['exit_counts'].get('SESSION_CLOSE',0)}"))
    L.append(sep)
    L.append("  VERDICT RULE: IMPLEMENT if RELAXED win rate >= 50% AND trades increase")
    L.append("               AND no significant drawdown increase; else DEFER.")
    L.append("  " + "-" * 74)
    for r in reasons:
        L.append(f"    - {r}")
    L.append("  " + "-" * 74)
    L.append(f"  >>> VERDICT: {'IMPLEMENT' if implement else 'DEFER'} Fix 3 "
             f"(relax 1h RSI veto 55->52 / 45->48)")
    L.append(sep)
    return L


# ── Output: CSV + results text ─────────────────────────────────────────────────

def save_trades_csv(all_runs: list) -> None:
    path = LOGS_DIR / "backtest_us_trades.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "config", "long_rsi_min", "short_rsi_max", "trade_no",
            "direction", "session", "entry_time", "exit_time",
            "entry_px", "exit_px", "pnl_pts", "stake_gbp_per_pt",
            "gross_pnl_gbp", "cost_gbp", "net_pnl_gbp", "peak_pts", "exit_reason",
        ])
        for stats, trades in all_runs:
            for i, t in enumerate(trades, 1):
                w.writerow([
                    stats["name"], f"{stats['long_min']:.0f}", f"{stats['short_max']:.0f}",
                    i, t.direction, t.session,
                    t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "",
                    t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "",
                    f"{t.entry_price:.1f}",
                    f"{t.exit_price:.1f}" if t.exit_price is not None else "",
                    f"{t.pnl_pts:.1f}" if t.pnl_pts is not None else "",
                    f"{t.stake:.4f}",
                    f"{t.pnl_gbp:.2f}" if t.pnl_gbp is not None else "",
                    f"{t._cost_gbp:.4f}", f"{t._net_pnl:.2f}",
                    f"{t._peak_pts:.1f}", t.exit_reason or "",
                ])
    log.info("Trades CSV saved -> %s", path)


def save_results_txt(lines: list) -> None:
    path    = LOGS_DIR / "backtest_us_results.txt"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header  = [f"  Generated: {now_str}", ""]
    path.write_text("\n".join(header + lines), encoding="utf-8")
    log.info("Results text saved -> %s", path)


# ── Entry point ────────────────────────────────────────────────────────────────

def run_backtest() -> None:
    LOGS_DIR.mkdir(exist_ok=True)

    log.info("USHybrid AI -- S&P 500 Backtest (BASELINE vs RELAXED 1h RSI veto)")
    log.info("Account: GBP %.0f | stake GBP %.2f/pt | stop %.0fpt | TP %.0fpt | spread %.1fpt",
             STARTING_CAPITAL_GBP, STAKE_PER_POINT_GBP, STOP_POINTS, TP_POINTS, SPREAD_COST_PTS)

    df_1d, df_1h, df_5m = fetch_historical_data()

    all_runs = []
    for name, long_min, short_max in CONFIGS:
        stats, trades = run_single_backtest(name, long_min, short_max,
                                            df_1d, df_1h, df_5m)
        all_runs.append((stats, trades))

    baseline = next(s for s, _ in all_runs if s["name"] == "BASELINE")
    relaxed  = next(s for s, _ in all_runs if s["name"] == "RELAXED")

    implement, reasons = decide_verdict(baseline, relaxed)
    lines = build_comparison_lines(baseline, relaxed, implement, reasons, df_5m)

    save_trades_csv(all_runs)
    save_results_txt(lines)

    print()
    for ln in lines:
        print(ln)

    log.info("")
    log.info("Backtest complete.")
    log.info("  logs/backtest_us_results.txt")
    log.info("  logs/backtest_us_trades.csv")


if __name__ == "__main__":
    run_backtest()
