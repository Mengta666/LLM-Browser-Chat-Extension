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
MEMORY_RECALL_TOP_K = int(os.getenv("MEMORY_RECALL_TOP_K", "5"))  # recall 工具返回条数

# 写入时检索相似旧记忆的数量(供 LLM 做 ADD/UPDATE/DELETE/NONE 决策)
WRITE_SEARCH_TOP_K = int(os.getenv("MEMORY_WRITE_SEARCH_TOP_K", "10"))

# 三类记忆(分层的唯一依据)
# - preference: 强用户偏好,成功任务抽取,常驻注入每步 prompt
# - site_experience: 站点操作经验,成功任务抽取,按需 recall(存高层步骤,非底层路径)
# - lesson: 失败教训,失败任务抽取,按需 recall,低权+待验证
MEMORY_TYPE_PREFERENCE = "preference"
MEMORY_TYPE_SITE_EXPERIENCE = "site_experience"
MEMORY_TYPE_LESSON = "lesson"

# 作用域
SCOPE_GLOBAL = "global"  # 全局用户偏好(原 SCOPE_USER,语义更准)
SCOPE_DOMAIN = "domain"  # 站点相关(site_experience/lesson)

# 分层 / 门控参数
RESIDENT_PREFERENCE_TOP_K = int(os.getenv("MEMORY_RESIDENT_PREF_TOP_K", "3"))  # 常驻偏好注入条数
RESIDENT_PREFERENCE_CHAR_LIMIT = int(os.getenv("MEMORY_RESIDENT_PREF_CHARS", "800"))  # 常驻块字符上限,超限触发蒸馏(本期先监控)
LESSON_RECALL_WEIGHT = float(os.getenv("MEMORY_LESSON_WEIGHT", "0.8"))  # lesson 检索排序降权系数
LESSON_INIT_CONFIDENCE = float(os.getenv("MEMORY_LESSON_CONFIDENCE", "0.4"))  # lesson 初始置信度(低=待验证)
MAX_CONSECUTIVE_BACKEND_TOOLS = int(os.getenv("MEMORY_MAX_BACKEND_TOOLS", "5"))  # web_search+recall 合并上限

# 单用户固定 id
DEFAULT_USER_ID = "local"
