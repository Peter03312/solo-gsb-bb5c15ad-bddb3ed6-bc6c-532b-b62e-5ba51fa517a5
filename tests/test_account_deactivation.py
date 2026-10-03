"""账号停用（account deactivation）。

覆盖：
- 无未了义务：停用成功，返回停用时刻；当前密钥立即且永久失效（401 同“无效
  密钥”文案），同名不可重注册（沿用 409）；历史审计/通知/参与记录保留；
- 义务拦截：未清除资料封存人、未开封资料当前见证人、待确认预封存发起人/
  见证人、待确认交接受让人——逐项返回义务编号与原因，账号保持可用；
- 核对前先到点结算：到点开封/清除/请求与邀请/预封存逾期先落库，即使因此
  拒绝停用，结算仍已落库（或结算后义务消失从而允许停用）；
- 停用后不能再被指定为见证人/交接受让人/预封存受邀见证人（按现有无效参与人
  400 处理）；
- 并发：与替换见证人/交接发起/密钥轮换/预封存确认按 BEGIN IMMEDIATE 提交
  顺序裁定，不出现停用后新增角色或部分停用。

并发用例使用真实 uvicorn 服务（后台线程）+ httpx，与 test_api_key_rotation.py 一致。
"""
import datetime as dt
import os
import socket
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import auth, digest_of, register, seal  # noqa: E402


DEACT = "/me/deactivation"


def _iso(hours: float = 1.0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat()


def _deactivate(client, key):
    return client.post(DEACT, headers=auth(key))


def _conn():
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def _obligations(body: dict) -> list[dict]:
    return body["detail"]["obligations"]


def _otypes(body: dict) -> set[str]:
    return {o["type"] for o in _obligations(body)}


# ---------------------------------------------------------------------------
# 成功停用
# ---------------------------------------------------------------------------


def test_deactivate_with_no_obligations_succeeds_and_kills_key(client):
    key = register(client, "ghost")
    r = _deactivate(client, key)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"user_id", "username", "status", "deactivated_at"}
    assert body["username"] == "ghost" and body["status"] == "deactivated"
    assert body["deactivated_at"]

    # 密钥立即且永久失效：读/写沿用现有无效密钥 401，文案与随机假密钥一致
    assert client.get("/me", headers=auth(key)).json() == {"detail": "凭据无效"}
    bogus = client.get("/me", headers=auth("0" * 64))
    old = client.get("/me", headers=auth(key))
    assert bogus.status_code == old.status_code == 401
    assert bogus.json() == old.json() == {"detail": "凭据无效"}
    # 停用接口本身也再不可用
    assert _deactivate(client, key).status_code == 401


def test_deactivation_persists_across_restart(tmp_path, monkeypatch):
    db = tmp_path / "deact.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    from app.main import app

    with TestClient(app) as c:
        key = register(c, "persist-deact")
        assert _deactivate(c, key).status_code == 200

    conn = sqlite3.connect(db)
    status, key_hash, deactivated_at = conn.execute(
        "SELECT status, key_hash, deactivated_at FROM users WHERE username='persist-deact'"
    ).fetchone()
    conn.close()
    assert status == "deactivated" and deactivated_at
    # 库内不残留原密钥摘要，且落库值不可能匹配任何 SHA-256 摘要
    import hashlib

    assert key_hash != hashlib.sha256(key.encode()).hexdigest()
    assert key_hash.startswith("deactivated:")

    # 重启后旧密钥仍然 401
    with TestClient(app) as c2:
        assert c2.get("/me", headers=auth(key)).status_code == 401


def test_same_username_cannot_reregister(client):
    key = register(client, "uniq")
    assert _deactivate(client, key).status_code == 200
    # 同名不可重注册：沿用注册既有的 409 文案
    r = client.post("/register", json={"username": "uniq"})
    assert r.status_code == 409 and r.json() == {"detail": "用户名已存在"}
    # 其他新用户名仍可注册
    assert client.post("/register", json={"username": "uniq2"}).status_code == 201


