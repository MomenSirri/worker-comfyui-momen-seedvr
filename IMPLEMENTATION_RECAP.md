# Implementation Recap: Docker + Handler

This document is a consolidated, detailed recap of the implementation work completed so far on:

- `Dockerfile`
- `handler.py`

It focuses on architecture decisions, behavior changes, environment variables, output contract compatibility, and operational implications for RunPod + Gradio integration.

---

## 1) Scope and Goals

The combined implementation effort addressed the following practical goals:

1. Make ComfyUI startup/reachability checks more reliable in RunPod.
2. Surface meaningful live progress to clients (Gradio) while jobs are running.
3. Parse useful workflow runtime signals from ComfyUI logs (especially SeedVR).
4. Improve enhancement-phase counting so progress reflects actual per-item progress.
5. Keep final output payload shape stable for existing clients.
6. Prevent false/stuck `IN_PROGRESS` states caused by late progress events.
7. Improve resilience (WebSocket timeouts/reconnect).
8. Reduce Docker rebuild time after handler-only edits.
9. Add failure-only worker refresh behavior for cleaner recovery after failed jobs.

---

## 2) Dockerfile Changes

### 2.1 Runtime file layering for faster rebuilds

Main change: runtime files (`start.sh`, `network_volume.py`, `handler.py`, `test_input.json`) were moved into a dedicated late stage:

- New stage: `FROM base AS runtime-files`
- Runtime files are copied at the end of each final target stage:
  - `final-flux2-klein`
  - `final-seedvr`
  - `final-enhance`

Why:

- Previously, handler edits invalidated earlier layers more often.
- With late `COPY --from=runtime-files ...`, model/node layers stay cached.
- Rebuilding after only handler tweaks becomes dramatically faster.

### 2.2 Startup command consistency

Each final stage explicitly ends with:

```dockerfile
CMD ["/start.sh"]
```

This keeps runtime behavior stable while still benefiting from improved layer caching.

### 2.3 Net effect

- Faster iteration during handler/debug cycles.
- Lower risk of unnecessary full rebuilds of massive model layers.
- Better developer productivity for frequent logic updates.

---

## 3) Handler Core Architecture Changes

## 3.1 Configurable ComfyUI readiness and health checks

Hardcoded readiness behavior was replaced with environment-driven controls:

- `COMFY_API_AVAILABLE_INTERVAL_MS` (default: `250`)
- `COMFY_API_AVAILABLE_MAX_RETRIES` (default: `600`)
- `COMFY_API_HEALTH_PATH` (default: `/object_info`)

Behavioral improvement:

- Reachability now treats any HTTP status `< 500` as reachable.
- This avoids false negatives where `/` returns 404 but ComfyUI is alive.

Operational outcome:

- Fewer startup false-failures.
- Better control over wait window vs responsiveness.

## 3.2 WebSocket stability and reconnect logic

Added/reinforced:

- `WEBSOCKET_RECONNECT_ATTEMPTS` (default: `5`)
- `WEBSOCKET_RECONNECT_DELAY_S` (default: `3`)
- Socket receive timeout handling with keep-alive style progress messages.

Behavior:

- On temporary disconnect, handler emits status and attempts reconnect.
- On timeout, it emits “still running” style updates rather than appearing frozen.
- If ComfyUI HTTP itself is unreachable, reconnect aborts early with a clearer failure path.

Operational outcome:

- Better survival through transient WS issues.
- Better observability during long-running jobs.

## 3.3 Safe progress emission bridge

Added `_safe_progress_update(...)`:

- Deduplicates repeated messages.
- Throttles non-forced updates using `PROGRESS_UPDATE_MIN_INTERVAL_S` (default `0.75`).
- Prevents progress reporting exceptions from breaking the job.

Added `_emit_live_log(...)`:

- Emits normalized progress lines as:
  - `[comfy-log][<phase>] <message>`

Reason:

- Consistent machine-parseable logs for Gradio polling/stream display.

---

## 4) Runtime Log Bridge (Comfy log parsing)

## 4.1 Added log tailing + pattern parsing

New env controls:

- `ENABLE_COMFY_RUNTIME_LOG_BRIDGE` (default `true`)
- `COMFY_RUNTIME_LOG_PATH` (default `/comfyui/user/comfyui.log`)

SeedVR regex extraction includes:

- Total input frames:
  - `Input: X frames`
- Batch counters:
  - `Encoding batch i/n`
  - `Upscaling batch i/n`
  - `Decoding batch i/n`

Emitted structured live phases:

- `seedvr-frames`
- `seedvr-encode`
- `seedvr-upscale`
- `seedvr-decode`

## 4.2 Why this was needed

Comfy native `progress` often reports sampler step progress (`x/100`) that does not map directly to “items in batch completed.”  
Runtime log bridge allows exposing item/frame-aware counters for SeedVR phases.

---

## 5) Enhancement Tracking System (Generalized)

## 5.1 Dynamic node detection

Enhancement tracking no longer depends on a fixed node id only.

Mechanism:

- Detects sampler nodes based on class/title hints (`ksampler`, `sampler`, `euler`, etc.).
- Excludes non-enhancement node types (e.g., save/vae helper nodes).
- Optional override with:
  - `ENHANCEMENT_TRACK_NODE_ID` (pin if needed).

Goal:

