"""数据库引擎、会话工厂与幂等初始化（表、排他约束、审计触发器）。"""
from __future__ import annotations

import time

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# psycopg (DBAPI) 默认不把服务端 TIMESTAMPTZ 带上时区信息，统一转换为带时区。
# 注意：connect 事件中不能直接执行会开启事务的语句（SQLAlchemy 可能在此之后
# 才设置隔离级别，会报 "can't change autocommit now: connection in transaction"）。
# 使用 psycopg 的 execute_nosync 在自动提交上下文设置时区，不留下开启的事务。
@event.listens_for(engine, "connect")
def _set_timezone(dbapi_conn, _record):
    try:
        # psycopg3：临时打开自动提交设置参数，随后恢复，事务状态保持干净
        prev = dbapi_conn.autocommit
        dbapi_conn.autocommit = True
        try:
            with dbapi_conn.cursor() as cur:
                cur.execute("SET TIME ZONE 'UTC'")
        finally:
            dbapi_conn.autocommit = prev
    except Exception:
        # 非 psycopg 驱动或极端情况下退化为服务端默认行为
        pass


_TRIGGER_SQL = r"""
-- 证书核心字段不可改写：仅允许状态、失效相关字段（及 created_at）变更。
CREATE OR REPLACE FUNCTION cert_immutable() RETURNS trigger AS $cert_immutable$
BEGIN
    IF NEW.cert_no        IS DISTINCT FROM OLD.cert_no
       OR NEW.device_id   IS DISTINCT FROM OLD.device_id
       OR NEW.category    IS DISTINCT FROM OLD.category
       OR NEW.calibrated_at IS DISTINCT FROM OLD.calibrated_at
       OR NEW.valid_from  IS DISTINCT FROM OLD.valid_from
       OR NEW.valid_until IS DISTINCT FROM OLD.valid_until
       OR NEW.conclusion  IS DISTINCT FROM OLD.conclusion
       OR NEW.parent_id   IS DISTINCT FROM OLD.parent_id
       OR NEW.snapshot    IS DISTINCT FROM OLD.snapshot
    THEN
        RAISE EXCEPTION '证书 % 为不可改写记录，核心字段禁止修改', OLD.cert_no
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$cert_immutable$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cert_immutable') THEN
        CREATE TRIGGER trg_cert_immutable BEFORE UPDATE ON certificates
            FOR EACH ROW EXECUTE FUNCTION cert_immutable();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION forbid_delete() RETURNS trigger AS $forbid_delete$
BEGIN
    RAISE EXCEPTION '审计数据禁止删除' USING ERRCODE = 'check_violation';
END;
$forbid_delete$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cert_nodelete') THEN
        CREATE TRIGGER trg_cert_nodelete BEFORE DELETE ON certificates
            FOR EACH ROW EXECUTE FUNCTION forbid_delete();
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_event_immutable') THEN
        CREATE TRIGGER trg_event_immutable
            BEFORE UPDATE OR DELETE ON certificate_events
            FOR EACH ROW EXECUTE FUNCTION forbid_delete();
    END IF;
END $$;
"""


def _split_plpgsql(sql: str) -> list[str]:
    """按空行切分上面的语句块（每个块要么是 CREATE FUNCTION，要么是 DO 块）。"""
    return [block.strip() for block in sql.split("\n\n") if block.strip()]


def wait_for_database(max_attempts: int = 30, delay: float = 1.0) -> None:
    """等待数据库就绪（容器同时启动时数据库可能尚未可连）。"""
    from sqlalchemy.exc import OperationalError

    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError:
            if attempt == max_attempts:
                raise
            time.sleep(delay)


def init_database() -> None:
    """幂等建库：扩展 -> 表/约束 -> 触发器。重复执行不会报错或覆盖数据。"""
    # 导入模型以注册元数据
    from . import models  # noqa: F401

    wait_for_database()

    with engine.begin() as conn:
        # 排他约束依赖 btree_gist（GIST 索引支持标量等值列）。
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))

    # ExcludeConstraint 依赖 btree_gist，因此扩展创建完成后再建表。
    models.Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        for statement in _split_plpgsql(_TRIGGER_SQL):
            conn.execute(text(statement))
