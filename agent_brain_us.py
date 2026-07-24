"""
USHybrid AI -- agent_brain_us.py  (Arthur)
Claude AI brain for US S&P 500 (US500) spread betting decisions.
Called only after Lancelot pre-checks have passed.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anthropic
import pandas as pd
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

log    = logging.getLogger("USHybrid.Arthur")
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Arthur, the EXIT MANAGER for USHybrid AI.

CRITICAL: You did NOT choose to enter this trade. Lancelot entered it automatically
(pre-checks + 3-timeframe SSL agreement). You are managing an OPEN POSITION only.
Your ONLY job is to decide HOLD or EXIT. You NEVER make entry decisions and you NEVER
output ENTER_LONG, ENTER_SHORT or STAY_OUT. Output exactly one of: HOLD, EXIT.

THE INSTRUMENT
S&P 500 (US500) via CFD on Capital.com, GBP 0.67 per point, GBP/USD converted. The trade
already has a mechanical 30pt trailing stop, a 45pt take-profit, and a Profit Protection
Ladder that locks in gains -- these run regardless of you. USHybrid is BIDIRECTIONAL, so
the open position may be LONG or SHORT. You add INTELLIGENT early exit on top: get out
before the stop when the trade is clearly turning against the position.

EXIT the position now if the trade is deteriorating:
  - Daily SSL has flipped AGAINST the open position (the trend that entered it broke).
  - 5-minute momentum is deteriorating / diverging against the position (TMO rolling over,
    MACD crossing back, money flow reversing).
  - RSI is extended and REVERSING against the position (overbought on a LONG turning down,
    or oversold on a SHORT turning up).
  - The session is near its 21:00 UTC close and the trade is not working -- limit the risk
    rather than ride into the close.
  - Guinevere news has turned against the position.
  - An FOMC decision or major US data release is within 30 minutes -- protect the position
    ahead of the volatility.

HOLD the position if the trade is still working:
  - Trend intact across daily / 1h / 5m in the position's direction.
  - Momentum still flowing the trade's way.
  - The Profit Protection Ladder is already protecting gains -- let it run.
  - Plenty of session time remaining and no clear reversal signal.

MORGAN CONFIDENCE sets how much room you give the OPEN trade (three-zone model):
  HIGH (75-100):    Give the trade more room; EXIT only on a clear reversal.
  NORMAL (50-74):   Normal exit criteria.
  WARNING (30-49):  Tighter -- EXIT on the first solid sign of deterioration.
  CRITICAL (<30):   Protect capital -- EXIT on clear deterioration. (The SYSTEM suspends
                    NEW entries in this zone; you only manage the existing position.)
Morgan is context for your EXIT posture only. It does NOT gate entry (Lancelot has
already entered) and must NOT stop you managing the trade.

RULES
  - When in doubt on a WORKING trade, HOLD -- the mechanical stop/ladder protects you.
  - When in doubt on a DETERIORATING trade, EXIT -- capital preservation first.
  - Never plan to hold past the 21:00 UTC session close (the engine force-closes anyway).
  - You manage ONE open position; if somehow flat, return HOLD (nothing to exit).

REQUIRED OUTPUT -- valid JSON only. No markdown, no preamble.
{
  "decision": "HOLD | EXIT",
  "confidence": 0-100,
  "reasoning": "2-4 sentences: is the trade working or deteriorating, and why HOLD/EXIT",
  "warnings": ["list of concerns about the open position"],
  "checklist": {
    "trend_intact": true,
    "momentum_with_position": true,
    "ladder_protecting": true,
    "session_time_ok": true,
    "news_supportive": true
  },
  "session_assessment": "brief comment on session phase and time-to-close"
}"""


# ── Format indicators for Arthur ──────────────────────────────────────────────

def _regime_block(regime: Optional[dict], min_confidence) -> str:
    """Live BULL/BEAR regime + LONG_ONLY + confidence-floor block for Arthur."""
    if not regime:
        return ("REGIME AND GATE (current)\n"
                "  Regime: BULL (assumed) | LONG_ONLY | min confidence to act: 50")
    reg = regime.get("regime", "BULL")
    mc = 50 if min_confidence is None else int(min_confidence)
    lvl = regime.get("sp500_level"); ma = regime.get("sp500_200ma")
    vix = regime.get("vix_level")
    pos = ("ABOVE" if regime.get("sp500_above_200ma") else "AT/BELOW")
    return (
        "REGIME AND GATE (current)\n"
        f"  Regime: {reg}  (S&P {pos} 200MA"
        + (f": {lvl} vs {ma}" if lvl and ma else "") + f", VIX {vix})\n"
        f"  USHybrid is LONG_ONLY -- never SHORT. "
        + ("Bull confirmed: act on valid dips.\n" if reg == "BULL"
           else "BEAR: stay out, wait for the bull to reassert (do NOT short).\n")
        + f"  Minimum confidence to open a LONG: {mc} (the system enforces this floor)."
    )


