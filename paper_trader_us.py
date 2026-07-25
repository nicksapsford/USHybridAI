"""
USHybrid AI -- paper_trader_us.py  (Stanley)
Records every US500 spread bet trade and tracks running P&L.
Persists state between sessions via logs/us_trades.csv.
"""

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from strategy_us import USTrade, TRAILING_STOP_POINTS, DEFAULT_GBPUSD

log = logging.getLogger("USHybrid.Stanley")

STARTING_CAPITAL_GBP = 1000.0
LOG_DIR      = Path(__file__).parent / "logs"
TRADES_LOG   = LOG_DIR / "us_trades.csv"
SUMMARY_LOG  = LOG_DIR / "us_summary.txt"
STATE_FILE   = LOG_DIR / "stanley_state.json"

CSV_HEADERS = [
    "date", "time", "direction",
    "entry_price_usd", "exit_price_usd",
    "stake_per_point", "points_gained",
    "pnl_usd", "pnl_gbp", "gbpusd_rate",
    "exit_reason", "capital_after_gbp",
    "entry_time", "exit_time", "session_phase",
    # ── ARTHUR_EXIT analytics (Gaius Commission 012 build, 25 Jul 2026) ──
    # Populated ONLY on ARTHUR_EXIT rows. Indicator snapshot + Arthur's exit-decision
    # confidence are captured at exit time; the post-exit prices are filled in
    # retrospectively (fill_post_exit_prices) so we can measure whether an early exit
    # was genuine skill (trade kept falling) or premature (trade recovered).
    "exit_daily_ssl", "exit_1h_ssl", "exit_5m_ssl",
    "exit_tmo", "exit_money_flow", "exit_rsi", "exit_chande_mo", "exit_confidence",
    "price_30m_after", "price_60m_after", "recovered_30m", "recovered_60m",
]

# The 12 analytics columns above, in order -- used by _migrate_csv and blank-fill.
_ANALYTICS_COLS = CSV_HEADERS[15:]


