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

# 具名向量:hybrid 检索需 dense(语义)+ sparse(BM25)双向量
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "text"

# SQLite 审计日志库路径(与 agent 其它本地数据同目录)
MEMORY_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "agent_memory.sqlite3"

# 检索参数
MEMORY_RECALL_TOP_K = int(os.getenv("MEMORY_RECALL_TOP_K", "5"))  # recall 工具返回条数

# 写入时检索相似旧记忆的数量(供 LLM 做 ADD/UPDATE/DELETE/NONE 决策)
WRITE_SEARCH_TOP_K = int(os.getenv("MEMORY_WRITE_SEARCH_TOP_K", "10"))

# 记忆类型
# agent 侧(浏览器自动化,现已从主流程断开,常量保留兼容旧数据):
# - preference: 强用户偏好,成功任务抽取,常驻注入每步 prompt(chat 侧复用此类)
# - site_experience: 站点操作经验,成功任务抽取,按需 recall(chat 侧停产)
# - lesson: 失败教训,失败任务抽取,按需 recall,低权+待验证(chat 侧停产)
# chat 侧:
# - persona: 用户是谁(稳定身份/职业/技术栈),常驻注入
# - preference: 用户想怎样(语言/风格/格式偏好),常驻注入(复用上面同名值)
# - episodic: 发生过什么(项目/事件/上下文),按需检索注入
MEMORY_TYPE_PREFERENCE = "preference"
MEMORY_TYPE_SITE_EXPERIENCE = "site_experience"
MEMORY_TYPE_LESSON = "lesson"
MEMORY_TYPE_PERSONA = "persona"
MEMORY_TYPE_EPISODIC = "episodic"

# 作用域
SCOPE_GLOBAL = "global"  # 全局用户偏好(原 SCOPE_USER,语义更准);chat 三类全用此作用域
SCOPE_DOMAIN = "domain"  # 站点相关(site_experience/lesson,仅 agent 用)

# 分层 / 门控参数
RESIDENT_PREFERENCE_TOP_K = int(os.getenv("MEMORY_RESIDENT_PREF_TOP_K", "3"))  # 常驻偏好注入条数
RESIDENT_PREFERENCE_CHAR_LIMIT = int(os.getenv("MEMORY_RESIDENT_PREF_CHARS", "800"))  # 常驻块字符上限,超限触发蒸馏(本期先监控)
LESSON_RECALL_WEIGHT = float(os.getenv("MEMORY_LESSON_WEIGHT", "0.8"))  # lesson 检索排序降权系数
LESSON_INIT_CONFIDENCE = float(os.getenv("MEMORY_LESSON_CONFIDENCE", "0.4"))  # lesson 初始置信度(低=待验证)
MAX_CONSECUTIVE_BACKEND_TOOLS = int(os.getenv("MEMORY_MAX_BACKEND_TOOLS", "5"))  # web_search+recall 合并上限

# 单用户固定 id
DEFAULT_USER_ID = "local"

# chat 记忆命名空间隔离:chat 记忆用独立 user_id,与 agent 的 DEFAULT_USER_ID 天然隔开
# (_build_filter 首个 must 即 user_id),避免 chat recall 捞到 agent 遗留的站点经验。
CHAT_USER_ID = os.getenv("CHAT_MEMORY_USER_ID", "chat:local")

# ── chat 分层记忆参数 ──
# core 层(persona + preference):常驻注入每轮对话,量小、都重要,不过相关性闸门。
CHAT_CORE_TYPES = [MEMORY_TYPE_PERSONA, MEMORY_TYPE_PREFERENCE]
CHAT_CORE_TOP_K = int(os.getenv("CHAT_CORE_TOP_K", "6"))          # 常驻 core 注入条数上限
# episodic 层:按需 hybrid 检索,过相关性闸门(防 Lost-in-the-Middle 噪声污染)。
RECALL_MIN_COSINE = float(os.getenv("CHAT_RECALL_MIN_COSINE", "0.5"))  # 绝对余弦门(主闸门)
RECALL_REL_RATIO = float(os.getenv("CHAT_RECALL_REL_RATIO", "0.6"))    # 相对比门(兜底,占 top1 比例)
# 写入去抖:攒 N 轮对话才抽取一次(对齐 mem0 滚动窗口,降 token 成本)。
CHAT_WRITE_EVERY_N_TURNS = int(os.getenv("CHAT_WRITE_EVERY_N_TURNS", "3"))

# 非对称 embedding(Qwen3):查询侧加英文 instruct 前缀,写入侧 doc 不加。
# 官方要求 instruct 用英文写(与中文 query 可不同语言),典型提升 1-5% 召回。
# 偏好走 scroll 全量(不检索),故只需站点/事件召回的 instruct。
INSTRUCT_SITE = "Retrieve site operation experiences and past failure lessons relevant to the task"
INSTRUCT_CHAT = "Retrieve facts about the user relevant to the current message"
