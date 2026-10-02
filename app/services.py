"""业务逻辑：签发校验、追溯链/下游递归查询、撤销级联、事务重试。"""
from __future__ import annotations

from datetime import datetime, timezone
import random
import time
from typing import Any, Callable, TypeVar

from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from .config import MAX_CHAIN_DEPTH, TX_MAX_ATTEMPTS
from .database import engine
from .errors import (
    CertificateNotActive,
    ConcurrentIssuance,
    DomainError,
    DuplicateCertNo,
    NotFoundError,
    ParentNotQualified,
    ReferenceChainCycle,
    ReferenceRootInvalid,
    TimeRangeInvalid,
    ValidityOverlap,
    ValidationFailed,
)
from .models import (
    ACTIVE,
    INVALIDATED,
    PASS,
    REFERENCE,
    REVOKED,
    Certificate,
    CertificateEvent,
    Device,
)

T = TypeVar("T")

# 自下而上的递归追溯（被查证书 -> 父 -> ... -> 基准），带环检测。
_ANCESTORS_SQL = """
WITH RECURSIVE chain AS (
    SELECT id, cert_no, parent_id, category, status, conclusion,
           valid_from, valid_until, 1 AS depth, ARRAY[id] AS path, false AS cycle
    FROM certificates WHERE id = :start
    UNION
    SELECT a.id, a.cert_no, a.parent_id, a.category, a.status, a.conclusion,
           a.valid_from, a.valid_until, ch.depth + 1, ch.path || a.id,
           a.id = ANY(ch.path)
    FROM certificates a JOIN chain ch ON a.id = ch.parent_id
    WHERE NOT ch.cycle AND ch.depth < :max_depth
)
SELECT * FROM chain ORDER BY depth
"""

# 自上而下的递归下游（被撤销证书 -> 直接下游 -> ...），带环检测。
_DESCENDANTS_SQL = """
WITH RECURSIVE down AS (
    SELECT id, cert_no, parent_id, 1 AS depth, ARRAY[id] AS path, false AS cycle
    FROM certificates WHERE id = :start
    UNION
    SELECT c.id, c.cert_no, c.parent_id, d.depth + 1, d.path || c.id,
           c.id = ANY(d.path)
    FROM certificates c JOIN down d ON c.parent_id = d.id
    WHERE NOT d.cycle AND d.depth < :max_depth
)
SELECT id, cert_no, depth, cycle FROM down ORDER BY depth
"""

# 一批父证书的直接子证书（parents 用 expanding bindparam 展开为 IN 列表）。
_DIRECT_CHILDREN_SQL = text(
    "SELECT id, parent_id FROM certificates WHERE parent_id IN :parents"
).bindparams(bindparam("parents", expanding=True))

_RETRYABLE_SQLSTATES = {"40001", "40P01"}  # serialization_failure / deadlock_detected


# ---------- 事务封装 ----------

