"""一次性阅览凭证（view pass）功能测试。

规则要点（规格）：
- 当前封存人凭资料编号与带时区阅览截止时刻为**已开封**资料签发一次性凭证；
  截止时刻须晚于当前、且严格早于已安排的清除时刻（未安排清除不能签发）；
- 凭证明文不可猜测，库内只存 SHA-256 摘要；明文仅签发响应返回一次，
  响应同时给出供撤销使用的凭证编号 pass_id；
- 无账号外部复核人凭明文在截止前可读取一次正文及摘要，无需 X-API-Key；
- 并发兑换至多一次成功（条件更新原子翻转 active -> redeemed）；
- 当前封存人凭凭证编号可在兑换前撤销；过期/已兑付不能撤销；
- 兑换前须在同一事务结算到期清除，清除时刻与兑换同刻以清除优先；
- 过期、撤销、已兑付、无效凭证或已清除资料统一 404 同一文案，
  不泄露正文或资料存在性，拒绝路径不写记录；
- 签发、成功兑换、撤销均写入原有审计链。
"""
import datetime as dt
import sqlite3
import time

from conftest import auth, digest_of, register, seal

BODY = "一次性阅览绝密正文 VIEW-PASS-SECRET"


def _in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def _in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def _setup_opened(client, *, names=("owner", "w1", "w2", "w3", "other"), purge_h=2.0):
    """注册、封存、两票开封、安排 h 小时后清除；返回 (keys, doc_id, digest)。"""
    keys = {n: register(client, n) for n in names}
    d = seal(client, keys["owner"], BODY, ["w1", "w2", "w3"])
    doc_id, dg = d["document_id"], d["digest"]
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "外部复核", "deadline": _in_hours(1)},
        headers=auth(keys["owner"]),
    )
    assert r.status_code == 201, r.text
    rid = r.json()["request_id"]
    for w in ("w1", "w2"):
        r = client.post(
            f"/open-requests/{rid}/approve",
            json={"digest": dg},
            headers=auth(keys[w]),
        )
        assert r.status_code == 200, r.text
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()["status"] == "opened"
    if purge_h is not None:
        r = client.put(
            f"/documents/{doc_id}/purge",
            json={"purge_at": _in_hours(purge_h)},
            headers=auth(keys["owner"]),
        )
        assert r.status_code == 200, r.text
    return keys, doc_id, dg


def _issue(client, owner_key, doc_id, expires_at):
    return client.post(
        f"/documents/{doc_id}/view-passes",
        json={"expires_at": expires_at},
        headers=auth(owner_key),
    )


def _redeem(client, token, key=None):
    headers = auth(key) if key else {}
    return client.post("/view-passes/redeem", json={"token": token}, headers=headers)


# ---------------------------------------------------------------------------
# 签发
# ---------------------------------------------------------------------------


def test_issue_requires_opened_document_with_purge(client):
    keys = {n: register(client, n) for n in ("o", "a", "b", "c")}
    d = seal(client, keys["o"], BODY, ["a", "b", "c"])
    doc_id = d["document_id"]

    # 未开封 -> 409
    r = _issue(client, keys["o"], doc_id, _in_hours(1))
    assert r.status_code == 409

    # 开封但未安排清除 -> 409
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "x", "deadline": _in_hours(1)},
        headers=auth(keys["o"]),
    )
    rid = r.json()["request_id"]
    for w in ("a", "b"):
        client.post(f"/open-requests/{rid}/approve", json={"digest": d["digest"]},
                    headers=auth(keys[w]))
    r = _issue(client, keys["o"], doc_id, _in_hours(1))
    assert r.status_code == 409
    assert "尚未安排到期清除" in r.json()["detail"]


