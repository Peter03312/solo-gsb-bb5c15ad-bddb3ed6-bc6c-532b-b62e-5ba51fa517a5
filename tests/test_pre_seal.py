"""预封存提案（pre-seal proposal）功能测试。

覆盖：
- 发起校验（三名互异、均已注册、非本人、截止时刻带时区且在未来），
  响应只给提案编号与摘要、不含正文；
- 仅发起人/受邀见证人可见（其他账号与不存在编号统一 403），
  只有受邀见证人可确认、只有发起人可撤回；
- 见证人凭摘要各确认一次：摘要不符/重复确认 409 且不计票；
- 三人截止前确认 -> 同一事务生成普通封存资料（审计与直接封存一致），
  已完成提案重复请求不再生成资料；
- 完成前撤回、到期未集齐失效；撤回/失效/完成后提案正文清空；
- 密钥轮换并发：轮换先提交则持旧密钥的确认/撤回 401 回滚，不产生资料；
- 截止判定以取得写锁后的实际时刻为准：发起/确认/撤回在截止前到达、
  等待写锁期间越过截止时刻的，发起 400 不落提案，确认/撤回先失效清正文
  再 409，不计票、不生成资料；截止前取得写锁的第三票仍同事务生成唯一资料；
- 真实 uvicorn + 多线程：第三票并发只生成一份资料；停机期间到期重启恢复。
"""
import datetime as dt
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import auth, digest_of, register  # noqa: E402


def in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def create_proposal(client, owner_key, body, witnesses, deadline=None):
    return client.post(
        "/pre-seal-proposals",
        json={
            "body": body,
            "witnesses": witnesses,
            "confirm_deadline": deadline or in_hours(1),
        },
        headers=auth(owner_key),
    )


def confirm(client, key, proposal_id, digest):
    return client.post(
        f"/pre-seal-proposals/{proposal_id}/confirmations",
        json={"digest": digest},
        headers=auth(key),
    )


def get_proposal(client, key, proposal_id):
    return client.get(f"/pre-seal-proposals/{proposal_id}", headers=auth(key))


def revoke(client, key, proposal_id):
    return client.post(f"/pre-seal-proposals/{proposal_id}/revoke", headers=auth(key))


@pytest.fixture
def five_users(client):
    keys = {n: register(client, n) for n in ["alice", "bob", "carol", "dave", "erin"]}
    return keys


BODY = "预封存正文：PRE-SEAL-SECRET"
WITNESSES = ["bob", "carol", "dave"]


# ---------------------------------------------------------------------------
# 发起校验与响应形状
# ---------------------------------------------------------------------------


def test_create_proposal_returns_id_and_digest_without_body(client, five_users):
    r = create_proposal(client, five_users["alice"], BODY, WITNESSES)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "pending"
    assert data["digest"] == digest_of(BODY)
    assert data["confirmation_count"] == 0
    assert data["required"] == 3
    assert data["document_id"] is None
    assert data["witnesses"] == WITNESSES
    # 任何位置都不含正文
    assert "body" not in data
    assert BODY not in r.text


@pytest.mark.parametrize(
    "witnesses,code",
    [
        (["bob", "bob", "dave"], 400),          # 不互异
        (["bob", "carol", "carol"], 400),
        (["alice", "carol", "dave"], 400),      # 含发起人本人
        (["bob", "carol", "ghost"], 400),       # 未注册
    ],
)
def test_create_proposal_witness_validation(client, five_users, witnesses, code):
    r = create_proposal(client, five_users["alice"], BODY, witnesses)
    assert r.status_code == code


def test_create_proposal_deadline_validation(client, five_users):
    # 截止时刻已过
    r = create_proposal(client, five_users["alice"], BODY, WITNESSES, deadline=in_hours(-1))
    assert r.status_code == 400
    # 不带时区
    r = client.post(
        "/pre-seal-proposals",
        json={"body": BODY, "witnesses": WITNESSES,
              "confirm_deadline": "2026-10-01T12:00:00"},
        headers=auth(five_users["alice"]),
    )
    assert r.status_code == 422


