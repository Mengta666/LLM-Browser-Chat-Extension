"""聊天链路接入联网搜索的轻量回归测试。"""

import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

import api.chat as chat


def test_rewrite_web_search_query_uses_generic_intents() -> None:
    """验证搜索词改写只依赖通用意图，不写死具体主题。"""
    definition_query, definition_rewritten = chat.rewrite_web_search_query("星河协议是什么？")
    story_query, story_rewritten = chat.rewrite_web_search_query("青灯录 断桥 讲了什么？")
    compare_query, compare_rewritten = chat.rewrite_web_search_query("甲方案和乙方案有什么区别？")

    assert definition_rewritten is True
    assert "星河协议" in definition_query
    assert "定义" in definition_query
    assert "原理" in definition_query
    assert "应用" in definition_query
    assert story_rewritten is True
    assert "青灯录" in story_query
    assert "断桥" in story_query
    assert "简介" in story_query
    assert "情节" in story_query
    assert "解析" in story_query
    assert compare_rewritten is True
    assert "区别" in compare_query
    assert "对比" in compare_query
    assert "优缺点" in compare_query


def test_extract_query_keywords_removes_question_words() -> None:
    """验证关键词提取会去掉疑问词和通用补充词。"""
    keywords = chat.extract_query_keywords("什么是星河协议？定义 原理 应用")

    assert "星河协议" in keywords
    assert "什么是" not in keywords
    assert "定义" not in keywords
    assert "原理" not in keywords
    assert "应用" not in keywords


def test_rank_web_search_results_uses_generic_quality_signals() -> None:
    """验证搜索结果排序会综合通用质量信号而非只看原始 rank。"""
    ranked = chat.rank_web_search_results(
        [
            {
                "title": "星河协议 个人博客",
                "url": "https://blog.example.com/post/star-protocol",
                "snippet": "星河协议 定义",
                "rank": 10,
            },
            {
                "title": "星河协议 Reference",
                "url": "https://archive.example.edu/reference/star-protocol",
                "snippet": "星河协议 definition",
                "rank": 1,
            },
        ],
        "星河协议是什么 定义 原理 应用",
    )

    assert ranked[0]["url"].startswith("https://archive.example.edu/")
    assert ranked[0]["search_quality_score"] > ranked[1]["search_quality_score"]


def test_rank_web_source_candidates_uses_generic_quality_signals() -> None:
    """验证正文候选 source 排序会优先更可靠的域名和内容。"""
    ranked = chat.rank_web_source_candidates(
        [
            {
                "source_id": "",
                "type": "web",
                "source_kind": "web_search",
                "url": "https://blog.example.com/post/star-protocol",
                "canonical_url": "https://blog.example.com/post/star-protocol",
                "domain": "blog.example.com",
                "title": "星河协议 个人博客",
                "content": "星河协议 定义",
                "preview": "星河协议 定义",
                "score": 0.9,
            },
            {
                "source_id": "",
                "type": "web",
                "source_kind": "web_search",
                "url": "https://archive.example.edu/reference/star-protocol",
                "canonical_url": "https://archive.example.edu/reference/star-protocol",
                "domain": "archive.example.edu",
                "title": "星河协议 Reference",
                "content": "星河协议 definition",
                "preview": "星河协议 definition",
                "score": 0.55,
            },
        ],
        "星河协议是什么 定义 原理 应用",
    )
    sources = chat.select_web_sources(ranked, source_start_index=1)

    assert sources[0]["domain"] == "archive.example.edu"
    assert sources[0]["source_id"] == "S1"
    assert sources[0]["quality_score"] > sources[1]["quality_score"]


