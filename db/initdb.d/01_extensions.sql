-- PostgreSQL 容器首次启动时自动执行（/docker-entrypoint-initdb.d/）。
-- GIST 排他约束“同一设备生效证书有效期不可重叠”依赖 btree_gist。
-- 应用启动时也会幂等地执行 CREATE EXTENSION IF NOT EXISTS，此处为双保险。
CREATE EXTENSION IF NOT EXISTS btree_gist;
