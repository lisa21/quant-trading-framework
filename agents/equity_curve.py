"""账户收益曲线 (2026-10-06, 用户要求公开页显示).

数据源: signals/nav_history.jsonl (paper_trader 每次同步时追加的账户净值).
  · 按美东交易日取当天最后一条, 去掉周末重复点.
  · 记录有断档 (例如 7/1→7/17 没有记录): 不插值, 标记 gap_before_days,
    前端用虚线连接, 让人看得出这段是"没数据", 不是"走得平".
  · 基准 SPY 同期: 收盘价按首个记录日缩放到同一起点 (只用于比较).
  · 历史峰值 (peak) 来自记录开始前, 单独标注, 不混进记录期内最大回撤.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

GAP_DAYS = 5            # 相邻两个记录日 > 5 个自然日 → 视为断档
SPY_CACHE_TTL_SEC = 6 * 3600
SPY_FETCH_TIMEOUT_SEC = 20


def _to_et_date(ts: str) -> str | None:
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # Windows 无 tzdata 时: 用 EDT 固定偏移近似
        et = dt.astimezone(timezone(timedelta(hours=-4)))
    return et.date().isoformat()


def load_nav_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def daily_nav(rows: list[dict]) -> list[dict]:
    """美东交易日 → 当天最后一条记录 (周末记录丢弃, 它们只是周五的重复)."""
    by_day: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: str(r.get("ts", ""))):
        nav = r.get("nav")
        d = _to_et_date(str(r.get("ts", "")))
        if d is None or not isinstance(nav, (int, float)) or nav <= 0:
            continue
        if datetime.fromisoformat(d).weekday() >= 5:
            continue
        by_day[d] = r
    return [{"date": d, "nav": float(by_day[d]["nav"]),
             "peak": by_day[d].get("peak"), "dd_pct": by_day[d].get("dd_pct")}
            for d in sorted(by_day)]


def build_equity_curve(rows: list[dict], spy_closes: dict[str, float] | None = None,
                       gap_days: int = GAP_DAYS) -> dict:
    days = daily_nav(rows)
    if not days:
        return {"exists": False, "points": []}
    nav0 = days[0]["nav"]

    spy_closes = spy_closes or {}
    spy_dates = sorted(spy_closes)

    def spy_on(d: str) -> float | None:
        # 当天没有收盘价 (假日/数据缺) → 用之前最近一个
        best = None
        for sd in spy_dates:
            if sd <= d:
                best = spy_closes[sd]
            else:
                break
        return best

    spy0 = spy_on(days[0]["date"])
    points = []
    run_peak = 0.0
    max_dd = 0.0
    max_dd_date = None
    prev_date = None
    for p in days:
        run_peak = max(run_peak, p["nav"])
        dd = (p["nav"] / run_peak - 1) * 100
        if dd < max_dd:
            max_dd, max_dd_date = dd, p["date"]
        gap = 0
        if prev_date is not None:
            gap = (datetime.fromisoformat(p["date"]) - datetime.fromisoformat(prev_date)).days
        spy = spy_on(p["date"])
        points.append({
            "date": p["date"],
            "nav": round(p["nav"], 2),
            "ret_pct": round((p["nav"] / nav0 - 1) * 100, 2),
            "dd_pct": round(dd, 2),
            "spy_scaled": round(nav0 * spy / spy0, 2) if (spy and spy0) else None,
            "gap_before_days": gap if gap > gap_days else 0,
        })
        prev_date = p["date"]

    last = points[-1]
    spy_ret = None
    if spy0 and last["spy_scaled"] is not None:
        spy_ret = round((last["spy_scaled"] / nav0 - 1) * 100, 2)
    gaps = [{"from": points[i - 1]["date"], "to": pt["date"], "days": pt["gap_before_days"]}
            for i, pt in enumerate(points) if pt["gap_before_days"]]
    stored_peak = max((p.get("peak") or 0) for p in days) or None
    return {
        "exists": True,
        "start_date": points[0]["date"],
        "end_date": last["date"],
        "start_nav": round(nav0, 2),
        "current_nav": last["nav"],
        "period_return_pct": last["ret_pct"],
        "spy_return_pct": spy_ret,
        "excess_pp": round(last["ret_pct"] - spy_ret, 2) if spy_ret is not None else None,
        "max_dd_in_record_pct": round(max_dd, 2),
        "max_dd_date": max_dd_date,
        # 历史峰值来自记录开始之前 (nav_history 没有那段明细)
        "all_time_peak": round(stored_peak, 2) if stored_peak else None,
        "dd_from_all_time_peak_pct": days[-1].get("dd_pct"),
        "gaps": gaps,
        "points": points,
        "note": "按美东交易日取当日最后一次记录; 虚线=无记录区间, 不代表走平; SPY 按首日缩放, 仅作对比",
    }


# ── SPY 收盘价 (yfinance, 文件缓存) ─────────────────────────────────────────
def fetch_spy_closes(start_date: str, cache_path: Path) -> dict[str, float]:
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (time.time() - cached.get("fetched_at", 0) < SPY_CACHE_TTL_SEC
                and cached.get("start") <= start_date):
            return cached.get("closes") or {}
    except Exception:
        cached = None

    box: dict = {}

    def _run():
        try:
            import yfinance as yf
            start = (datetime.fromisoformat(start_date) - timedelta(days=7)).date().isoformat()
            df = yf.Ticker("SPY").history(start=start, interval="1d", auto_adjust=True)
            box["v"] = {idx.strftime("%Y-%m-%d"): round(float(c), 4)
                        for idx, c in df["Close"].items()}
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(SPY_FETCH_TIMEOUT_SEC)
    closes = box.get("v")
    if closes:
        try:
            cache_path.write_text(json.dumps({"fetched_at": time.time(), "start": start_date,
                                              "closes": closes}), encoding="utf-8")
        except Exception:
            pass
        return closes
    # 拉取失败 → 用旧缓存 (哪怕过期), 再不行就没有基准
    return (cached or {}).get("closes") or {}


def compute(signals_dir: Path) -> dict:
    rows = load_nav_rows(signals_dir / "nav_history.jsonl")
    days = daily_nav(rows)
    if not days:
        return {"exists": False, "points": []}
    spy = fetch_spy_closes(days[0]["date"], signals_dir / "spy_daily_cache.json")
    out = build_equity_curve(rows, spy)
    out["benchmark_ok"] = bool(spy)
    return out


if __name__ == "__main__":
    import sys
    sd = Path(__file__).resolve().parent / "signals"
    res = compute(sd) if "--spy" in sys.argv else build_equity_curve(load_nav_rows(sd / "nav_history.jsonl"))
    print(json.dumps({k: v for k, v in res.items() if k != "points"}, ensure_ascii=False, indent=2))
    print(len(res.get("points", [])), "points")
