"""站内通知功能测试。

规则要点（规格）：
- 资料开封（opened）时向当时封存人 + 三名见证人各记一条；
- 到期清除（purged）时向当时封存人 + 三名见证人各记一条；
- 见证人替换（witness_replaced）时通知封存人 + 旧见证人 + 新见证人；
- 交接确认（handover_confirmed）时通知新旧封存人 + 三名见证人；
- 收件人在状态变更的同一事务内固定；通知按事件序号（event_seq，取主审计
  记录的 audit_log.id）排列；只含资料编号、事件类型、时刻、已读状态，
  不含正文、凭证或密钥；
- 账号凭现有 X-API-Key 以非负游标（>=0）与 1～100 页大小读取本人通知及
  下一游标；按通知编号标记已读并返回已读状态；
- 非法游标/页大小 422；他人或不存在通知编号统一 403 且不改状态；
  已读重试幂等，不产生额外记录；
- 失去资料权限的人仍可读自己收到的通知，但通知不提供资料访问；
- 失败或回滚的操作不留通知；扫描器与请求并发结算同一变化不重复投递。
"""
import datetime as dt
import time

import pytest
from fastapi.testclient import TestClient

from conftest import auth, register, seal

BODY = "通知测试绝密正文 NOTIFICATION-SECRET"


def _in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def _in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def _setup(client, names=("owner", "w1", "w2", "w3", "new")):
    keys = {n: register(client, n) for n in names}
    d = seal(client, keys["owner"], BODY, ["w1", "w2", "w3"])
    return keys, d["document_id"], d["digest"]


def _create_request(client, owner_key, doc_id, *, deadline_h=2, earliest_s=None):
    payload = {"reason": "通知测试", "deadline": _in_hours(deadline_h)}
    if earliest_s is not None:
        payload["earliest_open_at"] = _in_seconds(earliest_s)
    r = client.post(f"/documents/{doc_id}/open-requests", json=payload, headers=auth(owner_key))
    assert r.status_code == 201, r.text
    return r.json()["request_id"]


def _approve(client, key, req, dg):
    return client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(key))


def _open(client, keys, doc_id, dg, req=None):
    if req is None:
        req = _create_request(client, keys["owner"], doc_id)
    assert _approve(client, keys["w1"], req, dg).status_code == 200
    r = _approve(client, keys["w2"], req, dg)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "opened"
    return req


def _notifications(client, key, **params):
    return client.get("/notifications", params=params, headers=auth(key))


def _all_notifications(client, key):
    out, cursor = [], 0
    while True:
        r = _notifications(client, key, cursor=cursor, page_size=100)
        assert r.status_code == 200, r.text
        data = r.json()
        out.extend(data["notifications"])
        if data["next_cursor"] is None:
            return out
        cursor = data["next_cursor"]


def _types(client, key):
    return [n["event_type"] for n in _all_notifications(client, key)]


@pytest.fixture
def passive_client(tmp_path, monkeypatch):
    """后台扫描间隔拉到 1 小时：验证查询/写事务临界区自行做到点清除与投递。"""
    monkeypatch.setenv("SEAL_DB_PATH", str(tmp_path / "passive-notify.db"))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")
    from app.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# 开封通知：封存人 + 三名见证人各一条；无关账号无通知
# ---------------------------------------------------------------------------


def test_opened_fanout_to_owner_and_three_witnesses(client):
    keys, doc_id, dg = _setup(client)
    _open(client, keys, doc_id, dg)

    for name in ("owner", "w1", "w2", "w3"):
        notes = _all_notifications(client, keys[name])
        assert len(notes) == 1, (name, notes)
        n = notes[0]
        assert n["event_type"] == "opened"
        assert n["document_id"] == doc_id
        assert n["is_read"] is False
        assert n["at"]
        # 通知只含规定字段，绝不含正文/凭证/密钥
        assert set(n) == {"notification_id", "event_seq", "document_id",
                          "event_type", "at", "is_read"}
        assert BODY not in str(n) and dg not in str(n)

    # 无关账号收不到
    other = register(client, "stranger")
    assert _all_notifications(client, other) == []


