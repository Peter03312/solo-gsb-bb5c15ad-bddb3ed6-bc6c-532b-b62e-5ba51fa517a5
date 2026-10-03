"""账号停用：先结算再核对义务；有未了义务拒绝（义务编号+原因，账号保持可用，
到点结算仍落库）；无义务原子停用（密钥立即永久失效、同名不可重注册、历史
审计/通知/参与记录保留）；停用账号不得再被指定为见证人/受让人/预封存见证人；
义务检查、停用与封存/替换/交接/预封存/密钥轮换并发时按写事务提交顺序裁定。
"""
import datetime as dt
import hashlib
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


def _iso(hours: float = 24.0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _users(client, *names):
    """注册一组账号，返回 {用户名: 密钥}；同一名字只注册一次。"""
    return {n: register(client, n) for n in names}


def _seal(client, owner, witnesses, body="封存正文"):
    # 调用方负责先注册（_users）；这里直接封存
    return seal(client, owner, body, witnesses)


def _open_with_two_approvals(client, owner, doc_id, digest, wkeys):
    req_id = client.post(
        f"/documents/{doc_id}/open-requests",
        headers=auth(owner),
        json={"reason": "需要开封", "deadline": _iso(24)},
    ).json()["request_id"]
    for k in wkeys:
        client.post(f"/open-requests/{req_id}/approve", headers=auth(k), json={"digest": digest})
    return req_id


# ---------------------------------------------------------------------------
# 基础：无义务可停用
# ---------------------------------------------------------------------------


def test_deactivate_clean_account(client):
    key = register(client, "leaver")
    r = client.post("/me/deactivate", headers=auth(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"user_id", "username", "status", "deactivated_at"}
    assert body["username"] == "leaver"
    assert body["status"] == "deactivated"
    assert body["deactivated_at"]
    # 密钥立即失效：与“无效密钥”完全相同的 401，不区分账号存在性
    r1 = client.get("/me", headers=auth(key))
    r2 = client.get("/me", headers=auth("0" * 64))
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json() == {"detail": "凭据无效"}
    # 停用接口本身也不能再用
    assert client.post("/me/deactivate", headers=auth(key)).status_code == 401
    # 无凭据 401 文案保持
    assert client.post("/me/deactivate").json() == {"detail": "缺少 API Key"}


def test_deactivated_name_cannot_reregister(client):
    key = register(client, "gone")
    assert client.post("/me/deactivate", headers=auth(key)).status_code == 200
    # 同名不可重注册：沿用现有重名 409
    r = client.post("/register", json={"username": "gone"})
    assert r.status_code == 409 and r.json() == {"detail": "用户名已存在"}


def test_deactivation_persists_across_restart(tmp_path, monkeypatch):
    db = tmp_path / "deact.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    from app.main import app

    with TestClient(app) as c:
        key = register(c, "persist-leaver")
        resp = c.post("/me/deactivate", headers=auth(key)).json()

    conn = _db(str(db))
    row = conn.execute(
        "SELECT status, key_hash, deactivated_at FROM users WHERE username='persist-leaver'"
    ).fetchone()
    conn.close()
    assert row["status"] == "deactivated"
    assert row["deactivated_at"] == resp["deactivated_at"]
    assert row["key_hash"] != _hash(key)
    # 停用后旧密钥不再有效（重启后仍失效）
    with TestClient(app) as c2:
        assert c2.get("/me", headers=auth(key)).status_code == 401


def test_deactivation_audit_and_records_retained(client):
    users = _users(client, "aud-owner", "aud-w", "aud-x", "aud-y")
    owner, w, wx = users["aud-owner"], users["aud-w"], users["aud-x"]
    doc = _seal(client, owner, ["aud-w", "aud-x", "aud-y"])
    # 开封后清除，解除义务
    purge_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
    client.put(f"/documents/{doc['document_id']}/purge", headers=auth(owner), json={"purge_at": purge_at})
    _open_with_two_approvals(client, owner, doc["document_id"], doc["digest"], [w, wx])
    time.sleep(1.2)
    assert client.post("/me/deactivate", headers=auth(owner)).status_code == 200

    db_path = os.environ["SEAL_DB_PATH"]
    conn = _db(db_path)
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    conn.close()
    assert "account_deactivated" in actions
    # 资料、参与方、通知行均保留
    conn = _db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM document_witnesses").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] >= 1
    deact = conn.execute(
        "SELECT detail FROM audit_log WHERE action='account_deactivated'"
    ).fetchone()["detail"]
    conn.close()
    assert owner not in deact  # 审计细节不含密钥材料


# ---------------------------------------------------------------------------
# 未了义务：拒绝停用、返回义务编号与原因、账号保持可用
# ---------------------------------------------------------------------------


def test_obligation_document_owner_unpurged(client):
    owner = register(client, "own1")
    _users(client, "ow1", "ow2", "ow3")
    _seal(client, owner, ["ow1", "ow2", "ow3"])
    r = client.post("/me/deactivate", headers=auth(owner))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message"]
    ob = detail["obligations"]
    assert any(o["type"] == "document_owner" and o["document_id"] == 1 for o in ob)
    assert all("reason" in o and o["reason"] for o in ob)
    # 账号保持可用
    assert client.get("/me", headers=auth(owner)).status_code == 200


def test_purged_owner_obligation_lifted(client):
    owner = register(client, "own2")
    _users(client, "op1", "op2", "op3")
    doc = _seal(client, owner, ["op1", "op2", "op3"])
    purge_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
    client.put(f"/documents/{doc['document_id']}/purge", headers=auth(owner), json={"purge_at": purge_at})
    time.sleep(1.2)
    # 已清除：不再是“未清除资料的封存人”，可停用
    assert client.post("/me/deactivate", headers=auth(owner)).status_code == 200


def test_obligation_witness_unopened_sealed(client):
    users = _users(client, "own3", "wit3", "wi3b", "wi3c")
    owner, wkey = users["own3"], users["wit3"]
    doc = _seal(client, owner, ["wit3", "wi3b", "wi3c"])
    r = client.post("/me/deactivate", headers=auth(wkey))
    assert r.status_code == 409
    ob = r.json()["detail"]["obligations"]
    assert any(o["type"] == "document_witness" and o["document_id"] == doc["document_id"] for o in ob)
    # 封存人本人没有见证人义务
    r = client.post("/me/deactivate", headers=auth(owner))
    assert r.status_code == 409
    assert all(o["type"] != "document_witness" for o in r.json()["detail"]["obligations"])


def test_witness_of_opened_unpurged_has_no_obligation(client):
    users = _users(client, "own4", "w4a", "w4b", "w4c")
    owner, w1, w2 = users["own4"], users["w4a"], users["w4b"]
    doc = _seal(client, owner, ["w4a", "w4b", "w4c"])
    _open_with_two_approvals(client, owner, doc["document_id"], doc["digest"], [w1, w2])
    # 已开封资料的当前见证人无义务
    assert client.post("/me/deactivate", headers=auth(w1)).status_code == 200
    assert client.post("/me/deactivate", headers=auth(w2)).status_code == 200
    # 但封存人仍持有未清除资料
    r = client.post("/me/deactivate", headers=auth(owner))
    assert r.status_code == 409
    assert any(o["type"] == "document_owner" for o in r.json()["detail"]["obligations"])


def test_obligation_pre_seal_proposer_and_witness(client):
    owner = register(client, "pprop")
    wk = register(client, "pwit")
    for n in ("pw2", "pw3"):
        register(client, n)
    r = client.post(
        "/pre-seal-proposals",
        headers=auth(owner),
        json={"body": "预封存正文", "witnesses": ["pwit", "pw2", "pw3"], "confirm_deadline": _iso(24)},
    )
    assert r.status_code == 201
    pid = r.json()["proposal_id"]

    r = client.post("/me/deactivate", headers=auth(owner))
    assert r.status_code == 409
    assert any(o["type"] == "pre_seal_proposer" and o["proposal_id"] == pid
               for o in r.json()["detail"]["obligations"])

    r = client.post("/me/deactivate", headers=auth(wk))
    assert r.status_code == 409
    assert any(o["type"] == "pre_seal_witness" and o["proposal_id"] == pid
               for o in r.json()["detail"]["obligations"])

    # 发起人撤回后，提案义务解除（第三名称与发起人都可停用）
    assert client.post(f"/pre-seal-proposals/{pid}/revoke", headers=auth(owner)).status_code == 200
    assert client.post("/me/deactivate", headers=auth(owner)).status_code == 200
    assert client.post("/me/deactivate", headers=auth(wk)).status_code == 200


def test_pre_seal_witness_obligation_lifted_when_document_generated(client):
    owner = register(client, "pprop2")
    keys = [register(client, n) for n in ("gx1", "gx2", "gx3")]
    pid = client.post(
        "/pre-seal-proposals",
        headers=auth(owner),
        json={"body": "三人齐后生成资料", "witnesses": ["gx1", "gx2", "gx3"], "confirm_deadline": _iso(24)},
    ).json()["proposal_id"]
    # 提案查询拿 digest
    digest = client.get(f"/pre-seal-proposals/{pid}", headers=auth(keys[0])).json()["digest"]
    for k in keys:
        client.post(f"/pre-seal-proposals/{pid}/confirmations", headers=auth(k), json={"digest": digest})
    # 提案已生成资料：预封存义务消失，转为资料见证人义务（sealed）
    r = client.post("/me/deactivate", headers=auth(keys[0]))
    assert r.status_code == 409
    types = {o["type"] for o in r.json()["detail"]["obligations"]}
    assert "pre_seal_witness" not in types
    assert "document_witness" in types


def test_obligation_handover_assignee(client):
    users = _users(client, "hown", "hnew", "hw1", "hw2", "hw3")
    owner, assignee = users["hown"], users["hnew"]
    doc = _seal(client, owner, ["hw1", "hw2", "hw3"])
    iid = client.post(
        f"/documents/{doc['document_id']}/handover-invitations",
        headers=auth(owner),
        json={"to_username": "hnew", "confirm_deadline": _iso(24)},
    ).json()["invitation_id"]
    # 受让人有义务
    r = client.post("/me/deactivate", headers=auth(assignee))
    assert r.status_code == 409
    assert any(o["type"] == "handover_assignee" and o["invitation_id"] == iid
               for o in r.json()["detail"]["obligations"])
    # 发起人（封存人）只有 owner 义务，没有 assignee 义务
    r = client.post("/me/deactivate", headers=auth(owner))
    assert all(o["type"] != "handover_assignee" for o in r.json()["detail"]["obligations"])
    # 撤回后受让人义务解除
    client.post(f"/handover-invitations/{iid}/revoke", headers=auth(owner))
    assert client.post("/me/deactivate", headers=auth(assignee)).status_code == 200


# ---------------------------------------------------------------------------
# 到点结算先落库，义务在结算后的时点清点
# ---------------------------------------------------------------------------


def test_deactivation_settles_due_purge_and_then_lifts_obligation(client):
    owner = register(client, "settle-own")
    _users(client, "st1", "st2", "st3")
    doc = seal(client, owner, "到点清除后停用", ["st1", "st2", "st3"])
    purge_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
    client.put(f"/documents/{doc['document_id']}/purge", headers=auth(owner), json={"purge_at": purge_at})
    time.sleep(1.2)
    # 不手动触发扫描：停用事务自身的确定性结算先清除，再发现无义务 -> 停用成功
    r = client.post("/me/deactivate", headers=auth(owner))
    assert r.status_code == 200, r.text

    db_path = os.environ["SEAL_DB_PATH"]
    conn = _db(db_path)
    status, body = conn.execute(
        "SELECT status, body FROM documents WHERE id=?", (doc["document_id"],)
    ).fetchone()
    purged_audit = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='purged'").fetchone()[0]
    conn.close()
    assert status == "purged" and body == ""
    assert purged_audit == 1


def test_deactivation_settles_but_still_obligated_keeps_account(client):
    """有两份资料：一份到点被清除（结算落库），另一份仍未清除 -> 拒绝且账号可用。"""
    owner = register(client, "multi-own")
    _users(client, "m1", "m2", "m3", "m4", "m5", "m6")
    due = seal(client, owner, "将被清除", ["m1", "m2", "m3"])
    keep = seal(client, owner, "继续封存", ["m4", "m5", "m6"])
    purge_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
    client.put(f"/documents/{due['document_id']}/purge", headers=auth(owner), json={"purge_at": purge_at})
    time.sleep(1.2)
    r = client.post("/me/deactivate", headers=auth(owner))
    assert r.status_code == 409
    ids = {o["document_id"] for o in r.json()["detail"]["obligations"] if o["type"] == "document_owner"}
    assert ids == {keep["document_id"]}  # 到点那份已被结算清除，不再出现
    assert client.get("/me", headers=auth(owner)).status_code == 200

    db_path = os.environ["SEAL_DB_PATH"]
    conn = _db(db_path)
    assert conn.execute("SELECT status FROM documents WHERE id=?", (due["document_id"],)).fetchone()[0] == "purged"
    conn.close()


def test_deactivation_settles_expired_pre_seal_proposal(client, monkeypatch):
    """待确认预封存到截止时未集齐：停用事务先使其失效清正文，发起人/见证人义务解除。"""
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "60")  # 不依赖扫描器
    owner = register(client, "exp-prop")
    wk = register(client, "exp-wit")
    for n in ("exp2", "exp3"):
        register(client, n)
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
    pid = client.post(
        "/pre-seal-proposals",
        headers=auth(owner),
        json={"body": "即将失效的提案正文", "witnesses": ["exp-wit", "exp2", "exp3"],
              "confirm_deadline": deadline},
    ).json()["proposal_id"]
    time.sleep(1.2)
    assert client.post("/me/deactivate", headers=auth(owner)).status_code == 200

    conn = _db(os.environ["SEAL_DB_PATH"])
    row = conn.execute("SELECT status, body FROM pre_seal_proposals WHERE id=?", (pid,)).fetchone()
    conn.close()
    assert row["status"] == "expired" and row["body"] == ""
    # 见证人义务也随失效解除
    assert client.post("/me/deactivate", headers=auth(wk)).status_code == 200


def test_deactivation_settles_due_open_and_expired_request(client):
    """waiting 两票已齐到点：停用事务先开封（封存人仍持有已开封未清除资料，
    仍是封存人义务，继续拒绝）；pending 过截止的请求在结算中失效。"""
    users = _users(client, "sett-open", "so1", "so2", "so3")
    owner, w1, w2 = users["sett-open"], users["so1"], users["so2"]
    doc = _seal(client, owner, ["so1", "so2", "so3"])
    now = dt.datetime.now(dt.timezone.utc)
    req_id = client.post(
        f"/documents/{doc['document_id']}/open-requests",
        headers=auth(owner),
        json={"reason": "定时开封", "deadline": (now + dt.timedelta(hours=24)).isoformat(),
              "earliest_open_at": (now + dt.timedelta(seconds=1)).isoformat()},
    ).json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", headers=auth(w1), json={"digest": doc["digest"]})
    client.post(f"/open-requests/{req_id}/approve", headers=auth(w2), json={"digest": doc["digest"]})
    time.sleep(1.2)
    # 停用事务先把 waiting 开封；开封后封存人仍持有未清除资料 -> 409
    r = client.post("/me/deactivate", headers=auth(owner))
    assert r.status_code == 409
    conn = _db(os.environ["SEAL_DB_PATH"])
    doc_status, req_status = conn.execute(
        "SELECT d.status, r.status FROM documents d JOIN open_requests r ON r.document_id=d.id WHERE r.id=?",
        (req_id,),
    ).fetchone()
    conn.close()
    assert doc_status == "opened" and req_status == "opened"


# ---------------------------------------------------------------------------
# 停用账号不得再被指定为见证人 / 受让人 / 预封存见证人
# ---------------------------------------------------------------------------


def test_deactivated_user_rejected_as_witness_in_seal(client):
    owner = register(client, "sowner")
    gone = register(client, "gonew")
    for n in ("a1", "a2"):
        register(client, n)
    client.post("/me/deactivate", headers=auth(gone))
    r = client.post(
        "/documents",
        headers=auth(owner),
        json={"body": "x", "witnesses": ["gonew", "a1", "a2"]},
    )
    assert r.status_code == 400 and "gonew" in r.json()["detail"]


def test_deactivated_user_rejected_as_pre_seal_witness(client):
    owner = register(client, "powner")
    gone = register(client, "pgone")
    for n in ("p1", "p2"):
        register(client, n)
    client.post("/me/deactivate", headers=auth(gone))
    r = client.post(
        "/pre-seal-proposals",
        headers=auth(owner),
        json={"body": "x", "witnesses": ["pgone", "p1", "p2"], "confirm_deadline": _iso(24)},
    )
    assert r.status_code == 400 and "pgone" in r.json()["detail"]


def test_deactivated_user_rejected_as_replacement_witness(client):
    owner = register(client, "rowner")
    _users(client, "r1", "r2", "r3")
    doc = seal(client, owner, "x", ["r1", "r2", "r3"])
    gone = register(client, "rgone")
    client.post("/me/deactivate", headers=auth(gone))
    r = client.post(
        f"/documents/{doc['document_id']}/witness-replacements",
        headers=auth(owner),
        json={"old_witness": "r1", "new_witness": "rgone"},
    )
    assert r.status_code == 400 and r.json() == {"detail": "新见证人尚未注册"}


def test_deactivated_user_rejected_as_handover_assignee(client):
    owner = register(client, "howner2")
    gone = register(client, "hgone")
    _users(client, "hh1", "hh2", "hh3")
    doc = _seal(client, owner, ["hh1", "hh2", "hh3"])
    client.post("/me/deactivate", headers=auth(gone))
    r = client.post(
        f"/documents/{doc['document_id']}/handover-invitations",
        headers=auth(owner),
        json={"to_username": "hgone", "confirm_deadline": _iso(24)},
    )
    assert r.status_code == 400 and r.json() == {"detail": "受让人尚未注册"}


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


def test_concurrent_deactivation_first_blocks_role_assignment(live_server):
    """停用先提交：并发把该账号换入见证名单的替换按现有 400（未注册）拒绝。"""
    base, db_path = live_server
    with httpx.Client(timeout=15) as c:
        owner = _reg(c, base, "cowner")
        gone = _reg(c, base, "cgone")
        for n in ("cw1", "cw2", "cw3"):
            _reg(c, base, n)
        doc_id = c.post(
            base + "/documents", headers={"X-API-Key": owner},
            json={"body": "并发赛跑", "witnesses": ["cw1", "cw2", "cw3"]},
        ).json()["document_id"]
        barrier = threading.Barrier(2)

        def deactivate():
            barrier.wait()
            return c.post(base + "/me/deactivate", headers={"X-API-Key": gone}).status_code

        def replace_late():
            barrier.wait()
            time.sleep(0.5)  # 让停用先拿锁提交
            r = c.post(
                base + f"/documents/{doc_id}/witness-replacements",
                headers={"X-API-Key": owner},
                json={"old_witness": "cw1", "new_witness": "cgone"},
            )
            return r.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(deactivate)
            f2 = pool.submit(replace_late)
            assert f1.result() == 200
            assert f2.result() == 400

    conn = _db(db_path)
    witnesses = [r[0] for r in conn.execute(
        "SELECT u.username FROM document_witnesses w JOIN users u ON u.id=w.user_id"
        " WHERE w.document_id=? ORDER BY w.position", (doc_id,)).fetchall()]
    status = conn.execute("SELECT status FROM users WHERE username='cgone'").fetchone()[0]
    conn.close()
    assert "cgone" not in witnesses
    assert status == "deactivated"


def test_concurrent_role_assignment_first_blocks_deactivation(live_server):
    """角色先提交：停用在锁内看到本人已是未开封资料当前见证人 -> 409，账号可用。"""
    base, db_path = live_server
    with httpx.Client(timeout=15) as c:
        owner = _reg(c, base, "downer")
        gone = _reg(c, base, "dgone")
        for n in ("dw2", "dw3"):
            _reg(c, base, n)
        barrier = threading.Barrier(2)

        def seal_first():
            barrier.wait()
            r = c.post(
                base + "/documents", headers={"X-API-Key": owner},
                json={"body": "先提交的封存", "witnesses": ["dgone", "dw2", "dw3"]},
            )
            return r.status_code

        def deactivate_late():
            barrier.wait()
            time.sleep(0.5)  # 让封存先提交
            r = c.post(base + "/me/deactivate", headers={"X-API-Key": gone})
            return r.status_code, r.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(seal_first)
            f2 = pool.submit(deactivate_late)
            assert f1.result() == 201
            code, body = f2.result()

    assert code == 409
    assert any(o["type"] == "document_witness" for o in body["detail"]["obligations"])
    conn = _db(db_path)
    status = conn.execute("SELECT status FROM users WHERE username='dgone'").fetchone()[0]
    conn.close()
    assert status == "active"  # 未部分停用


def test_concurrent_deactivation_vs_rotation_by_commit_order(live_server):
    """停用与密钥轮换并发：只有一个赢家；无论谁先，旧密钥之后一律 401，
    且不会出现既停用又轮换成功的矛盾（账号不会出现“停用后新密钥可用”）。"""
    base, _ = live_server
    with httpx.Client(timeout=15) as c:
        key = _reg(c, base, "race-user")
        barrier = threading.Barrier(2)

        def deactivate():
            barrier.wait()
            r = c.post(base + "/me/deactivate", headers={"X-API-Key": key})
            return r.status_code

        def rotate():
            barrier.wait()
            r = c.post(base + "/me/api-key/rotate", headers={"X-API-Key": key})
            return r.status_code, r.json().get("api_key")

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(deactivate)
            f2 = pool.submit(rotate)
            code_deact = f1.result()
            code_rot, new_key = f2.result()

    assert {code_deact, code_rot} == {200, 401}
    with httpx.Client(timeout=15) as c:
        # 原始密钥必然失效
        assert c.get(base + "/me", headers={"X-API-Key": key}).status_code == 401
        if code_rot == 200:
            # 轮换先赢：新密钥可用；停用尚未发生，可凭新密钥再次申请
            assert c.get(base + "/me", headers={"X-API-Key": new_key}).status_code == 200
            r = c.post(base + "/me/deactivate", headers={"X-API-Key": new_key})
            assert r.status_code == 200
            assert c.get(base + "/me", headers={"X-API-Key": new_key}).status_code == 401
        else:
            # 停用先赢：不存在可用新密钥
            assert new_key is None


def test_concurrent_handover_confirm_vs_deactivation_unique_outcome(live_server):
    """待确认受让人确认交接 vs 受让人停用并发：按提交顺序唯一裁定。
    确认先提交 -> 成为未开封资料封存人，停用 409；停用先提交 -> 确认被现有
    规则拒绝（密钥 401），资料封存人不变。"""
    base, db_path = live_server
    with httpx.Client(timeout=15) as c:
        owner = _reg(c, base, "hv-owner")
        assignee = _reg(c, base, "hv-new")
        for n in ("hv1", "hv2", "hv3"):
            _reg(c, base, n)
        doc_id = c.post(
            base + "/documents", headers={"X-API-Key": owner},
            json={"body": "交接/停用赛跑", "witnesses": ["hv1", "hv2", "hv3"]},
        ).json()["document_id"]
        iid = c.post(
            base + f"/documents/{doc_id}/handover-invitations",
            headers={"X-API-Key": owner},
            json={"to_username": "hv-new", "confirm_deadline": _iso(24)},
        ).json()["invitation_id"]
        barrier = threading.Barrier(2)

        def confirm():
            barrier.wait()
            r = c.post(base + f"/handover-invitations/{iid}/confirm", headers={"X-API-Key": assignee})
            return r.status_code

        def deactivate():
            barrier.wait()
            r = c.post(base + "/me/deactivate", headers={"X-API-Key": assignee})
            return r.status_code

        # 多跑几轮都必须只有一个终态；这里用单轮屏障+立即提交（两者几乎同时抢锁）
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(confirm)
            f2 = pool.submit(deactivate)
            code_confirm = f1.result()
            code_deact = f2.result()

    assert sorted([code_confirm, code_deact]) in ([200, 401], [200, 409]), (code_confirm, code_deact)
    conn = _db(db_path)
    owner_now = conn.execute("SELECT owner_id FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
    assignee_status = conn.execute("SELECT status FROM users WHERE username='hv-new'").fetchone()[0]
    inv_status = conn.execute("SELECT status FROM handover_invitations WHERE id=?", (iid,)).fetchone()[0]
    conn.close()
    if code_confirm == 200:
        # 交接先提交：受让人成为封存人，停用必须失败且账号仍 active
        assert code_deact in (401, 409)
        assert assignee_status == "active"
        assert inv_status == "confirmed"
        assert owner_now is not None
    else:
        # 停用先提交：确认 401（密钥失效），身份不转移
        assert code_confirm == 401 and code_deact == 200
        assert assignee_status == "deactivated"
        assert inv_status == "pending"
