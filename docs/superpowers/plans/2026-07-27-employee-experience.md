# Employee Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add permission-safe suggestions, popular questions, favorites, subscriptions, notifications, personal history, and a unified employee home.

**Architecture:** Store personal references rather than content copies, recompute ACL at read time, and invalidate caches by user permission version. Event-driven notification aggregation feeds independently loadable home widgets.

**Tech Stack:** Python 3.12, FastAPI, SQLite WAL, pytest, Vue 3, Pinia, Element Plus, TypeScript, Vitest

## Global Constraints

- Suggestions never reveal invisible resource titles or counts.
- Popular questions require 5 distinct users in 30 days and are partitioned by accessible KB/assistant scope.
- Sensitive/private questions never enter public aggregates.
- Favorites save IDs and version references, not permanent answer truth.
- Notification content is a safe summary; opening rechecks ACL.
- First release is in-app only; no DingTalk/email/SMS.
- Personal history deletion never removes enterprise audit.
- Home widget failure is isolated.
- Final-answer cache remains disabled.
- Use `superpowers:executing-plans`; no subagents unless requested.

---

## File Structure

- New backend: `rag/experience/{models,suggestions,popular,favorites,notifications,history,home,api}.py`.
- New tests: `tests/test_experience_*.py`.
- Modify: `rag/user_db.py`, `rag/api.py`, `rag/document_versions.py`, `rag/permissions.py`.
- New frontend: `frontend/src/api/experience.ts`, `frontend/src/views/HomeView.vue`, `frontend/src/components/home/*`, `frontend/src/stores/notifications.ts`, tests.
- Modify: `frontend/src/router/index.ts`, `frontend/src/views/MainLayout.vue`, chat/file components.

---

### Task 1: Experience Persistence and Permission Version

**Files:** Create `rag/experience/models.py`, `tests/test_experience_schema.py`; modify `rag/user_db.py`, `rag/permissions.py`.

**Interfaces:** CRUD for `favorite_items`, `subscriptions`, `notifications`, `user_activity`, `curated_questions`, `popular_question_snapshots`, `user_home_preferences`; `bump_permission_version(user_id)`.

- [ ] **Step 1: Write failing uniqueness/invalidation test**

```python
def test_duplicate_favorite_is_idempotent_and_permission_change_bumps_version(db, user):
    first = db.add_favorite(user.id, "document", "7")
    second = db.add_favorite(user.id, "document", "7")
    before = db.get_permission_version(user.id)
    db.revoke_document_view(user.id, 7)
    assert first == second
    assert db.get_permission_version(user.id) == before + 1
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_experience_schema.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add idempotent tables/methods and version bumps**

Use unique `(user_id, object_type, object_key)` for favorites/subscriptions and unique event keys for notifications/activity. All share/revoke/classification/disable mutations bump affected permission versions.

```sql
CREATE UNIQUE INDEX favorite_identity ON favorite_items(user_id, object_type, object_key);
UPDATE users SET permission_version=permission_version+1 WHERE id=?;
```

- [ ] **Step 4: Run schema/permission tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_experience_schema.py tests\test_permissions.py tests\test_user_db.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/experience/models.py rag/user_db.py rag/permissions.py tests/test_experience_schema.py
git commit -m "feat: add employee experience storage"
```

---

### Task 2: Suggestions and Popular Questions

**Files:** Create `rag/experience/suggestions.py`, `rag/experience/popular.py`, tests `tests/test_experience_suggestions.py`, `tests/test_popular_questions.py`.

**Interfaces:** `suggest(user, query, limit=10) -> list[Suggestion]`; `build_popular_snapshot(scope_type, scope_id, window_days=30, min_users=5)`.

- [ ] **Step 1: Write failing leakage/threshold tests**

