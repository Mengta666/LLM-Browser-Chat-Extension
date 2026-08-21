"""记忆写入的 LLM prompt(改写自 mem0/ReUseIt,浏览器 agent 场景 + 中文)。

- EXTRACT_SYSTEM_PROMPT:从**成功**任务抽取 preference / site_experience。
- FAILURE_EXTRACT_SYSTEM_PROMPT:从**失败**任务抽取 lesson(可推翻的教训)。
- DECISION_SYSTEM_PROMPT:对每条新事实对照相似旧记忆决定 ADD/UPDATE/DELETE/NONE。
  关键:给 LLM 的旧记忆只带临时整数 id,LLM 不接触真实 UUID(反幻觉)。

抽取与分类**合并在抽取一步**输出 memory_type(枚举约束),但抽取(preference/
site_experience)与失败教训(lesson)走**两个独立 prompt**——成功/失败轨迹语义
不同,分开更可靠。拿不准归 site_experience(按需层),不轻易进常驻 preference。
"""

# ─────────────────────────────────────────────────────────────
# 阶段一(成功任务):抽取 preference / site_experience
# ─────────────────────────────────────────────────────────────

EXTRACT_SYSTEM_PROMPT = """你是一个浏览器自动化 agent 的记忆整理器。你的任务是从 agent 刚**成功完成**的一次网页操作任务中,抽取值得**长期记住**的事实,以便未来任务复用。

只记录这两类**稳定、可复用**的信息,并给每条标注 memory_type:
1. **preference**(用户偏好,scope=global):用户明确表达或反复体现的习惯与偏好。例:"用户偏好用键盘快捷键"、"用户喜欢深色主题"、"用户提交表单前总要先预览"。
2. **site_experience**(站点操作经验,scope=domain):在某网站上"怎么完成某类任务"的**高层操作步骤**,下次同类任务可复用。

**site_experience 的 content 必须是高层自然语言步骤**(不含元素编号、不含 CSS 选择器,因为页面每次重新观察、编号会变),并把可变的具体值参数化为 `<占位符>`,末尾带一句验证。格式:
```
任务:<一句话任务> | 入口:<域名或入口路径>
1. <高层步骤> 2. <高层步骤> ... N. <高层步骤,可变值用 <参数> 表示>
验证:<怎么确认成功,如出现"提交成功"提示>
```

**不要记录**:
- 一次性的任务细节(如"这次搜索了 XX 关键词"、"点了第 3 个结果")
- 临时状态、具体数据内容、时间戳
- 底层元素编号 / 选择器 / 精确坐标(页面会变,存了也失效)
- 不确定、可能变化的信息

few-shot 示例:

输入任务:在公司 OA 系统提交请假申请
执行轨迹:打开 oa.corp.com → 点右上角头像 → 我的申请 → 新建请假 → 填表(起止日期、事由) → 提交成功
输出:{"facts": [{"content": "任务:在OA提交请假 | 入口:oa.corp.com/home\\n1. 点右上角头像菜单 2. 进「我的申请」 3. 点「新建」选「请假」 4. 填表:起止=<date_range> 事由=<reason> 5. 点「提交」\\n验证:出现「提交成功」或进入待审批列表", "memory_type": "site_experience", "domain": "oa.corp.com", "entry_url": "https://oa.corp.com/home", "intent_keywords": ["请假", "leave", "申请"]}]}

输入任务:帮我在购物网站搜索无线耳机
执行轨迹:打开网站 → 搜索"无线耳机" → 查看结果
输出:{"facts": []}

输入任务:把这个表单填一下,我一般都用键盘 Tab 切换字段
执行轨迹:逐个字段 Tab 切换填写 → 提交
输出:{"facts": [{"content": "用户习惯用键盘 Tab 键在表单字段间切换", "memory_type": "preference", "domain": "", "entry_url": "", "intent_keywords": []}]}

要求:
- 只输出 JSON,格式为 {"facts": [{"content", "memory_type", "domain", "entry_url", "intent_keywords"}]}。
- memory_type ∈ {"preference", "site_experience"}(成功任务只出这两类)。
- preference:domain/entry_url 留空、intent_keywords 留空数组。
- site_experience:domain 填域名、entry_url 填入口、intent_keywords 填 2-4 个触发意图词。
- **拿不准是不是稳定偏好时,归 site_experience,不要轻易标 preference**(preference 会常驻注入每一步,误判代价高)。
- 用中文记录。没有值得记的就返回 {"facts": []}。宁缺毋滥。"""


def build_extract_user_prompt(task: str, trajectory: str, domain: str = "") -> str:
    """组装成功抽取阶段的 user 消息。"""
    domain_hint = f"\n当前站点域名:{domain}" if domain else ""
    return f"""请从下面这次**成功完成**的浏览器任务中抽取值得长期记住的事实。

任务描述:{task}{domain_hint}

执行轨迹:
{trajectory}

按系统提示的 JSON 格式输出。"""


# ─────────────────────────────────────────────────────────────
# 阶段一(失败任务):抽取 lesson(可推翻的教训)
# ─────────────────────────────────────────────────────────────

FAILURE_EXTRACT_SYSTEM_PROMPT = """你是一个浏览器自动化 agent 的复盘整理器。agent 刚**失败或未完成**一次网页任务,你的任务是提炼**可能有用、但仍需验证**的教训(lesson),帮未来同类任务少走弯路。

关键心态:这是**一条可能的线索,不是定论**。页面会变、原因可能判断错,所以措辞要保守——写"可能的原因",不要写死。

只记录这类信息(memory_type 固定 "lesson",scope=domain):
- 尝试了什么 + 为什么可能失败 + 可以试的替代做法。
- 例:"在该站点直接点「提交」可能失败,因为有必填项未填;可能需要先逐项检查红色星号字段再提交。"

**不要记录**:
- 一次性数据、具体输入内容、时间戳
- 底层元素编号 / 选择器(页面会变)
- 把偶发问题当成永久规律(要留有余地)

few-shot 示例:

输入任务:在报销系统提交单据(失败)
失败轨迹:打开系统 → 填金额 → 点提交 → 报错"请选择报销类型" → 未完成
输出:{"facts": [{"content": "在报销系统直接填金额后提交可能失败,原因可能是「报销类型」为必填项;下次可先选报销类型再填其他字段。", "domain": "expense.corp.com"}]}

输入任务:下载月度报表(失败,但看不出明确原因)
失败轨迹:打开页面 → 找不到下载按钮 → 反复滚动 → 超时
输出:{"facts": [{"content": "在该报表页未能找到下载入口,可能下载按钮不在主视图(也可能需要先展开某菜单);下次可尝试查看页面右上角操作区或「更多」菜单。", "domain": "report.corp.com"}]}

要求:
- 只输出 JSON,格式 {"facts": [{"content", "domain"}]}。
- content 用保守措辞("可能"、"也许需要"),给出可试的替代做法。
- 用中文。实在提炼不出有用教训就返回 {"facts": []}。"""


def build_failure_extract_user_prompt(task: str, trajectory: str, domain: str = "") -> str:
    """组装失败抽取阶段的 user 消息。"""
    domain_hint = f"\n当前站点域名:{domain}" if domain else ""
    return f"""请从下面这次**失败/未完成**的浏览器任务中提炼可能有用的教训(保守措辞)。

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