def run_transaction(
    session_factory: Callable[[], Session],
    fn: Callable[[Session], T],
    *,
    serializable: bool = False,
) -> T:
    """执行业务事务。

    并发正确性（防止“撤销扫描已有下游之后、签发才插入新下游”造成的漏失效）：
      1. 行锁把签发引用与撤销在父证书行上线性化（链校验/撤销中的 FOR UPDATE）；
      2. 写事务一律 SERIALIZABLE：PostgreSQL SSI 会把“撤销读取已有下游”与
         “签发插入新下游”识别为读写反依赖环，令后提交方收到 40001 并在此重试。
         必须双方都为 SERIALIZABLE —— SSI 不跨隔离级别追踪谓词依赖，这正是
         此前单边 SERIALIZABLE 仍然漏失效的根因。

    - 领域错误立即回滚并抛出；
    - 唯一/排他/检查约束冲突翻译成明确的领域错误；
    - 40001(可串行化失败)/40P01(死锁) 自动重试。
    """
    for attempt in range(TX_MAX_ATTEMPTS):        # 隔离级别必须在物理连接开启任何事务之前设置。
        connection = None
        if serializable:
            connection = engine.execution_options(
                isolation_level="SERIALIZABLE"
            ).connect()
            db = session_factory(bind=connection)
        else:
            db = session_factory()
        try:
            result = fn(db)
            db.commit()
            return result
        except DomainError:
            db.rollback()
            raise
        except IntegrityError as exc:
            db.rollback()
            raise _translate_integrity(exc) from exc
        except DBAPIError as exc:
            db.rollback()
            sqlstate = (
                getattr(exc.orig, "sqlstate", None)
                or getattr(getattr(exc.orig, "diag", None), "sqlstate", None)
            )
            if sqlstate in _RETRYABLE_SQLSTATES and attempt < TX_MAX_ATTEMPTS - 1:
                # 指数退避 + 抖动，避免大量并发事务被同时唤醒后再次集体冲突
                base = 0.02 * (2 ** attempt)
                time.sleep(min(base, 0.25) + random.uniform(0, 0.02))
                continue
            if sqlstate in _RETRYABLE_SQLSTATES:
                # 重试次数耗尽：统一转成明确的 409，不把底层 5xx 泄露给客户端
                raise ConcurrentIssuance(
                    "系统繁忙，检测到并发签发/撤销冲突，多次重试仍未成功，请稍后重试"
                ) from exc
            raise
        finally:
            db.close()
            if connection is not None:
                connection.close()


def _translate_integrity(exc: IntegrityError) -> DomainError:
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None)
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None) or ""

    if sqlstate == "23505":  # unique_violation
        if "cert_no" in constraint:
            return DuplicateCertNo("证书编号已存在：同一证书编号不得重复签发")
        if "code" in constraint:
            return ValidationFailed("设备编号已存在", code="DUPLICATE_DEVICE_CODE")
        return ConcurrentIssuance("唯一约束冲突，疑似重复签发")
    if sqlstate == "23P01":  # exclusion_violation —— 有效期重叠/并发重复签发
        if "active_window" in constraint:
            return ConcurrentIssuance(
                "签发冲突：该设备已有生效证书，有效期不可重叠（可能为并发重复签发）"
            )
        return ConcurrentIssuance("排他约束冲突，签发被拒绝")
    if sqlstate == "23514":  # check_violation
        return ValidationFailed(f"数据约束校验失败：{constraint or '业务规则不满足'}")
    if sqlstate == "23503":  # foreign_key_violation
        return NotFoundError("引用的设备或证书不存在")
    return ValidationFailed("数据库完整性校验失败")


# ---------- 基础查询 ----------

def get_device(db: Session, code: str, *, lock: bool = False) -> Device | None:
    stmt = select(Device).where(Device.code == code)
    if lock:
        stmt = stmt.with_for_update()
    return db.scalars(stmt).first()


def get_cert(db: Session, cert_no: str, *, lock: bool = False) -> Certificate | None:
    stmt = select(Certificate).where(Certificate.cert_no == cert_no)
    if lock:
        stmt = stmt.with_for_update()
    return db.scalars(stmt).first()


def _ancestor_rows(db: Session, start_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(_ANCESTORS_SQL), {"start": start_id, "max_depth": MAX_CHAIN_DEPTH}
    ).mappings().all()
    return [dict(r) for r in rows]


def descendant_rows(db: Session, start_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(_DESCENDANTS_SQL), {"start": start_id, "max_depth": MAX_CHAIN_DEPTH}
    ).mappings().all()
    return [dict(r) for r in rows]


def _lock_rows_by_id(db: Session, ids: list[int]) -> None:
    """按 id 全局顺序加锁，避免签发与撤销之间的锁序死锁。"""
    unique_ids = sorted(set(ids))
    db.execute(
        select(Certificate.id)
        .where(Certificate.id.in_(unique_ids))
        .order_by(Certificate.id)
        .with_for_update()
    ).all()


