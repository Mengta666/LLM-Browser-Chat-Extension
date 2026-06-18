"""文本清洗工具，用于统一页面正文和查询文本格式。"""

import re


def clean_page_text(text: str) -> str:
    """规范换行和空白字符，输出便于切块与检索的文本。"""
    if not isinstance(text, str):
        return ""

    # 先把不同平台的换行统一成 \n。
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 再压缩每一行内部的连续空白。
    lines = text.split("\n")
    clean_lines = []
    for line in lines:
        line = re.sub(r"[ \t\f\v]+", " ", line).strip()
        clean_lines.append(line)

    result = "\n".join(clean_lines)
    # 保留段落感，但避免出现过多空行。
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
