"""录制步骤意图补全。

用户录制的步骤只有原始 DOM 动作（click/type/select），没有高层意图。
保存前调一次 LLM，结合任务描述批量为每步生成 intent，
让召回时 LLM 能看到"这一步为了什么"。
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

__env_path = Path(__file__).resolve().parents[1] / "config" / ".env"
load_dotenv(dotenv_path=__env_path)

_client = OpenAI(
    base_url=os.getenv("MODEL_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
)

_ENRICH_PROMPT = """你是操作意图分析助手。用户录制了一段网页操作，你需要为每一步生成简短的意图说明。

## 任务目标
{task}

## 操作步骤（JSON数组）
{steps}

## 要求
- 为每一步生成一句话意图（说明这一步为了达成什么，不是复述动作）
- 只输出 JSON 数组，长度与步骤数一致：["意图1", "意图2", ...]
- 不要输出其他文字
"""


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def enrich_intents(task: str, steps: list[dict[str, Any]], model: str = "") -> list[dict[str, Any]]:
    """为缺少 intent 的步骤批量补全意图。失败时原样返回，不阻断保存。"""
    if not model:
        return steps
    # 已经都有 intent 就跳过
    if all(s.get("intent") for s in steps):
        return steps

    brief = [
        {k: v for k, v in s.items() if k in ("action", "target_text", "value", "text")}
        for s in steps
    ]
    prompt = _ENRICH_PROMPT.format(task=task, steps=json.dumps(brief, ensure_ascii=False))

    try:
        resp = _client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = _strip_think(resp.choices[0].message.content or "")
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return steps
        intents = json.loads(match.group(0))
        if not isinstance(intents, list):
            return steps
        for i, s in enumerate(steps):
            if i < len(intents) and not s.get("intent"):
                s["intent"] = str(intents[i])
        return steps
    except Exception:
        return steps
