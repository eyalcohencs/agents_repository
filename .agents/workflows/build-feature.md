---
description: End-to-end multi-agent software delivery pipeline from PRD to approved code.
---
Execute the following stages in order for the current feature in `docs/PRD.md`:

1. **Planning Stage (Sequential):**
   - Delegate to `product-manager` to generate `docs/SPEC.md` and `docs/TASKS.md`. Wait for completion.
   - Delegate to `architect` to generate `docs/ARCHITECTURE.md` from `docs/SPEC.md`. Wait for completion.
   - Pause and summarize the spec, architecture, and task list for human approval before coding.

2. **Build Stage (Parallel where independent):**
   - For independent tasks in `docs/TASKS.md` (e.g., separate API routes vs. UI components), spawn `fullstack-dev` subagents in parallel.
   - Wait for all build tasks to complete and pass build checks.

3. **Verification Stage (Sequential + Retry Loop):**
   - Delegate to `qa-engineer` to write and run tests against every `AC-*` in `docs/SPEC.md`.
   - If any test fails, invoke `debugger` to fix the root cause and re-run `qa-engineer` until all tests pass.

4. **Gatekeeping Stage (Parallel Audit):**
   - Run `code-reviewer` and `security-auditor` in parallel on the `git diff`.
   - If either reports `CHANGES_REQUESTED` or `SECURITY_BLOCK`, send the findings to `fullstack-dev` and re-verify.

5. **Release Docs Stage:**
   - Once approved, run `docs-writer` to update `README.md` and project docs, then mark tasks `[x]` in `docs/TASKS.md`.
