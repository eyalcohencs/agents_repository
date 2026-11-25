# Multi-Agent Engineering Workflow

All agents collaborate through shared repository artifacts in `docs/`:
1. `docs/PRD.md` -> Input written by the human.
2. `docs/SPEC.md` & `docs/TASKS.md` -> Owned by `product-manager`.
3. `docs/ARCHITECTURE.md` -> Owned by `architect`.
4. Application code -> Owned by `fullstack-dev` (and `debugger` on failures).
5. Automated tests & `docs/QA_REPORT.md` -> Owned by `qa-engineer`.
6. Security & code sign-off -> Owned by `code-reviewer` and `security-auditor`.

Strict Rules:
- Never skip stages. Downstream agents must read the upstream `.md` artifacts before acting.
- If two agents disagree on scope vs. feasibility, pause and ask the human user to decide.
- Update `docs/TASKS.md` status (`[ ]` -> `[~]` -> `[x]`) as each task moves through Dev, QA, and Review.
- **Credit-Safe Batch Execution:** Run **only ONE batch from `docs/TASKS.md` §1.3 per session** (2–3 tasks max unless the user explicitly asks for more), spawn **at most 3 concurrent workers**, commit each completed task immediately so progress is never lost, read only relevant line ranges of large docs, and **stop after the batch finishes** so the user can check remaining credits before starting the next batch.