def test_notification_event_seq_is_audit_seq_and_ordered_ascending(client):
    keys, doc1, dg1 = _setup(client)
    # 第二份资料产生第二个 opened 事件
    d2 = seal(client, keys["owner"], BODY + "2", ["w1", "w2", "w3"])
    _open(client, keys, doc1, dg1)
    _open(client, keys, d2["document_id"], d2["digest"])

    notes = _all_notifications(client, keys["owner"])
    assert [n["event_type"] for n in notes] == ["opened", "opened"]
    assert notes[0]["event_seq"] < notes[1]["event_seq"]

    # event_seq 等于该资料 opened 审计记录的 audit_log.id
    audit1 = client.get(f"/documents/{doc1}/audit", headers=auth(keys["owner"])).json()["records"]
    opened_seq = next(r["id"] for r in audit1 if r["action"] == "opened")
    assert notes[0]["event_seq"] == opened_seq


# ---------------------------------------------------------------------------
# 到点自动开封（扫描器/写事务触发）同样投递，且只投递一次
# ---------------------------------------------------------------------------


def test_waiting_open_by_sweeper_notifies_once(client):
    keys, doc_id, dg = _setup(client)
    req = _create_request(client, keys["owner"], doc_id, deadline_h=2, earliest_s=0.5)
    _approve(client, keys["w1"], req, dg)
    _approve(client, keys["w2"], req, dg)
    # 两票先于最早开封时刻集齐 -> waiting，此时不投递 opened 通知
    assert _all_notifications(client, keys["owner"]) == []

    time.sleep(1.2)  # 等扫描器到点开封（间隔 0.05s）
    deadline = time.time() + 5
    while time.time() < deadline:
        notes = _all_notifications(client, keys["owner"])
        if notes:
            break
        time.sleep(0.1)
    notes = _all_notifications(client, keys["owner"])
    assert [n["event_type"] for n in notes] == ["opened"]
    # 三名见证人也各恰好一条（扫描器结算与后续查询不重复投递）
    for name in ("w1", "w2", "w3"):
        got = _all_notifications(client, keys[name])
        assert [n["event_type"] for n in got] == ["opened"], (name, got)

    # 再等一个扫描周期，确认不会重复投递
    time.sleep(0.5)
    assert len(_all_notifications(client, keys["w3"])) == 1


# ---------------------------------------------------------------------------
# 到期清除通知
# ---------------------------------------------------------------------------


def _schedule_purge(client, key, doc_id, purge_at):
    return client.put(f"/documents/{doc_id}/purge", json={"purge_at": purge_at}, headers=auth(key))


def test_purge_by_sweeper_notifies_participants_once(client):
    keys, doc_id, dg = _setup(client)
    assert _schedule_purge(client, keys["owner"], doc_id, _in_seconds(0.5)).status_code == 200

    time.sleep(1.2)
    deadline = time.time() + 5
    while time.time() < deadline:
        if _all_notifications(client, keys["owner"]):
            break
        time.sleep(0.1)

    for name in ("owner", "w1", "w2", "w3"):
        notes = _all_notifications(client, keys[name])
        assert [n["event_type"] for n in notes] == ["purged"], (name, notes)
        assert notes[0]["document_id"] == doc_id
        assert BODY not in str(notes[0])
    time.sleep(0.5)
    assert len(_all_notifications(client, keys["owner"])) == 1