def test_create_proposal_requires_auth(client, five_users):
    r = client.post(
        "/pre-seal-proposals",
        json={"body": BODY, "witnesses": WITNESSES, "confirm_deadline": in_hours(1)},
    )
    assert r.status_code == 401
    r = create_proposal(client, "wrong-key", BODY, WITNESSES)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 可见性：仅发起人/受邀见证人
# ---------------------------------------------------------------------------


def test_only_party_can_view_proposal(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    for name in ["alice", "bob", "carol", "dave"]:
        assert get_proposal(client, five_users[name], pid).status_code == 200
    # 其他账号与不存在编号统一 403，不区分是否存在
    assert get_proposal(client, five_users["erin"], pid).status_code == 403
    assert get_proposal(client, five_users["erin"], 99999).status_code == 403
    # 无凭据 401
    assert client.get(f"/pre-seal-proposals/{pid}").status_code == 401


def test_get_proposal_excludes_body_in_all_states(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    for name in ["alice", "bob", "carol", "dave"]:
        text = get_proposal(client, five_users[name], pid).text
        assert BODY not in text


# ---------------------------------------------------------------------------
# 确认：摘要绑定、每人一次
# ---------------------------------------------------------------------------


def test_wrong_digest_rejected_and_not_counted(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    r = confirm(client, five_users["bob"], pid, "0" * 64)
    assert r.status_code == 409
    data = get_proposal(client, five_users["alice"], pid).json()
    assert data["confirmation_count"] == 0
    # 错误摘要不计票后，本人用正确摘要仍可确认
    assert confirm(client, five_users["bob"], pid, digest_of(BODY)).status_code == 200
    assert get_proposal(client, five_users["alice"], pid).json()["confirmation_count"] == 1


def test_duplicate_confirmation_counted_once(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    dg = digest_of(BODY)
    assert confirm(client, five_users["bob"], pid, dg).status_code == 200
    assert confirm(client, five_users["bob"], pid, dg).status_code == 409
    assert get_proposal(client, five_users["alice"], pid).json()["confirmation_count"] == 1


def test_only_invited_witness_can_confirm(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    dg = digest_of(BODY)
    # 其他账号
    assert confirm(client, five_users["erin"], pid, dg).status_code == 403
    # 发起人本人不能确认自己的提案
    assert confirm(client, five_users["alice"], pid, dg).status_code == 403
    # 不存在提案统一 403
    assert confirm(client, five_users["erin"], 99999, dg).status_code == 403
    assert get_proposal(client, five_users["alice"], pid).json()["confirmation_count"] == 0


def test_third_confirmation_creates_document(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    dg = digest_of(BODY)
    r1 = confirm(client, five_users["bob"], pid, dg).json()
    assert r1["status"] == "pending" and r1["confirmation_count"] == 1
    assert r1["document_id"] is None
    confirm(client, five_users["carol"], pid, dg)
    r3 = confirm(client, five_users["dave"], pid, dg)
    assert r3.status_code == 200, r3.text
    data = r3.json()
    assert data["status"] == "confirmed"
    assert data["confirmation_count"] == 3
    doc_id = data["document_id"]
    assert isinstance(doc_id, int)

    # 提案视图终结
    final = get_proposal(client, five_users["alice"], pid).json()
    assert final["status"] == "confirmed"
    assert final["document_id"] == doc_id
    assert "body" not in final

    # 生成的资料与直接封存一致：sealed、无正文、三名见证人
    doc = client.get(f"/documents/{doc_id}", headers=auth(five_users["alice"])).json()
    assert doc["status"] == "sealed"
    assert "body" not in doc
    assert doc["witnesses"] == WITNESSES
    for name in WITNESSES:
        assert client.get(f"/documents/{doc_id}", headers=auth(five_users[name])).status_code == 200

    # 审计沿用现有规则：只有一条 seal，actor 为封存人，不含正文
    records = client.get(f"/documents/{doc_id}/audit",
                         headers=auth(five_users["alice"])).json()["records"]
    assert [r["action"] for r in records] == ["seal"]
    assert records[0]["actor"] == "alice"
    assert BODY not in str(records)


def test_completed_proposal_repeated_requests_create_no_more_document(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    dg = digest_of(BODY)
    for w in WITNESSES:
        confirm(client, five_users[w], pid, dg)
    doc_id = get_proposal(client, five_users["alice"], pid).json()["document_id"]
    # 受邀见证人再确认 -> 409，不生成新资料
    for name in ["bob", "carol", "dave"]:
        assert confirm(client, five_users[name], pid, dg).status_code == 409
    # 发起人不是见证人：确认仍 403（不生成资料）
    assert confirm(client, five_users["alice"], pid, dg).status_code == 403
    # 完成后撤回 -> 409
    assert revoke(client, five_users["alice"], pid).status_code == 409
    # 库中只有一份资料
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert conn.execute("SELECT document_id FROM pre_seal_proposals WHERE id=?",
                        (pid,)).fetchone()[0] == doc_id
    # 提案正文已清空
    assert conn.execute("SELECT body FROM pre_seal_proposals WHERE id=?",
                        (pid,)).fetchone()[0] == ""
    conn.close()


# ---------------------------------------------------------------------------
# 撤回
# ---------------------------------------------------------------------------


def test_owner_can_revoke_before_completion(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    dg = digest_of(BODY)
    confirm(client, five_users["bob"], pid, dg)
    r = revoke(client, five_users["alice"], pid)
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"
    # 撤回后不能确认、不能重复撤回
    assert confirm(client, five_users["carol"], pid, dg).status_code == 409
    assert revoke(client, five_users["alice"], pid).status_code == 409

    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    # 正文已清空、确认保留（作废但可追溯）、未生成资料
    assert conn.execute("SELECT body FROM pre_seal_proposals WHERE id=?",
                        (pid,)).fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM pre_seal_confirmations WHERE proposal_id=?",
                        (pid,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    conn.close()


def test_only_owner_can_revoke(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    for name in ["bob", "carol", "dave", "erin"]:
        assert revoke(client, five_users[name], pid).status_code == 403


# ---------------------------------------------------------------------------
# 到期失效
# ---------------------------------------------------------------------------


def test_proposal_expires_at_deadline_via_sweeper(client, five_users, sweep):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES,
                          deadline=in_seconds(1)).json()["proposal_id"]
    dg = digest_of(BODY)
    confirm(client, five_users["bob"], pid, dg)
    time.sleep(1.2)
    assert sweep() >= 0  # 触发一轮扫描

    data = get_proposal(client, five_users["alice"], pid).json()
    assert data["status"] == "expired"
    # 迟到确认 409，不产生资料
    assert confirm(client, five_users["carol"], pid, dg).status_code == 409
    assert revoke(client, five_users["alice"], pid).status_code == 409

    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT body FROM pre_seal_proposals WHERE id=?",
                        (pid,)).fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    conn.close()


def test_proposal_expiry_determined_on_read_without_sweeper(client, five_users, monkeypatch):
    """不依赖扫描器：查询时确定性到期。"""
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES,
                          deadline=in_seconds(1)).json()["proposal_id"]
    time.sleep(1.2)
    # 见证人读也能触发确定性到期
    data = get_proposal(client, five_users["bob"], pid).json()
    assert data["status"] == "expired"
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT body FROM pre_seal_proposals WHERE id=?",
                        (pid,)).fetchone()[0] == ""
    conn.close()


def test_confirm_after_deadline_expires_and_rejects(client, five_users):
    """临界确认：到期时刻到达后的确认使提案先失效（提交），再 409。"""
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES,
                          deadline=in_seconds(1)).json()["proposal_id"]
    dg = digest_of(BODY)
    confirm(client, five_users["bob"], pid, dg)
    confirm(client, five_users["carol"], pid, dg)
    time.sleep(1.2)
    # 第三票迟到：提案先失效，确认 409，不产生资料
    assert confirm(client, five_users["dave"], pid, dg).status_code == 409
    assert get_proposal(client, five_users["alice"], pid).json()["status"] == "expired"
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# 生成的资料沿用现有规则（可被两票开封、可审计快照等）
# ---------------------------------------------------------------------------


def test_generated_document_follows_existing_open_flow(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    dg = digest_of(BODY)
    for w in WITNESSES:
        confirm(client, five_users[w], pid, dg)
    doc_id = get_proposal(client, five_users["alice"], pid).json()["document_id"]

    # 用现有开封流程开封：提交请求 + 两票同意
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "预封存生成资料的开封", "deadline": in_hours(24)},
        headers=auth(five_users["alice"]),
    )
    assert r.status_code == 201, r.text
    req_id = r.json()["request_id"]
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg},
                    headers=auth(five_users["bob"]))
    assert r.status_code == 200
    r = client.post(f"/open-requests/{req_id}/approve", json={"digest": dg},
                    headers=auth(five_users["carol"]))
    assert r.status_code == 200 and r.json()["status"] == "opened"
    opened = client.get(f"/documents/{doc_id}", headers=auth(five_users["alice"])).json()
    assert opened["body"] == BODY

    # 审计链：seal -> open_request_created -> approval -> approval -> opened
    actions = [r["action"] for r in
               client.get(f"/documents/{doc_id}/audit",
                          headers=auth(five_users["alice"])).json()["records"]]
    assert actions == ["seal", "open_request_created", "approval", "approval", "opened"]


def test_generated_document_audit_snapshot(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    dg = digest_of(BODY)
    for w in WITNESSES:
        confirm(client, five_users[w], pid, dg)
    doc_id = get_proposal(client, five_users["alice"], pid).json()["document_id"]
    r = client.post(f"/documents/{doc_id}/audit-snapshots", headers=auth(five_users["alice"]))
    assert r.status_code == 201
    snap = r.json()["snapshot"]
    assert snap["digest"] == dg
    assert [rec["action"] for rec in snap["records"]] == ["seal"]
    assert "body" not in snap


# ---------------------------------------------------------------------------
# 密钥轮换并发
# ---------------------------------------------------------------------------


def test_rotation_committed_first_makes_confirm_401_and_no_document(
    client, five_users, monkeypatch
):
    """轮换先提交：持旧密钥的第三票确认 401 回滚，不产生资料或封存审计。"""
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    dg = digest_of(BODY)
    old_dave = five_users["dave"]
    confirm(client, five_users["bob"], pid, dg)
    confirm(client, five_users["carol"], pid, dg)

    # dave 轮换自己的密钥，再用旧密钥投第三票 -> 401
    rot = client.post("/me/api-key/rotate", headers=auth(old_dave))
    assert rot.status_code == 200
    new_dave = rot.json()["api_key"]
    r = confirm(client, old_dave, pid, dg)
    assert r.status_code == 401
    assert r.json()["detail"] == "凭据无效"
    # 提案仍 pending、两票、无资料
    data = get_proposal(client, five_users["alice"], pid).json()
    assert data["status"] == "pending" and data["confirmation_count"] == 2
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='seal'").fetchone()[0] == 0
    conn.close()
    # 新密钥可正常投第三票并生成资料
    assert confirm(client, new_dave, pid, dg).status_code == 200
    assert get_proposal(client, five_users["alice"], pid).json()["status"] == "confirmed"


def test_rotation_committed_first_makes_revoke_401(client, five_users):
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES).json()["proposal_id"]
    old_alice = five_users["alice"]
    rot = client.post("/me/api-key/rotate", headers=auth(old_alice)).json()
    assert revoke(client, old_alice, pid).status_code == 401
    assert get_proposal(client, rot["api_key"], pid).json()["status"] == "pending"


def test_rotation_committed_first_makes_read_triggered_expiry_not_written(
    client, five_users
):
    """旧密钥的读请求不得触发提案到期写入（沿用读路径密钥复核约定）。

    不依赖扫描器：用较晚的截止时刻，先确认旧密钥读 401 且状态仍 pending，
    再推进到到期后由“新密钥读”确定性结算，隔离读路径自身的密钥复核行为。
    """
    # 截止时刻取 2 秒后：在 401 断言窗口内后台扫描不会先行到期
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES,
                          deadline=in_seconds(2)).json()["proposal_id"]
    old_alice = five_users["alice"]
    new_alice = client.post("/me/api-key/rotate", headers=auth(old_alice)).json()["api_key"]
    # 持旧密钥读：统一 401，且不借助该请求完成任何写入（此刻尚未到期）
    assert get_proposal(client, old_alice, pid).status_code == 401
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT status FROM pre_seal_proposals WHERE id=?",
                        (pid,)).fetchone()[0] == "pending"
    conn.close()
    # 到期后新密钥读确定性结算为 expired
    time.sleep(2.2)
    assert get_proposal(client, new_alice, pid).json()["status"] == "expired"


