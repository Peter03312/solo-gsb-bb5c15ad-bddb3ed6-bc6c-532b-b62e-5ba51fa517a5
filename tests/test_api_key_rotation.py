"""API Key 轮换：一次性返回新密钥、库内只存摘要、旧密钥立即失效、
重启后仍失效；与已认证业务写并发时按 BEGIN IMMEDIATE 提交先后判定。

并发用例使用真实 uvicorn 服务（后台线程）+ httpx，与 test_concurrency.py 一致。
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


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _audit_rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 基础语义（TestClient）
# ---------------------------------------------------------------------------


def test_rotate_returns_new_key_once_and_invalidates_old(client):
    old = register(client, "alice")
    r = client.post("/me/api-key/rotate", headers=auth(old))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"user_id", "username", "api_key", "rotated_at"}
    assert body["username"] == "alice"
    new = body["api_key"]
    assert len(new) == 64 and new != old

    # 新密钥立即可用；旧密钥立即失效（读 /me 与业务写均 401）
    assert client.get("/me", headers=auth(new)).json()["username"] == "alice"
    r = client.get("/me", headers=auth(old))
    assert r.status_code == 401 and r.json() == {"detail": "凭据无效"}

    for name in ("bob", "carol", "dave"):
        register(client, name)
    r = client.post(
        "/documents",
        headers=auth(old),
        json={"body": "旧密钥封存内容", "witnesses": ["bob", "carol", "dave"]},
    )
    assert r.status_code == 401
    # 新密钥业务写照常
    r = seal(client, new, "新密钥封存内容", ["bob", "carol", "dave"])
    assert r["status"] == "sealed"


def test_rotate_again_invalidates_previous_new_key(client):
    k1 = register(client, "rot")
    k2 = client.post("/me/api-key/rotate", headers=auth(k1)).json()["api_key"]
    k3 = client.post("/me/api-key/rotate", headers=auth(k2)).json()["api_key"]
    # 明文无法从任何接口找回：只有当前密钥可用
    assert client.get("/me", headers=auth(k3)).status_code == 200
    assert client.get("/me", headers=auth(k2)).status_code == 401
    assert client.get("/me", headers=auth(k1)).status_code == 401


def test_persistence_only_digest_stored_and_survives_restart(tmp_path, monkeypatch):
    db = tmp_path / "rot.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    from app.main import app

    old = None
    new = None
    with TestClient(app) as c:
        old = register(c, "persist")
        new = c.post("/me/api-key/rotate", headers=auth(old)).json()["api_key"]

    # 库里只有新密钥的 SHA-256 摘要：既无明文，也没有旧摘要残留
    conn = sqlite3.connect(db)
    stored_hash, username = conn.execute(
        "SELECT key_hash, username FROM users WHERE username='persist'"
    ).fetchone()
    conn.close()
    assert stored_hash == _hash(new)
    assert stored_hash != _hash(old)
    raw = db.read_bytes() + (tmp_path / "rot.db-wal").read_bytes() if (tmp_path / "rot.db-wal").exists() else db.read_bytes()
    assert new.encode() not in raw and old.encode() not in raw

    # 重启（同一数据库、新建 TestClient 触发生命周期）后结论不变
    with TestClient(app) as c2:
        assert c2.get("/me", headers=auth(new)).status_code == 200
        assert c2.get("/me", headers=auth(old)).status_code == 401


def test_rotation_leaves_audit_but_rejected_old_write_leaves_none(client):
    old = register(client, "owner-a")
    for name in ("wb1", "wc1", "wd1"):
        register(client, name)
    r = client.post("/me/api-key/rotate", headers=auth(old))
    assert r.status_code == 200

    # 旧密钥的封存被拒：资料不产生、不留下任何业务审计
    r = client.post(
        "/documents",
        headers=auth(old),
        json={"body": "不应落库的正文", "witnesses": ["wb1", "wc1", "wd1"]},
    )
    assert r.status_code == 401 and r.json() == {"detail": "凭据无效"}

    import os

    db_path = os.environ["SEAL_DB_PATH"]
    rows = _audit_rows(db_path)
    actions = [row["action"] for row in rows]
    assert actions.count("register") == 4
    assert actions.count("api_key_rotated") == 1
    assert "seal" not in actions
    rot = [row for row in rows if row["action"] == "api_key_rotated"][0]
    assert rot["actor_id"] is not None and rot["document_id"] is None
    # 审计细节不含任何密钥材料
    assert old not in rot["detail"]


def test_invalid_missing_and_revoked_key_share_existing_401(client):
    good = register(client, "holder")
    new = client.post("/me/api-key/rotate", headers=auth(good)).json()["api_key"]

    # 无凭据
    r = client.get("/me")
    assert r.status_code == 401 and r.json() == {"detail": "缺少 API Key"}
    # 随机无效密钥与“已轮换失效的旧密钥”响应完全一致，不泄露账号存在性
    r1 = client.get("/me", headers=auth("deadbeef" * 8))
    r2 = client.get("/me", headers=auth(good))
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json() == {"detail": "凭据无效"}
    # 新密钥不受影响
    assert client.get("/me", headers=auth(new)).status_code == 200


def test_unrotated_account_and_anonymous_endpoints_unchanged(client):
    # 轮换一个账号，不影响其他未轮换账号
    rotated_old = register(client, "rot-user")
    untouched = register(client, "plain-user")
    for name in ("x1", "x2", "x3"):
        register(client, name)
    client.post("/me/api-key/rotate", headers=auth(rotated_old))

    assert client.get("/me", headers=auth(untouched)).status_code == 200
    r = seal(client, untouched, "未轮换账号照常封存", ["x1", "x2", "x3"])
    assert r["document_id"] >= 1

    # 无需登录的行为保持原样：健康检查、公钥、兑换、验真都不带凭据
    assert client.get("/health").status_code == 200
    assert client.get("/audit-snapshots/public-key").status_code == 200
    r = client.post("/view-passes/redeem", json={"token": "not-a-real-token"})
    assert r.status_code == 404  # 统一 404，不因轮换变化
    r = client.post(
        "/audit-snapshots/verify",
        json={"snapshot": {}, "signature": "AAAA", "public_key": "AAAA"},
    )
    assert r.status_code in (400, 422)


def test_rotation_does_not_affect_other_users_writes(client):
    owner_old = register(client, "owner-b")
    w = register(client, "w-b1")
    for name in ("w-b2", "w-b3"):
        register(client, name)
    doc = seal(client, owner_old, "轮换前已封存正文 KEYROT", ["w-b1", "w-b2", "w-b3"])
    dg = doc["digest"]

    # 见证人在封存人轮换密钥后，用本人未轮换密钥完成完整开封流程
    owner_new = client.post(
        "/me/api-key/rotate", headers=auth(owner_old)
    ).json()["api_key"]
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
    req_id = client.post(
        f"/documents/{doc['document_id']}/open-requests",
        headers=auth(owner_new),
        json={"reason": "轮换后继续业务", "deadline": deadline},
    ).json()["request_id"]
    assert client.post(
        f"/open-requests/{req_id}/approve", headers=auth(w), json={"digest": dg}
    ).status_code == 200


# ---------------------------------------------------------------------------
# 并发：真实 uvicorn 服务
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    db = tmp_path / "rotlive.db"
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


def test_concurrent_double_rotation_same_old_key_one_winner(live_server):
    base, _ = live_server
    with httpx.Client(timeout=15) as c:
        old = _reg(c, base, "double")
        barrier = threading.Barrier(2)

        def rotate():
            barrier.wait()
            r = c.post(base + "/me/api-key/rotate", headers={"X-API-Key": old})
            return r.status_code, r.json().get("api_key") if r.status_code == 200 else None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: rotate(), range(2)))

    winners = [key for code, key in results if code == 200 and key]
    losers = [code for code, _ in results if code != 200]
    assert len(winners) == 1 and len(losers) == 1
    assert losers == [401]
    with httpx.Client(timeout=15) as c:
        assert c.get(base + "/me", headers={"X-API-Key": winners[0]}).status_code == 200
        assert c.get(base + "/me", headers={"X-API-Key": old}).status_code == 401


def test_rotation_first_commits_blocks_old_key_write(live_server):
    """确定性制造“轮换先提交”：业务写在屏障后故意延后拿锁。"""
    base, db_path = live_server
    with httpx.Client(timeout=15) as c:
        old = _reg(c, base, "rf")
        for n in ("rf1", "rf2", "rf3"):
            _reg(c, base, n)
        barrier = threading.Barrier(2)

        def rotate():
            barrier.wait()
            r = c.post(base + "/me/api-key/rotate", headers={"X-API-Key": old})
            return r.status_code, r.json().get("api_key")

        def seal_late():
            barrier.wait()
            time.sleep(0.5)  # 让轮换先拿到写锁并提交
            r = c.post(
                base + "/documents",
                headers={"X-API-Key": old},
                json={"body": "不应生效", "witnesses": ["rf1", "rf2", "rf3"]},
            )
            return r.status_code, r.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            rot_fut = pool.submit(rotate)
            seal_fut = pool.submit(seal_late)
            rot_code, new_key = rot_fut.result()
            seal_code, seal_body = seal_fut.result()

    assert rot_code == 200 and new_key
    assert seal_code == 401 and seal_body == {"detail": "凭据无效"}

    with httpx.Client(timeout=15) as c:
        # 旧密钥写入既未生效，也没有业务审计；新密钥一切正常
        docs = c.get(base + "/documents", headers={"X-API-Key": new_key}).json()["documents"]
        assert docs == []
        assert c.get(base + "/me", headers={"X-API-Key": old}).status_code == 401

    conn = sqlite3.connect(db_path)
    actions = [row[0] for row in conn.execute("SELECT action FROM audit_log").fetchall()]
    conn.close()
    assert "seal" not in actions
    assert actions.count("api_key_rotated") == 1

    with httpx.Client(timeout=15) as c:
        r = c.post(
            base + "/documents",
            headers={"X-API-Key": new_key},
            json={"body": "新密钥封存", "witnesses": ["rf1", "rf2", "rf3"]},
        )
        assert r.status_code == 201


def test_business_write_first_commits_then_rotation_completes(live_server):
    """确定性制造“业务写先提交”：轮换延后拿锁；业务写完成，轮换随后照常完成。"""
    base, db_path = live_server
    with httpx.Client(timeout=15) as c:
        old = _reg(c, base, "bf")
        for n in ("bf1", "bf2", "bf3"):
            _reg(c, base, n)
        barrier = threading.Barrier(2)

        def seal_now():
            barrier.wait()
            r = c.post(
                base + "/documents",
                headers={"X-API-Key": old},
                json={"body": "先提交的封存内容 BF", "witnesses": ["bf1", "bf2", "bf3"]},
            )
            return r.status_code, r.json()

        def rotate_late():
            barrier.wait()
            time.sleep(0.5)  # 让业务写先拿锁并提交
            r = c.post(base + "/me/api-key/rotate", headers={"X-API-Key": old})
            return r.status_code, r.json().get("api_key")

        with ThreadPoolExecutor(max_workers=2) as pool:
            seal_fut = pool.submit(seal_now)
            rot_fut = pool.submit(rotate_late)
            seal_code, seal_body = seal_fut.result()
            rot_code, new_key = rot_fut.result()

    assert seal_code == 201 and seal_body["status"] == "sealed"
    assert rot_code == 200 and new_key

    with httpx.Client(timeout=15) as c:
        # 先提交的封存完整保留；轮换提交后旧密钥失效、新密钥可用
        got = c.get(
            f"{base}/documents/{seal_body['document_id']}", headers={"X-API-Key": new_key}
        ).json()
        assert "body" not in got and got["status"] == "sealed"
        assert c.get(base + "/me", headers={"X-API-Key": old}).status_code == 401
        assert c.get(base + "/me", headers={"X-API-Key": new_key}).status_code == 200

    conn = sqlite3.connect(db_path)
    actions = [row[0] for row in conn.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    conn.close()
    assert actions.index("seal") < actions.index("api_key_rotated")


def test_old_key_cannot_trigger_due_purge_on_read_path(live_server):
    """旧密钥读请求不得借“确定性清除结算”写入任何数据。"""
    base, db_path = live_server
    with httpx.Client(timeout=15) as c:
        old = _reg(c, base, "pr")
        for n in ("pr1", "pr2", "pr3"):
            _reg(c, base, n)
        doc_id = c.post(
            base + "/documents",
            headers={"X-API-Key": old},
            json={"body": "读路径清除赛跑 PURGE", "witnesses": ["pr1", "pr2", "pr3"]},
        ).json()["document_id"]
        purge_at = (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
        ).isoformat()
        r = c.put(
            f"{base}/documents/{doc_id}/purge",
            headers={"X-API-Key": old},
            json={"purge_at": purge_at},
        )
        assert r.status_code == 200

        # 到点后先轮换，再用旧密钥查询：必须 401，且不能由该请求触发清除
        time.sleep(1.2)
        new_key = c.post(
            base + "/me/api-key/rotate", headers={"X-API-Key": old}
        ).json()["api_key"]
        r = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": old})
        assert r.status_code == 401

        conn = sqlite3.connect(db_path)
        status, body = conn.execute(
            "SELECT status, body FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        purged_audits = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='purged'"
        ).fetchone()[0]
        conn.close()
        # 旧密钥的读没有触发清除
        assert status == "sealed" and body != ""
        assert purged_audits == 0

        # 合法请求（新密钥）进入读路径时照常确定性清除
        r = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": new_key})
        assert r.status_code == 200 and r.json()["status"] == "purged"
        assert "body" not in r.json()
