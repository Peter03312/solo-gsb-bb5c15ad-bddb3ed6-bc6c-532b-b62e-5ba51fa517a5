#!/usr/bin/env python3
"""账号停用示例（纯标准库，无需第三方依赖）。

用法: python examples/demo_account_deactivation.py [base_url]

覆盖：
- 有未了义务时申请停用 -> 409，逐项返回义务类型/对象编号/原因，账号保持可用；
- 核对前先按现有规则到点结算（示例用“到期清除”解除封存人/见证人义务），
  结算即使伴随拒绝也已落库；
- 无未了义务 -> 200 停用成功，返回停用时刻；
- 停用后当前密钥立即且永久失效：/me 与业务接口沿用现有无效密钥 401；
- 同名不可重新注册（沿用注册既有 409）；
- 停用账号不能再被指定为见证人（沿用现有“见证人尚未注册”400，不暴露停用）。
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


def iso(hours: float = 1.0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat()


def main() -> None:
    suffix = str(int(time.time()))
    names = {
        "owner": f"alice-deact-{suffix}",
        "w1": f"bob-deact-{suffix}",
        "w2": f"carol-deact-{suffix}",
        "w3": f"dave-deact-{suffix}",
        "lone": f"erin-deact-{suffix}",
        "other": f"frank-deact-{suffix}",
    }
    keys = {}
    for role, name in names.items():
        _, body = call("POST", "/register", payload={"username": name})
        keys[role] = body["api_key"]
    print("registered:", list(names.values()))

    # 1. 封存一份资料（owner 为封存人，w1/w2/w3 为见证人）
    _, doc = call("POST", "/documents", key=keys["owner"],
                  payload={"body": "停用示例正文",
                           "witnesses": [names["w1"], names["w2"], names["w3"]]})
    doc_id = doc["document_id"]
    print("sealed document:", doc_id, "digest:", doc["digest"][:12], "…")

    # 2. 封存人申请停用：仍有未清除资料义务 -> 409，逐项返回义务编号与原因
    status, body = call("POST", "/me/deactivation", key=keys["owner"])
    print("\nowner deactivate while obligated ->", status)
    assert status == 409, body
    print(json.dumps(body, ensure_ascii=False, indent=2))
    # 账号保持可用
    assert call("GET", "/me", key=keys["owner"])[0] == 200

    # 见证人也有未开封见证义务
    status, body = call("POST", "/me/deactivation", key=keys["w1"])
    assert status == 409 and body["detail"]["obligations"][0]["type"] == "document_witness"
    print("witness deactivate ->", status, body["detail"]["obligations"][0]["type"])

    # 3. 安排 1 秒后到期清除：停用核对会先做“到点结算”（清除），义务随之解除
    purge_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
    assert call("PUT", f"/documents/{doc_id}/purge", key=keys["owner"],
                payload={"purge_at": purge_at})[0] == 200
    time.sleep(1.2)

    # 4. 无未了义务的账号可直接停用
    status, body = call("POST", "/me/deactivation", key=keys["lone"])
    print("\nlone user deactivate ->", status, body)
    assert status == 200 and body["status"] == "deactivated"

    # 5. owner / w1 的义务随到点清除解除：停用申请在锁内先结算清除再核对
    for role in ("owner", "w1", "w2", "w3"):
        status, body = call("POST", "/me/deactivation", key=keys[role])
        assert status == 200, (role, body)
        print(f"{role} deactivated at {body['deactivated_at']}")

    # 6. 停用后当前密钥立即永久失效：与“无效密钥”完全相同的 401
    print("\nGET /me with deactivated key ->", call("GET", "/me", key=keys["owner"]))
    print("seal with deactivated key ->",
          call("POST", "/documents", key=keys["owner"],
               payload={"body": "x",
                        "witnesses": [names["w1"], names["w2"], names["w3"]]}))

    # 7. 同名不可重新注册：沿用注册既有 409
    print("reregister same username ->",
          call("POST", "/register", payload={"username": names["lone"]}))

    # 8. 其他账号不能再把停用账号指定为见证人：沿用现有“尚未注册”400
    status, body = call("POST", "/documents", key=keys["other"],
                        payload={"body": "x",
                                 "witnesses": [names["w1"], names["w2"], names["w3"]]})
    print("seal naming deactivated witness ->", status, body)
    assert status == 400

    print("\ndemo complete.")


if __name__ == "__main__":
    main()
