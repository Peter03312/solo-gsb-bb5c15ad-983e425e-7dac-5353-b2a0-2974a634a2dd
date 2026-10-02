"""计量校准证书追溯服务 —— HTTP API。

设计要点：
- 追溯起点：登记基准设备的合格证书及其有效期（REFERENCE）；
- 普通证书签发时校验：上游在校准时刻有效且合格、链不成环、链顶为基准证书、
  同设备生效证书有效期不重叠（数据库 GIST 排他约束兜底并发）；
- 签发内容以快照永久保留，核心字段触发器禁止改写；
- 撤销上游 -> 全部下游在同一可串行化事务中级联失效，返回受影响证书及链；
- 历史签发内容（快照、有效期、结论、引用）任何状态下都可查询。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services
from .database import SessionLocal, init_database
from .errors import DomainError, NotFoundError
from .models import (
    ACTIVE,
    INVALIDATED,
    NORMAL,
    REFERENCE,
    REVOKED,
    Certificate,
    CertificateEvent,
    Device,
)
from .schemas import (
    AffectedItem,
    CertificateDetailOut,
    CertificateOut,
    DeviceCreate,
    DeviceOut,
    DownstreamOut,
    EventOut,
    NormalIssue,
    ReferenceIssue,
    RevokeRequest,
    RevokeResult,
    TraceabilityOut,
)
from .serialization import hydrate_certificates, serialize_certificate


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 幂等初始化：扩展、表与排他约束、审计触发器
    init_database()
    yield


app = FastAPI(
    title="计量校准证书追溯服务",
    version="1.0.0",
    description=(
        "离线可运行的校准证书签发、追溯与撤销级联服务（Python + FastAPI + PostgreSQL）。"
        "时间字段均为带时区的 ISO 8601。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.code,
            "message": exc.message,
        },
    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------- 序列化小工具 ----------

def _cert_payload(db: Session, cert: Certificate, *, detail: bool = False) -> dict:
    cert_map, device_map = hydrate_certificates(db, [cert])
    return serialize_certificate(cert, cert_map, device_map, with_snapshot=detail)


def _certs_payload(db: Session, certs: list[Certificate]) -> list[dict]:
    cert_map, device_map = hydrate_certificates(db, certs)
    return [serialize_certificate(c, cert_map, device_map) for c in certs]


def _traceability(db: Session, cert: Certificate) -> dict[str, Any]:
    rows = services._ancestor_rows(db, cert.id)
    ids = [r["id"] for r in rows if not r["cycle"]]
    cyclic = any(r["cycle"] for r in rows)

    chain_certs = (
        db.scalars(select(Certificate).where(Certificate.id.in_(ids))).all()
        if ids
        else []
    )
    by_id = {c.id: c for c in chain_certs}
    ordered = [by_id[i] for i in ids if i in by_id]

    cert_map, device_map = hydrate_certificates(db, [cert] + ordered)
    chain_data = [
        serialize_certificate(by_id[r["id"]], cert_map, device_map)
        for r in rows
        if not r["cycle"] and r["id"] in by_id
    ]
    top = rows[-1] if rows else None
    return {
        "certificate": serialize_certificate(cert, cert_map, device_map,
                                             with_snapshot=True),
        "root_cert_no": top["cert_no"] if top else None,
        "root_is_reference": bool(top and top["category"] == REFERENCE),
        "depth": max(len(rows) - 1, 0),
        "reaches_reference": bool(
            top and top["parent_id"] is None and top["category"] == REFERENCE
        ),
        "cyclic": cyclic,
        "chain": chain_data,
    }


# ---------- 健康检查 ----------

@app.get("/health", tags=["系统"])
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok"}


# ---------- 设备 ----------

@app.post("/devices", response_model=DeviceOut, status_code=201, tags=["设备管理"])
def create_device(body: DeviceCreate, db: Session = Depends(get_db)):
    def work(tx: Session) -> Device:
        if services.get_device(tx, body.code, lock=True) is not None:
            raise DomainError(
                f"设备编号 {body.code} 已存在", code="DUPLICATE_DEVICE_CODE"
            )
        device = Device(code=body.code, name=body.name)
        tx.add(device)
        tx.flush()
        return device

    device = services.run_transaction(SessionLocal, work)
    return device


@app.get("/devices", response_model=list[DeviceOut], tags=["设备管理"])
def list_devices(db: Session = Depends(get_db)):
    return db.scalars(select(Device).order_by(Device.id)).all()


# ---------- 证书签发 ----------

@app.post(
    "/certificates/reference",
    response_model=CertificateDetailOut,
    status_code=201,
    tags=["证书签发"],
    summary="登记基准设备合格证书（追溯起点）",
)
def issue_reference_certificate(body: ReferenceIssue, db: Session = Depends(get_db)):
    cert = services.run_transaction(
        SessionLocal,
        lambda tx: services.issue_certificate(tx, body, category=REFERENCE),
        serializable=True,
    )
    return _cert_payload(db, cert, detail=True)


@app.post(
    "/certificates",
    response_model=CertificateDetailOut,
    status_code=201,
    tags=["证书签发"],
    summary="签发普通校准证书（引用标准设备证书）",
    responses={
        409: {"description": "引用无效/失效、链成环、有效期重叠或并发重复签发"},
        422: {"description": "时间范围非法或基准链顶端无效"},
    },
)
def issue_normal_certificate(body: NormalIssue, db: Session = Depends(get_db)):
    cert = services.run_transaction(
        SessionLocal,
        lambda tx: services.issue_certificate(tx, body, category=NORMAL),
        serializable=True,
    )
    return _cert_payload(db, cert, detail=True)


# ---------- 证书查询 ----------

@app.get(
    "/certificates",
    response_model=list[CertificateOut],
    tags=["证书查询"],
    summary="查询证书（可按设备/状态过滤，active_at 查询某时刻有效）",
)
def list_certificates(
    device_code: str | None = Query(None, description="按设备编号过滤"),
    status: str | None = Query(None, pattern="^(ACTIVE|REVOKED|INVALIDATED)$"),
    active_at: datetime | None = Query(
        None, description="查询该时刻处于有效期内的 ACTIVE 证书（含时区）"
    ),
    db: Session = Depends(get_db),
):
    stmt = select(Certificate).join(Device, Device.id == Certificate.device_id)
    if device_code:
        stmt = stmt.where(Device.code == device_code)
    if status:
        stmt = stmt.where(Certificate.status == status)
    if active_at:
        stmt = stmt.where(
            Certificate.status == ACTIVE,
            Certificate.valid_from <= active_at,
            Certificate.valid_until > active_at,
        )
    stmt = stmt.order_by(Certificate.id)
    return _certs_payload(db, list(db.scalars(stmt).all()))


def _load_cert_or_404(db: Session, cert_no: str) -> Certificate:
    cert = services.get_cert(db, cert_no)
    if cert is None:
        raise NotFoundError(f"证书 {cert_no} 不存在")
    return cert


@app.get(
    "/certificates/{cert_no}",
    response_model=CertificateDetailOut,
    tags=["证书查询"],
    summary="查询单张证书（任何状态，含签发快照，历史内容永久可查）",
)
def get_certificate(cert_no: str, db: Session = Depends(get_db)):
    cert = _load_cert_or_404(db, cert_no)
    return _cert_payload(db, cert, detail=True)


@app.get(
    "/certificates/{cert_no}/traceability",
    response_model=TraceabilityOut,
    tags=["证书查询"],
    summary="自下而上追溯到基准设备证书（返回完整引用链及各证书当前状态）",
)
def trace_certificate(cert_no: str, db: Session = Depends(get_db)):
    cert = _load_cert_or_404(db, cert_no)
    return _traceability(db, cert)


@app.get(
    "/certificates/{cert_no}/downstream",
    response_model=DownstreamOut,
    tags=["证书查询"],
    summary="查询全部下游证书（传递闭包，撤销时这些证书将受影响）",
)
def downstream_certificate(cert_no: str, db: Session = Depends(get_db)):
    cert = _load_cert_or_404(db, cert_no)
    rows = services.descendant_rows(db, cert.id)
    ids = [r["id"] for r in rows if r["depth"] > 1 and not r["cycle"]]
    downstream = (
        db.scalars(select(Certificate).where(Certificate.id.in_(ids))).all()
        if ids
        else []
    )
    by_id = {c.id: c for c in downstream}
    ordered = [by_id[i] for i in ids if i in by_id]
    return {
        "certificate": _cert_payload(db, cert),
        "downstream_count": len(ordered),
        "downstream": _certs_payload(db, ordered),
    }


@app.get(
    "/certificates/{cert_no}/events",
    response_model=list[EventOut],
    tags=["证书查询"],
    summary="查询证书的审计事件（签发/撤销/级联失效，只增不可改写）",
)
def certificate_events(cert_no: str, db: Session = Depends(get_db)):
    cert = _load_cert_or_404(db, cert_no)
    return db.scalars(
        select(CertificateEvent)
        .where(CertificateEvent.certificate_id == cert.id)
        .order_by(CertificateEvent.id)
    ).all()


# ---------- 撤销与级联失效 ----------

@app.post(
    "/certificates/{cert_no}/revoke",
    response_model=RevokeResult,
    tags=["撤销管理"],
    summary="撤销证书：其所有下游证书在同一事务中级联失效",
    responses={
        409: {"description": "证书已撤销/已失效，或并发撤销冲突"},
    },
)
def revoke_certificate(
    cert_no: str, body: RevokeRequest, db: Session = Depends(get_db)
):
    result = services.run_transaction(
        SessionLocal,
        lambda tx: services.prepare_revoke(tx, cert_no, body.reason),
        serializable=True,
    )
    target: Certificate = result["target"]
    newly_invalidated: list[Certificate] = result["newly_invalidated"]
    depth_map: dict[int, int] = result["depth_map"]

    revoked_payload = _cert_payload(db, target)
    affected_payload = _certs_payload(db, newly_invalidated)
    trace = _traceability(db, target)

    return {
        "revoked": {
            "certificate": revoked_payload,
            "new_status": REVOKED,
            "depth": 0,
        },
        "affected_count": len(newly_invalidated),
        "affected": [
            AffectedItem(
                certificate=affected_payload[i],
                new_status=INVALIDATED,
                depth=depth_map[newly_invalidated[i].id] - 1,
            )
            for i in range(len(newly_invalidated))
        ],
        "traceability": trace,
    }