def test_build_message_injects_web_search_sources() -> None:
    """验证普通聊天构造会注入联网搜索上下文和引用约束。"""
    original_search_web = chat.search_web
    original_fetch_url = chat.fetch_url
    original_retrieve_web_context = chat.retrieve_web_context
    original_max_query_chars = chat.MAX_RETRIEVAL_QUERY_CHARS

    def fake_search_web(query: str, top_k: int) -> dict:
        """模拟搜索接口，并确认检索词已按长度截断。"""
        assert len(query) <= 12
        return {
            "results": [
                {
                    "title": "Search Result",
                    "url": "https://example.com/result",
                    "snippet": "short snippet",
                }
            ],
            "unresponsive_engines": [],
        }

    def fake_fetch_url(url: str) -> dict:
        """模拟网页正文抓取。"""
        return {
            "title": "Fetched Page",
            "final_url": url,
            "content": "fetched page content about current facts",
            "content_length": 40,
        }

    def fake_retrieve_web_context(query: str, results: list[dict], top_k_results: int, top_k_chunks: int) -> dict:
        """模拟正文 chunk 召回。"""
        return {
            "results": [
                {
                    **results[0],
                    "matches": [
                        {
                            "title": "Fetched Page",
                            "url": "https://example.com/result",
                            "preview": "matched preview",
                            "content": "matched web content",
                            "score": 0.91,
                            "source_key": "web:0:0",
                        }
                    ],
                }
            ],
            "retrieved_page_count": 1,
            "retrieved_chunk_count": 1,
        }

    chat.search_web = fake_search_web
    chat.fetch_url = fake_fetch_url
    chat.retrieve_web_context = fake_retrieve_web_context
    chat.MAX_RETRIEVAL_QUERY_CHARS = 12

    try:
        item = chat.Chat(
            model="test-model",
            messages=[chat.Message(role="user", content="查一下最新信息")],
            query_text="查一下最新信息，并补充很长的条件",
            use_web_search=True,
        )
        messages, sources, task_type, stats = chat.build_message(item)
    finally:
        chat.search_web = original_search_web
        chat.fetch_url = original_fetch_url
        chat.retrieve_web_context = original_retrieve_web_context
        chat.MAX_RETRIEVAL_QUERY_CHARS = original_max_query_chars

    assert task_type == "chat"
    assert len(sources) == 1
    assert sources[0]["source_id"] == "S1"
    assert sources[0]["type"] == "web"
    assert "matched web content" in sources[0]["content"]
    assert sources[0]["matched_chunk_count"] == 1
    assert stats["web_source_count"] == 1
    assert stats["web_retrieved_chunk_count"] == 1
    assert stats["retrieval_query_truncated"] is True
    assert stats["web_search_query_rewritten"] is False
    assert stats["web_resolved_search_query_length"] == 12
    assert any("联网搜索上下文" in message["content"] for message in messages)
    assert any("多个来源内容相似时" in message["content"] for message in messages if message["role"] == "system")
    assert any("必须至少引用一个来源" in message["content"] for message in messages if message["role"] == "system")


def test_web_context_guides_supplement_when_page_context_exists() -> None:
    """验证当前页和联网搜索同时存在时，会提示联网结果只作补充校验。"""
    original_collect_web_sources = chat.collect_web_sources

    def fake_collect_web_sources(query: str, source_start_index: int) -> tuple[list[dict], dict]:
        """返回一个从指定编号开始的联网 source。"""
        return [
            {
                "source_id": f"S{source_start_index}",
                "type": "web",
                "source_kind": "web_search",
                "title": "Supplement",
                "url": "https://example.com/supplement",
                "content": "supplemental web fact",
                "score": 0.8,
            }
        ], {
            "web_search_result_count": 1,
            "web_fetched_count": 1,
            "web_failed_count": 0,
            "web_retrieved_chunk_count": 1,
            "web_source_count": 1,
            "web_top_source_domains": ["example.com"],
            "web_search_error": "",
        }

    chat.collect_web_sources = fake_collect_web_sources
    try:
        messages, sources, stats = chat.build_web_context_messages(
            "explain",
            "这是什么？",
            "Qwen3 Embedding 4B",
            True,
            "",
            source_start_index=2,
            has_page_context=True,
        )
    finally:
        chat.collect_web_sources = original_collect_web_sources

    system_prompt = messages[0]["content"]
    assert "当前页用于定位正在处理的对象" in system_prompt
    assert "联网搜索结果用于补充、校验和扩展当前页没有覆盖的信息" in system_prompt
    assert "不要硬用" in system_prompt
    assert "必须在对应句子后标注来源编号" in system_prompt
    assert sources[0]["source_id"] == "S2"
    assert stats["web_source_count"] == 1


def test_build_message_degrades_when_web_search_fails() -> None:
    """验证联网搜索失败时聊天链路降级，不中断消息构造。"""
    original_search_web = chat.search_web

    def fake_search_web(query: str, top_k: int) -> dict:
        """模拟搜索后端不可用。"""
        raise RuntimeError("search backend unavailable")

    chat.search_web = fake_search_web
    try:
        item = chat.Chat(
            model="test-model",
            messages=[chat.Message(role="user", content="查一下最新信息")],
            query_text="查一下最新信息",
            use_web_search=True,
        )
        messages, sources, _, stats = chat.build_message(item)
    finally:
        chat.search_web = original_search_web

    assert sources == []
    assert stats["web_source_count"] == 0
    assert "search backend unavailable" in stats["web_search_error"]
    assert any("本次搜索失败" in message["content"] for message in messages)


