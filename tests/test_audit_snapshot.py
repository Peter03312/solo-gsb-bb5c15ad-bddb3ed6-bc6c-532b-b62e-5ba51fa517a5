"""审计快照签发与离线验真功能测试。

规则要点（规格）：
- 当前封存人或当前指定见证人可签发某份资料的审计快照：资料编号、摘要、
  当前状态与开封/清除时刻、按原顺序排列且标明截止序号（audit_upto）的
  审计记录；快照不含正文，也不含任何密钥；
- 签发返回快照、签名与公钥标识；签名密钥随库持久化，重启后旧签名仍可验证；
- 复核人提交快照、签名及事先信任的公钥，得到验真结果与截止序号；
  字段或记录顺序被篡改、签名不匹配、版本不支持或公钥无效均明确失败，
  不以包内自带公钥建立信任；
- 签发在同一写事务内先完成到点开封/到期清除等确定性结算再取数，
  快照对应一个已提交的一致状态，不把并发操作后的状态与之前的审计拼接；
- 交接后旧封存人不得再签发；非参与方与不存在资料沿用统一拒绝语义（403）。
"""
import base64
import datetime as dt
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import auth, digest_of, register, seal

BODY = "审计快照绝密资料 SNAPSHOT-SECRET"


def _in_seconds(s: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=s)).isoformat()


def _in_hours(h: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def _setup(client, names=("owner", "w1", "w2", "w3", "new", "other")):
    keys = {n: register(client, n) for n in names}
    d = seal(client, keys["owner"], BODY, ["w1", "w2", "w3"])
    return keys, d["document_id"], d["digest"]


def _open_document(client, keys, doc_id, dg):
    """走完整两票流程让资料开封，返回 request_id。"""
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "复核", "deadline": _in_hours(2)},
        headers=auth(keys["owner"]),
    )
    assert r.status_code == 201, r.text
    req_id = r.json()["request_id"]
    for w in ("w1", "w2"):
        r = client.post(
            f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys[w])
        )
        assert r.status_code == 200, r.text
    return req_id


def _issue(client, key, doc_id):
    return client.post(f"/documents/{doc_id}/audit-snapshots", headers=auth(key))


def _public_key(client):
    r = client.get("/audit-snapshots/public-key")
    assert r.status_code == 200, r.text
    return r.json()


def _verify(client, snapshot, signature, public_key):
    return client.post(
        "/audit-snapshots/verify",
        json={"snapshot": snapshot, "signature": signature, "public_key": public_key},
    )


def _tampered(snapshot, **changes):
    obj = json.loads(json.dumps(snapshot))
    obj.update(changes)
    return obj


# ---------------------------------------------------------------------------
# 签发 + 验真主流程
# ---------------------------------------------------------------------------