# ---------------------------------------------------------------------------
# 真实 uvicorn 并发 / 重启
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    db = tmp_path / "preseal-live.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "1")

    import uvicorn
    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
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
        pytest.fail("server did not start")
    yield base, str(db)
    server.should_exit = True
    thread.join(timeout=5)


def _reg(c, base, name):
    r = c.post(base + "/register", json={"username": name})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def test_concurrent_third_votes_create_exactly_one_document(live_server):
    base, _ = live_server
    with httpx.Client(timeout=10) as c:
        keys = {n: _reg(c, base, f"ps-{n}") for n in
                ["owner", "w1", "w2", "w3", "w4", "w5"]}
        body = "并发第三票正文 PRE-CONCURRENT-SECRET"
        dg = digest_of(body)
        pid = c.post(base + "/pre-seal-proposals",
                     json={"body": body,
                           "witnesses": ["ps-w1", "ps-w2", "ps-w3"],
                           "confirm_deadline": in_hours(1)},
                     headers={"X-API-Key": keys["owner"]}).json()["proposal_id"]
        # 先让 w1 投一票；w2/w3 在屏障处并发投“成为第三票”的确认
        c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
               json={"digest": dg}, headers={"X-API-Key": keys["w1"]})

    # 再造两票待投的并发：用两个不同见证人同时投，先到的成为第 2 票、
    # 另一人成为第 3 票 -> 恰好一份资料；再由 w4（非见证人，必 403）做对照。
    barrier = threading.Barrier(2)

    def vote(who):
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                          json={"digest": dg},
                          headers={"X-API-Key": keys[who]}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(vote, ["w2", "w3"]))
    assert sorted(codes) == [200, 200], codes

    with httpx.Client(timeout=10) as c:
        h = {"X-API-Key": keys["owner"]}
        data = c.get(f"{base}/pre-seal-proposals/{pid}", headers=h).json()
        assert data["status"] == "confirmed"
        doc_id = data["document_id"]
        assert isinstance(doc_id, int)
        # 再投/再撤均 409，不产生第二份资料
        assert c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                      json={"digest": dg}, headers={"X-API-Key": keys["w1"]}).status_code == 409
        assert c.post(f"{base}/pre-seal-proposals/{pid}/revoke",
                      headers=h).status_code == 409
        import sqlite3
        conn = sqlite3.connect(live_server[1])
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        rows = conn.execute(
            "SELECT document_id, status FROM pre_seal_proposals WHERE id=?", (pid,)
        ).fetchone()
        assert rows[0] == doc_id and rows[1] == "confirmed"
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE document_id=? AND action='seal'",
            (doc_id,)).fetchone()[0] == 1
        conn.close()


