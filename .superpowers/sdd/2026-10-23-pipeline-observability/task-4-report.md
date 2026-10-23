# Task 4 report — cancellation partial traces and terminal ownership

## Result

Implemented request-local terminal ownership and cancellation snapshots in `rag/pipeline_query.py`; added `tests/test_pipeline_cancellation_observability.py`. Public pipeline signatures and SSE events remain unchanged. Cancellation and explicit `aclose()` continue to propagate after closing the provider iterator. If no terminal trace owns the request, the pipeline seals current timings and saves one `route="error"`, empty-answer trace with `execution_outcome="cancelled"`. This path does not submit a FailureObserver observation or write answer memory.

A final success or handled degradation claims ownership before scheduling its SQLite save in `asyncio.to_thread`. A later cancellation cannot append a second trace. A terminal save error still propagates and no second error trace is attempted. Agent subpath calls to the existing failure recorder remain outside this final-trace ownership state.

For cancelled preparation in `to_thread`, sealing occurs at the cancellation boundary. The test confirms the request returns while the worker remains blocked, records the active 250 ms span, then releases the worker before `asyncio.run` joins it; the worker's late `finish()` does not alter the saved trace. A subsequent request on the same pipeline gets an independent timing collector. Cached and handled-degraded completion retain their prior no-extra-trace behavior.

`pipeline_diagnostics.py` and `stage_timing.py` needed no source changes. `pipeline_query.py` is 398 lines, within its 650-line cap.

## Test-first evidence

Before production edits, the three cancellation regressions failed for the intended reasons: no trace after public `aclose`, no trace on provider-await cancellation, and no trace when cancellation interrupted blocked preparation. The terminal-save error regression also failed because an `OSError` during success-trace persistence caused a second error-trace save. Exact commands, assertion outcomes, and exit codes are preserved in `artifacts/pipeline-observability-2026-10-23/task-4-red.log`.

Two baseline behaviors already met the contract: cancellation after terminal-save start and closing after `done` each passed without an extra trace. They remain regression tests and are recorded as existing behavior, not preimplementation RED.

## Verification

- Cancellation tests: **8 passed**, 1 existing Starlette `BlockingPortal` deprecation warning; exit 0. Log: `artifacts/pipeline-observability-2026-10-23/task-4-cancellation-final.log`.
- Cancellation, Task 3 timing, stream, arena, breaker, event-loop, structured outcome, and architecture budget groups: **62 passed**, the same existing warning; exit 0. Log: `artifacts/pipeline-observability-2026-10-23/task-4-regressions.log`.
- Ruff on changed Python files: **All checks passed**; exit 0. `git diff --check`: exit 0. Evidence: `artifacts/pipeline-observability-2026-10-23/task-4-quality.log`.
- No full suite or Docker runtime check was run; those remain under the root task's final validation.

PowerShell Transcript did not capture native pytest stdout. The evidence logs were rewritten from the direct tool outputs and explicitly identify that provenance.