def test_issue_validation_and_owner_only(client):
    keys, doc_id, _ = _setup_opened(client)

    # 非封存人（见证人/无关账号）-> 403，且不区分资料是否存在
    for name in ("w1", "other"):
        r = _issue(client, keys[name], doc_id, _in_hours(1))
        assert r.status_code == 403, r.text
    r = _issue(client, keys["owner"], 999999, _in_hours(1))
    assert r.status_code == 403

    # 无/错密钥 -> 401
    r = client.post(f"/documents/{doc_id}/view-passes", json={"expires_at": _in_hours(1)})
    assert r.status_code == 401

    # 截止时刻已过 -> 400；不带时区 -> 422
    r = _issue(client, keys["owner"], doc_id, _in_hours(-1))
    assert r.status_code == 400
    r = client.post(
        f"/documents/{doc_id}/view-passes",
        json={"expires_at": "2026-10-01T12:00:00"},
        headers=auth(keys["owner"]),
    )
    assert r.status_code == 422

    # 截止时刻必须严格早于已安排清除时刻（同刻、更晚均拒绝）
    purge_at = client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()["purge_at"]
    assert _issue(client, keys["owner"], doc_id, purge_at).status_code == 400
    assert _issue(client, keys["owner"], doc_id, _in_hours(3)).status_code == 400


def test_issue_response_and_token_storage(client):
    keys, doc_id, dg = _setup_opened(client)
    before = dt.datetime.now(dt.timezone.utc)
    r = _issue(client, keys["owner"], doc_id, _in_hours(1))
    assert r.status_code == 201, r.text
    data = r.json()
    assert set(data) == {
        "pass_id", "document_id", "status", "token", "expires_at", "purge_at", "created_at",
    }
    assert data["status"] == "active" and data["document_id"] == doc_id

    # 明文不可猜测：43+ 字符 URL-safe；两份不同
    token = data["token"]
    assert len(token) >= 43
    r2 = _issue(client, keys["owner"], doc_id, _in_hours(1))
    assert r2.status_code == 201
    assert r2.json()["token"] != token
    assert r2.json()["pass_id"] != data["pass_id"]

    # 库内只存 SHA-256 摘要，明文不落库
    import hashlib
    import os

    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    row = conn.execute("SELECT token_hash, status FROM view_passes WHERE id=?",
                       (data["pass_id"],)).fetchone()
    conn.close()
    assert row[0] == hashlib.sha256(token.encode()).hexdigest()
    assert row[1] == "active"

    # 签发写入审计链
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    issued = [r for r in records if r["action"] == "view_pass_issued"]
    assert len(issued) == 2
    assert str(data["pass_id"]) in issued[0]["detail"]
    # created_at/expires_at 为带 +00:00 的可排序 UTC 串
    assert data["created_at"][-6:] == "+00:00"
    assert dt.datetime.fromisoformat(data["expires_at"]) > before


# ---------------------------------------------------------------------------
# 兑换
# ---------------------------------------------------------------------------


def test_issue_settles_due_waiting_in_same_transaction(client):
    """waiting 请求到点但扫描器尚未处理时，签发临界区先确定性开封再签发成功。"""
    keys = {n: register(client, n) for n in ("ow", "x1", "x2", "x3")}
    d = seal(client, keys["ow"], BODY, ["x1", "x2", "x3"])
    doc_id, dg = d["document_id"], d["digest"]
    # 先安排较晚的清除（未开封资料也可安排）
    assert client.put(
        f"/documents/{doc_id}/purge", json={"purge_at": _in_hours(2)},
        headers=auth(keys["ow"]),
    ).status_code == 200
    # 最早开封时刻 1 秒后，截止 2 小时后；两票立即集齐 -> waiting
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "到点即开", "deadline": _in_hours(2),
              "earliest_open_at": _in_seconds(1)},
        headers=auth(keys["ow"]),
    )
    rid = r.json()["request_id"]
    for w in ("x1", "x2"):
        client.post(f"/open-requests/{rid}/approve", json={"digest": dg},
                    headers=auth(keys[w]))
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["ow"])).json()["latest_open_request"]["status"] == "waiting"

    time.sleep(1.2)  # 已到最早开封时刻，但不手动 sweep
    issued = _issue(client, keys["ow"], doc_id, _in_hours(1))
    assert issued.status_code == 201, issued.text
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["ow"])).json()
    assert got["status"] == "opened"