def test_concurrent_confirm_vs_revoke_single_outcome(live_server):
    base, _ = live_server
    with httpx.Client(timeout=10) as c:
        keys = {n: _reg(c, base, f"rv-{n}") for n in ["owner", "w1", "w2", "w3"]}
        body = "确认撤回赛跑正文 PRE-REVOKE-RACE"
        dg = digest_of(body)
        pid = c.post(base + "/pre-seal-proposals",
                     json={"body": body, "witnesses": ["rv-w1", "rv-w2", "rv-w3"],
                           "confirm_deadline": in_hours(1)},
                     headers={"X-API-Key": keys["owner"]}).json()["proposal_id"]
        c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
               json={"digest": dg}, headers={"X-API-Key": keys["w1"]})
        c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
               json={"digest": dg}, headers={"X-API-Key": keys["w2"]})

    # 第三票确认 与 发起人撤回 同时发生：唯一终态（confirmed 或 revoked）
    barrier = threading.Barrier(2)

    def confirm_call():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                          json={"digest": dg},
                          headers={"X-API-Key": keys["w3"]}).status_code

    def revoke_call():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/pre-seal-proposals/{pid}/revoke",
                          headers={"X-API-Key": keys["owner"]}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        cf_code, rv_code = list(pool.map(lambda f: f(), [confirm_call, revoke_call]))

    with httpx.Client(timeout=10) as c:
        h = {"X-API-Key": keys["owner"]}
        status = c.get(f"{base}/pre-seal-proposals/{pid}", headers=h).json()["status"]
        if status == "confirmed":
            assert cf_code == 200 and rv_code == 409
            import sqlite3
            assert sqlite3.connect(live_server[1]).execute(
                "SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        else:
            assert status == "revoked"
            assert rv_code == 200 and cf_code == 409
            import sqlite3
            assert sqlite3.connect(live_server[1]).execute(
                "SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_concurrent_rotation_vs_third_vote_single_outcome(live_server):
    """轮换与第三票并发：按提交先后，要么 401 无资料，要么生成资料后轮换成功。"""
    base, _ = live_server
    with httpx.Client(timeout=10) as c:
        keys = {n: _reg(c, base, f"kr-{n}") for n in ["owner", "w1", "w2", "w3"]}
        body = "轮换赛跑正文 PRE-ROTATION-RACE"
        dg = digest_of(body)
        pid = c.post(base + "/pre-seal-proposals",
                     json={"body": body, "witnesses": ["kr-w1", "kr-w2", "kr-w3"],
                           "confirm_deadline": in_hours(1)},
                     headers={"X-API-Key": keys["owner"]}).json()["proposal_id"]
        c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
               json={"digest": dg}, headers={"X-API-Key": keys["w1"]})
        c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
               json={"digest": dg}, headers={"X-API-Key": keys["w2"]})

    barrier = threading.Barrier(2)

    def vote():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(f"{base}/pre-seal-proposals/{pid}/confirmations",
                          json={"digest": dg},
                          headers={"X-API-Key": keys["w3"]}).status_code

    def rotate():
        barrier.wait()
        with httpx.Client(timeout=10) as c:
            return c.post(base + "/me/api-key/rotate",
                          headers={"X-API-Key": keys["w3"]}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        vote_code, rot_code = list(pool.map(lambda f: f(), [vote, rotate]))

    with httpx.Client(timeout=10) as c:
        h = {"X-API-Key": keys["owner"]}
        status = c.get(f"{base}/pre-seal-proposals/{pid}", headers=h).json()["status"]
        import sqlite3
        doc_count = sqlite3.connect(live_server[1]).execute(
            "SELECT COUNT(*) FROM documents").fetchone()[0]
        # 唯一结果
        if status == "confirmed":
            assert vote_code == 200 and doc_count == 1
        else:
            assert status == "pending" and vote_code == 401 and doc_count == 0
        assert rot_code == 200


def test_expired_during_downtime_recovered_on_restart(tmp_path, monkeypatch):
    """停机期间过截止时刻：重启恢复扫描把提案置 expired 并清空正文。"""
    db = tmp_path / "preseal-restart.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "3600")

    import uvicorn
    from app.main import app

    def serve():
        port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        return server, thread, f"http://127.0.0.1:{port}"

    def wait_ready(base):
        end = time.time() + 10
        while time.time() < end:
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        pytest.fail("server did not start")

    server1, thread1, base1 = serve()
    wait_ready(base1)
    try:
        with httpx.Client(timeout=10) as c:
            owner = _reg(c, base1, "rs-owner")
            ws = [_reg(c, base1, f"rs-w{i}") for i in range(1, 4)]
            body = "停机期间到期正文 PRE-RESTART-SECRET"
            dg = digest_of(body)
            pid = c.post(base1 + "/pre-seal-proposals",
                         json={"body": body,
                               "witnesses": [f"rs-w{i}" for i in range(1, 4)],
                               "confirm_deadline": in_seconds(1)},
                         headers={"X-API-Key": owner}).json()["proposal_id"]
            c.post(f"{base1}/pre-seal-proposals/{pid}/confirmations",
                   json={"digest": dg}, headers={"X-API-Key": ws[0]})
    finally:
        server1.should_exit = True
        thread1.join(timeout=5)

    time.sleep(1.2)  # 在“停机”期间过截止时刻

    server2, thread2, base2 = serve()
    wait_ready(base2)
    try:
        with httpx.Client(timeout=10) as c:
            owner = None  # 重新注册同名账号在新进程不可行；直接冷读库校验
        import sqlite3
        # 启动恢复扫描应已把提案置 expired 并清空正文
        conn = sqlite3.connect(str(db))
        row = conn.execute("SELECT status, body, document_id FROM pre_seal_proposals WHERE id=?",
                           (pid,)).fetchone()
        assert row[0] == "expired" and row[1] == "" and row[2] is None
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        conn.close()
    finally:
        server2.should_exit = True
        thread2.join(timeout=5)


# ---------------------------------------------------------------------------
# 截止判定以取得写锁后的实际时刻为准（等待写锁期间越过截止时刻）
# ---------------------------------------------------------------------------


@contextmanager
def held_write_lock():
    """在测试进程内占用同一数据库文件的写锁，模拟“请求已到达、等待写锁”的窗口。

    占用期间发起的提案写请求会阻塞在 BEGIN IMMEDIATE；释放写锁后，被阻塞的
    请求必须以“取得写锁后的实际时刻”做截止判定，而不是用到达时刻。
    """
    import sqlite3

    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"], timeout=30.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def no_sweeper(monkeypatch):
    """停用后台扫描器：确保越过截止时刻的结算由被测请求自身在取锁后完成。"""
    from app.main import ExpirySweeper

    monkeypatch.setattr(ExpirySweeper, "sweep_once", staticmethod(lambda: 0))
    time.sleep(0.2)  # 让进行中的扫描收尾（测试扫描间隔 0.05s）


def test_create_waiting_past_deadline_rejected_without_proposal(
    client, five_users, no_sweeper
):
    """发起在截止前到达、取得写锁时截止已过：沿用 400 拒绝且不落提案。"""
    outcome = {}

    def late_create():
        r = create_proposal(client, five_users["alice"], BODY, WITNESSES,
                            deadline=in_seconds(1.0))
        outcome["status"] = r.status_code
        outcome["detail"] = r.json().get("detail")

    with held_write_lock():
        t = threading.Thread(target=late_create)
        t.start()
        time.sleep(1.5)  # 持有写锁直至越过 1.0s 的确认截止时刻
    t.join(10)

    assert outcome["status"] == 400
    assert outcome["detail"] == "确认截止时刻必须晚于当前时间"
    # 整体回滚：提案与见证名单都不落库
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM pre_seal_proposals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pre_seal_witnesses").fetchone()[0] == 0
    conn.close()


def test_third_vote_waiting_past_deadline_expires_without_document(
    client, five_users, no_sweeper
):
    """第三票在截止前发起、取得写锁时截止已过：先失效并清空正文，再 409，
    不计新票、不生成资料或封存审计。"""
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES,
                          deadline=in_seconds(1.2)).json()["proposal_id"]
    dg = digest_of(BODY)
    confirm(client, five_users["bob"], pid, dg)
    confirm(client, five_users["carol"], pid, dg)

    outcome = {}

    def late_third_vote():
        r = confirm(client, five_users["dave"], pid, dg)
        outcome["status"] = r.status_code
        outcome["detail"] = r.json().get("detail")

    with held_write_lock():
        t = threading.Thread(target=late_third_vote)
        t.start()
        time.sleep(1.5)  # 第三票等待写锁期间越过 1.2s 的截止时刻
    t.join(10)

    assert outcome["status"] == 409
    assert outcome["detail"] == "预封存提案已过确认截止时刻，确认被拒绝"
    # 提案已失效、不计新票、未生成资料
    data = get_proposal(client, five_users["alice"], pid).json()
    assert data["status"] == "expired"
    assert data["confirmation_count"] == 2
    assert data["document_id"] is None
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    # 正文已清空；无资料、无封存审计
    assert conn.execute("SELECT body FROM pre_seal_proposals WHERE id=?",
                        (pid,)).fetchone()[0] == ""
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='seal'").fetchone()[0] == 0
    conn.close()


