"""供给冲击日历 (2026-10-05, 用户确认): 减持 / 增发 / 解禁 + 指数调仓日.

为什么: "砸盘" 多数来自可预见的集中供给 —— 投行盘后折价包销大宗、IPO 锁定期
到期、指数调仓与期权大到期. 这里把它们做成日历, 决策时:
  · 个股供给事件窗口内 → 新买入降级 HOLD (supply_guard), 不卖出, 不改仓位;
  · 市场级资金流日 (指数调仓 / 季度期权到期 / 季末) → 只在决策里标注 flow_day.

数据 (只读, 免费):
  · SEC EDGAR submissions (每家公司的申报列表, 带申报日):
      424B7         → 股东转售 (secondary)         窗口: 申报日起 3 个交易日
      424B4 / 424B5 → 增发 / 包销 (排除债券)       窗口: 申报日起 3 个交易日
      IPO 424B4     → 锁定期到期 ≈ IPO + 180 天    窗口: 到期前 3 个交易日 ~ 后 1 个交易日
  · 指数日历: 标普季度调仓 = 3/6/9/12 月第三个周五 (同季度期权到期);
    罗素重组 (2026 起半年一次) 用官方公布日期, 未公布的不猜.

运行: WebUI 看门狗白名单任务每天收盘后 (_supply_calendar.bat), 输出
signals/supply_calendar.json. 读取方超过 STALE_DAYS 天未更新 → 视为未知, 不拦截.
已知局限: 锁定期按 180 天估算 (部分公司分批解禁或提前解禁); 424B5 债券判断靠文件
描述关键词; Form 144 (内部人减持预告) 未纳入.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

AGENTS = Path(__file__).resolve().parent
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

OUT_PATH = AGENTS / "signals" / "supply_calendar.json"
STALE_DAYS = 3
LOOKBACK_DAYS = 30
LOCKUP_DAYS = 180
OFFERING_FORMS = {"424B4", "424B5", "424B7"}
DEBT_WORDS = ("NOTE", "DEBT", "BOND", "DEBENTURE", "SENIOR", "PREFERRED")
# 罗素美国指数重组生效日 (收盘后生效). 来源: LSEG/FTSE Russell 公告. 只写已公布的.
RUSSELL_RECON = {
    "2026-12-11": "https://www.lseg.com/en/media-centre/press-releases/ftse-russell/2026/"
                  "ftse-russell-announces-december-2026-russell-us-indexes-reconstitution-schedule",
}


# ---------------- 交易日 ----------------
def _is_session(d: date) -> bool:
    try:
        from regime_today import is_nyse_session
        return is_nyse_session(d)
    except Exception:
        return d.weekday() < 5


def add_sessions(d: date, n: int) -> date:
    """d 起第 n 个交易日 (n 可为负); d 本身不是交易日时先挪到下一个交易日."""
    step = 1 if n >= 0 else -1
    cur = d
    while not _is_session(cur):
        cur += timedelta(days=1)
    k = abs(n)
    while k:
        cur += timedelta(days=step)
        if _is_session(cur):
            k -= 1
    return cur


# ---------------- 解析 EDGAR submissions ----------------
def _rows(sub: dict) -> list[dict]:
    r = (sub.get("filings") or {}).get("recent") or {}
    keys = ("form", "filingDate", "accessionNumber", "primaryDocument", "primaryDocDescription")
    cols = [r.get(k) or [] for k in keys]
    n = min((len(c) for c in cols[:3]), default=0)
    out = []
    for i in range(n):
        out.append({k: (c[i] if i < len(c) else "") for k, c in zip(keys, cols)})
    return out


def _url(cik: int, row: dict) -> str:
    acc = str(row.get("accessionNumber", "")).replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{row.get('primaryDocument', '')}"


def events_from_submissions(sub: dict, today: date, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """一家公司的供给事件 (只用 today 当天及之前的申报)."""
    cik = int(sub.get("cik") or 0)
    rows = [r for r in _rows(sub) if r["filingDate"] and r["filingDate"] <= today.isoformat()]
    periodic = sorted(r["filingDate"] for r in rows if r["form"] in ("10-K", "10-Q", "20-F"))
    out = []
    for r in rows:
        form, fd = r["form"], date.fromisoformat(r["filingDate"])
        if form not in OFFERING_FORMS:
            continue
        desc = f"{r.get('primaryDocDescription', '')} {r.get('primaryDocument', '')}".upper()
        if form == "424B5" and any(w in desc for w in DEBT_WORDS):
            continue
        is_ipo = form == "424B4" and not any(p < r["filingDate"] for p in periodic)
        if is_ipo:
            lock = fd + timedelta(days=LOCKUP_DAYS)
            if lock + timedelta(days=10) >= today:
                out.append({"type": "lockup_expiry", "date": lock.isoformat(),
                            "window": [add_sessions(lock, -3).isoformat(), add_sessions(lock, 1).isoformat()],
                            "form": form, "ipo_filing_date": fd.isoformat(), "url": _url(cik, r),
                            "note": f"IPO {fd} + {LOCKUP_DAYS} 天 (估算)"})
            continue
        if (today - fd).days <= lookback_days:
            out.append({"type": "secondary_sale" if form == "424B7" else "offering",
                        "date": fd.isoformat(),
                        "window": [fd.isoformat(), add_sessions(fd, 2).isoformat()],
                        "form": form, "url": _url(cik, r)})
    return sorted(out, key=lambda e: e["date"])


def market_flow_days(today: date, horizon_days: int = 120) -> list[dict]:
    """标普季度调仓 / 季度期权到期 (3/6/9/12 月第三个周五), 罗素重组, 季末."""
    out = []
    end = today + timedelta(days=horizon_days)
    y, m = today.year, today.month
    while date(y, m, 1) <= end:
        if m in (3, 6, 9, 12):
            first = date(y, m, 1)
            fri = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
            if not _is_session(fri):
                fri = add_sessions(fri, -1)
            out.append({"type": "sp500_rebalance_quad_witching", "date": fri.isoformat(),
                        "note": "标普季度调仓收盘生效 + 季度期权到期"})
            last = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
            while not _is_session(last):
                last -= timedelta(days=1)
            out.append({"type": "quarter_end", "date": last.isoformat(), "note": "季末机构再平衡"})
        m += 1
        if m == 13:
            y, m = y + 1, 1
    for d, src in RUSSELL_RECON.items():
        out.append({"type": "russell_reconstitution", "date": d, "note": "罗素重组收盘生效", "source": src})
    return sorted([e for e in out if today.isoformat() <= e["date"] <= end.isoformat()],
                  key=lambda e: e["date"])


# ---------------- 决策用读取 ----------------
_CACHE: dict = {"mtime": None, "data": None}


def load(path: Path | None = None) -> dict | None:
    p = Path(path) if path is not None else OUT_PATH
    if not p.exists():
        return None
    mt = p.stat().st_mtime
    if path is None and _CACHE["mtime"] == mt:
        return _CACHE["data"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    data["_age_days"] = (time.time() - mt) / 86400
    if path is None:
        _CACHE.update(mtime=mt, data=data)
    return data


def active_events(ticker: str, today: date, data: dict | None) -> list[dict]:
    if not data or data.get("_age_days", 0) > STALE_DAYS:
        return []
    tk = ticker.replace("US.", "").upper()
    t = today.isoformat()
    return [e for e in (data.get("tickers") or {}).get(tk, [])
            if e.get("window") and e["window"][0] <= t <= e["window"][1]]


def flow_days_near(today: date, data: dict | None, days: int = 1) -> list[dict]:
    if not data or data.get("_age_days", 0) > STALE_DAYS:
        return []
    lo, hi = (today - timedelta(days=days)).isoformat(), (today + timedelta(days=days)).isoformat()
    return [e for e in data.get("market_days", []) if lo <= e["date"] <= hi]


# ---------------- 联网 (Windows) ----------------
def _ua() -> str:
    p = AGENTS / ".sec_user_agent"
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _get_json(url: str, ua: str):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def tracked_tickers() -> list[str]:
    from config import TRADE_ELIGIBLE_TICKERS
    tks = {t.replace("US.", "") for t in TRADE_ELIGIBLE_TICKERS}
    try:
        st = json.loads((AGENTS / "universe_state.json").read_text(encoding="utf-8"))
        tks |= {str(p.get("ticker") if isinstance(p, dict) else p).replace("US.", "")
                for p in st.get("picks") or []}
    except Exception:
        pass
    return sorted(t for t in tks if t)


def run(today: date | None = None, out_path: Path = OUT_PATH) -> dict:
    today = today or datetime.now(timezone.utc).date()
    ua = _ua()
    if "@" not in ua:
        raise SystemExit("agents/.sec_user_agent 缺失 (SEC 要求请求头带联系方式)")
    tick = _get_json("https://www.sec.gov/files/company_tickers_exchange.json", ua)
    cik_of = {str(r[2]).upper(): int(r[0]) for r in tick.get("data", [])}
    tickers, status = {}, {}
    for tk in tracked_tickers():
        cik = cik_of.get(tk)
        if not cik:
            status[tk] = "no_cik"
            continue
        try:
            sub = _get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", ua)
            ev = events_from_submissions(sub, today)
            if ev:
                tickers[tk] = ev
            status[tk] = "ok"
        except Exception as e:
            status[tk] = f"error: {e}"
        time.sleep(0.2)
    data = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": today.isoformat(), "tickers": tickers,
            "market_days": market_flow_days(today), "status": status}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    print(json.dumps({k: data[k] for k in ("generated_at", "tickers", "status")}, ensure_ascii=False)[:3000])
    return data


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception:
        import traceback
        log = AGENTS / "logs" / "supply_calendar_py.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"===== {datetime.now(timezone.utc).isoformat()} =====\n{traceback.format_exc()}\n")
        raise SystemExit(1)
