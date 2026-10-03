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


_HIDDEN = {"creationflags": 0x08000000} if os.name == "nt" else {}

# 走 Windows 系统网络设置 (WinINet: 代理 / PAC 自动配置 / WPAD, 与浏览器同一套),
# 用 .NET WebClient 下载. 参数经环境变量传入, 避免引号转义问题.
_PS_DOWNLOAD = r"""
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$wc = New-Object Net.WebClient
$wc.Headers.Add('User-Agent', $env:FSI_UA)
$p = [Net.WebRequest]::GetSystemWebProxy()
$p.Credentials = [Net.CredentialCache]::DefaultCredentials
$wc.Proxy = $p
Write-Output ("route: " + $p.GetProxy([Uri]$env:FSI_URL).AbsoluteUri)
$wc.DownloadFile($env:FSI_URL, $env:FSI_OUT)
Write-Output ("ok " + (Get-Item $env:FSI_OUT).Length)
"""

_PS_PROXY_FOR = r"""
$p = [Net.WebRequest]::GetSystemWebProxy()
Write-Output $p.GetProxy([Uri]$env:FSI_URL).AbsoluteUri
"""


def _run_ps(script: str, env_extra: dict, timeout: int):
    import subprocess
    env = {**os.environ, **env_extra}
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-Command", script], capture_output=True, timeout=timeout,
                          env=env, **_HIDDEN)


def _decode(raw) -> str:
    if not raw:
        return ""
    for enc in (("mbcs",) if os.name == "nt" else ()) + ("cp932", "utf-8"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _download_direct(url: str, dest: Path, ua: str) -> Path:
    import urllib.request
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


def _download_system_route(url: str, dest: Path, ua: str) -> Path:
    """Windows: 按系统网络设置 (与浏览器相同) 下载. 失败抛 RuntimeError (带 PowerShell 输出)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    r = _run_ps(_PS_DOWNLOAD, {"FSI_URL": url, "FSI_OUT": str(tmp), "FSI_UA": ua}, timeout=3 * 3600)
    out, err = _decode(r.stdout).strip(), _decode(r.stderr).strip()
    print(f"  [system-route] {out[-300:]}")
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        raise RuntimeError(f"system route failed rc={r.returncode}: {err[-500:]}")
    os.replace(tmp, dest)
    return dest


def _download(url: str, dest: Path, ua: str, max_age_days: float = 7) -> Path:
    """先 Python 直连; 失败且在 Windows 上 → 改走系统网络设置 (浏览器同款路径)."""
    if dest.exists() and (time.time() - dest.stat().st_mtime) < max_age_days * 86400:
        print(f"  [cache] {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _download_direct(url, dest, ua)
    except Exception as e:
        if os.name != "nt":
            raise
        print(f"  [route] 直连失败 ({e}); 改走 Windows 系统网络设置 (与浏览器相同的代理/自动配置)")
        return _download_system_route(url, dest, ua)


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


def network_diag(hosts=("www.sec.gov", "data.sec.gov", "query1.finance.yahoo.com",
                        "fred.stlouisfed.org", "www.google.com")) -> dict:
    """DNS 解析诊断 (2026-10-03: Windows 上 www.sec.gov getaddrinfo 11002 失败)."""
    import socket
    out = {}
    for h in hosts:
        try:
            out[h] = sorted({ai[4][0] for ai in socket.getaddrinfo(h, 443)})[:3]
        except Exception as e:
            out[h] = f"ERR {e}"
    import urllib.request
    out["proxies"] = urllib.request.getproxies()
    if os.name == "nt":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            inet = {}
            for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL", "AutoDetect"):
                try:
                    inet[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    inet[name] = None
            out["inet_settings"] = inet
        except Exception as e:
            out["inet_settings"] = f"ERR {e}"
        try:
            r = _run_ps(_PS_PROXY_FOR, {"FSI_URL": SEC_TICKERS_URL}, timeout=60)
            out["system_route_for_sec"] = _decode(r.stdout).strip() or _decode(r.stderr).strip()[-200:]
        except Exception as e:
            out["system_route_for_sec"] = f"ERR {e}"
    print("[diag]", json.dumps(out, ensure_ascii=False))
    return out


def _download_or_cached(url: str, dest: Path, ua: str, max_age_days: float) -> Path:
    """下载失败时, 若缓存目录里已有该文件 (例如在别的网络手动下载后放进来), 不论新旧都使用.

    2026-10-03: 本机所在网络无法解析 sec.gov; 不绕过网络限制, 改为允许手动放置.
    """
    try:
        return _with_retry(_download, url, dest, ua, max_age_days)
    except Exception as e:
        if dest.exists() and dest.stat().st_size > 0:
            age = (time.time() - dest.stat().st_mtime) / 86400
            print(f"  [手动文件] 下载失败 ({e}); 使用已放置的 {dest.name} (约 {age:.1f} 天前)")
            return dest
        raise SystemExit(
            f"无法下载 {url} ({e}).\n本机网络无法访问 sec.gov 时: 在其他网络用浏览器下载该文件, "
            f"放到 {dest} 后重跑.")


def _with_retry(fn, *a, tries: int = 4, wait: float = 20, **k):
    for i in range(tries):
        try:
            return fn(*a, **k)
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"  retry {i + 1}/{tries - 1} after error: {e}")
            time.sleep(wait * (i + 1))


def fetch_all(cache: Path = DEFAULT_CACHE, skip_prices: bool = False) -> dict:
    ua = _ua()
    network_diag()
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print("[1/3] SEC ticker 列表")
    tick_path = _download_or_cached(SEC_TICKERS_URL, cache / "company_tickers_exchange.json", ua, 1)
    universe = parse_universe(json.loads(tick_path.read_text(encoding="utf-8")))
    with open(cache / "universe.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cik", "ticker", "name", "exchange"])
        w.writeheader()
        w.writerows(universe)
    print(f"  {len(universe)} 家 (NYSE/Nasdaq)")
    print("[2/3] SEC companyfacts.zip (约 1GB, 首次需要几分钟)")
    zp = _download_or_cached(SEC_FACTS_ZIP_URL, cache / "companyfacts.zip", ua, 7)
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
