"""知识库相似度计算。

综合评分 = 0.6×向量语义相似 + 0.3×页面指纹相似 + 0.1×域名加分。
向量语义解决"任务描述相似"，指纹+域名补偿"跨环境/跨域名"差异。
"""

import math
from typing import Any


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _same_prefix(p1: str, p2: str, min_seg: int = 2) -> bool:
    """两个 URL 路径是否共享前缀（至少 min_seg 段）。"""
    s1 = [s for s in p1.split("/") if s]
    s2 = [s for s in p2.split("/") if s]
    common = 0
    for a, b in zip(s1, s2):
        if a == b:
            common += 1
        else:
            break
    return common >= min_seg


def _root_domain(host: str) -> str:
    """取主域名（最后两段），如 xingyun-test.jd.com → jd.com。"""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def fingerprint_sim(fp1: dict[str, Any], fp2: dict[str, Any]) -> float:
    """页面结构指纹相似度：URL路径 + 菜单文本 + 标题关键词。"""
    url1 = fp1.get("url_pattern", "")
    url2 = fp2.get("url_pattern", "")
    if url1 and url1 == url2:
        url_match = 1.0
    elif url1 and url2 and _same_prefix(url1, url2):
        url_match = 0.5
    else:
        url_match = 0.0

    menu1 = set(fp1.get("menu_texts", []))
    menu2 = set(fp2.get("menu_texts", []))
    menu_overlap = len(menu1 & menu2) / max(len(menu1 | menu2), 1) if (menu1 or menu2) else 0.0

    title1 = set(fp1.get("title_keywords", []))
    title2 = set(fp2.get("title_keywords", []))
    title_overlap = len(title1 & title2) / max(len(title1 | title2), 1) if (title1 or title2) else 0.0

    return 0.4 * url_match + 0.4 * menu_overlap + 0.2 * title_overlap


def domain_bonus(site1: str, site2: str) -> float:
    """域名加分：完全相同 1.0，主域名相同 0.5，不同 0。"""
    if not site1 or not site2:
        return 0.0
    if site1 == site2:
        return 1.0
    if _root_domain(site1) == _root_domain(site2):
        return 0.5
    return 0.0


def combined_score(
    vec_sim: float,
    fp_query: dict[str, Any],
    fp_record: dict[str, Any],
) -> float:
    """综合评分。"""
    fp = fingerprint_sim(fp_query, fp_record)
    db = domain_bonus(fp_query.get("site", ""), fp_record.get("site", ""))
    return 0.6 * vec_sim + 0.3 * fp + 0.1 * db
