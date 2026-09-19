"""按完整语义块选择原序窗口。预算不足时不截断表格、代码或半句话。"""

import hashlib
import math
import os
import re

from agent.token_utils import estimate_text_tokens, estimate_tokens
from .urls import normalize_web_url


OMISSION = "[中间内容已省略]"
COUNT_MODE = 'cl100k_estimate_1.25x' if estimate_tokens([])['method'] == 'tiktoken' else 'heuristic_estimate_1.25x'


class TurnWebBudget:
    def __init__(self):
        try:
            self.limit = max(128, min(16000, int(os.getenv("WEB_READER_CONTEXT_TOKENS", "6000"))))
        except ValueError:
            self.limit = 6000
        self.used = 0
        self.seen = {}

    @property
    def remaining(self):
        return max(0, self.limit - self.used)

    def allocate(self, results, query, context_tokens=None):
        available = min(self.remaining, max(0, context_tokens)) if context_tokens is not None else self.remaining
        for result in results:
            identity = normalize_web_url(getattr(result, 'final_url', '') or result.url)
            seen = self.seen.setdefault(identity, set())
            content = getattr(result, 'content', '') or result.snippet
            result.content_source = getattr(result, 'content_source', 'snippet')
            blocks = getattr(result, "blocks", None)
            # 优先完整窗口，同时为后续来源保留空间；不按成功页面数均分段落。
            cap = min(available, max(512, available // 2)) if len(results) > 1 else available
            details = {}
            excerpt, truncated, keys = select_blocks(content, query, cap, blocks, seen, details=details)
            cost = web_tokens(excerpt)
            result.content = excerpt
            result.context_status = ("included" if excerpt else "budget_exhausted" if cap <= 0 else
                                     "reused" if seen and details.get('available_count') == 0 else "no_complete_block")
            result.context_tokens = cost
            result.selected_blocks = len(keys)
            result.structure_mode = "blocks_v1" if blocks is not None else "legacy_text" if result.content_source == "page" else "snippet"
            result.truncated = getattr(result, 'truncated', False) or truncated
            if blocks is not None:
                del result.blocks
            seen.update(keys)
            self.used += cost
            available -= cost
        return {"limit": self.limit, "used": self.used, "remaining": self.remaining,
                "count_mode": COUNT_MODE, "scope": "user_turn"}


def web_tokens(text):
    # 当前网关没有匹配模型的 tokenizer；留出估算余量，不宣称精确计数。
    return (estimate_text_tokens(text) * 5 + 3) // 4


def validate_blocks(data):
    if "structure_version" not in data and "blocks" not in data:
        return
    blocks, text = data.get("blocks"), data["content"]
    if type(data.get("structure_version")) is not int or data["structure_version"] != 1 or not isinstance(blocks, list) or not 1 <= len(blocks) <= 2048:
        raise ValueError("invalid_structure")
    end = 0
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or set(block) != {"id", "type", "start", "end", "section", "level"}:
            raise ValueError("invalid_block")
        if (any(type(block[k]) is not int for k in ("id", "start", "end", "section", "level"))
                or block["id"] != index or not end <= block["start"] < block["end"] <= len(text)
                or text[end:block["start"]].strip() or not text[block["start"]:block["end"]].strip()
                or block["type"] not in ("heading", "paragraph", "list", "table", "code")
                or not -1 <= block["section"] <= index):
            raise ValueError("invalid_block")
        if block["type"] == "heading":
            if not 1 <= block["level"] <= 6 or block["section"] != index:
                raise ValueError("invalid_heading")
        elif block["level"] != 0:
            raise ValueError("invalid_level")
        if block["section"] >= 0 and blocks[block["section"]]["type"] != "heading":
            raise ValueError("invalid_section")
        end = block["end"]
    if text[end:].strip():
        raise ValueError("uncovered_text")


def legacy_blocks(text):
    blocks, section = [], -1
    # 旧服务缺少结构，只把显式 Markdown 标题识别为标题，不按短行猜测。
    lines = text.splitlines(keepends=True)
    index, offset = 0, 0
    while index < len(lines):
        if not lines[index].strip():
            offset += len(lines[index])
            index += 1
            continue
        start = offset
        fence = re.match(r'^ {0,3}(`{3,}|~{3,})', lines[index])
        is_heading = re.match(r'^#{1,6}\s+', lines[index])
        offset += len(lines[index])
        index += 1
        if fence:
            while index < len(lines):
                line = lines[index]
                offset += len(line)
                index += 1
                if re.fullmatch(r' {0,3}' + re.escape(fence[1][0]) + '{' + str(len(fence[1])) + r',}[ \t]*', line.rstrip('\r\n')):
                    break
        elif not is_heading:
            while index < len(lines) and lines[index].strip() and not re.match(r'^ {0,3}(`{3,}|~{3,}|#{1,6}\s+)', lines[index]):
                offset += len(lines[index])
                index += 1
        value = text[start:offset].rstrip()
        heading = re.fullmatch(r"(#{1,6})\s+[^\n]+", value)
        kind = ("heading" if heading else "code" if fence else "table" if value.startswith("|") else
                "list" if re.match(r'^\s*(?:[-*+] |\d+\. )', value) else "paragraph")
        if heading:
            section = len(blocks)
        blocks.append({"id": len(blocks), "type": kind, "start": start, "end": start + len(value),
                       "section": section, "level": len(heading[1]) if heading else 0})
    return blocks


def select_blocks(text, query, token_budget, blocks=None, excluded=None, *, details=None):
    blocks = blocks if blocks is not None else legacy_blocks(text)
    excluded = excluded or set()
    terms = set(re.findall(r"[a-z0-9][a-z0-9._-]+|[\u4e00-\u9fff]{1,2}", query.lower()))
    units = []
    for block in blocks:
        value = text[block["start"]:block["end"]]
        pieces = [value]
        # 超长段落先切完整句组；句子自身过长时跳过，不制造半句话。
        if block["type"] == "paragraph" and web_tokens(value) > 256:
            sentences = re.split(r'(?<=[。！？])|(?<=[.!?])\s+(?=[A-Z0-9\u4e00-\u9fff“"\'`])', value)
            pieces, group = [], ""
            for sentence in sentences:
                if group and web_tokens(group + " " + sentence) > 96:
                    pieces.append(group)
                    group = ""
                group += (" " if group else "") + sentence
            if group:
                pieces.append(group)
        for piece_index, piece in enumerate(pieces):
            key = hashlib.sha256((str(block["id"]) + ":" + str(piece_index) + ":" + piece).encode()).hexdigest()[:24]
            units.append({"text": piece, "block": block, "key": key})
    if token_budget <= 0:
        return "", True, []

    def render(indices):
        parts, previous = [], -2
        for i in sorted(indices):
            if i != previous + 1 and i != 0:
                parts.append(OMISSION)
            parts.append(units[i]["text"])
            previous = i
        if indices and max(indices) < len(units) - 1:
            parts.append(OMISSION)
        return "\n\n".join(parts)

    available = {i for i, u in enumerate(units) if u["key"] not in excluded}
    if details is not None:
        details['available_count'] = len(available)
    costs = [web_tokens(u["text"]) + 6 for u in units]
    sections = {}
    headings = {}
    for i in available:
        section = units[i]["block"]["section"]
        sections.setdefault(section, set()).add(i)
        if units[i]["block"]["type"] == "heading":
            headings.setdefault(units[i]["block"]["id"], set()).add(i)

    def fits(indices):
        ordered = sorted(indices)
        gaps = sum(i != j + 1 for i, j in zip(ordered, [-1, *ordered]))
        return sum(costs[i] for i in indices) + (gaps + 1) * web_tokens(OMISSION) <= token_budget

    scores = []
    for i, unit in enumerate(units):
        section = unit["block"]["section"]
        heading = text[blocks[section]["start"]:blocks[section]["end"]] if section >= 0 else ""
        value = unit["text"].lower()
        heading_text = re.sub(r'[\s_-]+', '', heading.lower())
        coverage = sum(3 * (term in value) + 3 * (re.sub(r'[\s_-]+', '', term) in heading_text) for term in terms)
        # 很长的列表天然命中更多词，不能仅按词数挤掉短小且有条件限定的段落。
        scores.append(coverage / math.sqrt(max(1, costs[i] / 256)))
    chosen = set()
    for index in sorted(available, key=lambda i: (-scores[i], i)):
        if index in chosen:
            continue
        section = units[index]["block"]["section"]
        neighbors = {i for i in range(max(0, index - 1), min(len(units), index + 2))
                     if i in available and units[i]["block"]["section"] == section}
        heading = headings.get(section, set())
        whole_section = sections.get(section, set()) if section >= 0 else set()
        candidates = [whole_section, neighbors | heading, {index} | heading]
        for window in candidates:
            if not window or index not in window:
                continue
            merged = chosen | window
            if fits(merged):
                chosen = merged
                break
    output = render(chosen)
    if web_tokens(output) > token_budget:
        return "", True, []
    return output, len(chosen) < len(units), [units[i]["key"] for i in sorted(chosen)]


def history_excerpt(text, max_chars=800):
    pieces, size = [], 0
    for block in legacy_blocks(text):
        value = text[block['start']:block['end']]
        candidates = [value]
        if block['type'] == 'paragraph' and len(value) > max_chars:
            candidates = re.split(r'(?<=[。！？])|(?<=[.!?])\s+', value)
        for piece in candidates:
            if size + len(piece) + 2 > max_chars:
                return '\n\n'.join(pieces)
            pieces.append(piece)
            size += len(piece) + 2
    return '\n\n'.join(pieces)
