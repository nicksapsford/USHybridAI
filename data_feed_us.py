"""
USHybrid AI -- data_feed_us.py  (Merlin)
US S&P 500 three-timeframe data feed with full indicator suite.
Primary: Capital.com API (epic US500). Fallback: Yahoo Finance (^GSPC).
Timeframes: 1d (daily trend), 1h (confirmation), 5m (entry timing).

All session boundaries are in UTC -- the US cash session runs 14:30-21:00 UTC.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("USHybrid.Merlin")

US_EPIC     = "US500"    # Capital.com epic for the S&P 500 (verified against demo API 2026-07-07)
US_TICKER   = "^GSPC"    # Yahoo Finance ticker for the S&P 500
RATE_LIMIT_S = 1.0
YF_TIMEOUT_S = 15        # hard timeout for any single Yahoo Finance download

# Session phase constants (all UTC)
PRE_MARKET = "PRE_MARKET"   # 13:30-14:30 UTC -- warming up, no entries
US_OPEN    = "US_OPEN"      # 14:30-14:45 UTC -- opening volatility, no entries
CORE       = "CORE"         # 14:45-20:00 UTC -- main trading window
LATE       = "LATE"         # 20:00-20:45 UTC -- no new entries, manage only
CLOSED     = "CLOSED"       # 20:45-13:30 next day + weekends

# Minute-of-day boundaries (UTC)
_PRE_MARKET_START = 13 * 60 + 30   # 810
_US_OPEN_START    = 14 * 60 + 30   # 870
_CORE_START       = 14 * 60 + 45   # 885
_LATE_START       = 20 * 60        # 1200
_FORCE_CLOSE      = 20 * 60 + 45   # 1245  -- CLOSED begins here
_SESSION_END      = 21 * 60        # 1260  -- US cash close


# ── Session phase logic ───────────────────────────────────────────────────────

def get_session_phase(ts_utc: Optional[datetime] = None) -> str:
    """
    Return the current US market session phase (UTC based).
    PRE_MARKET: 13:30-14:30 UTC
    US_OPEN:    14:30-14:45 UTC  (volatile -- no entries)
    CORE:       14:45-20:00 UTC  (main trading window)
    LATE:       20:00-20:45 UTC  (no new entries)
    CLOSED:     20:45-13:30 UTC next day, and weekends
    """
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    ts_utc = ts_utc.astimezone(timezone.utc)

    if ts_utc.weekday() >= 5:
        return CLOSED

    t = ts_utc.hour * 60 + ts_utc.minute
    if t < _PRE_MARKET_START:  return CLOSED
    elif t < _US_OPEN_START:   return PRE_MARKET
    elif t < _CORE_START:      return US_OPEN
    elif t < _LATE_START:      return CORE
    elif t < _FORCE_CLOSE:     return LATE
    else:                      return CLOSED


def is_market_open(ts_utc: Optional[datetime] = None) -> bool:
    """
    True during US_OPEN, CORE and LATE -- the phases where the 5-minute candle
    loop should run. New entries are gated more tightly by Lancelot (CORE only).
    """
    return get_session_phase(ts_utc) in (US_OPEN, CORE, LATE)


def minutes_until_next_open(ts_utc: Optional[datetime] = None) -> int:
    """Return minutes until the next US open (14:30 UTC)."""
    if ts_utc is None:
        ts_utc = datetime.now(timezone.utc)
    ts_utc = ts_utc.astimezone(timezone.utc)

    if ts_utc.weekday() >= 5:
        days_until_monday = 7 - ts_utc.weekday()
        next_open = ts_utc.replace(hour=14, minute=30, second=0, microsecond=0) + timedelta(days=days_until_monday)
    else:
        t = ts_utc.hour * 60 + ts_utc.minute
        if t < _US_OPEN_START:
            next_open = ts_utc.replace(hour=14, minute=30, second=0, microsecond=0)
        elif t < _SESSION_END:
            return 0
        else:
            days = 3 if ts_utc.weekday() == 4 else 1
            next_open = (ts_utc + timedelta(days=days)).replace(hour=14, minute=30, second=0, microsecond=0)

    delta = next_open - ts_utc
    return max(0, int(delta.total_seconds() / 60))


# ── Indicator calculations (identical across the Blackpool Trading Desk suite) ─

def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _calc_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
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
    hlv      = pd.Series(
        np.where(df["close"] > sma_high, 1,
        np.where(df["close"] < sma_low, -1, np.nan)),
        index=df.index,
    ).ffill()
    ssl_up   = np.where(hlv < 0, sma_low,  sma_high)
    ssl_down = np.where(hlv < 0, sma_high, sma_low)
    return pd.DataFrame({
        "ssl_up":   ssl_up,
        "ssl_down": ssl_down,
        "ssl_bull": ssl_up > ssl_down,
    }, index=df.index)


def _calc_tmo(
    df: pd.DataFrame,
    length: int = 14,
    calc_length: int = 5,
) -> pd.DataFrame:
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
    return (mfv.rolling(period).sum() / vol.rolling(period).sum()).rename("money_flow")


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Apply all 6 indicators to a OHLCV DataFrame. Returns enriched copy."""
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


