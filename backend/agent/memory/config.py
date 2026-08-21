"""长期记忆子系统的配置常量。

记忆存储对齐 mem0:Qdrant payload 是事实源,SQLite 只做变更审计日志。
向量维度硬编为 4096(Qwen3-Embedding-8B 输出),不再信任 env 默认值,
避免老系统 QDRANT_VECTOR_SIZE 默认 1024 的维度炸弹。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

__env_path = Path(__file__).resolve().parents[2] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

# Qdrant 连接(复用页面 RAG 的同一实例)
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
QDRANT_DISTANCE = os.getenv("QDRANT_DISTANCE", "Cosine")

# 记忆专用 collection(与页面 RAG 的 browser_pages 分开)
MEMORY_COLLECTION = os.getenv("QDRANT_MEMORY_COLLECTION", "agent_memories")

# 向量维度硬编 + 启动校验:Qwen3-Embedding-8B 输出 4096 维,必须与之一致。
MEMORY_VECTOR_SIZE = 4096

# SQLite 审计日志库路径(与 agent 其它本地数据同目录)
MEMORY_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "agent_memory.sqlite3"

# 检索参数
# threshold=0.6:实测 Qwen3-Embedding-8B 下,直接相关命中 ~0.73+,
# 而不相关中文文本的基线相似度高达 0.39-0.47(天气/烤蛋糕对咖啡记忆)。
# 取 0.6 干净隔开"查询确实关于这条记忆"与噪声。低于此易召回无关记忆污染 agent。
RETRIEVE_TOP_K = int(os.getenv("MEMORY_RETRIEVE_TOP_K", "5"))
RETRIEVE_THRESHOLD = float(os.getenv("MEMORY_RETRIEVE_THRESHOLD", "0.6"))

# 写入时检索相似旧记忆的数量(供 LLM 做 ADD/UPDATE/DELETE/NONE 决策)
WRITE_SEARCH_TOP_K = int(os.getenv("MEMORY_WRITE_SEARCH_TOP_K", "10"))

# 记忆种类(本期只 semantic;M6 workflow 第二期)
MEMORY_KIND_SEMANTIC = "semantic"
MEMORY_KIND_WORKFLOW = "workflow"

# 作用域
SCOPE_USER = "user"      # 全局用户偏好
SCOPE_DOMAIN = "domain"  # 站点相关事实

# 单用户固定 id
DEFAULT_USER_ID = "local"