def test_revoke_waiting_past_deadline_expires_instead(client, five_users, no_sweeper):
    """撤回在截止前发起、取得写锁时截止已过：先失效并清空正文，再 409。"""
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES,
                          deadline=in_seconds(1.2)).json()["proposal_id"]

    outcome = {}

    def late_revoke():
        r = revoke(client, five_users["alice"], pid)
        outcome["status"] = r.status_code
        outcome["detail"] = r.json().get("detail")

    with held_write_lock():
        t = threading.Thread(target=late_revoke)
        t.start()
        time.sleep(1.5)  # 撤回等待写锁期间越过 1.2s 的截止时刻
    t.join(10)

    assert outcome["status"] == 409
    assert outcome["detail"] == "预封存提案已过确认截止时刻，不能撤回"
    # 到期失效优先于撤回
    assert get_proposal(client, five_users["alice"], pid).json()["status"] == "expired"
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT body FROM pre_seal_proposals WHERE id=?",
                        (pid,)).fetchone()[0] == ""
    conn.close()


def test_third_vote_lock_acquired_before_deadline_creates_document(
    client, five_users, no_sweeper
):
    """截止前取得写锁的有效第三票：仍在同一事务生成唯一资料，
    完成时刻取取得写锁后的实际时刻（不早于写锁释放时刻）。"""
    pid = create_proposal(client, five_users["alice"], BODY, WITNESSES,
                          deadline=in_seconds(5)).json()["proposal_id"]
    dg = digest_of(BODY)
    confirm(client, five_users["bob"], pid, dg)
    confirm(client, five_users["carol"], pid, dg)

    outcome = {}

    def third_vote():
        r = confirm(client, five_users["dave"], pid, dg)
        outcome["status"] = r.status_code
        outcome["json"] = r.json() if r.status_code == 200 else None

    with held_write_lock():
        t = threading.Thread(target=third_vote)
        t.start()
        time.sleep(0.5)  # 写锁在截止前释放，第三票取锁时截止未过
        release_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
    t.join(10)

    assert outcome["status"] == 200
    data = outcome["json"]
    assert data["status"] == "confirmed"
    assert data["confirmation_count"] == 3
    doc_id = data["document_id"]
    assert isinstance(doc_id, int)
    # 完成时刻为取得写锁后的实际时刻，不早于写锁释放时刻
    assert data["resolved_at"] >= release_at
    # 同一事务生成唯一一份资料；已完成提案的重复请求不再生成资料
    import sqlite3
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE document_id=? AND action='seal'",
        (doc_id,)).fetchone()[0] == 1
    conn.close()
    assert confirm(client, five_users["dave"], pid, dg).status_code == 409
