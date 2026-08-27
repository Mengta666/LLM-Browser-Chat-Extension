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

# 记忆类型(chat 长期记忆):
# - persona: 用户是谁(稳定身份/职业/技术栈),常驻注入
# - preference: 用户想怎样(语言/风格/格式偏好),常驻注入
# - episodic: 发生过什么(项目/事件/上下文),按需检索注入
MEMORY_TYPE_PERSONA = "persona"
MEMORY_TYPE_PREFERENCE = "preference"
MEMORY_TYPE_EPISODIC = "episodic"

# 作用域:chat 三类全用 global(跨会话通用);保留常量供 payload/过滤统一使用
SCOPE_GLOBAL = "global"

# 存储层中性默认 user_id(vector 层函数默认值;chat 门面都显式传 CHAT_USER_ID)
DEFAULT_USER_ID = "local"

# chat 记忆命名空间:chat 记忆用独立 user_id(_build_filter 首个 must 即 user_id),
# 与其它命名空间天然隔离。
CHAT_USER_ID = os.getenv("CHAT_MEMORY_USER_ID", "chat:local")

# ── chat 分层记忆参数 ──
# core 层(persona + preference):常驻注入每轮对话,量小、都重要,不过相关性闸门。
CHAT_CORE_TYPES = [MEMORY_TYPE_PERSONA, MEMORY_TYPE_PREFERENCE]
CHAT_CORE_TOP_K = int(os.getenv("CHAT_CORE_TOP_K", "6"))              # 常驻 core 注入条数上限
RESIDENT_PREFERENCE_CHAR_LIMIT = int(os.getenv("MEMORY_CORE_CHARS", "800"))  # core 注入块字符上限
# episodic 层:按需 hybrid 检索,过相关性闸门(防 Lost-in-the-Middle 噪声污染)。
RECALL_MIN_COSINE = float(os.getenv("CHAT_RECALL_MIN_COSINE", "0.5"))  # 绝对余弦门(主闸门)
RECALL_REL_RATIO = float(os.getenv("CHAT_RECALL_REL_RATIO", "0.6"))    # 相对比门(兜底,占 top1 比例)
# 写入去抖:攒 N 轮对话才抽取一次(对齐 mem0 滚动窗口,降 token 成本)。
CHAT_WRITE_EVERY_N_TURNS = int(os.getenv("CHAT_WRITE_EVERY_N_TURNS", "3"))

# 非对称 embedding(Qwen3):查询侧加英文 instruct 前缀,写入侧 doc 不加。
# 官方要求 instruct 用英文写(与中文 query 可不同语言),典型提升 1-5% 召回。
INSTRUCT_CHAT = "Retrieve facts about the user relevant to the current message"

# ── 会话隔离(路线乙)──
# episodic 记忆按会话隔离:payload.chat_id 存所属会话 id、仅本会话检索;
# persona/preference 是全局记忆,chat_id 留空(跨所有会话常驻)。
# confidence 字段兼作 importance 存储位:抽取时 LLM 打 1-10 分,归一到 0.1-1.0,
# 供检索三因子重排与 episodic GC 排序共用(一次打分两用)。

# ── episodic 检索三因子重排(对齐 Generative Agents:relevance+recency+importance)──
# 仅用于 episodic 召回排序;core(persona/preference)是常驻全量注入,不套三因子。
# gap-gated 稀释(调研 IR:候选相关度扎堆时 relevance 区分度差,应看相对间隔而非绝对归一):
# relevance 直接用原始 cosine(不归一);次要因子(recency+importance)乘一个动态稀释系数
# dilution = 1 - min(1, spread/GAP_REF),spread=候选 cosine 极差。
#   - 相关度拉得开(spread 大)→ dilution→0 → relevance 主导(不被次要因子干扰);
#   - 相关度扎堆(spread 小)→ dilution→1 → 放手让 recency/importance 决定(打破 tie)。
RECALL_W_RECENCY = float(os.getenv("CHAT_RECALL_W_RECENCY", "0.25"))       # 时间新近(次要,受稀释门控)
RECALL_W_IMPORTANCE = float(os.getenv("CHAT_RECALL_W_IMPORTANCE", "0.20")) # 重要性(次要,受稀释门控)
RECALL_GAP_REF = float(os.getenv("CHAT_RECALL_GAP_REF", "0.15"))           # 多大 cosine 极差算"拉开了"
# recency 指数衰减半衰期(小时);用 created_at(事件新鲜度),非 last_accessed_at。
RECALL_HALFLIFE_HOURS = float(os.getenv("CHAT_RECALL_HALFLIFE_HOURS", "240"))  # 10 天

# ── episodic 会话级遗忘 GC(容量上限 + 软失效)──
# 每会话 episodic 超 cap → 写入后同步剪枝(只扫本会话,非后台全量扫描),
# 按 (verified/importance/reinforce/recency) 保留高价值,尾部 invalidate 软失效(可回溯)。
EPISODIC_CAP = int(os.getenv("CHAT_EPISODIC_CAP", "50"))                  # 每会话 episodic 上限
EPISODIC_KEEP_RATIO = float(os.getenv("CHAT_EPISODIC_KEEP_RATIO", "0.8")) # 超限剪到 cap*ratio
EPISODIC_PRUNE_GRACE_HOURS = float(os.getenv("CHAT_EPISODIC_GRACE_HOURS", "24"))  # 新记忆宽限窗,不剪