# ---------- 签发 ----------

def _validate_time_window(
    calibrated_at: datetime, valid_from: datetime, valid_until: datetime
) -> None:
    if valid_from >= valid_until:
        raise TimeRangeInvalid("有效期起始时刻必须早于截止时刻")
    if not (valid_from <= calibrated_at <= valid_until):
        raise TimeRangeInvalid("校准时刻必须落在证书有效期 [valid_from, valid_until] 内")


def _check_chain_rows(rows: list[dict[str, Any]], calibrated_at: datetime) -> None:
    """对链路上的每一行做业务校验（状态/结论/有效期/链顶）。"""
    for row in rows:
        if row["cycle"]:
            raise ReferenceChainCycle(
                f"引用链存在成环引用，证书 {row['cert_no']} 已在链上出现过"
            )
        if row["status"] != ACTIVE:
            state = "撤销" if row["status"] == REVOKED else "失效"
            raise ParentNotQualified(
                f"引用链上的证书 {row['cert_no']} 已{state}，不得再作为校准依据"
            )
        if row["conclusion"] != PASS:
            raise ParentNotQualified(
                f"引用链上的证书 {row['cert_no']} 结论不合格，不得作为校准依据"
            )
        if not (row["valid_from"] <= calibrated_at <= row["valid_until"]):
            raise ParentNotQualified(
                f"引用的标准证书 {row['cert_no']} 在校准时刻 "
                f"{calibrated_at.isoformat()} 不在有效期内"
                f"（有效期至 {row['valid_until'].isoformat()}）"
            )

    top = rows[-1]
    if top["parent_id"] is not None:
        raise ReferenceRootInvalid(
            "引用链顶端不是基准设备证书（链在普通证书处中断或超过最大追溯深度）"
        )
    if top["category"] != REFERENCE:
        raise ReferenceRootInvalid("引用链顶端证书不是基准设备（REFERENCE）证书")


def _validate_reference_chain(
    db: Session, parent: Certificate, calibrated_at: datetime
) -> None:
    """校验引用链并对整条链加行锁，与并发撤销严格串行化。

    步骤：递归 CTE 取链 -> 业务预检 -> 按 id 升序对链上证书 FOR UPDATE 加锁
    （与撤销侧同一全局加锁顺序，杜绝死锁）-> 加锁后用当前事务新快照重读并复检。
    复检是关键：若等待某张上游行锁期间，该证书恰好被撤销/级联失效，重读会看到
    最新状态并拒绝签发，从而不会出现“引用已失效证书仍签发成功”。
    """
    rows = _ancestor_rows(db, parent.id)
    if not rows:
        raise ParentNotQualified("引用的标准证书不存在")
    _check_chain_rows(rows, calibrated_at)

    ids = sorted({r["id"] for r in rows if not r["cycle"]})
    _lock_rows_by_id(db, ids)

    # 加锁后重读（READ COMMITTED 下每句获得新快照），确认等待期间链未被撤销
    relocked = db.scalars(
        select(Certificate).where(Certificate.id.in_(ids))
    ).all()
    rows_after = _rows_from_certs(relocked, start_id=parent.id)
    _check_chain_rows(rows_after, calibrated_at)


def _rows_from_certs(certs: list[Certificate], *, start_id: int) -> list[dict[str, Any]]:
    """把整链 ORM 对象转成与递归 CTE 同构的行（含 cycle 占位），用于复检。"""
    by_id = {c.id: c for c in certs}
    rows: list[dict[str, Any]] = []
    cur = by_id.get(start_id)
    seen: set[int] = set()
    while cur is not None:
        if cur.id in seen:
            rows.append({**_cert_row(cur), "cycle": True})
            break
        seen.add(cur.id)
        rows.append({**_cert_row(cur), "cycle": False})
        cur = by_id.get(cur.parent_id) if cur.parent_id is not None else None
    return rows