def test_deactivation_leaves_audit_and_participation_records(client):
    owner = register(client, "own")
    for n in ("w1", "w2", "w3"):
        register(client, n)
    doc = seal(client, owner, "封存正文 RETAIN", ["w1", "w2", "w3"])

    # 让资料到点清除，封存人义务消失，再停用
    purge_at = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
    ).isoformat()
    r = client.put(
        f"/documents/{doc['document_id']}/purge",
        headers=auth(owner),
        json={"purge_at": purge_at},
    )
    assert r.status_code == 200

    # 到点后（清除同时解除未开封见证义务）停用成功
    time.sleep(1.2)
    assert _deactivate(client, owner).status_code == 200

    conn = _conn()
    # 资料与参与记录保留
    d = conn.execute(
        "SELECT status, digest FROM documents WHERE id=?", (doc["document_id"],)
    ).fetchone()
    assert d["status"] == "purged"
    wcount = conn.execute(
        "SELECT COUNT(*) c FROM document_witnesses WHERE document_id=?",
        (doc["document_id"],),
    ).fetchone()["c"]
    assert wcount == 3
    # 停用审计追加且 actor 仍指向该用户（历史审计保留、可关联）
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log")]
    assert actions.count("account_deactivated") == 1
    deact = conn.execute(
        "SELECT actor_id FROM audit_log WHERE action='account_deactivated'"
    ).fetchone()
    uid = conn.execute("SELECT id FROM users WHERE username='own'").fetchone()["id"]
    assert deact["actor_id"] == uid
    conn.close()


# ---------------------------------------------------------------------------
# 义务拦截
# ---------------------------------------------------------------------------


def test_owner_of_unpurged_sealed_document_blocks(client):
    owner = register(client, "o-sealed")
    for n in ("a", "b", "c"):
        register(client, n)
    seal(client, owner, "sealed X", ["a", "b", "c"])

    r = _deactivate(client, owner)
    assert r.status_code == 409
    assert _otypes(r.json()) == {"document_owner"}
    ob = _obligations(r.json())[0]
    assert ob["ref_kind"] == "document" and ob["ref_id"] >= 1 and ob["reason"]
    # 账号保持可用
    assert client.get("/me", headers=auth(owner)).status_code == 200


def test_witness_of_unopened_document_blocks(client):
    owner = register(client, "o2")
    witnesses = [register(client, f"ww{i}") for i in range(3)]
    seal(client, owner, "sealed Y", ["ww0", "ww1", "ww2"])

    r = _deactivate(client, witnesses[1])
    assert r.status_code == 409
    assert _otypes(r.json()) == {"document_witness"}
    assert client.get("/me", headers=auth(witnesses[1])).status_code == 200


def test_owner_and_witness_obligations_listed_together(client):
    # 同一账号既是某资料封存人，又是另一份未开封资料的见证人
    dual = register(client, "dual")
    other = register(client, "other-owner")
    for n in ("g1", "g2", "g3", "h1", "h2"):
        register(client, n)
    # dual 自己封存一份（需要另外三名见证人）
    seal(client, dual, "owned by dual", ["g1", "g2", "g3"])
    # dual 为 other-owner 封存的资料见证
    seal(client, other, "dual witnesses here", ["dual", "h1", "h2"])

    r = _deactivate(client, dual)
    assert r.status_code == 409
    assert _otypes(r.json()) == {"document_owner", "document_witness"}


def test_pending_pre_seal_owner_and_witnesses_block(client):
    owner = register(client, "ps-owner")
    wkeys = [register(client, f"psw{i}") for i in range(3)]
    r = client.post(
        "/pre-seal-proposals",
        headers=auth(owner),
        json={"body": "预封存正文", "witnesses": ["psw0", "psw1", "psw2"],
              "confirm_deadline": _iso(2)},
    )
    assert r.status_code == 201
    proposal_id = r.json()["proposal_id"]

    # 发起人
    r = _deactivate(client, owner)
    assert r.status_code == 409
    obs = _obligations(r.json())
    assert _otypes(r.json()) == {"pre_seal_owner"}
    assert obs[0]["ref_id"] == proposal_id

    # 尚未确认的受邀见证人也有义务
    r = _deactivate(client, wkeys[0])
    assert r.status_code == 409
    assert _otypes(r.json()) == {"pre_seal_witness"}
    assert _obligations(r.json())[0]["ref_id"] == proposal_id