def test_purge_settled_in_read_path_notifies(passive_client):
    # 扫描间隔拉到 1 小时：到点清除只能由资料查询的确定性结算触发，
    # 验证“非扫描器路径”结算清除时通知同样在同一事务写入、不重复。
    from app.db import connect, utc_str, write_tx, now_utc
    from app.config import settings

    keys = {n: register(passive_client, n) for n in ("owner", "w1", "w2", "w3")}
    d = seal(passive_client, keys["owner"], BODY, ["w1", "w2", "w3"])
    doc_id = d["document_id"]
    assert _schedule_purge(passive_client, keys["owner"], doc_id, _in_hours(1)).status_code == 200

    # 把清除时刻直接改到过去（模拟扫描器停机期间到点）
    conn = connect(settings.db_path)
    try:
        with write_tx(conn) as tx:
            tx.execute(
                "UPDATE documents SET purge_at=? WHERE id=?",
                (utc_str(now_utc() - dt.timedelta(seconds=1)), doc_id),
            )
    finally:
        conn.close()

    # 此时扫描器不会运行：尚未投递通知
    assert _all_notifications(passive_client, keys["owner"]) == []
    # 见证人查询触发读路径确定性清除并在同一事务投递通知
    got = passive_client.get(f"/documents/{doc_id}", headers=auth(keys["w1"]))
    assert got.status_code == 200 and got.json()["status"] == "purged"
    for name in ("owner", "w1", "w2", "w3"):
        assert _types(passive_client, keys[name]) == ["purged"]


def test_opened_then_purged_delivers_two_notifications_in_order(client):
    keys, doc_id, dg = _setup(client)
    _open(client, keys, doc_id, dg)
    assert _schedule_purge(client, keys["owner"], doc_id, _in_seconds(0.5)).status_code == 200
    time.sleep(1.2)
    deadline = time.time() + 5
    while time.time() < deadline:
        types = _types(client, keys["owner"])
        if types == ["opened", "purged"]:
            break
        time.sleep(0.1)
    assert _types(client, keys["w2"]) == ["opened", "purged"]


# ---------------------------------------------------------------------------
# 见证人替换通知：封存人 + 旧见证人 + 新见证人；其余两名见证人各收到替换事件
# ---------------------------------------------------------------------------


def test_witness_replacement_notifies_owner_old_and_new(client):
    keys, doc_id, _ = _setup(client)
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "w3", "new_witness": "new"},
        headers=auth(keys["owner"]),
    )
    assert r.status_code == 200, r.text

    owners_notes = _all_notifications(client, keys["owner"])
    assert [n["event_type"] for n in owners_notes] == ["witness_replaced"]

    # 旧见证人 w3 已被替换、失去资料权限，但仍能读到自己收到的通知
    r = client.get(f"/documents/{doc_id}", headers=auth(keys["w3"]))
    assert r.status_code == 403
    old_notes = _all_notifications(client, keys["w3"])
    assert [n["event_type"] for n in old_notes] == ["witness_replaced"]

    # 新见证人 new 收到通知，且通知不能借此访问正文（资料仍未开封）
    new_notes = _all_notifications(client, keys["new"])
    assert [n["event_type"] for n in new_notes] == ["witness_replaced"]
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["new"]))
    assert got.status_code == 200 and "body" not in got.json()

    # 其他两名见证人也各收到一条替换通知
    for name in ("w1", "w2"):
        assert _types(client, keys[name]) == ["witness_replaced"]

    # 通知里不含新旧见证人用户名之外的任何敏感内容，也无正文
    assert BODY not in str(new_notes)


def test_replacement_then_open_recipients_follow_current_roster(client):
    keys, doc_id, dg = _setup(client)
    client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "w3", "new_witness": "new"},
        headers=auth(keys["owner"]),
    )
    req = _create_request(client, keys["owner"], doc_id)
    _approve(client, keys["w1"], req, dg)
    _approve(client, keys["w2"], req, dg)

    # 开封按当前名单投递：owner/w1/w2/new 收到 opened；被替换的 w3 没有 opened
    assert _types(client, keys["new"]) == ["witness_replaced", "opened"]
    assert _types(client, keys["w3"]) == ["witness_replaced"]
    assert _types(client, keys["owner"]) == ["witness_replaced", "opened"]


# ---------------------------------------------------------------------------
# 交接确认通知：新旧封存人 + 三名见证人
# ---------------------------------------------------------------------------