def test_collect_web_sources_deduplicates_url_and_keeps_metadata() -> None:
    """验证联网 sources 会按 URL/domain 去重限额并保留元数据。"""
    original_search_web = chat.search_web
    original_fetch_url = chat.fetch_url
    original_retrieve_web_context = chat.retrieve_web_context
    original_max_chunks_per_url = chat.WEB_MAX_CHUNKS_PER_URL
    original_max_sources_per_domain = chat.WEB_MAX_SOURCES_PER_DOMAIN
    original_max_sources = chat.WEB_MAX_SOURCES

    def fake_search_web(query: str, top_k: int) -> dict:
        """返回同域多结果和跨域结果。"""
        return {
            "results": [
                {"title": "A1", "url": "https://example.com/a?utm_source=x#top", "snippet": "a1"},
                {"title": "A2", "url": "https://example.com/b", "snippet": "a2"},
                {"title": "A3", "url": "https://example.com/c", "snippet": "a3"},
                {"title": "B1", "url": "https://other.example.org/page", "snippet": "b1"},
            ],
            "unresponsive_engines": [],
        }

    def fake_fetch_url(url: str) -> dict:
        """模拟每个搜索结果都能抓到正文。"""
        return {
            "title": f"Fetched {url}",
            "final_url": url,
            "content": f"content from {url}",
            "content_length": 20,
        }

    def fake_retrieve_web_context(query: str, results: list[dict], top_k_results: int, top_k_chunks: int) -> dict:
        """为每个结果返回两个 chunk，用于测试每 URL chunk 限额。"""
        processed = []
        for index, result in enumerate(results):
            url = result["final_url"] or result["url"]
            processed.append(
                {
                    **result,
                    "matches": [
                        {
                            "title": result["title"],
                            "url": url,
                            "preview": f"preview {index}-0",
                            "content": f"content {index}-0",
                            "score": 0.9 - index * 0.01,
                            "source_key": f"web:{index}:0",
                        },
                        {
                            "title": result["title"],
                            "url": url,
                            "preview": f"preview {index}-1",
                            "content": f"content {index}-1",
                            "score": 0.8 - index * 0.01,
                            "source_key": f"web:{index}:1",
                        },
                    ],
                }
            )
        return {
            "results": processed,
            "retrieved_page_count": len(processed),
            "retrieved_chunk_count": len(processed) * 2,
        }

    chat.search_web = fake_search_web
    chat.fetch_url = fake_fetch_url
    chat.retrieve_web_context = fake_retrieve_web_context
    chat.WEB_MAX_CHUNKS_PER_URL = 1
    chat.WEB_MAX_SOURCES_PER_DOMAIN = 2
    chat.WEB_MAX_SOURCES = 6

    try:
        sources, stats = chat.collect_web_sources("rag", source_start_index=3)
    finally:
        chat.search_web = original_search_web
        chat.fetch_url = original_fetch_url
        chat.retrieve_web_context = original_retrieve_web_context
        chat.WEB_MAX_CHUNKS_PER_URL = original_max_chunks_per_url
        chat.WEB_MAX_SOURCES_PER_DOMAIN = original_max_sources_per_domain
        chat.WEB_MAX_SOURCES = original_max_sources

    assert [source["source_id"] for source in sources] == ["S3", "S4", "S5"]
    assert [source["domain"] for source in sources] == ["example.com", "example.com", "other.example.org"]
    assert sources[0]["canonical_url"] == "https://example.com/a"
    assert all(source["type"] == "web" for source in sources)
    assert all(source["source_kind"] == "web_search" for source in sources)
    assert all("content 2-" not in source["content"] for source in sources)
    assert stats["web_candidate_source_count"] == 4
    assert stats["web_source_count"] == 3