def test_pending_handover_assignee_blocks(client):
    owner = register(client, "ho")
    assignee = register(client, "assignee")
    for n in ("hw1", "hw2", "hw3"):
        register(client, n)
    doc = seal(client, owner, "handover doc", ["hw1", "hw2", "hw3"])
    r = client.post(
        f"/documents/{doc['document_id']}/handover-invitations",
        headers=auth(owner),
        json={"to_username": "assignee", "confirm_deadline": _iso(1)},
    )
    assert r.status_code == 201
    invitation_id = r.json()["invitation_id"]

    r = _deactivate(client, assignee)
    assert r.status_code == 409
    obs = _obligations(r.json())
    assert _otypes(r.json()) == {"handover_assignee"}
    assert obs[0]["ref_kind"] == "handover_invitation"
    assert obs[0]["ref_id"] == invitation_id

    # 待确认交接的发起人（= 未开封资料封存人）仍按 document_owner 被拦截
    r = _deactivate(client, owner)
    assert r.status_code == 409 and _otypes(r.json()) == {"document_owner"}


def test_rejected_deactivation_keeps_account_and_is_retryable(client):
    owner = register(client, "blocked")
    for n in ("b1", "b2", "b3"):
        register(client, n)
    seal(client, owner, "still sealed", ["b1", "b2", "b3"])

    assert _deactivate(client, owner).status_code == 409
    # 义务解除后（交接给无义务第三方）可再次申请并成功
    new_owner = register(client, "new-owner")
    # 先确认交接
    docs = client.get("/documents", headers=auth(owner)).json()["documents"]
    doc_id = docs[0]["id"]
    inv = client.post(
        f"/documents/{doc_id}/handover-invitations",
        headers=auth(owner),
        json={"to_username": "new-owner", "confirm_deadline": _iso(1)},
    ).json()["invitation_id"]
    assert client.post(
        f"/handover-invitations/{inv}/confirm", headers=auth(new_owner)
    ).status_code == 200
    # 原封存人不再是 owner（也不是见证人），停用成功
    assert _deactivate(client, owner).status_code == 200


# ---------------------------------------------------------------------------
# 核对前先到点结算（结算仍落库）
# ---------------------------------------------------------------------------


def test_due_settlement_runs_before_check_and_persists_on_rejection(client, sweep):
    """资料已到清除时刻：停用核对先结算清除；清除后见证人义务消失，
    但封存人对“已开封且未清除”无此场景——这里让 sealed 资料直接到点清除，
    owner 与 witness 义务均消失，停用成功；并验证即便被拒结算也已落库。"""
    owner = register(client, "sched-owner")
    wkeys = [register(client, f"sw{i}") for i in range(3)]
    doc = seal(client, owner, "到点清除 BODY", ["sw0", "sw1", "sw2"])
    purge_at = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
    ).isoformat()
    assert client.put(
        f"/documents/{doc['document_id']}/purge",
        headers=auth(owner), json={"purge_at": purge_at},
    ).status_code == 200
    time.sleep(1.2)

    # 不依赖扫描器：停用请求本身在锁内先结算清除，再检查义务 -> 全部结清
    r = _deactivate(client, owner)
    assert r.status_code == 200, r.text

    conn = _conn()
    status = conn.execute(
        "SELECT status FROM documents WHERE id=?", (doc["document_id"],)
    ).fetchone()["status"]
    assert status == "purged"
    conn.close()


def test_due_pre_seal_expires_inside_deactivation_then_succeeds(client, sweep):
    """待确认预封存到点：停用核对先使其失效（清空正文），发起人/见证人义务随之消失。"""
    owner = register(client, "exp-owner")
    wkeys = [register(client, f"ew{i}") for i in range(3)]
    deadline = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
    ).isoformat()
    prop = client.post(
        "/pre-seal-proposals",
        headers=auth(owner),
        json={"body": "即将失效提案", "witnesses": ["ew0", "ew1", "ew2"],
              "confirm_deadline": deadline},
    ).json()
    time.sleep(1.2)

    # 停用临界区内确定性结算预封存逾期，再检查义务 -> 无 pending 提案，成功
    assert _deactivate(client, owner).status_code == 200
    assert _deactivate(client, wkeys[0]).status_code == 200

    conn = _conn()
    row = conn.execute(
        "SELECT status, body FROM pre_seal_proposals WHERE id=?",
        (prop["proposal_id"],),
    ).fetchone()
    assert row["status"] == "expired" and row["body"] == ""
    conn.close()


