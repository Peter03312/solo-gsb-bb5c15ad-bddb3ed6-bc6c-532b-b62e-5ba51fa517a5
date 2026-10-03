"""双人见证资料封存服务。

关键规则：
- 本地账号注册后凭 API Key（X-API-Key）操作，服务端只存密钥哈希；
- 封存后正文不可改；开封前任何查询与错误响应都不返回正文；
- 开封请求需两名不同指定见证人在截止时刻前分别同意，同一人重试只计一次；
- 同意必须绑定本次请求与封存时的摘要（客户端提交摘要，服务端比对）；
- 开封请求可选带时区的 earliest_open_at（最早开封时刻）：
  未填写时两票集齐即开封；填写后两票在该时刻前集齐只进入 waiting（待开封），
  到点自动开封；到点时不足两票的，截止前补齐第二票时立即开封；
- 撤销或到期后旧同意永久失效，不能用于后续请求；
- 已 waiting（未撤销、两票集齐）的请求到点必开封：后台扫描器与每个写事务
  都会先做确定性的“到点开封”判定，即使处理延迟，之后的撤销或到期扫描也无法使其失效；
- 资料未开封时，封存人可替换一名指定见证人：新见证人须已注册、非封存人、
  且不在当前名单（替换后仍是三名不同见证人）；有进行中的开封请求时，
  被替换者对本次请求的同意立即失效、其他同意保留，有效票不足两票则请求
  退回待同意（waiting -> pending），新见证人可凭封存摘要补票；已到最早
  开封时刻且两票已齐的请求先开封并拒绝替换；已过截止时刻的请求按原规则失效；
- 封存人可就未开封资料向另一名本地账号发起交接邀请：受让人须已注册、不是
  封存人本人也不是当前见证人；确认截止时刻带时区；每份资料同时至多一份
  待确认（pending）且未过期邀请（部分唯一索引兜底）；封存人可撤回待确认邀请；
  受让人须在截止前凭本人 X-API-Key 确认，已撤回/逾期/已确认均不可确认，
  逾期或撤回后封存人可重新邀请；
- 确认交接时先结算“到点开封 + 请求到期”：资料若已开封则拒绝交接；否则把
  进行中的开封请求置为 invalidated 并删除其全部旧同意（旧同意永久失效），
  再把封存人身份转移给受让人；见证名单、正文与摘要均不变；原封存人随即
  失去该资料的查看与操作权限，新封存人可重新申请开封；
- 交接邀请的发起/撤回/确认与开封请求的同意/撤销/替换/定时处理共用同一把
  写锁，全部按 BEGIN IMMEDIATE 已提交顺序产生唯一结果；成功交接与请求失效
  均追加审计；后台扫描器同时处理开封请求到期与交接邀请逾期；
- 封存人可为资料安排带时区的未来清除时刻（到期清除正文），到期前可改期或
  取消；交接后清除安排保留并改由新封存人管理，原封存人无权操作；
- 到期时无论扫描器是否准时运行，后续查询与操作都呈现已清除结果：扫描器、
  每个写事务与资料查询都会先做确定性到点清除判定——清空正文（body），
  状态置 purged，保留摘要、参与方与审计元数据，任何接口不再返回正文；
  已开封资料仍按期清除；未开封资料的进行中开封请求失效（旧同意作废）、
  待确认交接邀请失效；清除与最早开封同刻时清除优先，开封更早则先开封；
  已清除资料不能再开封、替换见证人、交接或再安排清除；已到期的改期或取消
  一律拒绝且不泄露正文；
- 所有“检查 + 写入”都在 BEGIN IMMEDIATE 短事务内完成，并发同意/撤销/定时开封/
  到期/替换见证人/交接确认/到期清除只有一个已提交结果；
- 当前封存人或当前指定见证人可在到点开封、清除等结算完成后签发**审计快照**：
  资料编号、摘要、当前状态与开封/清除时刻、按原顺序排列且标明截止序号的
  审计记录（不含正文与密钥），由服务端持久化的 Ed25519 密钥签名；
  复核人凭“快照 + 签名 + 事先信任的公钥”离线验真，字段或记录顺序被篡改、
  签名不匹配、版本不支持或公钥无效均明确失败，且不以包内自带公钥建立信任；
  签发在同一写事务内先完成确定性结算再取数，快照始终对应一个已提交的一致状态；
- 当前封存人可就**已开封**资料凭资料编号与带时区阅览截止时刻签发**一次性阅览
  凭证**给无账号外部复核人：截止时刻须晚于当前且严格早于已安排的清除时刻
  （未安排清除不能签发）；凭证明文为不可猜测的随机串，库内只存 SHA-256 摘要，
  明文仅在签发响应中返回一次，响应同时给出供撤销使用的凭证编号；
- 外部复核人无需账号，凭明文在截止前可且仅可读取一次正文及摘要：兑换临界区
  先结算到期清除（清除时刻与兑换同刻以清除优先，拒绝兑换且不返回正文），
  再以“条件更新 + 行写锁”把 active 凭证原子置为 redeemed，并发兑换至多一次
  成功；过期、撤销、已兑付、无效凭证或已清除资料统一 404，不泄露正文或资料
  存在性，且拒绝路径不写任何记录；
- 当前封存人凭凭证编号可在兑换前撤销（资料到清除时刻或被改期清除的同样拒绝）；
  签发、成功兑换、撤销均按顺序追加到原有 audit_log 审计链；
- 账号持有人可凭当前 X-API-Key 轮换密钥：成功响应只返回一次新密钥明文，
  持久层只保存新密钥的 SHA-256 摘要（与注册一致），旧密钥自轮换提交后立即
  失效并随 WAL 持久化（重启后仍然失效）。轮换与封存、见证同意、交接、清除
  安排等已认证写操作并发时，统一按 BEGIN IMMEDIATE 已提交先后判定：轮换先
  提交，则持旧密钥的业务写入在其写锁内复核密钥时按既有 401 行为拒绝并随事务
  回滚——既不生效，也不留下业务审计；业务写入先提交则照常完成，之后旧密钥
  失效。无效或已失效密钥一律沿用现有 401（不区分账号是否存在，不留任何记录）；
  未轮换账号以及无需登录的阅览兑换、快照验真行为完全保持原样。
- 封存人可发起**需见证人确认的预封存**：提交正文、三名互异且非本人的已注册
  见证人及带时区的未来确认截止时刻，取得提案编号与正文摘要（响应不含正文）。
  仅发起人本人与受邀见证人可凭编号查看提案的摘要、截止时刻、状态与确认人数，
  其他账号（含凭编号试探）统一 403，不区分提案是否存在。见证人凭摘要各确认
  一次：摘要不符、重复确认一律拒绝且不计票（UNIQUE 兜底）。三人在截止前
  确认后，在第三票的同一写事务内按“现有封存规则”生成一份普通封存资料并返回
  资料编号（seal 审计与直接封存完全一致）；已生成资料的提案重复请求不得再
  生成资料。发起人可在完成前撤回；截止时未集齐三人即失效；撤回、失效或
  完成后提案正文立即清空。确认、撤回、到期结算与密钥轮换并发时按 BEGIN
  IMMEDIATE 已提交顺序产生唯一结果（轮换先提交则持旧密钥的确认/撤回 401
  回滚，不产生资料或封存审计）；后台扫描器与每个提案写事务/提案查询都做
  确定性到期判定，停机期间到期在重启恢复扫描时补结算。
  发起/确认/撤回的截止判定以取得写锁后的实际时刻为准：等待写锁期间越过
  截止时刻的，发起沿用 400 且不落提案，确认/撤回先使提案失效并清空正文
  再沿用 409，不计新票、不生成资料或封存审计。
- 账号持有人可凭当前 X-API-Key 申请停用账号（POST /me/deactivation）。停用
  临界区先按现有规则做一轮全局到点结算（到点开封、到期清除、开封请求到期、
  交接邀请逾期、预封存提案逾期），结算随事务提交落库——即使随后拒绝停用，
  本次到点结算仍已落库。结算后若本人仍有未了义务（任一未清除资料的封存人、
  任一未开封资料的当前见证人、任一待确认预封存的发起人/受邀见证人、任一待
  确认交接的受让人），拒绝停用（409）并返回每项义务的类型、对象编号与原因，
  账号保持可用；义务全部结清则账号立即停用：当前密钥永久失效（key_hash 覆
  盖为不可匹配值并置 status=deactivated），之后该密钥的一切访问沿用现有
  无效密钥 401（不区分账号是否存在），同名不可重新注册（用户名唯一约束保留，
  沿用 409）；历史审计、通知与参与记录全部保留（停用只追加一条 account_deactivated
  审计，不删除任何行）。停用后其他账号不得再把该账号指定为见证人、交接受让人
  或预封存受邀见证人——按现有“无效参与人”错误处理（封存/预封存 400 “尚未
  注册”、替换新见证人 400 “尚未注册”、交接受让人 400 “尚未注册”，文案与
  未注册一致，不暴露停用状态）。停用与封存、替换、交接、预封存、密钥轮换
  并发时全部按 BEGIN IMMEDIATE 写事务提交顺序裁定：停用先提交则并发角色
  指定在锁内复查到该账号已停用而按上述既有错误拒绝回滚，并发业务写持有的
  旧密钥复核失败按既有 401 回滚；业务写先提交则新增角色可能构成义务，停用
  随后在同一串行顺序下被拒绝；不存在“停用后新增角色”或部分停用。
- 每次操作按顺序追加到 audit_log，SQLite 持久化，重启后状态一致。
- 站内通知：资料开封（opened）或到期清除（purged）时，在状态变更的同一写
  事务内向“当时封存人 + 三名见证人”各记一条；见证人替换（witness_replaced）
  时通知封存人和新旧见证人；交接确认（handover_confirmed）时通知新旧封存人
  及三名见证人。收件人在状态变更的同一事务内按当时名单固定，事件序号
  （event_seq）取该状态变更主审计记录的 audit_log.id，通知按事件序号排列，
  内容只含资料编号、事件类型、事件时刻与已读状态，不含正文、凭证或密钥。
  账号凭现有 X-API-Key 以非负事件序号游标和 1～100 页大小读取本人通知及下一
  游标，并按通知编号标记已读（幂等，返回已读状态）；非法游标/页大小拒绝
  （422），他人或不存在的通知编号统一 403 且不改状态。失去资料权限的人仍可
  读自己收到的通知，但不能借此访问资料（通知不含正文/凭证/密钥）。失败或
  回滚的操作不留通知；扫描器与请求并发结算同一变化不重复投递（通知与状态
  变更同事务 + UNIQUE(recipient_id, event_seq) 兜底），已读重试不产生额外记录。
"""
import asyncio
import base64
import binascii
import hashlib
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Iterator

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import ed25519, snapshots
from .config import settings
from .db import connect, init_db, now_utc, utc_str, write_tx
from .schemas import (
    ApproveIn,
    AuditSnapshotIn,
    HandoverIn,
    OpenRequestIn,
    PreSealConfirmIn,
    PreSealIn,
    PurgeIn,
    RegisterIn,
    ReplaceWitnessIn,
    RevokeIn,
    SealIn,
    VerifySnapshotIn,
    ViewPassIn,
    ViewPassRedeemIn,
)

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuthUser:
    """已认证账号：除 users 行字段外保留请求提交的 X-API-Key 明文。

    明文只用于“写锁内二次复核”：进入已认证写事务后在 BEGIN IMMEDIATE 临界区
    内重新比对当前 key_hash，判定该密钥是否已被并发提交的轮换作废；
    绝不写库、不进审计、不随任何响应返回。行访问方式（["col"]、keys()）与
    sqlite3.Row 完全一致，调用方无需改动。
    """

    def __init__(self, row: sqlite3.Row, presented_key: str) -> None:
        self._row = row
        self.presented_key = presented_key

    def __getitem__(self, key: str) -> Any:
        return self._row[key]

    def keys(self) -> list[str]:
        return self._row.keys()

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self._row[key]
        except (IndexError, KeyError):
            return default


