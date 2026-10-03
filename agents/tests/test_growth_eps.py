"""成长股 EPS 筛选 (2026-10-02): 数据解析 / 规则 / point-in-time / 回测管线."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))

import growth_eps_data as gd
import growth_eps_screen as gs


def q(kind, s, e, val, filed, form="10-Q"):
    return {"cik": 1, "kind": kind, "start": s, "end": e, "val": val, "filed": filed, "form": form}


# 日历年公司: Q1..Q3 10-Q, 年度 10-K (Q4 推算)
def year_rows(y, eps, rev=None, filed_lag=30):
    rows = []
    qs = [(f"{y}-01-01", f"{y}-03-31"), (f"{y}-04-01", f"{y}-06-30"), (f"{y}-07-01", f"{y}-09-30")]
    fl = [f"{y}-05-01", f"{y}-08-01", f"{y}-11-01"]
    for (s, e), v, f in zip(qs, eps[:3], fl):
        rows.append(q("eps", s, e, v, f))
        if rev:
            rows.append(q("rev", s, e, rev[qs.index((s, e))], f))
    rows.append(q("eps", f"{y}-01-01", f"{y}-12-31", sum(eps), f"{y + 1}-02-15", "10-K"))
    if rev:
        rows.append(q("rev", f"{y}-01-01", f"{y}-12-31", sum(rev), f"{y + 1}-02-15", "10-K"))
    return rows


class ExtractFacts(unittest.TestCase):
    def test_keeps_quarter_and_annual_prefers_diluted_drops_ytd(self):
        company = {"cik": 7, "facts": {"us-gaap": {
            "EarningsPerShareDiluted": {"units": {"USD/shares": [
                {"start": "2020-01-01", "end": "2020-03-31", "val": 1.0, "filed": "2020-05-01", "form": "10-Q"},
                {"start": "2020-01-01", "end": "2020-06-30", "val": 2.1, "filed": "2020-08-01", "form": "10-Q"},  # 半年 YTD
                {"start": "2020-01-01", "end": "2020-12-31", "val": 4.0, "filed": "2021-02-01", "form": "10-K"},
                {"start": "2020-01-01", "end": "2020-03-31", "val": 9.9, "filed": "2020-05-01", "form": "8-K"}]}},
            "EarningsPerShareBasic": {"units": {"USD/shares": [
                {"start": "2020-01-01", "end": "2020-03-31", "val": 1.1, "filed": "2020-05-01", "form": "10-Q"}]}},
            "Revenues": {"units": {"USD": [
                {"start": "2020-01-01", "end": "2020-03-31", "val": 100.0, "filed": "2020-05-01", "form": "10-Q"}]}}}}}
        rows = gd.extract_facts(company)
        eps = [(r["start"], r["end"], r["val"], r["tag"]) for r in rows if r["kind"] == "eps"]
        self.assertIn(("2020-01-01", "2020-03-31", 1.0, "EarningsPerShareDiluted"), eps)
        self.assertNotIn(1.1, [e[2] for e in eps])
        self.assertNotIn(2.1, [e[2] for e in eps])
        self.assertNotIn(9.9, [e[2] for e in eps])
        self.assertIn(4.0, [e[2] for e in eps])
        self.assertEqual([r["val"] for r in rows if r["kind"] == "rev"], [100.0])

    def test_universe_one_ticker_per_cik_listed_only(self):
        payload = {"fields": ["cik", "name", "ticker", "exchange"],
                   "data": [[1, "A", "AAA", "Nasdaq"], [1, "A", "AAA-P", "Nasdaq"],
                            [2, "B", "BBB", "OTC"], [3, "C", "ccc", "NYSE"]]}
        self.assertEqual([u["ticker"] for u in gd.parse_universe(payload)], ["AAA", "CCC"])


class Rules(unittest.TestCase):
    def cf(self, *years):
        rows = []
        for y, eps, rev in years:
            rows += year_rows(y, eps, rev)
        return gs.CompanyFacts(rows)

    def test_yoy_not_qoq_and_q4_derived(self):
        c = self.cf((2020, [0.5, 0.5, 0.5, 0.5], None), (2021, [0.6, 0.6, 0.6, 0.9], None))
        qs, ann = gs.quarterly_series(c.known("eps", "2022-03-01"))
        self.assertAlmostEqual(qs["2021-12-31"], 0.9, places=6)   # 2.7 − 1.8
        g = gs.yoy_growths(qs, 0.05)
        self.assertAlmostEqual(g[0], 0.8, places=6)               # 0.9 vs 0.5 (上年同季)
        self.assertAlmostEqual(g[1], 0.2, places=6)

    def test_point_in_time_ignores_later_filings_and_restatements(self):
        rows = year_rows(2020, [0.5] * 4, None) + year_rows(2021, [0.5, 0.5, 1.0, 0.5], None)
        rows.append(q("eps", "2021-07-01", "2021-09-30", 0.55, "2022-05-01"))   # 2022 年修订
        c = gs.CompanyFacts(rows)
        m = gs.metrics(c, "2021-11-15")
        self.assertEqual(m["last_quarter_end"], "2021-09-30")
        self.assertAlmostEqual(m["eps_growth"][0], 1.0)            # 原值 1.0, 不是修订后 0.55
        self.assertEqual(gs.metrics(c, "2021-10-31")["last_quarter_end"], "2021-06-30")  # 11/1 才公告

    def test_small_or_negative_base_rejected(self):
        c = self.cf((2020, [0.01, 0.01, 0.01, 0.01], None), (2021, [0.05, 0.05, 0.05, 0.05], None))
        r = gs.evaluate(c, "2021-11-15")
        self.assertFalse(r["pass"])
        self.assertEqual(r["reason"], "base_too_small_or_missing")

    def test_accel_sales_annual_and_decel(self):
        rev20, rev21 = [100] * 4, [120, 130, 150, 160]
        c = self.cf((2017, [0.2] * 4, None), (2018, [0.3] * 4, None), (2019, [0.4] * 4, None),
                    (2020, [0.5] * 4, rev20), (2021, [0.6, 0.7, 0.9, 0.9], rev21))
        as_of = "2021-11-15"
        r = gs.evaluate(c, as_of, {"require_accel": True, "min_sales_growth": 0.25,
                                    "require_annual": True, "require_no_decel": True})
        self.assertTrue(r["pass"], r)
        self.assertAlmostEqual(r["sales_growth"], 0.5)
        self.assertFalse(gs.evaluate(c, as_of, {"min_sales_growth": 0.6})["pass"])
        # 增速连续两季大幅回落: 100% → 60% → 20%
        d = self.cf((2020, [0.5] * 4, None), (2021, [1.0, 0.8, 0.6, 0.6], None))
        r = gs.evaluate(d, as_of, {"min_eps_growth": 0.1, "require_no_decel": True})
        self.assertEqual(r["reason"], "two_quarter_deceleration")
        r = gs.evaluate(d, as_of, {"min_eps_growth": 0.1, "require_accel": True})
        self.assertEqual(r["reason"], "not_accelerating")

    def test_stale_report_excluded(self):
        c = self.cf((2020, [0.5] * 4, None), (2021, [0.9] * 4, None))
        self.assertEqual(gs.evaluate(c, "2023-06-30")["reason"], "stale_report")


class BacktestPipeline(unittest.TestCase):
    def test_excess_and_no_lookahead(self):
        import pandas as pd
        import _backtest_eps_growth as bt
        months = [bt.month_end(f"{y}-{m:02d}-01") for y in (2021, 2022) for m in range(1, 13)]
        grow = gs.CompanyFacts(year_rows(2020, [0.5] * 4, None) + year_rows(2021, [1.0] * 4, None))
        flat = gs.CompanyFacts(year_rows(2020, [0.5] * 4, None) + year_rows(2021, [0.5] * 4, None))
        close = pd.DataFrame({"GROW": [10 * 1.05 ** i for i in range(24)],
                              "FLAT": [10.0] * 24}, index=months)
        dvol = pd.DataFrame({"GROW": [2e8] * 24, "FLAT": [2e8] * 24}, index=months)
        res = bt.run_backtest({"GROW": grow, "FLAT": flat}, close, dvol,
                              variants={"C25": {"min_eps_growth": 0.25}}, start="2021-01-31")
        rows = {r["month"]: r for r in res["per_month"]["C25"]}
        # Q1 2021 在 5/1 公告: 4 月底还不能入选, 5 月底可以
        self.assertEqual(rows["2021-04-30"]["n"], 0)
        self.assertEqual(rows["2021-05-31"]["n"], 1)
        self.assertGreater(rows["2021-05-31"]["x3"], 0)
        summ = bt.summarize(res)
        self.assertIn("admission", summ["C25"])


class ManualFileFallback(unittest.TestCase):
    """2026-10-03: 本机网络解析不了 sec.gov 时不绕过, 允许手动放置文件."""

    def test_uses_placed_file_when_download_fails(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "companyfacts.zip"
            dest.write_bytes(b"x")
            import os, time as _t
            old = _t.time() - 30 * 86400
            os.utime(dest, (old, old))
            with patch.object(gd, "_download", side_effect=OSError("getaddrinfo failed")), \
                 patch.object(gd.time, "sleep", lambda s: None):
                self.assertEqual(gd._download_or_cached("u", dest, "ua x@y", 7), dest)

    def test_missing_file_gives_instruction(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "companyfacts.zip"
            with patch.object(gd, "_download", side_effect=OSError("getaddrinfo failed")), \
                 patch.object(gd.time, "sleep", lambda s: None):
                with self.assertRaises(SystemExit) as cm:
                    gd._download_or_cached("https://x/companyfacts.zip", dest, "ua x@y", 7)
            self.assertIn("其他网络", str(cm.exception))


class SystemRouteFallback(unittest.TestCase):
    """2026-10-03: Python 直连解析不了 sec.gov, 浏览器可以 → Windows 上改走系统网络设置."""

    def test_falls_back_to_system_route_on_windows(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "t.json"
            def fake_sys(url, d, ua):
                d.write_text("{}")
                return d
            with patch.object(gd, "_download_direct", side_effect=OSError("getaddrinfo failed")), \
                 patch.object(gd, "_download_system_route", side_effect=fake_sys) as sysr, \
                 patch.object(gd.os, "name", "nt"):
                self.assertEqual(gd._download("https://www.sec.gov/x", dest, "ua a@b", 1), dest)
            sysr.assert_called_once()

    def test_no_fallback_off_windows(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            with patch.object(gd, "_download_direct", side_effect=OSError("x")), \
                 patch.object(gd, "_download_system_route") as sysr, \
                 patch.object(gd.os, "name", "posix"):
                with self.assertRaises(OSError):
                    gd._download("https://www.sec.gov/x", Path(td) / "t.json", "ua a@b", 1)
            sysr.assert_not_called()

    def test_system_route_reports_failure(self):
        import tempfile
        from unittest.mock import patch
        class R:
            returncode, stdout, stderr = 1, b"route: http://proxy:8080/", b"boom"
        with tempfile.TemporaryDirectory() as td, patch.object(gd, "_run_ps", return_value=R()):
            with self.assertRaises(RuntimeError):
                gd._download_system_route("u", Path(td) / "t.json", "ua")


if __name__ == "__main__":
    unittest.main()