class PaperTraderUS:
    """
    Stanley -- paper trading accountant for US500 spread bets.
    Tracks capital in GBP, records trades (USD prices + GBP/USD P&L), saves CSV.
    """

    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not TRADES_LOG.exists():
            self._init_csv()
            log.info("Created new trades log: %s", TRADES_LOG)
        else:
            log.info("Using existing trades log: %s", TRADES_LOG)
            self._migrate_csv()   # one-time: add ARTHUR_EXIT analytics columns if absent

        self.capital_gbp   = STARTING_CAPITAL_GBP
        self.current_trade: Optional[USTrade] = None
        self.trade_history: list[USTrade]     = []

        previous_capital = self._load_last_capital()
        if previous_capital:
            self.capital_gbp = previous_capital
            log.info("Resumed | capital=GBP %.2f", self.capital_gbp)
        else:
            log.info("Fresh start | capital=GBP %.2f", STARTING_CAPITAL_GBP)

        self._restore_state()
        log.info("Stanley ready -- US500 paper trader")

    # ── CSV management ────────────────────────────────────────────────────────

    def _init_csv(self) -> None:
        with open(TRADES_LOG, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()

    def _migrate_csv(self) -> None:
        """One-time migration (Gaius Commission 012, 25 Jul 2026): add the ARTHUR_EXIT
        analytics columns to an existing trades log, leaving historical rows blank in
        the new fields. No-op if the columns are already present. Best-effort."""
        try:
            with open(TRADES_LOG, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                existing = next(reader, [])
            if all(c in existing for c in _ANALYTICS_COLS):
                return  # already migrated
            with open(TRADES_LOG, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            with open(TRADES_LOG, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in CSV_HEADERS})
            log.info("Migrated trades log: added %d ARTHUR_EXIT analytics columns",
                     len(_ANALYTICS_COLS))
        except Exception as exc:
            log.warning("Trades-log migration skipped (%s)", exc)

    def _load_last_capital(self) -> Optional[float]:
        if not TRADES_LOG.exists():
            return None
        try:
            df = pd.read_csv(TRADES_LOG)
            if df.empty:
                return None
            return float(df["capital_after_gbp"].iloc[-1])
        except Exception:
            return None

    def _save_state(self) -> None:
        """Persist open trade to JSON so it survives engine restarts."""
        if self.current_trade is None:
            self._clear_state()
            return
        t = self.current_trade
        state = {
            "direction":     t.direction,
            "entry_price":   t.entry_price,
            "stop_pts":      t.stop_pts,
            "entry_time":    t.entry_time.isoformat() if t.entry_time else None,
            "session_phase": t.session_phase,
            "trail_best":    t.trail_best,
            "stop_loss":     t.stop_loss,
            "take_profit":   t.take_profit,
            "stake":         t.stake,
            "gbpusd_rate":   t.gbpusd_rate,
        }
        try:
            STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not save state: %s", exc)

    def _clear_state(self) -> None:
        try:
            if STATE_FILE.exists():
                STATE_FILE.unlink()
        except Exception:
            pass

    def _restore_state(self) -> None:
        """Reload an open trade from the state file after a restart."""
        if not STATE_FILE.exists():
            return
        try:
            from strategy_us import should_force_close
            if should_force_close():
                log.info("State file found but force-close time has passed -- discarding stale state")
                self._clear_state()
                return
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            trade = USTrade(
                direction     = data["direction"],
                entry_price   = data["entry_price"],
                stop_pts      = data["stop_pts"],
                entry_time    = datetime.fromisoformat(data["entry_time"]) if data.get("entry_time") else None,
                session_phase = data.get("session_phase", ""),
                gbpusd_rate   = data.get("gbpusd_rate", DEFAULT_GBPUSD),
            )
            trade.trail_best  = data["trail_best"]
            trade.stop_loss   = data["stop_loss"]
            trade.take_profit = data["take_profit"]
            trade.stake       = data["stake"]
            self.current_trade = trade
            log.info(
                "STATE RESTORED: %s entry=%.1f stop=%.1f trail_best=%.1f stake=£%.2f/pt",
                trade.direction, trade.entry_price,
                trade.stop_loss, trade.trail_best, trade.stake,
            )
        except Exception as exc:
            log.warning("Could not restore state (%s) -- starting fresh", exc)
            self._clear_state()

    def _log_trade(self, trade: USTrade, exit_meta: dict = None) -> None:
        if trade.exit_price is None:
            return
        exit_t  = trade.exit_time or datetime.now(timezone.utc)
        entry_t = trade.entry_time or exit_t
        row = {k: "" for k in CSV_HEADERS}   # analytics cols default blank
        row.update({
            "date":              exit_t.strftime("%Y-%m-%d"),
            "time":              exit_t.strftime("%H:%M:%S"),
            "direction":         trade.direction,
            "entry_price_usd":   f"{trade.entry_price:.1f}",
            "exit_price_usd":    f"{trade.exit_price:.1f}",
            "stake_per_point":   f"{trade.stake:.2f}",
            "points_gained":     f"{trade.pnl_pts:+.1f}",
            "pnl_usd":           f"{trade.pnl_usd:+.2f}",
            "pnl_gbp":           f"{trade.pnl_gbp:+.2f}",
            "gbpusd_rate":       f"{trade.gbpusd_rate:.4f}",
            "exit_reason":       trade.exit_reason,
            "capital_after_gbp": f"{self.capital_gbp:.2f}",
            "entry_time":        entry_t.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time":         exit_t.strftime("%Y-%m-%d %H:%M:%S"),
            "session_phase":     trade.session_phase,
        })
        # ARTHUR_EXIT analytics: indicator snapshot + Arthur's exit confidence (Comm 012).
        if exit_meta:
            for k in _ANALYTICS_COLS:
                if k in exit_meta and exit_meta[k] is not None:
                    row[k] = exit_meta[k]
        with open(TRADES_LOG, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
        log.info("Trade logged: %s", TRADES_LOG)

    def fill_post_exit_prices(self, current_price: float, now_utc: datetime = None) -> None:
        """Retrospectively fill price_30m_after / price_60m_after (+ recovered flags) on
        ARTHUR_EXIT rows once 30 / 60 min have elapsed since the exit (Gaius Commission
        012). Robust across restarts -- rescans the CSV each call, samples the current
        price at the first tick past T+30 / T+60, and rewrites only if something changed.
        'recovered' = would the trade be at/through its ENTRY level (breakeven) at that
        sample (LONG: price >= entry; SHORT: price <= entry). Best-effort; never raises."""
        try:
            if current_price is None:
                return
            now_utc = now_utc or datetime.now(timezone.utc)
            with open(TRADES_LOG, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if not rows:
                return
            changed = False
            for r in rows:
                if r.get("exit_reason") != "ARTHUR_EXIT":
                    continue
                if r.get("price_30m_after") and r.get("price_60m_after"):
                    continue  # already fully filled
                try:
                    xt = datetime.strptime(r["exit_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    entry = float(r["entry_price_usd"]); direction = r["direction"]
                except (KeyError, ValueError):
                    continue
                elapsed = (now_utc - xt).total_seconds() / 60.0
                for mins, pcol, rcol in ((30, "price_30m_after", "recovered_30m"),
                                         (60, "price_60m_after", "recovered_60m")):
                    if elapsed >= mins and not r.get(pcol):
                        r[pcol] = f"{current_price:.1f}"
                        recovered = (current_price >= entry) if direction == "LONG" else (current_price <= entry)
                        r[rcol] = "YES" if recovered else "NO"
                        changed = True
            if changed:
                with open(TRADES_LOG, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
                    w.writeheader()
                    for r in rows:
                        w.writerow({k: r.get(k, "") for k in CSV_HEADERS})
        except Exception as exc:
            log.debug("fill_post_exit_prices skipped (%s)", exc)

    def _save_summary(self) -> None:
        total    = len(self.trade_history)
        winners  = sum(1 for t in self.trade_history if (t.pnl_gbp or 0) >= 0)
        win_rate = (winners / total * 100) if total > 0 else 0.0
        total_pnl = sum(t.pnl_gbp for t in self.trade_history if t.pnl_gbp is not None)
        lines = [
            "=" * 50,
            "USHybrid AI -- Stanley Paper Trader Summary",
            "Generated: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "=" * 50,
            f"Starting capital:  GBP {STARTING_CAPITAL_GBP:.2f}",
            f"Current capital:   GBP {self.capital_gbp:.2f}",
            f"Total P&L:         GBP {total_pnl:+.2f}",
            f"Total return:      {(self.capital_gbp / STARTING_CAPITAL_GBP - 1) * 100:+.2f}%",
            "",
            f"Total trades:      {total}",
            f"Winning trades:    {winners}",
            f"Win rate:          {win_rate:.1f}%",
            "",
        ]
        if self.trade_history:
            lines.append("Recent trades (last 10):")
            lines.append("-" * 50)
            for t in self.trade_history[-10:]:
                result = "WIN " if (t.pnl_gbp or 0) >= 0 else "LOSS"
                lines.append(
                    f"  [{result} {t.direction}] {t.session_phase} | "
                    f"entry={t.entry_price:.1f} exit={t.exit_price:.1f} "
                    f"pts={t.pnl_pts:+.1f} P&L=GBP {t.pnl_gbp:+.2f} (USD {t.pnl_usd:+.2f}) "
                    f"reason={t.exit_reason}"
                )
        lines.append("=" * 50)
        with open(SUMMARY_LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ── Trade management ──────────────────────────────────────────────────────

    @property
    def in_trade(self) -> bool:
        return self.current_trade is not None

    @property
    def total_trades(self) -> int:
        return len(self.trade_history)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trade_history if (t.pnl_gbp or 0) >= 0)

    @property
    def win_rate(self) -> float:
        if not self.trade_history:
            return 0.0
        return self.winning_trades / len(self.trade_history) * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl_gbp for t in self.trade_history if t.pnl_gbp is not None)

    def open_trade(self, direction: str, price: float, session_phase: str = "",
                   gbpusd_rate: float = DEFAULT_GBPUSD) -> USTrade:
        """Open a new paper trade and log it."""
        from strategy_us import open_trade
        self.current_trade = open_trade(direction, price, session_phase, gbpusd_rate=gbpusd_rate)
        self._save_state()
        log.info(
            "[OPEN] %s | entry=%.1f | stake=£%.2f/pt | stop=%.1f | target=%.1f",
            direction, price, self.current_trade.stake,
            self.current_trade.stop_loss, self.current_trade.take_profit,
        )
        return self.current_trade

    def close_trade(self, price: float, reason: str,
                    gbpusd_rate: Optional[float] = None,
                    exit_meta: dict = None) -> Optional[USTrade]:
        """Close the current paper trade, update capital, save CSV. exit_meta (optional)
        carries the indicator snapshot + Arthur's exit confidence for an ARTHUR_EXIT
        (Gaius Commission 012); it is written to the analytics columns of the row."""
        if self.current_trade is None:
            return None
        # stop-fill fidelity (Job 10, 24 Jul 2026, Nick-confirmed 23 Jul): on a
        # STOP_LOSS exit the observed price may have gapped through the stop
        # between 30-second monitor checks; fill at the stop level instead so the
        # clamped price feeds the P&L computation (USD + GBP). Other exit reasons
        # (TAKE_PROFIT, ARTHUR_EXIT, FORCE_CLOSE_EOD) keep the observed price.
        if reason == "STOP_LOSS":
            if self.current_trade.direction == "LONG":
                price = max(self.current_trade.stop_loss, price)
            else:
                price = min(self.current_trade.stop_loss, price)
        from strategy_us import close_trade
        trade = close_trade(self.current_trade, price, reason, gbpusd_rate)
        self.capital_gbp = round(self.capital_gbp + trade.pnl_gbp, 2)
        self.trade_history.append(trade)
        self._log_trade(trade, exit_meta=exit_meta)
        self._save_summary()
        self._clear_state()
        result = "PROFIT" if trade.pnl_gbp >= 0 else "LOSS"
        log.info(
            "[%s] Trade complete | %s | pts=%+.1f | P&L=GBP %+.2f | capital=GBP %.2f",
            result, trade.direction, trade.pnl_pts, trade.pnl_gbp, self.capital_gbp,
        )
        self.current_trade = None
        return trade

    def monitor_trade(self, price: float, gbpusd_rate: Optional[float] = None) -> Optional[str]:
        """
        Update trailing stop and check for exit.
        Returns exit reason string if closed, else None.
        """
        if self.current_trade is None:
            return None
        moved = self.current_trade.update_trailing_stop(price)
        rung = self.current_trade.apply_profit_ladder(price)   # Profit ladder (Variant 2)
        if rung:
            self._log_ladder_step(rung)
            moved = True
        if moved:
            self._save_state()
        reason = self.current_trade.check_exit(price)
        if reason:
            self.close_trade(price, reason, gbpusd_rate)
            return reason
        return None

    def _log_ladder_step(self, rung):
        """Append a profit-ladder rung trigger to logs/profit_ladder.csv (Variant 2)."""
        import csv, os
        from datetime import datetime, timezone
        t = self.current_trade
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "profit_ladder.csv")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp_utc", "system", "direction",
                    "entry_price", "trigger_float_gbp", "floor_gbp", "step_number",
                    "stop_before", "stop_after"])
                if new:
                    w.writeheader()
                w.writerow({"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "system": "USHybrid", "direction": t.direction, "entry_price": t.entry_price,
                    "trigger_float_gbp": rung["trigger_float_gbp"], "floor_gbp": rung["floor_gbp"],
                    "step_number": rung["step"], "stop_before": rung["stop_before"],
                    "stop_after": rung["stop_after"]})
        except Exception as exc:
            log.warning("Could not log ladder step: %s", exc)

    def print_status(self) -> None:
        log.info("=" * 60)
        log.info("USHybrid AI -- Stanley Paper Trader Status")
        log.info("-" * 60)
        log.info("  Starting capital:  GBP %.2f", STARTING_CAPITAL_GBP)
        log.info("  Current capital:   GBP %.2f", self.capital_gbp)
        log.info("  Total P&L:         GBP %+.2f", self.total_pnl)
        log.info("  Return:            %+.2f%%",
                 (self.capital_gbp / STARTING_CAPITAL_GBP - 1) * 100)
        log.info("-" * 60)
        log.info("  Total trades:  %d", self.total_trades)
        log.info("  Win rate:      %.1f%%", self.win_rate)
        if self.in_trade:
            t = self.current_trade
            log.info("  Open trade:  %s", t.summary())
        else:
            log.info("  Open trade:  None -- watching for setup")
        if self.trade_history:
            log.info("  Recent trades:")
            for t in self.trade_history[-3:]:
                log.info("    %s", t.summary())
        log.info("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Stanley self-test (US500)")
    stanley = PaperTraderUS()
    stanley.open_trade("LONG", 7500.0, "CORE", gbpusd_rate=1.27)
    stanley.monitor_trade(7510.0, 1.27)
    stanley.monitor_trade(7530.0, 1.27)
    result = stanley.close_trade(7540.0, "SESSION_CLOSE", 1.27)
    log.info("Trade result: %s", result.summary() if result else "None")
    stanley.print_status()
    log.info("Check %s for the trade record", TRADES_LOG)
    log.info("Stanley self-test complete.")
