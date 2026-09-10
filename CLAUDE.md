## Language

Respond in French. Keep English for software-engineering and agent vocabulary (spec, ticket, unit of work, blocking edge, context, tracer bullet, commit, pull request, etc.) rather than translating it. Reason internally in English; only the final answer goes out in French.

## Agent skills

### Issue tracker

Issues live as GitHub Issues in `Jiro-science/MATHutrice` (uses the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Uses the default five canonical triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Working directory

Always confirm the working directory is `MATHutrice-fork`, never the original `MATHutrice` repo, before making any change.

- If a file path, terminal prompt, or `pwd` output shows `MATHutrice` without `-fork`, stop and flag it before continuing — do not assume it's fine.
- The original `MATHutrice` folder exists locally for reference only. Never write, commit, or push to it.

**Done means:** every file edit, commit, and push in this session happened inside a path containing `MATHutrice-fork`. If you're unsure which folder you're in, run `pwd` (or equivalent) before acting.