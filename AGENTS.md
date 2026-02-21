# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
```

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

## Releasing (CalVer)

This project uses **CalVer** (`YYYY.MM.DD` or `YYYY.MM.DD.N` for multiple releases per day).

Pushing a tag triggers GitHub Actions to build cross-platform binaries and create a GitHub Release automatically.

```bash
# First release of the day
git tag 2026.02.22
git push origin 2026.02.22

# Second release same day
git tag 2026.02.22.1
git push origin 2026.02.22.1
```

Pre-release tags (contain `dev`, `rc`, `alpha`, `beta`) are marked as pre-release on GitHub:
```bash
git tag 2026.02.22-beta
git push origin 2026.02.22-beta
```

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds

