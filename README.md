# USHybrid A.I.

Second system on the Albion **Hybrid Desk** (after FTSEHybrid). Hybrid architecture:
**Lancelot enters, Arthur manages exits.** Port **5044**, S&P 500 (US500), paper £1,000.

- **Template:** USTrader v1.2.3 · **Direction:** BIDIRECTIONAL (unlike the LONG_ONLY original)
- **Session:** 14:30–21:00 UTC · Runs beside USTrader (5004) + USBenchmark (5024)

## Architecture
**ENTRY — Lancelot only:** fires when (1) all `pre_checks_us` pass, (2) Daily + 1h + 5m
SSL all agree a direction (LONG **or** SHORT), and (3) no HARD_BLOCK calendar event
(FOMC / US CPI / jobs) is within 60 min. SHORTs also require Morgan ≥ 65. No Arthur,
no confidence/RSI entry gate. GBP/USD conversion as USTrader.

**EXIT — Arthur only:** consulted every 5 min, **HOLD** or **EXIT** only (never entry).
Exits early on daily-SSL flip, deteriorating momentum, extended/reversing RSI, news
against the position, an FOMC/US-data event within 30 min, or the 21:00 UTC close.
Morgan sets his exit posture. Mechanical 30pt stop / 45pt target / Profit Protection
Ladder run regardless.

Stop 30pt · Target 45pt · Spread 0.6pt · Stake £0.67/pt · **No phantom logging**.
Appears automatically on HybridRoundTable (5050): **Hybrid − Benchmark = Arthur's exit value.**

## Running
```
python dashboard_us.py     # port 5044
python watchdog_us.py      # supervises main_ushybrid.py
```
Or the **Start USHybrid** desktop shortcut. Dashboard pages: Dashboard | P&L →. All times UTC.
