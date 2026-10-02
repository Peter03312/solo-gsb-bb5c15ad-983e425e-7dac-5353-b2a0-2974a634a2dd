"""关系模型：设备、证书、只增审计事件。

不可改写性分两层保证：
1. 应用层从不 UPDATE/DELETE 证书的签发内容（撤销只改状态列）；
2. PostgreSQL 触发器禁止修改证书核心字段、禁止删除证书与事件记录。

同一设备已生效证书有效期不重叠，由数据库 GIST 排他约束强制，
并发签发也会被数据库拒绝（应用再翻译成明确的 409 错误）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# 证书类别 / 结论 / 状态的取值（CHECK 约束同步强制）
REFERENCE = "REFERENCE"   # 基准设备证书（追溯起点）
NORMAL = "NORMAL"         # 普通证书

PASS = "PASS"
FAIL = "FAIL"

ACTIVE = "ACTIVE"                 # 有效
REVOKED = "REVOKED"               # 被直接撤销
INVALIDATED = "INVALIDATED"       # 因上游撤销而级联失效

# 半开区间 [valid_from, valid_until)，两张同时生效的证书仅端点相接不算重叠。
_ACTIVE_WINDOW = "tstzrange(valid_from, valid_until, '[)')"


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("code", name="uq_devices_code"),)


class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cert_no: Mapped[str] = mapped_column(String(80), nullable=False)
    device_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("devices.id"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)

    # 校准时刻；valid_from ~ valid_until 为证书有效期（均含时区）。
    calibrated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    valid_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    conclusion: Mapped[str] = mapped_column(String(8), nullable=False)

    # 引用的标准设备证书（基准证书为追溯起点，parent_id 为空）
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("certificates.id"), nullable=True
    )

    # 签发时的输入快照（设备/校准时刻/有效期/结论/引用），随记录永久保留
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ACTIVE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_by_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("certificates.id"), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    parent = relationship("Certificate", remote_side=[id], foreign_keys=[parent_id])
    device = relationship("Device")

    __table_args__ = (
        UniqueConstraint("cert_no", name="uq_certificates_cert_no"),
        CheckConstraint("category IN ('REFERENCE', 'NORMAL')", name="ck_cert_category"),
        CheckConstraint("conclusion IN ('PASS', 'FAIL')", name="ck_cert_conclusion"),
        CheckConstraint("status IN ('ACTIVE', 'REVOKED', 'INVALIDATED')",
                        name="ck_cert_status"),
        CheckConstraint("valid_from < valid_until", name="ck_cert_valid_range"),
        CheckConstraint("valid_from <= calibrated_at AND calibrated_at <= valid_until",
                        name="ck_cert_calibrated_in_range"),
        # 普通证书必须引用上游；基准证书是起点，不能有上游
        CheckConstraint(
            "(category = 'REFERENCE') = (parent_id IS NULL)",
            name="ck_cert_parent_by_category",
        ),
        # 基准设备证书作为合格源头，本身必须合格
        CheckConstraint("category <> 'REFERENCE' OR conclusion = 'PASS'",
                        name="ck_cert_reference_passes"),
        # 失效/撤销证书必须记录原因与失效源；有效证书则不允许有这些字段
        CheckConstraint(
            "(status = 'ACTIVE' AND revoked_at IS NULL "
            "AND invalidated_by_id IS NULL AND revoke_reason IS NULL) "
            "OR (status IN ('REVOKED', 'INVALIDATED') "
            "AND revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_cert_status_fields",
        ),
        CheckConstraint(
            "status <> 'INVALIDATED' OR invalidated_by_id IS NOT NULL",
            name="ck_cert_invalidated_by",
        ),
        # 核心防重叠约束：同一设备、两张 ACTIVE 证书的有效期区间不得相交。
        # 仅 ACTIVE 行进入索引（部分索引），历史失效证书不影响新签发。
        ExcludeConstraint(
            ("device_id", "="),
            (literal_column(_ACTIVE_WINDOW), "&&"),
            using="gist",
            where=literal_column("status = 'ACTIVE'"),
            name="ex_cert_active_window_no_overlap",
        ),
        Index("ix_certificates_device_id", "device_id"),
        Index("ix_certificates_parent_id", "parent_id"),
        Index("ix_certificates_status", "status"),
    )


class CertificateEvent(Base):
    """只增审计事件：签发、撤销、级联失效。"""
    __tablename__ = "certificate_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    certificate_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("certificates.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_events_certificate_id", "certificate_id"),
        CheckConstraint(
            "event_type IN ('ISSUED', 'REVOKED', 'INVALIDATED')",
            name="ck_event_type",
        ),
    )
