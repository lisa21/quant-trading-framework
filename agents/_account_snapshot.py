"""只读: 账户资金全字段 + 持仓 + 当日订单 → logs/account_snapshot_last.json (2026-10-07).

为什么: 持仓市值合计 (~161 万) 与 total_assets (~128 万) 和 cash (~15 万) 对不上,
需要 moomoo accinfo 的全部字段 (各币种现金 / 负债 / 购买力) 才能判断是否在借钱.
只调用 *_query, 不下单、不改账户.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from moomoo import OpenSecTradeContext, SecurityFirm, TrdEnv, TrdMarket, RET_OK

from config import MOOMOO_ACC_ID, OPEND_HOST, OPEND_PORT

OUT = Path(__file__).resolve().parent / "logs" / "account_snapshot_last.json"


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):
        try:
            return _clean(v.item())
        except Exception:
            pass
    return v if isinstance(v, (int, float, str, bool)) or v is None else str(v)


def _rows(df):
    if df is None or getattr(df, "empty", True):
        return []
    return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]


def main():
    res = {"ts": datetime.now(timezone.utc).isoformat()}
    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=OPEND_HOST, port=OPEND_PORT,
                              security_firm=SecurityFirm.FUTUSECURITIES)
    try:
        for cur in ("USD", "JPY", "HKD"):
            try:
                ret, info = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=MOOMOO_ACC_ID, currency=cur)
                res[f"accinfo_{cur}"] = _rows(info) if ret == RET_OK else f"error: {info}"
            except Exception as e:
                res[f"accinfo_{cur}"] = f"exception: {e}"
        try:
            ret, acc = ctx.get_acc_list()
            res["acc_list"] = _rows(acc) if ret == RET_OK else f"error: {acc}"
        except Exception as e:
            res["acc_list"] = f"exception: {e}"
        ret, pos = ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=MOOMOO_ACC_ID)
        res["positions"] = _rows(pos) if ret == RET_OK else f"error: {pos}"
        ret, od = ctx.order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=MOOMOO_ACC_ID)
        res["orders_today"] = _rows(od) if ret == RET_OK else f"error: {od}"
    finally:
        ctx.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written", OUT)


if __name__ == "__main__":
    main()
