"""FastAPI administration control plane."""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Annotated, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import AdminSettings
from .connection_probe import (
    ConnectionProbe,
    ConnectionProbeResult,
    WeComCredentialProbe,
)
from .schemas import (
    AdminUserCreate,
    AlertAction,
    ConfigProfileCreate,
    ConfigProfileUpdate,
    ConfigRevisionCreate,
    ConnectionCreate,
    ConnectionUpdate,
    ControlRequest,
    LoginRequest,
    RetentionRequest,
    RoleAssignment,
    SettingsUpdate,
    SetupRequest,
    UserUpdate,
)
from .security import SecretBox, hash_password, new_token, token_hash
from .store import AdminStore, AuthenticationLockedError, ConflictError, NotFoundError, StoreError
from .runtime_worker import (
    SUPPORTED_SERVICES,
    SupervisorFileClient,
    SupervisorRuntimeCommandWorker,
)


API_PREFIX = "/api/admin/v1"


def create_app(
    settings: AdminSettings | None = None,
    store: AdminStore | None = None,
    runtime_worker: SupervisorRuntimeCommandWorker | None = None,
    connection_probe: ConnectionProbe | None = None,
) -> FastAPI:
    settings = settings or AdminSettings.from_environment()
    store = store or AdminStore(settings.database_path, SecretBox.load(settings.master_key_path))
    connection_probe = connection_probe or WeComCredentialProbe()
    if runtime_worker is None and settings.runtime_control_enabled:
        runtime_dir = (
            settings.supervisor_runtime_dir
            or settings.database_path.parent / "supervisor"
        )
        runtime_worker = SupervisorRuntimeCommandWorker(
            store,
            SupervisorFileClient(
                runtime_dir,
                command_timeout_seconds=settings.supervisor_command_timeout_seconds,
                status_stale_seconds=settings.supervisor_status_stale_seconds,
            ),
            poll_seconds=settings.supervisor_poll_seconds,
            lease_seconds=settings.supervisor_command_lease_seconds,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if runtime_worker is not None:
            await runtime_worker.start()
        try:
            yield
        finally:
            if runtime_worker is not None:
                await runtime_worker.close()

    app = FastAPI(
        title="WeCom Computer Agent Admin API",
        version="1.0.0",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.admin_settings = settings
    app.state.admin_store = store
    app.state.supervisor_runtime_worker = runtime_worker
    app.state.connection_probe = connection_probe

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id[:128]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
        return response

    @app.exception_handler(StoreError)
    async def store_error(request: Request, exc: StoreError) -> JSONResponse:
        code = status.HTTP_404_NOT_FOUND if isinstance(exc, NotFoundError) else status.HTTP_409_CONFLICT
        if isinstance(exc, AuthenticationLockedError):
            code = status.HTTP_429_TOO_MANY_REQUESTS
        return _error(request, code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        safe_errors = [
            {key: value for key, value in error.items() if key != "input"}
            for error in exc.errors()
        ]
        return _error(request, 422, "VALIDATION_ERROR", "Request validation failed", safe_errors)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, _exc: sqlite3.Error) -> JSONResponse:
        return _error(request, 503, "DATABASE_UNAVAILABLE", "The administration database is temporarily unavailable")

    # Public bootstrap and liveness ------------------------------------------
    @app.get(f"{API_PREFIX}/health", tags=["system"])
    def health() -> Response:
        try:
            with store.database.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return JSONResponse({"status": "ok", "database": "ok", "setup_required": store.setup_required(), "time": _now()})
        except sqlite3.Error:
            return JSONResponse(
                {"status": "unavailable", "database": "error", "setup_required": True, "time": _now()},
                status_code=503,
            )

    @app.get(f"{API_PREFIX}/health/live", tags=["system"])
    def live() -> dict[str, Any]:
        return {"status": "alive", "time": _now()}

    @app.get(f"{API_PREFIX}/setup/status", tags=["auth"])
    def setup_status() -> dict[str, Any]:
        return {"setup_required": store.setup_required()}

    @app.post(f"{API_PREFIX}/setup", status_code=201, tags=["auth"])
    def setup(body: SetupRequest, request: Request) -> dict[str, Any]:
        try:
            password = hash_password(body.password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "WEAK_PASSWORD", "message": str(exc)}) from exc
        user = store.bootstrap_admin(body.username, body.display_name, password)
        return {"user": user, "setup_required": False}

    @app.post(f"{API_PREFIX}/auth/login", tags=["auth"])
    def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        user = store.authenticate(body.username, body.password)
        if user is None:
            raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "Invalid username or password"})
        token, csrf = new_token(), new_token() if body.mode == "cookie" else None
        expires = datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)
        store.create_session(user["id"], token, csrf, body.mode, expires.isoformat(), _client_ip(request), request.headers.get("user-agent"))
        if body.mode == "cookie":
            response.set_cookie(settings.cookie_name, token, max_age=settings.session_hours * 3600, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
            return {"user": user, "csrf_token": csrf, "expires_at": expires.isoformat(), "token_type": "cookie"}
        return {"user": user, "access_token": token, "token_type": "bearer", "expires_at": expires.isoformat()}

    # Authenticated routes ----------------------------------------------------
    @app.get(f"{API_PREFIX}/auth/me", tags=["auth"])
    def me(principal: Annotated[dict[str, Any], Depends(_principal)]) -> dict[str, Any]:
        return {"user": principal["user"]}

    @app.post(f"{API_PREFIX}/auth/logout", tags=["auth"])
    def logout(response: Response, principal: Annotated[dict[str, Any], Depends(_principal)]) -> dict[str, bool]:
        store.revoke_session(principal["id"], principal["user"]["id"])
        response.delete_cookie(settings.cookie_name, path="/")
        return {"ok": True}

    @app.get(f"{API_PREFIX}/dashboard/summary", tags=["dashboard"])
    def dashboard(_p: Annotated[dict[str, Any], Depends(_permission("dashboard.read"))]) -> dict[str, Any]:
        return store.dashboard()

    @app.get(f"{API_PREFIX}/settings", tags=["settings"])
    def read_settings(_p: Annotated[dict[str, Any], Depends(_permission("settings.read"))]) -> dict[str, Any]:
        return {"host": settings.host, "port": settings.port, "cookie_secure": settings.cookie_secure, "session_hours": settings.session_hours, "database": {"engine": "sqlite", "wal": True}, "capabilities": {"settings_write": False, "control_command_queue": True, "sse": True, "online_backup": True, "config_revisions": True}}

    @app.patch(f"{API_PREFIX}/settings", tags=["settings"])
    def update_settings(_body: SettingsUpdate, _p: Annotated[dict[str, Any], Depends(_permission("settings.write"))]) -> dict[str, Any]:
        raise HTTPException(status_code=501, detail={"code": "SETTINGS_PUBLISH_NOT_IMPLEMENTED", "message": "Runtime settings are immutable in this release; edit deployment configuration and restart."})

    _register_list_routes(app, store)

    @app.get(f"{API_PREFIX}/connections/{{connection_id}}", tags=["connections"])
    def connection_detail(connection_id: str, _p: Annotated[dict[str, Any], Depends(_permission("connections.read"))]) -> dict[str, Any]:
        return store.get_record("connections", connection_id)

    @app.post(f"{API_PREFIX}/connections", status_code=201, tags=["connections"])
    def create_connection(body: ConnectionCreate, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("connections.write"))]) -> dict[str, Any]:
        return store.create_connection(body.model_dump(), principal["user"]["id"], _client_ip(request))

    @app.patch(f"{API_PREFIX}/connections/{{connection_id}}", tags=["connections"])
    def update_connection(connection_id: str, body: ConnectionUpdate, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("connections.write"))], if_match: str | None = Header(default=None, alias="If-Match")) -> dict[str, Any]:
        data = body.model_dump(exclude_unset=True)
        version = data.pop("version")
        if if_match:
            try:
                header_version = int(if_match.strip().strip('W/').strip('"'))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"code": "INVALID_ETAG", "message": "If-Match must contain a numeric version"}) from exc
            if header_version != version:
                raise HTTPException(status_code=400, detail={"code": "VERSION_MISMATCH", "message": "Body version and If-Match do not match"})
        return store.update_connection(connection_id, data, version, principal["user"]["id"], _client_ip(request))

    @app.delete(f"{API_PREFIX}/connections/{{connection_id}}", status_code=204, tags=["connections"])
    def delete_connection(connection_id: str, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("connections.write"))]) -> Response:
        store.delete_connection(connection_id, principal["user"]["id"], _client_ip(request))
        return Response(status_code=204)

    @app.post(f"{API_PREFIX}/connections/{{connection_id}}/test", tags=["connections"])
    async def test_connection(connection_id: str, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("connections.write"))]) -> dict[str, Any]:
        target = store.get_connection_probe_target(connection_id)
        if target["is_active"]:
            result = _active_connection_observation(target)
        else:
            result = await _run_connection_probe(
                connection_probe,
                target["bot_id"],
                target["secret"],
                settings.connection_probe_timeout_seconds,
            )
        public = result.public()
        public["mode"] = (
            "LIVE_RUNTIME_OBSERVATION" if target["is_active"] else "ISOLATED_SDK_PROBE"
        )
        public["connection_id"] = connection_id
        store.record_connection_test(
            connection_id, public, principal["user"]["id"], _client_ip(request)
        )
        return public

    @app.post(f"{API_PREFIX}/connections/{{connection_id}}/activate", status_code=202, tags=["connections"])
    async def activate_connection(connection_id: str, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("connections.write"))], idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
        target = store.get_connection_probe_target(connection_id)
        if target["is_active"]:
            current = store.get_record("connections", connection_id)
            current.update(
                activation_state="ALREADY_ACTIVE",
                needs_restart=False,
                previous_connection_id=connection_id,
                control_command=None,
                message="This connection is already selected; no restart was queued.",
            )
            return current
        probe_result = await _run_connection_probe(
            connection_probe,
            target["bot_id"],
            target["secret"],
            settings.connection_probe_timeout_seconds,
        )
        public_probe = probe_result.public()
        public_probe["mode"] = "ISOLATED_SDK_PROBE"
        store.record_connection_test(
            connection_id,
            public_probe,
            principal["user"]["id"],
            _client_ip(request),
        )
        if not probe_result.ok:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CONNECTION_CANDIDATE_REJECTED",
                    "message": "The candidate was not activated because live authentication failed.",
                    "probe": public_probe,
                },
            )
        key = idempotency_key or f"connection-activate:{connection_id}:v{target['version']}"
        return store.activate_connection_after_probe(
            connection_id,
            expected_version=target["version"],
            probe_result=public_probe,
            actor_id=principal["user"]["id"],
            ip=_client_ip(request),
            idempotency_key=key,
        )

    @app.get(f"{API_PREFIX}/users/{{user_id}}", tags=["users"])
    def user_detail(user_id: str, _p: Annotated[dict[str, Any], Depends(_permission("users.read"))]) -> dict[str, Any]:
        return store.get_record("users", user_id)

    @app.patch(f"{API_PREFIX}/users/{{user_id}}", tags=["users"])
    def update_user(user_id: str, body: UserUpdate, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("users.write"))]) -> dict[str, Any]:
        return store.update_wecom_user(user_id, body.model_dump(exclude_unset=True), principal["user"]["id"], _client_ip(request))

    @app.get(f"{API_PREFIX}/conversations/{{conversation_id}}", tags=["conversations"])
    def conversation_detail(conversation_id: str, _p: Annotated[dict[str, Any], Depends(_permission("conversations.read"))]) -> dict[str, Any]:
        return store.get_record("conversations", conversation_id)

    @app.get(f"{API_PREFIX}/conversations/{{conversation_id}}/messages", tags=["conversations"])
    def conversation_messages(conversation_id: str, _p: Annotated[dict[str, Any], Depends(_permission("conversations.read"))], page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        store.get_record("conversations", conversation_id)
        return store.list_conversation_messages(conversation_id, page, page_size)

    @app.post(f"{API_PREFIX}/conversations/{{conversation_id}}/end", status_code=202, tags=["conversations"])
    def end_conversation(conversation_id: str, body: ControlRequest, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("tasks.control"))], idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
        conversation = store.get_record("conversations", conversation_id)
        key = idempotency_key or f"end:{conversation_id}:{principal['user']['id']}"
        payload = body.model_dump()
        payload.update(
            session_id=f"wecom:{conversation['chat_type']}:{conversation['external_chat_id']}",
            external_chat_id=conversation["external_chat_id"],
            chat_type=conversation["chat_type"],
        )
        return store.enqueue_control_command("END_SESSION", "conversation", conversation_id, payload=payload, idempotency_key=key, actor_id=principal["user"]["id"], ip=_client_ip(request))

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}", tags=["tasks"])
    def task_detail(task_id: str, _p: Annotated[dict[str, Any], Depends(_permission("tasks.read"))]) -> dict[str, Any]:
        return store.task_detail(task_id)

    @app.post(f"{API_PREFIX}/tasks/{{task_id}}/cancel", status_code=202, tags=["tasks"])
    def cancel_task(task_id: str, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("tasks.control"))], idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
        key = idempotency_key or f"cancel:{task_id}:{principal['user']['id']}"
        return store.enqueue_task_cancel(task_id, key, principal["user"]["id"], _client_ip(request))

    @app.post(f"{API_PREFIX}/tasks/{{task_id}}/retry", status_code=202, tags=["tasks"])
    def retry_task(task_id: str, body: ControlRequest, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("tasks.control"))], idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
        del body, request, principal, idempotency_key
        store.get_record("tasks", task_id)
        raise HTTPException(
            status_code=501,
            detail={
                "code": "TASK_RETRY_NOT_SUPPORTED",
                "message": "A WeCom response frame is required to replay a task safely; retry is not supported by this worker version.",
            },
        )

    @app.get(f"{API_PREFIX}/control-commands/{{command_id}}", tags=["tasks"])
    def control_status(command_id: str, _p: Annotated[dict[str, Any], Depends(_permission("tasks.read"))]) -> dict[str, Any]:
        return store.get_control_command(command_id)

    @app.post(
        f"{API_PREFIX}/deliveries/{{delivery_id}}/retry",
        status_code=202,
        tags=["deliveries"],
    )
    def retry_delivery(
        delivery_id: str,
        request: Request,
        principal: Annotated[
            dict[str, Any], Depends(_permission("artifacts.send"))
        ],
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", max_length=200
        ),
    ) -> dict[str, Any]:
        key = idempotency_key or f"delivery-retry:{delivery_id}:{uuid.uuid4()}"
        return store.enqueue_delivery_retry(
            delivery_id,
            key,
            principal["user"]["id"],
            _client_ip(request),
        )

    # Agent config profiles and immutable revisions --------------------------
    @app.get(f"{API_PREFIX}/config-profiles/{{profile_id}}", tags=["configs"])
    def config_profile(profile_id: str, _p: Annotated[dict[str, Any], Depends(_permission("configs.read"))]) -> dict[str, Any]:
        return store.get_config_profile(profile_id)

    @app.post(f"{API_PREFIX}/config-profiles", status_code=201, tags=["configs"])
    def create_config_profile(body: ConfigProfileCreate, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("configs.write"))]) -> dict[str, Any]:
        return store.create_config_profile(body.name, body.description, principal["user"]["id"], _client_ip(request))

    @app.patch(f"{API_PREFIX}/config-profiles/{{profile_id}}", tags=["configs"])
    def update_config_profile(profile_id: str, body: ConfigProfileUpdate, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("configs.write"))]) -> dict[str, Any]:
        return store.update_config_profile(profile_id, body.model_dump(exclude_unset=True), principal["user"]["id"], _client_ip(request))

    @app.post(f"{API_PREFIX}/config-profiles/{{profile_id}}/revisions", status_code=201, tags=["configs"])
    def create_config_revision(profile_id: str, body: ConfigRevisionCreate, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("configs.write"))]) -> dict[str, Any]:
        return store.create_config_revision(profile_id, body.model_dump(), principal["user"]["id"], _client_ip(request))

    @app.post(f"{API_PREFIX}/config-profiles/{{profile_id}}/revisions/{{revision_id}}/publish", tags=["configs"])
    def publish_config(profile_id: str, revision_id: str, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("configs.publish"))]) -> dict[str, Any]:
        return store.publish_config_revision(profile_id, revision_id, principal["user"]["id"], _client_ip(request))

    @app.post(f"{API_PREFIX}/config-profiles/{{profile_id}}/rollback/{{revision_id}}", status_code=201, tags=["configs"])
    def rollback_config(profile_id: str, revision_id: str, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("configs.publish"))]) -> dict[str, Any]:
        return store.rollback_config(profile_id, revision_id, principal["user"]["id"], _client_ip(request))

    # Alerts and administrator RBAC ------------------------------------------
    @app.post(f"{API_PREFIX}/alerts/{{alert_id}}/acknowledge", tags=["alerts"])
    def acknowledge_alert(alert_id: str, body: AlertAction, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("alerts.write"))]) -> dict[str, Any]:
        return store.update_alert(alert_id, "acknowledge", body.note, principal["user"]["id"], _client_ip(request))

    @app.post(f"{API_PREFIX}/alerts/{{alert_id}}/resolve", tags=["alerts"])
    def resolve_alert(alert_id: str, body: AlertAction, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("alerts.write"))]) -> dict[str, Any]:
        return store.update_alert(alert_id, "resolve", body.note, principal["user"]["id"], _client_ip(request))

    @app.get(f"{API_PREFIX}/roles", tags=["administrators"])
    def roles(_p: Annotated[dict[str, Any], Depends(_permission("admins.read"))]) -> dict[str, Any]:
        items = store.list_roles()
        return {"items": items, "page": 1, "page_size": len(items), "total": len(items)}

    @app.get(f"{API_PREFIX}/admin-users", tags=["administrators"])
    def admin_users(_p: Annotated[dict[str, Any], Depends(_permission("admins.read"))], page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200), q: str | None = Query(None, max_length=200)) -> dict[str, Any]:
        return store.list_admin_users(page, page_size, q)

    @app.post(f"{API_PREFIX}/admin-users", status_code=201, tags=["administrators"])
    def create_admin_user(body: AdminUserCreate, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("admins.write"))]) -> dict[str, Any]:
        try:
            password = hash_password(body.password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "WEAK_PASSWORD", "message": str(exc)}) from exc
        return store.create_admin_user(body.username, body.display_name, password, body.roles, principal["user"]["id"], _client_ip(request))

    @app.put(f"{API_PREFIX}/admin-users/{{user_id}}/roles", tags=["administrators"])
    def assign_admin_roles(user_id: str, body: RoleAssignment, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("admins.write"))]) -> dict[str, Any]:
        return store.assign_roles(user_id, body.roles, principal["user"]["id"], _client_ip(request))

    # Online backup, retention and honest runtime command queue --------------
    @app.post(f"{API_PREFIX}/system/backup", status_code=201, tags=["system"])
    def backup(request: Request, principal: Annotated[dict[str, Any], Depends(_permission("system.backup"))]) -> dict[str, Any]:
        directory = settings.backup_directory or settings.database_path.parent / "backups"
        return store.backup_database(directory, principal["user"]["id"], _client_ip(request))

    @app.post(f"{API_PREFIX}/system/retention", tags=["system"])
    def retention(body: RetentionRequest, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("system.retention"))]) -> dict[str, Any]:
        return store.retention(**body.model_dump(), actor_id=principal["user"]["id"], ip=_client_ip(request))

    @app.post(f"{API_PREFIX}/runtime/services/{{service}}/{{action}}", status_code=202, tags=["runtime"])
    def control_service(service: str, action: str, request: Request, principal: Annotated[dict[str, Any], Depends(_permission("runtime.control"))], idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
        allowed_services = SUPPORTED_SERVICES
        allowed_actions = {"start": "START_SERVICE", "stop": "STOP_SERVICE", "restart": "RESTART_SERVICE"}
        if service not in allowed_services or action not in allowed_actions:
            raise HTTPException(status_code=404, detail={"code": "RUNTIME_ACTION_NOT_FOUND", "message": "Unsupported managed service or action"})
        key = idempotency_key or str(uuid.uuid4())
        return store.enqueue_control_command(allowed_actions[action], "service", service, payload={}, idempotency_key=key, actor_id=principal["user"]["id"], ip=_client_ip(request))

    @app.get(f"{API_PREFIX}/events/stream", tags=["events"])
    async def stream_events(request: Request, _p: Annotated[dict[str, Any], Depends(_permission("dashboard.read"))], after: int = Query(0, ge=0)) -> StreamingResponse:
        async def generate():
            cursor, silent_polls = after, 0
            while not await request.is_disconnected():
                events = await asyncio.to_thread(store.fetch_events, cursor, 200)
                if events:
                    silent_polls = 0
                    for event in events:
                        cursor = event["seq"]
                        data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {data}\n\n"
                else:
                    silent_polls += 1
                    if silent_polls * settings.sse_poll_seconds >= 15:
                        yield f": ping {_now()}\n\n"
                        silent_polls = 0
                await asyncio.sleep(settings.sse_poll_seconds)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/admin/", status_code=307)

    static = settings.static_directory
    if static and static.is_dir():
        app.mount("/admin", StaticFiles(directory=static, html=True), name="admin")
    else:
        @app.get("/admin/", include_in_schema=False)
        def missing_frontend() -> JSONResponse:
            return JSONResponse({"message": "Admin frontend is not installed", "docs": f"{API_PREFIX}/docs"}, status_code=503)
    return app


def _register_list_routes(app: FastAPI, store: AdminStore) -> None:
    definitions = {
        "connections": "connections.read", "users": "users.read",
        "conversations": "conversations.read", "tasks": "tasks.read",
        "tool-calls": "tasks.read", "artifacts": "artifacts.read",
        "deliveries": "artifacts.read", "logs": "logs.read", "audit": "audit.read",
        "alerts": "alerts.read", "nodes": "runtime.read", "services": "runtime.read",
        "config-profiles": "configs.read",
    }
    table_names = {"tool-calls": "tool_calls", "config-profiles": "config_profiles"}
    for route, permission in definitions.items():
        table = table_names.get(route, route)

        def endpoint_factory(table_name: str, required_permission: str):
            def endpoint(
                page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
                q: str | None = Query(None, max_length=200), status_filter: str | None = Query(None, alias="status", max_length=50),
                connection_id: str | None = Query(None, max_length=100), trace_id: str | None = Query(None, max_length=100),
                _principal_value: dict[str, Any] = Depends(_permission(required_permission)),
            ) -> dict[str, Any]:
                del _principal_value
                return store.list_page(table_name, page=page, page_size=page_size, q=q, status=status_filter, connection_id=connection_id, trace_id=trace_id)

            return endpoint

        endpoint = endpoint_factory(table, permission)
        endpoint.__name__ = f"list_{route.replace('-', '_')}"
        app.add_api_route(f"{API_PREFIX}/{route}", endpoint, methods=["GET"], tags=[route])


async def _run_connection_probe(
    probe: ConnectionProbe,
    bot_id: str,
    secret: str,
    timeout_seconds: float,
) -> ConnectionProbeResult:
    try:
        return await probe.probe(
            bot_id, secret, timeout_seconds=timeout_seconds
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # A custom/injected SDK implementation must not be able to make FastAPI
        # serialize an exception that echoes the supplied Secret.
        return ConnectionProbeResult(
            ok=False,
            code="PROBE_INTERNAL_ERROR",
            phase="sdk",
            message="The isolated credential probe failed unexpectedly.",
            stages={
                "configuration": {"status": "SUCCEEDED"},
                "sdk": {"status": "FAILED", "code": "PROBE_INTERNAL_ERROR"},
                "network": {"status": "NOT_RUN"},
                "authentication": {"status": "NOT_RUN"},
            },
            duration_ms=0,
        )


def _active_connection_observation(target: dict[str, Any]) -> ConnectionProbeResult:
    """Report live-worker evidence without opening a competing same-Bot socket."""

    online = str(target.get("status") or "").upper() == "ONLINE"
    state = "SUCCEEDED" if online else "NOT_VERIFIED"
    return ConnectionProbeResult(
        ok=online,
        code="LIVE_WORKER_AUTHENTICATED" if online else "ACTIVE_CONNECTION_NOT_ONLINE",
        phase="runtime",
        message=(
            "The active Bridge has emitted an authenticated runtime event. A competing probe was not opened."
            if online
            else "The active connection is not currently ONLINE. A competing same-Bot probe was not opened."
        ),
        stages={
            "configuration": {"status": "SUCCEEDED"},
            "sdk": {"status": "OBSERVED_ON_LIVE_WORKER"},
            "network": {"status": state},
            "authentication": {"status": state},
        },
        duration_ms=0,
    )


async def _principal(request: Request) -> dict[str, Any]:
    store: AdminStore = request.app.state.admin_store
    settings: AdminSettings = request.app.state.admin_settings
    authorization = request.headers.get("authorization", "")
    mode, token = "cookie", request.cookies.get(settings.cookie_name, "")
    if authorization.lower().startswith("bearer "):
        mode, token = "token", authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "Authentication is required"})
    session = await asyncio.to_thread(store.resolve_session, token)
    if not session or session["kind"] != mode:
        raise HTTPException(status_code=401, detail={"code": "SESSION_INVALID", "message": "Session is invalid or expired"})
    if mode == "cookie" and request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or not secrets.compare_digest(token_hash(csrf), session.get("csrf_hash") or ""):
            raise HTTPException(status_code=403, detail={"code": "CSRF_INVALID", "message": "CSRF token is missing or invalid"})
    return session


def _permission(name: str):
    async def dependency(principal: Annotated[dict[str, Any], Depends(_principal)]) -> dict[str, Any]:
        if name not in principal["user"]["permissions"]:
            raise HTTPException(status_code=403, detail={"code": "PERMISSION_DENIED", "message": f"Permission {name} is required"})
        return principal
    return dependency


def _error(request: Request, status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"detail": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}
    if details is not None:
        body["detail"]["details"] = details
    return JSONResponse(body, status_code=status_code)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
