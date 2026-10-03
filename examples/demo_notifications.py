#!/usr/bin/env python3
"""站内通知功能调用示例（纯标准库，无需第三方依赖）。

覆盖本轮新增：
1) 资料开封 -> 当时封存人 + 三名见证人各收到一条 opened 通知；
2) 见证人替换 -> 封存人 + 旧见证人 + 新见证人收到 witness_replaced，
   旧见证人随即失去资料权限但仍能读自己的通知；
3) 交接确认 -> 新旧封存人 + 三名见证人收到 handover_confirmed；
4) 到期清除（扫描器结算）-> 参与方各收到一条 purged；
5) 非负事件序号游标 + 1～100 页大小分页读取、下一游标；
6) 按通知编号标记已读（幂等，返回已读状态）；他人通知编号 403、
   非法游标/页大小 422；通知只含资料编号/事件类型/时刻/已读状态，
   不含正文、凭证或密钥。

用法: python examples/demo_notifications.py [base_url]
"""
import datetime as dt
import json
import sys
import time
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


def in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def show(title: str, status: int, body) -> None:
    print(f"-- {title} -> {status}")
    print("   " + json.dumps(body, ensure_ascii=False))


def list_notifications(key: str, cursor: int = 0, page_size: int = 20):
    return call("GET", f"/notifications?cursor={cursor}&page_size={page_size}", key)


def types(key: str):
    # 用游标翻页直到 next_cursor 为 null，收集本人全部通知类型
    out, cur = [], 0
    while True:
        _, data = list_notifications(key, cur, 100)
        out.extend(n["event_type"] for n in data["notifications"])
        if data["next_cursor"] is None:
            return out
        cur = data["next_cursor"]


