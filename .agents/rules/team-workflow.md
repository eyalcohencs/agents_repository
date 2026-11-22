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
