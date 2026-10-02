"""ORM 对象 -> 可序列化 dict。

为避免事务提交后懒加载触发 DetachedInstanceError，hydrate_* 会把链路上
需要的设备、父证书、失效源证书一次性批量取出（命中 identity map 即零额外查询）。
"""
from __future__ import annotations

from collections import deque
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Certificate, Device


def hydrate_certificates(
    db: Session, certs: Iterable[Certificate]
) -> tuple[dict[int, Certificate], dict[int, Device]]:
    """收集 certs 中引用到的父证书/失效源证书（递归）与全部设备并批量加载。"""
    cert_map: dict[int, Certificate] = {}
    needed_cert_ids: set[int] = set()
    needed_device_ids: set[int] = set()

    pending = deque(certs)
    while pending:
        cert = pending.popleft()
        if cert.id in cert_map:
            continue
        cert_map[cert.id] = cert
        needed_device_ids.add(cert.device_id)
        if cert.parent_id is not None:
            needed_cert_ids.add(cert.parent_id)
        if cert.invalidated_by_id is not None:
            needed_cert_ids.add(cert.invalidated_by_id)

    # 逐层把引用到但尚未入图的证书补齐（链通常很短，查询次数 = 链深）
    while needed_cert_ids - cert_map.keys():
        missing = needed_cert_ids - cert_map.keys()
        rows = db.scalars(select(Certificate).where(Certificate.id.in_(missing))).all()
        needed_cert_ids.difference_update(missing)
        for row in rows:
            cert_map[row.id] = row
            needed_device_ids.add(row.device_id)
            if row.parent_id is not None:
                needed_cert_ids.add(row.parent_id)
            if row.invalidated_by_id is not None:
                needed_cert_ids.add(row.invalidated_by_id)

    devices = db.scalars(
        select(Device).where(Device.id.in_(needed_device_ids))
    ).all() if needed_device_ids else []
    device_map = {d.id: d for d in devices}
    return cert_map, device_map


def serialize_certificate(
    cert: Certificate,
    cert_map: dict[int, Certificate],
    device_map: dict[int, Device],
    *,
    with_snapshot: bool = False,
) -> dict:
    device = device_map[cert.device_id]
    parent = cert_map.get(cert.parent_id) if cert.parent_id else None
    invalidator = (
        cert_map.get(cert.invalidated_by_id) if cert.invalidated_by_id else None
    )
    data = {
        "id": cert.id,
        "cert_no": cert.cert_no,
        "device_code": device.code,
        "device_name": device.name,
        "category": cert.category,
        "calibrated_at": cert.calibrated_at,
        "valid_from": cert.valid_from,
        "valid_until": cert.valid_until,
        "conclusion": cert.conclusion,
        "status": cert.status,
        "parent_cert_no": parent.cert_no if parent else None,
        "invalidated_by_cert_no": invalidator.cert_no if invalidator else None,
        "revoked_at": cert.revoked_at,
        "revoke_reason": cert.revoke_reason,
        "created_at": cert.created_at,
    }
    if with_snapshot:
        data["snapshot"] = cert.snapshot or {}
    return data