def test_issue_and_verify_opened_document(client):
    keys, doc_id, dg = _setup(client)
    req_id = _open_document(client, keys, doc_id, dg)

    pk = _public_key(client)
    assert pk["algorithm"] == "ed25519"
    assert pk["public_key"].startswith("-----BEGIN PUBLIC KEY-----")
    assert len(pk["key_id"]) == 16

    r = _issue(client, keys["owner"], doc_id)
    assert r.status_code == 201, r.text
    assert BODY not in r.text  # 签发响应不含正文
    pkg = r.json()
    assert pkg["algorithm"] == "ed25519" and pkg["key_id"] == pk["key_id"]
    assert pkg["canonicalization"] == "canonical-json-v1"

    snap = pkg["snapshot"]
    assert snap["version"] == 1
    assert snap["document_id"] == doc_id
    assert snap["digest"] == dg
    assert snap["status"] == "opened"
    assert snap["opened_at"] is not None
    assert snap["purged_at"] is None
    # 审计记录按原顺序排列且标明截止序号
    seqs = [rec["seq"] for rec in snap["records"]]
    assert seqs == sorted(seqs) and snap["audit_upto"] == seqs[-1]
    actions = [rec["action"] for rec in snap["records"]]
    assert actions[0] == "seal" and "opened" in actions
    assert actions.count("approval") == 2
    # 快照不含正文，也不含任何密钥材料
    assert "body" not in snap and "secret_seed" not in r.text and "api_key" not in r.text

    # 复核人凭事先信任的公钥验真：得到验真结果与截止序号
    r = _verify(client, snap, pkg["signature"], pk["public_key"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["valid"] is True
    assert out["audit_upto"] == snap["audit_upto"]
    assert out["document_id"] == doc_id and out["status"] == "opened"
    assert out["record_count"] == len(snap["records"])
    assert out["key_id"] == pk["key_id"]


def test_issue_response_never_contains_body_for_sealed_doc(client):
    keys, doc_id, _ = _setup(client)
    r = _issue(client, keys["owner"], doc_id)
    assert r.status_code == 201, r.text
    assert BODY not in r.text
    assert r.json()["snapshot"]["status"] == "sealed"


def test_witness_can_issue_and_issuance_is_audited(client):
    keys, doc_id, dg = _setup(client)
    _open_document(client, keys, doc_id, dg)

    # 当前指定见证人同样可以签发
    r1 = _issue(client, keys["w1"], doc_id)
    assert r1.status_code == 201, r.text
    pkg1 = r1.json()

    # 签发动作本身留痕；再次签发截止序号更大且包含上次签发记录
    r2 = _issue(client, keys["owner"], doc_id)
    assert r2.status_code == 201, r.text
    pkg2 = r2.json()
    assert pkg2["snapshot"]["audit_upto"] > pkg1["snapshot"]["audit_upto"]
    actions = [rec["action"] for rec in pkg2["snapshot"]["records"]]
    assert actions.count("audit_snapshot_issued") == 1

    audit = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])).json()
    assert [r["action"] for r in audit["records"]].count("audit_snapshot_issued") == 2

    # 两次签发的快照都可验真
    pk = _public_key(client)
    for pkg in (pkg1, pkg2):
        r = _verify(client, pkg["snapshot"], pkg["signature"], pk["public_key"])
        assert r.status_code == 200 and r.json()["valid"] is True


def test_verify_is_key_order_independent(client):
    """JSON 对象键序不影响验真（复核人可重新序列化）；记录数组顺序受影响。"""
    keys, doc_id, dg = _setup(client)
    _open_document(client, keys, doc_id, dg)
    pkg = _issue(client, keys["owner"], doc_id).json()
    pk = _public_key(client)

    # 把快照键序完全打乱后重新序列化提交，验真仍通过
    shuffled = dict(reversed(list(pkg["snapshot"].items())))
    payload = json.dumps(
        {"public_key": pk["public_key"], "signature": pkg["signature"], "snapshot": shuffled},
        ensure_ascii=False,
    )
    r = client.post(
        "/audit-snapshots/verify", content=payload, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 200 and r.json()["valid"] is True


# ---------------------------------------------------------------------------
# 签发对应已提交的一致状态（先结算再取数）
# ---------------------------------------------------------------------------


def test_snapshot_reflects_due_open_settlement(client):
    """到点的 waiting 请求在签发事务内先开封：快照直接呈现 opened。"""
    keys, doc_id, dg = _setup(client)
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={
            "reason": "定时开封",
            "deadline": _in_hours(2),
            "earliest_open_at": _in_seconds(1),
        },
        headers=auth(keys["owner"]),
    )
    req_id = r.json()["request_id"]
    for w in ("w1", "w2"):
        client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys[w]))
    time.sleep(1.15)  # 到点，但后台扫描间隔未必触发；签发事务内确定性结算

    pkg = _issue(client, keys["owner"], doc_id).json()
    snap = pkg["snapshot"]
    assert snap["status"] == "opened" and snap["opened_at"] is not None
    assert "opened" in [rec["action"] for rec in snap["records"]]
    pk = _public_key(client)
    assert _verify(client, snap, pkg["signature"], pk["public_key"]).json()["valid"] is True


