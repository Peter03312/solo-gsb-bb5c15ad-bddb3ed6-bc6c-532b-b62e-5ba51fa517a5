#!/usr/bin/env python3
"""API Key 轮换示例（纯标准库，无需第三方依赖）。

用法: python examples/demo_api_key_rotation.py [base_url]

覆盖：
- 凭当前 X-API-Key 轮换：新密钥明文只返回一次，旧密钥即刻失效；
- 旧密钥在轮换提交后任何已认证操作都返回 401（与“无效密钥”同文案，不区分账号存在性）；
- 新密钥业务照常；再次轮换使上一轮新密钥失效；
- 未轮换账号与无凭据接口（健康检查/快照公钥/阅览兑换）完全不受影响。
"""
import json
import sys
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


def main() -> None:
    # 1. 注册两个账号；alice 持有初始密钥
    _, alice = call("POST", "/register", payload={"username": "alice-rot"})
    _, bob = call("POST", "/register", payload={"username": "bob-plain"})
    old_key = alice["api_key"]
    print("registered alice; old key tail:", old_key[-8:])

    # 2. 轮换：响应只在这一次给出新密钥明文，服务端只保存其摘要
    status, body = call("POST", "/me/api-key/rotate", key=old_key)
    assert status == 200, body
    new_key = body["api_key"]
    print("rotated:", {k: (v[-8:] if k == "api_key" else v) for k, v in body.items()})
    assert len(new_key) == 64 and new_key != old_key

    # 3. 旧密钥立即失效：查询与业务写都沿用现有 401，文案与“无效密钥”完全一致
    print("GET /me with old key:", call("GET", "/me", key=old_key))
    print("GET /me with bogus key:", call("GET", "/me", key="0" * 64))

    # 4. 新密钥立即可用，业务照常
    print("GET /me with new key:", call("GET", "/me", key=new_key))

    # 5. 再次轮换：上一轮新密钥随之失效，只有最新密钥可用
    status, body2 = call("POST", "/me/api-key/rotate", key=new_key)
    assert status == 200, body2
    newest_key = body2["api_key"]
    print("second rotate ok; previous new key ->", call("GET", "/me", key=new_key)[0])
    print("latest key /me ->", call("GET", "/me", key=newest_key)[0])

    # 6. 未轮换账号不受任何影响
    print("unrotated bob /me ->", call("GET", "/me", key=bob["api_key"])[0])

    # 7. 无需登录的接口保持原样（兑换无效凭证仍是统一 404）
    print("health:", call("GET", "/health"))
    print("snapshot public key:", call("GET", "/audit-snapshots/public-key")[0])
    print("redeem bogus token:", call("POST", "/view-passes/redeem", payload={"token": "x"}))


if __name__ == "__main__":
    main()