def test_select_web_sources_groups_chunks_by_url() -> None:
    """验证同一 URL 的多个 web chunk 会合并成一个文档级 source。"""
    candidates = [
        {
            "source_id": "",
            "type": "web",
            "source_kind": "web_search",
            "url": "https://example.com/a",
            "canonical_url": "https://example.com/a",
            "domain": "example.com",
            "title": "A",
            "content": "first chunk",
            "preview": "first chunk",
            "score": 0.8,
            "quality_score": 0.8,
        },
        {
            "source_id": "",
            "type": "web",
            "source_kind": "web_search",
            "url": "https://example.com/a",
            "canonical_url": "https://example.com/a",
            "domain": "example.com",
            "title": "A",
            "content": "second chunk",
            "preview": "second chunk",
            "score": 0.7,
            "quality_score": 0.7,
        },
    ]

    sources = chat.select_web_sources(candidates, source_start_index=1)

    assert len(sources) == 1
    assert sources[0]["source_id"] == "S1"
    assert sources[0]["matched_chunk_count"] == 2
    assert "first chunk" in sources[0]["content"]
    assert "second chunk" in sources[0]["content"]


def test_select_page_sources_groups_chunks_by_url() -> None:
    """验证当前页多个 chunk 会合并成一个 page source 并保留 chunk_ids。"""
    sources = chat.select_page_sources(
        [
            {
                "source_id": "S1",
                "type": "page",
                "source_kind": "current_page",
                "url": "https://example.com/page",
                "canonical_url": "https://example.com/page",
                "domain": "example.com",
                "title": "Page",
                "content": "page chunk one",
                "score": 0.8,
                "chunk_id": "chunk-1",
                "page_id": "page-1",
            },
            {
                "source_id": "S2",
                "type": "page",
                "source_kind": "current_page",
                "url": "https://example.com/page",
                "canonical_url": "https://example.com/page",
                "domain": "example.com",
                "title": "Page",
                "content": "page chunk two",
                "score": 0.7,
                "chunk_id": "chunk-2",
                "page_id": "page-1",
            },
        ]
    )

    assert len(sources) == 1
    assert sources[0]["source_id"] == "S1"
    assert sources[0]["matched_chunk_count"] == 2
    assert sources[0]["chunk_ids"] == ["chunk-1", "chunk-2"]
    assert "page chunk one" in sources[0]["content"]
    assert "page chunk two" in sources[0]["content"]


def test_prepare_cited_response_renumbers_sources() -> None:
    """验证最终回答会过滤并连续重编号被引用 sources。"""
    text = "这个方法可以降低错误率 [S5, S9]，也能提供来源 [S9]。"
    sources = [
        {"source_id": "S5", "url": "https://a.example", "title": "A", "content": "A", "score": 0.5},
        {"source_id": "S9", "url": "https://b.example", "title": "B", "content": "B", "score": 0.4},
    ]

    answer, filtered_sources, cited_ids = chat.prepare_cited_response(text, sources)

    assert answer == "这个方法可以降低错误率 [S1, S2]，也能提供来源 [S2]。"
    assert cited_ids == ["S1", "S2"]
    assert [source["source_id"] for source in filtered_sources] == ["S1", "S2"]
    assert filtered_sources[0]["url"] == "https://a.example"
    assert filtered_sources[1]["url"] == "https://b.example"
    assert "content" not in filtered_sources[0]
    assert filtered_sources[0]["reference_kind"] == "document"


def test_prepare_cited_response_falls_back_to_first_source() -> None:
    """验证强制引用时没有显式来源也会回退到第一个 source。"""
    sources = [
        {"source_id": "S3", "url": "https://a.example", "title": "A", "content": "A", "score": 0.5},
        {"source_id": "S4", "url": "https://b.example", "title": "B", "content": "B", "score": 0.4},
    ]

    answer, filtered_sources, cited_ids = chat.prepare_cited_response(
        "这是一个没有显式引用的回答。",
        sources,
        force_first_source=True,
    )

    assert answer == "这是一个没有显式引用的回答。 [S1]"
    assert cited_ids == ["S1"]
    assert len(filtered_sources) == 1
    assert filtered_sources[0]["source_id"] == "S1"
    assert filtered_sources[0]["url"] == "https://a.example"
    assert "content" not in filtered_sources[0]


if __name__ == "__main__":
    test_rewrite_web_search_query_uses_generic_intents()
    test_extract_query_keywords_removes_question_words()
    test_rank_web_search_results_uses_generic_quality_signals()
    test_rank_web_source_candidates_uses_generic_quality_signals()
    test_build_message_injects_web_search_sources()
    test_web_context_guides_supplement_when_page_context_exists()
    test_build_message_degrades_when_web_search_fails()
    test_collect_web_sources_deduplicates_url_and_keeps_metadata()
    test_select_web_sources_groups_chunks_by_url()
    test_select_page_sources_groups_chunks_by_url()
    test_prepare_cited_response_renumbers_sources()
    test_prepare_cited_response_falls_back_to_first_source()
    print("PASS chat web search flow")
