"""_webui_watchdog.py
─────────────────────
守护 webui.py：检查 http://127.0.0.1:8080/api/health 是否响应；
不响应就重启 webui.bat（脱离终端，daemon 模式）。

用法：
  · Windows Task Scheduler 每 5 分钟跑一次 webui_watchdog.bat
  · 也可手动跑：python _webui_watchdog.py

输出：
  signals/webui_watchdog.jsonl（每次执行追加一条 { ts, event, msg }）
  event: healthy / restart / restart_failed / unresponsive / hung_restart /
         hung_no_webui_pid

2026-09-29: 区分 "dead" 与 "slow".
  · 端口没人监听 → dead → 立即拉起.
  · 端口仍在监听但 health 失败 → unresponsive, 不拉新实例 (避免多实例并存);
    连续 UNRESPONSIVE_LIMIT 次 (≈15 分钟) 才结束监听 8080 的 webui.py 进程再拉起.
  计数存在 signals/webui_watchdog_state.json.

2026-10-02: 文件触发重启 (不需要人操作电脑).
  · 写入 signals/webui_restart_request.json (内容可选 {"reason": ...}).
  · 下一次运行 (≤5 分钟): 只结束监听 8080 且命令行含 webui.py 的进程,
    等端口释放后拉起新实例, 请求文件改名 .done (一次请求只执行一次).
  · 端口被非 webui 进程占用 → 不杀不拉 (requested_restart_skipped);
    结束后端口仍未释放 → 不叠第二个实例 (requested_restart_failed).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
LOG_PATH   = SCRIPT_DIR / "signals" / "webui_watchdog.jsonl"
HEALTH_URL = "http://127.0.0.1:8080/api/health"
WEBUI_BAT  = SCRIPT_DIR / "webui.bat"
STATE_PATH = SCRIPT_DIR / "signals" / "webui_watchdog_state.json"
HOST, PORT = "127.0.0.1", 8080
RESTART_REQUEST_PATH = SCRIPT_DIR / "signals" / "webui_restart_request.json"
PORT_RELEASE_WAIT_S = 30
UNRESPONSIVE_LIMIT = 3          # 连续 3 次 (5 分钟一次) 无响应才判定卡死
# 任务计划用 pythonw 运行; 子进程 (netstat/powershell/taskkill) 不加这个会闪出控制台窗口
_HIDDEN = {"creationflags": 0x08000000} if os.name == "nt" else {}

# Env vars 保持与 webui.bat 一致
WEBUI_ENV = {
    "PYTHONUTF8":       "1",
    "PYTHONIOENCODING": "utf-8",
    "WEBUI_HOST":       "127.0.0.1",
    "WEBUI_PORT":       "8080",
}


def _webui_alive(timeout: int = 5) -> bool:
    try:
        req = urllib.request.Request(HEALTH_URL, headers={"User-Agent": "webui-watchdog"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _webui_healthy() -> bool:
    """两次尝试, 单次 10 秒; 一次偶发慢响应不算失败."""
    return _webui_alive(timeout=10) or _webui_alive(timeout=10)


def _port_listening(host: str = HOST, port: int = PORT, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _webui_listener_pids(port: int = PORT) -> list[int]:
    """监听 port 且命令行含 webui.py 的进程 PID (Windows). 其他进程一律不返回."""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15, **_HIDDEN).stdout
    except Exception:
        return []
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" \
                and parts[1].endswith(f":{port}"):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                pass
    result = []
    for pid in sorted(pids):
        try:
            cmd = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=20, **_HIDDEN).stdout
        except Exception:
            continue
        if "webui.py" in (cmd or ""):
            result.append(pid)
    return result


def _kill_pid(pid: int) -> bool:
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=20, **_HIDDEN)
        return r.returncode == 0
    except Exception:
        return False


def _log(event: str, msg: str = "") -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts":    datetime.now(timezone.utc).isoformat(),
                "event": event,
                "msg":   msg,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _launch_webui() -> int | None:
    """脱离父进程启动 webui.bat 后台运行。"""
    env = os.environ.copy()
    env.update(WEBUI_ENV)
    try:
        DETACHED = 0x00000008
        NEW_GROUP = 0x00000200
        NO_WINDOW = 0x08000000       # CREATE_NO_WINDOW — 关键: 让 cmd/webui 完全不弹窗
        proc = subprocess.Popen(
            ["cmd.exe", "/c", str(WEBUI_BAT)],
            cwd=str(SCRIPT_DIR),
            env=env,
            creationflags=DETACHED | NEW_GROUP | NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return proc.pid
    except Exception as e:
        _log("launch_failed", f"{e}")
        return None


def _consume_restart_request() -> str | None:
    """有请求 → 改名 .done 并返回 reason (先消费, 保证一次请求只执行一次)."""
    if not RESTART_REQUEST_PATH.exists():
        return None
    reason = ""
    try:
        reason = str((json.loads(RESTART_REQUEST_PATH.read_text(encoding="utf-8") or "{}")
                      or {}).get("reason") or "")
    except Exception:
        pass
    done = RESTART_REQUEST_PATH.with_suffix(".done")
    try:
        os.replace(RESTART_REQUEST_PATH, done)
    except OSError as e:
        _log("requested_restart_failed", f"cannot consume request: {e}")
        return None
    return reason or "(no reason)"


def _handle_restart_request(reason: str) -> int:
    pids = _webui_listener_pids() if _port_listening() else []
    if _port_listening() and not pids:
        _log("requested_restart_skipped", f"port {PORT} held by non-webui process; reason={reason}")
        return 3
    killed = [pid for pid in pids if _kill_pid(pid)]
    for _ in range(PORT_RELEASE_WAIT_S):
        if not _port_listening():
            break
        time.sleep(1)
    else:
        _log("requested_restart_failed", f"port {PORT} not released; killed={killed}; reason={reason}")
        return 2
    _save_state({"unresponsive": 0})
    new_pid = _launch_webui()
    _log("requested_restart", f"killed={killed} new pid={new_pid}; reason={reason}")
    return 1 if new_pid else 2


def main() -> int:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reason = _consume_restart_request()
    if reason is not None:
        rc = _handle_restart_request(reason)
        print(f"[{now_str}] restart request ({reason}) → rc={rc}")
        return rc
    state = _load_state()
    if _webui_healthy():
        if state.get("unresponsive"):
            _save_state({"unresponsive": 0})
        _log("healthy")
        print(f"[{now_str}] webui alive @ {HEALTH_URL}")
        return 0

    if _port_listening():
        # 进程还在占着端口, 只是没响应: 不叠新实例
        n = int(state.get("unresponsive", 0)) + 1
        if n < UNRESPONSIVE_LIMIT:
            _save_state({"unresponsive": n})
            _log("unresponsive", f"{n}/{UNRESPONSIVE_LIMIT}")
            print(f"[{now_str}] webui unresponsive ({n}/{UNRESPONSIVE_LIMIT}), waiting")
            return 0
        pids = _webui_listener_pids()
        if not pids:
            _save_state({"unresponsive": n})
            _log("hung_no_webui_pid", f"port {PORT} held by non-webui process")
            print(f"[{now_str}] port {PORT} busy but no webui.py listener found; not launching")
            return 3
        killed = [pid for pid in pids if _kill_pid(pid)]
        _save_state({"unresponsive": 0})
        new_pid = _launch_webui()
        _log("hung_restart", f"killed={killed} new pid={new_pid}")
        print(f"[{now_str}] webui hung → killed {killed}, relaunched (new pid {new_pid})")
        return 1

    # 端口没人监听 → 真死了
    _save_state({"unresponsive": 0})
    new_pid = _launch_webui()
    if new_pid:
        _log("restart", f"new pid={new_pid}")
        print(f"[{now_str}] webui dead → restarted (new pid {new_pid})")
        try:
            from notifications import send_alert
            send_alert(f"webui 自愈重启 (new pid {new_pid})", level="info")
        except Exception:
            pass
        return 1
    _log("restart_failed")
    print(f"[{now_str}] webui dead + relaunch failed")
    return 2


if __name__ == "__main__":
    sys.exit(main())