def _cert_row(c: Certificate) -> dict[str, Any]:
    return {
        "id": c.id,
        "cert_no": c.cert_no,
        "parent_id": c.parent_id,
        "category": c.category,
        "status": c.status,
        "conclusion": c.conclusion,
        "valid_from": c.valid_from,
        "valid_until": c.valid_until,
    }


def issue_certificate(
    db: Session,
    payload: Any,
    *,
    category: str,
) -> Certificate:
    calibrated_at = payload.calibrated_at
    valid_from = payload.valid_from
    valid_until = payload.valid_until
    _validate_time_window(calibrated_at, valid_from, valid_until)

    device = get_device(db, payload.device_code)
    if device is None:
        raise NotFoundError(f"被校设备 {payload.device_code} 尚未登记")

    if get_cert(db, payload.cert_no) is not None:
        raise DuplicateCertNo(f"证书编号 {payload.cert_no} 已存在，不得重复签发")

    if category == REFERENCE:
        if payload.conclusion != PASS:
            raise ReferenceRootInvalid("基准设备证书作为追溯起点，结论必须为 PASS")
        parent = None
    else:
        # 注意：此处不先单独锁父证书——所有多证书加锁必须经
        # _validate_reference_chain 按全局 id 升序一次性完成，否则与并发的
        # 其他签发/撤销事务形成不一致的加锁顺序导致死锁。
        parent = get_cert(db, payload.parent_cert_no)
        if parent is None:
            raise ParentNotQualified(
                f"引用的标准设备证书 {payload.parent_cert_no} 不存在"
            )
        _validate_reference_chain(db, parent, calibrated_at)

    # 同设备生效证书有效期不得重叠（友好预检；真正防线是数据库排他约束）
    overlap = db.scalars(
        select(Certificate)
        .where(
            Certificate.device_id == device.id,
            Certificate.status == ACTIVE,
            Certificate.valid_from < valid_until,
            Certificate.valid_until > valid_from,
        )
        .limit(1)
    ).first()
    if overlap is not None:
        raise ValidityOverlap(
            f"设备 {device.code} 的生效证书 {overlap.cert_no} 与本次有效期 "
            f"[{valid_from.isoformat()}, {valid_until.isoformat()}) 重叠"
        )

    snapshot = payload.model_dump(mode="json")
    snapshot["category"] = category

    cert = Certificate(
        cert_no=payload.cert_no,
        device_id=device.id,
        category=category,
        calibrated_at=calibrated_at,
        valid_from=valid_from,
        valid_until=valid_until,
        conclusion=payload.conclusion,
        parent_id=parent.id if parent else None,
        snapshot=snapshot,
        status=ACTIVE,
    )
    db.add(cert)
    db.flush()  # 触发唯一/排他约束，冲突随即翻译成 409

    db.add(
        CertificateEvent(
            certificate_id=cert.id,
            event_type="ISSUED",
            detail="签发基准设备证书" if category == REFERENCE else "签发普通证书",
            payload=snapshot,
        )
    )
    db.flush()
    return cert


# ---------- 撤销与级联失效 ----------

