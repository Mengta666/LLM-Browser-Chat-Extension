"""将提取树转换为顺序稳定、带原文偏移的块；不使用 xmltotxt。"""

import re


def _inline(node):
    parts = [node.text or ""]
    for child in node:
        parts.append("\n" if child.tag == "lb" else _inline(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _text(node):
    return re.sub(r"\s+", " ", _inline(node)).strip()


def _list(node):
    lines = []
    for index, item in enumerate(node, 1):
        parts = [item.text or ""]
        chunks = []
        for child in item:
            if child.tag in ("list", "code", "table", "p", "quote"):
                value = re.sub(r"\s+", " ", "".join(parts)).strip()
                if value:
                    chunks.append(value)
                parts = []
                chunks.append(_list(child) if child.tag == "list" else _code(child) if child.tag == "code"
                              else _table(child) if child.tag == "table" else _text(child))
            else:
                parts.append(_inline(child))
            parts.append(child.tail or "")
        value = re.sub(r"\s+", " ", "".join(parts)).strip()
        if value:
            chunks.append(value)
        marker = f"{index}." if node.get("rend") == "ol" else "-"
        value = "\n".join(chunks)
        pieces = value.splitlines() or [""]
        lines.append(marker + " " + pieces[0])
        lines.extend(" " * (len(marker) + 1) + line for line in pieces[1:])
    return "\n".join(lines)


def _code(node):
    code = _inline(node).replace("\r\n", "\n").strip("\n")
    fence = "`" * max(3, max((len(m) + 1 for m in re.findall(r"`+", code)), default=3))
    return fence + "\n" + code + "\n" + fence


def _table(node):
    lines = []
    for row in node.iter("row"):
        cells = [c for c in row if c.tag == "cell"]
        lines.append("| " + " | ".join(_text(c).replace("|", "\\|") for c in cells) + " |")
        if cells and all(c.get("role") == "head" for c in cells):
            lines.append("| " + " | ".join("---" for _ in cells) + " |")
    return "\n".join(lines)


def serialize_body(body, max_chars):
    records = []

    def visit(node):
        tag = node.tag
        if tag == "head":
            level = int(node.get("rend", "h2")[-1:]) if re.fullmatch(r"h[1-6]", node.get("rend", "")) else 2
            records.append(("heading", "#" * level + " " + _text(node), level))
        elif tag == "list":
            records.append(("list", _list(node), 0))
        elif tag == "table":
            records.append(("table", _table(node), 0))
        elif tag == "code":
            records.append(("code", _code(node), 0))
        elif tag in ("p", "quote"):
            records.append(("paragraph", _text(node), 0))
        else:
            if (node.text or "").strip():
                records.append(("paragraph", re.sub(r"\s+", " ", node.text).strip(), 0))
            for child in node:
                visit(child)
                if (child.tail or "").strip():
                    records.append(("paragraph", re.sub(r"\s+", " ", child.tail).strip(), 0))

    visit(body)
    records = [r for r in records if r[1].strip()]
    original_length = sum(len(r[1]) for r in records) + max(0, len(records) - 1) * 2
    content, blocks, section = "", [], -1
    for kind, value, level in records:
        start = len(content) + (2 if content else 0)
        if start + len(value) > max_chars:
            if kind != "paragraph":
                break
            sentences = re.findall(r".*?[。！？](?:[\"'”’])?|.*?[.!?](?:[\"'”’])?(?=\s|$)", value)
            prefix = ""
            for sentence in sentences:
                if start + len(prefix + sentence) > max_chars:
                    break
                prefix += sentence
            value = prefix.strip()
            if not value:
                break
        if kind == "heading":
            section = len(blocks)
        content += ("\n\n" if content else "") + value
        blocks.append({"id": len(blocks), "type": kind, "start": start, "end": len(content),
                       "section": section, "level": level})
        if len(blocks) >= 2048 or len(value) < len(records[len(blocks) - 1][1]):
            break
    return {"content": content, "blocks": blocks, "structure_version": 1,
            "content_length": original_length, "truncated": len(content) < original_length}
