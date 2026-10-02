"""F10 (MODEL_AUDIT 2026-09-19): 校准 / HMM 版本指纹.

只做"可追溯": 给决策时用到的校准文件与 HMM 状态一个确定性 id, 并记录到
决策结果 (decision["model_versions"]) 与 trade_log. 不改变任何决策逻辑,
不触发重训 (审计: 未重新校准不自动证明参数失效).
"""
from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Any, Optional


def _plain(o: Any) -> Any:
    """MappingProxyType / tuple / numpy 标量 → 纯 JSON 结构 (context 快照是只读包装)."""
    if isinstance(o, (dict, MappingProxyType)):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            return o.item()
        except Exception:
            pass
    return o


def fingerprint(obj: Any, n: int = 12) -> str:
    raw = json.dumps(_plain(obj), sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def calibration_version(data: Optional[Any]) -> dict:
    if not data:
        return {"id": None, "status": "missing"}
    d = _plain(data)
    return {"id": f"calib-{fingerprint(d)}", "trained_at": d.get("ts"),
            "n_tickers": len(d.get("tickers") or []),
            "lookback_days": d.get("lookback_days"), "forward_days": d.get("forward_days")}


def hmm_version(info: Optional[Any]) -> dict:
    if not info:
        return {"id": None, "status": "missing"}
    d = _plain(info)
    vid = d.get("version_id")
    return {"id": vid or f"hmm-legacy-{fingerprint(d)}",
            "frozen": bool(vid),
            "trained_at": d.get("ts_utc") or d.get("ts"),
            "train_start": d.get("train_start"), "train_end": d.get("train_end"),
            "label": d.get("current_label"), "prob": d.get("current_prob")}