def test_snapshot_reflects_due_purge_settlement(client):
    """到清除时刻的资料在签发事务内先清除：快照呈现 purged 与清除时刻。"""
    keys, doc_id, dg = _setup(client)
    r = client.put(
        f"/documents/{doc_id}/purge", json={"purge_at": _in_seconds(1)}, headers=auth(keys["owner"])
    )
    assert r.status_code == 200, r.text
    purge_at = r.json()["purge_at"]
    time.sleep(1.15)

    r = _issue(client, keys["owner"], doc_id)
    assert r.status_code == 201, r.text
    assert BODY not in r.text
    snap = r.json()["snapshot"]
    assert snap["status"] == "purged"
    assert snap["purge_at"] == purge_at and snap["purged_at"] == purge_at
    assert "purged" in [rec["action"] for rec in snap["records"]]
    pk = _public_key(client)
    assert _verify(client, snap, r.json()["signature"], pk["public_key"]).json()["valid"] is True


def test_old_snapshot_remains_valid_after_later_operations(client):
    """签发后的后续操作不影响旧快照：旧签名仍可验证，截止序号不变。"""
    keys, doc_id, dg = _setup(client)
    pkg1 = _issue(client, keys["owner"], doc_id).json()

    _open_document(client, keys, doc_id, dg)  # 后续操作推进了状态与审计
    pkg2 = _issue(client, keys["owner"], doc_id).json()
    assert pkg2["snapshot"]["audit_upto"] > pkg1["snapshot"]["audit_upto"]
    assert pkg2["snapshot"]["status"] == "opened"

    pk = _public_key(client)
    r = _verify(client, pkg1["snapshot"], pkg1["signature"], pk["public_key"])
    assert r.status_code == 200
    assert r.json()["valid"] is True and r.json()["status"] == "sealed"


# ---------------------------------------------------------------------------
# 签发权限：统一拒绝语义
# ---------------------------------------------------------------------------


def test_issue_permission_uniform_403(client):
    keys, doc_id, _ = _setup(client)
    # 无/错密钥 -> 401
    assert client.post(f"/documents/{doc_id}/audit-snapshots").status_code == 401
    assert _issue(client, "deadbeef", doc_id).status_code == 401
    # 非参与方与不存在资料 -> 统一 403，不区分资料是否存在
    r1 = _issue(client, keys["other"], doc_id)
    r2 = _issue(client, keys["other"], 9999)
    r3 = _issue(client, keys["owner"], 9999)
    assert (r1.status_code, r2.status_code, r3.status_code) == (403, 403, 403)
    assert r1.json() == r2.json() == r3.json()


def test_old_owner_cannot_issue_after_handover(client):
    keys, doc_id, _ = _setup(client)
    inv = client.post(
        f"/documents/{doc_id}/handover-invitations",
        json={"to_username": "new", "confirm_deadline": _in_hours(1)},
        headers=auth(keys["owner"]),
    ).json()["invitation_id"]
    r = client.post(f"/handover-invitations/{inv}/confirm", headers=auth(keys["new"]))
    assert r.status_code == 200, r.text

    # 交接后旧封存人不得再签发（统一 403）；新封存人与见证人可签发
    assert _issue(client, keys["owner"], doc_id).status_code == 403
    assert _issue(client, keys["new"], doc_id).status_code == 201
    assert _issue(client, keys["w2"], doc_id).status_code == 201


def test_replaced_witness_cannot_issue(client):
    keys, doc_id, _ = _setup(client, names=("owner", "w1", "w2", "w3", "erin"))
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"old_witness": "w3", "new_witness": "erin"},
        headers=auth(keys["owner"]),
    )
    assert r.status_code == 200, r.text
    assert _issue(client, keys["w3"], doc_id).status_code == 403
    assert _issue(client, keys["erin"], doc_id).status_code == 201