```python
def test_suggestions_exclude_invisible_titles(suggestion_fixture):
    result = suggestion_fixture.suggest("薪资")
    assert "高管薪资.xlsx" not in [item.title for item in result]


def test_popular_question_requires_five_distinct_users(popular_fixture):
    popular_fixture.ask_same_question(users=["a", "b", "c", "d", "a"])
    assert popular_fixture.snapshot() == []


def test_popular_question_is_filtered_by_current_document_acl(popular_fixture):
    popular_fixture.seed_snapshot(scope_id="hr", resource_ids=[7], distinct_users=10)
    popular_fixture.revoke_document_view(user="alice", document_id=7)
    assert popular_fixture.list_for("alice") == []


def test_permission_version_change_cannot_reuse_popular_cache(popular_fixture):
    first = popular_fixture.list_for("alice")
    popular_fixture.revoke_document_view(user="alice", document_id=7)
    second = popular_fixture.list_for("alice")
    assert first.cache_key != second.cache_key
    assert all(7 not in item.supporting_resource_ids for item in second.items)
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_experience_suggestions.py tests\test_popular_questions.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement scoped merge and privacy filter**

Merge visible names, curated questions, personal history, scoped popular snapshots, and favorites in the specified priority. Every snapshot stores the current supporting resource IDs; response serialization intersects them with the requesting user's current visible resource set and suppresses a question when no visible current support remains. `visible_resource_fingerprint` is SHA-256 over sorted `(resource_type, resource_id, current_version_id, classification_revision)` tuples, so equal knowledge-library buckets cannot erase document-level ACL differences. Cache key is `(user_id, permission_version, visible_resource_fingerprint, query_prefix)`. Exclude restricted, private-marked, and sensitive-pattern questions before aggregation.

```python
cache_key = (
    f"suggest:{user['id']}:{user['permission_version']}:"
    f"{visible_resource_fingerprint}:{normalize_prefix(query)}"
)
```

- [ ] **Step 4: Run suggestion/popular tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_experience_suggestions.py tests\test_popular_questions.py tests\test_document_security_boundaries.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/experience/suggestions.py rag/experience/popular.py tests/test_experience_suggestions.py tests/test_popular_questions.py
git commit -m "feat: suggest accessible knowledge"
```

---

### Task 3: Favorites and Version Staleness

**Files:** Create `rag/experience/favorites.py`, `tests/test_favorites.py`; modify `rag/document_versions.py`.

**Interfaces:** `favorite_response(db, user, favorite) -> FavoriteView`; `mark_resource_update(document_id, old_version_id, new_version_id)`.

- [ ] **Step 1: Write failing revoked/stale test**

```python
def test_favorite_hides_old_excerpt_after_acl_revoke(favorite_fixture):
    favorite_fixture.favorite_answer(version_id=3, excerpt="薪资明细")
    favorite_fixture.revoke()
    view = favorite_fixture.get()
    assert view.status == "unavailable"
    assert view.excerpt is None
    assert view.download_url is None
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_favorites.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement reference-only serialization**

Document/citation/question/assistant favorites resolve current ACL. Answer favorites expose the saved message only while still authorized and return `new_version_available` when cited version differs from current.

```python
if not can_view_favorite_target(db, user, favorite):
    return FavoriteView(id=favorite["id"], status="unavailable")
```

- [ ] **Step 4: Run favorite/version tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_favorites.py tests\test_document_versions.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/experience/favorites.py rag/document_versions.py tests/test_favorites.py
git commit -m "feat: add permission-safe favorites"
```

---

### Task 4: Subscriptions and In-App Notifications

**Files:** Create `rag/experience/notifications.py`, `tests/test_notifications.py`; modify version/task services.

**Interfaces:** `publish_resource_event(event: ResourceEvent) -> None`; `deliver_pending_notifications(limit=100) -> DeliveryReport`.

- [ ] **Step 1: Write failing aggregation test**

```python
def test_three_version_events_become_one_notification(notification_fixture):
    for version in (2, 3, 4):
        notification_fixture.publish("document.updated", document_id=7, version_id=version)
    delivered = notification_fixture.deliver()
    assert delivered.created == 1
    assert notification_fixture.items()[0].event_count == 3


def test_delivery_drops_event_after_acl_revocation(notification_fixture):
    notification_fixture.subscribe(document_id=7)
    notification_fixture.publish("document.updated", document_id=7, version_id=2)
    notification_fixture.revoke_document_view(document_id=7)
    notification_fixture.deliver()
    assert notification_fixture.items() == []
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_notifications.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement outbox, aggregation window, and ACL-safe opening**

Version/task commits append identifier-only outbox events in their transaction: event type, resource type/ID, version/task ID, actor ID, and time; no title, excerpt, question, or generated summary is stored there. Delivery rechecks recipient ACL before creating a notification, drops unauthorized events, generates a safe summary only after authorization, then groups allowed events by recipient/resource/type/window. Opening performs a second ACL check as defense in depth.

```python
event_key = f"{recipient_id}:{resource_type}:{resource_id}:{event_type}:{window_start}"
db.insert_notification_once(event_key, safe_summary)
```

- [ ] **Step 4: Run notification/lifecycle tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_notifications.py tests\test_document_versions.py tests\test_knowledge_tasks.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/experience/notifications.py rag/document_versions.py rag/knowledge_tasks.py tests/test_notifications.py
git commit -m "feat: notify knowledge subscribers"
```

