"""Web 搜索排序与质量打分（纯逻辑，无 IO）。

从 api/chat.py 抽出的自洽函数簇：查询改写、关键词提取、来源质量打分、
搜索结果与候选片段的重排。均为无状态纯函数，仅依赖入参、re 与
core.utils.safe_float。质量打分权重通过环境变量配置。
"""

from __future__ import annotations

import os
import re
from typing import Any

from core.utils import safe_float
from urllib.parse import urlsplit

WEB_SEARCH_TITLE_WEIGHT = float(os.getenv("WEB_SEARCH_TITLE_WEIGHT", "0.18"))
WEB_SEARCH_SNIPPET_WEIGHT = float(os.getenv("WEB_SEARCH_SNIPPET_WEIGHT", "0.06"))
WEB_SEARCH_RANK_WEIGHT = float(os.getenv("WEB_SEARCH_RANK_WEIGHT", "0.02"))
WEB_RETRIEVAL_SCORE_WEIGHT = float(os.getenv("WEB_RETRIEVAL_SCORE_WEIGHT", "0.7"))


def append_missing_terms(query: str, terms: list[str]) -> str:
    """向 query 追加缺失的通用检索词。"""
    rewritten = query.strip()
    normalized = rewritten.lower()
    for term in terms:
        if term.lower() in normalized:
            continue
        rewritten = f"{rewritten} {term}".strip()
        normalized = rewritten.lower()
    return rewritten


def rewrite_web_search_query(query: str) -> tuple[str, bool]:
    """按通用问题意图改写搜索 query，不识别具体主题实体。"""
    normalized_query = re.sub(r"\s+", " ", query).strip()
    if not normalized_query:
        return "", False

    lowered = normalized_query.lower()
    rewritten = normalized_query

    if re.search(r"\bwhat\s+is\b|\bdefine\b|\bdefinition\b", lowered) or re.search(r"什么是|是什么|何为|含义|定义", normalized_query):
        rewritten = append_missing_terms(rewritten, ["定义", "原理", "应用"])

    if re.search(r"\bhow\b|\bworkflow\b|\bsteps?\b", lowered) or re.search(r"如何|怎么|原理|流程|步骤|工作方式|实现", normalized_query):
        rewritten = append_missing_terms(rewritten, ["原理", "流程", "实现"])

    if re.search(r"\bsummary\b|\bsynopsis\b|\bplot\b", lowered) or re.search(r"讲了什么|主要内容|内容简介|故事梗概|剧情|情节", normalized_query):
        rewritten = append_missing_terms(rewritten, ["简介", "情节", "解析"])

    if re.search(r"\bvs\b|\bversus\b|\bcompare\b|\bdifference\b", lowered) or re.search(r"区别|对比|比较|差异|优缺点", normalized_query):
        rewritten = append_missing_terms(rewritten, ["区别", "对比", "优缺点"])

    return rewritten, rewritten != normalized_query


def extract_query_keywords(query: str) -> list[str]:
    """从 query 中提取通用相关性关键词，不内置具体主题白名单。"""
    keywords: list[str] = []
    english_stop_words = {
        "what",
        "is",
        "are",
        "the",
        "a",
        "an",
        "of",
        "and",
        "or",
        "in",
        "to",
        "for",
        "about",
        "define",
        "definition",
        "how",
        "does",
        "do",
    }
    chinese_stop_phrases = {
        "什么是",
        "是什么",
        "何为",
        "含义",
        "定义",
        "介绍",
        "解释",
        "一下",
        "详细",
        "讲了什么",
        "主要内容",
        "内容简介",
        "故事梗概",
        "剧情",
        "情节",
        "简介",
        "解析",
        "原理",
        "流程",
        "实现",
        "应用",
        "区别",
        "对比",
        "比较",
        "差异",
        "优缺点",
    }

    for token in re.findall(r"[a-zA-Z][a-zA-Z0-9+-]{1,}", query):
        normalized_token = token.lower()
        if normalized_token not in english_stop_words and normalized_token not in keywords:
            keywords.append(normalized_token)

    for raw_token in re.findall(r"[一-鿿]{2,}", query):
        token = raw_token
        token = re.sub(r"^(请问|帮我|介绍一下|解释一下|什么是|何为)", "", token)
        token = re.sub(r"(是什么|讲了什么|主要内容|内容简介|故事梗概)$", "", token)
        if len(token) < 2 or token in chinese_stop_phrases:
            continue
        if token not in keywords:
            keywords.append(token)

    return keywords


