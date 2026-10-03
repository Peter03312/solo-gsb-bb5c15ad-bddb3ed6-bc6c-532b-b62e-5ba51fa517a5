#!/usr/bin/env python3
"""审计快照签发与离线验真端到端示例（纯标准库，无需第三方依赖）。

覆盖：
1) 注册账号、封存资料、两票开封；
2) 复核人事先取得签名公钥（GET /audit-snapshots/public-key）并建立信任；
3) 封存人/见证人签发审计快照（POST /documents/{id}/audit-snapshots）：
   快照含资料编号、摘要、当前状态与开封/清除时刻、按原顺序排列且标明
   截止序号的审计记录，不含正文与密钥；
4) 复核人提交“快照 + 签名 + 事先信任的公钥”验真（POST /audit-snapshots/verify）；
5) 篡改字段 / 调换审计记录顺序 / 换版本号 / 用错公钥 -> 全部明确失败；
6) 交接后旧封存人不得再签发（统一 403）。

用法: python examples/demo_audit_snapshot.py [base_url]
"""
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


def show(title: str, status: int, body) -> None:
    print(f"-- {title} -> {status}")
    text = json.dumps(body, ensure_ascii=False)
    print("   " + (text if len(text) <= 400 else text[:400] + " …"))


def main() -> None:
    # 1) 注册与封存
    keys = {}
    for name in ("alice", "bob", "carol", "dave", "erin"):
        status, body = call("POST", "/register", payload={"username": name})
        assert status == 201, body
        keys[name] = body["api_key"]
    status, doc = call(
        "POST", "/documents", keys["alice"],
        {"body": "遗嘱正文（示例）", "witnesses": ["bob", "carol", "dave"]},
    )
    assert status == 201, doc
    doc_id, digest = doc["document_id"], doc["digest"]
    show("封存资料", status, doc)

    # 2) 复核人事先取得签名公钥并建立信任（实际场景应通过可信渠道核对 key_id）
    status, pk = call("GET", "/audit-snapshots/public-key")
    assert status == 200, pk
    trusted_key = pk["public_key"]
    show("签名公钥（复核人事先信任）", status, {"key_id": pk["key_id"], "algorithm": pk["algorithm"]})

    # 3) 两票开封
    import datetime as dt
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).isoformat()
    status, req = call(
        "POST", f"/documents/{doc_id}/open-requests", keys["alice"],
        {"reason": "家庭会议", "deadline": deadline},
    )
    assert status == 201, req
    for w in ("bob", "carol"):
        status, _ = call("POST", f"/open-requests/{req['request_id']}/approve",
                         keys[w], {"digest": digest})
        assert status == 200

    # 4) 封存人签发审计快照（见证人同样可签发）
    status, pkg = call("POST", f"/documents/{doc_id}/audit-snapshots", keys["alice"])
    assert status == 201, pkg
    snap = pkg["snapshot"]
    show("签发审计快照", status, {
        "key_id": pkg["key_id"],
        "audit_upto": snap["audit_upto"],
        "status": snap["status"],
        "records": len(snap["records"]),
        "signature": pkg["signature"][:24] + "…",
    })
    assert "遗嘱正文" not in json.dumps(pkg, ensure_ascii=False)  # 快照不含正文

    # 5) 复核人验真：快照 + 签名 + 事先信任的公钥
    status, out = call("POST", "/audit-snapshots/verify",
                       payload={"snapshot": snap, "signature": pkg["signature"],
                                "public_key": trusted_key})
    show("验真通过", status, out)
    assert status == 200 and out["valid"] is True and out["audit_upto"] == snap["audit_upto"]

    # 6) 篡改场景：全部明确失败
    bad = json.loads(json.dumps(snap)); bad["status"] = "sealed"
    status, out = call("POST", "/audit-snapshots/verify",
                       payload={"snapshot": bad, "signature": pkg["signature"],
                                "public_key": trusted_key})
    show("篡改状态字段 -> 明确失败", status, out)
    assert status == 400

    bad = json.loads(json.dumps(snap)); bad["records"] = bad["records"][::-1]
    status, out = call("POST", "/audit-snapshots/verify",
                       payload={"snapshot": bad, "signature": pkg["signature"],
                                "public_key": trusted_key})
    show("调换审计记录顺序 -> 明确失败", status, out)
    assert status == 400

    bad = json.loads(json.dumps(snap)); bad["version"] = 2
    status, out = call("POST", "/audit-snapshots/verify",
                       payload={"snapshot": bad, "signature": pkg["signature"],
                                "public_key": trusted_key})
    show("不支持的版本 -> 明确失败", status, out)
    assert status == 400

    status, out = call("POST", "/audit-snapshots/verify",
                       payload={"snapshot": snap, "signature": pkg["signature"],
                                "public_key": "not-a-public-key"})
    show("公钥无效 -> 明确失败", status, out)
    assert status == 400

    # 7) 交接后旧封存人不得再签发
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    status, inv = call("POST", f"/documents/{doc_id}/handover-invitations", keys["alice"],
                       {"to_username": "erin", "confirm_deadline": deadline})
    # 资料已开封不能交接；换一份未开封资料演示
    assert status == 409
    status, doc2 = call("POST", "/documents", keys["alice"],
                        {"body": "另一份未开封资料", "witnesses": ["bob", "carol", "dave"]})
    doc2_id = doc2["document_id"]
    status, inv = call("POST", f"/documents/{doc2_id}/handover-invitations", keys["alice"],
                       {"to_username": "erin", "confirm_deadline": deadline})
    assert status == 201, inv
    status, _ = call("POST", f"/handover-invitations/{inv['invitation_id']}/confirm", keys["erin"])
    assert status == 200
    status, out = call("POST", f"/documents/{doc2_id}/audit-snapshots", keys["alice"])
    show("交接后旧封存人再签发 -> 统一 403", status, out)
    assert status == 403
    status, out = call("POST", f"/documents/{doc2_id}/audit-snapshots", keys["erin"])
    show("新封存人可正常签发", status, {"audit_upto": out["snapshot"]["audit_upto"]})
    assert status == 201

    print("\n全部演示通过。")


if __name__ == "__main__":
    main()