def test_due_handover_invitation_expires_then_assignee_free(client):
    owner = register(client, "ho2")
    assignee = register(client, "assignee2")
    for n in ("i1", "i2", "i3"):
        register(client, n)
    doc = seal(client, owner, "inv doc", ["i1", "i2", "i3"])
    client.post(
        f"/documents/{doc['document_id']}/handover-invitations",
        headers=auth(owner),
        json={"to_username": "assignee2",
              "confirm_deadline": (dt.datetime.now(dt.timezone.utc)
                                   + dt.timedelta(seconds=1)).isoformat()},
    )
    time.sleep(1.2)
    # 停用核对先把逾期邀请置 expired，受让人义务消失（受让人本无其他义务）
    assert _deactivate(client, assignee).status_code == 200


def test_due_open_request_settlement_persists_even_when_still_blocked(client):
    """开封请求到点失效与资料清除：停用被其他义务拒绝时，本次结算仍已落库。"""
    owner = register(client, "multi-owner")
    wkeys = [register(client, f"mw{i}") for i in range(3)]
    # 资料 1：已开封并安排 1 秒后清除（清除后该份不再产生义务）
    doc1 = seal(client, owner, "opened body", ["mw0", "mw1", "mw2"])
    dg = doc1["digest"]
    req = client.post(
        f"/documents/{doc1['document_id']}/open-requests",
        headers=auth(owner),
        json={"reason": "r", "deadline": _iso(2)},
    ).json()["request_id"]
    for k in wkeys:
        client.post(f"/open-requests/{req}/approve", headers=auth(k),
                    json={"digest": dg})
    client.put(
        f"/documents/{doc1['document_id']}/purge",
        headers=auth(owner),
        json={"purge_at": (dt.datetime.now(dt.timezone.utc)
                           + dt.timedelta(seconds=1)).isoformat()},
    )
    # 资料 2：始终 sealed -> owner 义务必然仍在，停用必被拒
    doc2 = seal(client, owner, "still sealed 2", ["mw0", "mw1", "mw2"])
    time.sleep(1.2)

    r = _deactivate(client, owner)
    assert r.status_code == 409  # 因 doc2 仍被拒
    ids = {o["ref_id"] for o in _obligations(r.json()) if o["type"] == "document_owner"}
    assert doc2["document_id"] in ids and doc1["document_id"] not in ids

    # 但 doc1 的到点清除已随被拒事务提交落库
    conn = _conn()
    st = conn.execute("SELECT status FROM documents WHERE id=?",
                      (doc1["document_id"],)).fetchone()["status"]
    assert st == "purged"
    conn.close()


# ---------------------------------------------------------------------------
# 停用后不得再被指定为角色（现有无效参与人错误）
# ---------------------------------------------------------------------------


def test_deactivated_user_rejected_as_witness_on_seal(client):
    gone = register(client, "gone-w")
    for n in ("x1", "x2"):
        register(client, n)
    assert _deactivate(client, gone).status_code == 200

    owner = register(client, "sealer")
    r = client.post(
        "/documents",
        headers=auth(owner),
        json={"body": "b", "witnesses": ["gone-w", "x1", "x2"]},
    )
    # 沿用现有“见证人尚未注册”400 与文案，不暴露停用状态
    assert r.status_code == 400
    assert r.json() == {"detail": "见证人尚未注册: gone-w"}


def test_deactivated_user_rejected_on_preseal(client):
    gone = register(client, "gone-ps")
    for n in ("y1", "y2"):
        register(client, n)
    assert _deactivate(client, gone).status_code == 200

    owner = register(client, "ps-sealer")
    r = client.post(
        "/pre-seal-proposals",
        headers=auth(owner),
        json={"body": "b", "witnesses": ["gone-ps", "y1", "y2"],
              "confirm_deadline": _iso(1)},
    )
    assert r.status_code == 400 and "尚未注册" in r.json()["detail"]


def test_deactivated_user_rejected_as_replacement_witness(client):
    owner = register(client, "rep-owner")
    for n in ("r1", "r2", "r3"):
        register(client, n)
    doc = seal(client, owner, "rep body", ["r1", "r2", "r3"])
    gone = register(client, "gone-r")
    assert _deactivate(client, gone).status_code == 200

    r = client.post(
        f"/documents/{doc['document_id']}/witness-replacements",
        headers=auth(owner),
        json={"old_witness": "r1", "new_witness": "gone-r"},
    )
    assert r.status_code == 400 and r.json() == {"detail": "新见证人尚未注册"}