def get_composite_signal(row: pd.Series) -> str:
    """Return LONG, SHORT, or NEUTRAL for a single bar."""
    signals = []
    if pd.notna(row.get("ssl_bull")):
        signals.append(1 if row["ssl_bull"] else -1)
    rsi = row.get("rsi")
    if pd.notna(rsi):
        signals.append(1 if rsi > 55 else (-1 if rsi < 45 else 0))
    hist = row.get("macd_histogram")
    if pd.notna(hist):
        signals.append(1 if hist > 0 else -1)
    tmo_main, tmo_smooth = row.get("tmo_main"), row.get("tmo_smooth")
    if pd.notna(tmo_main) and pd.notna(tmo_smooth):
        signals.append(1 if tmo_main > tmo_smooth else -1)
    cmo = row.get("chande_mo")
    if pd.notna(cmo):
        signals.append(1 if cmo > 0 else -1)
    mf = row.get("money_flow")
    if pd.notna(mf):
        signals.append(1 if mf > 0 else -1)
    if not signals:
        return "NEUTRAL"
    score = sum(signals) / len(signals)
    return "LONG" if score >= 0.5 else ("SHORT" if score <= -0.5 else "NEUTRAL")


# ── Yahoo Finance fallback ────────────────────────────────────────────────────

def _yf_download_timed(ticker: str, period: str, interval: str,
                       timeout: float = YF_TIMEOUT_S) -> pd.DataFrame:
    """
    Run yf.download() in a worker thread and enforce a hard timeout.

    yfinance has no timeout parameter and can hang indefinitely on a stalled
    connection. Running it in a daemon thread with a bounded join() lets us
    raise instead of freezing the whole trading loop (which Galahad would
    otherwise misread as a crash).
    """
    import yfinance as yf
    result: list = [None]
    error:  list = [None]

    def fetch() -> None:
        try:
            result[0] = yf.download(ticker, period=period, interval=interval,
                                    auto_adjust=True, progress=False)
        except Exception as exc:            # noqa: BLE001 -- surfaced below
            error[0] = exc

    t = threading.Thread(target=fetch, name=f"yf-{interval}", daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        # Thread is abandoned (daemon) -- it will die with the process.
        raise TimeoutError(
            f"Yahoo Finance timed out after {timeout}s ({ticker} {period} {interval})"
        )
    if error[0] is not None:
        raise error[0]
    return result[0]


def _fetch_yf(ticker: str, period: str, interval: str) -> pd.DataFrame:
    """Fetch OHLCV from Yahoo Finance and normalise column names."""
    try:
        raw = _yf_download_timed(ticker, period, interval)
        if raw is None or raw.empty:
            return pd.DataFrame()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]
        raw = raw.rename(columns={"adj close": "close"})
        if raw.index.tz is None:
            raw.index = raw.index.tz_localize("UTC")
        else:
            raw.index = raw.index.tz_convert("UTC")
        for col in ["open", "high", "low", "close"]:
            if col in raw.columns:
                raw[col] = pd.to_numeric(raw[col], errors="coerce")
        if "volume" not in raw.columns:
            raw["volume"] = 0.0
        raw["volume"] = raw["volume"].fillna(0.0)
        return raw.dropna(subset=["close"])
    except Exception as exc:
        log.warning("Yahoo Finance fetch failed (%s %s %s): %s", ticker, period, interval, exc)
        return pd.DataFrame()


