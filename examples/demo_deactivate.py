#!/usr/bin/env python3
"""账号停用示例（纯标准库，无需第三方依赖）。

用法: python examples/demo_deactivate.py [base_url]

覆盖：
- 无未了义务：凭当前 X-API-Key 停用，返回停用时刻，密钥立即永久失效（401），
  同名不可重注册（409）；
- 有未了义务：先完成到点结算（开封/清除/逾期），再拒绝停用并返回本人义务
  编号与原因，账号保持可用，到点结算已落库；
  · 未清除资料的封存人 / 未开封资料的当前见证人
  · 待确认预封存的发起人/见证人
  · 待确认交接的受让人
- 已开封（未清除）资料的见证人无义务，可停用；
- 停用账号不能再被指定为见证人或交接受让人（沿用“未注册”错误）。
"""
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SUFFIX = str(int(time.time()))  # 允许重复运行示例：用户名加时间戳后缀


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


def reg(name: str) -> str:
    status, body = call("POST", "/register", payload={"username": f"{name}-{SUFFIX}"})
    assert status == 201, body
    return body["api_key"]


def digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def iso(hours: float = 24.0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)).isoformat()


def show_obligations(label: str, code: int, body: dict) -> list:
    assert code == 409, (label, code, body)
    obs = body["detail"]["obligations"]
    print(f"{label} -> 409 {body['detail']['message']}")
    for o in obs:
        ref = o.get("document_id") or o.get("proposal_id") or o.get("invitation_id")
        print(f"   · {o['type']} #{ref}: {o['reason']}")
    return obs


def main() -> None:
    # 1) 干净账号直接停用
    clean = reg("clean")
    code, body = call("POST", "/me/deactivate", key=clean)
    assert code == 200, body
    print("clean deactivation:", body)
    print("old key after deactivation:", call("GET", "/me", key=clean))  # 401
    code, _ = call("POST", "/register", payload={"username": f"clean-{SUFFIX}"})
    print("reregister same name ->", code)  # 409

    # 2) 未清除资料的封存人 / 未开封资料的见证人：拒绝停用
    owner = reg("owner")
    w1, w2, w3 = reg("w1"), reg("w2"), reg("w3")
    code, doc = call("POST", "/documents", key=owner,
                     payload={"body": "重要遗嘱", "witnesses": [f"w{i}-{SUFFIX}" for i in (1, 2, 3)]})
    assert code == 201, doc
    show_obligations("owner (未清除封存人)", *call("POST", "/me/deactivate", key=owner))
    show_obligations("witness (未开封见证人)", *call("POST", "/me/deactivate", key=w1))
    print("owner /me still usable:", call("GET", "/me", key=owner)[0])  # 200

    # 3) 两票开封：已开封资料的见证人不再有见证义务（封存人仍有 owner 义务）
    code, req = call("POST", f"/documents/{doc['document_id']}/open-requests", key=owner,
                     payload={"reason": "家庭会议", "deadline": iso(24)})
    req_id = req["request_id"]
    dg = doc["digest"]
    call("POST", f"/open-requests/{req_id}/approve", key=w1, payload={"digest": dg})
    code, opened = call("POST", f"/open-requests/{req_id}/approve", key=w2, payload={"digest": dg})
    assert code == 200 and opened["status"] == "opened", opened
    print("opened witness deactivate:", call("POST", "/me/deactivate", key=w1)[0])  # 200

    # 4) 待确认交接的受让人：有义务；撤回后可停用（交接仅针对未开封资料，
    #    另建一份）
    h_owner = reg("howner")
    new_owner = reg("newowner")
    for n in ("hw1", "hw2", "hw3"):
        reg(n)
    code, hdoc = call("POST", "/documents", key=h_owner,
                      payload={"body": "待交接资料",
                               "witnesses": [f"hw{i}-{SUFFIX}" for i in (1, 2, 3)]})
    assert code == 201, hdoc
    code, inv = call("POST", f"/documents/{hdoc['document_id']}/handover-invitations", key=h_owner,
                     payload={"to_username": f"newowner-{SUFFIX}", "confirm_deadline": iso(24)})
    assert code == 201, inv
    show_obligations("assignee (待确认受让人)", *call("POST", "/me/deactivate", key=new_owner))
    call("POST", f"/handover-invitations/{inv['invitation_id']}/revoke", key=h_owner)
    print("assignee after revoke:", call("POST", "/me/deactivate", key=new_owner)[0])  # 200

    # 5) 待确认预封存：发起人/见证人义务
    prop_owner = reg("proposer")
    pw1 = reg("pw1")
    for n in ("pw2", "pw3"):
        reg(n)
    code, prop = call("POST", "/pre-seal-proposals", key=prop_owner, payload={
        "body": "预存正文",
        "witnesses": [f"pw{i}-{SUFFIX}" for i in (1, 2, 3)],
        "confirm_deadline": iso(24),
    })
    assert code == 201, prop
    show_obligations("pre-seal proposer", *call("POST", "/me/deactivate", key=prop_owner))
    show_obligations("pre-seal witness", *call("POST", "/me/deactivate", key=pw1))
    call("POST", f"/pre-seal-proposals/{prop['proposal_id']}/revoke", key=prop_owner)
    print("proposer after revoke:", call("POST", "/me/deactivate", key=prop_owner)[0])  # 200
    print("pre-seal witness after revoke:", call("POST", "/me/deactivate", key=pw1)[0])  # 200

    # 6) 到点结算先落库：安排 1 秒后清除，停用事务自身结算，清除后封存人可停用
    purge_owner = reg("purgeowner")
    for n in ("q1", "q2", "q3"):
        reg(n)
    code, d2 = call("POST", "/documents", key=purge_owner,
                    payload={"body": "即将清除", "witnesses": [f"q{i}-{SUFFIX}" for i in (1, 2, 3)]})
    purge_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)).isoformat()
    call("PUT", f"/documents/{d2['document_id']}/purge", key=purge_owner, payload={"purge_at": purge_at})
    time.sleep(1.2)
    code, body = call("POST", "/me/deactivate", key=purge_owner)
    assert code == 200, body
    print("owner after due purge settled in deactivation:", body["status"], body["deactivated_at"])

    # 7) 停用账号不能再被指定为见证人 / 受让人（沿用“未注册”错误）
    someone = reg("someone")
    gone = reg("gone")
    call("POST", "/me/deactivate", key=gone)
    code, body = call("POST", "/documents", key=someone,
                      payload={"body": "x",
                               "witnesses": [f"gone-{SUFFIX}", f"q1-{SUFFIX}", f"q2-{SUFFIX}"]})
    print("seal with deactivated witness ->", code, body)  # 400 见证人尚未注册
    print("done.")


if __name__ == "__main__":
    main()
