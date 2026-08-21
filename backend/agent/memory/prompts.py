"""记忆写入的两个 LLM prompt(改写自 mem0,浏览器 agent 场景 + 中文)。

- EXTRACT_PROMPT:从一次已完成的浏览器任务里抽取值得长期记住的事实。
- 决策 prompt:对每条新事实,对照向量检索到的相似旧记忆,决定 ADD/UPDATE/DELETE/NONE。
  关键:给 LLM 的旧记忆只带临时整数 id,LLM 不接触真实 UUID(反幻觉)。
"""

# ─────────────────────────────────────────────────────────────
# 阶段一:事实抽取
# ─────────────────────────────────────────────────────────────

EXTRACT_SYSTEM_PROMPT = """你是一个浏览器自动化 agent 的记忆整理器。你的任务是从 agent 刚完成的一次网页操作任务中,抽取值得**长期记住**的事实,以便未来任务复用。

只记录这两类**稳定、可复用**的信息:
1. **用户偏好**(scope=user):用户明确表达或反复体现的习惯与偏好。例:"用户偏好用键盘快捷键"、"用户喜欢深色主题"、"用户提交表单前总要先预览"。
2. **站点事实**(scope=domain):某个网站上稳定的、下次还用得上的界面/操作事实。例:"该系统的登录入口在右上角头像菜单里"、"报销提交按钮在页面最底部"、"搜索框需要按回车才触发"。

**不要记录**:
- 一次性的任务细节(如"这次搜索了 XX 关键词"、"点了第 3 个结果")
- 临时状态、具体数据内容、时间戳
- agent 自己的执行步骤流水(那是 workflow 记忆,不在本次范围)
- 不确定、可能变化的信息

few-shot 示例:

输入任务:在公司 OA 系统提交请假申请
执行轨迹:打开 oa.corp.com → 点右上角头像 → 我的申请 → 新建请假 → 填表 → 提交成功
输出:{"facts": [{"content": "OA 系统(oa.corp.com)的申请入口在右上角头像菜单的“我的申请”里", "scope": "domain", "domain": "oa.corp.com"}]}

输入任务:帮我在购物网站搜索无线耳机
执行轨迹:打开网站 → 搜索"无线耳机" → 查看结果
输出:{"facts": []}

输入任务:把这个表单填一下,我一般都用键盘 Tab 切换字段
执行轨迹:逐个字段 Tab 切换填写 → 提交
输出:{"facts": [{"content": "用户习惯用键盘 Tab 键在表单字段间切换", "scope": "user", "domain": ""}]}

要求:
- 只输出 JSON,格式为 {"facts": [{"content": "...", "scope": "user"|"domain", "domain": "..."}]}。
- scope=user 时 domain 留空字符串;scope=domain 时 domain 填该网站域名。
- 用中文记录事实。没有值得记的就返回 {"facts": []}。
- 拿不准就不记(宁缺毋滥)。"""


def build_extract_user_prompt(task: str, trajectory: str, domain: str = "") -> str:
    """组装抽取阶段的 user 消息。"""
    domain_hint = f"\n当前站点域名:{domain}" if domain else ""
    return f"""请从下面这次已完成的浏览器任务中抽取值得长期记住的事实。

任务描述:{task}{domain_hint}

执行轨迹:
{trajectory}

按系统提示的 JSON 格式输出。"""


# ─────────────────────────────────────────────────────────────
# 阶段二:ADD/UPDATE/DELETE/NONE 决策(反幻觉:只用临时整数 id)
# ─────────────────────────────────────────────────────────────

DECISION_SYSTEM_PROMPT = """你是一个记忆管理器,负责维护记忆库的一致性。

给你两样东西:
1. **已有记忆**:每条带一个整数 id(如 "0"、"1")。
2. **新抽取的事实**:一批候选事实。

对照已有记忆,为整体决定一组操作,每条操作是四种之一:
- **ADD**:新事实是全新信息,已有记忆里没有 → 新增(生成一个新整数 id)。
- **UPDATE**:新事实与某条已有记忆是同一主题但信息更丰富/有变化 → 更新那条(**必须沿用它原来的整数 id**)。若二者表达完全相同的意思,则不算 UPDATE(用 NONE)。
- **DELETE**:新事实与某条已有记忆**矛盾** → 删除旧的那条(**必须沿用它原来的整数 id**)。
- **NONE**:新事实已存在或无关紧要 → 不动(沿用原 id)。

**关键规则(务必遵守)**:
- UPDATE/DELETE/NONE 的 id **只能从上面"已有记忆"给出的 id 里选**,绝不能编造新 id。
- 只有 ADD 才生成新 id(用一个未出现过的整数)。
- 拿不准就用 NONE,不要乱改。

few-shot 示例:

已有记忆:
[{"id": "0", "text": "用户是软件工程师"}]
新事实:["用户叫张伟"]
输出:{"memory": [{"id": "0", "text": "用户是软件工程师", "event": "NONE"}, {"id": "1", "text": "用户叫张伟", "event": "ADD"}]}

已有记忆:
[{"id": "0", "text": "用户喜欢喝咖啡"}]
新事实:["用户喜欢喝不加糖的美式咖啡"]
输出:{"memory": [{"id": "0", "text": "用户喜欢喝不加糖的美式咖啡", "event": "UPDATE", "old_memory": "用户喜欢喝咖啡"}]}

已有记忆:
[{"id": "0", "text": "报销按钮在页面顶部"}]
新事实:["报销提交按钮在页面最底部"]
输出:{"memory": [{"id": "0", "text": "报销按钮在页面顶部", "event": "DELETE"}, {"id": "1", "text": "报销提交按钮在页面最底部", "event": "ADD"}]}

要求:只输出 JSON,格式 {"memory": [{"id", "text", "event", "old_memory"(仅UPDATE需要)}]}。event ∈ ADD/UPDATE/DELETE/NONE。"""


def build_decision_user_prompt(existing_memories: list[dict], new_facts: list[str]) -> str:
    """组装决策阶段的 user 消息。existing_memories 只含临时整数 id。"""
    import json
    if existing_memories:
        existing_part = json.dumps(existing_memories, ensure_ascii=False, indent=2)
    else:
        existing_part = "(当前记忆库为空)"
    facts_part = json.dumps(new_facts, ensure_ascii=False)
    return f"""已有记忆:
{existing_part}

新抽取的事实:
{facts_part}

按系统提示的 JSON 格式,给出对每条的操作决策。"""
