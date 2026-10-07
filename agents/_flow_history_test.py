"""大额看跌 / 大额卖出 是否预示指数下跌? —— 预注册历史检验 (2026-10-07).

用户问题: 是否要重点监控 QQQ/SPY 的异常大额做空期权或巨大卖单.
系统原则: 规则必须有逻辑和证据, 不能拍脑袋. 以下检验条件在看到结果之前写定, 不事后调.

可得的免费历史数据 (逐笔期权成交/大宗成交需要付费数据, 本检验不覆盖):
  · CBOE 每日 put/call 成交量比: index (以 SPX 为主) / total / equity, 2006-11 ~ 2019-10 (CBOE 存档)
  · FINRA Reg SHO 每日卖空成交量 (CNMS 合并文件): SPY / QQQ, 2018-08 ~ 现在
  · 价格: yfinance SPY / QQQ 日线 (复权)

信号 (都只用当天及以前的数据):
  rank_t = 当天数值在过去 252 个交易日 (含当天) 中的百分位
  高尾 = rank ≥ 0.90 (主假设: "看跌/卖出异常多")   低尾 = rank ≤ 0.10 (次要, 只报告)
  信号在 t 日收盘后才知道 → 结果从 t+1 收盘起算 (不偷看).
结果: 未来 5/10/20 日收益; 未来 20 日最大回撤; 20 日内回撤 ≥5% 的概率.
显著性: 随机平移检验 (把信号序列整体循环平移 ≥60 天, 2000 次), 保留信号的聚集性, 双侧 p.
分期: CBOE 训练 2006-11~2012-12, 样本外 2013-01~2019-10;
      FINRA 训练 (百分位可用起) ~2022-06, 样本外 2022-07~.
判定 (每个信号, 只看高尾):
  bearish_warning     : 训练期 20 日最大回撤更深且 p<0.05, 样本外同方向, 两期各 ≥10 个独立事件
  contrarian_bullish  : 训练期 20 日收益更高且 p<0.05, 样本外同方向, 两期各 ≥10 个独立事件
  no_reliable_signal  : 其他
只读研究, 不下单.
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "development" / "2026-10-07" / "flow_test"
CACHE = OUT_DIR / "cache"
UA = {"User-Agent": "Mozilla/5.0 (research; fsi-skills flow test)"}
CBOE_URLS = {
    "index_pc": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/indexpc.csv",
    "total_pc": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/totalpc.csv",
    "equity_pc": "https://cdn.cboe.com/resources/options/volume_and_call_put_ratios/equitypc.csv",
}
FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"
FINRA_START = date(2018, 8, 1)
RANK_WIN = 252
HIGH, LOW = 0.90, 0.10
HORIZONS = (5, 10, 20)
N_PERM = 2000
MIN_EPISODES = 10
EPISODE_GAP = 10
PERIODS = {
    "cboe": [("train", "2006-11-01", "2012-12-31"), ("oos", "2013-01-01", "2019-10-31")],
    "finra": [("train", "2018-08-01", "2022-06-30"), ("oos", "2022-07-01", "2100-01-01")],
}


# ── 数据 ─────────────────────────────────────────────────────────────────────
def _get(url: str, timeout: int = 30) -> bytes | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        if "404" not in str(e):
            print(f"  fetch fail {url[-40:]}: {str(e)[:80]}")
        return None


def parse_cboe_csv(raw: str) -> pd.Series:
    """CBOE 存档 csv: 前面若干行说明, 表头含 'P/C'. 返回日期索引的 put/call 比."""
    lines = raw.splitlines()
    start = next(i for i, l in enumerate(lines) if "P/C" in l.upper())
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    df.columns = [c.strip().upper() for c in df.columns]
    dcol = next(c for c in df.columns if "DATE" in c)
    pcol = next(c for c in df.columns if "P/C" in c)
    s = pd.Series(pd.to_numeric(df[pcol], errors="coerce").values,
                  index=pd.to_datetime(df[dcol], errors="coerce")).dropna()
    s = s[~s.index.isna()].sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_cboe() -> dict[str, pd.Series]:
    out = {}
    for name, url in CBOE_URLS.items():
        f = CACHE / f"{name}.csv"
        if not f.exists():
            raw = _get(url)
            if raw:
                f.write_bytes(raw)
        if f.exists():
            try:
                out[name] = parse_cboe_csv(f.read_text(encoding="utf-8", errors="replace"))
                print(f"[cboe] {name}: {len(out[name])} days {out[name].index.min().date()}~{out[name].index.max().date()}")
            except Exception as e:  # noqa: BLE001
                print(f"[cboe] {name} parse fail: {e}")
    return out


def load_finra(symbols=("SPY", "QQQ")) -> pd.DataFrame:
    """逐日下载 CNMS 卖空量文件, 只保留 SPY/QQQ; 结果增量缓存, 可断点续跑."""
    f = CACHE / "finra_short_volume.csv"
    have = pd.read_csv(f, dtype={"date": str}) if f.exists() else pd.DataFrame(
        columns=["date", "symbol", "short_volume", "total_volume"])
    done = set(have["date"].astype(str))
    missing_log = CACHE / "finra_missing_days.txt"
    missing = set(missing_log.read_text().split()) if missing_log.exists() else set()
    d = FINRA_START
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    new_rows, n_fetch = [], 0
    while d <= end:
        ds = d.strftime("%Y%m%d")
        if d.weekday() < 5 and ds not in done and ds not in missing:
            raw = _get(FINRA_URL.format(d=ds))
            n_fetch += 1
            if raw is None:
                missing.add(ds)
            else:
                for line in raw.decode("utf-8", "replace").splitlines():
                    p = line.split("|")
                    if len(p) >= 5 and p[1] in symbols:
                        new_rows.append({"date": ds, "symbol": p[1],
                                         "short_volume": float(p[2]), "total_volume": float(p[4])})
            if n_fetch % 100 == 0:
                print(f"[finra] {ds} fetched {n_fetch}")
                _flush_finra(f, have, new_rows, missing_log, missing)
                have = pd.read_csv(f, dtype={"date": str}); new_rows = []
            time.sleep(0.15)
        d += timedelta(days=1)
    _flush_finra(f, have, new_rows, missing_log, missing)
    df = pd.read_csv(f, dtype={"date": str})
    print(f"[finra] rows {len(df)} days {df['date'].nunique()}")
    return df


def _flush_finra(f, have, new_rows, missing_log, missing):
    df = pd.concat([have, pd.DataFrame(new_rows)], ignore_index=True) if new_rows else have
    df.to_csv(f, index=False)
    missing_log.write_text("\n".join(sorted(missing)))


def load_prices() -> dict[str, pd.Series]:
    import yfinance as yf
    out = {}
    for tk in ("SPY", "QQQ"):
        f = CACHE / f"px_{tk}.csv"
        if not f.exists() or time.time() - f.stat().st_mtime > 86400:
            df = yf.Ticker(tk).history(start="2005-01-01", interval="1d", auto_adjust=True)
            if not df.empty:
                df.index = df.index.tz_localize(None) if df.index.tz else df.index
                df[["Close"]].to_csv(f)
        s = pd.read_csv(f, index_col=0, parse_dates=True)["Close"]
        out[tk] = s.sort_index()
        print(f"[px] {tk}: {len(s)} {s.index.min().date()}~{s.index.max().date()}")
    return out


# ── 分析 (纯函数, 有单元测试) ────────────────────────────────────────────────
def rolling_rank(s: pd.Series, win: int = RANK_WIN) -> pd.Series:
    return s.rolling(win, min_periods=win).rank(pct=True)


def forward_outcomes(px: pd.Series, horizons=HORIZONS) -> pd.DataFrame:
    """t 日信号 → 从 t+1 收盘起算."""
    entry = px.shift(-1)
    out = pd.DataFrame(index=px.index)
    for h in horizons:
        out[f"ret_{h}d"] = px.shift(-1 - h) / entry - 1
    # 20 日内相对入场价的最大跌幅 (含入场日 → 不会为正)
    lows = pd.concat([px.shift(-1 - k) for k in range(0, 21)], axis=1).min(axis=1)
    out["maxdd_20d"] = (lows / entry - 1).where(px.shift(-21).notna())
    out["dd5_20d"] = (out["maxdd_20d"] <= -0.05).astype(float).where(out["maxdd_20d"].notna())
    return out


def count_episodes(flag: pd.Series, gap: int = EPISODE_GAP) -> int:
    idx = np.flatnonzero(flag.fillna(False).values.astype(bool))
    if len(idx) == 0:
        return 0
    return int(1 + np.sum(np.diff(idx) > gap))


def rotation_pvalue(flag: np.ndarray, y: np.ndarray, n_perm: int = N_PERM, min_shift: int = 60,
                    seed: int = 7) -> tuple[float, float]:
    """差值 = mean(y|flag) - mean(y). 循环平移 flag 构造零分布, 返回 (差值, 双侧 p)."""
    ok = ~np.isnan(y)
    f, yy = flag[ok].astype(bool), y[ok]
    n = len(yy)
    if f.sum() == 0 or n < 2 * min_shift:
        return float("nan"), float("nan")
    base = yy.mean()
    obs = yy[f].mean() - base
    rng = np.random.default_rng(seed)
    shifts = rng.integers(min_shift, n - min_shift, size=n_perm)
    null = np.array([yy[np.roll(f, k)].mean() - base for k in shifts])
    p = (np.sum(np.abs(null) >= abs(obs)) + 1) / (n_perm + 1)
    return float(obs), float(p)


def evaluate(signal: pd.Series, px: pd.Series, periods) -> dict:
    rank = rolling_rank(signal)
    outc = forward_outcomes(px)
    df = pd.concat([rank.rename("rank"), outc], axis=1, join="inner").dropna(subset=["rank"])
    res = {}
    for name, a, b in periods:
        d = df.loc[a:b]
        if len(d) < 300:
            res[name] = {"n_days": int(len(d)), "insufficient": True}
            continue
        r = {"n_days": int(len(d)), "from": str(d.index.min().date()), "to": str(d.index.max().date())}
        for tail, flag in (("high", d["rank"] >= HIGH), ("low", d["rank"] <= LOW)):
            t = {"n_signal_days": int(flag.sum()), "episodes": count_episodes(flag)}
            for col in ("ret_5d", "ret_10d", "ret_20d", "maxdd_20d", "dd5_20d"):
                y = d[col].values.astype(float)
                diff, p = rotation_pvalue(flag.values, y)
                t[col] = {"cond": round(float(np.nanmean(y[flag.values])), 5) if flag.any() else None,
                          "base": round(float(np.nanmean(y)), 5), "diff": round(diff, 5), "p": round(p, 4)}
            r[tail] = t
        res[name] = r
    return res


def verdict(ev: dict) -> str:
    tr, oos = ev.get("train", {}), ev.get("oos", {})
    if tr.get("insufficient") or oos.get("insufficient") or "high" not in tr or "high" not in oos:
        return "insufficient_data"
    th, oh = tr["high"], oos["high"]
    if th["episodes"] < MIN_EPISODES or oh["episodes"] < MIN_EPISODES:
        return "insufficient_episodes"
    dd_t, dd_o = th["maxdd_20d"], oh["maxdd_20d"]
    r_t, r_o = th["ret_20d"], oh["ret_20d"]
    if dd_t["diff"] < 0 and dd_t["p"] < 0.05 and dd_o["diff"] < 0:
        return "bearish_warning"
    if r_t["diff"] > 0 and r_t["p"] < 0.05 and r_o["diff"] > 0:
        return "contrarian_bullish"
    return "no_reliable_signal"


VERDICT_ZH = {"bearish_warning": "有下跌预警价值", "contrarian_bullish": "反向指标 (极端看跌后反而偏涨)",
              "no_reliable_signal": "没有可靠预测力", "insufficient_data": "数据不足",
              "insufficient_episodes": "独立事件太少"}


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    px = load_prices()
    signals = {}
    for name, s in load_cboe().items():
        signals[f"cboe_{name}"] = ("cboe", s, "SPY")
    fin = load_finra()
    if len(fin):
        fin["date"] = pd.to_datetime(fin["date"], format="%Y%m%d")
        for sym in ("SPY", "QQQ"):
            d = fin[fin["symbol"] == sym].set_index("date").sort_index()
            ratio = (d["short_volume"] / d["total_volume"]).replace([np.inf, -np.inf], np.nan).dropna()
            signals[f"finra_short_ratio_{sym}"] = ("finra", ratio, sym)
    results = {}
    for name, (kind, s, target) in signals.items():
        ev = evaluate(s, px[target], PERIODS[kind])
        results[name] = {"target": target, "verdict": verdict(ev), "eval": ev}
        print(f"[result] {name} → {results[name]['verdict']}")
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "preregistered": __doc__,
           "params": {"rank_win": RANK_WIN, "high": HIGH, "low": LOW, "n_perm": N_PERM,
                      "min_episodes": MIN_EPISODES, "periods": PERIODS}, "results": results}
    (OUT_DIR / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT_DIR / "report.md").write_text(render_report(out), encoding="utf-8")
    print("written", OUT_DIR)


def render_report(out: dict) -> str:
    L = ["# 大额看跌 / 卖出 预测力检验 (预注册)", "", f"生成: {out['generated_at']}", "",
         "高尾 = 当天数值处于过去 252 日前 10%. 结果从次日收盘起算. p = 随机平移检验双侧 p.", "",
         "| 信号 | 标的 | 结论 | 期间 | 事件数 | 20日收益 差 (p) | 20日内相对入场最大跌幅 差 (p) | 20日内跌≥5%概率 条件/基准 |",
         "|---|---|---|---|---|---|---|---|"]
    for name, r in out["results"].items():
        for per in ("train", "oos"):
            e = r["eval"].get(per, {})
            h = e.get("high")
            if not h:
                L.append(f"| {name} | {r['target']} | {VERDICT_ZH.get(r['verdict'])} | {per} | — | 数据不足 | | |")
                continue
            L.append(
                f"| {name} | {r['target']} | {VERDICT_ZH.get(r['verdict'])} | {per} {e['from']}~{e['to']} | {h['episodes']} | "
                f"{h['ret_20d']['diff']*100:+.2f}% ({h['ret_20d']['p']:.3f}) | "
                f"{h['maxdd_20d']['diff']*100:+.2f}% ({h['maxdd_20d']['p']:.3f}) | "
                f"{(h['dd5_20d']['cond'] or 0)*100:.0f}% / {h['dd5_20d']['base']*100:.0f}% |")
    L += ["", "低尾 (极端低) 的数字见 result.json, 不参与判定."]
    return "\n".join(L) + "\n"


def _run_logged():
    import traceback
    log = HERE / "logs" / "flow_history_test.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as fh:
        class Tee:
            def write(self, s):
                fh.write(s); fh.flush()
                try:
                    sys.__stdout__.write(s)
                except Exception:
                    pass
            def flush(self):
                fh.flush()
        sys.stdout = sys.stderr = Tee()
        try:
            main()
            return 0
        except Exception:
            traceback.print_exc()
            return 1


if __name__ == "__main__":
    sys.exit(_run_logged())
