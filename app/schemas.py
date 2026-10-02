"""HTTP 请求与响应模型。所有时间均为带时区的 ISO 8601 字符串。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------- 设备 ----------

class DeviceCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, description="设备唯一编号")
    name: str = Field(..., min_length=1, max_length=256, description="设备名称")


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    name: str
    created_at: datetime


# ---------- 证书签发 ----------

class _TimeAwareMixin:
    @classmethod
    def _require_tz(cls, v: datetime | None, name: str) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError(f"{name} 必须携带时区信息（例如 2026-09-01T08:00:00+00:00）")
        return v


class ReferenceIssue(BaseModel, _TimeAwareMixin):
    """登记基准设备合格证书（追溯起点，不引用任何上游）。"""
    cert_no: str = Field(..., min_length=1, max_length=80)
    device_code: str = Field(..., description="基准设备编号（须先登记）")
    calibrated_at: datetime
    valid_from: datetime
    valid_until: datetime
    conclusion: Literal["PASS", "FAIL"] = Field(
        ..., description="基准证书作为追溯起点必须为 PASS（FAIL 会被拒绝）"
    )

    @field_validator("calibrated_at", "valid_from", "valid_until")
    @classmethod
    def _tz(cls, v: datetime):
        return cls._require_tz(v, "时间字段")


class NormalIssue(BaseModel, _TimeAwareMixin):
    """签发普通证书，必须引用一张在校准时刻有效且合格的标准设备证书。"""
    cert_no: str = Field(..., min_length=1, max_length=80)
    device_code: str = Field(..., description="被校设备编号")
    calibrated_at: datetime = Field(..., description="校准时刻")
    valid_from: datetime
    valid_until: datetime
    conclusion: Literal["PASS", "FAIL"]
    parent_cert_no: str = Field(..., description="引用的标准设备证书编号")

    @field_validator("calibrated_at", "valid_from", "valid_until")
    @classmethod
    def _tz(cls, v: datetime):
        return cls._require_tz(v, "时间字段")


class RevokeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=512, description="撤销原因")


# ---------- 证书查询输出 ----------

class CertificateOut(BaseModel):
    id: int
    cert_no: str
    device_code: str
    device_name: str
    category: Literal["REFERENCE", "NORMAL"]
    calibrated_at: datetime
    valid_from: datetime
    valid_until: datetime
    conclusion: Literal["PASS", "FAIL"]
    status: Literal["ACTIVE", "REVOKED", "INVALIDATED"]
    parent_cert_no: str | None = None
    invalidated_by_cert_no: str | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None
    created_at: datetime


class CertificateDetailOut(CertificateOut):
    snapshot: dict[str, Any] = Field(default_factory=dict,
                                     description="签发时不可改写的原始输入快照")


class TraceabilityOut(BaseModel):
    """追溯链：从被查证书自下而上直到基准设备证书。"""
    certificate: CertificateDetailOut
    root_cert_no: str | None
    root_is_reference: bool
    depth: int
    reaches_reference: bool
    cyclic: bool
    chain: list[CertificateOut] = Field(
        ..., description="链节点，顺序为被查证书 -> ... -> 基准证书"
    )


class DownstreamOut(BaseModel):
    """某证书的全部下游（传递闭包）。"""
    certificate: CertificateOut
    downstream_count: int
    downstream: list[CertificateOut]


class AffectedItem(BaseModel):
    certificate: CertificateOut
    new_status: Literal["REVOKED", "INVALIDATED"]
    depth: int


class RevokeResult(BaseModel):
    revoked: AffectedItem
    affected_count: int = Field(..., description="被级联失效的下游证书数量")
    affected: list[AffectedItem]
    traceability: TraceabilityOut = Field(
        ..., description="撤销后该证书的链及最终状态（历史内容仍可查）"
    )


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    certificate_id: int
    event_type: Literal["ISSUED", "REVOKED", "INVALIDATED"]
    detail: str | None
    payload: dict[str, Any]
    created_at: datetime