def test_redeem_once_returns_body_and_digest(client):
    keys, doc_id, dg = _setup_opened(client)
    issued = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()

    # 无需任何账号凭据即可兑换
    r = _redeem(client, issued["token"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["body"] == BODY
    assert data["digest"] == dg
    assert data["status"] == "redeemed"
    assert data["pass_id"] == issued["pass_id"]
    assert data["document_id"] == doc_id
    assert "redeemed_at" in data

    # 第二次兑换：同一 404，不返回正文
    r = _redeem(client, issued["token"])
    assert r.status_code == 404
    assert "body" not in r.text and BODY not in r.text

    # 成功兑换写入审计链（actor 为空：外部复核人无账号）
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    redeemed = [r for r in records if r["action"] == "view_pass_redeemed"]
    assert len(redeemed) == 1 and redeemed[0]["actor"] is None

    # 库内状态为 redeemed
    import os
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    status, redeemed_at = conn.execute(
        "SELECT status, redeemed_at FROM view_passes WHERE id=?", (issued["pass_id"],)
    ).fetchone()
    conn.close()
    assert status == "redeemed" and redeemed_at is not None


def test_redeem_rejections_uniform_404(client):
    keys, doc_id, _ = _setup_opened(client)

    # 无效明文（从未签发）
    r = _redeem(client, "a-totally-made-up-token")
    assert r.status_code == 404
    # 空 token / 缺字段 -> 422（结构错误，错误体不回显正文）
    assert _redeem(client, "").status_code == 422
    assert client.post("/view-passes/redeem", json={}).status_code == 422

    # 已撤销凭证
    issued = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()
    assert client.post(f"/view-passes/{issued['pass_id']}/revoke",
                       headers=auth(keys["owner"])).status_code == 200
    r = _redeem(client, issued["token"])
    assert r.status_code == 404 and BODY not in r.text

    # 已过期凭证（失败不改状态、不留痕）
    issued2 = _issue(client, keys["owner"], doc_id, _in_seconds(1)).json()
    time.sleep(1.2)
    r = _redeem(client, issued2["token"])
    assert r.status_code == 404 and BODY not in r.text
    import os
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    status = conn.execute("SELECT status FROM view_passes WHERE id=?",
                          (issued2["pass_id"],)).fetchone()[0]
    conn.close()
    assert status == "active"  # 过期不重写凭证行

    # 同一文案，无法区分“不存在 / 过期 / 撤销 / 已兑付”
    # 再签发第三份并成功兑付，使“已兑付”也落入同一文案集合
    issued3 = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()
    assert _redeem(client, issued3["token"]).status_code == 200
    msg = r.json()["detail"]
    assert _redeem(client, "another-made-up-token").json()["detail"] == msg
    assert _redeem(client, issued["token"]).json()["detail"] == msg
    assert _redeem(client, issued3["token"]).json()["detail"] == msg

    # 失败兑换不写审计记录；第三份的成功兑换恰好一条
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    assert [r["action"] for r in records].count("view_pass_redeemed") == 1
    assert [r["action"] for r in records].count("view_pass_revoked") == 1


def test_redeem_after_document_purged_is_rejected(client, sweep):
    keys, doc_id, _ = _setup_opened(client, purge_h=2.0)
    # 先签发一份 30 分钟有效的凭证（此时清除时刻为 2 小时后，满足早于清除），
    # 再由封存人把清除改期到 1 秒后，模拟清除时刻临近。
    issued = _issue(client, keys["owner"], doc_id, _in_hours(0.5)).json()
    assert client.put(
        f"/documents/{doc_id}/purge", json={"purge_at": _in_seconds(1)},
        headers=auth(keys["owner"]),
    ).status_code == 200

    # 等扫描器确定性清除
    deadline = time.time() + 5
    while time.time() < deadline:
        sweep()
        got = client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
        if got["status"] == "purged":
            break
        time.sleep(0.1)
    assert got["status"] == "purged"

    r = _redeem(client, issued["token"])
    assert r.status_code == 404 and "body" not in r.text and BODY not in r.text


def test_redeem_settles_due_purge_in_same_transaction_purge_wins(client):
    """兑换请求本身触发到期清除：清除先提交落库，兑换统一拒绝。"""
    keys, doc_id, _ = _setup_opened(client, purge_h=2.0)
    issued = _issue(client, keys["owner"], doc_id, _in_hours(0.5)).json()
    # 封存人随后把清除改期到 1 秒后（凭证仍 active）；到期由兑换临界区结算
    assert client.put(
        f"/documents/{doc_id}/purge", json={"purge_at": _in_seconds(1)},
        headers=auth(keys["owner"]),
    ).status_code == 200
    time.sleep(1.2)  # 不手动 sweep：由兑换临界区结算

    r = _redeem(client, issued["token"])
    assert r.status_code == 404 and BODY not in r.text

    # 清除已随兑换事务提交（purged 审计存在），但兑换不留成功记录
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
    assert got["status"] == "purged" and "body" not in got
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    actions = [r["action"] for r in records]
    assert "purged" in actions
    assert "view_pass_redeemed" not in actions

    import os
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    body, status = conn.execute(
        "SELECT body, status FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    vp_status = conn.execute("SELECT status FROM view_passes WHERE id=?",
                             (issued["pass_id"],)).fetchone()[0]
    conn.close()
    assert body == "" and status == "purged" and vp_status == "active"


def test_redeem_with_api_key_still_works_and_is_once_only(client):
    """带任意（甚至无效的）X-API-Key 不影响兑换：兑换不做账号鉴权。"""
    keys, doc_id, _ = _setup_opened(client)
    issued = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()
    r = _redeem(client, issued["token"], key="any-string-indeed")
    assert r.status_code == 200
    r = _redeem(client, issued["token"], key=keys["owner"])
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 撤销
# ---------------------------------------------------------------------------


def test_revoke_rules(client):
    keys, doc_id, _ = _setup_opened(client)
    issued = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()
    pid = issued["pass_id"]

    # 见证人/无关账号 -> 403；不存在的凭证编号同样 403（不区分是否存在）
    for name in ("w1", "other"):
        r = client.post(f"/view-passes/{pid}/revoke", headers=auth(keys[name]))
        assert r.status_code == 403
    assert client.post("/view-passes/999999/revoke", headers=auth(keys["owner"])).status_code == 403
    assert client.post(f"/view-passes/{pid}/revoke").status_code == 401

    # 封存人撤销成功
    r = client.post(f"/view-passes/{pid}/revoke", headers=auth(keys["owner"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked" and r.json()["pass_id"] == pid

    # 撤销后：兑换 404、重复撤销 409
    assert _redeem(client, issued["token"]).status_code == 404
    r = client.post(f"/view-passes/{pid}/revoke", headers=auth(keys["owner"]))
    assert r.status_code == 409

    # 撤销写入审计链
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    assert [r["action"] for r in records].count("view_pass_revoked") == 1


def test_cannot_revoke_redeemed_or_expired_pass(client):
    keys, doc_id, _ = _setup_opened(client)

    issued = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()
    assert _redeem(client, issued["token"]).status_code == 200
    r = client.post(f"/view-passes/{issued['pass_id']}/revoke", headers=auth(keys["owner"]))
    assert r.status_code == 409 and "已兑付" in r.json()["detail"]

    issued2 = _issue(client, keys["owner"], doc_id, _in_seconds(1)).json()
    time.sleep(1.2)
    r = client.post(f"/view-passes/{issued2['pass_id']}/revoke", headers=auth(keys["owner"]))
    assert r.status_code == 409


def test_revoke_after_purge_settled_in_same_transaction(client, sweep):
    """资料在撤销临界区内到点清除：清除先提交，撤销被拒，凭证不再可用。"""
    keys, doc_id, _ = _setup_opened(client, purge_h=2.0)
    issued = _issue(client, keys["owner"], doc_id, _in_hours(0.5)).json()
    client.put(f"/documents/{doc_id}/purge", json={"purge_at": _in_seconds(1)},
               headers=auth(keys["owner"]))
    time.sleep(1.2)  # 不手动 sweep

    r = client.post(f"/view-passes/{issued['pass_id']}/revoke", headers=auth(keys["owner"]))
    assert r.status_code == 409
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["owner"])).json()
    assert got["status"] == "purged"
    assert _redeem(client, issued["token"]).status_code == 404


# ---------------------------------------------------------------------------
# 并发：兑换至多一次成功
# ---------------------------------------------------------------------------


def test_concurrent_redeem_only_one_succeeds(client):
    keys, doc_id, _ = _setup_opened(client)
    issued = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()

    import threading

    results: list[int] = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(_redeem(client, issued["token"]).status_code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(404) == 7

    import os
    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    n = conn.execute(
        "SELECT COUNT(*) FROM view_passes WHERE id=? AND status='redeemed'",
        (issued["pass_id"],),
    ).fetchone()[0]
    conn.close()
    assert n == 1
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()["records"]
    assert [r["action"] for r in records].count("view_pass_redeemed") == 1


def test_concurrent_redeem_vs_revoke_single_outcome(client):
    keys, doc_id, _ = _setup_opened(client)
    issued = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()
    token = issued["token"]
    pid = issued["pass_id"]

    import threading

    outcomes: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def redeem():
        barrier.wait()
        outcomes["redeem"] = _redeem(client, token).status_code

    def revoke():
        barrier.wait()
        outcomes["revoke"] = client.post(
            f"/view-passes/{pid}/revoke", headers=auth(keys["owner"])
        ).status_code

    threads = [threading.Thread(target=redeem), threading.Thread(target=revoke)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一方成功：兑换 200 则撤销 409；撤销 200 则兑换 404
    assert outcomes["redeem"] == 200 or outcomes["revoke"] == 200
    assert not (outcomes["redeem"] == 200 and outcomes["revoke"] == 200)
    if outcomes["redeem"] == 200:
        assert outcomes["revoke"] == 409
    else:
        assert outcomes["revoke"] == 200 and outcomes["redeem"] == 404

    # 终态稳定：再兑换必失败
    assert _redeem(client, token).status_code == 404


# ---------------------------------------------------------------------------
# 兼容性：既有读权限 / 清除 / 审计快照约定保持不变
# ---------------------------------------------------------------------------


def test_existing_participant_reads_and_purge_unchanged(client):
    keys, doc_id, _ = _setup_opened(client)
    issued = _issue(client, keys["owner"], doc_id, _in_hours(1)).json()

    # 见证人仍可读正文（既有读权限不受影响）
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["w1"])).json()
    assert got["body"] == BODY

    # 快照仍可由参与方签发且包含凭证相关审计记录、不含正文
    r = client.post(f"/documents/{doc_id}/audit-snapshots", headers=auth(keys["w1"]))
    assert r.status_code == 201, r.text
    snap = r.json()["snapshot"]
    serialized = str(snap)
    assert BODY not in serialized
    actions = {rec["action"] for rec in snap["records"]}
    assert "view_pass_issued" in actions

    # 兑换后审计快照同样包含 view_pass_redeemed
    assert _redeem(client, issued["token"]).status_code == 200
    r = client.post(f"/documents/{doc_id}/audit-snapshots", headers=auth(keys["owner"]))
    snap = r.json()["snapshot"]
    assert BODY not in str(snap)
    assert "view_pass_redeemed" in {rec["action"] for rec in snap["records"]}