def main() -> None:
    users = {}
    for name in ["alice", "bob", "carol", "dave", "erin"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    owner, bob, carol, dave, erin = (users[n] for n in
                                     ["alice", "bob", "carol", "dave", "erin"])

    # ------------------------------------------------------------------
    # 1) 封存并两票开封 -> 封存人 + 三名见证人各收到 opened
    # ------------------------------------------------------------------
    print("== 1) 资料开封通知 ==")
    _, sealed = call("POST", "/documents", owner,
                     {"body": "通知示例正文 NOTIFY-DEMO-SECRET",
                      "witnesses": ["bob", "carol", "dave"]})
    doc1, digest1 = sealed["document_id"], sealed["digest"]
    _, req = call("POST", f"/documents/{doc1}/open-requests", owner,
                  {"reason": "家庭会议", "deadline": in_hours(24)})
    req1 = req["request_id"]
    call("POST", f"/open-requests/{req1}/approve", bob, {"digest": digest1})
    status, opened = call("POST", f"/open-requests/{req1}/approve", carol,
                          {"digest": digest1})
    show("第二票同意（开封）", status, opened)
    assert opened["status"] == "opened"

    s, data = list_notifications(owner)
    show("封存人通知列表", s, data)
    assert s == 200 and [n["event_type"] for n in data["notifications"]] == ["opened"]
    note = data["notifications"][0]
    # 通知字段固定：不含正文、凭证或密钥
    assert set(note) == {"notification_id", "event_seq", "document_id",
                         "event_type", "at", "is_read"}
    assert note["is_read"] is False and note["document_id"] == doc1
    assert types(bob) == ["opened"] and types(dave) == ["opened"]

    # ------------------------------------------------------------------
    # 2) 见证人替换通知：封存人 + 旧见证人 dave + 新见证人 erin
    # ------------------------------------------------------------------
    print("== 2) 见证人替换通知 ==")
    _, sealed2 = call("POST", "/documents", owner,
                      {"body": "替换通知示例正文", "witnesses": ["bob", "carol", "dave"]})
    doc2 = sealed2["document_id"]
    status, resp = call("POST", f"/documents/{doc2}/witness-replacements", owner,
                        {"old_witness": "dave", "new_witness": "erin"})
    show("替换 dave -> erin", status, resp)
    assert status == 200

    assert types(owner) == ["opened", "witness_replaced"]
    assert types(bob) == ["opened", "witness_replaced"]
    assert types(erin) == ["witness_replaced"]
    # 旧见证人 dave 收到通知；他已失去资料权限，但通知仍可读
    assert types(dave) == ["opened", "witness_replaced"]
    s, _ = call("GET", f"/documents/{doc2}", dave)
    print(f"-- 旧见证人 dave 访问资料 -> {s}（403：权限已收回，通知仍可读）")
    assert s == 403

    # ------------------------------------------------------------------
    # 3) 交接确认通知：新旧封存人 + 三名见证人
    # ------------------------------------------------------------------
    print("== 3) 交接确认通知 ==")
    _, sealed3 = call("POST", "/documents", owner,
                      {"body": "交接通知示例正文", "witnesses": ["bob", "carol", "dave"]})
    doc3 = sealed3["document_id"]
    _, inv = call("POST", f"/documents/{doc3}/handover-invitations", owner,
                  {"to_username": "erin", "confirm_deadline": in_hours(1)})
    inv_id = inv["invitation_id"]
    status, resp = call("POST", f"/handover-invitations/{inv_id}/confirm", erin)
    show("受让人 erin 确认交接", status, resp)
    assert status == 200
    assert types(owner)[-1] == "handover_confirmed"
    assert types(erin)[-1] == "handover_confirmed"
    for k in (bob, carol, dave):
        assert types(k)[-1] == "handover_confirmed"

    # ------------------------------------------------------------------
    # 4) 到期清除通知（安排 1 秒后清除，等扫描器结算）
    # ------------------------------------------------------------------
    print("== 4) 到期清除通知 ==")
    status, resp = call("PUT", f"/documents/{doc1}/purge", owner,
                        {"purge_at": in_seconds(1)})
    show("安排 1 秒后清除", status, resp)
    assert status == 200
    for _ in range(40):
        time.sleep(0.1)
        if types(owner)[-1] == "purged":
            break
    show("封存人通知（最后一条应为 purged）", 200, types(owner))
    assert types(owner)[-2:] == ["handover_confirmed", "purged"] or types(owner)[-1] == "purged"
    assert types(bob)[-1] == "purged" and types(carol)[-1] == "purged"
    assert types(dave)[-1] == "purged"
    # 通知里永远不带正文
    _, data = list_notifications(owner)
    assert all("body" not in n for n in data["notifications"])

    # ------------------------------------------------------------------
    # 5) 游标分页 + 6) 标记已读与拒绝场景
    # ------------------------------------------------------------------
    print("== 5) 游标分页 ==")
    s, page1 = list_notifications(owner, cursor=0, page_size=2)
    show("page_size=2 第一页", s, page1)
    assert s == 200 and len(page1["notifications"]) == 2
    assert page1["next_cursor"] == page1["notifications"][-1]["event_seq"]
    s, empty = list_notifications(owner, cursor=10**18, page_size=100)
    show("越过末尾的游标", s, empty)
    assert empty == {"notifications": [], "next_cursor": None}

    print("== 6) 标记已读 / 幂等 / 越权与非法参数 ==")
    nid = page1["notifications"][0]["notification_id"]
    s, read1 = call("POST", f"/notifications/{nid}/read", owner)
    show("首次标记已读", s, read1)
    assert s == 200 and read1["is_read"] is True and read1["read_at"]
    time.sleep(0.01)
    s, read2 = call("POST", f"/notifications/{nid}/read", owner)
    show("重复标记（幂等，read_at 不变）", s, read2)
    assert read2["read_at"] == read1["read_at"]

    # 他人不能标记 alice 的通知 -> 403；不存在编号统一 403
    assert call("POST", f"/notifications/{nid}/read", bob)[0] == 403
    assert call("POST", "/notifications/999999/read", bob)[0] == 403
    # 非法游标 / 页大小 -> 422；缺少凭据 -> 401
    assert call("GET", "/notifications?cursor=-1", owner)[0] == 422
    assert call("GET", "/notifications?page_size=101", owner)[0] == 422
    assert call("GET", "/notifications", None)[0] == 401

    print("\n全部通知示例断言通过。")


if __name__ == "__main__":
    main()
