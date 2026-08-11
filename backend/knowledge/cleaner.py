"""操作步骤清洗。

保存前对步骤序列做清洗：
- 过滤动态定位值（#jd-id-xxx 动态ID、纯数字 annotation_id）
- 合并连续重复的同类操作
- 去掉无信息量的 scroll 噪音
- 保留 intent/value/result 等语义字段
"""

import re
from typing import Any


# 动态 ID 模式：框架生成的一次性 ID，下次页面刷新会变
_DYNAMIC_ID_PATTERNS = [
    re.compile(r"^#?jd-id-\d+"),        # #jd-id-9314-207
    re.compile(r"^\d+$"),                # 纯数字（annotation_id 临时编号）
    re.compile(r"^#[a-z]+-\d{4,}"),      # #el-1234 之类
    re.compile(r":r[0-9a-z]+:"),         # React useId
]


def _is_dynamic(value: str) -> bool:
    """判断定位值是否为动态/易变值。"""
    if not value:
        return False
    v = value.strip()
    return any(p.search(v) for p in _DYNAMIC_ID_PATTERNS)


# 动态数量短语：如 "250 个提交"、"9 个分支" —— 数字会变，归一化为 N
_COUNT_PHRASE = re.compile(r"\d+\s*(个|条|项|次)")


def _normalize_dynamic_text(text: str) -> str:
    """归一化含动态数量的文本，避免下次数字变化导致定位失败。"""
    return _COUNT_PHRASE.sub(r"N\1", text)


def _clean_selector(step: dict[str, Any]) -> None:
    """清除动态的 css_selector 和 target_text，避免存无效定位。"""
    if _is_dynamic(step.get("css_selector", "")):
        step["css_selector"] = ""
    # target_text 是动态 ID 时清空（保留 intent 作为语义锚点）
    tt = step.get("target_text", "")
    if _is_dynamic(tt):
        step["target_text"] = ""
    elif tt:
        # 归一化动态数量短语（"250个提交" → "N个提交"）
        step["target_text"] = _normalize_dynamic_text(tt)
    # 清洗回放定位级联：剔除动态 css 项、含动态数量的文本项，去重，保留稳定项
    sels = step.get("selectors")
    if isinstance(sels, list) and sels:
        cleaned_sels = []
        seen = set()
        for s in sels:
            if not isinstance(s, dict):
                continue
            val = s.get("value", "")
            by = s.get("by", "")
            if by == "css" and _is_dynamic(val):
                continue
            # 含动态数量的文本选择器（"259 个提交"）无法字面回放，丢弃
            if by == "text" and _COUNT_PHRASE.search(val):
                continue
            key = (by, val)
            if not val or key in seen:
                continue
            seen.add(key)
            cleaned_sels.append(s)
        step["selectors"] = cleaned_sels


def clean_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清洗步骤序列，返回精简后的语义化步骤。"""
    cleaned: list[dict[str, Any]] = []
    for step in steps:
        s = dict(step)
        # 字段名归一化：旧录制用 url_pattern，统一为 url_before
        if "url_pattern" in s and "url_before" not in s:
            s["url_before"] = s.pop("url_pattern")
        _clean_selector(s)

        action = s.get("action", "")

        # 去掉连续的 scroll（只保留每段的第一个，且最多保留信息量）
        if action == "scroll" and cleaned and cleaned[-1].get("action") == "scroll":
            continue

        # 合并连续重复：同 action + 同 target_text + 同 value 视为重复
        if cleaned:
            prev = cleaned[-1]
            same = (prev.get("action") == action
                    and prev.get("target_text", "") == s.get("target_text", "")
                    and prev.get("value", "") == s.get("value", "")
                    and prev.get("text", "") == s.get("text", ""))
            if same:
                # 用后一次覆盖（保留最新的 result/intent）
                cleaned[-1] = s
                continue

        cleaned.append(s)

    # 反推 expected.url_after：第 i 步的结果 URL 约等于第 i+1 步的 url_before。
    # 仅在 expected 缺失时填（agent 记录已带真实 expected，不覆盖）；供 recorded 记录回放验证。
    for i in range(len(cleaned) - 1):
        cur = cleaned[i]
        nxt_url = cleaned[i + 1].get("url_before", "")
        cur_url = cur.get("url_before", "")
        exp = cur.get("expected") or {}
        if nxt_url and nxt_url != cur_url and not exp.get("url_after"):
            exp = dict(exp)
            exp["url_after"] = nxt_url
            cur["expected"] = exp

    # 省略空字段 + 剥掉临时排序字段（epoch/seq 仅前端排序用，不入库）
    _drop = {"epoch", "seq"}
    return [
        {k: v for k, v in s.items() if v not in ("", None, [], {}) and k not in _drop}
        for s in cleaned
    ]


# 菜单文本清洗：过滤业务数据、去重、限长
_MENU_MAX_LEN = 8
_MENU_MAX_COUNT = 12
_BUSINESS_DATA = re.compile(r"[\d()（）]")  # 含数字/括号的是业务数据而非菜单


def clean_fingerprint(fp: dict[str, Any]) -> dict[str, Any]:
    """清洗页面指纹的 menu_texts：去重、过滤业务数据、限长限量。

    兼容旧版扩展或录制路径传来的脏数据（前端已做同样清洗，此处为兜底）。
    """
    if not isinstance(fp, dict):
        return {}
    result = dict(fp)
    menu = result.get("menu_texts", [])
    if isinstance(menu, list):
        seen: set[str] = set()
        cleaned: list[str] = []
        for item in menu:
            t = str(item or "").strip()
            if not t or len(t) > _MENU_MAX_LEN:
                continue
            if _BUSINESS_DATA.search(t):
                continue
            if t in seen:
                continue
            seen.add(t)
            cleaned.append(t)
            if len(cleaned) >= _MENU_MAX_COUNT:
                break
        result["menu_texts"] = cleaned
    return result
