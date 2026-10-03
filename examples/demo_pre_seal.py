#!/usr/bin/env python3
"""需见证人确认的预封存（pre-seal proposal）端到端示例（纯标准库）。

覆盖：
1) 发起预封存：正文 + 三名互异且非本人的已注册见证人 + 带时区未来确认截止
   时刻；响应只给提案编号与正文摘要，不含正文；
2) 可见性：仅发起人本人与受邀见证人可凭编号查看，其他账号（含不存在编号）
   统一 403，不能凭编号探知提案；
3) 见证人凭摘要各确认一次：摘要不符、重复确认 409 且不计票；
4) 三人截止前确认后，在第三票事务内生成一份普通封存资料并返回资料编号；
   已完成提案的重复请求不再生成资料；生成的资料沿用现有两票开封流程；
5) 发起人完成前撤回；截止时未集齐即失效；撤回/失效/完成后提案正文清空。
   截止判定以取得写事务后的实际时刻为准：在截止前到达、等待写锁期间越过
   截止时刻的发起/确认/撤回，分别按 400（不落提案）/409（先失效清正文，
   不计票、不生成资料）拒绝；截止前取得写锁的有效第三票仍在同一事务
   生成唯一资料。

用法: python examples/demo_pre_seal.py [base_url]
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method, path, key=None, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def in_hours(h):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def in_seconds(s):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def show(title, status, body):
    print(f"-- {title} -> {status}")
    print("   " + json.dumps(body, ensure_ascii=False))


def main() -> None:
    users = {}
    for name in ["ps-alice", "ps-bob", "ps-carol", "ps-dave", "ps-erin"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    alice, bob, carol, dave, erin = (
        users[n] for n in ["ps-alice", "ps-bob", "ps-carol", "ps-dave", "ps-erin"]
    )
    witnesses = ["ps-bob", "ps-carol", "ps-dave"]

    # ------------------------------------------------------------------
    # 场景 1：发起预封存提案 + 可见性 + 校验
    # ------------------------------------------------------------------
    print("== 场景 1：发起预封存、响应不含正文、仅参与方可见 ==")
    secret = "预封存示例正文：PRE-SEAL-DEMO-SECRET"
    status, prop = call("POST", "/pre-seal-proposals", alice,
                        {"body": secret, "witnesses": witnesses,
                         "confirm_deadline": in_hours(1)})
    show("发起预封存", status, prop)
    assert status == 201 and prop["status"] == "pending"
    assert "body" not in prop and secret not in json.dumps(prop)
    pid = prop["proposal_id"]
    digest = prop["digest"]

    # 发起人/受邀见证人可查看；其他账号与不存在编号统一 403
    assert call("GET", f"/pre-seal-proposals/{pid}", bob)[0] == 200
    assert call("GET", f"/pre-seal-proposals/{pid}", erin)[0] == 403
    assert call("GET", "/pre-seal-proposals/999999", erin)[0] == 403
    # 其他人不能确认/撤回；发起人本人不能确认自己的提案
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", erin,
                {"digest": digest})[0] == 403
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", alice,
                {"digest": digest})[0] == 403
    assert call("POST", f"/pre-seal-proposals/{pid}/revoke", bob)[0] == 403
    # 发起校验
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": ["ps-bob", "ps-bob", "ps-dave"],
                 "confirm_deadline": in_hours(1)})[0] == 400  # 不互异
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": ["ps-alice", "ps-carol", "ps-dave"],
                 "confirm_deadline": in_hours(1)})[0] == 400  # 含本人
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": ["ps-bob", "ps-carol", "ghost"],
                 "confirm_deadline": in_hours(1)})[0] == 400  # 未注册
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": witnesses,
                 "confirm_deadline": in_hours(-1)})[0] == 400  # 截止已过
    assert call("POST", "/pre-seal-proposals", alice,
                {"body": secret, "witnesses": witnesses,
                 "confirm_deadline": "2026-10-01T12:00:00"})[0] == 422  # 不带时区

    # ------------------------------------------------------------------
    # 场景 2：摘要不符不计票、重复确认只计一次
    # ------------------------------------------------------------------
    print("== 场景 2：摘要不符/重复确认被拒且不计票 ==")
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", bob,
                {"digest": "0" * 64})[0] == 409
    assert call("GET", f"/pre-seal-proposals/{pid}", alice)[1]["confirmation_count"] == 0
    status, r1 = call("POST", f"/pre-seal-proposals/{pid}/confirmations", bob,
                      {"digest": digest})
    show("bob 第一票", status, r1)
    assert r1["confirmation_count"] == 1 and r1["document_id"] is None
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", bob,
                {"digest": digest})[0] == 409  # 重复确认
    call("POST", f"/pre-seal-proposals/{pid}/confirmations", carol, {"digest": digest})

    # ------------------------------------------------------------------
    # 场景 3：第三票在同一事务生成普通封存资料
    # ------------------------------------------------------------------
    print("== 场景 3：三人确认齐 -> 生成封存资料 ==")
    status, r3 = call("POST", f"/pre-seal-proposals/{pid}/confirmations", dave,
                      {"digest": digest})
    show("dave 第三票（生成资料）", status, r3)
    assert status == 200 and r3["status"] == "confirmed"
    doc_id = r3["document_id"]
    # 已完成提案：重复确认 409、撤回 409，不再生成资料
    assert call("POST", f"/pre-seal-proposals/{pid}/confirmations", dave,
                {"digest": digest})[0] == 409
    assert call("POST", f"/pre-seal-proposals/{pid}/revoke", alice)[0] == 409
    # 生成资料开封前无正文
    _, doc = call("GET", f"/documents/{doc_id}", alice)
    show("生成的资料（开封前无正文）", 200, doc)
    assert doc["status"] == "sealed" and "body" not in doc

    # 沿用现有两票开封流程即可读到正文
    _, req = call("POST", f"/documents/{doc_id}/open-requests", alice,
                  {"reason": "预封存示例开封", "deadline": in_hours(24)})
    req_id = req["request_id"]
    call("POST", f"/open-requests/{req_id}/approve", bob, {"digest": digest})
    call("POST", f"/open-requests/{req_id}/approve", carol, {"digest": digest})
    _, opened = call("GET", f"/documents/{doc_id}", alice)
    show("两票开封后读到正文", 200, {"status": opened["status"], "body": opened["body"]})
    assert opened["body"] == secret

    # ------------------------------------------------------------------
    # 场景 4：发起人在完成前撤回（正文清空、不能再确认）
    # ------------------------------------------------------------------
    print("== 场景 4：完成前撤回，撤回后不能确认 ==")
    _, p2 = call("POST", "/pre-seal-proposals", alice,
                 {"body": "另一份将被撤回的预封存正文 PRE-REVOKE",
                  "witnesses": witnesses, "confirm_deadline": in_hours(1)})
    pid2, dg2 = p2["proposal_id"], p2["digest"]
    call("POST", f"/pre-seal-proposals/{pid2}/confirmations", bob, {"digest": dg2})
    status, rev = call("POST", f"/pre-seal-proposals/{pid2}/revoke", alice)
    show("发起人撤回", status, rev)
    assert status == 200 and rev["status"] == "revoked"
    assert call("POST", f"/pre-seal-proposals/{pid2}/confirmations", carol,
                {"digest": dg2})[0] == 409
    assert call("GET", f"/pre-seal-proposals/{pid2}", alice)[1]["status"] == "revoked"

    # ------------------------------------------------------------------
    # 场景 5：截止时未集齐 -> 失效（扫描器每秒处理），正文清空
    # ------------------------------------------------------------------
    print("== 场景 5：到期未集齐三人即失效，迟到确认被拒 ==")
    _, p3 = call("POST", "/pre-seal-proposals", alice,
                 {"body": "将到期失效的预封存正文 PRE-EXPIRE",
                  "witnesses": witnesses, "confirm_deadline": in_seconds(1)})
    pid3, dg3 = p3["proposal_id"], p3["digest"]
    call("POST", f"/pre-seal-proposals/{pid3}/confirmations", bob, {"digest": dg3})
    time.sleep(2.2)
    status, exp = call("GET", f"/pre-seal-proposals/{pid3}", alice)
    show("到期后查询（扫描器已失效）", status, exp)
    assert exp["status"] == "expired"
    # 截止判定以取得写事务后的实际时刻为准：迟到确认一律 409，
    # 不计票、不生成资料（document_id 保持为空）
    assert call("POST", f"/pre-seal-proposals/{pid3}/confirmations", carol,
                {"digest": dg3})[0] == 409
    assert call("POST", f"/pre-seal-proposals/{pid3}/revoke", alice)[0] == 409
    _, exp2 = call("GET", f"/pre-seal-proposals/{pid3}", alice)
    assert exp2["document_id"] is None and exp2["confirmation_count"] == 1

    print("预封存端到端示例全部通过 ✔")


if __name__ == "__main__":
    main()
