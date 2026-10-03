from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RegisterIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class SealIn(BaseModel):
    body: str = Field(min_length=1, max_length=1_000_000)
    witnesses: list[str] = Field(min_length=3, max_length=3)


class PreSealIn(BaseModel):
    """预封存提案：正文 + 三名互异且非本人的已注册见证人 + 带时区未来截止时刻。"""

    body: str = Field(min_length=1, max_length=1_000_000)
    witnesses: list[str] = Field(min_length=3, max_length=3)
    confirm_deadline: datetime  # 必须带时区，例如 2026-10-01T12:00:00Z

    @field_validator("confirm_deadline")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("确认截止时刻必须带时区信息，例如 2026-10-01T12:00:00Z")
        return v


class PreSealConfirmIn(BaseModel):
    """见证人凭提案摘要确认预封存。"""

    digest: str = Field(min_length=1, max_length=128)


class OpenRequestIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    deadline: datetime  # 必须带时区，例如 2026-10-01T12:00:00Z
    # 可选：最早开封时刻（须不早于提交时刻、且早于 deadline）。
    # 填写后两票在该时刻前集齐只会进入 waiting（待开封），到点自动开封。
    earliest_open_at: datetime | None = None

    @field_validator("deadline", "earliest_open_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            raise ValueError("时间字段必须带时区信息，例如 2026-10-01T12:00:00Z")
        return v

    @model_validator(mode="after")
    def _earliest_before_deadline(self) -> "OpenRequestIn":
        if self.earliest_open_at is not None and not self.earliest_open_at < self.deadline:
            raise ValueError("earliest_open_at 必须早于 deadline")
        return self


class ApproveIn(BaseModel):
    digest: str = Field(min_length=1, max_length=128)


class ReplaceWitnessIn(BaseModel):
    """封存人在资料未开封时替换一名指定见证人。"""

    old_witness: str = Field(min_length=1, max_length=64)
    new_witness: str = Field(min_length=1, max_length=64)


class RevokeIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


class HandoverIn(BaseModel):
    """封存人就未开封资料发起交接邀请。"""

    to_username: str = Field(min_length=1, max_length=64)
    confirm_deadline: datetime  # 受让人确认截止时刻，必须带时区，例如 2026-10-01T12:00:00Z

    @field_validator("confirm_deadline")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("确认截止时刻必须带时区信息，例如 2026-10-01T12:00:00Z")
        return v


class PurgeIn(BaseModel):
    """封存人为资料安排（或改期）到期清除时刻。"""

    purge_at: datetime  # 到期清除时刻，必须带时区，例如 2026-10-01T12:00:00Z

    @field_validator("purge_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("清除时刻必须带时区信息，例如 2026-10-01T12:00:00Z")
        return v


class ViewPassIn(BaseModel):
    """封存人就已开封资料签发一次性阅览凭证：带时区的阅览截止时刻。"""

    expires_at: datetime  # 必须带时区、晚于当前、且早于已安排的清除时刻

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("阅览截止时刻必须带时区信息，例如 2026-10-01T12:00:00Z")
        return v


class ViewPassRedeemIn(BaseModel):
    """无账号外部复核人凭明文凭证兑换一次正文。"""

    token: str = Field(min_length=1, max_length=256)


# ---------------------------------------------------------------------------
# 审计快照离线验真
#
# 快照本体按“严格结构”校验（字段不多不少），随后对调用方提交的原始 JSON
# 对象做规范化序列化并验签：字段值或审计记录顺序的任何改动都会改变
# 规范化字节串，从而验签失败。验真只使用随请求提交的、复核人事先信任的
# 公钥，绝不从快照包内取公钥建立信任。
# ---------------------------------------------------------------------------


class AuditSnapshotRecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int            # 审计记录的全局自增序号（audit_log.id）
    at: str
    action: str
    actor: str | None
    request_id: int | None
    detail: str


class AuditSnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    document_id: int
    digest: str
    status: str
    created_at: str
    opened_at: str | None
    purge_at: str | None
    purged_at: str | None
    audit_upto: int     # 截止序号：快照所包含的最后一条审计记录序号
    records: list[AuditSnapshotRecordIn]


class VerifySnapshotIn(BaseModel):
    """复核人提交的验真请求：快照 + 签名 + 事先信任的公钥。"""

    model_config = ConfigDict(extra="forbid")

    snapshot: dict[str, Any]
    signature: str = Field(min_length=1)   # base64 编码的 64 字节 Ed25519 签名
    public_key: str = Field(min_length=1)  # PEM（SPKI）或 base64 的 32 字节原始公钥
