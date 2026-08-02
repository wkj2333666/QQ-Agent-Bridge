# Repository Privacy Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist privacy-safe contribution rules for coding agents, contributors, and release owners.

**Architecture:** A root `AGENTS.md` is the authoritative repository-wide instruction source. `CONTRIBUTING.md` summarizes the same requirements for normal changes, while `docs/PUBLISHING.md` adds history and release-specific checks.

**Tech Stack:** Markdown, Git

## Global Constraints

- This is documentation-only; do not add a scanner, CI job, pre-commit hook, dependency, or runtime behavior.
- Never use real QQ identities, conversations, personal paths, credentials, or private configuration as examples or fixtures.
- Use synthetic identities and privacy-safe GitHub noreply commit addresses.

---

### Task 1: Persist Repository Privacy Rules

**Files:**
- Create: `AGENTS.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/PUBLISHING.md`

**Interfaces:**
- Consumes: `docs/superpowers/specs/2026-08-02-repository-privacy-rules-design.md`
- Produces: repository-wide instructions and matching contributor/release guidance

- [x] **Step 1: Create the authoritative agent rules**

Add a `Privacy And Test Data` section to root `AGENTS.md` that prohibits real
QQ identities, chat content, credentials, personal email addresses, private
configuration, runtime state, and machine-specific paths in every tracked
artifact and commit message. Require synthetic examples such as
`1000000001`, `2000000001`, `示例用户`, and `/home/example`.

- [x] **Step 2: Align contributor guidance**

Extend `CONTRIBUTING.md` so its pull-request checklist requires synthetic chat
content, privacy-safe attachment/profile examples, and a GitHub noreply commit
email.

- [x] **Step 3: Align publishing guidance**

Extend `docs/PUBLISHING.md` to review commit messages and author/committer
emails, and state explicitly that deleting private data in a later commit does
not remove it from Git history.

- [x] **Step 4: Verify the documentation**

Run:

```bash
rg -n "real QQ|synthetic|noreply|commit message|history" AGENTS.md CONTRIBUTING.md docs/PUBLISHING.md
git diff --check
```

Expected: all three documents contain their scoped privacy guidance and
`git diff --check` exits successfully.

- [x] **Step 5: Commit**

```bash
git add AGENTS.md CONTRIBUTING.md docs/PUBLISHING.md docs/superpowers/plans/2026-08-02-repository-privacy-rules.md
git commit -m "docs: persist repository privacy rules"
```