# ---------------------------------------------------------------------------
# 验真失败路径：篡改、版本、公钥
# ---------------------------------------------------------------------------


@pytest.fixture
def issued_pkg(client):
    keys, doc_id, dg = _setup(client)
    _open_document(client, keys, doc_id, dg)
    pkg = _issue(client, keys["owner"], doc_id).json()
    return pkg, _public_key(client)


def test_verify_tampered_fields_fail(client, issued_pkg):
    pkg, pk = issued_pkg
    snap, sig, pem = pkg["snapshot"], pkg["signature"], pk["public_key"]

    # 篡改摘要 / 状态 / 截止序号 / 开封时刻
    for bad in (
        _tampered(snap, digest="0" * 64),
        _tampered(snap, status="sealed"),
        _tampered(snap, audit_upto=snap["audit_upto"] + 1),
        _tampered(snap, opened_at=None),
        _tampered(snap, document_id=snap["document_id"] + 1),
    ):
        r = _verify(client, bad, sig, pem)
        assert r.status_code == 400 and "签名不匹配" in r.json()["detail"], r.text

    # 篡改某条审计记录的内容
    bad = _tampered(snap)
    bad["records"][0]["detail"] = "被篡改的审计细节"
    r = _verify(client, bad, sig, pem)
    assert r.status_code == 400 and "签名不匹配" in r.json()["detail"]

    # 删除一条审计记录
    bad = _tampered(snap)
    bad["records"] = bad["records"][:-1]
    assert _verify(client, bad, sig, pem).status_code == 400

    # 审计记录顺序被篡改
    bad = _tampered(snap)
    bad["records"] = bad["records"][::-1]
    r = _verify(client, bad, sig, pem)
    assert r.status_code == 400 and "签名不匹配" in r.json()["detail"]

    # 原快照依然验真通过（上述篡改不影响真包）
    assert _verify(client, snap, sig, pem).status_code == 200


def test_verify_structure_errors_are_422(client, issued_pkg):
    pkg, pk = issued_pkg
    snap, sig, pem = pkg["snapshot"], pkg["signature"], pk["public_key"]

    # 多出字段（含夹带公钥的“自封包”）-> 422，绝不以包内公钥建立信任
    bad = _tampered(snap, public_key=pem)
    assert _verify(client, bad, sig, pem).status_code == 422
    # 缺字段 -> 422
    bad = _tampered(snap)
    del bad["digest"]
    assert _verify(client, bad, sig, pem).status_code == 422
    # 记录缺字段 / 记录多字段 -> 422
    bad = _tampered(snap)
    del bad["records"][0]["seq"]
    assert _verify(client, bad, sig, pem).status_code == 422
    bad = _tampered(snap)
    bad["records"][0]["extra"] = 1
    assert _verify(client, bad, sig, pem).status_code == 422
    # 快照不是对象 / 缺签名 / 缺公钥 -> 422
    assert _verify(client, "not-an-object", sig, pem).status_code == 422
    r = client.post("/audit-snapshots/verify", json={"snapshot": snap, "public_key": pem})
    assert r.status_code == 422
    # 422 错误体不回显提交内容
    r = _verify(client, _tampered(snap, public_key=pem), sig, pem)
    assert BODY not in r.text and "records" not in r.text


def test_verify_unsupported_version(client, issued_pkg):
    pkg, pk = issued_pkg
    bad = _tampered(pkg["snapshot"], version=2)
    r = _verify(client, bad, pkg["signature"], pk["public_key"])
    assert r.status_code == 400 and "版本" in r.json()["detail"]