def test_handover_confirm_notifies_old_and_new_owner_and_witnesses(client):
    keys, doc_id, _ = _setup(client)
    r = client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": "new", "confirm_deadline": _in_hours(1)},
        headers=auth(keys["owner"]),
    )
    assert r.status_code == 201, r.text
    inv = r.json()["invitation_id"]
    r = client.post(f"/handover-invitations/{inv}/confirm", headers=auth(keys["new"]))
    assert r.status_code == 200, r.text

    # 新、旧封存人与三名见证人各恰好一条 handover_confirmed
    for name in ("owner", "new", "w1", "w2", "w3"):
        assert _types(client, keys[name]) == ["handover_confirmed"], name

    # 旧封存人失去资料权限，但通知仍可读；通知不含正文
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).status_code == 403
    old_notes = _all_notifications(client, keys["owner"])
    assert BODY not in str(old_notes)

    # 无关账号无通知
    assert _all_notifications(client, register(client, "nobody")) == []


def test_handover_then_open_notifies_new_owner_not_old(client):
    keys, doc_id, dg = _setup(client)
    r = client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": "new", "confirm_deadline": _in_hours(1)},
        headers=auth(keys["owner"]),
    )
    inv = r.json()["invitation_id"]
    assert client.post(f"/handover-invitations/{inv}/confirm", headers=auth(keys["new"])).status_code == 200

    # 新封存人重新申请开封（见证人不变）
    req = _create_request(client, keys["new"], doc_id)
    _approve(client, keys["w1"], req, dg)
    _approve(client, keys["w2"], req, dg)

    assert _types(client, keys["new"]) == ["handover_confirmed", "opened"]
    assert _types(client, keys["w3"]) == ["handover_confirmed", "opened"]
    # 旧封存人只有交接通知，收不到开封通知
    assert _types(client, keys["owner"]) == ["handover_confirmed"]


# ---------------------------------------------------------------------------
# 失败/回滚的操作不留通知
# ---------------------------------------------------------------------------


def test_failed_operations_leave_no_notification(client):
    keys, doc_id, dg = _setup(client)
    req = _create_request(client, keys["owner"], doc_id)
    # 错误摘要的同意被拒，不开封 -> 无通知
    r = _approve(client, keys["w1"], req, "0" * 64)
    assert r.status_code == 409
    # 非见证人同意 403
    assert _approve(client, keys["new"], req, dg).status_code == 403
    # 撤销开封请求：不产生通知
    assert client.post(f"/open-requests/{req}/revoke", json={}, headers=auth(keys["owner"])).status_code == 200
    # 无效替换（新见证人未注册）400
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "w3", "new_witness": "ghost"},
        headers=auth(keys["owner"]),
    )
    assert r.status_code == 400
    # 无清除安排却取消 409
    assert client.delete(f"/documents/{doc_id}/purge", headers=auth(keys["owner"])).status_code == 409

    for name in ("owner", "w1", "w2", "w3", "new"):
        assert _all_notifications(client, keys[name]) == [], name


def test_rollback_settlement_leaves_no_notification(client):
    # 兑换/操作临界区内若先结算了“其他资料”的清除又整体回滚，通知也必须回滚。
    # 直接用底层连接模拟：开启写事务 -> 清除 -> 回滚，库中无通知。
    from app.db import connect, utc_str, now_utc, write_tx

    keys, doc_id, _ = _setup(client)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE documents SET purge_at=? WHERE id=?",
                (utc_str(now_utc() - dt.timedelta(seconds=1)), doc_id),
            )
            from app.main import purge_due_documents

            assert purge_due_documents(conn, utc_str(now_utc()), doc_id) == 1
            # 事务内已有通知
            assert conn.execute("SELECT COUNT(*) AS c FROM notifications").fetchone()["c"] == 4
        finally:
            conn.rollback()
    finally:
        conn.close()
    # 回滚后：资料未清除、通知不存在
    assert _all_notifications(client, keys["owner"]) == []


# ---------------------------------------------------------------------------
# 游标分页：默认页、page_size 边界、next_cursor、非法参数
# ---------------------------------------------------------------------------