def count_keyword_hits(text: str, keywords: list[str]) -> int:
    """统计文本命中的 query 关键词数量。"""
    normalized_text = str(text or "").lower()
    return sum(1 for keyword in keywords if keyword.lower() in normalized_text)


def calculate_domain_quality(url: str, title: str = "") -> float:
    """用通用域名/路径形态估算来源质量，不绑定具体主题。"""
    normalized_url = str(url or "").lower()
    try:
        parsed = urlsplit(normalized_url)
        domain = parsed.hostname or ""
        path = parsed.path or ""
    except ValueError:
        domain = ""
        path = normalized_url
    normalized_title = str(title or "").lower()
    quality = 0.0

    if domain.endswith(".gov") or ".gov." in domain or domain.endswith(".edu") or ".edu." in domain:
        quality += 0.35
    if domain.startswith(("docs.", "developer.", "learn.")):
        quality += 0.2
    if re.search(r"/(docs|documentation|reference|manual|learn|guide|wiki)(/|$)", path):
        quality += 0.12
    if re.search(r"(blog|forum|bbs|question|questions|ask|answers)", domain + path):
        quality -= 0.18
    if re.search(r"转载|搬运|个人博客|论坛|问答", normalized_title):
        quality -= 0.16

    return quality


def calculate_web_quality_score(item: dict[str, Any], query: str) -> float:
    """结合搜索 rank、召回分数、关键词重合和来源形态计算质量分。"""
    url = str(item.get("canonical_url") or item.get("final_url") or item.get("url") or "")
    title = str(item.get("title") or "")
    snippet = str(item.get("snippet") or item.get("preview") or "")
    content = str(item.get("content") or "")
    keywords = extract_query_keywords(query)
    rank_score = max(safe_float(item.get("rank") or item.get("search_rank")), 0.0)
    retrieval_score = max(safe_float(item.get("score")), 0.0)

    quality = 0.0
    quality += min(rank_score, 10.0) * WEB_SEARCH_RANK_WEIGHT
    quality += retrieval_score * WEB_RETRIEVAL_SCORE_WEIGHT
    quality += min(count_keyword_hits(title, keywords), 4) * WEB_SEARCH_TITLE_WEIGHT
    quality += min(count_keyword_hits(f"{snippet} {content[:1000]}", keywords), 4) * WEB_SEARCH_SNIPPET_WEIGHT
    quality += calculate_domain_quality(url, title)

    if item.get("content_source") == "page":
        quality += 0.05
    if item.get("fetch_error"):
        quality -= 0.08

    return round(quality, 6)


def rank_web_search_results(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """抓取正文前按通用质量信号重排搜索结果。"""
    ranked: list[tuple[float, float, int, dict[str, Any]]] = []
    for index, result in enumerate(results):
        item = dict(result)
        item["search_quality_score"] = calculate_web_quality_score(item, query)
        ranked.append((
            safe_float(item["search_quality_score"]),
            safe_float(item.get("rank")),
            -index,
            item,
        ))

    ranked.sort(reverse=True)
    return [item for _, _, _, item in ranked]


def rank_web_source_candidates(candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """对正文召回后的候选片段做通用质量排序。"""
    ranked: list[tuple[float, float, int, dict[str, Any]]] = []
    for index, candidate in enumerate(candidates):
        source = dict(candidate)
        source["quality_score"] = calculate_web_quality_score(source, query)
        ranked.append((
            safe_float(source["quality_score"]),
            safe_float(source.get("score")),
            -index,
            source,
        ))

    ranked.sort(reverse=True)
    return [source for _, _, _, source in ranked]