def _lock_and_discover_descendants(
    db: Session, target: Certificate
) -> dict[int, int]:
    """先锁定被撤销目标，再迭代发现并锁定全部下游，直到集合不再增长。

    为什么不能先递归 CTE 再一次性加锁：与签发并发时，新下游可能在 CTE 快照之后、
    加锁之前被插入，从而漏失效。改为：
      1. 先 FOR UPDATE 锁定目标行 —— 此后任何想引用它（及其已锁定祖先）的新签发
         都必须等本事务结束，结束后其复检会看到已撤销并拒绝；
      2. 已持锁行下不可能再冒出新子证书（签发插子证书前必须锁父行），
         于是“加锁一层 -> 重新发现”迭代必然收敛到完整下游闭包；
      3. 所有行统一按 id 升序加锁，与签发侧同序，杜绝死锁。

    返回 depth_map: id -> 距目标的层数（含目标自身 depth=1）。
    """
    locked: set[int] = {target.id}
    depth_map: dict[int, int] = {target.id: 1}
    _lock_rows_by_id(db, [target.id])

    # frontier 为当前最外一层、需要向下扩展子证书的节点
    frontier: list[int] = [target.id]
    while frontier:
        # 每条新语句在 READ COMMITTED 下取到新快照，
        # 能看到此前已提交、但上一轮尚未锁定的下游。
        rows = db.execute(
            _DIRECT_CHILDREN_SQL, {"parents": tuple(frontier)}
        ).mappings().all()
        new_rows = [(r["id"], depth_map[r["parent_id"]] + 1)
                    for r in rows if r["id"] not in locked]
        if not new_rows:
            break
        new_ids = sorted({cid for cid, _ in new_rows})
        # 统一按 id 升序加锁，与签发侧同一全局加锁顺序，杜绝死锁
        _lock_rows_by_id(db, new_ids)
        for cid, depth in new_rows:
            locked.add(cid)
            depth_map[cid] = depth
        frontier = new_ids
    return depth_map


def revoke_certificate(db: Session, target: Certificate, reason: str) -> dict[str, Any]:
    """直接撤销目标，并在同一事务中把全部下游传递级联失效。

    并发安全不依赖 SERIALIZABLE（签发为 READ COMMITTED，SSI 无法跨隔离级别
    追踪），而是靠行锁把“引用签发”与“撤销”在父行上线性化，并迭代发现下游闭包。
    """
    # 环检测（撤销前只读预检；触发器/约束也保证了链的完整性）
    pre_rows = descendant_rows(db, target.id)
    if any(r["cycle"] for r in pre_rows):
        raise ReferenceChainCycle("下游引用链检测到环，拒绝撤销")

    depth_map = _lock_and_discover_descendants(db, target)

    descendant_ids = [cid for cid, depth in depth_map.items() if depth > 1]
    descendants = (
        db.scalars(
            select(Certificate).where(Certificate.id.in_(descendant_ids))
        ).all()
        if descendant_ids
        else []
    )

    now = datetime.now(timezone.utc)

    target.status = REVOKED
    target.revoked_at = now
    target.revoke_reason = reason
    db.add(
        CertificateEvent(
            certificate_id=target.id,
            event_type="REVOKED",
            detail=f"证书被直接撤销：{reason}",
            payload={"reason": reason},
        )
    )

    newly_invalidated: list[Certificate] = []
    for cert in descendants:
        if cert.status != ACTIVE:
            continue
        cert.status = INVALIDATED
        cert.invalidated_by_id = target.id
        cert.revoked_at = now
        cert.revoke_reason = f"上游证书 {target.cert_no} 被撤销，级联失效"
        db.add(
            CertificateEvent(
                certificate_id=cert.id,
                event_type="INVALIDATED",
                detail=(
                    f"因上游证书 {target.cert_no} 被撤销而级联失效"
                    f"（引用深度 {depth_map[cert.id] - 1}）"
                ),
                payload={
                    "reason": reason,
                    "upstream_cert_no": target.cert_no,
                    "depth": depth_map[cert.id] - 1,
                },
            )
        )
        newly_invalidated.append(cert)

    db.flush()
    return {
        "target": target,
        "newly_invalidated": newly_invalidated,
        "depth_map": depth_map,
    }


def prepare_revoke(db: Session, cert_no: str, reason: str) -> dict[str, Any]:
    target = get_cert(db, cert_no, lock=True)
    if target is None:
        raise NotFoundError(f"证书 {cert_no} 不存在")
    if target.status != ACTIVE:
        state = "撤销" if target.status == REVOKED else "失效"
        raise CertificateNotActive(f"证书 {cert_no} 已{state}，不能重复撤销")
    return revoke_certificate(db, target, reason)
