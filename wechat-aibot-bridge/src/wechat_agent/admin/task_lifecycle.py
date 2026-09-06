"""Monotonic task projections; raw events remain an immutable audit trail."""

TERMINAL_STATES = frozenset({
    "SUCCEEDED", "PARTIAL_SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED",
})
EVENT_STATES = {
    "task.received": "RECEIVED", "task.queued": "QUEUED", "task.started": "RUNNING",
    "task.progress": "RUNNING", "task.completed": "SUCCEEDED", "task.failed": "FAILED",
    "task.cancelled": "CANCELLED", "task.timeout": "TIMED_OUT",
}


def projected_state(current: str | None, event: str, payload: dict) -> str | None:
    """None means audit-only: do not overwrite the current task projection."""
    candidate = EVENT_STATES.get(event)
    if candidate is None or current in TERMINAL_STATES:
        return None
    if event == "task.completed" and str(payload.get("state") or payload.get("status") or "").upper() in {
        "PARTIAL_SUCCEEDED", "PARTIALLY_SUCCEEDED", "PARTIAL_SUCCESS",
    }:
        candidate = "PARTIAL_SUCCEEDED"
    if current == "CANCEL_REQUESTED" and candidate not in TERMINAL_STATES:
        return None  # Cancellation requested is not cancellation confirmed.
    order = {"RECEIVED": 0, "QUEUED": 1, "RUNNING": 2}
    if current in order and candidate in order and order[candidate] < order[current]:
        return None
    return candidate


def task_evidence(events: list[dict], task_id: str) -> dict:
    """Separate model execution from response acknowledgement; never infer success."""
    result = {"execution_state": "unknown", "response_status": "unknown", "failure_stage": None}
    for event in events:
        payload = event.get("payload", {})
        if payload.get("task_id") != task_id:
            continue
        kind = event.get("event_type")
        if kind == "agent.execution.completed":
            result["execution_state"] = payload.get("execution_state", "unknown")
            # No final acknowledgement yet; a crash could lose the next event.
            result["response_status"] = "unknown"
        if kind in {"task.completed", "task.failed", "task.cancelled", "task.timeout"}:
            for key in result:
                if key in payload:
                    result[key] = payload[key]
            break  # The same terminal boundary as the persisted projection.
    return result
