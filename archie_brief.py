"""Prototype Archie Brief builders -- tested against live /api/state before being
inserted into the dashboards. Plain text only, all UTC, reads already-assembled
state (no new external fetch)."""
import csv
import os
from datetime import datetime, timezone

BAR = "=" * 64


def _num(v, nd=2):
    try:
        return ("{:." + str(nd) + "f}").format(float(v))
    except (TypeError, ValueError):
        return "--"


def _ssl(ind):
    if not isinstance(ind, dict) or ind.get("ssl_bull") is None:
        return "--"
    return "BULL" if ind.get("ssl_bull") else "BEAR"


def _guinevere_kind(system_name):
    """Which Guinevere module a system runs: news (Gold/Oil/Gas have guinevere_news),
    calendar-only (FTSE/US, and Crypto's economic calendar), else none."""
    n = (system_name or "").lower()
    if any(x in n for x in ("gold", "oil", "gas")):
        return "news"
    if any(x in n for x in ("ftse", "us", "crypto")):
        return "calendar"
    return "none"


def _news_line(appender):
    """Append cached Guinevere news sentiment + top headlines (no new external
    fetch -- reads the in-process guinevere_news cache the dashboard already polls)."""
    nd = None
    try:
        import guinevere_news
        nd = getattr(guinevere_news, "_news_cache", None)
    except Exception:
        nd = None
    if isinstance(nd, dict) and nd.get("sentiment") and (nd.get("timestamp") or nd.get("headlines")):
        sc = nd.get("score")
        appender("News sentiment: %s (score %s)" % (nd.get("sentiment", "NEUTRAL"),
                 sc if sc is not None else "--"))
        # Dedup headlines by url/title -- the news API can repeat an article
        # several times; keep the first occurrence only (Snag 16).
        seen = set()
        uniq = []
        for h in (nd.get("headlines") or []):
            if isinstance(h, dict):
                t = (h.get("title") or h.get("headline") or "").strip()
                k = (h.get("url") or h.get("link") or t).strip().lower()
                sc_h = h.get("score")
            else:
                t = str(h).strip()
                k = t.lower()
                sc_h = None
            # Part 2: only surface headlines that moved sentiment (|score| >= 1)
            try:
                if abs(float(sc_h)) < 1:
                    continue
            except (TypeError, ValueError):
                continue
            if not k or k in seen:
                continue
            seen.add(k)
            uniq.append(t or str(h))
        if uniq:
            for t in uniq[:3]:
                appender("  - %s" % t[:100])
        else:
            appender("  No significant headlines in current period")
    else:
        appender("News module active -- awaiting sentiment poll")


def _morgan_journey(logs_dir):
    """(start, current) confidence from morgan_confidence.csv, else (None, None)."""
    try:
        path = os.path.join(logs_dir, "morgan_confidence.csv")
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        if rows:
            return rows[0].get("confidence"), rows[-1].get("confidence")
    except Exception:
        pass
    return None, None


def _phantom_recent(soq, appender, show_market=False):
    """Snag 19 (20 Jul 2026): the last 5 phantom rows under STAY OUT QUALITY, newest
    first, so Archie sees overnight phantom activity inline without a screenshot.
    Columns: Date/Time (UTC) [| Market] | Direction | Confidence | 1hr Move | Verdict.
    Entry price omitted; PENDING rows shown as PENDING; empty -> 'No phantom data yet'."""
    appender("PHANTOM RECENT (last 5):")
    decisions = soq.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        appender("  No phantom data yet")
        return
    for r in list(reversed(decisions))[:5]:
        if not isinstance(r, dict):
            continue
        ts = (str(r.get("timestamp") or "").replace("T", " ")[:16]) or "--"
        direction = str(r.get("direction_blocked") or r.get("direction") or "--")
        conf = str(r.get("confidence") or "--")
        try:
            pv = float(r.get("pnl_1hr"))
            move = ("+£%.2f" % pv) if pv >= 0 else ("-£%.2f" % abs(pv))
        except (TypeError, ValueError):
            move = "--"
        verdict = str(r.get("verdict") or "PENDING") or "PENDING"
        if show_market:
            appender("  %-16s  %-3s  %-5s  %-6s  %-10s  %s" % (
                ts, str(r.get("market") or "--"), direction, conf, move, verdict))
        else:
            appender("  %-16s  %-5s  %-6s  %-10s  %s" % (ts, direction, conf, move, verdict))


