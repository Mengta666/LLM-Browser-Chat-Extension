# -*- coding: utf-8 -*-
"""HTTP 端到端验证:A4 A5 G1 G3 —— 需 8000 后端就绪"""
import sys, io, time, json
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).absolute().parents[1]))

import requests
BASE = "http://localhost:8000"

# 网关实际可用的模型(与 MEMORY_MODEL 同源,已知能通)
import os
from dotenv import load_dotenv
load_dotenv(Path(__file__).absolute().parents[1] / "config" / ".env")
CHAT_MODEL = os.getenv("MEMORY_MODEL") or "gpt-4o"

from agent.memory import vector as V
from agent.memory.config import CHAT_USER_ID

results = []
def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    results.append((tag, name))
    line = f"{tag} {name}"
    if detail:
        line += f" | {detail}"
    print(line, flush=True)

def clean():
    for m in V.scroll_memories(user_id=CHAT_USER_ID, limit=1000, include_invalid=True):
        V.delete_memory(m["memory_id"])

clean()

# ===== A4 白名单穷举 =====
for bad in ["persona", "preference", "random_xxx", ""]:
    r = requests.post(f"{BASE}/v1/memory",
                      json={"content":"t","memory_type":bad}, timeout=30)
    check(f"A4 拒绝 memory_type='{bad}'", r.status_code == 400,
          f"got {r.status_code}")

# ===== A5 手动条 confidence = 0.7 =====
r = requests.post(f"{BASE}/v1/memory",
                  json={"content":"手动条测试内容","memory_type":"core"}, timeout=60)
check("A5 手动 POST core 返 200", r.status_code == 200, f"got {r.status_code}")
if r.status_code == 200:
    mid = r.json()["memory_id"]
    p = V.get_memory(mid)
    check("A5 手动条 confidence=0.7", abs(p["confidence"] - 0.7) < 1e-6,
          f"confidence={p['confidence']}")
    check("A5 手动条 verified=True", p["verified"] is True,
          f"verified={p['verified']}")

# ===== G1 完整 chat 链路(非流式) =====
clean()
chat_body = {
    "model": CHAT_MODEL,
    "messages": [{"role":"user","content":"我叫张三,做数据科学,主要用 Python"}],
    "stream": False,
    "chat_id": "sess_G1"
}
r = requests.post(f"{BASE}/v1/chat/completions", json=chat_body, timeout=120)
check("G1 /chat/completions 非流式 200", r.status_code == 200,
      f"status={r.status_code}, body={r.text[:100]}")
if r.status_code == 200:
    reply = r.json().get("choices",[{}])[0].get("message",{}).get("content","")
    check("G1 有 assistant 回复", len(reply) > 0, f"reply_len={len(reply)}")

# 等后台异步写入(N=3 去抖:第一轮不写,history_summary 补前面轮次)
# 触发第 3 轮以让 debounce 满足
for i in range(2):
    r2 = requests.post(f"{BASE}/v1/chat/completions", json={
        "model": CHAT_MODEL,
        "messages": [
            {"role":"user","content":"我叫张三,做数据科学,主要用 Python"},
            {"role":"assistant","content":"了解"},
            {"role":"user","content":"这是我第 " + str(i+2) + " 轮消息"}
        ],
        "stream": False,
        "chat_id": "sess_G1"
    }, timeout=120)

# 给后台 daemon 时间抽取(真实观测:一轮抽取约 20-40s,polling 直到看到 core 或超 60s)
deadline = time.time() + 60
n_core = 0
while time.time() < deadline:
    time.sleep(3)
    lst = requests.get(f"{BASE}/v1/memory/list", timeout=30).json()
    n_core = sum(1 for m in lst["memories"] if m["memory_type"] == "core")
    if n_core >= 1:
        break
check("G1 后台 daemon 抽出 core", n_core >= 1, f"list count={lst['count']}, core={n_core}")

# ===== G3 注入生效(建1条 core → 再发 chat → 后端日志可见注入) =====
clean()
r = requests.post(f"{BASE}/v1/memory",
                  json={"content":"用户偏好回答用中文简洁","memory_type":"core"}, timeout=60)
if r.status_code == 200:
    r2 = requests.post(f"{BASE}/v1/chat/completions", json={
        "model": CHAT_MODEL,
        "messages":[{"role":"user","content":"你好"}],
        "stream": False,
        "chat_id":"sess_G3"
    }, timeout=120)
    check("G3 有 core 时 chat 请求成功", r2.status_code == 200,
          f"status={r2.status_code}")
    # 注入生效通过读日志验证(chat_memory_injected 事件里 core_injected>=1)
    # 这里给个 sleep 让日志落盘
    time.sleep(1)
    # 简单读最新日志文件的 chat_memory_injected 事件
    from datetime import date
    logf = Path(__file__).absolute().parents[1] / "logs" / f"chat_{date.today().isoformat()}.jsonl"
    if logf.exists():
        with open(logf, encoding="utf-8") as f:
            recent = f.readlines()[-30:]
        injected = [json.loads(l) for l in recent
                    if '"chat_memory_injected"' in l]
        if injected:
            latest = injected[-1]["data"]
            check("G3 日志显示 core_injected>=1",
                  latest.get("core_injected", 0) >= 1,
                  f"core_injected={latest.get('core_injected')}")
        else:
            check("G3 日志无 chat_memory_injected 事件", False, "logs 未落盘或事件名不同")
    else:
        check("G3 日志文件存在", False, f"{logf} 不存在")

print()
print("=" * 40)
passed = sum(1 for t,_ in results if t == "PASS")
print(f"HTTP: {passed}/{len(results)} pass")
for t, n in results:
    if t == "FAIL":
        print(f"  ! {n}")