- Stay robust across different workflows with different node ids.

## 5.2 Per-step and per-item state model

Key emitted phases:

- `enhance-step`
- `enhance-item`
- `enhance-sample`
- `enhance-state`

State concepts:

- `enhance_samples_done`
- current `step`/`max` tracking per sampler cycle
- active enhancement node selection
- total hint derived from SeedVR frame count where available

## 5.3 Completion detection logic (multi-signal)

To reduce undercount/missed-last-item problems, counting uses multiple signals:

1. Progress reaches maximum step (`value >= max`).
2. Step reset is observed (e.g., next item starts and step goes backward).
3. Node transition away from enhancement sampler.
4. Execution finished reconciliation.
5. Post-loop reconciliation.

Extra safeguard:

- `ENHANCE_CYCLE_COMPLETE_RATIO` (default `0.85`) allows a near-complete cycle to count as done when final exact step signal is missed.

Result:

- More reliable item counting even with imperfect/irregular progress event ordering.

---

## 6) Output Contract and RunPod Status Behavior

## 6.1 Final output format alignment

Handler success returns:

```json
{
  "status": "success",
  "message": ["s3-url/base64", "..."]
}
```

This preserves compatibility with the RunPod status envelope expected by Gradio:

```json
{
  "delayTime": ...,
  "executionTime": ...,
  "id": "...",
  "output": {
    "message": [...],
    "status": "success"
  },
  "status": "COMPLETED",
  "workerId": "..."
}
```

## 6.2 Stuck `IN_PROGRESS` mitigation

A critical guard was added conceptually and in flow:

- Avoid sending progress updates after output is finalized.

Reason:

- Late progress events can arrive out-of-order and keep request state appearing `IN_PROGRESS` even after work completed.

Result:

- Better chance of request status transitioning cleanly to `COMPLETED`.

---

## 7) Failure Handling and Worker Refresh

## 7.1 Unified failure response helper

Added `_failure_result(error_message, details=None)`:

- Standardizes error payload.
- Supports optional details list.
- Optionally requests worker refresh on failure.

## 7.2 Failure-only worker refresh control

New env flag:

- `REFRESH_WORKER_ON_FAILURE` (default `true`)

Behavior:

- On failed paths, response includes `refresh_worker: true` when enabled.
- Intended to recycle possibly bad worker state after failures only.
- Success paths do not force refresh via this mechanism.

## 7.3 Where failure helper is used

Applied across major failure branches:

- Input validation failures
- Comfy readiness failures
- Image upload failures
- Missing prompt/history error branches
- WS/HTTP/value/unexpected exception branches
- No-output-with-errors terminal branch

Operational effect:

- Consistent error payloads + cleaner self-healing behavior in failure scenarios.

---

## 8) Detailed Live Event Types Emitted

The handler now emits a richer set of structured events that clients can parse:

- `ws`
- `queue`
- `status`
- `node`
- `progress`
- `execution`
- `executed`
- `cache`
- `error`
- `seedvr-frames`
- `seedvr-encode`
- `seedvr-upscale`
- `seedvr-decode`
- `enhance-step`
- `enhance-item`
- `enhance-sample`
- `enhance-state`

This enables building a cleaner “client-facing” progress UI while still retaining deep diagnostics when needed.

---

## 9) Environment Variables Summary (Current Behavior)

### Readiness / API

- `COMFY_API_AVAILABLE_INTERVAL_MS`
- `COMFY_API_AVAILABLE_MAX_RETRIES`
- `COMFY_API_HEALTH_PATH`

### WebSocket

- `WEBSOCKET_RECONNECT_ATTEMPTS`
- `WEBSOCKET_RECONNECT_DELAY_S`
- `WEBSOCKET_TRACE` (verbose protocol logging)

### Progress emission

- `PROGRESS_UPDATE_MIN_INTERVAL_S`

### Runtime log bridge

- `ENABLE_COMFY_RUNTIME_LOG_BRIDGE`
- `COMFY_RUNTIME_LOG_PATH`

### Enhancement tracking

- `ENHANCEMENT_TRACK_NODE_ID`
- `ENHANCE_CYCLE_COMPLETE_RATIO`

### Worker refresh behavior

- `REFRESH_WORKER`
- `REFRESH_WORKER_ON_FAILURE`

---

## 10) Practical Operational Notes

1. Faster progress frequency is controlled by both sides:
   - Handler emit interval (`PROGRESS_UPDATE_MIN_INTERVAL_S`)
   - Gradio polling/stream cadence
2. Poll-only clients can still miss intermediate events by definition.
3. Stream responses improve real-time fidelity (while final output schema can remain unchanged).
4. Runtime parsing is best-effort and depends on model/workflow logging style.
5. Generalized enhancement logic is workflow-agnostic, but optional pinning exists for hard guarantees.

---

## 11) Validation Status

Current `handler.py` compiles successfully:

```bash
python -m py_compile handler.py
```

---

## 12) Bottom-Line Outcome

The worker has moved from a “single final result, low visibility” implementation to a robust execution monitor that:

- Tracks ComfyUI lifecycle more reliably,
- Emits structured in-flight progress,
- Preserves your required final output contract,
- Handles failures consistently,
- Supports generalized workflows better,
- And rebuilds much faster for handler-only iterations due to Docker layer restructuring.

