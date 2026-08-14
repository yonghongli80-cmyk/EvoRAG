# New GitHub Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the complete verified V3 codebase to the new private GitHub repository and make future company/home synchronization agent-managed.

**Architecture:** The repository keeps code, tests, migrations, build output, dependency locks, and configuration templates in Git while excluding credentials and runtime knowledge data. `origin/main` becomes the single code source of truth; agents pull before coding and test, commit, rebase, push, and verify after completing a code task.

**Tech Stack:** Git, GitHub HTTPS, Git Credential Manager, Markdown, Python 3.12, pytest, Node.js/npm, Vitest, Vite.

## Global Constraints

- New remote URL is exactly `https://github.com/hhg00uaecv/rag.git`.
- Remove the blocked-account remote completely; do not preserve a legacy remote.
- Never commit `.env`, API keys, SQLite databases, vector indexes, uploaded documents, or document-version runtime files.
- Publish stable `main`; do not push temporary feature branches or local stash entries.
- Never force-push `main`.
- Agents own normal pull, test, commit, push, and remote-hash verification; the user only confirms local browser authentication when required.
- A failed test, unresolved conflict, unknown remote commit, or missing authentication stops publication without overwriting remote state.

---

### Task 1: Repository Usage and Agent Workflow Documentation

**Files:**
- Create: `AGENTS.md`
- Modify: `README.md`
- Modify: `docs/deployment-guide.md`
- Modify: `docs/deployment-step-by-step.md`

**Interfaces:**
- Consumes: synchronization rules from `docs/superpowers/specs/2026-08-14-dual-device-github-workflow-design.md`.
- Produces: a root-level agent contract and human-readable setup instructions for both computers.

- [ ] **Step 1: Write the root agent contract**

Create `AGENTS.md` with these enforceable rules:

```markdown
# Repository Agent Workflow

For any task that changes tracked project files:

1. Before editing, ensure the worktree is clean and synchronize `main` with `git pull --rebase origin main` when `origin` is configured.
2. Preserve unknown local changes and stop instead of resetting or overwriting them.
3. Never add `.env`, credentials, databases, vector stores, uploaded documents, or runtime document versions.
4. Before completion, run the relevant focused tests, the full backend suite, the full frontend suite, and the production frontend build.
5. Commit a coherent completed task with a descriptive message.
6. Rebase on the latest `origin/main`, then push `main` without force.
7. Verify the remote `main` hash matches the local commit and report both hashes.
8. If tests, authentication, rebase, or push fails, stop and report the exact blocker.

Read-only analysis and tasks with no file changes do not create empty commits or pushes.
```

- [ ] **Step 2: Rewrite the project README as a reproducible entry point**

Document all of the following in `README.md`:

- V3 capabilities: hybrid RAG retrieval, material lifecycle, versioned spreadsheet cell/structure editing, rollback, and audit;
- repository layout for `rag/`, `frontend/`, `tests/`, `docs/`, `static/`, and `data/`;
- prerequisites: Python 3.12, Node.js/npm, and optional Docker Compose;
- first setup on Windows: clone, create `.venv`, install `requirements.lock`, copy `.env.example` to `.env`, install frontend dependencies, run tests, build, and start;
- Docker Compose setup and shutdown;
- agent-managed company/home workflow and the requirement that only pushed commits synchronize;
- explicit statement that `.env` and runtime knowledge data are copied or backed up separately;
- backend, frontend, and build verification commands;
- troubleshooting for authentication, rejected pushes, and local uncommitted changes.

- [ ] **Step 3: Replace the blocked GitHub URL in deployment documentation**

Run:

```powershell
rg -n "sighistp/RAG|github.com/sighistp" README.md docs deploy.sh render.yaml
```

Replace every repository clone URL with:

```text
https://github.com/hhg00uaecv/rag.git
```

- [ ] **Step 4: Validate documentation and exclusions**

Run:

```powershell
rg -n "sighistp/RAG|github.com/sighistp" README.md docs deploy.sh render.yaml
git check-ignore -v .env data/document_versions/example.xlsx .sdd-test-data/example.db
git diff --check
```

Expected: no blocked-account URL, all three private/runtime paths are ignored, and no new whitespace errors in modified documentation.

- [ ] **Step 5: Commit documentation**

```powershell
git add AGENTS.md README.md docs/deployment-guide.md docs/deployment-step-by-step.md
git commit -m "docs: add reproducible dual-device workflow"
```

### Task 2: Release Safety and Reproducibility Verification

**Files:**
- Verify only; no expected file modifications.

**Interfaces:**
- Consumes: the committed repository tree from Task 1.
- Produces: fresh evidence that the exact tree to publish is secret-free, tested, and buildable.

- [ ] **Step 1: Confirm a clean worktree and excluded private data**

```powershell
git status --short
git ls-files .env data/document_versions .sdd-test-data qdrant_data
```

Expected: clean status and no tracked private/runtime path.

- [ ] **Step 2: Scan tracked text for credential signatures without printing values**

```powershell
git grep -Il -E "sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" -- . ":(exclude).env.example"
```

Expected: no matching tracked file.

- [ ] **Step 3: Run the full backend suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 4: Run the full frontend suite**

```powershell
Set-Location frontend
npm.cmd test -- --maxWorkers=1
```

Expected: all Vitest files and tests pass.

- [ ] **Step 5: Build the production frontend**

```powershell
npm.cmd run build
Set-Location ..
git status --short
```

Expected: Vite exits successfully. If deterministic build assets change, commit only the current `static/index.html` and current `static/assets/` output after removing stale hashes, then repeat Tasks 2.1 through 2.5.

### Task 3: Promote Stable Main and Publish the New Remote

**Files:**
- Git references and remote configuration only.

**Interfaces:**
- Consumes: clean, fully verified commits from Tasks 1 and 2.
- Produces: new `origin/main` whose hash exactly matches local `main`.

- [ ] **Step 1: Record the stable source branch and confirm fast-forward safety**

```powershell
git status --short
git rev-list --left-right --count main...fix/review-2026-08-07
git merge-base main fix/review-2026-08-07
```

Expected: the left count is `0`, proving `main` can fast-forward without discarding commits.

- [ ] **Step 2: Fast-forward local main**

```powershell
git switch main
git merge --ff-only fix/review-2026-08-07
```

Expected: `main` advances to the verified publication commit with no merge conflict.

- [ ] **Step 3: Remove the blocked remote and configure the new private repository**

```powershell
git remote remove origin
git remote add origin https://github.com/hhg00uaecv/rag.git
git remote -v
```

Expected: both fetch and push URLs reference only `github.com/hhg00uaecv/rag.git`.

- [ ] **Step 4: Confirm the remote is empty before publishing**

```powershell
git ls-remote --heads origin
```

Expected: no branch is returned. If a branch exists, stop and inspect it instead of force-pushing.

- [ ] **Step 5: Push stable main through HTTPS authentication**

```powershell
git push -u origin main
```

Expected: Git Credential Manager requests browser authorization when necessary and Git reports a new `main` branch.

- [ ] **Step 6: Verify local and remote hashes**

```powershell
$localHash = git rev-parse main
$remoteHash = (git ls-remote origin refs/heads/main).Split()[0]
"LOCAL=$localHash"
"REMOTE=$remoteHash"
if ($localHash -ne $remoteHash) { throw "Remote main does not match local main" }
```

Expected: `LOCAL` and `REMOTE` are identical.

- [ ] **Step 7: Remove the merged temporary branch and retain recovery backups locally**

```powershell
git branch -d fix/review-2026-08-07
git status --short
git branch -vv
```

Expected: local `main` tracks `origin/main`, the worktree is clean, and the pre-cleanup stash plus external runtime ZIP remain local and are not published.