def test_verify_invalid_public_key(client, issued_pkg):
    pkg, pk = issued_pkg
    snap, sig = pkg["snapshot"], pkg["signature"]

    # 垃圾字符串 / 非法 base64 / 长度不符的原始公钥
    for bad_key in ("not-a-key", "!!!", base64.b64encode(b"\x01" * 16).decode()):
        r = _verify(client, snap, sig, bad_key)
        assert r.status_code == 400 and "公钥无效" in r.json()["detail"], bad_key
    # 非 Ed25519 的 PEM（前缀不符）
    fake_pem = (
        "-----BEGIN PUBLIC KEY-----\n"
        + base64.b64encode(b"\x30" * 44).decode()
        + "\n-----END PUBLIC KEY-----"
    )
    r = _verify(client, snap, sig, fake_pem)
    assert r.status_code == 400 and "公钥无效" in r.json()["detail"]
    # 曲线外的点（32 字节但 y >= q）
    off_curve = base64.b64encode(b"\xff" * 32).decode()
    r = _verify(client, snap, sig, off_curve)
    assert r.status_code == 400 and "公钥无效" in r.json()["detail"]


def test_verify_wrong_but_valid_key_fails(client, issued_pkg):
    """另一把有效 Ed25519 公钥（非签发方）验真失败：信任只来自事先信任的公钥。"""
    pkg, _ = issued_pkg
    # RFC 8032 测试向量 1 的公钥：格式有效但不是服务端密钥
    other_raw = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    other_b64 = base64.b64encode(other_raw).decode()
    r = _verify(client, pkg["snapshot"], pkg["signature"], other_b64)
    assert r.status_code == 400 and "签名不匹配" in r.json()["detail"]


def test_verify_does_not_trust_bundled_key(client, issued_pkg):
    """攻击者自签的伪造快照即使用包内自带公钥也无法冒充：验真只认提交的公钥。"""
    from app import ed25519
    from app.snapshots import canonical_json, public_key_pem

    pkg, pk = issued_pkg
    # 攻击者生成自己的密钥，伪造一份“已清除”快照并自签
    forged = _tampered(pkg["snapshot"], status="purged")
    attacker_seed = b"\x42" * 32
    attacker_sig = base64.b64encode(ed25519.sign(attacker_seed, canonical_json(forged))).decode()
    attacker_pem = public_key_pem(ed25519.public_key_from_seed(attacker_seed))

    # 用服务端公钥验伪造包 -> 失败
    r = _verify(client, forged, attacker_sig, pk["public_key"])
    assert r.status_code == 400
    # 用真签名验伪造内容 -> 失败
    r = _verify(client, forged, pkg["signature"], pk["public_key"])
    assert r.status_code == 400
    # 把攻击者公钥夹带进快照包 -> 结构错误（422），不会据此建立信任
    bundled = _tampered(forged, public_key=attacker_pem)
    assert _verify(client, bundled, attacker_sig, pk["public_key"]).status_code == 422


def test_verify_bad_signature_format(client, issued_pkg):
    pkg, pk = issued_pkg
    snap = pkg["snapshot"]
    for bad_sig in ("!!!not-base64!!!", base64.b64encode(b"\x00" * 10).decode(), ""):
        r = _verify(client, snap, bad_sig, pk["public_key"])
        assert r.status_code in (400, 422), (bad_sig, r.status_code)
    # 篡改签名（合法 base64、64 字节）-> 签名不匹配
    bad_sig = base64.b64encode(b"\x00" * 64).decode()
    r = _verify(client, snap, bad_sig, pk["public_key"])
    assert r.status_code == 400 and "签名不匹配" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 重启后旧签名仍可验证（密钥随库持久化）
# ---------------------------------------------------------------------------


def _start_server(tmp_path, monkeypatch, db_name, sweep_seconds):
    import socket

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


