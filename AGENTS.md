# Movary Backend AGENTS

This repository is the public backend for the future base edition.

## Scope

This AGENTS.md applies to everything under `movary-backend/`.

## Intent

- Keep the public backend independently runnable.
- Prefer simple base-edition behavior over carrying forward pro-only complexity.
- Introduce edition/capability contracts in the public repo first, then let the pro backend extend them.

## Rules

- The workspace-root `../AGENTS.md` defines the canonical `main` / `dev` branch and release policy.
- Do not add hard dependencies on `movary-backend-pro`.
- Keep base-edition flows simple and explicit: one Emby server, one MoviePilot server, one lightweight plan system.
- Prefer new lightweight APIs over exposing pro-era multi-step admin flows in the public repo.
- Keep database compatibility where practical; avoid destructive schema changes unless clearly required.
- If a change narrows or reassigns base/pro ownership, update the relevant orchestration docs in `../movary-orchestration/docs/`.
- Prefer capability-based checks over scattered edition-specific conditionals.
- Reuse existing schemas/services only when they still fit the base-edition model cleanly.
- Format touched backend files with the repo-local Python tooling before finishing.
- Backend behavior changes should add or update the most relevant automated tests unless the gap is explicitly unavoidable.

## Verification

Before claiming completion on backend changes:
- run targeted format and lint checks
- run the most relevant tests available
- if full test execution is blocked by local environment issues, say so explicitly

## Commit guidance

- Keep commits small and migration-oriented.
- Use concise scoped commit messages that match the implemented slice.
