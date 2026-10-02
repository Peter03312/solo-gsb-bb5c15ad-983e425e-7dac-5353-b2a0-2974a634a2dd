"""应用配置，全部通过环境变量注入（离线环境无外部依赖）。"""
import os

# 数据库连接串。Compose 中默认指向 postgres 服务。
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://calib:calib@localhost:5432/calibration",
)

# 证书追溯链允许的最大深度，防止异常长链拖垮递归查询。
MAX_CHAIN_DEPTH = int(os.getenv("MAX_CHAIN_DEPTH", "100"))

# 服务监听配置
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# 级联失效 / 可串行化冲突时的事务重试次数
TX_MAX_ATTEMPTS = int(os.getenv("TX_MAX_ATTEMPTS", "8"))