# ── Main USDataFeed class ─────────────────────────────────────────────────────

class USDataFeed:
    """
    Merlin -- S&P 500 three-timeframe data feed.

    Usage:
        feed = USDataFeed(connector)  # CapitalComConnector or None for Yahoo fallback
        feed.initialise()
        bar_1d = feed.latest_bar("1d")
        bar_1h = feed.latest_bar("1h")
        bar_5m = feed.latest_bar("5m")
        feed.refresh()  # call every 5 minutes
    """

    def __init__(self, connector=None) -> None:
        self._cap = connector
        self._use_cap = connector is not None and getattr(connector, "connected", False)
        self._frames: dict[str, pd.DataFrame] = {}
        log.info(
            "Merlin initialising | source=%s (US500)",
            "Capital.com" if self._use_cap else "Yahoo Finance (fallback)",
        )

    def _fetch_cap(self, resolution: str, num_points: int) -> pd.DataFrame:
        """Fetch via Capital.com API (Excalibur)."""
        if self._cap is None:
            return pd.DataFrame()
        df = self._cap.get_historical_prices(US_EPIC, resolution, num_points)
        if df is None or df.empty:
            return pd.DataFrame()
        return df

    # Full history is fetched once at startup; every refresh() thereafter only
    # pulls a small recent window (recent=True) and merges it into the cached
    # frame -- so we never re-download hundreds of bars on each 5-minute tick.
    def _fetch_1d(self, recent: bool = False) -> pd.DataFrame:
        if self._use_cap:
            df = self._fetch_cap("DAY", 10 if recent else 200)
            if not df.empty:
                return df
        log.info("Falling back to Yahoo Finance for 1d data")
        return _fetch_yf(US_TICKER, "5d" if recent else "2y", "1d")

    def _fetch_1h(self, recent: bool = False) -> pd.DataFrame:
        if self._use_cap:
            df = self._fetch_cap("HOUR", 10 if recent else 200)
            if not df.empty:
                return df
        log.info("Falling back to Yahoo Finance for 1h data")
        return _fetch_yf(US_TICKER, "5d" if recent else "90d", "1h")

    def _fetch_5m(self, recent: bool = False) -> pd.DataFrame:
        if self._use_cap:
            df = self._fetch_cap("MINUTE_5", 20 if recent else 200)
            if not df.empty:
                return df
        log.info("Falling back to Yahoo Finance for 5m data")
        period = "1d" if recent else "60d"
        df = _fetch_yf(US_TICKER, period, "5m")
        if not df.empty:
            return df
        log.info("5m fallback: trying 15m")
        return _fetch_yf(US_TICKER, period, "15m")

    def initialise(self) -> None:
        """Full historical load at startup. Builds all three timeframes."""
        log.info("=== Merlin initialising data feed (US500) ===")
        for tf, fetch_fn in [("1d", self._fetch_1d), ("1h", self._fetch_1h), ("5m", self._fetch_5m)]:
            log.info("  Fetching %s...", tf)
            df = fetch_fn()
            if df.empty:
                log.warning("  [%s] No data returned", tf)
                self._frames[tf] = pd.DataFrame()
                continue
            df = add_indicators(df)
            self._frames[tf] = df
            bar   = df.iloc[-1]
            ssl   = "BULL" if bar.get("ssl_bull") else "BEAR"
            rsi   = bar.get("rsi", 0)
            close = bar.get("close", 0)
            log.info(
                "  [%s] %d candles | close=%.1f | rsi=%.1f | ssl=%s",
                tf, len(df), close,
                rsi if pd.notna(rsi) else 0,
                ssl,
            )
            time.sleep(RATE_LIMIT_S)
        log.info("Merlin ready -- all timeframes loaded")

    def refresh(self) -> None:
        """Incremental update -- fetch new candles and recalculate indicators."""
        log.info("=== Merlin refreshing ===")
        for tf, fetch_fn in [("1d", self._fetch_1d), ("1h", self._fetch_1h), ("5m", self._fetch_5m)]:
            new_df = fetch_fn(recent=True)   # small recent window only -- merged into cache below
            if new_df.empty:
                log.warning("  [%s] No new data", tf)
                continue

            if tf in self._frames and not self._frames[tf].empty:
                combined = pd.concat([self._frames[tf], new_df])
                combined = combined[~combined.index.duplicated(keep="last")]
                combined.sort_index(inplace=True)
                combined = add_indicators(combined)
                self._frames[tf] = combined
            else:
                new_df = add_indicators(new_df)
                self._frames[tf] = new_df

            bar   = self._frames[tf].iloc[-1]
            ssl   = "BULL" if bar.get("ssl_bull") else "BEAR"
            rsi   = bar.get("rsi", 0)
            close = bar.get("close", 0)
            log.info(
                "  [%s] %d bars | close=%.1f | rsi=%.1f | ssl=%s",
                tf, len(self._frames[tf]), close,
                rsi if pd.notna(rsi) else 0,
                ssl,
            )
            time.sleep(RATE_LIMIT_S)

    def get(self, timeframe: str = "5m") -> pd.DataFrame:
        """Return enriched DataFrame for the requested timeframe."""
        if timeframe not in self._frames:
            raise KeyError(f"Timeframe '{timeframe}' not loaded. Call initialise() first.")
        return self._frames[timeframe].copy()

    def latest_bar(self, timeframe: str = "5m") -> pd.Series:
        """Return the most recent completed bar with all indicators."""
        df = self.get(timeframe)
        if df.empty:
            raise ValueError(f"No data available for timeframe {timeframe}")
        return df.iloc[-1]

    def get_historical_price(self, market, timestamp_utc):
        """Return the 5m candle close at/just before timestamp_utc from the cached
        frame -- used to resolve stale phantom PENDING rows on restart. float|None."""
        try:
            df = self._frames.get("5m")
            if df is None or df.empty:
                return None
            ts = pd.Timestamp(timestamp_utc)
            ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
            sub = df[df.index <= ts]
            if sub.empty:
                return None
            return float(sub["close"].iloc[-1])
        except Exception as exc:
            log.warning("Merlin get_historical_price error: %s", exc)
            return None

    def composite_signal(self, timeframe: str = "5m") -> str:
        """Quick composite directional label for the latest bar."""
        return get_composite_signal(self.latest_bar(timeframe))

    def print_status(self) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        phase = get_session_phase()
        log.info("-" * 60)
        log.info("Merlin -- US500 Data Feed Status | %s", now)
        log.info("  Session phase: %s | Market open: %s", phase, is_market_open())
        for tf in ("1d", "1h", "5m"):
            if tf not in self._frames or self._frames[tf].empty:
                log.info("  [%s] No data", tf)
                continue
            bar = self.latest_bar(tf)
            sig = get_composite_signal(bar)
            rsi = bar.get("rsi", 0)
            log.info(
                "  [%s] close=%.1f | rsi=%.1f | ssl=%s | tmo=%.3f | -> %s",
                tf,
                bar["close"],
                rsi if pd.notna(rsi) else 0,
                "BULL" if bar.get("ssl_bull") else "BEAR",
                bar.get("tmo_main", 0) if pd.notna(bar.get("tmo_main")) else 0,
                sig,
            )
        log.info("-" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Merlin self-test (Yahoo Finance fallback)...")
    feed = USDataFeed(connector=None)
    feed.initialise()
    feed.print_status()
    phase = get_session_phase()
    log.info("Current phase: %s | Open: %s", phase, is_market_open())
    log.info("Merlin self-test complete.")
