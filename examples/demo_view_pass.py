#!/usr/bin/env python3
"""一次性阅览凭证（view pass）端到端示例（纯标准库）。

角色：
- alice：当前封存人（本地账号，持 X-API-Key）；
- bob/carol/dave：三名见证人（本地账号）；
- 外部复核人：**没有账号**，只凭封存人转交的明文凭证读取一次正文。

演示：
1. 资料按常规流程两票开封，封存人安排未来清除时刻；
2. 封存人凭资料编号 + 带时区阅览截止时刻签发一次性凭证
   （截止时刻必须晚于当前、且早于已安排的清除时刻）；
3. 明文凭证仅在签发响应中返回（库内只存摘要），连同凭证编号一起交给复核人；
4. 复核人无需任何账号凭据，凭明文在截止前读取一次正文与摘要；
5. 再次兑换（以及无效/撤销/过期/已清除）统一 404，不泄露正文或资料存在性；
6. 封存人可凭凭证编号在兑换前撤销（演示第二份凭证）。

用法: python examples/demo_view_pass.py [base_url]
"""
import datetime as dt
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, key: str | None = None, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def main() -> None:
    users = {}
    for name in ["vp-alice", "vp-bob", "vp-carol", "vp-dave"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    owner, w1, w2 = users["vp-alice"], users["vp-bob"], users["vp-carol"]

    # 1) 常规封存 -> 两票开封
    text = "外部复核一次性阅览示例正文：EXTERNAL-REVIEW-SECRET"
    _, sealed = call("POST", "/documents", owner,
                     {"body": text, "witnesses": ["vp-bob", "vp-carol", "vp-dave"]})
    doc_id, digest = sealed["document_id"], sealed["digest"]
    _, req = call("POST", f"/documents/{doc_id}/open-requests", owner,
                  {"reason": "交外部复核人临时阅览", "deadline": in_hours(2)})
    rid = req["request_id"]
    call("POST", f"/open-requests/{rid}/approve", w1, {"digest": digest})
    call("POST", f"/open-requests/{rid}/approve", w2, {"digest": digest})

    # 2) 安排 24 小时后清除（凭证截止时刻必须早于它）
    code, _ = call("PUT", f"/documents/{doc_id}/purge", owner, {"purge_at": in_hours(24)})
    assert code == 200

    # 未安排清除 / 截止晚于清除 / 不带时区 等场景均会被拒（409/400/422）
    code, err = call("POST", f"/documents/{doc_id}/view-passes", owner,
                     {"expires_at": in_hours(48)})
    assert code == 400 and "早于已安排的清除时刻" in err["detail"]

    # 3) 签发：截止时刻取 1 小时后（早于 24 小时后的清除时刻）
    code, issued = call("POST", f"/documents/{doc_id}/view-passes", owner,
                        {"expires_at": in_hours(1)})
    assert code == 201, issued
    pass_id, token = issued["pass_id"], issued["token"]
    print("issued:", json.dumps({k: v for k, v in issued.items() if k != "token"},
                                ensure_ascii=False))
    print("==> 请通过安全渠道把 pass_id 与明文 token 一次性转交给外部复核人：")
    print(f"    pass_id={pass_id}")
    print(f"    token={token}")

    # 4) 外部复核人：无 X-API-Key，凭明文兑换一次
    code, viewed = call("POST", "/view-passes/redeem", None, {"token": token})
    assert code == 200 and viewed["body"] == text
    print("redeemed:", json.dumps({**viewed, "body": viewed["body"]}, ensure_ascii=False))

    # 5) 第二次兑换：统一 404（已兑付），不返回正文
    code, err = call("POST", "/view-passes/redeem", None, {"token": token})
    assert code == 404 and "body" not in err
    print("redeem again ->", code, err["detail"])
    # 无效明文同样 404、同一文案，不区分凭证/资料是否存在
    code, err2 = call("POST", "/view-passes/redeem", None, {"token": "guessed-token"})
    assert code == 404 and err2["detail"] == err["detail"]

    # 6) 第二份凭证：封存人在兑换前凭凭证编号撤销
    _, issued2 = call("POST", f"/documents/{doc_id}/view-passes", owner,
                      {"expires_at": in_hours(1)})
    code, revoked = call("POST", f"/view-passes/{issued2['pass_id']}/revoke", owner)
    assert code == 200 and revoked["status"] == "revoked"
    print("revoked:", json.dumps(revoked, ensure_ascii=False))
    # 撤销后兑换统一 404
    code, _ = call("POST", "/view-passes/redeem", None, {"token": issued2["token"]})
    print("redeem revoked pass ->", code)
    assert code == 404

    # 7) 审计链包含签发/兑换/撤销（外部复核人的兑换 actor 为 null）
    _, audit = call("GET", f"/documents/{doc_id}/audit", owner)
    print("audit actions:", [r["action"] for r in audit["records"]])
    assert "view_pass_issued" in [r["action"] for r in audit["records"]]
    assert any(r["action"] == "view_pass_redeemed" and r["actor"] is None
               for r in audit["records"])
    assert "view_pass_revoked" in [r["action"] for r in audit["records"]]


if __name__ == "__main__":
    main()