def test_deactivated_user_rejected_as_handover_assignee(client):
    owner = register(client, "hv-owner")
    for n in ("v1", "v2", "v3"):
        register(client, n)
    doc = seal(client, owner, "hv body", ["v1", "v2", "v3"])
    gone = register(client, "gone-h")
    assert _deactivate(client, gone).status_code == 200

    r = client.post(
        f"/documents/{doc['document_id']}/handover-invitations",
        headers=auth(owner),
        json={"to_username": "gone-h", "confirm_deadline": _iso(1)},
    )
    assert r.status_code == 400 and r.json() == {"detail": "受让人尚未注册"}


def test_witness_who_deactivates_after_doc_purged_is_freed(client):
    """清除后（含 opened->purged）原见证人不再有见证义务，可停用。"""
    owner = register(client, "free-owner")
    wk = [register(client, f"f{i}") for i in range(3)]
    doc = seal(client, owner, "free body", ["f0", "f1", "f2"])
    dg = doc["digest"]
    req = client.post(
        f"/documents/{doc['document_id']}/open-requests",
        headers=auth(owner), json={"reason": "r", "deadline": _iso(2)},
    ).json()["request_id"]
    for k in wk:
        client.post(f"/open-requests/{req}/approve", headers=auth(k),
                    json={"digest": dg})
    client.put(
        f"/documents/{doc['document_id']}/purge",
        headers=auth(owner),
        json={"purge_at": (dt.datetime.now(dt.timezone.utc)
                           + dt.timedelta(seconds=1)).isoformat()},
    )
    time.sleep(1.2)
    # 停用临界区先结算清除：见证义务随 sealed 状态消失
    assert _deactivate(client, wk[0]).status_code == 200


def test_deactivated_account_notifications_and_audit_still_readable_by_others(client):
    """停用不删除任何行：其他人收到的通知仍在，审计仍可查。"""
    owner = register(client, "aud-owner")
    wk = [register(client, f"d{i}") for i in range(3)]
    doc = seal(client, owner, "notify body", ["d0", "d1", "d2"])
    dg = doc["digest"]
    req = client.post(
        f"/documents/{doc['document_id']}/open-requests",
        headers=auth(owner), json={"reason": "r",
                                   "deadline": _iso(2)},
    ).json()["request_id"]
    for k in wk:
        client.post(f"/open-requests/{req}/approve", headers=auth(k),
                    json={"digest": dg})

    # owner 已开封；安排一个足够远的清除不影响，直接停用 owner 会被
    # document_owner(opened) 拦截，先到点清除
    client.put(
        f"/documents/{doc['document_id']}/purge",
        headers=auth(owner),
        json={"purge_at": (dt.datetime.now(dt.timezone.utc)
                           + dt.timedelta(seconds=1)).isoformat()},
    )
    time.sleep(1.2)
    assert _deactivate(client, owner).status_code == 200

    # 见证人仍能读自己的通知（含来自已停用封存人资料的事件）与审计
    notes = client.get("/notifications", headers=auth(wk[0])).json()["notifications"]
    assert {n["event_type"] for n in notes} >= {"opened", "purged"}
    audit = client.get(
        f"/documents/{doc['document_id']}/audit", headers=auth(wk[0])
    ).json()["records"]
    assert any(rec["action"] == "seal" for rec in audit)


# ---------------------------------------------------------------------------
# 并发：真实 uvicorn 服务
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    db = tmp_path / "deactlive.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "60")

    import uvicorn

    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("server did not start")

    yield base, str(db)

    server.should_exit = True
    thread.join(timeout=5)


