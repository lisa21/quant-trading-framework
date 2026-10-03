"""成长股 EPS 筛选回测 (2026-10-02).

问题: 欧奈尔 "600 只大牛股涨前 EPS 大增" 只看了赢家 (幸存者偏差). 这里反过来问:
**所有满足 EPS 条件的股票, 之后是否跑赢同期全市场?** 以及翻倍概率是否更高?

方法 (point-in-time, 月度):
- 每月末 t: 只用 t 前一天及之前已提交的 10-Q/10-K 数字 (growth_eps_screen).
- 股票池: 当月收盘 ≥ $5 且当月成交额 ≥ $1 亿 (约 $500 万/日) 的 NYSE/Nasdaq 股票.
- 入选股等权; 基准 = 同月股票池等权. 远期收益 3/6/12 个月 (复权月收盘).
- 样本内 2012-2018 / 样本外 2019 起; 准入沿用系统 research_validation 口径:
  样本外不重叠 3 个月期 ≥ 20 个, "组合跑赢基准" 比例的 Wilson 95% 单侧下界 > 50%,
  平均超额 > 0, 分年稳定性 (超额为正的年份占比) ≥ 0.5.

输入: growth_eps_data 生成的 <cache>/universe.csv, facts.csv.gz, prices_monthly.csv.gz
输出: development/<日期>/eps_growth/backtest_report.md + .json + current_picks.csv
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

AGENTS = Path(__file__).resolve().parent
ROOT = AGENTS.parent
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from growth_eps_screen import CompanyFacts, apply_rules, evaluate, metrics  # noqa: E402

HORIZONS = (1, 3, 6, 12)
IS_END = "2018-12-31"
MIN_PRICE = 5.0
MIN_DOLLAR_VOL_MONTH = 1e8
VARIANTS = {
    "C25":                {"min_eps_growth": 0.25},
    "C25+营收20":         {"min_eps_growth": 0.25, "min_sales_growth": 0.20},
    "C25+加速":           {"min_eps_growth": 0.25, "require_accel": True},
    "C25+3年年度增长":    {"min_eps_growth": 0.25, "require_annual": True},
    "C25 全条件":         {"min_eps_growth": 0.25, "min_sales_growth": 0.20, "require_accel": True,
                           "require_annual": True, "require_no_decel": True},
    "C50+营收25":         {"min_eps_growth": 0.50, "min_sales_growth": 0.25},
    "C100+营收25":        {"min_eps_growth": 1.00, "min_sales_growth": 0.25},
    # M = 大盘方向 (2026-10-03): 等权市场指数 < 10 个月均线时空仓 (收益记 0)
    "C50+营收25+M":       {"min_eps_growth": 0.50, "min_sales_growth": 0.25, "market_filter": True},
}
# —— 书中观点扩展测试 (2026-10-03) ——
_B = {"min_eps_growth": 0.50, "min_sales_growth": 0.25}
VARIANTS.update({
    "C50+营收25+SPY":          {**_B, "spy_filter": True},           # 标普 500 在 10 月均线上才买
    "C50+营收25+趋势":         {**_B, "trend": True},                # 鱼身: 股价在 10 月均线上且距 12 月高点 ≤15%
    "C50+营收25+SPY+趋势":     {**_B, "spy_filter": True, "trend": True},
    "C50+营收25 连续2季":      {**_B, "min_consecutive_q": 2, "min_consecutive_sales_q": 2},
    "C50+营收25 连续4季":      {**_B, "min_consecutive_q": 4, "min_consecutive_sales_q": 4},
    "C50+营收25+年度1年":      {**_B, "min_annual_up_years": 1},
    "C50+营收25+年度2年":      {**_B, "min_annual_up_years": 2},
    "C50+营收25+年度3年":      {**_B, "min_annual_up_years": 3},
    "Top4 按EPS增速":          {**_B, "top_n": 4, "rank_by": "eps"},
    "Top4 按6月动量":          {**_B, "top_n": 4, "rank_by": "mom6"},
    "Top4 按6月动量+SPY+趋势": {**_B, "top_n": 4, "rank_by": "mom6", "spy_filter": True, "trend": True},
})
TREND_NEAR_HIGH = 0.15
COST_ONE_WAY = 0.001             # 组合模拟: 单边 0.1% 交易成本
WATCH_RULE = "C50+营收25"         # 观察名单默认规则 (2026-10-03 回测样本外最稳)
MARKET_SMA_MONTHS = 10


def month_end(d: str) -> str:
    y, m = int(d[:4]), int(d[5:7])
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def wilson_lower(wins: int, n: int, z: float = 1.645) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def load(cache: Path):
    import pandas as pd
    uni = pd.read_csv(cache / "universe.csv")
    facts = pd.read_csv(cache / "facts.csv.gz", dtype={"start": str, "end": str, "filed": str})
    prices = pd.read_csv(cache / "prices_monthly.csv.gz")
    return uni, facts, prices


def build(uni, facts, prices):
    """→ companies {ticker: CompanyFacts}, close/dollar-volume 宽表 (行=月末)."""
    tick_by_cik = dict(zip(uni["cik"].astype(int), uni["ticker"]))
    companies = {}
    for cik, g in facts.groupby("cik"):
        tk = tick_by_cik.get(int(cik))
        if tk:
            companies[tk] = CompanyFacts(g.to_dict("records"))
    prices = prices.copy()
    prices["me"] = prices["date"].map(month_end)
    prices = prices.drop_duplicates(["me", "ticker"], keep="last")
    close = prices.pivot(index="me", columns="ticker", values="close").sort_index()
    dvol = (prices.assign(dv=prices["close"] * prices["volume"])
            .pivot(index="me", columns="ticker", values="dv").reindex(close.index))
    return companies, close, dvol


def market_trend(close, min_price: float = MIN_PRICE) -> dict[str, bool | None]:
    """月末 → 等权市场指数是否在 MARKET_SMA_MONTHS 个月均线之上 (只用当月及以前的价格)."""
    rets = []
    prev = None
    for m in close.index:
        row = close.loc[m]
        if prev is not None:
            ok = prev.notna() & row.notna() & (prev >= min_price)
            r = (row[ok] / prev[ok] - 1)
            rets.append(float(r.mean()) if len(r) else 0.0)
        else:
            rets.append(0.0)
        prev = row
    level, idx_vals, out = 1.0, [], {}
    for m, r in zip(close.index, rets):
        level *= 1 + r
        idx_vals.append(level)
        if len(idx_vals) >= MARKET_SMA_MONTHS:
            sma = sum(idx_vals[-MARKET_SMA_MONTHS:]) / MARKET_SMA_MONTHS
            out[m] = level > sma
        else:
            out[m] = None
    return out


def run_backtest(companies, close, dvol, variants=VARIANTS, start="2012-01-31",
                 last_month: str | None = None) -> dict:
    today = date.today().isoformat()
    months = [m for m in close.index if m >= start and m <= today
              and (last_month is None or m <= last_month)]   # 未走完的当月不参与回测
    idx = {m: i for i, m in enumerate(close.index)}
    cache: dict[tuple, dict] = {}
    per_month = {v: [] for v in variants}
    stock_rows = {v: [] for v in variants}
    universe_rows = []
    trend = market_trend(close) if any(v.get("market_filter") for v in variants.values()) else {}
    # 价格特征 (只用当月及以前): 10 月均线 / 12 月最高 / 6 月动量; SPY 方向
    sma10 = close.rolling(10, min_periods=10).mean()
    hi12 = close.rolling(12, min_periods=6).max()
    mom6 = close / close.shift(6) - 1
    spy_up = {}
    if "SPY" in close.columns:
        for m_, c_, s_ in zip(close.index, close["SPY"], sma10["SPY"]):
            spy_up[m_] = None if (c_ != c_ or s_ != s_) else bool(c_ > s_)
    holdings = {v: [] for v in variants}
    for m in months:
        i = idx[m]
        as_of = (date.fromisoformat(m) - timedelta(days=1)).isoformat()
        px = close.iloc[i].to_dict()
        dv = dvol.iloc[i].to_dict()
        liquid = [tk for tk in close.columns
                  if tk in companies and px[tk] == px[tk] and px[tk] >= MIN_PRICE
                  and (dv.get(tk) or 0) >= MIN_DOLLAR_VOL_MONTH]
        fwd = {}
        for h in HORIZONS:
            fwd[h] = {}
            if i + h < len(close.index):
                fut = close.iloc[i + h].to_dict()
                for tk in liquid:
                    f = fut.get(tk)
                    if f == f and f is not None:
                        fwd[h][tk] = f / px[tk] - 1
        uni_mean = {h: (sum(fwd[h].values()) / len(fwd[h]) if fwd[h] else None) for h in HORIZONS}
        universe_rows.append({"month": m, "n": len(liquid), **{f"r{h}": uni_mean[h] for h in HORIZONS},
                              "p_double12": (sum(1 for v in fwd[12].values() if v >= 1.0) / len(fwd[12])
                                             if fwd[12] else None)})
        mets = {}
        for tk in liquid:
            cf = companies[tk]
            key = (tk, cf.state_key(as_of))
            if key not in cache:
                cache[key] = metrics(cf, as_of)
            mets[tk] = cache[key]
        s10, h12, m6 = sma10.iloc[i].to_dict(), hi12.iloc[i].to_dict(), mom6.iloc[i].to_dict()
        for vname, rules in variants.items():
            picks = [tk for tk in liquid if apply_rules(mets[tk], as_of, rules)["pass"]]
            if rules.get("trend"):
                picks = [tk for tk in picks
                         if s10.get(tk) == s10.get(tk) and s10.get(tk) is not None and px[tk] > s10[tk]
                         and h12.get(tk) == h12.get(tk) and h12.get(tk) is not None
                         and px[tk] >= h12[tk] * (1 - TREND_NEAR_HIGH)]
            if rules.get("top_n"):
                key = rules.get("rank_by", "eps")
                def _score(tk):
                    if key == "mom6":
                        v = m6.get(tk)
                        return v if v == v and v is not None else -1e9
                    g = (mets[tk].get("eps_growth") or [None])[0]
                    return g if g is not None else -1e9
                picks = sorted(picks, key=_score, reverse=True)[:int(rules["top_n"])]
            row = {"month": m, "n": len(picks)}
            off = ((rules.get("market_filter") and trend.get(m) is False)
                   or (rules.get("spy_filter") and spy_up.get(m) is False))
            holdings[vname].append((m, [] if off else list(picks)))
            if off:
                # 大盘向下: 空仓, 收益 0, 超额 = −市场
                row["n"] = 0
                for h in HORIZONS:
                    row[f"r{h}"] = 0.0 if uni_mean[h] is not None else None
                    row[f"x{h}"] = -uni_mean[h] if uni_mean[h] is not None else None
                per_month[vname].append(row)
                continue
            for h in HORIZONS:
                rs = [fwd[h][tk] for tk in picks if tk in fwd[h]]
                row[f"r{h}"] = sum(rs) / len(rs) if rs else None
                row[f"x{h}"] = (row[f"r{h}"] - uni_mean[h]
                                if rs and uni_mean[h] is not None else None)
            per_month[vname].append(row)
            for tk in picks:
                if tk in fwd[12]:
                    stock_rows[vname].append(fwd[12][tk])
    spy_r1 = {}
    if "SPY" in close.columns:
        for m in months:
            i = idx[m]
            if i + 1 < len(close.index):
                a_, b_ = close["SPY"].iloc[i], close["SPY"].iloc[i + 1]
                if a_ == a_ and b_ == b_:
                    spy_r1[m] = float(b_ / a_ - 1)
    return {"months": months, "per_month": per_month, "universe": universe_rows,
            "stock_r12": stock_rows, "holdings": holdings, "spy_r1": spy_r1}


def _series_stats(rets: list[float]) -> dict:
    if not rets:
        return {"n_months": 0}
    level, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        level *= 1 + r
        peak = max(peak, level)
        mdd = min(mdd, level / peak - 1)
    n = len(rets)
    mean = sum(rets) / n
    sd = (sum((r - mean) ** 2 for r in rets) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {"n_months": n, "cagr_pct": round((level ** (12 / n) - 1) * 100, 1),
            "max_dd_pct": round(mdd * 100, 1),
            "sharpe": round(mean / sd * math.sqrt(12), 2) if sd > 0 else None,
            "total_pct": round((level - 1) * 100, 1)}


def portfolio_sim(bt: dict, lo: str | None = None) -> dict:
    """每月调仓、等权持有入选股 1 个月 (空仓 = 0), 扣单边 COST_ONE_WAY 成本."""
    uni = {r["month"]: r.get("r1") for r in bt["universe"]}
    out = {}
    for v, rows in bt["per_month"].items():
        hold = dict(bt["holdings"][v])
        prev, rets, nh = set(), [], []
        for r in rows:
            m = r["month"]
            if (lo and m < lo) or uni.get(m) is None:
                continue
            cur = set(hold.get(m, []))
            gross = r.get("r1") if cur else 0.0
            if gross is None:
                gross = 0.0
            if cur or prev:
                changed = len(cur ^ prev) / max(len(cur | prev), 1)
            else:
                changed = 0.0
            rets.append(gross - 2 * COST_ONE_WAY * changed)
            nh.append(len(cur))
            prev = cur
        out[v] = {**_series_stats(rets), "avg_holdings": round(sum(nh) / len(nh), 1) if nh else 0,
                  "months_in_cash_pct": round(sum(1 for x in nh if x == 0) / len(nh) * 100, 1) if nh else None}
    ms = [m for m in bt["months"] if (not lo or m >= lo) and uni.get(m) is not None]
    out["[基准] 全市场等权"] = _series_stats([uni[m] for m in ms])
    if bt.get("spy_r1"):
        out["[基准] SPY"] = _series_stats([bt["spy_r1"][m] for m in ms if m in bt["spy_r1"]])
    return out


def _summ(rows, h, lo=None, hi=None, step=1):
    sel = [r for r in rows if (lo is None or r["month"] >= lo) and (hi is None or r["month"] <= hi)
           and r.get(f"x{h}") is not None]
    sel = sel[::step]
    if not sel:
        return {"n": 0}
    xs = [r[f"x{h}"] for r in sel]
    wins = sum(1 for x in xs if x > 0)
    mean = sum(xs) / len(xs)
    sd = (sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5 if len(xs) > 1 else 0.0
    return {"n": len(xs), "avg_excess_pct": round(mean * 100, 2),
            "beat_rate_pct": round(wins / len(xs) * 100, 1),
            "wilson_lower_pct": round(wilson_lower(wins, len(xs)) * 100, 1),
            "t_stat": round(mean / (sd / math.sqrt(len(xs))), 2) if sd > 0 else None,
            "avg_picks": round(sum(r["n"] for r in sel) / len(sel), 1),
            "avg_ret_pct": round(sum(r[f"r{h}"] for r in sel) / len(sel) * 100, 2)}


def summarize(bt: dict) -> dict:
    out = {}
    uni = bt["universe"]
    u12 = [r["p_double12"] for r in uni if r["p_double12"] is not None]
    base_double = sum(u12) / len(u12) if u12 else None
    for v, rows in bt["per_month"].items():
        s = {f"all_{h}m": _summ(rows, h) for h in HORIZONS}
        s["IS_3m"] = _summ(rows, 3, hi=IS_END)
        s["OOS_3m"] = _summ(rows, 3, lo="2019-01-01")
        s["OOS_3m_nonoverlap"] = _summ(rows, 3, lo="2019-01-01", step=3)
        s["OOS_12m"] = _summ(rows, 12, lo="2019-01-01")
        years = {}
        for r in rows:
            if r.get("x3") is not None:
                years.setdefault(r["month"][:4], []).append(r["x3"])
        yearly = {y: round(sum(v) / len(v) * 100, 2) for y, v in sorted(years.items())}
        s["yearly_excess_3m_pct"] = yearly
        stab = sum(1 for x in yearly.values() if x > 0) / len(yearly) if yearly else 0.0
        s["stability"] = round(stab, 2)
        r12 = bt["stock_r12"][v]
        s["p_double_12m_pct"] = round(sum(1 for x in r12 if x >= 1.0) / len(r12) * 100, 2) if r12 else None
        s["p_double_12m_universe_pct"] = round(base_double * 100, 2) if base_double is not None else None
        oos = s["OOS_3m_nonoverlap"]
        checks = {"n>=20": oos.get("n", 0) >= 20,
                  "wilson_lower>50": oos.get("wilson_lower_pct", 0) > 50,
                  "avg_excess>0": oos.get("avg_excess_pct", 0) > 0,
                  "stability>=0.5": stab >= 0.5}
        s["admission"] = {"pass": all(checks.values()),
                          "failed": [k for k, ok in checks.items() if not ok]}
        out[v] = s
    return out


def current_picks(companies, close, dvol, rules, as_of: str | None = None) -> list[dict]:
    """最新一个月的入选名单 (按 EPS 增速排序)."""
    as_of = as_of or date.today().isoformat()
    # 最后一根月线可能是未走完的当月 (成交额偏小) → 流动性用最近一个完整月
    li = -2 if close.index[-1] > as_of and len(close.index) > 1 else -1
    m = close.index[-1]
    px = close.iloc[-1].where(close.iloc[-1].notna(), close.iloc[li])
    dv = dvol.iloc[li]
    out = []
    for tk, cf in companies.items():
        if tk not in close.columns or not (px[tk] == px[tk] and px[tk] >= MIN_PRICE
                                           and (dv[tk] or 0) >= MIN_DOLLAR_VOL_MONTH):
            continue
        res = evaluate(cf, as_of, rules)
        if res["pass"]:
            g = res.get("eps_growth") or [None]
            out.append({"ticker": tk, "price_month": m, "close": round(float(px[tk]), 2),
                        "last_quarter_end": res["last_quarter_end"],
                        "eps_growth_pct": round(g[0] * 100, 1),
                        "prev_eps_growth_pct": (round(g[1] * 100, 1) if len(g) > 1 and g[1] is not None else None),
                        "sales_growth_pct": (round(res["sales_growth"] * 100, 1)
                                             if res.get("sales_growth") is not None else None)})
    return sorted(out, key=lambda r: -r["eps_growth_pct"])


WATCH_PATH = AGENTS / "signals" / "growth_watch.json"
WATCH_LOG_PATH = AGENTS / "signals" / "growth_watch_log.jsonl"
WATCH_LOG_MIN_DAYS = 6


def _latest_prices(close, dvol, as_of: str) -> tuple[dict, dict]:
    """最新价格 (当月未完结时取当月最新收盘) 与基准股票池 (最近完整月流动性达标)."""
    li = -2 if close.index[-1] > as_of and len(close.index) > 1 else -1
    last = close.iloc[-1].where(close.iloc[-1].notna(), close.iloc[li])
    dv = dvol.iloc[li]
    prices = {tk: float(v) for tk, v in last.items() if v == v}
    bench = {tk: px for tk, px in prices.items()
             if px >= MIN_PRICE and (dv.get(tk) or 0) >= MIN_DOLLAR_VOL_MONTH}
    return prices, bench


def shadow_performance(log: list[dict], prices: dict) -> list[dict]:
    """观察名单的影子收益: 记录时价格 → 最新价格; 基准 = 记录时流动性股票池等权."""
    out = []
    for e in log:
        pr = [prices[p["ticker"]] / p["close"] - 1 for p in e.get("picks", [])
              if p.get("close") and prices.get(p["ticker"])]
        br = [prices[tk] / px - 1 for tk, px in (e.get("benchmark_prices") or {}).items()
              if px and prices.get(tk)]
        if not pr or not br:
            continue
        a, b = sum(pr) / len(pr), sum(br) / len(br)
        out.append({"date": e["date"], "rule": e.get("rule"), "market_up": e.get("market_up"),
                    "n": len(pr), "ret_pct": round(a * 100, 2), "bench_pct": round(b * 100, 2),
                    "excess_pct": round((a - b) * 100, 2)})
    return out


def update_watch(picks: list[dict], close, dvol, rule: str, market_up, today: str | None = None,
                 watch_path: Path = WATCH_PATH, log_path: Path = WATCH_LOG_PATH) -> dict:
    """写观察名单 (不下单) + 每周一条影子记录 + 历史记录的跟踪收益."""
    today = today or date.today().isoformat()
    prices, bench = _latest_prices(close, dvol, today)
    log = []
    if log_path.exists():
        log = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    last = log[-1]["date"] if log else None
    if last is None or (date.fromisoformat(today) - date.fromisoformat(last)).days >= WATCH_LOG_MIN_DAYS:
        entry = {"date": today, "rule": rule, "market_up": market_up,
                 "picks": [{"ticker": p["ticker"], "close": prices.get(p["ticker"])} for p in picks],
                 "benchmark_prices": bench}
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.append(entry)
    watch = {"generated_at": datetime.now().isoformat(timespec="seconds"), "rule": rule,
             "market_up": market_up,
             "note": ("观察名单 (影子跟踪), 不会自动下单. market_up = 等权市场指数是否在 10 个月均线上方, "
                      "仅供参考: 2026-10-03 回测中按它空仓反而降低收益 (C50+营收25+M 未通过准入)."),
             "picks": picks, "shadow": shadow_performance(log, prices)}
    tmp = watch_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(watch, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(watch_path)
    return watch


def render(summary: dict, meta: dict, picks: list[dict], sims: dict | None = None) -> str:
    L = ["# 成长股 EPS 筛选回测", "",
         f"生成 {meta['generated']} · 区间 {meta['first_month']} ~ {meta['last_month']} · "
         f"月均股票池 {meta['avg_universe']} 只 · 样本内 ≤{IS_END[:4]}, 样本外 2019 起", "",
         "超额 = 入选股等权远期收益 − 同月股票池等权远期收益. 跑赢率 = 超额 > 0 的月份占比.", "",
         "| 规则 | 月均入选 | 3月超额 | 6月超额 | 12月超额 | 3月跑赢率 | 样本内3月超额 (t) | 样本外3月超额 (t) | 样本外不重叠 n / Wilson下界 | 12月翻倍率 (全市场) | 稳定性 | 准入 |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for v, s in summary.items():
        a3, a6, a12 = s["all_3m"], s["all_6m"], s["all_12m"]
        oos, nov = s["OOS_3m"], s["OOS_3m_nonoverlap"]
        L.append(f"| {v} | {a3.get('avg_picks', 0)} | {a3.get('avg_excess_pct', '—')}% | "
                 f"{a6.get('avg_excess_pct', '—')}% | {a12.get('avg_excess_pct', '—')}% | "
                 f"{a3.get('beat_rate_pct', '—')}% | {s['IS_3m'].get('avg_excess_pct', '—')}% ({s['IS_3m'].get('t_stat')}) | "
                 f"{oos.get('avg_excess_pct', '—')}% ({oos.get('t_stat')}) | "
                 f"{nov.get('n', 0)} / {nov.get('wilson_lower_pct', '—')}% | "
                 f"{s['p_double_12m_pct']}% ({s['p_double_12m_universe_pct']}%) | {s['stability']} | "
                 f"{'通过' if s['admission']['pass'] else '未通过: ' + ', '.join(s['admission']['failed'])} |")
    L += ["", "## 分年 3 个月超额 (%)", "", "| 规则 | " + " | ".join(next(iter(summary.values()))["yearly_excess_3m_pct"]) + " |",
          "|---|" + "---:|" * len(next(iter(summary.values()))["yearly_excess_3m_pct"])]
    for v, s in summary.items():
        L.append(f"| {v} | " + " | ".join(str(x) for x in s["yearly_excess_3m_pct"].values()) + " |")
    if sims:
        L += ["", f"## 组合模拟 (每月调仓, 等权持有 1 个月, 空仓收益 0, 单边成本 {COST_ONE_WAY*100:.1f}%)", "",
              "| 规则 | 平均持股 | 空仓月份 | 全期年化 | 全期最大回撤 | 全期夏普 | 2019 起年化 | 2019 起最大回撤 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
        full, oos = sims["all"], sims["oos"]
        for v, a in full.items():
            o = oos.get(v, {})
            L.append(f"| {v} | {a.get('avg_holdings', '—')} | {a.get('months_in_cash_pct', '—')}% | "
                     f"{a.get('cagr_pct', '—')}% | {a.get('max_dd_pct', '—')}% | {a.get('sharpe', '—')} | "
                     f"{o.get('cagr_pct', '—')}% | {o.get('max_dd_pct', '—')}% |")
    mu = meta.get("market_up")
    L += ["", f"当前大盘方向 (等权市场指数 vs {MARKET_SMA_MONTHS} 个月均线): "
          + ("向上" if mu else "向下" if mu is False else "未知")
          + " — 仅供参考; 见 C50+营收25+M 一行: 按此规则空仓的回测效果更差."]
    L += ["", f"## 当前入选 ({meta.get('picks_rule')}, 前 30)", "",
          "| 股票 | 季度末 | EPS 同比 | 上季同比 | 营收同比 | 月收盘 |", "|---|---|---:|---:|---:|---:|"]
    for p in picks[:30]:
        L.append(f"| {p['ticker']} | {p['last_quarter_end']} | {p['eps_growth_pct']}% | "
                 f"{p['prev_eps_growth_pct'] if p['prev_eps_growth_pct'] is not None else '—'}% | "
                 f"{p['sales_growth_pct'] if p['sales_growth_pct'] is not None else '—'}% | {p['close']} |")
    L += ["", "## 已知局限", "",
          "- 幸存者偏差: 股票池只含当前仍上市的公司, 已退市的缺失 (入选股和基准都受影响, 绝对收益偏高).",
          "- 非经常性收益未剔除: 用 GAAP 稀释 EPS; Q4 由 年度 − 前三季 推算.",
          "- 未计交易成本与滑点; 月度调仓, 未叠加价格形态 / 大盘方向 (CAN SLIM 其他条件).",
          "- 这是选股研究, 不是交易建议; 不进入自动交易, 除非样本外准入通过且另行评审."]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(AGENTS / ".growth_cache"))
    ap.add_argument("--out")
    ap.add_argument("--picks-rule", default=WATCH_RULE)
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    cache = Path(a.cache)
    print("[load]")
    companies, close, dvol = build(*load(cache))
    print(f"  公司 {len(companies)} · 月份 {len(close.index)} · 价格标的 {len(close.columns)}")
    bt = run_backtest(companies, close, dvol)
    summ = summarize(bt)
    picks = current_picks(companies, close, dvol, VARIANTS[a.picks_rule])
    trend = market_trend(close)
    done_months = [m for m in close.index if m <= date.today().isoformat()]
    market_up = trend.get(done_months[-1]) if done_months else None
    watch = update_watch(picks, close, dvol, a.picks_rule, market_up)
    print(f"[watch] {len(picks)} 只 · 大盘向上={market_up} · 影子记录 {len(watch['shadow'])} 条")
    meta = {"generated": datetime.now().isoformat(timespec="seconds"),
            "first_month": bt["months"][0], "last_month": bt["months"][-1],
            "avg_universe": round(sum(r["n"] for r in bt["universe"]) / len(bt["universe"])),
            "picks_rule": a.picks_rule, "market_up": market_up}
    out = Path(a.out) if a.out else ROOT / "development" / date.today().isoformat() / "eps_growth"
    out.mkdir(parents=True, exist_ok=True)
    sims = {"all": portfolio_sim(bt), "oos": portfolio_sim(bt, "2019-01-01")}
    md = render(summ, meta, picks, sims)
    (out / "backtest_report.md").write_text(md, encoding="utf-8")
    (out / "backtest_summary.json").write_text(json.dumps({"meta": meta, "summary": summ, "portfolio_sim": sims}, ensure_ascii=False, indent=2), encoding="utf-8")
    import csv
    with open(out / "current_picks.csv", "w", encoding="utf-8", newline="") as f:
        if picks:
            w = csv.DictWriter(f, fieldnames=list(picks[0]))
            w.writeheader()
            w.writerows(picks)
    print(md)
    print(f"[out] {out}")


def _run_logged(fn, log_name: str) -> None:
    """stdout/stderr 同时写入 logs/<log_name> (后台无控制台运行时也能看到输出与异常)."""
    import traceback
    log_path = Path(__file__).resolve().parent / "logs" / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)

    class _Tee:
        def __init__(self, *streams):
            self.streams = [s for s in streams if s is not None]
        def write(self, s):
            for st in self.streams:
                try:
                    st.write(s)
                    st.flush()
                except Exception:
                    pass
            return len(s)
        def flush(self):
            pass

    with open(log_path, "a", encoding="utf-8") as f:
        sys.stdout = _Tee(sys.__stdout__, f)
        sys.stderr = _Tee(sys.__stderr__, f)
        print(f"===== {datetime.now().isoformat(timespec='seconds')} {Path(sys.argv[0]).name} {sys.argv[1:]} =====")
        try:
            fn()
        except SystemExit as e:
            print(f"[exit] {e}")
            raise
        except BaseException:
            traceback.print_exc()
            raise SystemExit(1)
        finally:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


if __name__ == "__main__":
    _run_logged(main, "eps_growth_py.log")
