"""半导体风险开关 (2026-10-03, 用户确认).

看多半导体的核心前提: "高利率只伤估值, 不伤盈利". 下面任一条件触发, 说明前提
可能被证伪 → 半导体 (thesis_config.semi_risk_guard.tickers) 的买入门槛自动提高
到 min_confidence (canonical 10 分制; 5 分制下即满分 5/5 才允许买入).

  earnings_revisions_down  大市值龙头的盈利预期 30 天内转为下调
                           (yfinance eps_trend: 今年 + 明年 EPS 一致预期, 市值加权)
  yield_5p5_real_rising    10 年期名义收益率 > 5.5% 且 10 年期实际利率 (TIPS) 60 天上行
  hy_spread_widening       高收益债利差 (ICE BofA HY OAS) 60 天走阔 ≥ 75bp

运行: 每天美股收盘后由 WebUI 看门狗白名单任务 (_semi_risk_guard.bat) 调用,
结果写 signals/semi_risk_guard.json. 读取方 thesis_config.semi_risk_guard_state():
文件超过 STALE_DAYS 天或数据不全 → 视为 "未知", 不额外限制 (只告警).
只改买入门槛, 不卖出, 不改仓位.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AGENTS = Path(__file__).resolve().parent
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

STATE_PATH = AGENTS / "signals" / "semi_risk_guard.json"
STALE_DAYS = 4
# 标普 500 权重最大的公司 (盈利预期的代理; 市值加权)
MEGA_CAPS = ("NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSLA",
             "BRK-B", "JPM", "LLY", "WMT", "V", "ORCL", "XOM")
DEFAULT_CONDITIONS = {
    "earnings_revisions_down": {"metric": "mega_eps_revision_30d_pct", "op": "<", "threshold": -0.5},
    "yield_5p5_real_rising": {"metric": "dgs10_pct", "op": ">", "threshold": 5.5,
                              "and": {"metric": "dfii10_60d_delta_bps", "op": ">", "threshold": 0}},
    "hy_spread_widening": {"metric": "hy_oas_60d_delta_bps", "op": ">=", "threshold": 75},
}


def _cmp(actual, op, threshold) -> bool:
    a, t = float(actual), float(threshold)
    return {"<": a < t, "<=": a <= t, ">": a > t, ">=": a >= t}[op]


def evaluate(metrics: dict, conditions: dict | None = None) -> dict:
    """→ {triggered: [id], unknown: [id], active: bool, details}. 缺数据的条件记 unknown."""
    conditions = conditions or DEFAULT_CONDITIONS
    triggered, unknown, details = [], [], {}
    for cid, c in conditions.items():
        parts = [c] + ([c["and"]] if c.get("and") else [])
        vals = [metrics.get(p["metric"]) for p in parts]
        if any(v is None for v in vals):
            unknown.append(cid)
            details[cid] = {"status": "unknown"}
            continue
        hit = all(_cmp(v, p["op"], p["threshold"]) for v, p in zip(vals, parts))
        details[cid] = {"status": "triggered" if hit else "ok",
                        "values": {p["metric"]: v for v, p in zip(vals, parts)}}
        if hit:
            triggered.append(cid)
    return {"triggered": triggered, "unknown": unknown, "active": bool(triggered),
            "details": details}


def mega_eps_revision(trends: dict[str, dict], caps: dict[str, float]) -> float | None:
    """市值加权的 30 天 EPS 预期修正 (%). trends[tk] = {"cur": [今年, 明年], "ago30": [...]}"""
    num = den = 0.0
    for tk, t in trends.items():
        cap = caps.get(tk)
        revs = [(c / a - 1) * 100 for c, a in zip(t.get("cur", []), t.get("ago30", []))
                if c is not None and a is not None and a > 0]
        if not cap or not revs:
            continue
        num += cap * sum(revs) / len(revs)
        den += cap
    return round(num / den, 3) if den else None


# ---------------- 联网部分 (Windows 上运行) ----------------
def _fred_series(sid: str, limit: int = 90) -> list[tuple[str, float]]:
    import urllib.request
    from config import FRED_API_KEY
    if not FRED_API_KEY:
        return []
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
           f"&api_key={FRED_API_KEY}&file_type=json&sort_order=desc&limit={limit}")
    with urllib.request.urlopen(url, timeout=20) as r:
        obs = json.loads(r.read()).get("observations", [])
    return [(o["date"], float(o["value"])) for o in obs if o.get("value") not in ("", ".", None)]


def _delta_bps(series: list[tuple[str, float]], lag: int = 60) -> float | None:
    if len(series) <= lag:
        return None
    return round((series[0][1] - series[lag][1]) * 100, 1)


def fetch_metrics() -> tuple[dict, dict]:
    metrics, status = {}, {}
    try:
        d10 = _fred_series("DGS10", 10)
        metrics["dgs10_pct"] = d10[0][1] if d10 else None
        status["DGS10"] = d10[0][0] if d10 else "unavailable"
    except Exception as e:
        status["DGS10"] = f"error: {e}"
    for sid, key in (("DFII10", "dfii10_60d_delta_bps"), ("BAMLH0A0HYM2", "hy_oas_60d_delta_bps")):
        try:
            s = _fred_series(sid, 90)
            metrics[key] = _delta_bps(s)
            if sid == "BAMLH0A0HYM2" and s:
                metrics["hy_oas_pct"] = s[0][1]
            status[sid] = s[0][0] if s else "unavailable"
        except Exception as e:
            status[sid] = f"error: {e}"
    try:
        import yfinance as yf
        trends, caps = {}, {}
        for tk in MEGA_CAPS:
            try:
                t = yf.Ticker(tk)
                et = t.get_eps_trend()
                rows = [r for r in ("0y", "+1y") if r in et.index]
                trends[tk] = {"cur": [float(et.loc[r, "current"]) for r in rows],
                              "ago30": [float(et.loc[r, "30daysAgo"]) for r in rows]}
                caps[tk] = float(t.fast_info["marketCap"])
            except Exception:
                continue
            time.sleep(0.5)
        metrics["mega_eps_revision_30d_pct"] = mega_eps_revision(trends, caps)
        status["eps_trend"] = f"{len(trends)}/{len(MEGA_CAPS)} tickers"
    except Exception as e:
        status["eps_trend"] = f"error: {e}"
    return metrics, status


def run(state_path: Path = STATE_PATH, fetch=fetch_metrics, notify=True) -> dict:
    from thesis_config import semi_risk_guard_config
    cfg = semi_risk_guard_config() or {}
    prev = {}
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    metrics, status = fetch()
    ev = evaluate(metrics, cfg.get("conditions") or None)
    state = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "metrics": metrics, "data_status": status, **ev}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path)
    print(json.dumps(state, ensure_ascii=False))
    if notify and bool(prev.get("active")) != state["active"]:
        try:
            from notifications import send_alert
            msg = (f"半导体风险开关 {'开启' if state['active'] else '解除'}: "
                   f"触发 {', '.join(state['triggered']) or '无'}"
                   + (f" → 半导体买入门槛提高到 min_confidence={cfg.get('min_confidence')}"
                      if state["active"] else ""))
            send_alert(msg, level="warning" if state["active"] else "recovery", dedup=True)
        except Exception as e:
            print(f"[notify] {e}")
    return state


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    # 后台无控制台运行时 stdout 可能丢失 → 异常写进自己的日志
    try:
        run()
    except Exception:
        import traceback
        log = AGENTS / "logs" / "semi_risk_guard_py.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"===== {datetime.now(timezone.utc).isoformat()} =====\n{traceback.format_exc()}\n")
        raise SystemExit(1)