def add_audit(
    conn: sqlite3.Connection,
    *,
    actor_id: int | None,
    action: str,
    detail: str,
    document_id: int | None = None,
    request_id: int | None = None,
) -> int:
    """追加一条审计记录，返回其自增序号（即事件在审计链上的全局序号）。

    通知的事件序号（event_seq）即取状态变更主审计记录的序号，保证通知与
    审计链上的事件一一对应、按同一全局顺序排列。
    """
    cur = conn.execute(
        """INSERT INTO audit_log (at, actor_id, action, document_id, request_id, detail)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (utc_str(now_utc()), actor_id, action, document_id, request_id, detail),
    )
    return int(cur.lastrowid)


def participant_ids(tx: sqlite3.Connection, doc_id: int, owner_id: int) -> set[int]:
    """取某资料“当时的封存人 + 三名指定见证人”的账号编号集合。

    在已持有的写事务内按当前名单计算：开封/清除在其结算时刻取名单，
    替换在名单更新后取（封存人 + 新旧见证人并集），交接确认在转移
    owner 后取（新旧封存人 + 三名见证人并集）。
    """
    ids = {owner_id}
    ids.update(
        row["user_id"]
        for row in tx.execute(
            "SELECT user_id FROM document_witnesses WHERE document_id = ?",
            (doc_id,),
        ).fetchall()
    )
    return ids


def fanout_notifications(
    tx: sqlite3.Connection,
    *,
    recipients: set[int],
    document_id: int,
    event_seq: int,
    event_type: str,
    at: str,
) -> None:
    """在状态变更的同一写事务内，给每名收件人各写一条通知。

    - 收件人在调用前已按状态变更时刻的封存人/见证人名单在事务内固定；
    - event_seq 取该状态变更主审计记录的 audit_log.id，通知按事件序号排列；
    - 内容只含资料编号、事件类型、事件时刻与已读状态，绝不含正文、凭证或密钥；
    - 与状态变更同事务：变更回滚则通知一并回滚，不留通知；变更已提交则
      通知必已写入，扫描器与请求并发结算同一变化时唯一约束兜底不重复投递。
    """
    now = utc_str(now_utc())
    for recipient_id in sorted(recipients):
        tx.execute(
            "INSERT INTO notifications"
            " (recipient_id, document_id, event_seq, event_type, at, is_read, created_at)"
            " VALUES (?, ?, ?, ?, ?, 0, ?)",
            (recipient_id, document_id, event_seq, event_type, at, now),
        )


# ---------------------------------------------------------------------------
# 定时开封 / 过期扫描
#
# 到点开封（waiting -> opened）与到期失效（pending -> expired）共用同一把
# 串行化写锁。waiting 请求到点后只能开封：任何写临界区内都先做“到点开封”
# 判定，因此即便后台处理延迟，之后到达的撤销或到期扫描也无法将其作废。
# ---------------------------------------------------------------------------


def open_request_now(
    tx: sqlite3.Connection,
    *,
    req_id: int,
    doc_id: int,
    now: str,
    actor_id: int | None = None,
    detail: str = "已到最早开封时刻且两名不同指定见证人均已同意，资料自动开封",
) -> None:
    """在已持有的写事务内把资料与请求置为 opened 并追加审计。

    资料一旦开封，其上待确认的封存人交接邀请不再可确认，随开封一并失效。
    开封成功的同一事务内，按“开封时刻的封存人 + 三名见证人”各记一条
    opened 通知（事件序号取 opened 审计记录序号）。
    """
    tx.execute(
        "UPDATE documents SET status='opened', opened_at=? WHERE id=? AND status='sealed'",
        (now, doc_id),
    )
    tx.execute(
        "UPDATE open_requests SET status='opened', resolved_at=? WHERE id=?",
        (now, req_id),
    )
    event_seq = add_audit(
        tx,
        actor_id=actor_id,
        action="opened",
        document_id=doc_id,
        request_id=req_id,
        detail=detail,
    )
    invalidate_pending_invitations(
        tx,
        doc_id=doc_id,
        now=now,
        detail="资料已开封，待确认的封存人交接邀请失效",
    )
    owner_id = tx.execute(
        "SELECT owner_id FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()["owner_id"]
    fanout_notifications(
        tx,
        recipients=participant_ids(tx, doc_id, owner_id),
        document_id=doc_id,
        event_seq=event_seq,
        event_type="opened",
        at=now,
    )


def invalidate_pending_invitations(
    tx: sqlite3.Connection, *, doc_id: int, now: str, detail: str,
    exclude_id: int | None = None,
) -> int:
    """在已持有的写事务内把该资料全部待确认交接邀请置为 expired 并逐条留痕。

    开封与交接确认会调用本函数（确认时排除正在确认的这一份）；返回失效条数。
    """
    rows = tx.execute(
        "SELECT id FROM handover_invitations"
        " WHERE document_id = ? AND status = 'pending'"
        + (" AND id != ?" if exclude_id is not None else ""),
        (doc_id, exclude_id) if exclude_id is not None else (doc_id,),
    ).fetchall()
    for row in rows:
        tx.execute(
            "UPDATE handover_invitations SET status='expired', resolved_at=? WHERE id=?",
            (now, row["id"]),
        )
        add_audit(
            tx,
            actor_id=None,
            action="handover_invitation_expired",
            document_id=doc_id,
            detail=f"交接邀请 {row['id']}：{detail}",
        )
    return len(rows)


def resolve_due_waiting(tx: sqlite3.Connection, now: str, request_id: int | None = None) -> int:
    """在已持有的写事务内把已到最早开封时刻的 waiting 请求确定性开封。

    所有写路径（同意/撤销/新建请求/后台扫描）进入临界区后都先调用本函数，
    保证“到点必开封”不依赖后台扫描器的及时性。返回本次开封条数。

    清除优先：资料已安排清除时刻且 purge_at <= earliest_open_at（清除更早
    或与最早开封同刻）时，请求不在此处开封，交由 purge_due_documents 直接
    清除（资料 purged、请求 invalidated）；只有 purge_at 严格晚于
    earliest_open_at（开封更早）的请求才在此处先开封。
    """
    # 清除优先条件：d.purge_at IS NULL OR d.purge_at > r.earliest_open_at 才开封
    if request_id is not None:
        rows = tx.execute(
            "SELECT r.id, r.document_id FROM open_requests r"
            " JOIN documents d ON d.id = r.document_id"
            " WHERE r.id = ? AND r.status = 'waiting' AND r.earliest_open_at <= ?"
            " AND (d.purge_at IS NULL OR d.purge_at > r.earliest_open_at)",
            (request_id, now),
        ).fetchall()
    else:
        rows = tx.execute(
            "SELECT r.id, r.document_id FROM open_requests r"
            " JOIN documents d ON d.id = r.document_id"
            " WHERE r.status = 'waiting' AND r.earliest_open_at <= ?"
            " AND (d.purge_at IS NULL OR d.purge_at > r.earliest_open_at)",
            (now,),
        ).fetchall()
    for row in rows:
        open_request_now(tx, req_id=row["id"], doc_id=row["document_id"], now=now)
    return len(rows)


# ---------------------------------------------------------------------------
# 到期清除正文（scheduled purge）
#
# 封存人可为资料安排带时区的未来清除时刻；到期后无论扫描器是否准时运行，
# 后续查询与操作都必须呈现已清除结果：扫描器、每个写事务与资料查询都会先
# 做确定性到点清除判定。清除生效时：清空正文（body 置空）、状态置 purged、
# purged_at 记为约定的清除时刻；摘要、参与方与审计元数据全部保留；未开封
# 资料的进行中开封请求失效（旧同意删除）、待确认交接邀请失效；已开封资料
# 同样按期清除。清除与最早开封同刻时清除优先，开封更早则先开封再清除。
# ---------------------------------------------------------------------------


def purge_document_now(
    tx: sqlite3.Connection,
    *,
    doc_id: int,
    now: str,
    actor_id: int | None = None,
) -> None:
    """在已持有的写事务内清除一份到点资料的正文并追加审计。

    结算顺序：先把“最早开封时刻严格早于清除时刻”的待开封请求补开
    （开封更早则先开封；读路径直接调用本函数时也能得到一致历史），
    再把仍进行中的开封请求失效并删除旧同意、使待确认交接邀请失效，
    最后清空正文、置 purged。清除与最早开封同刻时清除优先（不开封）。
    """
    doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    purge_at = doc["purge_at"]

    # 开封时刻严格更早的 waiting 请求先开封（同刻不清除优先 -> 不在这里开）。
    # 写路径中 resolve_due_waiting 已处理过这些请求，此处是读路径/兜底补齐。
    earlier = tx.execute(
        "SELECT id FROM open_requests"
        " WHERE document_id = ? AND status = 'waiting' AND earliest_open_at < ?",
        (doc_id, purge_at),
    ).fetchall()
    for row in earlier:
        open_request_now(tx, req_id=row["id"], doc_id=doc_id, now=now)

    # 仍进行中的开封请求失效（过截止时刻的按原规则 expired），旧同意删除。
    active = tx.execute(
        "SELECT * FROM open_requests"
        " WHERE document_id = ? AND status IN ('pending', 'waiting')"
        " ORDER BY id DESC LIMIT 1",
        (doc_id,),
    ).fetchone()
    if active is not None:
        if active["status"] == "pending" and active["deadline"] <= now:
            tx.execute(
                "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                (now, active["id"]),
            )
            add_audit(
                tx,
                actor_id=None,
                action="request_expired",
                document_id=doc_id,
                request_id=active["id"],
                detail="清除时刻到达时开封请求已过截止时刻，请求按原规则失效，已有同意作废",
            )
        else:
            tx.execute(
                "UPDATE open_requests SET status='invalidated', resolved_at=? WHERE id=?",
                (now, active["id"]),
            )
            add_audit(
                tx,
                actor_id=None,
                action="request_invalidated",
                document_id=doc_id,
                request_id=active["id"],
                detail="资料已到清除时刻，进行中的开封请求失效，其全部旧同意作废",
            )
        tx.execute("DELETE FROM approvals WHERE request_id = ?", (active["id"],))

    # 待确认的封存人交接邀请随清除失效。
    invalidate_pending_invitations(
        tx,
        doc_id=doc_id,
        now=now,
        detail="资料已到清除时刻，待确认的封存人交接邀请失效",
    )

    # 清空正文、置 purged；purged_at 记为约定的清除时刻，与扫描及时性无关。
    # 摘要、参与方（封存人/见证人）与审计元数据全部保留。
    tx.execute(
        "UPDATE documents SET status='purged', body='', purged_at=?"
        " WHERE id=? AND status IN ('sealed', 'opened')",
        (purge_at, doc_id),
    )
    event_seq = add_audit(
        tx,
        actor_id=actor_id,
        action="purged",
        document_id=doc_id,
        detail=(
            f"已到清除时刻 {purge_at}，资料正文已清除；"
            "摘要、参与方与审计元数据保留，任何接口不再返回正文"
        ),
    )
    # 清除时刻的封存人 + 三名见证人各记一条 purged 通知；与清除同事务，
    # 失败/回滚不留通知，扫描器与请求并发结算同一变化不重复投递。
    owner_id = tx.execute(
        "SELECT owner_id FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()["owner_id"]
    fanout_notifications(
        tx,
        recipients=participant_ids(tx, doc_id, owner_id),
        document_id=doc_id,
        event_seq=event_seq,
        event_type="purged",
        at=purge_at,
    )


def purge_due_documents(
    tx: sqlite3.Connection, now: str, document_id: int | None = None
) -> int:
    """在已持有的写事务内把已到清除时刻的资料确定性清除。返回本次清除份数。

    与到点开封同理：扫描器、每个写事务与资料查询都先做本判定，
    “到期必清除”不依赖后台扫描器的及时性。
    """
    if document_id is not None:
        rows = tx.execute(
            "SELECT id FROM documents"
            " WHERE id = ? AND purge_at IS NOT NULL AND purge_at <= ?"
            " AND status != 'purged'",
            (document_id, now),
        ).fetchall()
    else:
        rows = tx.execute(
            "SELECT id FROM documents"
            " WHERE purge_at IS NOT NULL AND purge_at <= ? AND status != 'purged'",
            (now,),
        ).fetchall()
    for row in rows:
        purge_document_now(tx, doc_id=row["id"], now=now)
    return len(rows)


def settle_due_purges_for_read(
    db: sqlite3.Connection,
    document_id: int | None = None,
    user: AuthUser | None = None,
) -> None:
    """读路径的确定性到点清除：仅当存在到点未清除的资料时才进入写事务。

    保证“到期后查询也呈现已清除结果”，同时不改变未清除资料的只读行为。
    若提供已认证账号，则清除写入同样要在写锁内复核密钥：密钥已被轮换提交的
    请求不得借读路径产生任何写入（扫描器与后续合法请求会照常完成结算）。
    """
    now = utc_str(now_utc())
    if document_id is not None:
        due = db.execute(
            "SELECT 1 FROM documents"
            " WHERE id = ? AND purge_at IS NOT NULL AND purge_at <= ?"
            " AND status != 'purged'",
            (document_id, now),
        ).fetchone()
    else:
        due = db.execute(
            "SELECT 1 FROM documents"
            " WHERE purge_at IS NOT NULL AND purge_at <= ? AND status != 'purged'"
            " LIMIT 1",
            (now,),
        ).fetchone()
    if due is None:
        return
    with write_tx(db) as tx:
        if user is not None and not key_still_valid(tx, user):
            raise err(401, "凭据无效")
        purge_due_documents(tx, utc_str(now_utc()), document_id)


def expire_due_invitations(tx: sqlite3.Connection, now: str, invitation_id: int | None = None) -> int:
    """在已持有的写事务内把已过确认截止时刻仍 pending 的交接邀请确定性置为 expired。

    与到点开封同理：所有交接写路径进入临界区后先调用本函数，逾期结论不依赖
    后台扫描器的及时性。返回失效条数。
    """
    if invitation_id is not None:
        rows = tx.execute(
            "SELECT id, document_id FROM handover_invitations"
            " WHERE id = ? AND status = 'pending' AND confirm_deadline <= ?",
            (invitation_id, now),
        ).fetchall()
    else:
        rows = tx.execute(
            "SELECT id, document_id FROM handover_invitations"
            " WHERE status = 'pending' AND confirm_deadline <= ?",
            (now,),
        ).fetchall()
    for row in rows:
        tx.execute(
            "UPDATE handover_invitations SET status='expired', resolved_at=? WHERE id=?",
            (now, row["id"]),
        )
        add_audit(
            tx,
            actor_id=None,
            action="handover_invitation_expired",
            document_id=row["document_id"],
            detail=f"交接邀请 {row['id']} 已过确认截止时刻且未被确认，邀请失效，可重新邀请",
        )
    return len(rows)


def expire_due_pre_seal_proposals(
    tx: sqlite3.Connection, now: str, proposal_id: int | None = None
) -> int:
    """在已持有的写事务内把已过确认截止时刻仍 pending 的预封存提案确定性置为
    expired，并立即清空提案正文。

    与到点开封/交接邀请逾期同理：扫描器、每个提案写事务与提案查询进入临界区
    后都先调用本函数，到期结论不依赖后台扫描器及时性；截止时未集齐三人确认即
    失效，撤回/失效/完成后正文一律不在库中保留。返回本次失效条数。
    """
    if proposal_id is not None:
        rows = tx.execute(
            "SELECT id FROM pre_seal_proposals"
            " WHERE id = ? AND status = 'pending' AND deadline <= ?",
            (proposal_id, now),
        ).fetchall()
    else:
        rows = tx.execute(
            "SELECT id FROM pre_seal_proposals"
            " WHERE status = 'pending' AND deadline <= ?",
            (now,),
        ).fetchall()
    for row in rows:
        tx.execute(
            "UPDATE pre_seal_proposals SET status='expired', body='', resolved_at=?"
            " WHERE id=? AND status='pending'",
            (now, row["id"]),
        )
    return len(rows)


def settle_all_due(tx: sqlite3.Connection, now: str) -> int:
    """在已持有的写事务内按与后台扫描器完全相同的顺序做一轮全局到点结算。

    顺序固定为：到点开封 -> 到期清除 -> 开封请求到期 -> 交接邀请逾期 ->
    预封存提案逾期（并清空逾期提案正文）。返回失效（expired）的开封请求数。
    账号停用临界区先调用本函数：即便随后因未了义务拒绝停用，本次结算也已随
    该事务提交落库（“到点结算仍落库”）。
    """
    # 第一步：到点开封（清除优先的 waiting 请求不在此开封）。
    resolve_due_waiting(tx, now)
    # 第二步：到清除时刻的资料清空正文（purged），其进行中请求、
    # 旧同意与待确认交接邀请在 purge_document_now 内一并结算。
    purge_due_documents(tx, now)
    # 第三步：到截止时刻仍未集齐两票（pending）的请求失效。
    rows = tx.execute(
        "SELECT id, document_id FROM open_requests"
        " WHERE status = 'pending' AND deadline <= ?",
        (now,),
    ).fetchall()
    for row in rows:
        tx.execute(
            "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
            (now, row["id"]),
        )
        add_audit(
            tx,
            actor_id=None,
            action="request_expired",
            document_id=row["document_id"],
            request_id=row["id"],
            detail="开封请求到期前未取得两名不同见证人同意，请求失效，已有同意作废",
        )
    # 第四步：过确认截止时刻仍待确认的交接邀请失效（逾期后可重新邀请）。
    expire_due_invitations(tx, now)
    # 第五步：过确认截止时刻仍待确认的预封存提案失效并清空提案正文。
    expire_due_pre_seal_proposals(tx, now)
    return len(rows)


class ExpirySweeper:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop = asyncio.Event()  # 允许同一进程内停止后再次启动（测试/多服务）
        await asyncio.to_thread(init_db)
        await asyncio.to_thread(self.sweep_once)  # 重启后先恢复一次
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.sleep(settings.expiry_interval_seconds)
                await asyncio.to_thread(self.sweep_once)
            except asyncio.CancelledError:
                break

    @staticmethod
    def sweep_once() -> int:
        """一轮定时处理：顺序执行全局到点结算（见 settle_all_due 的文档）；
        返回失效（expired）的开封请求条数。"""
        conn = connect()
        try:
            with write_tx(conn) as tx:
                return settle_all_due(tx, utc_str(now_utc()))
        finally:
            conn.close()


sweeper = ExpirySweeper()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await sweeper.start()
    # 启动即确保审计快照签名密钥存在（不存在则生成并持久化）：
    # 复核人可在任何快照签发之前取得公钥并事先建立信任；密钥随库保留，
    # 重启后旧签名仍可验证。
    await asyncio.to_thread(ensure_signing_key)
    try:
        yield
    finally:
        await sweeper.stop()


app = FastAPI(
    title="双人见证资料封存服务",
    version="1.11.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 错误体只返回字段路径与类型信息，绝不回显提交内容（否则会泄露封存正文）。"""
    safe_errors = [
        {
            "loc": list(e.get("loc", [])),
            "msg": e.get("msg", "参数错误"),
            "type": e.get("type", "value_error"),
        }
        for e in exc.errors()
    ]
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={"error": "请求参数校验失败", "details": safe_errors})


