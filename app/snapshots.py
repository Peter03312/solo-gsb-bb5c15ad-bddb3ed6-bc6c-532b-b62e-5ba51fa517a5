"""审计快照的签发与离线验真。

参与方（当前封存人或当前指定见证人）可为某份资料签发一份**审计快照**：
资料编号、摘要、当前状态与开封/清除时刻、按原顺序排列且标明截止序号
（audit_upto）的审计记录——不含正文，也不含任何密钥。快照经服务端持有的
Ed25519 密钥签名后交付；复核人事先通过可信渠道获得公钥，凭
“快照 + 签名 + 事先信任的公钥”离线验真。

要点：
- 规范化序列化（canonical-json-v1）：sort_keys + 紧凑分隔符 + ASCII 转义，
  同一快照对象在任何环境都序列化为同一字节串，签名才可被独立重放验证；
  审计记录是数组，顺序不同则字节不同，记录顺序被篡改必然验签失败；
- 签名密钥持久化在 signing_keys 表（随库备份/重启保留），重启后旧签名
  仍可验证；公钥标识 key_id 为 SPKI DER 的 SHA-256 前 16 位十六进制；
- 验真只使用调用方提交的公钥，绝不使用快照包内夹带的任何公钥建立信任。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import sqlite3
from typing import Any

from . import ed25519
from .db import now_utc, utc_str

SNAPSHOT_VERSION = 1
ALGORITHM = "ed25519"
CANONICALIZATION = "canonical-json-v1"

# Ed25519 SubjectPublicKeyInfo 的 DER 前缀（RFC 8410）：12 字节头 + 32 字节原始公钥
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def canonical_json(obj: Any) -> bytes:
    """canonical-json-v1：键排序、紧凑分隔符、非 ASCII 一律 \\uXXXX 转义。

    等价于 json.dumps(obj, ensure_ascii=True, sort_keys=True,
    separators=(",", ":")) 的 ASCII 字节串；纯 ASCII 输出对任何输入
    （含孤立代理项等异常字符）都可编码，杜绝序列化歧义。
    """
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


# ---------------------------------------------------------------------------
# 公钥编码：PEM（SPKI）<-> 32 字节原始公钥；key_id 为公钥指纹
# ---------------------------------------------------------------------------


def public_key_pem(raw: bytes) -> str:
    if len(raw) != 32:
        raise ValueError("原始公钥必须为 32 字节")
    b64 = base64.b64encode(_SPKI_PREFIX + raw).decode("ascii")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----"


def parse_public_key(text: str) -> bytes:
    """把 PEM（Ed25519 SPKI）或 base64 编码的 32 字节原始公钥解析为原始公钥。

    任何格式问题（无法解码、长度/前缀不符、点不在曲线上）都抛 ValueError，
    由调用方统一映射为“公钥无效”。
    """
    if not isinstance(text, str):
        raise ValueError("公钥必须为字符串")
    s = text.strip()
    if "-----BEGIN" in s:
        body: list[str] = []
        in_block = False
        for line in s.splitlines():
            line = line.strip()
            if line == "-----BEGIN PUBLIC KEY-----":
                in_block = True
            elif line == "-----END PUBLIC KEY-----":
                break
            elif in_block:
                body.append(line)
        try:
            der = base64.b64decode("".join(body), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("PEM 公钥无法解码") from exc
        if len(der) != len(_SPKI_PREFIX) + 32 or not der.startswith(_SPKI_PREFIX):
            raise ValueError("不是 Ed25519 SPKI 公钥")
        raw = der[len(_SPKI_PREFIX) :]
    else:
        try:
            raw = base64.b64decode(s, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("公钥既不是 PEM 也不是 base64 原始公钥") from exc
        if len(raw) != 32:
            raise ValueError("原始公钥必须为 32 字节")
    # 必须是曲线上的有效点，否则属于“公钥无效”而非“签名不匹配”
    ed25519._decode_point(raw)
    return raw


def key_id_from_raw(raw: bytes) -> str:
    """公钥标识：SPKI DER 的 SHA-256 前 16 位十六进制。"""
    return hashlib.sha256(_SPKI_PREFIX + raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 签名密钥：持久化在 signing_keys 表，重启后旧签名仍可验证
# ---------------------------------------------------------------------------


def load_or_create_signing_key(tx: sqlite3.Connection) -> sqlite3.Row:
    """在已持有的写事务内取签名密钥；不存在则生成并落库（幂等）。"""
    row = tx.execute("SELECT * FROM signing_keys ORDER BY id LIMIT 1").fetchone()
    if row is not None:
        return row
    seed = secrets.token_bytes(32)
    raw = ed25519.public_key_from_seed(seed)
    tx.execute(
        "INSERT INTO signing_keys (key_id, algorithm, public_key, secret_seed, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (key_id_from_raw(raw), ALGORITHM, public_key_pem(raw), seed.hex(), utc_str(now_utc())),
    )
    return tx.execute("SELECT * FROM signing_keys ORDER BY id LIMIT 1").fetchone()


def public_key_info(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "algorithm": row["algorithm"],
        "key_id": row["key_id"],
        "public_key": row["public_key"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# 快照组装与签名
# ---------------------------------------------------------------------------


def build_snapshot(tx: sqlite3.Connection, doc: sqlite3.Row) -> dict[str, Any]:
    """在已持有的事务内组装快照：文档元数据 + 按原顺序排列的审计记录。

    调用方必须先在同一事务内完成到点开封/到期清除等确定性结算，
    使快照对应一个已提交的一致状态；快照不含正文，也不含任何密钥。
    """
    rows = tx.execute(
        """SELECT a.id, a.at, a.action, a.request_id, a.detail,
                  u.username AS actor
           FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id
           WHERE a.document_id = ?
           ORDER BY a.id""",
        (doc["id"],),
    ).fetchall()
    records = [
        {
            "seq": r["id"],
            "at": r["at"],
            "action": r["action"],
            "actor": r["actor"],
            "request_id": r["request_id"],
            "detail": r["detail"],
        }
        for r in rows
    ]
    return {
        "version": SNAPSHOT_VERSION,
        "document_id": doc["id"],
        "digest": doc["digest"],
        "status": doc["status"],
        "created_at": doc["created_at"],
        "opened_at": doc["opened_at"],
        "purge_at": doc["purge_at"],
        "purged_at": doc["purged_at"],
        # 截止序号：快照所包含的最后一条审计记录的全局序号
        "audit_upto": records[-1]["seq"] if records else 0,
        "records": records,
    }


def sign_snapshot(key_row: sqlite3.Row, snapshot: dict[str, Any]) -> str:
    """对快照的规范化字节串签名，返回 base64 编码的 64 字节签名。"""
    seed = bytes.fromhex(key_row["secret_seed"])
    return base64.b64encode(ed25519.sign(seed, canonical_json(snapshot))).decode("ascii")
