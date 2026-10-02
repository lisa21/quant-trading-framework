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

HORIZONS = (3, 6, 12)
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
}


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


def run_backtest(companies, close, dvol, variants=VARIANTS, start="2012-01-31",
                 last_month: str | None = None) -> dict:
    months = [m for m in close.index if m >= start and (last_month is None or m <= last_month)]
    idx = {m: i for i, m in enumerate(close.index)}
    cache: dict[tuple, dict] = {}
    per_month = {v: [] for v in variants}
    stock_rows = {v: [] for v in variants}
    universe_rows = []
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
        for vname, rules in variants.items():
            picks = [tk for tk in liquid if apply_rules(mets[tk], as_of, rules)["pass"]]
            row = {"month": m, "n": len(picks)}
            for h in HORIZONS:
                rs = [fwd[h][tk] for tk in picks if tk in fwd[h]]
                row[f"r{h}"] = sum(rs) / len(rs) if rs else None
                row[f"x{h}"] = (row[f"r{h}"] - uni_mean[h]
                                if rs and uni_mean[h] is not None else None)
            per_month[vname].append(row)
            for tk in picks:
                if tk in fwd[12]:
                    stock_rows[vname].append(fwd[12][tk])
    return {"months": months, "per_month": per_month, "universe": universe_rows,
            "stock_r12": stock_rows}


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
    m = close.index[-1]
    as_of = as_of or date.today().isoformat()
    px, dv = close.iloc[-1], dvol.iloc[-1]
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


def render(summary: dict, meta: dict, picks: list[dict]) -> str:
    L = ["# 成长股 EPS 筛选回测", "",
         f"生成 {meta['generated']} · 区间 {meta['first_month']} ~ {meta['last_month']} · "
         f"月均股票池 {meta['avg_universe']} 只 · 样本内 ≤{IS_END[:4]}, 样本外 2019 起", "",
         "超额 = 入选股等权远期收益 − 同月股票池等权远期收益. 跑赢率 = 超额 > 0 的月份占比.", "",
         "| 规则 | 月均入选 | 3月超额 | 6月超额 | 12月超额 | 3月跑赢率 | 样本外3月超额 | 样本外不重叠 n / Wilson下界 | 12月翻倍率 (全市场) | 稳定性 | 准入 |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for v, s in summary.items():
        a3, a6, a12 = s["all_3m"], s["all_6m"], s["all_12m"]
        oos, nov = s["OOS_3m"], s["OOS_3m_nonoverlap"]
        L.append(f"| {v} | {a3.get('avg_picks', 0)} | {a3.get('avg_excess_pct', '—')}% | "
                 f"{a6.get('avg_excess_pct', '—')}% | {a12.get('avg_excess_pct', '—')}% | "
                 f"{a3.get('beat_rate_pct', '—')}% | {oos.get('avg_excess_pct', '—')}% | "
                 f"{nov.get('n', 0)} / {nov.get('wilson_lower_pct', '—')}% | "
                 f"{s['p_double_12m_pct']}% ({s['p_double_12m_universe_pct']}%) | {s['stability']} | "
                 f"{'通过' if s['admission']['pass'] else '未通过: ' + ', '.join(s['admission']['failed'])} |")
    L += ["", "## 分年 3 个月超额 (%)", "", "| 规则 | " + " | ".join(next(iter(summary.values()))["yearly_excess_3m_pct"]) + " |",
          "|---|" + "---:|" * len(next(iter(summary.values()))["yearly_excess_3m_pct"])]
    for v, s in summary.items():
        L.append(f"| {v} | " + " | ".join(str(x) for x in s["yearly_excess_3m_pct"].values()) + " |")
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
    ap.add_argument("--picks-rule", default="C25 全条件")
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
    meta = {"generated": datetime.now().isoformat(timespec="seconds"),
            "first_month": bt["months"][0], "last_month": bt["months"][-1],
            "avg_universe": round(sum(r["n"] for r in bt["universe"]) / len(bt["universe"])),
            "picks_rule": a.picks_rule}
    out = Path(a.out) if a.out else ROOT / "development" / date.today().isoformat() / "eps_growth"
    out.mkdir(parents=True, exist_ok=True)
    md = render(summ, meta, picks)
    (out / "backtest_report.md").write_text(md, encoding="utf-8")
    (out / "backtest_summary.json").write_text(json.dumps({"meta": meta, "summary": summ}, ensure_ascii=False, indent=2), encoding="utf-8")
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