def test_notification_cursor_pagination(client):
    keys = {f"p{n}": register(client, f"p{n}") for n in range(6)}
    owner = keys.pop("p0")
    witnesses = ["p1", "p2", "p3"]
    # 连续封存 + 开封 3 份资料，owner 与见证人各收到 3 条 opened
    for i in range(3):
        d = seal(client, owner, f"分页正文 {i}", witnesses)
        req = _create_request(client, owner, d["document_id"])
        _approve(client, keys["p1"], req, d["digest"])
        _approve(client, keys["p2"], req, d["digest"])

    # page_size=2：第一页 2 条、有下一游标；第二页 1 条、next_cursor=null
    r = _notifications(client, owner, cursor=0, page_size=2)
    assert r.status_code == 200
    page1 = r.json()
    assert len(page1["notifications"]) == 2
    assert page1["next_cursor"] == page1["notifications"][-1]["event_seq"]
    seqs = [n["event_seq"] for n in page1["notifications"]]
    assert seqs == sorted(seqs)

    r = _notifications(client, owner, cursor=page1["next_cursor"], page_size=2)
    page2 = r.json()
    assert len(page2["notifications"]) == 1
    assert page2["next_cursor"] is None
    assert page2["notifications"][0]["event_seq"] > page1["next_cursor"]

    # 游标之后无更新 -> 空页、next_cursor=null
    r = _notifications(client, owner, cursor=page2["notifications"][0]["event_seq"], page_size=2)
    assert r.json() == {"notifications": [], "next_cursor": None}


def test_notification_pagination_boundaries(client):
    keys, doc_id, dg = _setup(client)
    _open(client, keys, doc_id, dg)

    # 边界值合法：page_size=1 与 100
    assert _notifications(client, keys["owner"], page_size=1).status_code == 200
    assert _notifications(client, keys["owner"], page_size=100).status_code == 200
    # 默认参数
    assert _notifications(client, keys["owner"]).status_code == 200
    # 游标 0 合法
    assert _notifications(client, keys["owner"], cursor=0).status_code == 200


def test_notification_pagination_invalid_params(client):
    keys, doc_id, dg = _setup(client)
    _open(client, keys, doc_id, dg)

    # 负游标 422
    r = _notifications(client, keys["owner"], cursor=-1)
    assert r.status_code == 422
    # page_size 越界（0 / 101）422
    assert _notifications(client, keys["owner"], page_size=0).status_code == 422
    assert _notifications(client, keys["owner"], page_size=101).status_code == 422
    # 非整数 422
    assert client.get("/notifications?cursor=abc", headers=auth(keys["owner"])).status_code == 422
    # 参数校验失败不改变已读状态
    n = _all_notifications(client, keys["owner"])[0]
    assert n["is_read"] is False


def test_notifications_require_api_key(client):
    assert client.get("/notifications").status_code == 401
    assert client.get("/notifications", headers={"X-API-Key": "deadbeef"}).status_code == 401
    assert client.post("/notifications/1/read").status_code == 401


# ---------------------------------------------------------------------------
# 标记已读：幂等、返回已读状态；他人/不存在编号拒绝且不改状态
# ---------------------------------------------------------------------------


def test_mark_read_idempotent_and_scoped_to_owner(client):
    keys, doc_id, dg = _setup(client)
    _open(client, keys, doc_id, dg)
    w1_note = _all_notifications(client, keys["w1"])[0]
    nid = w1_note["notification_id"]

    # 首次标记
    r = client.post(f"/notifications/{nid}/read", headers=auth(keys["w1"]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["notification_id"] == nid and data["is_read"] is True
    first_read_at = data["read_at"]
    assert first_read_at

    # 列表呈现已读
    got = _all_notifications(client, keys["w1"])[0]
    assert got["is_read"] is True

    # 重试幂等：read_at 保留首次时刻，不产生额外记录
    time.sleep(0.01)
    r = client.post(f"/notifications/{nid}/read", headers=auth(keys["w1"]))
    assert r.status_code == 200
    assert r.json()["read_at"] == first_read_at
    assert len(_all_notifications(client, keys["w1"])) == 1

    # 他人不能标记（封存人标记 w1 的通知 -> 403），且不改变其状态
    r = client.post(f"/notifications/{nid}/read", headers=auth(keys["owner"]))
    assert r.status_code == 403
    assert _all_notifications(client, keys["w1"])[0]["is_read"] is True
    # 无关账号同样 403
    stranger = register(client, "stranger2")
    assert client.post(f"/notifications/{nid}/read", headers=auth(stranger)).status_code == 403

    # 不存在的编号统一 403
    assert client.post("/notifications/999999/read", headers=auth(keys["w1"])).status_code == 403


def test_lost_permission_keeps_notification_access(client):
    keys, doc_id, dg = _setup(client)
    # 先开封让 w3 收到 opened 通知，再（资料已开封不能替换）——改用替换场景：
    d2 = seal(client, keys["owner"], BODY + "-2", ["w1", "w2", "w3"])
    client.post(
        f"/documents/{d2['document_id']}/witness-replacements",
        json={"old_witness": "w3", "new_witness": "new"},
        headers=auth(keys["owner"]),
    )
    # w3 失去资料权限，但通知可列、可标记已读
    assert client.get(f"/documents/{d2['document_id']}", headers=auth(keys["w3"])).status_code == 403
    notes = _all_notifications(client, keys["w3"])
    assert [n["event_type"] for n in notes] == ["witness_replaced"]
    nid = notes[0]["notification_id"]
    r = client.post(f"/notifications/{nid}/read", headers=auth(keys["w3"]))
    assert r.status_code == 200 and r.json()["is_read"] is True


# ---------------------------------------------------------------------------
# 真实 uvicorn + 多线程：多个请求并发结算同一次到点清除，通知不重复投递
# ---------------------------------------------------------------------------


def _start_server(tmp_path, monkeypatch, db_name, sweep_seconds):
    import socket
    import threading

    import httpx
    import uvicorn

    from app.main import app

    monkeypatch.setenv("SEAL_DB_PATH", str(tmp_path / db_name))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", str(sweep_seconds))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    end = time.time() + 10
    while time.time() < end:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        raise AssertionError("server did not start")
    return server, thread, base


def test_concurrent_settlement_delivers_purge_notification_once(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    server, thread, base = _start_server(tmp_path, monkeypatch, "notify-race.db", 3600)
    try:
        with httpx.Client(timeout=10) as c:
            def reg(n):
                return c.post(base + "/register", json={"username": n}).json()["api_key"]

            owner = reg("nr-owner")
            wkeys = [reg(f"nr-w{i}") for i in range(1, 4)]
            body = "NOTIFY-RACE-SECRET"
            doc_id = c.post(
                base + "/documents",
                json={"body": body, "witnesses": ["nr-w1", "nr-w2", "nr-w3"]},
                headers={"X-API-Key": owner},
            ).json()["document_id"]
            purge_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
            assert c.put(
                f"{base}/documents/{doc_id}/purge",
                json={"purge_at": purge_at}, headers={"X-API-Key": owner},
            ).status_code == 200

        time.sleep(1.3)  # 清除时刻已过，扫描器不运行（3600s）

        # 4 个并发请求同时进入写临界区结算同一资料的到点清除
        barrier = threading.Barrier(4)

        def settle(i):
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                # 两个并发资料查询触发读路径结算，两个并发“改期清除”写路径也结算
                if i % 2 == 0:
                    return c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": wkeys[0]}).status_code
                return c.put(
                    f"{base}/documents/{doc_id}/purge",
                    json={"purge_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()},
                    headers={"X-API-Key": owner},
                ).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(pool.map(settle, range(4)))
        assert 200 in codes  # 至少一个读请求成功看到 purged

        with httpx.Client(timeout=10) as c:
            got = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": owner})
            assert got.json()["status"] == "purged"

            def types_for(key):
                notes, cur = [], 0
                while True:
                    data = c.get(base + "/notifications",
                                 params={"cursor": cur, "page_size": 100},
                                 headers={"X-API-Key": key}).json()
                    notes.extend(data["notifications"])
                    if data["next_cursor"] is None:
                        return [n["event_type"] for n in notes]
                    cur = data["next_cursor"]

            # 每名参与方恰好一条 purged 通知，绝不因并发结算重复投递
            assert types_for(owner) == ["purged"]
            for k in wkeys:
                assert types_for(k) == ["purged"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
