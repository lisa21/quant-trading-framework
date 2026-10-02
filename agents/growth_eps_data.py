"""成长股 EPS 筛选 · 数据层 (2026-10-02).

数据源 (都免费):
- SEC EDGAR companyfacts.zip: 全部上市公司 XBRL 财务事实, 每条带 filed (公告/提交日)
  → 可做 point-in-time (回测里只用当时已公告的数字, 不用后来修订的值).
- SEC company_tickers_exchange.json: CIK ↔ ticker ↔ 交易所.
- yfinance 月线 (复权收盘 + 成交量): 远期收益与流动性过滤.

只能在 Windows 上取数 (VM / 云端代理不放行 SEC / Yahoo). 取数结果是三个小文件:
  <cache>/universe.csv            cik,ticker,name,exchange
  <cache>/facts.csv.gz            cik,kind(eps|rev),start,end,val,filed,form,tag
  <cache>/prices_monthly.csv.gz   date,ticker,close,volume
之后的筛选 / 回测只读这些文件, 不再联网.

SEC 要求请求头带联系方式: 环境变量 SEC_USER_AGENT="名字 邮箱".

已知局限 (报告里也会写):
- 幸存者偏差: company_tickers 只含当前仍在上市的公司, 已退市公司缺失.
- 非经常性收益: XBRL 没有统一的"调整后 EPS"; 用 GAAP 稀释 EPS
  (缺失时用基本 EPS), 无法逐条剔除出售资产等一次性收益.
- Q4 单季没有直接披露, 用 年度 − 前三季 推算 (EPS 不严格可加, 有小误差).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

AGENTS = Path(__file__).resolve().parent
DEFAULT_CACHE = AGENTS / ".growth_cache"

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_FACTS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
EXCHANGES = {"NYSE", "Nasdaq"}

# 优先级从高到低; 同一期间同一次提交只保留最高优先级的标签
EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted",
            "EarningsPerShareBasic")
REV_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
            "SalesRevenueGoodsNet", "SalesRevenueServicesNet")
QUARTER_DAYS = (80, 100)
ANNUAL_DAYS = (350, 380)
FACT_FIELDS = ["cik", "kind", "start", "end", "val", "filed", "form", "tag"]


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def extract_facts(company: dict) -> list[dict]:
    """一家公司的 companyfacts JSON → 季度/年度 EPS 与营收事实 (保留每次提交).

    只保留 10-Q / 10-K (含 /A) 的期间型事实, 期长 80-100 天 (季度) 或
    350-380 天 (年度). 同一 (kind, start, end, filed) 只保留优先级最高的标签.
    """
    cik = int(company.get("cik") or 0)
    gaap = (company.get("facts") or {}).get("us-gaap") or {}
    best: dict[tuple, tuple[int, dict]] = {}
    for kind, tags, unit in (("eps", EPS_TAGS, "USD/shares"), ("rev", REV_TAGS, "USD")):
        for prio, tag in enumerate(tags):
            for f in ((gaap.get(tag) or {}).get("units") or {}).get(unit) or []:
                form = str(f.get("form") or "")
                if not form.startswith(("10-Q", "10-K")):
                    continue
                s, e, filed = f.get("start"), f.get("end"), f.get("filed")
                if not (s and e and filed) or f.get("val") is None:
                    continue
                try:
                    d = _days(s, e)
                except ValueError:
                    continue
                if not (QUARTER_DAYS[0] <= d <= QUARTER_DAYS[1]
                        or ANNUAL_DAYS[0] <= d <= ANNUAL_DAYS[1]):
                    continue
                key = (kind, s, e, filed)
                row = {"cik": cik, "kind": kind, "start": s, "end": e,
                       "val": float(f["val"]), "filed": filed, "form": form, "tag": tag}
                if key not in best or prio < best[key][0]:
                    best[key] = (prio, row)
    return [r for _, r in sorted(best.values(), key=lambda x: (x[1]["kind"], x[1]["end"], x[1]["filed"]))]


def parse_universe(payload: dict, exchanges=EXCHANGES) -> list[dict]:
    """company_tickers_exchange.json → 每个 CIK 一个 ticker (取首个, 通常是主股)."""
    fields = payload.get("fields") or []
    seen, out = set(), []
    for row in payload.get("data") or []:
        r = dict(zip(fields, row))
        if r.get("exchange") not in exchanges or not r.get("ticker"):
            continue
        cik = int(r["cik"])
        if cik in seen:
            continue
        seen.add(cik)
        out.append({"cik": cik, "ticker": str(r["ticker"]).upper(), "name": r.get("name", ""),
                    "exchange": r["exchange"]})
    return out


def facts_from_zip(zip_path: Path, ciks: set[int]) -> list[dict]:
    rows: list[dict] = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            try:
                cik = int(Path(name).stem.replace("CIK", ""))
            except ValueError:
                continue
            if cik not in ciks:
                continue
            try:
                rows.extend(extract_facts(json.loads(z.read(name))))
            except Exception:
                continue
    return rows


def write_csv_gz(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


# ---------------- 联网部分 (只在 Windows 上运行) ----------------
def _ua() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        raise SystemExit("请设置环境变量 SEC_USER_AGENT='名字 邮箱' (SEC 要求请求头带联系方式)")
    return ua


def _download(url: str, dest: Path, ua: str, max_age_days: float = 7) -> Path:
    import urllib.request
    if dest.exists() and (time.time() - dest.stat().st_mtime) < max_age_days * 86400:
        print(f"  [cache] {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        total, n = int(r.headers.get("Content-Length") or 0), 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            n += len(chunk)
            if total and n % (50 << 20) < (1 << 20):
                print(f"  {dest.name}: {n >> 20} / {total >> 20} MB")
    os.replace(tmp, dest)
    return dest


def fetch_prices(tickers: list[str], start: str = "2009-01-01", batch: int = 150) -> list[dict]:
    import yfinance as yf
    rows: list[dict] = []
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        for attempt in range(3):
            try:
                df = yf.download(chunk, start=start, interval="1mo", auto_adjust=True,
                                 group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:
                print(f"  prices batch {i} retry {attempt + 1}: {e}")
                time.sleep(10 * (attempt + 1))
        else:
            continue
        for tk in chunk:
            try:
                sub = df[tk] if len(chunk) > 1 else df
                sub = sub.dropna(subset=["Close"])
            except Exception:
                continue
            for ts, r in sub.iterrows():
                rows.append({"date": ts.strftime("%Y-%m-%d"), "ticker": tk,
                             "close": round(float(r["Close"]), 4),
                             "volume": float(r.get("Volume") or 0)})
        print(f"  prices {min(i + batch, len(tickers))}/{len(tickers)}")
        time.sleep(2)
    return rows


def fetch_all(cache: Path = DEFAULT_CACHE, skip_prices: bool = False) -> dict:
    ua = _ua()
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print("[1/3] SEC ticker 列表")
    tick_path = _download(SEC_TICKERS_URL, cache / "company_tickers_exchange.json", ua, 1)
    universe = parse_universe(json.loads(tick_path.read_text(encoding="utf-8")))
    with open(cache / "universe.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cik", "ticker", "name", "exchange"])
        w.writeheader()
        w.writerows(universe)
    print(f"  {len(universe)} 家 (NYSE/Nasdaq)")
    print("[2/3] SEC companyfacts.zip (约 1GB, 首次需要几分钟)")
    zp = _download(SEC_FACTS_ZIP_URL, cache / "companyfacts.zip", ua, 7)
    facts = facts_from_zip(zp, {u["cik"] for u in universe})
    write_csv_gz(cache / "facts.csv.gz", facts, FACT_FIELDS)
    have = {r["cik"] for r in facts}
    print(f"  事实 {len(facts)} 条, 覆盖 {len(have)} 家")
    status = {"generated_at": datetime.now(timezone.utc).isoformat(), "n_universe": len(universe),
              "n_facts": len(facts), "n_companies_with_facts": len(have)}
    if not skip_prices:
        print("[3/3] yfinance 月线")
        tickers = [u["ticker"] for u in universe if u["cik"] in have]
        prices = fetch_prices(tickers)
        write_csv_gz(cache / "prices_monthly.csv.gz", prices, ["date", "ticker", "close", "volume"])
        status["n_price_rows"] = len(prices)
        status["n_price_tickers"] = len({p["ticker"] for p in prices})
    status["elapsed_s"] = round(time.time() - t0)
    (cache / "fetch_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))
    return status


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--skip-prices", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
    fetch_all(Path(a.cache), skip_prices=a.skip_prices)


if __name__ == "__main__":
    main()