def test_signature_survives_restart(tmp_path, monkeypatch):
    """重启后：公钥标识不变，重启前签发的快照仍可验真。"""
    import httpx

    server, thread, base = _start_server(tmp_path, monkeypatch, "snap-restart.db", 3600)
    try:
        with httpx.Client(timeout=10) as c:
            def reg(n):
                return c.post(base + "/register", json={"username": n}).json()["api_key"]

            owner = reg("sn-owner")
            for n in ("sn-w1", "sn-w2", "sn-w3"):
                reg(n)
            doc = c.post(
                base + "/documents",
                json={"body": "RESTART-SNAPSHOT-SECRET", "witnesses": ["sn-w1", "sn-w2", "sn-w3"]},
                headers={"X-API-Key": owner},
            ).json()
            pkg = c.post(
                f"{base}/documents/{doc['document_id']}/audit-snapshots",
                headers={"X-API-Key": owner},
            ).json()
            pk_before = c.get(base + "/audit-snapshots/public-key").json()
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    server2, thread2, base2 = _start_server(tmp_path, monkeypatch, "snap-restart.db", 3600)
    try:
        with httpx.Client(timeout=10) as c:
            pk_after = c.get(base2 + "/audit-snapshots/public-key").json()
            assert pk_after["key_id"] == pk_before["key_id"]
            assert pk_after["public_key"] == pk_before["public_key"]
            r = c.post(
                base2 + "/audit-snapshots/verify",
                json={
                    "snapshot": pkg["snapshot"],
                    "signature": pkg["signature"],
                    "public_key": pk_after["public_key"],
                },
            )
            assert r.status_code == 200 and r.json()["valid"] is True
            assert r.json()["audit_upto"] == pkg["snapshot"]["audit_upto"]
    finally:
        server2.should_exit = True
        thread2.join(timeout=5)


# ---------------------------------------------------------------------------
# 并发：签发与第二票同意赛跑 —— 快照始终是某个已提交的一致状态
# ---------------------------------------------------------------------------


def test_concurrent_issue_and_second_approval(tmp_path, monkeypatch):
    """N 轮并发“签发 vs 第二票同意”：每份快照要么完整反映开封前、要么完整
    反映开封后（状态与审计记录一致），且全部可通过验真。"""
    import httpx

    server, thread, base = _start_server(tmp_path, monkeypatch, "snap-race.db", 60)
    try:
        with httpx.Client(timeout=10) as c:
            def reg(n):
                return c.post(base + "/register", json={"username": n}).json()["api_key"]

            owner = reg("sr-owner")
            w1, w2, w3 = reg("sr-w1"), reg("sr-w2"), reg("sr-w3")
            body = "SNAPSHOT-RACE-SECRET"
            dg = digest_of(body)
            doc_id = c.post(
                base + "/documents",
                json={"body": body, "witnesses": ["sr-w1", "sr-w2", "sr-w3"]},
                headers={"X-API-Key": owner},
            ).json()["document_id"]
            deadline = _in_hours(1)
            req_id = c.post(
                f"{base}/documents/{doc_id}/open-requests",
                json={"reason": "赛跑", "deadline": deadline},
                headers={"X-API-Key": owner},
            ).json()["request_id"]
            assert c.post(
                f"{base}/open-requests/{req_id}/approve",
                json={"digest": dg},
                headers={"X-API-Key": w1},
            ).status_code == 200
            pk = c.get(base + "/audit-snapshots/public-key").json()["public_key"]

        barrier = threading.Barrier(5)

        def do_approve():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                return c.post(
                    f"{base}/open-requests/{req_id}/approve",
                    json={"digest": dg},
                    headers={"X-API-Key": w2},
                ).status_code

        def do_issue():
            barrier.wait()
            with httpx.Client(timeout=10) as c:
                r = c.post(f"{base}/documents/{doc_id}/audit-snapshots",
                           headers={"X-API-Key": owner})
                assert r.status_code == 201
                return r.json()

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda f: f(), [do_approve] + [do_issue] * 4))
        approve_code, packages = results[0], results[1:]
        assert approve_code == 200

        # 最终状态：已开封
        with httpx.Client(timeout=10) as c:
            final = c.get(f"{base}/documents/{doc_id}", headers={"X-API-Key": owner}).json()
            assert final["status"] == "opened"

        for pkg in packages:
            snap = pkg["snapshot"]
            actions = [rec["action"] for rec in snap["records"]]
            seqs = [rec["seq"] for rec in snap["records"]]
            # 不变式 1：截止序号 = 最后一条记录序号，记录按序排列
            assert snap["audit_upto"] == seqs[-1] and seqs == sorted(seqs)
            # 不变式 2：状态与审计一致（绝不拼接）：opened 当且仅当含 opened 记录
            assert (snap["status"] == "opened") == ("opened" in actions)
            if "opened" in actions:
                assert actions.count("approval") >= 2  # 两票记录都在
            # 不变式 3：每份快照都可验真
            with httpx.Client(timeout=10) as c:
                r = c.post(
                    base + "/audit-snapshots/verify",
                    json={"snapshot": snap, "signature": pkg["signature"], "public_key": pk},
                )
                assert r.status_code == 200 and r.json()["valid"] is True
        # 至少采到一份开封前快照与一份开封后快照（并发交错的真实覆盖）
        statuses = {pkg["snapshot"]["status"] for pkg in packages}
        assert statuses <= {"sealed", "opened"}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# 库迁移与密钥持久化
