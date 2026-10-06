"""只读诊断: 在本机请求 WebUI 的几个接口, 结果写 logs/webui_diag_last.json (2026-10-06)."""
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).resolve().parent / "logs" / "webui_diag_last.json"
ENDPOINTS = ["/api/health", "/api/positions", "/api/ai_targets", "/api/nav", "SLEEP20", "/api/positions"]


def main():
    res = {"ts": datetime.now(timezone.utc).isoformat()}
    for i, ep in enumerate(ENDPOINTS):
        if ep == "SLEEP20":          # 缓存过期时第一次请求只触发后台刷新, 等一下再取
            time.sleep(20)
            continue
        key = ep if ep not in res else f"{ep}#{i}"
        t0 = time.time()
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080" + ep, timeout=60) as r:
                body = r.read()
            try:
                data = json.loads(body)
            except Exception:
                data = body[:500].decode("utf-8", "replace")
            res[key] = {"status": "ok", "seconds": round(time.time() - t0, 2), "data": data}
        except Exception as e:
            res[key] = {"status": f"error: {type(e).__name__}: {e}", "seconds": round(time.time() - t0, 2)}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str)[:400000], encoding="utf-8")


if __name__ == "__main__":
    main()