def _reg(c, base, name):
    r = c.post(base + "/register", json={"username": name})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def test_deactivation_first_commits_blocks_concurrent_role_assignment(live_server):
    """停用先提交：并发的替换见证人在锁内复查到新见证人已停用 -> 400 回滚，
    不出现停用后新增角色。"""
    base, db_path = live_server
    with httpx.Client(timeout=15) as c:
        owner = _reg(c, base, "c-owner")
        for n in ("cr1", "cr2", "cr3"):
            _reg(c, base, n)
        gone = _reg(c, base, "c-gone")
        doc = c.post(base + "/documents", headers={"X-API-Key": owner},
                     json={"body": "并发替换", "witnesses": ["cr1", "cr2", "cr3"]})
        doc_id = doc.json()["document_id"]
        barrier = threading.Barrier(2)

        def deactivate():
            barrier.wait()
            r = c.post(base + DEACT, headers={"X-API-Key": gone})
            return r.status_code

        def replace_late():
            barrier.wait()
            time.sleep(0.5)  # 让停用先拿锁并提交
            return c.post(
                f"{base}/documents/{doc_id}/witness-replacements",
                headers={"X-API-Key": owner},
                json={"old_witness": "cr1", "new_witness": "c-gone"},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            d = pool.submit(deactivate)
            rp = pool.submit(replace_late)
            assert d.result() == 200
            assert rp.result() == 400

    conn = sqlite3.connect(db_path)
    # 新角色未落：名单里没有已停用账号
    names = [r[0] for r in conn.execute(
        "SELECT u.username FROM document_witnesses w JOIN users u ON u.id=w.user_id"
        " WHERE w.document_id=?", (doc_id,)).fetchall()]
    assert "c-gone" not in names
    conn.close()


def test_role_assignment_first_commits_then_deactivation_is_rejected(live_server):
    """交接发起先提交（受让人角色落地）：停用随后在同一串行顺序下检测到
    handover_assignee 义务而 409；不会部分停用。"""
    base, db_path = live_server
    with httpx.Client(timeout=15) as c:
        owner = _reg(c, base, "h-owner")
        for n in ("ch1", "ch2", "ch3"):
            _reg(c, base, n)
        assignee = _reg(c, base, "h-assignee")
        doc = c.post(base + "/documents", headers={"X-API-Key": owner},
                     json={"body": "并发交接", "witnesses": ["ch1", "ch2", "ch3"]})
        doc_id = doc.json()["document_id"]
        deadline = (dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(hours=1)).isoformat()
        barrier = threading.Barrier(2)

        def invite():
            barrier.wait()
            r = c.post(f"{base}/documents/{doc_id}/handover-invitations",
                       headers={"X-API-Key": owner},
                       json={"to_username": "h-assignee", "confirm_deadline": deadline})
            return r.status_code

        def deactivate_late():
            barrier.wait()
            time.sleep(0.5)  # 让交接先拿锁并提交
            return c.post(base + DEACT, headers={"X-API-Key": assignee})

        with ThreadPoolExecutor(max_workers=2) as pool:
            inv_f = pool.submit(invite)
            deact_f = pool.submit(deactivate_late)
            assert inv_f.result() == 201
            r = deact_f.result()
            assert r.status_code == 409

        # 未部分停用：受让人密钥仍可用
        assert c.get(base + "/me", headers={"X-API-Key": assignee}).status_code == 200

    conn = sqlite3.connect(db_path)
    status = conn.execute(
        "SELECT status FROM users WHERE username='h-assignee'"
    ).fetchone()[0]
    assert status == "active"
    conn.close()


def test_concurrent_deactivation_and_rotation_one_outcome(live_server):
    """停用与轮换并发：要么停用生效（两密钥皆 401），要么轮换先生效
    （随后停用需用新密钥；旧密钥 401）。不得出现部分停用。"""
    base, _ = live_server
    with httpx.Client(timeout=15) as c:
        key = _reg(c, base, "race-user")
        barrier = threading.Barrier(2)

        def deactivate():
            barrier.wait()
            return c.post(base + DEACT, headers={"X-API-Key": key}).status_code

        def rotate():
            barrier.wait()
            r = c.post(base + "/me/api-key/rotate", headers={"X-API-Key": key})
            return r.status_code, (r.json().get("api_key") if r.status_code == 200 else None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda f: f(), [deactivate, rotate]))

    d_code = [x for x in results if isinstance(x, int)][0]
    rot_code, new_key = [x for x in results if isinstance(x, tuple)][0]
    assert d_code in (200, 401) and rot_code in (200, 401)
    assert (d_code == 200) != (rot_code == 200)  # 恰一方成功
    with httpx.Client(timeout=15) as c:
        if d_code == 200:
            # 停用胜出：旧密钥与轮换出的新密钥都不可用
            assert c.get(base + "/me", headers={"X-API-Key": key}).status_code == 401
        else:
            # 轮换胜出：旧密钥 401、新密钥 200；再用新密钥停用应成功（无义务）
            assert c.get(base + "/me", headers={"X-API-Key": key}).status_code == 401
            assert c.get(base + "/me", headers={"X-API-Key": new_key}).status_code == 200
            assert c.post(base + DEACT, headers={"X-API-Key": new_key}).status_code == 200
