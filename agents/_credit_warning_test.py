"""信用利差 / 债市反应能否提前警告股市下跌? —— 预注册检验 (2026-10-07).

用户问题: 是否监测信用利差, 根据债市的反应对股市发出警告.
现状: bond_monitor 只按"水平"报警 (HY OAS >400/500bp, IG >120/150bp, 阈值无回测依据);
      对股票的决策只有半导体风险开关用到 HY 60 日扩大 ≥75bp. 其余股票不受信用利差影响.
原则: 规则要有逻辑和证据. 以下条件在看结果之前写定.

逻辑假设: 债权人对违约风险更敏感, 信用利差扩大 (或高收益债跑输国债) 可能早于股市反映
  盈利/融资压力. 关键在"变化"而非"水平"; 最有价值的是"股市还在高位、债市已经在跑"的背离.

数据 (只用当天及以前):
  · BAA10Y  = 穆迪 Baa 公司债收益率 − 10 年国债 (FRED, 1986~)        ← 历史最长
  · HY OAS  = ICE BofA 美国高收益债期权调整利差 (FRED BAMLH0A0HYM2, 能拿多少用多少)
  · HYG/IEF = 高收益债 ETF 相对 7-10 年国债 ETF 的复权价格比 (yfinance, 2007~)
  · SPY 复权收盘价
信号:
  S_baa   : BAA10Y 20 日变化 处于过去 252 日前 10% (扩大最快)
  S_hy    : HY OAS 20 日变化 处于过去 252 日前 10%
  S_hygief: HYG/IEF 20 日收益 处于过去 252 日后 10% (高收益债跑输最多)
  D_*     : 上面任一信号 且 SPY 收盘在 252 日高点 3% 以内 (股市平静、债市先动 = 背离)
  对照 (不参与判定, 只用来看信用是否比"看股价自己"多出信息):
  C_spy   : SPY 20 日收益 处于过去 252 日后 10%
结果: 次日收盘起算 20 日收益、20 日内相对入场最大跌幅、20 日内跌 ≥5% 概率; 60 日收益/跌幅作参考.
显著性: 随机平移检验 2000 次, 双侧 p; 独立事件 = 间隔 >10 个交易日.
分期: BAA10Y 训练 1987-2005 / 样本外 2006-;  HYG/IEF 训练 2008-2016 / 样本外 2017-;
      HY OAS 按可得数据前一半训练 / 后一半样本外.
判定 (同 _flow_history_test.verdict): 有下跌预警价值 = 训练期 20 日内最大跌幅更深 p<0.05
  且样本外同方向, 两期各 ≥10 个独立事件. 只有"有下跌预警价值"的信号才考虑接入系统.
只读研究, 不下单.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import _flow_history_test as ft

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "development" / "2026-10-07" / "credit_test"
CACHE = OUT_DIR / "cache"
NEAR_HIGH = 0.03
CHG_WIN = 20


def _fred(sid: str) -> pd.Series:
    f = CACHE / f"fred_{sid}.csv"
    if not f.exists() or time.time() - f.stat().st_mtime > 86400:
        from config import FRED_API_KEY
        url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
               f"&api_key={FRED_API_KEY}&file_type=json&observation_start=1980-01-01&sort_order=asc")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r:
            obs = json.loads(r.read()).get("observations", [])
        rows = [(o["date"], float(o["value"])) for o in obs if o.get("value") not in ("", ".", None)]
        pd.DataFrame(rows, columns=["date", "value"]).to_csv(f, index=False)
    df = pd.read_csv(f, parse_dates=["date"])
    s = df.set_index("date")["value"].sort_index()
    print(f"[fred] {sid}: {len(s)} {s.index.min().date() if len(s) else '-'}~{s.index.max().date() if len(s) else '-'}")
    return s


def _yf(tk: str) -> pd.Series:
    f = CACHE / f"px_{tk}.csv"
    if not f.exists() or time.time() - f.stat().st_mtime > 86400:
        import yfinance as yf
        df = yf.Ticker(tk).history(start="1993-01-01", interval="1d", auto_adjust=True)
        df.index = df.index.tz_localize(None) if df.index.tz else df.index
        df[["Close"]].to_csv(f)
    s = pd.read_csv(f, index_col=0, parse_dates=True)["Close"].sort_index()
    print(f"[px] {tk}: {len(s)} {s.index.min().date()}~{s.index.max().date()}")
    return s


def tail_flag(x: pd.Series, high: bool) -> pd.Series:
    """百分位尾部信号; 百分位还算不出来的日子保持 NaN (不当作"无信号"混进基准)."""
    r = ft.rolling_rank(x)
    f = (r >= ft.HIGH) if high else (r <= ft.LOW)
    return f.astype(float).where(r.notna())


def near_high(px: pd.Series, tol: float = NEAR_HIGH) -> pd.Series:
    hi = px.rolling(252, min_periods=252).max()
    return (px >= hi * (1 - tol)).astype(float).where(hi.notna())


def both(a: pd.Series, b: pd.Series) -> pd.Series:
    a, b = a.align(b, join="left")
    return ((a == 1) & (b == 1)).astype(float).where(a.notna() & b.notna())


def evaluate_flag(flag: pd.Series, px: pd.Series, periods) -> dict:
    """flag 已是布尔信号 (不再做百分位). 复用 ft 的结果与检验; 额外报告 60 日."""
    outc = ft.forward_outcomes(px)
    entry = px.shift(-1)
    outc["ret_60d"] = px.shift(-61) / entry - 1
    lows60 = pd.concat([px.shift(-1 - k) for k in range(0, 61)], axis=1).min(axis=1)
    outc["maxdd_60d"] = (lows60 / entry - 1).where(px.shift(-61).notna())
    df = pd.concat([flag.rename("flag"), outc], axis=1, join="inner")
    df = df[df["flag"].notna()]
    res = {}
    for name, a, b in periods:
        d = df.loc[a:b]
        if len(d) < 300:
            res[name] = {"n_days": int(len(d)), "insufficient": True}
            continue
        f = d["flag"].astype(float) == 1
        t = {"n_signal_days": int(f.sum()), "episodes": ft.count_episodes(f)}
        for col in ("ret_20d", "maxdd_20d", "dd5_20d", "ret_60d", "maxdd_60d"):
            y = d[col].values.astype(float)
            diff, p = ft.rotation_pvalue(f.values, y)
            t[col] = {"cond": round(float(np.nanmean(y[f.values])), 5) if f.any() else None,
                      "base": round(float(np.nanmean(y)), 5), "diff": round(diff, 5), "p": round(p, 4)}
        res[name] = {"n_days": int(len(d)), "from": str(d.index.min().date()), "to": str(d.index.max().date()),
                     "high": t}
    return res


def split_half(s: pd.Series) -> list:
    s = s.dropna()
    if len(s) < 800:
        return [("train", "1900-01-01", "1900-01-02"), ("oos", "1900-01-03", "1900-01-04")]
    mid = s.index[len(s) // 2]
    return [("train", str(s.index.min().date()), str(mid.date())),
            ("oos", str((mid + pd.Timedelta(days=1)).date()), "2100-01-01")]


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    spy = _yf("SPY")
    hyg, ief = _yf("HYG"), _yf("IEF")
    baa = _fred("BAA10Y")
    try:
        hy = _fred("BAMLH0A0HYM2")
    except Exception as e:  # noqa: BLE001
        print(f"[fred] HY OAS fail: {e}"); hy = pd.Series(dtype=float)

    idx = spy.index
    nh = near_high(spy)
    sig = {}
    baa_d = baa.reindex(idx).ffill(limit=3)
    sig["S_baa"] = (tail_flag(baa_d.diff(CHG_WIN), True), [("train", "1987-01-01", "2005-12-31"), ("oos", "2006-01-01", "2100-01-01")])
    if len(hy):
        hy_d = hy.reindex(idx).ffill(limit=3)
        sig["S_hy"] = (tail_flag(hy_d.diff(CHG_WIN), True), split_half(hy_d.diff(CHG_WIN).dropna().iloc[251:]))
    ratio = (hyg / ief).reindex(idx)
    sig["S_hygief"] = (tail_flag(ratio.pct_change(CHG_WIN), False), [("train", "2008-01-01", "2016-12-31"), ("oos", "2017-01-01", "2100-01-01")])
    for k in list(sig):
        f, per = sig[k]
        sig["D_" + k[2:]] = (both(f, nh), per)
    sig["C_spy"] = (tail_flag(spy.pct_change(CHG_WIN), False), [("train", "1994-01-01", "2005-12-31"), ("oos", "2006-01-01", "2100-01-01")])

    results = {}
    for name, (flag, periods) in sig.items():
        ev = evaluate_flag(flag, spy, periods)
        v = ft.verdict(ev) if not name.startswith("C_") else "control"
        results[name] = {"verdict": v, "periods": periods, "eval": ev}
        print(f"[result] {name} → {v}")
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "preregistered": __doc__, "results": results,
           "current": current_state(spy, baa, hy, hyg, ief)}
    (OUT_DIR / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (OUT_DIR / "report.md").write_text(render(out), encoding="utf-8")
    print("written", OUT_DIR)


def current_state(spy, baa, hy, hyg, ief) -> dict:
    cur = {}
    try:
        cur["baa10y"] = round(float(baa.iloc[-1]), 2)
        cur["baa10y_20d_chg"] = round(float(baa.iloc[-1] - baa.iloc[-21]), 2)
        if len(hy):
            cur["hy_oas"] = round(float(hy.iloc[-1]), 2)
            cur["hy_oas_20d_chg"] = round(float(hy.iloc[-1] - hy.iloc[-21]), 2)
        r = (hyg / ief).dropna()
        cur["hyg_ief_20d_pct"] = round(float(r.iloc[-1] / r.iloc[-21] - 1) * 100, 2)
        cur["spy_from_252d_high_pct"] = round(float(spy.iloc[-1] / spy.iloc[-252:].max() - 1) * 100, 2)
    except Exception as e:  # noqa: BLE001
        cur["error"] = str(e)
    return cur


def render(out: dict) -> str:
    L = ["# 信用利差 → 股市预警 预测力检验 (预注册)", "", f"生成: {out['generated_at']}", "",
         "结果都是 SPY, 次日收盘起算. 差 = 信号日平均 − 全期平均; p = 随机平移检验双侧 p.", "",
         "| 信号 | 结论 | 期间 | 独立事件 | 20日收益 差 (p) | 20日内最大跌幅 差 (p) | 跌≥5% 条件/基准 | 60日内最大跌幅 差 (p) |",
         "|---|---|---|---|---|---|---|---|"]
    for name, r in out["results"].items():
        for per in ("train", "oos"):
            e = r["eval"].get(per, {})
            h = e.get("high")
            vz = "对照" if r["verdict"] == "control" else ft.VERDICT_ZH.get(r["verdict"], r["verdict"])
            if not h:
                L.append(f"| {name} | {vz} | {per} | — | 数据不足 | | | |")
                continue
            L.append(f"| {name} | {vz} | {per} {e['from']}~{e['to']} | {h['episodes']} | "
                     f"{h['ret_20d']['diff']*100:+.2f}% ({h['ret_20d']['p']:.3f}) | "
                     f"{h['maxdd_20d']['diff']*100:+.2f}% ({h['maxdd_20d']['p']:.3f}) | "
                     f"{(h['dd5_20d']['cond'] or 0)*100:.0f}% / {h['dd5_20d']['base']*100:.0f}% | "
                     f"{h['maxdd_60d']['diff']*100:+.2f}% ({h['maxdd_60d']['p']:.3f}) |")
    L += ["", "当前读数: " + json.dumps(out.get("current", {}), ensure_ascii=False)]
    return "\n".join(L) + "\n"


def _run_logged():
    import traceback
    log = HERE / "logs" / "credit_warning_test.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as fh:
        class Tee:
            def write(self, s):
                fh.write(s); fh.flush()
            def flush(self):
                fh.flush()
        sys.stdout = sys.stderr = Tee()
        try:
            main(); return 0
        except Exception:
            traceback.print_exc(); return 1


if __name__ == "__main__":
    sys.exit(_run_logged())
