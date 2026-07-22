"""
USHybrid AI -- regime_us.py
Market-regime reader for the System 4 Review reset (18 Jul 2026).

Reads the weekly market context published by Gaius and classifies the S&P 500
regime for the LONG_ONLY confidence gate and Arthur's prompt:

  BULL regime : S&P 500 above its 200-day MA
  BEAR regime : S&P 500 at/below its 200-day MA

Refreshed weekly by Gaius (Sunday 20:00 UTC); between updates the cached values
are used. Additionally cached in-process for 30 minutes (same pattern as
CryptoTrader's regime.py). All times UTC. Relative paths only.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("USHybrid.Regime")

BASE_DIR = Path(__file__).resolve().parent
# Relative to the desk root: .../Desktop/USHybridAI -> .../Desktop/GaiusAI/logs
MARKET_CONTEXT_PATH = BASE_DIR.parent / "GaiusAI" / "logs" / "market_context.json"

CACHE_SECONDS = 30 * 60   # re-read the file at most every 30 minutes
_cache = {"at": 0.0, "data": None}


def _read_file() -> dict:
    """Parse the latest equities block from Gaius market_context.json. On any
    failure falls back to BULL (the confirmed regime as of 18 Jul 2026) so the
    LONG_ONLY system keeps its normal LONG bar rather than the stricter bear bar."""
    try:
        raw = json.loads(MARKET_CONTEXT_PATH.read_text(encoding="utf-8"))
        entries = raw if isinstance(raw, list) else [raw]
        latest = entries[-1]
        eq = latest.get("equities", {})
        above = bool(eq.get("sp500_above_200ma", True))
        return {
            "regime":            "BULL" if above else "BEAR",
            "sp500_above_200ma": above,
            "sp500_level":       eq.get("sp500_level"),
            "sp500_200ma":       eq.get("sp500_200ma"),
            "vix_level":         eq.get("vix_level"),
            "vix_regime":        eq.get("vix_regime", ""),
            "week_commencing":   latest.get("week_commencing", ""),
            "source":            "market_context.json",
        }
    except Exception as exc:
        log.warning("Regime read failed (%s) -- defaulting to BULL", exc)
        return {"regime": "BULL", "sp500_above_200ma": True, "sp500_level": None,
                "sp500_200ma": None, "vix_level": None, "vix_regime": "",
                "week_commencing": "", "source": "fallback"}


def get_regime(force: bool = False) -> dict:
    """Return the current regime dict, cached in-process for 30 minutes."""
    now = time.monotonic()
    if (not force) and _cache["data"] is not None and (now - _cache["at"] < CACHE_SECONDS):
        return _cache["data"]
    data = _read_file()
    _cache.update({"at": now, "data": data})
    log.info("Regime: %s | S&P above 200MA=%s | VIX=%s (%s) | wc=%s",
             data["regime"], data["sp500_above_200ma"], data["vix_level"],
             data["vix_regime"], data["week_commencing"])
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    print(json.dumps(get_regime(force=True), indent=2))