def build_system_brief(state, system_name, asset_label, logs_dir=None, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    ver = state.get("version_string") or ("v" + str(state.get("version", "--")))
    price = None
    for pk in ("gas_price_usd", "oil_price_usd", "gold_price_usd",
               "ftse_level", "us_level", "price", "current_price"):
        if state.get(pk) is not None:
            price = state[pk]
            break
    session = state.get("liquidity_period") or state.get("phase") or "--"
    if session == "--" and "crypto" in (system_name or "").lower():
        session = "24/7 -- Always Active"    # crypto has no session concept
    i1d = state.get("indicators_1d") or {}
    i1h = state.get("indicators_1h") or {}
    i5m = state.get("indicators_5m") or {}
    perf = state.get("perf") or state.get("performance") or {}
    soq = state.get("stay_out_quality") or {}
    dec = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    cal = state.get("calendar")

    L = []
    a = L.append
    a(BAR)
    a("ARCHIE BRIEF -- %s %s" % (system_name, ver))
    a("Generated: %s UTC" % now_utc.strftime("%Y-%m-%d %H:%M:%S"))
    a(BAR)
    a("")
    a("MARKET")
    pnd = 2 if (isinstance(price, (int, float)) and price < 100) else 1
    a("%s %s | Session: %s" % (asset_label, _num(price, pnd), session))
    if cal:
        a(str(cal))
    a("")
    a("TREND")
    a("Daily:  SSL %s  RSI %s" % (_ssl(i1d), _num(i1d.get("rsi"), 1)))
    a("1-Hour: SSL %s  RSI %s  MACD %s  TMO %s  Chande MO %s  Money Flow %s" % (
        _ssl(i1h), _num(i1h.get("rsi"), 1), _num(i1h.get("macd"), 3),
        _num(i1h.get("tmo_main"), 2), _num(i1h.get("chande_mo"), 1), _num(i1h.get("money_flow"), 1)))
    a("5-Min:  SSL %s  RSI %s  MACD %s  TMO %s  Chande MO %s  Money Flow %s" % (
        _ssl(i5m), _num(i5m.get("rsi"), 1), _num(i5m.get("macd"), 3),
        _num(i5m.get("tmo_main"), 2), _num(i5m.get("chande_mo"), 1), _num(i5m.get("money_flow"), 1)))
    a("")
    a("ARTHUR")
    a("Decision: %s" % (state.get("arthur_decision") or "--"))
    ac = state.get("arthur_confidence")
    a("Confidence: %s | Liquidity Bias: %s" % (
        ac if ac is not None else "--", dec.get("liquidity_bias") or "--"))
    if dec.get("reasoning"):
        a("Reasoning: %s" % dec.get("reasoning"))
    a("")
    a("OPEN POSITION")
    ct = state.get("current_trade")
    if isinstance(ct, dict) and ct:
        a("Direction: %s | Entry: %s" % (ct.get("direction", "--"), _num(ct.get("entry_price"), pnd)))
        a("Stop: %s | Target: %s" % (_num(ct.get("stop_loss"), pnd), _num(ct.get("take_profit"), pnd)))
        # Points move vs entry (SHORT inverts). GasTrader moves in tiny increments
        # ($0.01-0.10/MMBtu) so show 3dp for it; 1dp is enough elsewhere (Snag 15).
        try:
            _entry = float(ct.get("entry_price"))
            _px = float(price)
            _pts = (_entry - _px) if (ct.get("direction") or "").upper() == "SHORT" else (_px - _entry)
            _ppd = 3 if "gas" in (system_name or "").lower() else 1
            a("Points: %+.*f" % (_ppd, _pts))
        except (TypeError, ValueError):
            pass
        pnl = state.get("unrealised_gbp")
        pnl = ct.get("pnl_gbp") if pnl is None else pnl
        a("P&L GBP: %s" % _num(pnl, 2))
        # Profit Protection Ladder status (Variant 2)
        try:
            _lstep = int(float(ct.get("ladder_step") or 0))
            _lfloor = float(ct.get("ladder_floor_gbp") or 0)
        except (TypeError, ValueError):
            _lstep, _lfloor = 0, 0.0
        if _lstep > 0:
            a("Ladder: Step %d active -- floor GBP %.2f" % (_lstep, _lfloor))
        else:
            a("Ladder: no step triggered yet")
    else:
        a("No open position")
    a("")
    a("MORGAN")
    start, cur = _morgan_journey(logs_dir) if logs_dir else (None, None)
    jr = ""
    if start is not None and cur is not None:
        jr = " | Journey: %s->%s" % (_num(start, 0), _num(cur, 0))
    a("Score: %s/100 (%s)%s" % (perf.get("confidence_score", "--"), perf.get("confidence_level", "--"), jr))
    a("")
    a("STAY OUT QUALITY")
    dcount = soq.get("decisions")
    if isinstance(dcount, list):
        dcount = len(dcount)
    if dcount is None:
        dcount = soq.get("correct", 0) + soq.get("wrong", 0) + soq.get("neutral", 0)
    a("Last %s decisions | Quality: %s%%" % (dcount,
      soq.get("quality_score") if soq.get("quality_score") is not None else "--"))
    a("Correct: %s | Wrong: %s | Neutral: %s" % (
        soq.get("correct", 0), soq.get("wrong", 0), soq.get("neutral", 0)))
    # Net Saved is always shown positive (money saved by correct stay-outs);
    # Net Missed always negative (money missed) -- matches the dashboard's
    # +abs(saved) / -abs(missed) display. Stored net_saved is negative.
    _sv = abs(float(soq.get("net_saved", 0) or 0))
    _ms = abs(float(soq.get("net_missed", 0) or 0))
    a("Net Saved: GBP +%s | Net Missed: GBP -%s" % (_num(_sv, 2), _num(_ms, 2)))
    _phantom_recent(soq, a, show_market=("crypto" in (system_name or "").lower()))
    a("")
    a("GUINEVERE")
    gkind = _guinevere_kind(system_name)
    if gkind == "news":
        _news_line(a)
    elif gkind == "calendar":
        a("Calendar only -- no news module (calendar shown under MARKET)")
    else:
        a("No Guinevere module")
    a("")
    a("LANCELOT PRE-CHECKS")
    pc = state.get("pre_checks")
    if isinstance(pc, dict) and pc:
        for k, v in pc.items():
            a("  [%s] %s" % ("PASS" if v else "FAIL", k))
    else:
        a("  %s" % (state.get("lancelot_status") or "n/a"))
    a("")
    a("SYSTEM")
    # CryptoTrader (TideTraderAI) exposes neither 'mode' nor 'connector_status'
    # in /api/state; it is Kraken paper trading and derives feed health from
    # last-update freshness (same as its dashboard). Map both robustly.
    mode = state.get("mode") or "PAPER"
    cs = state.get("connector_status")
    if cs:
        feed = "OK" if cs in ("capitalcom", "kraken", "ig", "yahoo") else cs
    elif state.get("last_update_utc") or state.get("last_update") or state.get("updated_at"):
        feed = "OK"
    else:
        feed = "--"
    a("Mode: %s | Version: %s | Feed: %s" % (mode, ver, feed))
    a(BAR)
    a("End of %s Archie Brief" % system_name)
    a(BAR)
    return "\n".join(L)


def build_roundtable_brief(agg, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    L = []
    a = L.append
    a(BAR)
    a("ARCHIE BRIEF -- ROUNDTABLE COMMAND CENTRE")
    a("Generated: %s UTC" % now_utc.strftime("%Y-%m-%d %H:%M:%S"))
    a(BAR)
    a("")
    a("PORTFOLIO")
    a("Total: GBP %s | Today: GBP %s" % (_num(agg.get("portfolio", 0), 2), _num(agg.get("today_pnl", 0), 2)))
    a("Systems: %s/%s running" % (agg.get("running", 0), agg.get("total", 6)))
    a("")
    a("SYSTEMS")
    opens = []
    for s in agg.get("systems", []):
        pxt = (s.get("price_html", "") or "")
        pxt = pxt.replace("£", "GBP ").replace("&pound;", "GBP ").replace("&nbsp;", " ").replace("&middot;", "-")
        a("%-13s %-8s %-24s %s" % (s.get("name", "?"), s.get("status", "?"), pxt, s.get("position", "")))
        conf = s.get("confidence")
        a("   locked=%s  today=GBP %s  bal=GBP %s  lancelot=%s  conf=%s" % (
            s.get("locked_pnl") if s.get("locked_pnl") is not None else "--",
            _num(s.get("daily_pnl", 0), 2), _num(s.get("balance", 0), 2),
            s.get("lancelot_status", "--"), conf if conf is not None else "--"))
        if (s.get("position") or "").upper() not in ("", "NO_TRADE", "FLAT", "NONE"):
            opens.append("%s %s (float GBP %s)" % (s.get("name"), s.get("position"),
                         _num(s.get("float_gbp", 0), 2)))
    a("")
    a("OPEN POSITIONS")
    a("\n".join(opens) if opens else "No open positions")
    a("")
    a("GAIUS")
    g = agg.get("gaius") or {}
    a("Collector:   %s  %s" % ("OK" if g.get("collector_ok") else "ERROR", g.get("collector_last") or "never"))
    a("Market data: %s  %s" % ("OK" if g.get("market_ok") else "ERROR", g.get("market_last") or "never"))
    a("")
    a("ALERTS")
    al = []
    for s in agg.get("systems", []):
        if s.get("status") not in ("RUNNING", "OK"):
            al.append("%s is %s" % (s.get("name"), s.get("status")))
        ls = s.get("lancelot_status") or ""
        if "FAIL" in ls:
            al.append("%s Lancelot: %s" % (s.get("name"), ls))
    a("\n".join(al) if al else "None")
    a(BAR)
    a("End of RoundTable Archie Brief")
    a(BAR)
    return "\n".join(L)