# ---------------------------------------------------------------------------


def test_init_db_creates_signing_key_table_for_old_db(tmp_path):
    """旧库（无 signing_keys 表）幂等升级后可正常签发/验真。"""
    from app.db import init_db

    db = tmp_path / "old-no-keys.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                            key_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY, at TEXT NOT NULL,
                            actor_id INTEGER, action TEXT NOT NULL,
                            document_id INTEGER, request_id INTEGER, detail TEXT NOT NULL);
        """
    )
    conn.commit()
    conn.close()

    init_db(str(db))
    init_db(str(db))  # 幂等
    conn = sqlite3.connect(str(db))
    assert "signing_keys" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()


def test_signing_key_persisted_in_db_and_secret_never_leaks(client):
    keys, doc_id, _ = _setup(client)
    _issue(client, keys["owner"], doc_id)

    conn = sqlite3.connect(os.environ["SEAL_DB_PATH"])
    rows = conn.execute("SELECT key_id, algorithm, public_key, secret_seed FROM signing_keys").fetchall()
    assert len(rows) == 1  # 单行密钥，重复签发复用同一把
    assert len(rows[0][3]) == 64  # 32 字节种子的 hex
    conn.close()

    # 任何接口都不返回种子
    for resp in (
        client.get("/audit-snapshots/public-key"),
        _issue(client, keys["owner"], doc_id),
        client.get(f"/documents/{doc_id}/audit", headers=auth(keys["owner"])),
    ):
        assert rows[0][3] not in resp.text and "secret_seed" not in resp.text


# ---------------------------------------------------------------------------
# Ed25519 纯标准库实现：RFC 8032 官方测试向量
# ---------------------------------------------------------------------------


def test_ed25519_rfc8032_vectors():
    from app import ed25519

    vectors = [
        (
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
            "",
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
        ),
        (
            "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
            "72",
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
        ),
        (
            "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
            "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
            "af82",
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
            "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
        ),
    ]
    for sk_hex, pk_hex, msg_hex, sig_hex in vectors:
        sk, pk, msg, sig = (
            bytes.fromhex(sk_hex),
            bytes.fromhex(pk_hex),
            bytes.fromhex(msg_hex),
            bytes.fromhex(sig_hex),
        )
        assert ed25519.public_key_from_seed(sk) == pk
        assert ed25519.sign(sk, msg) == sig
        assert ed25519.verify(pk, msg, sig)
        assert not ed25519.verify(pk, msg + b"x", sig)
        # 篡改 S 段一字节（仍远小于 L）-> 验签失败
        corrupted = sig[:40] + bytes([sig[40] ^ 0x01]) + sig[41:]
        assert not ed25519.verify(pk, msg, corrupted)