# ---------------------------------------------------------------------------
# 依赖：数据库连接 + API Key 认证（无效凭据一律 401，不写任何记录）
# ---------------------------------------------------------------------------


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def current_user(
    x_api_key: str | None = Header(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> AuthUser:
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 API Key")
    row = db.execute(
        "SELECT * FROM users WHERE key_hash = ?", (sha256_hex(x_api_key),)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="凭据无效")
    return AuthUser(row, x_api_key)


def key_still_valid(tx: sqlite3.Connection, user: AuthUser) -> bool:
    """在已持有的写锁内复核请求密钥是否仍是该账号当前密钥。

    密钥轮换与业务写事务经 BEGIN IMMEDIATE 串行化：本函数读取到的
    key_hash 即“本事务提交顺序下”的最新值。旧密钥在轮换提交后进入的
    任何已认证写事务都会得到 False，由调用方按既有 401 行为拒绝并回滚。
    """
    current = tx.execute(
        "SELECT key_hash FROM users WHERE id = ?", (user["id"],)
    ).fetchone()
    return current is not None and current["key_hash"] == sha256_hex(user.presented_key)


@contextmanager
def authed_write_tx(db: sqlite3.Connection, user: AuthUser) -> Iterator[sqlite3.Connection]:
    """已认证业务写操作专用事务：BEGIN IMMEDIATE 取锁后先复核密钥。

    - 复核发生在任何业务检查/写入与审计之前：密钥若已被先提交的轮换作废，
      立即按既有 401 行为拒绝并整体回滚，业务写入不生效、不留业务审计，
      也不触发到点开封/清除等连带结算（结算由扫描器与后续合法请求完成）；
    - 密钥仍有效时与普通 write_tx 等价；业务校验在锁内失败仍按原规则回滚，
      行为与引入轮换前完全一致。
    """
    with write_tx(db) as tx:
        if not key_still_valid(tx, user):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="凭据无效")
        yield tx


def err(status_code: int, message: str) -> HTTPException:
    """统一错误体，只含一句不含正文/摘要的提示。"""
    return HTTPException(status_code=status_code, detail=message)


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


@app.post("/register", status_code=201)
def register(payload: RegisterIn, db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """本地注册：用户名唯一；只在本次响应中返回 API Key，之后无法找回。"""
    api_key = secrets.token_hex(32)
    with write_tx(db) as tx:
        exists = tx.execute("SELECT 1 FROM users WHERE username = ?", (payload.username,)).fetchone()
        if exists:
            raise err(409, "用户名已存在")
        cur = tx.execute(
            "INSERT INTO users (username, key_hash, created_at) VALUES (?, ?, ?)",
            (payload.username, sha256_hex(api_key), utc_str(now_utc())),
        )
        add_audit(tx, actor_id=cur.lastrowid, action="register", detail=f"用户 {payload.username} 注册")
        user_id = cur.lastrowid
    return {"user_id": user_id, "username": payload.username, "api_key": api_key}


@app.get("/me")
def me(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
    return {"user_id": user["id"], "username": user["username"]}


@app.post("/me/api-key/rotate")
def rotate_api_key(
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """账号持有人凭当前 X-API-Key 轮换密钥。

    - 新密钥为 256 位随机串，明文只在本次响应返回一次，服务端之后无法找回；
      持久层仅保存其 SHA-256 摘要（旧摘要被覆盖，不保留历史密钥）；
    - 事务一旦提交，旧密钥立即失效（WAL 持久化，重启后仍然失效）；
    - 与其他已认证写操作共用同一把 BEGIN IMMEDIATE 写锁：轮换先提交则并发的
      旧密钥业务写一律 401 回滚；业务写先提交则该业务写照常完成，本次轮换
      随后提交。无效/缺失凭据沿用既有 401（不区分账号是否存在，不留记录）。
    """
    new_api_key = secrets.token_hex(32)
    now = utc_str(now_utc())
    with authed_write_tx(db, user) as tx:
        # 锁内复核（authed_write_tx 已做一次；此处条件更新同时保证只更新本人行）
        cur = tx.execute(
            "UPDATE users SET key_hash = ? WHERE id = ? AND key_hash = ?",
            (sha256_hex(new_api_key), user["id"], sha256_hex(user.presented_key)),
        )
        if cur.rowcount != 1:
            # 理论不可达：复核刚通过；保持与既有 401 完全一致的失败语义
            raise err(401, "凭据无效")
        add_audit(
            tx,
            actor_id=user["id"],
            action="api_key_rotated",
            detail="账号 API Key 已轮换：旧密钥即刻失效，库内仅保存新密钥摘要，明文未持久化",
        )
    return {
        "user_id": user["id"],
        "username": user["username"],
        "api_key": new_api_key,
        "rotated_at": now,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 账号停用（account deactivation）
#
# 账号持有人凭当前 X-API-Key 申请停用。停用临界区先按现有规则做一轮全局到点
# 结算（settle_all_due，与后台扫描器同一顺序）：即使随后因未了义务拒绝停用，
# 本次结算也随事务提交落库。结算后仍有未了义务则 409 拒绝并逐项返回义务
# （类型/对象编号/原因），账号保持可用；义务结清则账号立即停用：
# key_hash 覆盖为不可匹配值、status 置 deactivated，当前密钥立即且永久失效，
# 后续访问沿用无效密钥 401（不区分账号是否存在），同名不可重注册（用户名
# 唯一约束保留，沿用 409）。历史审计、通知与参与记录全部保留，不删除任何行。
#
# 停用后其他账号不得再把该账号指定为见证人/交接受让人/预封存受邀见证人：
# 各指定路径在写锁内按用户名解析参与人时只接受 status='active'，停用账号
# 一律按现有“无效参与人”错误处理（沿用未注册的 400 与文案，不暴露停用状态）。
# 停用与封存/替换/交接/预封存/轮换并发时，全部按 BEGIN IMMEDIATE 提交顺序
# 裁定（详见模块文档），不会出现停用后新增角色或部分停用。
# ---------------------------------------------------------------------------


def collect_deactivation_obligations(
    tx: sqlite3.Connection, user_id: int
) -> list[dict[str, Any]]:
    """在已持有的写事务内（全局到点结算之后）枚举本人未了义务。

    义务口径（全部以结算后的当前状态为准）：
    - 任一未清除（sealed/opened）资料的封存人；
    - 任一未开封（sealed）资料的当前见证人；
    - 任一待确认（pending）预封存提案的发起人或受邀见证人；
    - 任一待确认（pending）交接邀请的受让人。
    返回按 (type, ref_id) 稳定排序的义务清单。
    """
    obligations: list[dict[str, Any]] = []

    for row in tx.execute(
        "SELECT id, status FROM documents WHERE owner_id = ? AND status != 'purged'"
        " ORDER BY id",
        (user_id,),
    ).fetchall():
        obligations.append({
            "type": "document_owner",
            "ref_kind": "document",
            "ref_id": row["id"],
            "reason": (
                "本人是未清除资料的封存人（资料未开封），须先完成交接或到点清除"
                if row["status"] == "sealed"
                else "本人是未清除资料的封存人（资料已开封、正文尚未到点清除），须待到点清除"
            ),
        })

    for row in tx.execute(
        """SELECT d.id AS id FROM document_witnesses w
           JOIN documents d ON d.id = w.document_id
           WHERE w.user_id = ? AND d.status = 'sealed'
           ORDER BY d.id""",
        (user_id,),
    ).fetchall():
        obligations.append({
            "type": "document_witness",
            "ref_kind": "document",
            "ref_id": row["id"],
            "reason": "本人是未开封资料的当前见证人，须待资料开封/清除或被替换后再停用",
        })

    for row in tx.execute(
        "SELECT id FROM pre_seal_proposals WHERE owner_id = ? AND status = 'pending'"
        " ORDER BY id",
        (user_id,),
    ).fetchall():
        obligations.append({
            "type": "pre_seal_owner",
            "ref_kind": "pre_seal_proposal",
            "ref_id": row["id"],
            "reason": "本人是待确认预封存提案的发起人，须待三人确认完成、撤回或逾期后再停用",
        })

    for row in tx.execute(
        """SELECT p.id AS id FROM pre_seal_witnesses pw
           JOIN pre_seal_proposals p ON p.id = pw.proposal_id
           WHERE pw.user_id = ? AND p.status = 'pending'
           ORDER BY p.id""",
        (user_id,),
    ).fetchall():
        obligations.append({
            "type": "pre_seal_witness",
            "ref_kind": "pre_seal_proposal",
            "ref_id": row["id"],
            "reason": "本人是待确认预封存提案的受邀见证人，须待提案完成、失效或被替换后再停用",
        })

    for row in tx.execute(
        "SELECT id FROM handover_invitations WHERE to_user_id = ? AND status = 'pending'"
        " ORDER BY id",
        (user_id,),
    ).fetchall():
        obligations.append({
            "type": "handover_assignee",
            "ref_kind": "handover_invitation",
            "ref_id": row["id"],
            "reason": "本人是待确认交接邀请的受让人，须先确认、拒绝（逾期）或待邀请撤回后再停用",
        })

    obligations.sort(key=lambda o: (o["type"], o["ref_id"]))
    return obligations


@app.post("/me/deactivation")
def deactivate_account(
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """账号持有人凭当前 X-API-Key 申请停用本人账号。

    - 临界区先做一轮与扫描器同序的全局到点结算：即使随后拒绝停用，本次
      到点开封/清除/失效也随事务提交落库；
    - 结算后仍有未了义务：409 拒绝，返回逐项义务（类型/对象编号/原因），
      账号保持可用（本次结算仍已落库）；
    - 无未了义务：立即停用，key_hash 覆盖为不可匹配值并置
      status='deactivated'；当前密钥自提交起永久失效，后续访问沿用无效
      密钥 401，同名不可重注册；历史审计、通知与参与记录保留。
    """
    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with authed_write_tx(db, user) as tx:
        now = utc_str(now_utc())
        # 第一步：按现有规则全局到点结算（与 ExpirySweeper.sweep_once 同序）。
        settle_all_due(tx, now)

        # 第二步：以结算后的当前状态枚举未了义务。
        obligations = collect_deactivation_obligations(tx, user["id"])
        if obligations:
            # 到点结算必须先落库：错误延迟到提交后再抛出（与同意/撤销同一语义）。
            post_commit_error = HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "仍有未了义务，账号停用被拒绝；账号保持可用",
                    "obligations": obligations,
                },
            )
        else:
            # 第三步：无未了义务 -> 立即停用。
            # key_hash 覆盖为带前缀的不可匹配随机值：任何提交密钥的 SHA-256
            # 摘要（64 位十六进制）都不可能等于它，认证随即与“无效密钥”完全一致。
            tombstone = "deactivated:" + secrets.token_hex(32)
            cur = tx.execute(
                "UPDATE users SET status='deactivated', deactivated_at=?, key_hash=?"
                " WHERE id=? AND status='active'",
                (now, tombstone, user["id"]),
            )
            if cur.rowcount != 1:
                # 理论不可达：authed_write_tx 已在锁内复核密钥且账号仍 active
                raise err(401, "凭据无效")
            add_audit(
                tx,
                actor_id=user["id"],
                action="account_deactivated",
                detail=(
                    "账号已停用：密钥即刻且永久失效，同名不可重新注册；"
                    "历史审计、通知与参与记录保留，不再接受以该账号为见证人/"
                    "交接受让人/预封存受邀见证人的任何指定"
                ),
            )
            result = {
                "user_id": user["id"],
                "username": user["username"],
                "status": "deactivated",
                "deactivated_at": now,
            }
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# 资料封存
# ---------------------------------------------------------------------------


def create_sealed_document(
    tx: sqlite3.Connection,
    *,
    owner_id: int,
    body: str,
    witness_ids: list[int],
    witness_names: list[str],
    actor_id: int,
    now: str,
) -> int:
    """在已持有的写事务内按“现有封存规则”创建一份封存资料并追加 seal 审计。

    直接封存与预存存在三人确认齐后均调用本函数，两者生成的资料与审计
    完全一致：documents / document_witnesses 行相同，seal 审计的 actor 为
    封存人、detail 文案与直接封存一致（不含预封存流程信息）。返回资料编号。
    调用方须保证三名见证人互异、均已注册且不含封存人本人。
    """
    digest = sha256_hex(body)
    cur = tx.execute(
        "INSERT INTO documents (owner_id, body, digest, status, created_at)"
        " VALUES (?, ?, ?, 'sealed', ?)",
        (owner_id, body, digest, now),
    )
    doc_id = int(cur.lastrowid)
    for position, witness_id in enumerate(witness_ids):
        tx.execute(
            "INSERT INTO document_witnesses (document_id, user_id, position) VALUES (?, ?, ?)",
            (doc_id, witness_id, position),
        )
    add_audit(
        tx,
        actor_id=actor_id,
        action="seal",
        document_id=doc_id,
        detail=f"封存资料，摘要 {digest[:12]}…；见证人: {', '.join(witness_names)}",
    )
    return doc_id


@app.post("/documents", status_code=201)
def seal_document(
    payload: SealIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    if len(set(payload.witnesses)) != 3:
        raise err(400, "必须指定三名互不相同的见证人")
    if user["username"] in payload.witnesses:
        raise err(400, "封存人不能同时担任见证人")

    with authed_write_tx(db, user) as tx:
        placeholders = ",".join("?" for _ in payload.witnesses)
        witness_rows = tx.execute(
            f"SELECT id, username FROM users WHERE status='active' AND username IN ({placeholders})",
            payload.witnesses,
        ).fetchall()
        found = {row["username"] for row in witness_rows}
        missing = [w for w in payload.witnesses if w not in found]
        if missing:
            # 未注册与已停用账号按现有“无效参与人”错误同一处理：不暴露停用状态
            raise err(400, f"见证人尚未注册: {', '.join(missing)}")
        id_by_name = {row["username"]: row["id"] for row in witness_rows}
        witness_ids = [id_by_name[name] for name in payload.witnesses]

        now = utc_str(now_utc())
        digest = sha256_hex(payload.body)
        doc_id = create_sealed_document(
            tx,
            owner_id=user["id"],
            body=payload.body,
            witness_ids=witness_ids,
            witness_names=payload.witnesses,
            actor_id=user["id"],
            now=now,
        )

    # 注意：响应不含正文
    return {"document_id": doc_id, "status": "sealed", "digest": digest, "witnesses": payload.witnesses}


# ---------------------------------------------------------------------------
# 预封存提案（pre-seal proposal）
#
# 封存人先提交正文、三名互异且非本人的已注册见证人及带时区的未来确认截止
# 时刻，取得提案编号与正文摘要（响应不含正文）。仅发起人本人与受邀见证人
# 可凭编号查看提案的摘要、截止时刻、状态与确认人数；其他账号（含凭编号
# 试探）统一 403，不区分提案是否存在。见证人凭摘要各确认一次：摘要不符、
# 重复确认一律 409 拒绝且不计票。三人在截止前确认后，在第三票的同一写
# 事务内调用 create_sealed_document 按现有封存规则生成一份普通封存资料
# （seal 审计与直接封存完全一致）并返回资料编号；已完成的提案重复请求
# 不得再生成资料。发起人可在完成前撤回；截止时未集齐三人即失效；撤回、
# 失效或完成后立即清空提案正文。确认/撤回/到期与密钥轮换按写锁已提交
# 顺序产生唯一结果；后台扫描器、每个提案写事务与提案查询都做确定性到期
# 判定，停机期间到期在重启恢复扫描时补结算。
#
# 截止判定以取得写事务后的实际时刻为准：发起、确认、撤回都可能在截止前
# 到达、等待写锁期间越过截止时刻。为此三个写接口都在 BEGIN IMMEDIATE
# 取锁（并先在锁内复核当前密钥）之后才取“当前时刻”做判定——
#   发起：取锁时截止已过则沿用 400 拒绝并整体回滚，不落提案；
#   确认/撤回：取锁时截止已过则先把待处理提案确定性失效并清空正文，
#     再沿用 409 拒绝，不计新票、不生成资料或封存审计；
# 截止前取得写锁的有效第三票仍在同一事务内生成唯一资料，记录的完成时刻
# 即取锁后的实际时刻。
# ---------------------------------------------------------------------------


def serialize_proposal(tx: sqlite3.Connection, prop: sqlite3.Row) -> dict[str, Any]:
    """组装提案视图：摘要、截止时刻、状态与确认人数；绝不含提案正文。"""
    witnesses = [
        row["username"]
        for row in tx.execute(
            """SELECT u.username FROM pre_seal_witnesses w
               JOIN users u ON u.id = w.user_id
               WHERE w.proposal_id = ? ORDER BY w.position""",
            (prop["id"],),
        ).fetchall()
    ]
    confirmation_count = tx.execute(
        "SELECT COUNT(*) AS c FROM pre_seal_confirmations WHERE proposal_id = ?",
        (prop["id"],),
    ).fetchone()["c"]
    return {
        "proposal_id": prop["id"],
        "status": prop["status"],
        "digest": prop["digest"],
        "confirm_deadline": prop["deadline"],
        "witnesses": witnesses,
        "confirmation_count": confirmation_count,
        "required": 3,
        "document_id": prop["document_id"],
        "created_at": prop["created_at"],
        "resolved_at": prop["resolved_at"],
    }


def load_proposal_for_party(
    db: sqlite3.Connection, proposal_id: int, user: AuthUser
) -> sqlite3.Row:
    """取提案并校验“发起人本人或受邀见证人”身份；其他情况统一 403，不区分存在性。

    提案终结（撤回/失效/完成）后正文已清空，仍允许原参与方查看元数据
    （摘要、截止时刻、状态、确认人数），但任何路径都不返回正文。
    """
    prop = db.execute(
        "SELECT * FROM pre_seal_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if prop is None:
        raise err(403, "无权访问该预封存提案或提案不存在")
    if prop["owner_id"] == user["id"]:
        return prop
    is_witness = db.execute(
        "SELECT 1 FROM pre_seal_witnesses WHERE proposal_id = ? AND user_id = ?",
        (proposal_id, user["id"]),
    ).fetchone()
    if is_witness is None:
        raise err(403, "无权访问该预封存提案或提案不存在")
    return prop


def settle_due_pre_seal_for_read(
    db: sqlite3.Connection,
    proposal_id: int | None = None,
    user: AuthUser | None = None,
) -> None:
    """读路径的确定性预封存到期：仅当存在到点未失效的 pending 提案时才进入写事务。

    与到期清除读路径同理：提案查询时若已过截止时刻仍 pending，先在写事务内
    确定性置 expired 并清空正文，再返回最新状态，到期结论不依赖扫描器。
    已认证账号的到期写入同样要在写锁内复核密钥：密钥已被轮换提交的请求不得
    借读路径产生任何写入。
    """
    now = utc_str(now_utc())
    if proposal_id is not None:
        due = db.execute(
            "SELECT 1 FROM pre_seal_proposals"
            " WHERE id = ? AND status = 'pending' AND deadline <= ?",
            (proposal_id, now),
        ).fetchone()
    else:
        due = db.execute(
            "SELECT 1 FROM pre_seal_proposals"
            " WHERE status = 'pending' AND deadline <= ? LIMIT 1",
            (now,),
        ).fetchone()
    if due is None:
        return
    with write_tx(db) as tx:
        if user is not None and not key_still_valid(tx, user):
            raise err(401, "凭据无效")
        expire_due_pre_seal_proposals(tx, utc_str(now_utc()), proposal_id)


@app.post("/pre-seal-proposals", status_code=201)
def create_pre_seal_proposal(
    payload: PreSealIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """封存人发起需见证人确认的预封存提案。

    三名见证人须互异、均已注册且不含封存人本人；确认截止时刻带时区且晚于
    当前。响应只给提案编号与正文摘要，绝不回显正文。
    """
    if len(set(payload.witnesses)) != 3:
        raise err(400, "必须指定三名互不相同的见证人")
    if user["username"] in payload.witnesses:
        raise err(400, "封存人不能同时担任见证人")

    deadline = utc_str(payload.confirm_deadline)
    # 锁外快速预检；权威判定在写事务内以取得写锁后的实际时刻复核（见下）。
    if deadline <= utc_str(now_utc()):
        raise err(400, "确认截止时刻必须晚于当前时间")

    with authed_write_tx(db, user) as tx:
        # 截止判定以取得写锁后的实际时刻为准：截止前发起、但等待写锁期间
        # 越过截止时刻的请求，沿用 400 拒绝并整体回滚，不落提案。
        now = utc_str(now_utc())
        if deadline <= now:
            raise err(400, "确认截止时刻必须晚于当前时间")
        placeholders = ",".join("?" for _ in payload.witnesses)
        witness_rows = tx.execute(
            f"SELECT id, username FROM users WHERE status='active' AND username IN ({placeholders})",
            payload.witnesses,
        ).fetchall()
        found = {row["username"] for row in witness_rows}
        missing = [w for w in payload.witnesses if w not in found]
        if missing:
            # 未注册与已停用账号按现有“无效参与人”错误同一处理：不暴露停用状态
            raise err(400, f"见证人尚未注册: {', '.join(missing)}")
        id_by_name = {row["username"]: row["id"] for row in witness_rows}
        witness_ids = [id_by_name[name] for name in payload.witnesses]

        digest = sha256_hex(payload.body)
        cur = tx.execute(
            "INSERT INTO pre_seal_proposals"
            " (owner_id, body, digest, status, deadline, created_at)"
            " VALUES (?, ?, ?, 'pending', ?, ?)",
            (user["id"], payload.body, digest, deadline, now),
        )
        proposal_id = cur.lastrowid
        for position, witness_id in enumerate(witness_ids):
            tx.execute(
                "INSERT INTO pre_seal_witnesses (proposal_id, user_id, position)"
                " VALUES (?, ?, ?)",
                (proposal_id, witness_id, position),
            )
        prop = tx.execute(
            "SELECT * FROM pre_seal_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        result = serialize_proposal(tx, prop)

    # 响应不含正文
    return result


@app.get("/pre-seal-proposals/{proposal_id}")
def get_pre_seal_proposal(
    proposal_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """查看提案：仅发起人本人或受邀见证人可见（统一 403，不区分是否存在）。

    只返回摘要、截止时刻、状态、确认人数与（完成后的）资料编号，绝不返回正文。
    已过截止时刻仍 pending 的提案先被确定性置为 expired 并清空正文。
    """
    # 先做一次存在性/参与方校验，避免非参与方的请求借读路径触发到期写入，
    # 也避免凭编号试探者让任意提案进入写事务（到期写只由参与方/扫描器推进）。
    load_proposal_for_party(db, proposal_id, user)
    settle_due_pre_seal_for_read(db, proposal_id, user)
    prop = load_proposal_for_party(db, proposal_id, user)
    return serialize_proposal(db, prop)


@app.post("/pre-seal-proposals/{proposal_id}/confirmations")
def confirm_pre_seal_proposal(
    proposal_id: int,
    payload: PreSealConfirmIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """受邀见证人凭提案摘要确认一次预封存。

    摘要不符、重复确认一律 409 拒绝且不计票；截止时刻到达后不能确认
    （提案确定性失效、正文清空）。第三名不同见证人确认后，在本事务内按
    现有封存规则生成一份普通封存资料并返回资料编号；已完成提案的任何重复
    请求（含重复确认）都不得再生成资料。
    """
    # 锁外预校验：非受邀见证人/不存在统一 403，不区分提案是否存在。
    prop = db.execute(
        "SELECT * FROM pre_seal_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if prop is None:
        raise err(403, "无权访问该预封存提案或提案不存在")
    is_witness = db.execute(
        "SELECT 1 FROM pre_seal_witnesses WHERE proposal_id = ? AND user_id = ?",
        (proposal_id, user["id"]),
    ).fetchone()
    if is_witness is None:
        raise err(403, "只有受邀见证人可以确认预封存提案")

    # 到期若在本临界区内生效，失效先落库（清空正文），拒绝在提交后再返回，
    # 不能让回滚撤销已生效的到期判定（与同意/撤销开封请求同一语义）。
    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    try:
        with authed_write_tx(db, user) as tx:
            # 截止判定以取得写锁后的实际时刻为准：截止前发起、但等待写锁期间
            # 越过截止时刻的确认，先把提案确定性失效并清空正文，再沿用 409
            # 拒绝——不计新票、不生成资料或封存审计。
            now = utc_str(now_utc())
            expired_here = expire_due_pre_seal_proposals(tx, now, proposal_id) > 0

            prop = tx.execute(
                "SELECT * FROM pre_seal_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            # 进入写临界区后以“当前名单”复核（提案名单不可变，此处仍锁内再查一次）
            if not tx.execute(
                "SELECT 1 FROM pre_seal_witnesses WHERE proposal_id = ? AND user_id = ?",
                (proposal_id, user["id"]),
            ).fetchone():
                raise err(403, "只有受邀见证人可以确认预封存提案")

            if prop["status"] == "confirmed":
                raise err(409, "预封存提案已完成，封存资料已生成，不能重复确认")
            if prop["status"] == "revoked":
                raise err(409, "预封存提案已被发起人撤回，不能确认")
            if prop["status"] == "expired" or expired_here:
                # 到期（含本临界区刚触发）必须先提交，故延迟到提交后再报错
                post_commit_error = err(409, "预封存提案已过确认截止时刻，确认被拒绝")
            elif payload.digest != prop["digest"]:
                # 摘要不符：拒绝且不计票
                raise err(409, "提交的摘要与预封存提案摘要不一致，拒绝确认")
            elif tx.execute(
                "SELECT 1 FROM pre_seal_confirmations WHERE proposal_id = ? AND witness_id = ?",
                (proposal_id, user["id"]),
            ).fetchone() is not None:
                # 同一见证人重复确认：拒绝且不重复计票
                raise err(409, "你已确认本预封存提案，重复提交只计一次")
            else:
                tx.execute(
                    "INSERT INTO pre_seal_confirmations"
                    " (proposal_id, witness_id, bound_digest, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (proposal_id, user["id"], prop["digest"], now),
                )
                count = tx.execute(
                    "SELECT COUNT(*) AS c FROM pre_seal_confirmations WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()["c"]

                new_status = "pending"
                document_id: int | None = None
                if count >= 3:
                    # 三名互异见证人（UNIQUE(proposal_id, witness_id) 保证）在
                    # 截止前确认齐：同一事务内按现有封存规则生成资料。
                    witness_rows = tx.execute(
                        """SELECT u.id, u.username FROM pre_seal_witnesses w
                           JOIN users u ON u.id = w.user_id
                           WHERE w.proposal_id = ? ORDER BY w.position""",
                        (proposal_id,),
                    ).fetchall()
                    # 见证人名单按提案提交时的顺序落到资料上
                    witness_ids = [row["id"] for row in witness_rows]
                    witness_names = [row["username"] for row in witness_rows]
                    document_id = create_sealed_document(
                        tx,
                        owner_id=prop["owner_id"],
                        body=prop["body"],
                        witness_ids=witness_ids,
                        witness_names=witness_names,
                        actor_id=prop["owner_id"],
                        now=now,
                    )
                    tx.execute(
                        "UPDATE pre_seal_proposals"
                        " SET status='confirmed', body='', document_id=?, resolved_at=?"
                        " WHERE id=? AND status='pending'",
                        (document_id, now, proposal_id),
                    )
                    new_status = "confirmed"

                prop = tx.execute(
                    "SELECT * FROM pre_seal_proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
                result = {
                    **serialize_proposal(tx, prop),
                    "new_status": new_status,
                }
                if document_id is not None:
                    result["document_id"] = document_id
        if post_commit_error is not None:
            raise post_commit_error
        assert result is not None
        return result
    except sqlite3.IntegrityError:
        # UNIQUE(proposal_id, witness_id) 兜底（并发重复确认）：只计一次
        raise err(409, "你已确认本预封存提案，重复提交只计一次")


@app.post("/pre-seal-proposals/{proposal_id}/revoke")
def revoke_pre_seal_proposal(
    proposal_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """发起人在完成（或到期）前撤回预封存提案；撤回后正文立即清空。

    非发起人/不存在统一 403；已完成、已撤回、已失效的提案不能撤回（409）。
    到期若在本临界区内生效，失效先提交（清空正文），撤回失败在提交后返回。
    """
    prop = db.execute(
        "SELECT * FROM pre_seal_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if prop is None or prop["owner_id"] != user["id"]:
        raise err(403, "无权操作该预封存提案或提案不存在")

    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with authed_write_tx(db, user) as tx:
        # 截止判定以取得写锁后的实际时刻为准：截止前发起、但等待写锁期间
        # 越过截止时刻的撤回，先把提案确定性失效并清空正文，再沿用 409 拒绝。
        now = utc_str(now_utc())
        expired_here = expire_due_pre_seal_proposals(tx, now, proposal_id) > 0

        prop = tx.execute(
            "SELECT * FROM pre_seal_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if prop["owner_id"] != user["id"]:
            raise err(403, "只有预封存发起人可以撤回该提案")
        if prop["status"] == "confirmed":
            raise err(409, "预封存提案已完成，封存资料已生成，不能撤回")
        if prop["status"] == "revoked":
            raise err(409, "预封存提案已撤回")
        if prop["status"] == "expired" or expired_here:
            post_commit_error = err(409, "预封存提案已过确认截止时刻，不能撤回")
        else:
            tx.execute(
                "UPDATE pre_seal_proposals SET status='revoked', body='', resolved_at=?"
                " WHERE id=? AND status='pending'",
                (now, proposal_id),
            )
            prop = tx.execute(
                "SELECT * FROM pre_seal_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            result = serialize_proposal(tx, prop)
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


def load_doc_for_participant(
    db: sqlite3.Connection, doc_id: int, user: AuthUser
) -> sqlite3.Row:
    """取资料并校验参与方（封存人或指定见证人）；否则统一 403，不区分是否存在。"""
    doc = db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise err(403, "无权访问该资料或资料不存在")
    if doc["owner_id"] == user["id"]:
        return doc
    is_witness = db.execute(
        "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
        (doc_id, user["id"]),
    ).fetchone()
    if is_witness is None:
        raise err(403, "无权访问该资料或资料不存在")
    return doc


@app.get("/documents")
def list_documents(
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """仅列出本人封存或本人见证的资料（只有元数据，永不返回正文）。

    到清除时刻的资料在查询前被确定性清除（purged），列表呈现已清除状态。
    """
    settle_due_purges_for_read(db, user=user)
    rows = db.execute(
        """
        SELECT DISTINCT d.id, d.status, d.created_at, d.opened_at, d.purge_at, d.purged_at
        FROM documents d
        LEFT JOIN document_witnesses w ON w.document_id = d.id
        WHERE d.owner_id = ? OR w.user_id = ?
        ORDER BY d.id
        """,
        (user["id"], user["id"]),
    ).fetchall()
    return {"documents": [dict(row) for row in rows]}


def _open_request_meta(req: sqlite3.Row) -> dict[str, Any]:
    """开封请求的元数据视图（不含正文、不含投票人）。"""
    return {
        "request_id": req["id"],
        "reason": req["reason"],
        "deadline": req["deadline"],
        "earliest_open_at": req["earliest_open_at"],
        "status": req["status"],
        "created_at": req["created_at"],
        "resolved_at": req["resolved_at"],
    }


@app.get("/documents/{doc_id}")
def get_document(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """查询资料。开封前不含正文；开封后返回正文、摘要、理由与见证记录。

    已到清除时刻的资料先被确定性清除：返回 purged 状态与清除时刻，
    保留摘要、参与方与审计元数据，正文永不返回。除此之外只读，不改状态。
    """
    settle_due_purges_for_read(db, doc_id, user)
    doc = load_doc_for_participant(db, doc_id, user)

    witnesses = [
        row["username"]
        for row in db.execute(
            """SELECT u.username FROM document_witnesses w
               JOIN users u ON u.id = w.user_id
               WHERE w.document_id = ? ORDER BY w.position""",
            (doc_id,),
        ).fetchall()
    ]

    result: dict[str, Any] = {
        "document_id": doc_id,
        "status": doc["status"],
        "created_at": doc["created_at"],
        "witnesses": witnesses,
    }

    latest = db.execute(
        "SELECT * FROM open_requests WHERE document_id = ? ORDER BY id DESC LIMIT 1",
        (doc_id,),
    ).fetchone()

    if doc["status"] == "opened":
        result["body"] = doc["body"]            # 开封后才返回正文
        result["digest"] = doc["digest"]
        result["opened_at"] = doc["opened_at"]
        result["purge_at"] = doc["purge_at"]    # 已安排的清除时刻（可能为 null）
        if latest is not None:
            result["open_request"] = _open_request_meta(latest)
            witness_records = db.execute(
                """SELECT u.username, a.bound_digest, a.created_at AS approved_at
                   FROM approvals a JOIN users u ON u.id = a.witness_id
                   WHERE a.request_id = ? ORDER BY a.id""",
                (latest["id"],),
            ).fetchall()
            result["witness_approvals"] = [dict(row) for row in witness_records]
    elif doc["status"] == "purged":
        # 已清除：保留摘要、参与方与审计元数据；任何接口不再返回正文
        result["digest"] = doc["digest"]
        result["purge_at"] = doc["purge_at"]
        result["purged_at"] = doc["purged_at"]
        if doc["opened_at"] is not None:
            result["opened_at"] = doc["opened_at"]
        if latest is not None:
            result["open_request"] = _open_request_meta(latest)
            witness_records = db.execute(
                """SELECT u.username, a.bound_digest, a.created_at AS approved_at
                   FROM approvals a JOIN users u ON u.id = a.witness_id
                   WHERE a.request_id = ? ORDER BY a.id""",
                (latest["id"],),
            ).fetchall()
            result["witness_approvals"] = [dict(row) for row in witness_records]
    else:
        # 开封前：只给当前开封请求的元数据，不给正文，也不给谁已同意
        result["purge_at"] = doc["purge_at"]    # 已安排的清除时刻（可能为 null）
        if latest is not None:
            result["latest_open_request"] = {
                "request_id": latest["id"],
                "reason": latest["reason"],
                "deadline": latest["deadline"],
                "earliest_open_at": latest["earliest_open_at"],
                "status": latest["status"],
                "created_at": latest["created_at"],
                "approval_count": db.execute(
                    "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?",
                    (latest["id"],),
                ).fetchone()["c"],
            }
    return result


# ---------------------------------------------------------------------------
# 开封请求
# ---------------------------------------------------------------------------


@app.post("/documents/{doc_id}/open-requests", status_code=201)
def create_open_request(
    doc_id: int,
    payload: OpenRequestIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以提交开封请求")

    deadline = utc_str(payload.deadline)
    earliest = utc_str(payload.earliest_open_at) if payload.earliest_open_at else None
    now = utc_str(now_utc())
    if deadline <= now:
        raise err(400, "截止时刻必须晚于当前时间")
    if earliest is not None and earliest < now:
        raise err(400, "最早开封时刻不能早于当前时间")

    # 清除若在本临界区内生效，清除先落库，拒绝在提交后再返回（与同意/撤销一致）
    post_commit_error: HTTPException | None = None
    request_id: int | None = None
    with authed_write_tx(db, user) as tx:
        # 锁内先处理到点的定时开封与到期清除，避免与同意/撤销/扫描并发
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] == "purged":
            post_commit_error = err(409, "资料已清除，不能提交开封请求")
        elif doc["status"] != "sealed":
            raise err(409, "资料已开封，重复开封被拒绝")
        active = None if post_commit_error else tx.execute(
            "SELECT id, deadline FROM open_requests"
            " WHERE document_id = ? AND status IN ('pending', 'waiting')",
            (doc_id,),
        ).fetchone()
        if active is not None:
            if active["deadline"] <= now:
                # 理论上扫描器很快会处理；在同一临界区内顺手确定性失效
                tx.execute(
                    "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                    (now, active["id"]),
                )
                add_audit(
                    tx,
                    actor_id=None,
                    action="request_expired",
                    document_id=doc_id,
                    request_id=active["id"],
                    detail="旧开封请求已过截止时刻，提交新请求时确认失效，旧同意作废",
                )
            else:
                raise err(409, "已有进行中的开封请求，请先撤销或等待其到期")

        if post_commit_error is None:
            cur = tx.execute(
                "INSERT INTO open_requests (document_id, reason, deadline, status, created_at, earliest_open_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                (doc_id, payload.reason, deadline, now, earliest),
            )
            request_id = cur.lastrowid
            timing = f"最早开封时刻: {earliest}" if earliest else "未指定最早开封时刻，两票集齐即开封"
            add_audit(
                tx,
                actor_id=user["id"],
                action="open_request_created",
                document_id=doc_id,
                request_id=request_id,
                detail=f"提交开封请求，理由: {payload.reason}；截止时刻: {deadline}；{timing}",
            )

    if post_commit_error is not None:
        raise post_commit_error
    assert request_id is not None
    return {
        "request_id": request_id,
        "document_id": doc_id,
        "status": "pending",
        "reason": payload.reason,
        "deadline": deadline,
        "earliest_open_at": earliest,
    }


@app.post("/open-requests/{request_id}/approve")
def approve_open_request(
    request_id: int,
    payload: ApproveIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """见证人同意：必须是该资料的三名指定见证人之一；摘要须与封存摘要一致。

    所有判定均在写事务内完成；任何拒绝路径都不写入数据（状态不变）。
    """
    meta = db.execute(
        """SELECT r.id AS request_id, r.document_id, r.status AS req_status,
                  r.deadline, r.earliest_open_at, d.status AS doc_status, d.digest
           FROM open_requests r JOIN documents d ON d.id = r.document_id
           WHERE r.id = ?""",
        (request_id,),
    ).fetchone()
    if meta is None:
        raise err(403, "无权操作该请求或请求不存在")
    is_witness = db.execute(
        "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
        (meta["document_id"], user["id"]),
    ).fetchone()
    if is_witness is None:
        raise err(403, "只有指定见证人可以同意")

    now = utc_str(now_utc())
    # 到点开封/到期清除必须先落库：若本请求因此开封（或资料因此清除）而本次
    # 同意仍需拒绝，要等事务提交后再抛错，绝不能让回滚把已经生效的判定撤销。
    post_commit_error: HTTPException | None = None
    response: dict[str, Any] | None = None
    try:
        with authed_write_tx(db, user) as tx:
            # 临界区第一步：到点的 waiting 请求必开封、到清除时刻的资料必清除；
            # 之后任何状态判定都以此为准，因此定时处理即使被后台延迟，
            # 也不会被并发同意/撤销/扫描作废。
            due_opened = resolve_due_waiting(tx, now, request_id) > 0
            purged_here = purge_due_documents(tx, now, meta["document_id"]) > 0
            req = tx.execute(
                "SELECT * FROM open_requests WHERE id = ?", (request_id,)
            ).fetchone()
            doc = tx.execute(
                "SELECT * FROM documents WHERE id = ?", (req["document_id"],)
            ).fetchone()

            if doc["status"] != "sealed":
                if doc["status"] == "purged":
                    not_sealed = err(409, "资料已清除，同意被拒绝")
                else:
                    not_sealed = err(409, "资料已开封，重复开封被拒绝")
                if due_opened or purged_here:
                    post_commit_error = not_sealed
                else:
                    raise not_sealed
            elif req["status"] == "waiting":
                raise err(409, "两票已集齐，待到达最早开封时刻自动开封，同意被拒绝")
            elif req["status"] != "pending":
                raise err(409, f"开封请求当前状态为 {req['status']}，同意被拒绝")
            elif req["deadline"] <= now:
                raise err(409, "已过截止时刻，同意被拒绝，请求即将失效")
            elif not tx.execute(
                "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
                (doc["id"], user["id"]),
            ).fetchone():
                # 见证人名单可在资料未开封时被封存人替换：进入写临界区后必须以
                # “当前名单”为准复核，防止“预校验通过后在锁外被替换”的旧见证人投票。
                raise err(403, "只有当前指定见证人可以同意")
            elif payload.digest != doc["digest"]:
                # 不同意即不绑定本次请求：不落库
                raise err(409, "提交的摘要与封存时摘要不一致，拒绝同意")
            else:
                already = tx.execute(
                    "SELECT 1 FROM approvals WHERE request_id = ? AND witness_id = ?",
                    (request_id, user["id"]),
                ).fetchone()
                if already is not None:
                    raise err(409, "你已同意本次请求，重复提交只计一次")

                tx.execute(
                    "INSERT INTO approvals (request_id, witness_id, bound_digest, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (request_id, user["id"], doc["digest"], now),
                )
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="approval",
                    document_id=doc["id"],
                    request_id=request_id,
                    detail="见证人同意开封，已绑定本次请求与封存摘要",
                )

                count = tx.execute(
                    "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?", (request_id,)
                ).fetchone()["c"]

                new_status = "pending"
                if count >= 2:
                    # 第二名不同见证人到位。UNIQUE(request_id, witness_id)
                    # 保证两名同意必然来自不同见证人。
                    earliest = req["earliest_open_at"]
                    if earliest is None or earliest <= now:
                        # 未设最早开封时刻（集齐即开），或已到最早开封时刻：立即开封
                        open_request_now(
                            tx,
                            req_id=request_id,
                            doc_id=doc["id"],
                            now=now,
                            actor_id=user["id"],
                            detail="两名不同指定见证人均已同意，资料正式开封",
                        )
                        new_status = "opened"
                    else:
                        # 两票在最早开封时刻前集齐：只进入待开封，到点自动开封
                        tx.execute(
                            "UPDATE open_requests SET status='waiting' WHERE id=?",
                            (request_id,),
                        )
                        add_audit(
                            tx,
                            actor_id=user["id"],
                            action="request_waiting",
                            document_id=doc["id"],
                            request_id=request_id,
                            detail=f"两名不同指定见证人均已同意，待最早开封时刻 {earliest} 自动开封",
                        )
                        new_status = "waiting"

                response = {
                    "request_id": request_id,
                    "document_id": doc["id"],
                    "approval_count": count,
                    "required": 2,
                    "status": new_status,
                }
                if req["earliest_open_at"] is not None:
                    response["earliest_open_at"] = req["earliest_open_at"]
        if post_commit_error is not None:
            raise post_commit_error
        assert response is not None
        return response
    except sqlite3.IntegrityError:
        # 唯一约束兜底（并发重复同意）
        raise err(409, "你已同意本次请求，重复提交只计一次")


@app.post("/open-requests/{request_id}/revoke")
def revoke_open_request(
    request_id: int,
    payload: RevokeIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    meta = db.execute(
        """SELECT r.id, r.document_id, r.status, r.deadline, d.owner_id
           FROM open_requests r JOIN documents d ON d.id = r.document_id
           WHERE r.id = ?""",
        (request_id,),
    ).fetchone()
    if meta is None:
        raise err(403, "无权操作该请求或请求不存在")
    if meta["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以撤销开封请求")

    now = utc_str(now_utc())
    # 与同意相同：到点开封/到期清除先提交，拒绝（撤销已随开封/清除失效的请求）
    # 在提交后再返回。
    post_commit_error: HTTPException | None = None
    with authed_write_tx(db, user) as tx:
        due_opened = resolve_due_waiting(tx, now, request_id) > 0
        purged_here = purge_due_documents(tx, now, meta["document_id"]) > 0
        req = tx.execute("SELECT * FROM open_requests WHERE id = ?", (request_id,)).fetchone()
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (req["document_id"],)).fetchone()
        if doc["status"] != "sealed":
            if doc["status"] == "purged":
                not_sealed = err(409, "资料已清除，无法撤销")
            else:
                not_sealed = err(409, "资料已开封，无法撤销")
            if due_opened or purged_here:
                post_commit_error = not_sealed
            else:
                raise not_sealed
        elif req["status"] not in ("pending", "waiting"):
            raise err(409, f"开封请求当前状态为 {req['status']}，无法撤销")
        elif req["deadline"] <= now:
            raise err(409, "已过截止时刻，不能撤销，请求即将失效")
        else:
            waiting_note = "（请求已集齐两票，待到点开封）" if req["status"] == "waiting" else ""
            tx.execute(
                "UPDATE open_requests SET status='revoked', resolved_at=? WHERE id=?",
                (now, request_id),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="revoked",
                document_id=req["document_id"],
                request_id=request_id,
                detail=f"封存人撤销开封请求{waiting_note}，已有同意全部作废。原因: {payload.reason or '（未填写）'}",
            )
    if post_commit_error is not None:
        raise post_commit_error
    return {"request_id": request_id, "status": "revoked"}


# ---------------------------------------------------------------------------
# 见证人替换（资料未开封）
#
# 封存人可在资料未开封时把一名指定见证人换成新见证人。新见证人必须已注册、
# 非封存人、且不在当前名单；替换后仍是三名互不相同的见证人。与同意/撤销
# 相同：临界区内先做确定性到点开封——已到最早开封时刻且两票已齐的请求会
# 先开封，本次替换随即被拒绝；已过截止时刻的进行中请求按原规则先失效，
# 再完成替换。被替换者对进行中请求的同意立即删除（失效），其他同意保留，
# 有效票不足两票时 waiting 退回 pending；旧见证人立刻失去访问与同意权限。
# ---------------------------------------------------------------------------


@app.post("/documents/{doc_id}/witness-replacements")
def replace_witness(
    doc_id: int,
    payload: ReplaceWitnessIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以替换见证人")

    now = utc_str(now_utc())
    post_commit_error: HTTPException | None = None
    result: dict[str, Any] = {}
    with authed_write_tx(db, user) as tx:
        # 临界区第一步与其他写路径一致：先确定性到点开封与到期清除。
        # 用全局解析：与扫描器等价，顺带把已到点的 waiting 请求开封、
        # 到清除时刻的资料清除。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] != "sealed":
            # 资料已开封/已清除即不允许替换：无论状态变更是本临界区刚触发的
            # 到点判定，还是并发事务在更早时完成的，结论一致。
            # 若为前者，变更必须先随本事务提交，故错误延迟到提交后再抛出。
            if doc["status"] == "purged":
                post_commit_error = err(409, "资料已清除，无法替换见证人")
            else:
                post_commit_error = err(409, "资料已开封，无法替换见证人")
        else:
            old_row = tx.execute(
                """SELECT w.id, w.user_id, u.username FROM document_witnesses w
                   JOIN users u ON u.id = w.user_id
                   WHERE w.document_id = ? AND u.username = ?""",
                (doc_id, payload.old_witness),
            ).fetchone()
            if old_row is None:
                raise err(404, "被替换者不在当前见证人名单中")

            new_user = tx.execute(
                "SELECT id, username FROM users WHERE username = ? AND status='active'",
                (payload.new_witness,),
            ).fetchone()
            if new_user is None:
                # 未注册与已停用账号按现有“无效参与人”错误同一处理：不暴露停用状态
                raise err(400, "新见证人尚未注册")
            if new_user["id"] == user["id"]:
                raise err(400, "封存人不能同时担任见证人")
            already = tx.execute(
                "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
                (doc_id, new_user["id"]),
            ).fetchone()
            if already is not None:
                raise err(409, "新见证人已在当前见证人名单中")
            # 待确认交接邀请的受让人不能成为见证人：交接规则要求受让人始终
            # 不是当前见证人，否则确认后会出现“新封存人同时是见证人”。
            pending_assignee = tx.execute(
                "SELECT 1 FROM handover_invitations"
                " WHERE document_id = ? AND status = 'pending' AND to_user_id = ?",
                (doc_id, new_user["id"]),
            ).fetchone()
            if pending_assignee is not None:
                raise err(409, "该账号是待确认交接邀请的受让人，不能同时成为见证人")

            # 进行中的开封请求（pending/waiting）。已过截止时刻的 pending
            # 请求按原规则在同一临界区内先确定性失效，再完成替换。
            active = tx.execute(
                "SELECT * FROM open_requests"
                " WHERE document_id = ? AND status IN ('pending', 'waiting')"
                " ORDER BY id DESC LIMIT 1",
                (doc_id,),
            ).fetchone()
            expired_id: int | None = None
            if active is not None and active["status"] == "pending" and active["deadline"] <= now:
                tx.execute(
                    "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                    (now, active["id"]),
                )
                add_audit(
                    tx,
                    actor_id=None,
                    action="request_expired",
                    document_id=doc_id,
                    request_id=active["id"],
                    detail="替换见证人时确认开封请求已过截止时刻，请求按原规则失效，已有同意作废",
                )
                expired_id = active["id"]
                active = None

            # 替换名单：新见证人占据被替换者的位置，三名互异不变。
            tx.execute(
                "UPDATE document_witnesses SET user_id = ? WHERE id = ?",
                (new_user["id"], old_row["id"]),
            )

            details = [
                f"封存人替换见证人：{payload.old_witness} -> {payload.new_witness}",
                "新见证人已注册且不在原名单，替换后仍为三名互不相同见证人",
            ]

            req_info: dict[str, Any] | None = None
            if expired_id is not None:
                req_info = {
                    "request_id": expired_id,
                    "status": "expired",
                    "approval_count": 0,
                }
                details.append(f"进行中的开封请求 {expired_id} 已过截止时刻，按原规则失效")
            elif active is not None:
                removed = tx.execute(
                    "DELETE FROM approvals WHERE request_id = ? AND witness_id = ?",
                    (active["id"], old_row["user_id"]),
                ).rowcount
                if removed:
                    details.append(
                        f"被替换者 {payload.old_witness} 对开封请求 {active['id']} 的同意已失效"
                    )

                count = tx.execute(
                    "SELECT COUNT(*) AS c FROM approvals WHERE request_id = ?",
                    (active["id"],),
                ).fetchone()["c"]
                req_status = active["status"]
                if req_status == "waiting" and count < 2:
                    # 被替换者带走一票后有效票不足两票：待开封退回待同意，
                    # 新见证人可凭封存摘要为本次请求补票。
                    tx.execute(
                        "UPDATE open_requests SET status='pending' WHERE id=?",
                        (active["id"],),
                    )
                    req_status = "pending"
                    details.append(
                        f"开封请求 {active['id']} 有效票不足两票，由 waiting 退回 pending，"
                        "新见证人可凭封存摘要补票"
                    )
                else:
                    details.append("其他见证人对本次请求的同意保留")

                req_info = {
                    "request_id": active["id"],
                    "status": req_status,
                    "approval_count": count,
                }

            # 通知收件人在状态变更的同一事务内固定：封存人、旧见证人、
            # 新见证人。新见证人此刻已在三名名单中，旧见证人已不在名单，
            # 故取“旧名单 + 新见证人”（即封存人 + 新旧见证人并集）。
            event_seq = add_audit(
                tx,
                actor_id=user["id"],
                action="witness_replaced",
                document_id=doc_id,
                request_id=req_info["request_id"] if req_info else None,
                detail="；".join(details),
            )
            recipients = participant_ids(tx, doc_id, user["id"]) | {old_row["user_id"]}
            fanout_notifications(
                tx,
                recipients=recipients,
                document_id=doc_id,
                event_seq=event_seq,
                event_type="witness_replaced",
                at=now,
            )

            witnesses = [
                row["username"]
                for row in tx.execute(
                    """SELECT u.username FROM document_witnesses w
                       JOIN users u ON u.id = w.user_id
                       WHERE w.document_id = ? ORDER BY w.position""",
                    (doc_id,),
                ).fetchall()
            ]
            result = {
                "document_id": doc_id,
                "status": "sealed",
                "witnesses": witnesses,
                "open_request": req_info,
            }

    if post_commit_error is not None:
        raise post_commit_error
    return result


# ---------------------------------------------------------------------------
# 封存人交接（资料未开封）
#
# 封存人可就未开封资料向另一名本地账号发起交接邀请，填写受让人账号与带时区
# 的确认截止时刻。受让人须已注册、不是封存人本人、不是当前见证人；每份资料
# 同时至多一份待确认且未过期的邀请（DB 部分唯一索引 + 事务内预检双保险）。
# 封存人可撤回待确认邀请；受让人须在截止前凭本人 X-API-Key 确认。
#
# 确认结算顺序（全部在同一写临界区内）：
#   1) 确定性到点开封（resolve_due_waiting）与开封请求到期判定；
#   2) 资料已开封 -> 拒绝交接（开封若为本临界区触发则先提交再报错）；
#   3) 把进行中的开封请求置为 invalidated，并删除其全部旧同意（永久失效）；
#   4) 转移 documents.owner_id；见证名单、正文、摘要一律不变；
#   5) 原封存人失去该资料查看/操作权限，新封存人可重新申请开封。
# 已撤回/逾期/已确认/资料已开封的邀请一律不能确认；逾期或撤回后可重新邀请。
# 开封前任何响应（含全部错误体）都不返回正文。
# ---------------------------------------------------------------------------


def serialize_invitation(tx: sqlite3.Connection, inv: sqlite3.Row) -> dict[str, Any]:
    """组装邀请状态；只含账号名与状态/时刻等元数据，绝不含正文或摘要。"""
    from_user = tx.execute(
        "SELECT username FROM users WHERE id = ?", (inv["from_user_id"],)
    ).fetchone()["username"]
    to_user = tx.execute(
        "SELECT username FROM users WHERE id = ?", (inv["to_user_id"],)
    ).fetchone()["username"]
    return {
        "invitation_id": inv["id"],
        "document_id": inv["document_id"],
        "from_username": from_user,
        "to_username": to_user,
        "confirm_deadline": inv["confirm_deadline"],
        "status": inv["status"],
        "created_at": inv["created_at"],
        "resolved_at": inv["resolved_at"],
    }


@app.post("/documents/{doc_id}/handover-invitations", status_code=201)
def create_handover_invitation(
    doc_id: int,
    payload: HandoverIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以发起交接")

    deadline = utc_str(payload.confirm_deadline)
    now = utc_str(now_utc())
    if deadline <= now:
        raise err(400, "确认截止时刻必须晚于当前时间")

    post_commit_error: HTTPException | None = None
    invitation: dict[str, Any] | None = None
    try:
        with authed_write_tx(db, user) as tx:
            # 临界区第一步与其他写路径一致：先确定性到点开封与到期清除
            # （开封/清除会连带使既有待确认邀请失效，必须先落库）。
            resolve_due_waiting(tx, now)
            purge_due_documents(tx, now)
            expire_due_invitations(tx, now)

            doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if doc["status"] != "sealed":
                # 开封/清除若为本临界区刚触发，必须先提交，错误延迟到提交后抛出。
                if doc["status"] == "purged":
                    post_commit_error = err(409, "资料已清除，不能发起交接")
                else:
                    post_commit_error = err(409, "资料已开封，不能发起交接")
            else:
                if doc["owner_id"] != user["id"]:
                    raise err(403, "只有封存人可以发起交接")

                to_user = tx.execute(
                    "SELECT id, username FROM users WHERE username = ? AND status='active'",
                    (payload.to_username,),
                ).fetchone()
                if to_user is None:
                    # 未注册与已停用账号按现有“无效参与人”错误同一处理：不暴露停用状态
                    raise err(400, "受让人尚未注册")
                if to_user["id"] == user["id"]:
                    raise err(400, "受让人不能是封存人本人")
                is_witness = tx.execute(
                    "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
                    (doc_id, to_user["id"]),
                ).fetchone()
                if is_witness is not None:
                    raise err(400, "受让人不能是当前见证人")

                active = tx.execute(
                    "SELECT id FROM handover_invitations"
                    " WHERE document_id = ? AND status = 'pending'",
                    (doc_id,),
                ).fetchone()
                if active is not None:
                    raise err(409, "已有待确认且未过期的交接邀请，请先撤回或等待其逾期")

                cur = tx.execute(
                    "INSERT INTO handover_invitations"
                    " (document_id, from_user_id, to_user_id, confirm_deadline, status, created_at)"
                    " VALUES (?, ?, ?, ?, 'pending', ?)",
                    (doc_id, user["id"], to_user["id"], deadline, now),
                )
                invitation_id = cur.lastrowid
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="handover_invited",
                    document_id=doc_id,
                    detail=(
                        f"封存人向 {payload.to_username} 发起交接邀请 {invitation_id}，"
                        f"确认截止时刻: {deadline}"
                    ),
                )
                inv = tx.execute(
                    "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
                ).fetchone()
                invitation = serialize_invitation(tx, inv)
        if post_commit_error is not None:
            raise post_commit_error
        assert invitation is not None
        return invitation
    except sqlite3.IntegrityError:
        # 部分唯一索引兜底（并发重复邀请）：每份资料至多一份待确认邀请
        raise err(409, "已有待确认且未过期的交接邀请，请先撤回或等待其逾期")


def _load_invitation_for_owner(
    db: sqlite3.Connection, invitation_id: int, user: AuthUser
) -> sqlite3.Row:
    """取邀请并校验“当前封存人”身份；其他情况统一 403，不区分是否存在。"""
    inv = db.execute(
        "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
    ).fetchone()
    if inv is None:
        raise err(403, "无权操作该邀请或邀请不存在")
    doc = db.execute(
        "SELECT * FROM documents WHERE id = ?", (inv["document_id"],)
    ).fetchone()
    if doc is None or doc["owner_id"] != user["id"] or inv["from_user_id"] != user["id"]:
        raise err(403, "只有发起交接的封存人可以操作该邀请")
    return inv


@app.post("/handover-invitations/{invitation_id}/revoke")
def revoke_handover_invitation(
    invitation_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    _load_invitation_for_owner(db, invitation_id, user)

    now = utc_str(now_utc())
    # 与同意/撤销开封请求相同：若开封/清除在本临界区内被确定性触发，
    # 状态变更先提交，撤回失败在提交后再返回。
    post_commit_error: HTTPException | None = None
    invitation: dict[str, Any] | None = None
    with authed_write_tx(db, user) as tx:
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)
        expired_here = expire_due_invitations(tx, now, invitation_id) > 0

        inv = tx.execute(
            "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
        ).fetchone()
        doc = tx.execute(
            "SELECT * FROM documents WHERE id = ?", (inv["document_id"],)
        ).fetchone()
        if doc["owner_id"] != user["id"] or inv["from_user_id"] != user["id"]:
            raise err(403, "只有发起交接的封存人可以撤回该邀请")
        if doc["status"] != "sealed":
            if doc["status"] == "purged":
                post_commit_error = err(409, "资料已清除，邀请已失效，无法撤回")
            else:
                post_commit_error = err(409, "资料已开封，邀请已失效，无法撤回")
        elif expired_here or inv["status"] == "expired":
            post_commit_error = err(409, "交接邀请已逾期，不能撤回（可重新邀请）")
        elif inv["status"] == "confirmed":
            raise err(409, "交接已完成，不能撤回")
        elif inv["status"] == "revoked":
            raise err(409, "交接邀请已撤回")
        else:
            tx.execute(
                "UPDATE handover_invitations SET status='revoked', resolved_at=? WHERE id=?",
                (now, invitation_id),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="handover_revoked",
                document_id=inv["document_id"],
                detail=f"封存人撤回交接邀请 {invitation_id}",
            )
            inv = tx.execute(
                "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            invitation = serialize_invitation(tx, inv)
    if post_commit_error is not None:
        raise post_commit_error
    assert invitation is not None
    return invitation


@app.post("/handover-invitations/{invitation_id}/confirm")
def confirm_handover(
    invitation_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """受让人凭本人 X-API-Key 在截止前确认交接。

    成功后返回邀请状态、当前封存人、当前见证名单与旧开封请求结果；
    任何拒绝路径都不转移身份、不泄露正文。
    """
    pre = db.execute(
        "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
    ).fetchone()
    if pre is None:
        raise err(403, "无权操作该邀请或邀请不存在")
    if pre["to_user_id"] != user["id"]:
        # 发起方、见证人、无关账号一律不可确认，且不区分邀请是否存在
        raise err(403, "只有受让人本人可以确认交接")

    now = utc_str(now_utc())
    post_commit_error: HTTPException | None = None
    response: dict[str, Any] | None = None
    with authed_write_tx(db, user) as tx:
        # 结算顺序第 1 步：到点开封 / 到期清除 / 邀请逾期全部确定性落库。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)
        expire_due_invitations(tx, now)

        inv = tx.execute(
            "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
        ).fetchone()
        doc = tx.execute(
            "SELECT * FROM documents WHERE id = ?", (inv["document_id"],)
        ).fetchone()

        # 先锁定当前进行中（pending/waiting）的开封请求（若有），后续结算以它为准。
        active = tx.execute(
            "SELECT * FROM open_requests"
            " WHERE document_id = ? AND status IN ('pending', 'waiting')"
            " ORDER BY id DESC LIMIT 1",
            (doc["id"],),
        ).fetchone()

        # 与提交开封请求/替换见证人路径一致：已过截止时刻仍 pending 的
        # 旧请求先按原规则确定性失效（其同意随后面的交接结算一并删除）。
        if active is not None and active["status"] == "pending" and active["deadline"] <= now:
            tx.execute(
                "UPDATE open_requests SET status='expired', resolved_at=? WHERE id=?",
                (now, active["id"]),
            )
            add_audit(
                tx,
                actor_id=None,
                action="request_expired",
                document_id=doc["id"],
                request_id=active["id"],
                detail="确认交接时确认开封请求已过截止时刻，请求按原规则失效，已有同意作废",
            )

        if inv["to_user_id"] != user["id"]:
            raise err(403, "只有受让人本人可以确认交接")
        if doc["status"] != "sealed":
            # 开封/清除（含本临界区刚触发的到点判定）后拒绝交接；状态变更必须先随
            # 本事务提交，故错误延迟到提交后再返回（与同意/替换路径一致）。
            if doc["status"] == "purged":
                post_commit_error = err(409, "资料已清除，交接被拒绝")
            else:
                post_commit_error = err(409, "资料已开封，交接被拒绝")
        elif inv["status"] == "expired":
            # 本临界区刚判定逾期也要先提交，故延迟到提交后再报错
            post_commit_error = err(409, "交接邀请已逾期，不能确认（请封存人重新邀请）")
        elif inv["status"] == "revoked":
            raise err(409, "交接邀请已撤回，不能确认")
        elif inv["status"] == "confirmed":
            raise err(409, "交接邀请已确认")
        elif inv["confirm_deadline"] <= now:
            # 锁内兜底：即便上面的统一逾期处理未命中（理论上不会），也绝不超期确认
            post_commit_error = err(409, "已过确认截止时刻，不能确认")
        else:
            # 结算顺序第 2 步：进行中的开封请求失效（过截止的上面已置 expired），
            # 旧同意删除。
            old_request_info: dict[str, Any] | None = None
            if active is not None:
                if active["deadline"] <= now:
                    # 上面已按原规则把过截止时刻的 pending 请求置 expired
                    old_status = "expired"
                else:
                    tx.execute(
                        "UPDATE open_requests SET status='invalidated', resolved_at=? WHERE id=?",
                        (now, active["id"]),
                    )
                    add_audit(
                        tx,
                        actor_id=user["id"],
                        action="request_invalidated",
                        document_id=doc["id"],
                        request_id=active["id"],
                        detail="封存人交接确认：进行中的开封请求失效，其全部旧同意作废，新封存人可重新申请开封",
                    )
                    old_status = "invalidated"
                tx.execute(
                    "DELETE FROM approvals WHERE request_id = ?", (active["id"],)
                )
                old_request_info = {
                    "request_id": active["id"],
                    "status": old_status,
                    "approval_count": 0,
                }

            # 该资料其他待确认邀请（部分唯一索引保证至多一份，这里防御性处理）
            # 一并失效；正在确认的本邀请下一步置为 confirmed，不能误判为逾期。
            invalidate_pending_invitations(
                tx,
                doc_id=doc["id"],
                now=now,
                exclude_id=invitation_id,
                detail="封存人交接已完成，其他待确认邀请失效",
            )

            old_owner_row = tx.execute(
                "SELECT id, username FROM users WHERE id = ?", (doc["owner_id"],)
            ).fetchone()
            old_owner = old_owner_row["username"]

            # 结算顺序第 3 步：转移封存人身份；名单、正文、摘要不变。
            tx.execute(
                "UPDATE documents SET owner_id = ? WHERE id = ?",
                (user["id"], doc["id"]),
            )
            tx.execute(
                "UPDATE handover_invitations SET status='confirmed', resolved_at=? WHERE id=?",
                (now, invitation_id),
            )
            event_seq = add_audit(
                tx,
                actor_id=user["id"],
                action="handover_confirmed",
                document_id=doc["id"],
                detail=(
                    f"封存人交接完成：{old_owner} -> {user['username']}；"
                    "见证名单、正文与摘要不变；原封存人失去该资料权限"
                ),
            )
            # 收件人在身份转移的同一事务内固定：新封存人（受让人）、旧封存人
            # 与当前三名见证人。转移后 participant_ids 取到新封存人 + 见证人，
            # 并集补上旧封存人编号。
            recipients = participant_ids(tx, doc["id"], user["id"]) | {old_owner_row["id"]}
            fanout_notifications(
                tx,
                recipients=recipients,
                document_id=doc["id"],
                event_seq=event_seq,
                event_type="handover_confirmed",
                at=now,
            )

            witnesses = [
                row["username"]
                for row in tx.execute(
                    """SELECT u.username FROM document_witnesses w
                       JOIN users u ON u.id = w.user_id
                       WHERE w.document_id = ? ORDER BY w.position""",
                    (doc["id"],),
                ).fetchall()
            ]
            inv = tx.execute(
                "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
            ).fetchone()
            response = {
                **serialize_invitation(tx, inv),
                "document_id": doc["id"],
                "document_status": "sealed",
                "current_owner": user["username"],
                "previous_owner": old_owner,
                "witnesses": witnesses,
                "old_open_request": old_request_info,
            }
    if post_commit_error is not None:
        raise post_commit_error
    assert response is not None
    return response


@app.get("/handover-invitations/{invitation_id}")
def get_handover_invitation(
    invitation_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """查询邀请状态：邀请双方或该资料当前参与方（封存人/见证人）可见；只含元数据。

    资料到清除时刻的，先确定性清除（待确认邀请随之失效），再返回最新状态。
    """
    inv = db.execute(
        "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
    ).fetchone()
    if inv is None:
        raise err(403, "无权操作该邀请或邀请不存在")
    settle_due_purges_for_read(db, inv["document_id"], user)
    inv = db.execute(
        "SELECT * FROM handover_invitations WHERE id = ?", (invitation_id,)
    ).fetchone()
    is_party = user["id"] in (inv["from_user_id"], inv["to_user_id"])
    is_participant = False
    doc = db.execute(
        "SELECT owner_id FROM documents WHERE id = ?", (inv["document_id"],)
    ).fetchone()
    if doc is not None:
        if doc["owner_id"] == user["id"]:
            is_participant = True
        else:
            is_participant = db.execute(
                "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
                (inv["document_id"], user["id"]),
            ).fetchone() is not None
    if not is_party and not is_participant:
        raise err(403, "无权操作该邀请或邀请不存在")
    return serialize_invitation(db, inv)


# ---------------------------------------------------------------------------
# 到期清除正文（资料封存人专享）
#
# 封存人凭本人密钥为资料安排带时区的未来清除时刻；到期前可改期（再次 PUT）
# 或取消（DELETE）。交接后清除安排保留，改由新封存人管理（原封存人 403）。
# 到点判定与开封/交接共用同一把写锁：清除若在本临界区内生效，清除先落库，
# 改期/取消的拒绝在提交后再返回；已到期的改期或取消一律 409，且不泄露正文。
# ---------------------------------------------------------------------------


@app.put("/documents/{doc_id}/purge")
def schedule_purge(
    doc_id: int,
    payload: PurgeIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """安排或改期到期清除时刻。已开封资料同样可安排（到点照常清除正文）。"""
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以安排到期清除")

    purge_at = utc_str(payload.purge_at)
    now = utc_str(now_utc())
    if purge_at <= now:
        raise err(400, "清除时刻必须晚于当前时间")

    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with authed_write_tx(db, user) as tx:
        # 临界区第一步与其他写路径一致：先确定性到点开封与到期清除。
        # 清除时刻已到的资料在此生效（purged），随后的改期请求随即被拒。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)

        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] == "purged":
            # 清除可能刚在本临界区生效：必须先提交，错误延迟到提交后抛出
            post_commit_error = err(409, "资料已清除，不能再安排或改期清除")
        else:
            previous = doc["purge_at"]
            tx.execute(
                "UPDATE documents SET purge_at = ? WHERE id = ?",
                (purge_at, doc_id),
            )
            if previous is None:
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="purge_scheduled",
                    document_id=doc_id,
                    detail=f"封存人安排到期清除，清除时刻: {purge_at}",
                )
            else:
                add_audit(
                    tx,
                    actor_id=user["id"],
                    action="purge_rescheduled",
                    document_id=doc_id,
                    detail=f"封存人改期到期清除：{previous} -> {purge_at}",
                )
            result = {
                "document_id": doc_id,
                "status": doc["status"],
                "purge_at": purge_at,
                "previous_purge_at": previous,
            }
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


@app.delete("/documents/{doc_id}/purge")
def cancel_purge(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """在清除时刻到来前取消已安排的到期清除。"""
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以取消到期清除")

    now = utc_str(now_utc())
    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with authed_write_tx(db, user) as tx:
        # 与改期相同：先确定性到点开封与到期清除；清除时刻已到的资料在此
        # 生效，取消请求随即被拒（已到期不能取消）。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)

        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["status"] == "purged":
            post_commit_error = err(409, "资料已清除，不能取消清除")
        elif doc["purge_at"] is None:
            raise err(409, "当前没有已安排的清除时刻")
        else:
            previous = doc["purge_at"]
            tx.execute(
                "UPDATE documents SET purge_at = NULL WHERE id = ?",
                (doc_id,),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="purge_canceled",
                document_id=doc_id,
                detail=f"封存人取消到期清除（原清除时刻: {previous}）",
            )
            result = {"document_id": doc_id, "status": doc["status"], "purge_at": None}
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# 一次性阅览凭证（view pass）
#
# 当前封存人把**已开封**资料的正文临时交给无账号的外部复核人：
#   1) 签发 POST /documents/{id}/view-passes：凭资料编号 + 带时区阅览截止时刻；
#      截止时刻须晚于当前、且严格早于已安排的清除时刻（未安排清除不能签发）。
#      凭证明文为 256 位不可猜测随机串，库内只存 SHA-256 摘要；明文仅在本次
#      响应中返回，响应同时给出供撤销使用的凭证编号（pass_id）。
#   2) 兑换 POST /view-passes/redeem：无需任何账号凭据，凭明文在截止前读取一次
#      正文与摘要。兑换临界区先在同一事务内结算到期清除——清除时刻与兑换同刻
#      时清除优先（先提交清除，再拒绝兑换）；随后以条件更新把 active 凭证原子
#      置为 redeemed，并发兑换至多一次成功。过期/撤销/已兑付/无效/已清除统一
#      404 且文案相同，不泄露正文或资料存在性；拒绝路径不写任何记录。
#   3) 撤销 POST /view-passes/{id}/revoke：当前封存人凭凭证编号在兑换前撤销。
# 签发、成功兑换、撤销均追加到原有 audit_log 审计链（兑换的 actor 为空，
# 因为外部复核人没有账号）。
# ---------------------------------------------------------------------------

# 兑换类失败统一文案与状态码：调用方无法据此区分凭证是否存在、资料是否存在
VIEW_PASS_REJECTED = 404
VIEW_PASS_REJECTED_MSG = "凭证无效、已过期、已撤销、已兑付，或资料已清除"


@app.post("/documents/{doc_id}/view-passes", status_code=201)
def issue_view_pass(
    doc_id: int,
    payload: ViewPassIn,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """封存人为已开封资料签发一次性阅览凭证。"""
    doc = load_doc_for_participant(db, doc_id, user)
    if doc["owner_id"] != user["id"]:
        raise err(403, "只有封存人可以签发阅览凭证")

    expires_at = utc_str(payload.expires_at)
    now = utc_str(now_utc())
    if expires_at <= now:
        raise err(400, "阅览截止时刻必须晚于当前时间")

    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with authed_write_tx(db, user) as tx:
        # 临界区第一步与其他写路径一致：先确定性到点开封，再结算到期清除，
        # 避免签发与定时开封/清除赛跑（同刻时清除优先由 resolve_due_waiting
        # 的 purge_at 比较保证；清除若在本临界区生效，先提交，再拒绝签发）。
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now, doc_id)

        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if doc["owner_id"] != user["id"]:
            raise err(403, "只有封存人可以签发阅览凭证")
        if doc["status"] == "purged":
            # 清除可能刚在本临界区生效：先提交，错误延迟到提交后返回
            post_commit_error = err(409, "资料已清除，不能签发阅览凭证")
        elif doc["status"] != "opened":
            raise err(409, "资料尚未开封，只能为已开封资料签发阅览凭证")
        elif doc["purge_at"] is None:
            raise err(409, "资料尚未安排到期清除，不能签发阅览凭证")
        elif not expires_at < doc["purge_at"]:
            raise err(400, "阅览截止时刻必须早于已安排的清除时刻")
        else:
            # 256 位不可猜测随机串；库内只存 SHA-256 摘要，明文仅此一次返回
            token = secrets.token_urlsafe(32)
            cur = tx.execute(
                "INSERT INTO view_passes"
                " (document_id, token_hash, status, expires_at, issued_by, created_at)"
                " VALUES (?, ?, 'active', ?, ?, ?)",
                (doc_id, sha256_hex(token), expires_at, user["id"], now),
            )
            pass_id = cur.lastrowid
            add_audit(
                tx,
                actor_id=user["id"],
                action="view_pass_issued",
                document_id=doc_id,
                detail=(
                    f"签发一次性阅览凭证 {pass_id}，阅览截止时刻: {expires_at}"
                    f"（早于清除时刻 {doc['purge_at']}）；库内仅保存凭证摘要，"
                    "明文仅本次返回，外部复核人可在截止前无账号读取正文一次"
                ),
            )
            result = {
                "pass_id": pass_id,
                "document_id": doc_id,
                "status": "active",
                "token": token,
                "expires_at": expires_at,
                "purge_at": doc["purge_at"],
                "created_at": now,
            }
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


@app.post("/view-passes/redeem")
def redeem_view_pass(
    payload: ViewPassRedeemIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """外部复核人无需账号，凭明文在截止前兑换一次正文与摘要。

    所有失败（无效凭证、过期、撤销、已兑付、资料已清除）统一返回同一个 404，
    不区分原因，绝不泄露正文或资料/凭证的存在性；失败不写任何记录。
    """
    token_hash = sha256_hex(payload.token)
    now = utc_str(now_utc())
    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with write_tx(db) as tx:
        # 先按摘要定位凭证（BEGIN IMMEDIATE 已取得写锁，随后的状态判定无并发窗口）
        vp = tx.execute(
            "SELECT * FROM view_passes WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if vp is None:
            raise err(VIEW_PASS_REJECTED, VIEW_PASS_REJECTED_MSG)
        doc_id = vp["document_id"]

        # 兑换前须在同一事务结算到期清除：清除时刻与兑换同刻（<=）时清除优先。
        # 清除先随本事务提交，拒绝延迟到提交后再返回，不能让回滚撤销已生效的清除。
        purged_here = purge_due_documents(tx, now, doc_id) > 0

        vp = tx.execute("SELECT * FROM view_passes WHERE id = ?", (vp["id"],)).fetchone()
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

        if doc["status"] == "purged":
            if purged_here:
                post_commit_error = err(VIEW_PASS_REJECTED, VIEW_PASS_REJECTED_MSG)
            else:
                raise err(VIEW_PASS_REJECTED, VIEW_PASS_REJECTED_MSG)
        elif vp["status"] != "active":
            # 撤销 / 已兑付同一结论
            raise err(VIEW_PASS_REJECTED, VIEW_PASS_REJECTED_MSG)
        elif vp["expires_at"] <= now:
            # 过期（截止时刻本身不再可兑换；过期状态不改写凭证行，失败不留痕）
            raise err(VIEW_PASS_REJECTED, VIEW_PASS_REJECTED_MSG)
        else:
            # 条件更新原子兑付：仅 active -> redeemed 生效，并发兑换至多一行成功
            cur = tx.execute(
                "UPDATE view_passes SET status='redeemed', redeemed_at=?"
                " WHERE id=? AND status='active'",
                (now, vp["id"]),
            )
            if cur.rowcount != 1:
                raise err(VIEW_PASS_REJECTED, VIEW_PASS_REJECTED_MSG)
            add_audit(
                tx,
                actor_id=None,  # 外部复核人无账号
                action="view_pass_redeemed",
                document_id=doc_id,
                detail=f"一次性阅览凭证 {vp['id']} 已由外部复核人成功兑换，正文仅本次返回，凭证随即失效",
            )
            result = {
                "pass_id": vp["id"],
                "document_id": doc_id,
                "status": "redeemed",
                "body": doc["body"],
                "digest": doc["digest"],
                "expires_at": vp["expires_at"],
                "redeemed_at": now,
            }
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


@app.post("/view-passes/{pass_id}/revoke")
def revoke_view_pass(
    pass_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """当前封存人凭凭证编号在兑换前撤销一次性阅览凭证。"""
    vp = db.execute("SELECT * FROM view_passes WHERE id = ?", (pass_id,)).fetchone()
    if vp is None:
        raise err(403, "无权操作该凭证或凭证不存在")
    doc = db.execute("SELECT * FROM documents WHERE id = ?", (vp["document_id"],)).fetchone()
    if doc is None or doc["owner_id"] != user["id"]:
        raise err(403, "无权操作该凭证或凭证不存在")

    now = utc_str(now_utc())
    post_commit_error: HTTPException | None = None
    result: dict[str, Any] | None = None
    with authed_write_tx(db, user) as tx:
        # 与其他写路径一致：先确定性到点开封，再结算该资料的到期清除
        resolve_due_waiting(tx, now)
        purged_here = purge_due_documents(tx, now, vp["document_id"]) > 0

        vp = tx.execute("SELECT * FROM view_passes WHERE id = ?", (pass_id,)).fetchone()
        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (vp["document_id"],)).fetchone()
        if doc["owner_id"] != user["id"]:
            raise err(403, "只有当前封存人可以撤销该凭证")
        if doc["status"] == "purged":
            # 清除若在本临界区刚生效，必须先提交，错误延迟到提交后返回
            post_commit_error = err(409, "资料已清除，凭证已失效，无法撤销")
        elif purged_here:  # 防御性：与上一分支等价的先提交语义
            post_commit_error = err(409, "资料已清除，凭证已失效，无法撤销")
        elif vp["status"] == "redeemed":
            raise err(409, "凭证已兑付，不能撤销")
        elif vp["status"] == "revoked":
            raise err(409, "凭证已撤销")
        elif vp["expires_at"] <= now:
            raise err(409, "凭证已过阅览截止时刻，不能撤销")
        else:
            tx.execute(
                "UPDATE view_passes SET status='revoked', revoked_at=?"
                " WHERE id=? AND status='active'",
                (now, pass_id),
            )
            add_audit(
                tx,
                actor_id=user["id"],
                action="view_pass_revoked",
                document_id=vp["document_id"],
                detail=f"封存人在兑换前撤销一次性阅览凭证 {pass_id}",
            )
            result = {
                "pass_id": pass_id,
                "document_id": vp["document_id"],
                "status": "revoked",
                "revoked_at": now,
            }
    if post_commit_error is not None:
        raise post_commit_error
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# 操作记录（按顺序持久保存；非参与方不可见）
# ---------------------------------------------------------------------------


@app.get("/documents/{doc_id}/audit")
def get_audit(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    # 到清除时刻的资料先确定性清除，审计随即包含 purged 记录
    settle_due_purges_for_read(db, doc_id, user)
    load_doc_for_participant(db, doc_id, user)
    rows = db.execute(
        """SELECT a.id, a.at, a.action, a.request_id, a.detail,
                  u.username AS actor
           FROM audit_log a LEFT JOIN users u ON u.id = a.actor_id
           WHERE a.document_id = ?
           ORDER BY a.id""",
        (doc_id,),
    ).fetchall()
    return {"document_id": doc_id, "records": [dict(row) for row in rows]}


# ---------------------------------------------------------------------------
# 站内通知（收件人只读自己的通知；不再反查资料权限）
#
# 四类资料状态变更在状态变更的同一写事务内向固定收件人各投递一条通知：
#   - opened（资料开封）：当时封存人 + 三名见证人；
#   - purged（到期清除）：当时封存人 + 三名见证人；
#   - witness_replaced（见证人替换）：封存人 + 旧见证人 + 新见证人；
#   - handover_confirmed（交接确认）：新旧封存人 + 三名见证人。
# event_seq 固定为该状态变更主审计记录的 audit_log.id（全局事件序号），
# 通知按事件序号排列；同一事件对每名收件人恰好一条
# （UNIQUE(recipient_id, event_seq)）。通知只含资料编号、事件类型、事件
# 时刻与已读状态——绝不含正文、凭证或密钥。失败或回滚的操作不留通知。
#
# 账号凭现有 X-API-Key 读取：非负事件序号游标 + 1～100 页大小，只返回本人
# 通知与下一游标；按通知编号标记已读并返回已读状态。非法游标/页大小（422）、
# 他人或不存在的通知编号（403）一律拒绝且不改状态；已读重试幂等，不产生
# 额外记录。失去资料权限的人（被替换的旧见证人、交接后的旧封存人等）仍可
# 读自己收到的通知，但通知不提供任何资料正文/凭证/密钥，不能借此访问资料。
# ---------------------------------------------------------------------------


def _serialize_notification(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "notification_id": row["id"],
        "event_seq": row["event_seq"],
        "document_id": row["document_id"],
        "event_type": row["event_type"],
        "at": row["at"],
        "is_read": bool(row["is_read"]),
    }


@app.get("/notifications")
def list_notifications(
    cursor: int = Query(default=0, ge=0, description="事件序号游标：只返回 event_seq 大于该值的通知"),
    page_size: int = Query(
        default=20, ge=1, le=100, alias="page_size", description="页大小（1～100）"
    ),
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """读取本人通知：按事件序号升序，非负游标分页，返回通知与下一游标。

    游标语义为“上一页最后一条通知的 event_seq”：只返回 event_seq > cursor
    的记录；页内严格按 (event_seq, id) 升序。下一游标为本页最后一条通知的
    事件序号；本页之后再无更新的通知时为 null。通知不含正文、凭证或密钥。
    """
    rows = db.execute(
        "SELECT * FROM notifications WHERE recipient_id = ? AND event_seq > ?"
        " ORDER BY event_seq, id LIMIT ?",
        (user["id"], cursor, page_size + 1),
    ).fetchall()
    has_more = len(rows) > page_size
    page = rows[:page_size]
    next_cursor = page[-1]["event_seq"] if (has_more and page) else None
    return {
        "notifications": [_serialize_notification(r) for r in page],
        "next_cursor": next_cursor,
    }


@app.post("/notifications/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """把本人的一条通知标记已读并返回已读状态。

    条件更新只翻转本人名下通知（id 与收件人同时匹配）；他人通知编号或不存在
    的编号统一 403，不区分是否存在，且不改任何状态。重复标记幂等：保留首次
    read_at，不产生额外记录，也不写审计。
    """
    now = utc_str(now_utc())
    with authed_write_tx(db, user) as tx:
        row = tx.execute(
            "SELECT * FROM notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        if row is None or row["recipient_id"] != user["id"]:
            # 他人通知/不存在统一拒绝，不泄露通知存在性；只读判断在写锁内完成，
            # 条件更新本身也按 recipient_id 限定，双保险。
            raise err(403, "无权操作该通知或通知不存在")
        cur = tx.execute(
            "UPDATE notifications SET is_read = 1,"
            " read_at = COALESCE(read_at, ?) WHERE id = ? AND recipient_id = ?",
            (now, notification_id, user["id"]),
        )
        if cur.rowcount != 1:  # 理论不可达：上面已锁定本人行
            raise err(403, "无权操作该通知或通知不存在")
        saved = tx.execute(
            "SELECT * FROM notifications WHERE id = ?", (notification_id,)
        ).fetchone()
    return {
        "notification_id": notification_id,
        "is_read": True,
        "read_at": saved["read_at"],
    }


# ---------------------------------------------------------------------------
# 审计快照签发与离线验真
#
# 参与方（当前封存人或当前指定见证人）可为资料签发审计快照：资料编号、摘要、
# 当前状态与开封/清除时刻、按原顺序排列且标明截止序号的审计记录——不含正文，
# 也不含任何密钥。签发在同一写事务内先完成到点开封/到期清除等确定性结算再
# 取数，快照始终对应一个已提交的一致状态（不会把并发操作后的状态与之前的
# 审计拼接）；签名密钥持久化在 signing_keys 表，重启后旧签名仍可验证。
#
# 复核人提交“快照 + 签名 + 事先信任的公钥”验真：字段或记录顺序被篡改、
# 签名不匹配、版本不支持或公钥无效均明确失败；验真只使用随请求提交的公钥，
# 绝不从快照包内取公钥建立信任。
# ---------------------------------------------------------------------------


def ensure_signing_key() -> None:
    """启动时确保签名密钥存在（幂等）；密钥随库持久化，重启后旧签名仍可验证。"""
    conn = connect()
    try:
        with write_tx(conn) as tx:
            snapshots.load_or_create_signing_key(tx)
    finally:
        conn.close()


@app.post("/documents/{doc_id}/audit-snapshots", status_code=201)
def issue_audit_snapshot(
    doc_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """签发审计快照：仅当前封存人或当前指定见证人；其他情况统一 403。

    临界区内先做确定性到点开封与到期清除结算，再按同一事务内的一致视图
    组装快照并签名；签发动作本身追加一条审计记录（截止序号之前的状态即
    本次快照内容），响应不含正文与密钥。
    """
    load_doc_for_participant(db, doc_id, user)

    now = utc_str(now_utc())
    with authed_write_tx(db, user) as tx:
        # 先完成到点开封/到期清除等确定性结算：快照只反映结算完成后的状态
        resolve_due_waiting(tx, now)
        purge_due_documents(tx, now)

        doc = tx.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        # 锁内复核参与方身份（交接/替换见证人可能与预校验并发）：以当前名单为准
        is_party = doc["owner_id"] == user["id"] or tx.execute(
            "SELECT 1 FROM document_witnesses WHERE document_id = ? AND user_id = ?",
            (doc_id, user["id"]),
        ).fetchone()
        if not is_party:
            raise err(403, "无权访问该资料或资料不存在")

        snapshot = snapshots.build_snapshot(tx, doc)
        key_row = snapshots.load_or_create_signing_key(tx)
        signature = snapshots.sign_snapshot(key_row, snapshot)
        add_audit(
            tx,
            actor_id=user["id"],
            action="audit_snapshot_issued",
            document_id=doc_id,
            detail=(
                f"签发审计快照：截止序号 {snapshot['audit_upto']}"
                f"（{len(snapshot['records'])} 条审计记录），"
                f"签名公钥标识 {key_row['key_id']}；快照不含正文与密钥"
            ),
        )
        key_id = key_row["key_id"]

    return {
        "snapshot": snapshot,
        "signature": signature,
        "key_id": key_id,
        "algorithm": snapshots.ALGORITHM,
        "canonicalization": snapshots.CANONICALIZATION,
    }


@app.get("/audit-snapshots/public-key")
def get_snapshot_public_key(db: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """返回当前快照签名公钥与标识，供离线复核人事先建立信任。

    公钥本身可公开分发（复核人应通过可信渠道核对 key_id / 公钥指纹）；
    验真接口只使用调用方提交的公钥，不以任何包内夹带的公钥建立信任。
    """
    row = db.execute("SELECT * FROM signing_keys ORDER BY id LIMIT 1").fetchone()
    if row is None:
        # 兜底：启动流程尚未创建时（如直接调用应用对象）即时生成
        with write_tx(db) as tx:
            row = snapshots.load_or_create_signing_key(tx)
    return snapshots.public_key_info(row)


@app.post("/audit-snapshots/verify")
def verify_audit_snapshot(payload: VerifySnapshotIn) -> dict[str, Any]:
    """离线验真：复核人提交快照、签名与事先信任的公钥。

    成功返回验真结果与截止序号；字段或记录顺序被篡改、签名不匹配、
    版本不支持或公钥无效均明确失败（400），结构不合法为 422。
    无需任何账号凭据；不读取服务端任何数据，信任只来自提交的公钥。
    """
    # 1) 结构校验：字段必须不多不少（422，错误体不回显提交内容）
    try:
        snap = AuditSnapshotIn.model_validate(payload.snapshot)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc

    # 2) 版本必须受支持（版本字段本身也在签名保护之内）
    if snap.version != snapshots.SNAPSHOT_VERSION:
        raise err(400, f"不支持的快照版本: {snap.version}")

    # 3) 公钥必须可解析为有效的 Ed25519 公钥
    try:
        raw_public_key = snapshots.parse_public_key(payload.public_key)
    except ValueError as exc:
        raise err(400, f"公钥无效：{exc}") from exc

    # 4) 签名必须为 base64 编码的 64 字节签名
    try:
        signature = base64.b64decode(payload.signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise err(400, "签名格式无效：应为 base64 编码的 Ed25519 签名") from exc
    if len(signature) != 64:
        raise err(400, "签名格式无效：应为 base64 编码的 64 字节 Ed25519 签名")

    # 5) 对提交的原始快照对象做规范化序列化并验签：字段值或审计记录
    #    顺序的任何改动都会改变规范化字节串，从而验签失败
    if not ed25519.verify(raw_public_key, snapshots.canonical_json(payload.snapshot), signature):
        raise err(400, "签名不匹配：快照字段或审计记录顺序可能被篡改")

    return {
        "valid": True,
        "document_id": snap.document_id,
        "status": snap.status,
        "audit_upto": snap.audit_upto,
        "record_count": len(snap.records),
        "key_id": snapshots.key_id_from_raw(raw_public_key),
        "algorithm": snapshots.ALGORITHM,
    }


def main() -> None:  # pragma: no cover
    init_db()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
