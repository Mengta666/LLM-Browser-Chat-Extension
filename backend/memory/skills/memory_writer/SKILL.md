---
name: memory-writer
description: Classify completed Browser Agent chat turns into durable memory decisions. Use when extracting, updating, superseding, or rejecting memories from user conversations, project decisions, procedural feedback, episodic lessons, or external reference leads.
---

# Memory Writer

Use this skill to convert one completed chat turn into memory decisions. Output strict JSON only. Do not output Markdown.

## Goal

Store only information that can improve future answers, planning, implementation, debugging, review, or interaction. When uncertain, choose `noop`.

## Memory Types

`user_profile`: Long-term user preferences, language style, answer style, detail level, and interaction habits.

`project_state`: Current project background, goals, tech stack, architecture decisions, progress, constraints, and completed work.

`task_state`: Short-term task state, current TODOs, blockers, next steps, and pending decisions.

Task state is scoped to the current `chat_id`. It is agent workflow state, not a global long-term user memory.

`procedural_feedback`: Long-term corrections to how the agent should work, such as code design rules, review/debugging rules, planning order, or requirements to inspect code before answering.

`episodic_lesson`: Lessons from a past incident or debugging episode that should guide future diagnosis.

`external_knowledge_ref`: External document, article, framework, benchmark, or reference lead. This is not a user preference and not a cited fact source.

## Write Rules

- Save explicit user preferences, user-confirmed project decisions, durable constraints, current task state, and concrete lessons.
- Do not save webpage body text, search result body text, assistant speculation, temporary one-off questions, failed turns, secrets, tokens, passwords, or private credentials.
- If new information duplicates an old memory, use `update` instead of `insert`.
- If new information conflicts with an old memory and replaces it, use `supersede`.
- If the content is only useful for the current answer and not future behavior, use `noop`.

## Evidence Boundaries

- `user_profile` must be based only on explicit user text in `query_text`, `focus_text`, or `user_message`, plus already stored user-profile evidence when updating an existing memory.
- `procedural_feedback` must be based only on explicit user corrections or workflow requirements in user text, plus already stored procedural-feedback evidence when updating an existing memory.
- Never infer user preferences from `assistant` wording, assistant answer structure, examples invented by the assistant, or retrieved source content.
- When updating an old memory, preserve old supported information, but add only the new details that are directly supported by the current user text.
- If the user says "专业全面回答", store only that preference. Do not expand it into unstated requirements such as specific reasoning frameworks, production-grade code, error handling, type definitions, complexity analysis, or best practices unless the user explicitly said those words.
- `assistant_final_answer` or assistant previews, if present in diagnostic input, are only for debugging the job. They are not evidence for user preferences or procedural feedback.
- `task_state` may use explicit user task instructions, assistant execution/completion statements, and local trace/test summaries as evidence because it tracks agent task progress.
- If assistant says a task is complete but the user later says it is not complete, has a bug, or needs more work in the same chat, update the same `task_state` to `reopened`.

## Task Status

For `task_state`, include optional fields:

- `task_status`: one of `open`, `in_progress`, `blocked`, `done`, `reopened`, `cancelled`.
- `task_updated_by`: one of `user`, `assistant`, `system`.

Use these meanings:

- `open`: user has assigned or clarified a task.
- `in_progress`: assistant is actively working or has started implementation.
- `blocked`: task needs user input, environment access, or an external condition.
- `done`: assistant believes the task is complete.
- `reopened`: user says a done/in-progress task is still incomplete or wrong.
- `cancelled`: user cancels, defers, or explicitly says not to do the task.

Terminal task states `done` and `cancelled` remain recorded but should not be treated as current task context unless reopened.

## Scoring

- `importance=0.3`: weak preference or low-impact reference lead.
- `importance=0.5`: ordinary user preference or project fact.
- `importance=0.7`: explicit long-term preference, confirmed project decision, clear next step, or useful lesson.
- `importance=0.9`: strong constraint, repeated correction, or rule that significantly changes future behavior.
- `confidence` measures evidence quality.
- `stability` measures how likely the memory remains true over time.

## Required JSON Shape

Read `schema.json` for exact enums and required fields. Every decision must include:

- `action`
- `memory_type`
- `content`
- `evidence`
- `classification_reason`
  - Explain the classification in user-facing language.
  - Explain why the decision is not another commonly confused type when relevant, especially `user_profile` vs `procedural_feedback`, `task_state` vs `procedural_feedback`, and `project_state` vs `task_state`.
  - Do not mention internal identifiers such as `mem_xxx`, `chat_xxx`, `turn_xxx`, `msg_xxx`, or `memjob_xxx`.
- `mode_affinity`
- `tags`
- `importance`
- `confidence`
- `stability`
- `target_memory_id`
- `related_memory_ids`

Read `examples.md` before classifying ambiguous memories.

## Ambiguous Type Rules

- Future/default answer language, depth, style, or tone belongs to `user_profile`, not `procedural_feedback`.
- Current TODOs, next steps, blockers, or pending decisions belong to `task_state`, not `procedural_feedback`.
- Agent workflow corrections, code design rules, review/debug rules, and requirements to inspect code before answering belong to `procedural_feedback`, not `task_state`.
- Current project goals, tech stack, architecture decisions, progress, or constraints belong to `project_state`; only immediate next steps belong to `task_state`.
