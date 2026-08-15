# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Chrome MV3 side-panel AI assistant with **two largely independent subsystems** that share one FastAPI backend and one extension frontend:

1. **Browser automation agent** — drives web pages from natural-language commands (click/type/scroll/hover/select). This is the actively developed core. Tested against JD internal enterprise platforms (xingyun.jd.com / coding.jd.com; jmtd / Ant Design / Element UI component libraries).
2. **Page RAG + chat** — OpenAI-compatible chat, current-page RAG, snapshot indexing into Qdrant, SQLite metadata. Documented in `README.md`.

These do not depend on each other. Work on the agent rarely touches RAG and vice versa. `app.py` mounts RAG/search/pages routers inside a `try/except` so the agent + logs routers still load when RAG deps (qdrant-client, numpy, trafilatura) are absent.

## Commands

Backend runs on Windows PowerShell; a Bash tool is also available. Working dir for backend commands is `backend/`.

```powershell
# Setup
cd backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# Run backend (port 8000)
.\.venv\Scripts\python.exe app.py

# Backend self-tests (RAG side)
.\.venv\Scripts\python.exe -B test\test_page_identity.py
.\.venv\Scripts\python.exe -B test\test_rag_refresh_flow.py

# Frontend syntax check (no build step — plain JS)
node --check extension\background.js
node --check extension\sidepanel.js
```

Load the extension: `chrome://extensions` → developer mode → load unpacked → select `extension/`. Front-end API Base URL = `http://localhost:8000/v1`.

**CRITICAL — backend restart:** uvicorn `--reload` does NOT reliably pick up changes here. After ANY backend edit you MUST kill the python process on port 8000 and restart `app.py`. Config lives in `backend/config/.env` (gitignored; template `backend/config/.env.example`) — key vars: `MODEL_BASE_URL`, `OPENAI_API_KEY`, `AGENT_MODE`.

There is no lint/build step for the extension (vanilla JS, `node --check` only). There is no test suite for the agent subsystem — verification is done by running real tasks and reading the JSONL logs (see below).

## Browser automation agent — architecture

This is the part that requires reading multiple files to understand. Read `docs/architecture.md` for the full write-up; the essentials:

**Index-direct locating (the core contract).** `observePageState()` in `extension/sidepanel.js` walks the DOM (piercing open shadow roots), tags every interactive element with `data-agent-id="N"`, and sends a numbered list to the backend. The LLM only ever returns an element **index**; `resolveByIndex(index)` in the frontend does `querySelector([data-agent-id="N"])` to hit the exact node. There is NO css/text fuzzy matching — that was removed because it caused wrong-element clicks. If the index no longer resolves (page re-rendered), the frontend returns `{stale:true}` and the loop re-observes.

**Structured-output verify-first loop (backend `agent/loop.py`).** Single LLM call per step returning ONE JSON object:
`{evaluation_previous_goal, memory, next_goal, plan?, current_plan_item?, action}`.
`evaluation_previous_goal`/`memory`/`next_goal`/`action` are required. The LLM must judge whether its *previous* action actually worked (against the new observation) before choosing the next action. eval + memory are written back into message history so state flows across the stateless LLM calls. This is modeled on browser-use; the system prompt in `agent/context_builder.py` (`SYSTEM_PROMPT`) is the source of truth for the output contract and behavior rules — edit prompt behavior there.

**Four-layer click (frontend `executePageAction`).** Per browser-use: (1) `scrollIntoView`; (2) occlusion check via `elementFromPoint`; (3a) not occluded → dispatch REAL mouse events through Chrome Debugger Protocol (`background.js` `handleDebuggerClick`, gives `isTrusted=true` so CSS `:hover` menus work); (3b) occluded → `element.click()` in-page to bypass the covering layer; (4) debugger unavailable → synthetic events. Hover uses the same debugger path (`mouseMoved`) so hover-to-expand enterprise menus work.

**Request flow:** `sidepanel.js runAgentTask()` loop → `POST /v1/agent/{execute,step,cancel}` (`api/agent.py`) → `agent/loop.py run_step()`. Session state (`AgentSession` in `agent/state.py`) is in-memory in the backend, keyed by session_id, with TTL cleanup. `agent/router.py` decides which actions need user confirmation. `tools/tool_registry.py` is now just the `ALLOWED_ACTION_TYPES` whitelist (function-calling schemas were dropped when the loop moved to structured output).

**Observability.** `observability/logger.py` writes structured JSONL to `backend/logs/agent_YYYY-MM-DD.jsonl`. Key events: `llm_step` (eval/memory/next_goal/action per step), `execution_result` (action + resolved target tag/text/role), `step_result` (ok/fail/stale + state_changes), `observation` (url/title/text_len/head/tail), `session_complete`/`max_steps_exceeded`. **This is the primary debugging tool** — after a run, read the latest session's events to diagnose what the agent saw and did, rather than guessing.

## Design lineage (important context)

The agent was heavily refactored. Earlier versions had: dual-LLM planning (removed — external planning framed the LLM), a knowledge base with recorded-path replay (removed — injecting historical paths hurt varied-task success), css/text fuzzy matching (removed — wrong clicks), and code-layer stall/dead-click detection (removed when structured output's forced self-evaluation replaced it). The current architecture deliberately mirrors browser-use's philosophy: **verify against ground truth at every layer, fall back gracefully at every failure**. When considering re-adding "smart" mechanisms, check whether the structured self-eval or a prompt rule already covers it before adding code.

When aligning behavior with browser-use, fetch and quote its actual source (`github.com/browser-use/browser-use`, e.g. `agent/system_prompts/system_prompt.md`, `agent/views.py`, `browser/watchdogs/default_action_watchdog.py`) rather than working from memory — several past mistakes came from guessing its behavior.

## Page RAG subsystem

Covered in `README.md`. Page identity (`backend/common/page_identity.py`): `page_id` (normalized URL) → `content_hash` (cleaned text) → `snapshot_id`. Endpoints: `/v1/chat/completions`, `/api/pages/refresh_snapshot`, `/search`. Qdrant holds chunk embeddings, SQLite holds page/snapshot/chat bindings. `QDRANT_VECTOR_SIZE` must match the embedding model's output dimension.

## Conventions

- `docs/` is gitignored (local design docs — `architecture.md`, `full-refactor-plan.md`, etc.). Do not assume it's version-controlled.
- Runtime artifacts stay out of git: `backend/logs/*.jsonl`, `backend/config/.env`, SQLite/Qdrant data, `backend/backend_run.log`. When committing, stage source files explicitly rather than `git add -A`.
- Commit messages in this repo are English, Conventional-Commits style (`feat:`/`fix:`/`refactor:`), with a body explaining the "why".