---

### Task 5: Personal History and Home Aggregator

**Files:** Create `rag/experience/history.py`, `rag/experience/home.py`, `tests/test_personal_history.py`, `tests/test_experience_home.py`.

**Interfaces:** `search_history(user_id, filters, page) -> Page[HistoryItem]`; `build_home(user, widget_keys) -> HomeResponse`.

- [ ] **Step 1: Write failing isolation test**

```python
def test_delete_personal_history_keeps_audit(history_fixture):
    activity_id = history_fixture.record_query("年假")
    audit_id = history_fixture.audit_id(activity_id)
    history_fixture.delete(activity_id)
    assert history_fixture.personal_items() == []
    assert history_fixture.audit_exists(audit_id)
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_personal_history.py tests\test_experience_home.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement user-only history and isolated widgets**

Each home widget runs independently and returns `{status, data, error_code}`. History stores resource references/safe titles only and resolves ACL for linked data on read.

```python
async def load_widget(loader):
    try:
        return {"status": "ok", "data": await loader(), "error_code": None}
    except Exception:
        return {"status": "error", "data": None, "error_code": "widget_unavailable"}
```

- [ ] **Step 4: Run history/home tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_personal_history.py tests\test_experience_home.py -v
```

Expected: PASS including one widget timeout.

- [ ] **Step 5: Commit**

```powershell
git add rag/experience/history.py rag/experience/home.py tests/test_personal_history.py tests/test_experience_home.py
git commit -m "feat: add personal history and home"
```

---

### Task 6: Experience API, Frontend, and Verification

**Files:** Create `rag/experience/api.py`, API tests, `frontend/src/api/experience.ts`, `frontend/src/views/HomeView.vue`, `frontend/src/stores/notifications.ts`, home components/tests; modify `rag/api.py`, router/layout.

**Interfaces:** Produces all `/experience`, `/favorites`, `/subscriptions`, `/notifications`, `/history`, and curated-question routes in the spec.

- [ ] **Step 1: Write failing API/UI tests**

```python
def test_home_partial_failure_is_200(isolated_api, user_headers, monkeypatch):
    monkeypatch.setattr("rag.experience.home.load_updates", lambda *_: (_ for _ in ()).throw(TimeoutError()))
    response = isolated_api.get("/experience/home", headers=user_headers)
    assert response.status_code == 200
    assert response.json()["updates"]["status"] == "error"
    assert response.json()["favorites"]["status"] == "ok"
```

```typescript
it('continues search when suggestions fail', async () => {
  mockSuggestionsFailure()
  const wrapper = mountHome()
  await wrapper.get('input').setValue('年假')
  await wrapper.get('form').trigger('submit')
  expect(mockRouterPush).toHaveBeenCalledWith(expect.objectContaining({ name: 'chat' }))
})
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_experience_api.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/views/HomeView.test.ts
Set-Location ..
```

Expected: missing route/component.

- [ ] **Step 3: Implement API and responsive home**

Use one experience router. Frontend suggestions debounce at 200ms; home widgets load concurrently; notification badge uses unread endpoint. No UI path for cross-user personal history.

```typescript
const loadHome = () => Promise.allSettled(widgetLoaders.map(loader => loader()))
const requestSuggestions = useDebounceFn((q: string) => experienceApi.suggest(q), 200)
```

- [ ] **Step 4: Run full verification**

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check rag/experience tests/test_experience_schema.py tests/test_experience_suggestions.py tests/test_popular_questions.py tests/test_favorites.py tests/test_notifications.py tests/test_personal_history.py tests/test_experience_home.py tests/test_experience_api.py
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
git diff --check
```

Expected: PASS.

- [ ] **Step 5: Document and commit**

```powershell
git add rag/experience rag/api.py tests frontend/src docs/development-log-security-boundary-hardening.md
git commit -m "docs: verify employee experience"
```