def _format_indicators(
    bar_1d: Optional[pd.Series],
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    current_price: float,
    session_phase: str,
    current_trade=None,
    calendar_context: Optional[str] = None,
    perf_context: Optional[str] = None,
    regime: Optional[dict] = None,
    min_confidence=None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    regime_block = _regime_block(regime, min_confidence)

    def _f(v, dp=2):
        if v is None or pd.isna(v):
            return "N/A"
        return f"{float(v):.{dp}f}"

    candle_colour = "GREEN" if bar_5m.get("close", 0) >= bar_5m.get("open", 0) else "RED"
    ssl_1d = "BULL" if (bar_1d is not None and bar_1d.get("ssl_bull")) else ("BEAR" if bar_1d is not None else "N/A (no daily data)")
    ssl_1h = "BULL" if bar_1h.get("ssl_bull") else "BEAR"
    ssl_5m = "BULL" if bar_5m.get("ssl_bull") else "BEAR"

    position_text = "None -- no open position"
    if current_trade is not None:
        pts_from_entry = (current_price - current_trade.entry_price) if current_trade.direction == "LONG" \
                         else (current_trade.entry_price - current_price)
        position_text = (
            f"OPEN {current_trade.direction} | "
            f"entry={current_trade.entry_price:.1f} | "
            f"current={current_price:.1f} | "
            f"pts_from_entry={pts_from_entry:+.1f} | "
            f"stop={current_trade.stop_loss:.1f} | "
            f"target={current_trade.take_profit:.1f} | "
            f"stake=£{current_trade.stake:.2f}/pt | "
            f"session={current_trade.session_phase}"
        )

    if current_trade is not None and getattr(current_trade, "ladder_step", 0):
        position_text += (
            " | PROFIT LADDER ACTIVE: floor locked at £%.2f (step %d). Position cannot "
            "close below this floor unless a gap event occurs -- factor this into your "
            "HOLD reasoning." % (getattr(current_trade, "ladder_floor_gbp", 0.0),
                                 int(getattr(current_trade, "ladder_step", 0))))

    return f"""Please analyse the current US S&P 500 (US500) market conditions.

TIME AND PRICE
  Time (UTC):       {now}
  Session Phase:    {session_phase}
  US500 Level:      {current_price:,.1f}

{regime_block}

DAILY CHART (Trend Direction -- sets allowed direction for today)
  SSL Cloud:        {ssl_1d}
  RSI (14):         {_f(bar_1d.get('rsi') if bar_1d is not None else None, 1)}
  TMO Main:         {_f(bar_1d.get('tmo_main') if bar_1d is not None else None, 3)}
  Chande MO (20):   {_f(bar_1d.get('chande_mo') if bar_1d is not None else None, 1)}

1-HOUR CHART (Trend Confirmation)
  SSL Cloud:        {ssl_1h}
  RSI (14):         {_f(bar_1h.get('rsi'), 1)}
  MACD Histogram:   {_f(bar_1h.get('macd_histogram'), 3)}
  TMO Main:         {_f(bar_1h.get('tmo_main'), 3)}
  TMO Smooth:       {_f(bar_1h.get('tmo_smooth'), 3)}
  Chande MO (20):   {_f(bar_1h.get('chande_mo'), 1)}
  Money Flow (14):  {_f(bar_1h.get('money_flow'), 2)}

5-MINUTE CHART (Entry Timing)
  SSL Cloud:        {ssl_5m}
  RSI (14):         {_f(bar_5m.get('rsi'), 1)}
  MACD Histogram:   {_f(bar_5m.get('macd_histogram'), 3)}
  TMO Main:         {_f(bar_5m.get('tmo_main'), 3)}
  TMO Smooth:       {_f(bar_5m.get('tmo_smooth'), 3)}
  Chande MO (20):   {_f(bar_5m.get('chande_mo'), 1)}
  Money Flow (14):  {_f(bar_5m.get('money_flow'), 2)}
  Last Candle:      {candle_colour} (close={_f(bar_5m.get('close'), 1)} open={_f(bar_5m.get('open'), 1)})

CURRENT POSITION
  {position_text}

{calendar_context if calendar_context else 'US ECONOMIC CALENDAR\n  No calendar data available.'}

{perf_context if perf_context else 'SELF PERFORMANCE AWARENESS\n  No performance data yet -- first trading session.'}

Please provide your analysis and trading decision in the required JSON format."""


# ── Main decision function ────────────────────────────────────────────────────

def get_trading_decision(
    bar_1h: pd.Series,
    bar_5m: pd.Series,
    current_price: float,
    session_phase: str,
    bar_1d: Optional[pd.Series] = None,
    current_trade=None,
    calendar_context: Optional[str] = None,
    perf_context: Optional[str] = None,
    regime: Optional[dict] = None,
    min_confidence=None,
) -> dict:
    """
    Send indicator data to Arthur (Claude) and receive a trading decision.
    Only call this AFTER Lancelot pre-checks have passed.
    """
    log.info("Sending indicators to Arthur...")

    user_message = _format_indicators(
        bar_1d, bar_1h, bar_5m, current_price, session_phase,
        current_trade, calendar_context, perf_context, regime, min_confidence,
    )

    for attempt in range(2):
        try:
            response = client.messages.create(
                model      = "claude-sonnet-4-6",
                max_tokens = 2000,
                system     = SYSTEM_PROMPT,
                messages   = [{"role": "user", "content": user_message}],
            )

            if response.stop_reason == "max_tokens":
                log.warning("Arthur hit max_tokens -- JSON may be truncated")

            raw_text = response.content[0].text.strip()
            if raw_text.startswith("```"):
                raw_text = "\n".join(
                    l for l in raw_text.split("\n")
                    if not l.strip().startswith("```")
                ).strip()

            try:
                decision = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                log.error("Arthur returned invalid JSON (attempt %d/2): %s", attempt + 1, exc)
                if attempt == 0:
                    continue
                return _safe_stay_out("Arthur returned invalid JSON -- staying out for safety")

            decision["timestamp"]     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            decision["tokens_used"]   = response.usage.input_tokens + response.usage.output_tokens
            decision["current_price"] = current_price
            decision["session_phase"] = session_phase

            log.info(
                "Arthur decision: %s | confidence=%s | tokens=%d",
                decision.get("decision"),
                decision.get("confidence"),
                decision.get("tokens_used", 0),
            )
            return decision

        except anthropic.APIError as exc:
            log.error("Anthropic API error: %s", exc)
            return _safe_stay_out(f"API error: {str(exc)}")
        except Exception as exc:
            log.error("Unexpected error calling Arthur: %s", exc)
            return _safe_stay_out(f"Unexpected error: {str(exc)}")

    return _safe_stay_out("Arthur failed after all attempts")


def _safe_stay_out(reason: str) -> dict:
    return {
        "decision":            "HOLD",
        "confidence":          0,
        "session_bias":        "CLOSED",
        "reasoning":           reason,
        "warnings":            [reason],
        "checklist":           {},
        "calendar_assessment": "",
        "session_assessment":  "",
        "timestamp":           datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tokens_used":         0,
    }


def format_decision_for_display(decision: dict) -> str:
    """Format Arthur's decision for terminal display."""
    d         = decision.get("decision", "UNKNOWN")
    conf      = decision.get("confidence", "--")
    bias      = decision.get("session_bias", "--")
    reasoning = decision.get("reasoning", "No reasoning")
    warnings  = decision.get("warnings", [])
    tokens    = decision.get("tokens_used", 0)
    ts        = decision.get("timestamp", "")
    price     = decision.get("current_price", "--")
    price_str = f"{price:,.1f}" if isinstance(price, (int, float)) else str(price)
    lines = [
        "=" * 60,
        "  USHybrid AI -- Arthur's Decision",
        f"  {ts}",
        "=" * 60,
        f"  Decision:        {d}",
        f"  Confidence:      {conf}/100",
        f"  Session Bias:    {bias}",
        f"  US500 Level:     {price_str}",
        f"  Session Phase:   {decision.get('session_phase', '--')}",
        "",
        "  Reasoning:",
        f"  {reasoning}",
        "",
    ]
    if warnings:
        lines.append("  Warnings:")
        for w in warnings:
            lines.append(f"    - {w}")
        lines.append("")
    cal = decision.get("calendar_assessment")
    if cal:
        lines.append(f"  Calendar: {cal}")
    ses = decision.get("session_assessment")
    if ses:
        lines.append(f"  Session:  {ses}")
    cl = decision.get("checklist", {})
    if cl:
        lines.append("  Checklist:")
        for k, v in cl.items():
            icon = "PASS" if v else "FAIL"
            lines.append(f"    [{icon}] {k.replace('_', ' ').title()}")
    lines.append(f"  Tokens used: {tokens}")
    lines.append("=" * 60)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.info("Arthur self-test -- calling Claude with a bullish US500 setup...")
    bar_1d = pd.Series({"ssl_bull": True, "rsi": 58.0, "tmo_main": 1.5, "chande_mo": 25.0})
    bar_1h = pd.Series({
        "ssl_bull": True, "rsi": 62.0, "macd_histogram": 8.5,
        "tmo_main": 2.1, "tmo_smooth": 1.5, "chande_mo": 45.0, "money_flow": 150.0,
    })
    bar_5m = pd.Series({
        "ssl_bull": True, "rsi": 58.0, "macd_histogram": 2.5,
        "tmo_main": 0.8, "tmo_smooth": 0.5, "chande_mo": 30.0, "money_flow": 80.0,
        "open": 7490.0, "close": 7500.0,
    })
    decision = get_trading_decision(
        bar_1h=bar_1h, bar_5m=bar_5m,
        current_price=7500.0, session_phase="CORE", bar_1d=bar_1d,
    )
    print(format_decision_for_display(decision))
    log.info("Arthur self-test complete.")
