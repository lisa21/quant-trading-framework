"""成长股 EPS 筛选规则 (欧奈尔 CAN SLIM 的 C + A, 2026-10-02).

纯函数, 不联网. 输入是 growth_eps_data 产出的 XBRL 事实 (每条带 filed),
所有计算都是 point-in-time: 只用 as_of 当天之前已提交的数字 (同一期间若后来
修订, 回测在修订提交日之前仍用原值).

规则 (每条可单独开关, 见 DEFAULT_RULES):
  C   最新季度 EPS 同比 (对比上年同季度, 不是上一季度) ≥ min_eps_growth;
      上年同季 EPS ≥ min_base_eps (>0) — 防止 0.01→0.05 = 400% 这类小基数
  accel  最新季度同比增速 > 上一季度同比增速 (加速)
  sales  最新季度营收同比 ≥ min_sales_growth (增长要有营收支撑, 不是砍费用)
  annual 最近 4 个财年 EPS 连续 3 年增长, 且最新财年 EPS > 0
  no_decel  排除连续两个季度增速大幅回落 (如 50% → 30% → 15%)
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta

DEFAULT_RULES = {
    "min_eps_growth": 0.25,
    "min_base_eps": 0.05,
    "require_accel": False,
    "min_sales_growth": None,     # None = 不检查
    "require_annual": False,
    "require_no_decel": False,
    "decel_ratio": 0.67,          # g0 < g2 × 0.67 且连续两次下降 → 视为大幅回落
    "max_report_age_days": 200,   # 最新季报太旧 → 不参与 (停报/退市)
}
YEAR_TOL = 25                     # "上年同季度" 期末日期容差 (天)


def _d(s: str) -> date:
    return date.fromisoformat(s)


class CompanyFacts:
    """一家公司的事实, 按 (kind, start, end) 分组, 每组按 filed 排序 → bisect 取当时值."""

    def __init__(self, rows: list[dict]):
        tmp: dict[str, dict[tuple, list[tuple[str, float]]]] = {}
        for r in rows:
            tmp.setdefault(r["kind"], {}).setdefault((r["start"], r["end"]), []).append(
                (r["filed"], float(r["val"])))
        self._g: dict[str, dict[tuple, tuple[list[str], list[float]]]] = {}
        filed_all = set()
        for kind, groups in tmp.items():
            self._g[kind] = {}
            for k, lst in groups.items():
                lst.sort()
                self._g[kind][k] = ([f for f, _ in lst], [v for _, v in lst])
                filed_all.update(f for f, _ in lst)
        # 所有提交日 (升序): 回测里同一提交状态的结果可复用
        self.filed_dates = sorted(filed_all)

    def known(self, kind: str, as_of: str) -> dict[tuple[str, str], float]:
        """as_of 当天及之前已提交的 (start, end) → 值 (取当时最新一次提交)."""
        out = {}
        for k, (filed, vals) in (self._g.get(kind) or {}).items():
            i = bisect_right(filed, as_of)
            if i:
                out[k] = vals[i - 1]
        return out

    def state_key(self, as_of: str) -> int:
        """as_of 时已知的提交次数 (相同 → 已知信息相同)."""
        return bisect_right(self.filed_dates, as_of)


def _split(periods: dict[tuple[str, str], float]):
    q, a = {}, {}
    for (s, e), v in periods.items():
        n = (_d(e) - _d(s)).days
        if 80 <= n <= 100:
            q[e] = v
        elif 350 <= n <= 380:
            a[e] = (s, v)
    return q, a


def quarterly_series(periods: dict[tuple[str, str], float]) -> tuple[dict[str, float], dict[str, float]]:
    """→ (季度 end→值 (含推算 Q4), 年度 end→值). Q4 = 年度 − 同财年前三季."""
    q, a = _split(periods)
    out = dict(q)
    for e, (s, v) in a.items():
        if e in out:
            continue
        inside = [qe for qe in q if _d(s) < _d(qe) < _d(e) - timedelta(days=45)]
        if len(inside) == 3:
            out[e] = v - sum(q[x] for x in inside)
    return dict(sorted(out.items())), {e: v for e, (s, v) in sorted(a.items())}


def _prior_year(series: dict[str, float], end: str):
    target = _d(end) - timedelta(days=365)
    best = None
    for e, v in series.items():
        gap = abs((_d(e) - target).days)
        if gap <= YEAR_TOL and (best is None or gap < best[0]):
            best = (gap, v)
    return None if best is None else best[1]


def yoy_growths(series: dict[str, float], min_base: float, n: int = 3) -> list[float | None]:
    """最近 n 个季度的同比增速 (新→旧). 上年同季 < min_base (含亏损) → None."""
    ends = list(series)[-n:][::-1]
    out = []
    for e in ends:
        prev = _prior_year(series, e)
        cur = series[e]
        out.append(None if prev is None or prev < min_base else (cur - prev) / abs(prev))
    return out


def metrics(cf: CompanyFacts, as_of: str, min_base_eps: float = DEFAULT_RULES["min_base_eps"]) -> dict:
    """规则无关的 point-in-time 指标 (回测里按提交状态缓存, 各规则共用)."""
    eps_q, eps_a = quarterly_series(cf.known("eps", as_of))
    if not eps_q:
        return {"last_quarter_end": None}
    last_end = list(eps_q)[-1]
    m = {"last_quarter_end": last_end, "eps_growth": yoy_growths(eps_q, min_base_eps),
         "min_base_eps": min_base_eps}
    rev_q, _ = quarterly_series(cf.known("rev", as_of))
    m["sales_growth"] = (yoy_growths(rev_q, 1.0, n=1)[0]
                         if rev_q and list(rev_q)[-1] == last_end else None)
    vals, ends = list(eps_a.values())[-4:], list(eps_a)[-4:]
    spaced = len(ends) == 4 and all(330 <= (_d(b) - _d(a)).days <= 400
                                    for a, b in zip(ends, ends[1:]))
    m["annual_3y_growth"] = bool(spaced and vals[-1] > 0
                                 and all(x < y for x, y in zip(vals, vals[1:])))
    return m


def apply_rules(m: dict, as_of: str, rules: dict | None = None) -> dict:
    r = {**DEFAULT_RULES, **(rules or {})}
    res = {**m, "pass": False, "reason": None}
    if not m.get("last_quarter_end"):
        res["reason"] = "no_eps"
        return res
    if (_d(as_of) - _d(m["last_quarter_end"])).days > r["max_report_age_days"]:
        res["reason"] = "stale_report"
        return res
    g = m["eps_growth"]
    if g[0] is None:
        res["reason"] = "base_too_small_or_missing"
        return res
    if g[0] < r["min_eps_growth"]:
        res["reason"] = "eps_growth_low"
        return res
    if r["require_accel"] and (len(g) < 2 or g[1] is None or not g[0] > g[1]):
        res["reason"] = "not_accelerating"
        return res
    if r["require_no_decel"] and len(g) == 3 and None not in g \
            and g[0] < g[1] < g[2] and g[0] < g[2] * r["decel_ratio"]:
        res["reason"] = "two_quarter_deceleration"
        return res
    if r["min_sales_growth"] is not None and (m["sales_growth"] is None
                                              or m["sales_growth"] < r["min_sales_growth"]):
        res["reason"] = "sales_growth_low"
        return res
    if r["require_annual"] and not m["annual_3y_growth"]:
        res["reason"] = "annual_not_3y_growth"
        return res
    res["pass"] = True
    return res


def evaluate(cf: CompanyFacts, as_of: str, rules: dict | None = None) -> dict:
    """返回指标与是否通过. 不通过时 reason 说明第一条不满足的规则."""
    r = {**DEFAULT_RULES, **(rules or {})}
    return apply_rules(metrics(cf, as_of, r["min_base_eps"]), as_of, r)
