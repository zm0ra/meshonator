from __future__ import annotations

from urllib.parse import quote_plus
from uuid import UUID

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from meshonator.api.schemas import BatchPatchRequest, ConfigPatchRequest, DiscoveryRequest, GroupCreateRequest
from meshonator.audit.service import AuditService
from meshonator.auth.security import CurrentUser, authenticate_user, bootstrap_admin, require_role, require_session_user
from meshonator.config.settings import get_settings
from meshonator.db.base import Base
from meshonator.db.models import AuditLogModel, JobModel, ManagedNodeModel, NodeGroupModel, ProviderEndpointModel
from meshonator.db.session import engine, get_db
from meshonator.discovery.service import DiscoveryService
from meshonator.domain.models import ConfigPatch
from meshonator.groups.service import GroupsService
from meshonator.inventory.service import InventoryService
from meshonator.jobs.service import JobsService
from meshonator.map.service import MapService
from meshonator.operations.service import OperationsService
from meshonator.providers.registry import ProviderRegistry
from meshonator.sync.service import SyncService

settings = get_settings()
registry = ProviderRegistry()

app = FastAPI(title="Meshonator", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 8)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="src/meshonator/static"), name="static")
templates = Jinja2Templates(directory="src/meshonator/templates")


def _parse_multi_value(raw: str) -> list[str]:
    values = []
    for chunk in raw.replace(",", "\n").splitlines():
        item = chunk.strip()
        if item:
            values.append(item)
    return values


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        bootstrap_admin(
            db,
            username=settings.bootstrap_admin_username,
            password=settings.bootstrap_admin_password,
            role=settings.bootstrap_admin_role,
        )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "providers": [p.health().model_dump() for p in registry.all()],
        "tcp_only": True,
    }


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = authenticate_user(db, username, password)
    if user is None:
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid credentials"}, status_code=401)
    request.session["user"] = {"username": user.username, "role": user.role}
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_session_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    total_nodes = db.scalar(select(func.count()).select_from(ManagedNodeModel)) or 0
    online_nodes = db.scalar(select(func.count()).select_from(ManagedNodeModel).where(ManagedNodeModel.reachable.is_(True))) or 0
    stale_nodes = db.scalar(select(func.count()).select_from(ManagedNodeModel).where(ManagedNodeModel.reachable.is_(False))) or 0
    pending_jobs = db.scalar(select(func.count()).select_from(JobModel).where(JobModel.status.in_(["pending", "running"]))) or 0
    failed_jobs = db.scalar(select(func.count()).select_from(JobModel).where(JobModel.status == "failed")) or 0
    provider_summary = list(
        db.execute(select(ManagedNodeModel.provider, func.count()).group_by(ManagedNodeModel.provider)).all()
    )
    recent_changes = list(db.scalars(select(AuditLogModel).order_by(AuditLogModel.created_at.desc()).limit(10)).all())
    providers = [provider.name for provider in registry.all()]
    ui_message = request.query_params.get("message")
    ui_error = request.query_params.get("error")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "total_nodes": total_nodes,
            "online_nodes": online_nodes,
            "stale_nodes": stale_nodes,
            "pending_jobs": pending_jobs,
            "failed_jobs": failed_jobs,
            "provider_summary": provider_summary,
            "recent_changes": recent_changes,
            "providers": providers,
            "ui_message": ui_message,
            "ui_error": ui_error,
        },
    )


@app.post("/ui/discovery/scan")
def ui_discovery_scan(
    provider: str = Form("meshtastic"),
    hosts_text: str = Form(""),
    cidrs_text: str = Form(""),
    endpoints_text: str = Form(""),
    port: int | None = Form(default=None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    hosts = _parse_multi_value(hosts_text)
    cidrs = _parse_multi_value(cidrs_text)
    endpoints = _parse_multi_value(endpoints_text)
    try:
        found = DiscoveryService(db, registry).scan(
            provider_name=provider,
            hosts=hosts,
            cidrs=cidrs,
            manual_endpoints=endpoints,
            port=port,
            source="ui",
        )
        AuditService(db).log(
            actor=user.username,
            source="ui",
            action="discovery.scan",
            provider=provider,
            metadata={
                "hosts": hosts,
                "cidrs": cidrs,
                "manual_endpoints": endpoints,
                "port": port,
            },
        )
        message = quote_plus(f"Discovery complete: {len(found)} reachable endpoints.")
        return RedirectResponse(url=f"/?message={message}", status_code=303)
    except Exception as exc:
        error = quote_plus(f"Discovery failed: {exc}")
        return RedirectResponse(url=f"/?error={error}", status_code=303)


@app.post("/ui/sync/run")
def ui_sync_run(
    quick: bool = Form(False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> RedirectResponse:
    jobs = JobsService(db)
    job = jobs.create(job_type="sync_all", requested_by=user.username, source="ui", payload={"quick": quick})
    jobs.start(job.id)
    results = SyncService(db, registry).sync_all(quick=quick)
    success = all(result.get("status") == "success" for result in results)
    jobs.finish(job.id, success=success)
    AuditService(db).log(actor=user.username, source="ui", action="sync.all", metadata={"results": results})
    failures = sum(1 for result in results if result.get("status") != "success")
    message = quote_plus(
        f"Sync finished. Endpoints: {len(results)}, failed: {failures}, mode: {'quick' if quick else 'full'}."
    )
    return RedirectResponse(url=f"/?message={message}", status_code=303)


@app.get("/nodes", response_class=HTMLResponse)
def nodes_page(
    request: Request,
    provider: str | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    svc = InventoryService(db)
    nodes = svc.list_nodes(provider=provider)
    return templates.TemplateResponse(request, "nodes.html", {"user": user, "nodes": nodes, "provider": provider})


@app.get("/nodes/{node_id}", response_class=HTMLResponse)
def node_details_page(
    request: Request,
    node_id: UUID,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    node = SyncService(db, registry).node_details(str(node_id))
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    audits = list(db.scalars(select(AuditLogModel).where(AuditLogModel.node_id == node.id).order_by(AuditLogModel.created_at.desc()).limit(20)).all())
    return templates.TemplateResponse(request, "node_detail.html", {"user": user, "node": node, "audits": audits})


@app.get("/map", response_class=HTMLResponse)
def map_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    markers = MapService(db).markers()
    return templates.TemplateResponse(
        request,
        "map.html",
        {
            "user": user,
            "markers": markers,
            "map_default_lat": settings.map_default_lat,
            "map_default_lon": settings.map_default_lon,
            "map_default_zoom": settings.map_default_zoom,
        },
    )


@app.get("/groups", response_class=HTMLResponse)
def groups_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    groups = GroupsService(db).list_groups()
    return templates.TemplateResponse(request, "groups.html", {"user": user, "groups": groups})


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    jobs = JobsService(db).list_jobs()
    return templates.TemplateResponse(request, "jobs.html", {"user": user, "jobs": jobs})


@app.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    entries = AuditService(db).list_recent(limit=300)
    return templates.TemplateResponse(request, "audit.html", {"user": user, "entries": entries})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: CurrentUser = Depends(require_session_user),
) -> HTMLResponse:
    providers = [
        {
            "name": p.name,
            "health": p.health().model_dump(),
            "operation_matrix": p.operation_matrix().model_dump(),
            "capabilities": p.capabilities().model_dump(),
            "experimental": p.experimental,
        }
        for p in registry.all()
    ]
    return templates.TemplateResponse(request, "settings.html", {"user": user, "providers": providers, "tcp_only": True})


@app.post("/api/discovery/scan")
def api_discovery_scan(
    payload: DiscoveryRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    found = DiscoveryService(db, registry).scan(
        provider_name=payload.provider,
        hosts=payload.hosts,
        cidrs=payload.cidrs,
        manual_endpoints=payload.manual_endpoints,
        port=payload.port,
        source="api",
    )
    AuditService(db).log(actor=user.username, source="api", action="discovery.scan", provider=payload.provider, metadata=payload.model_dump())
    return {"count": len(found), "endpoints": [e.endpoint for e in found], "tcp_only": True}


@app.get("/api/nodes")
def api_nodes(
    provider: str | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> list[dict]:
    rows = InventoryService(db).list_nodes(provider=provider)
    return [
        {
            "id": str(r.id),
            "provider": r.provider,
            "provider_node_id": r.provider_node_id,
            "short_name": r.short_name,
            "long_name": r.long_name,
            "firmware": r.firmware_version,
            "hardware": r.hardware_model,
            "role": r.role,
            "favorite": r.favorite,
            "location": {"lat": r.latitude, "lon": r.longitude, "alt": r.altitude},
            "last_seen": r.last_seen,
            "reachable": r.reachable,
            "capabilities": r.capability_matrix,
        }
        for r in rows
    ]


@app.post("/api/sync")
def api_sync_all(
    quick: bool = False,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    jobs = JobsService(db)
    job = jobs.create(job_type="sync_all", requested_by=user.username, source="api", payload={"quick": quick})
    jobs.start(job.id)
    results = SyncService(db, registry).sync_all(quick=quick)
    success = all(r.get("status") == "success" for r in results)
    jobs.finish(job.id, success=success)
    AuditService(db).log(actor=user.username, source="api", action="sync.all", metadata={"results": results})
    return {"job_id": str(job.id), "results": results}


@app.post("/api/nodes/{node_id}/patch")
def api_patch_node(
    node_id: UUID,
    payload: ConfigPatchRequest,
    dry_run: bool = True,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    patch = ConfigPatch(**payload.model_dump())
    node = db.get(ManagedNodeModel, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")
    operation_matrix = registry.get(node.provider).operation_matrix()
    if operation_matrix.write_config.value == "unsupported_or_restricted":
        raise HTTPException(status_code=400, detail="Provider does not support remote config write")
    return OperationsService(db, registry).apply_patch(node_id, patch, actor=user.username, source="api", dry_run=dry_run)


@app.post("/api/batch/patch")
def api_batch_patch(
    payload: BatchPatchRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("admin")),
) -> dict:
    results = []
    ops = OperationsService(db, registry)
    for node_id in payload.node_ids:
        try:
            result = ops.apply_patch(
                node_id=node_id,
                patch=ConfigPatch(**payload.patch.model_dump()),
                actor=user.username,
                source="api",
                dry_run=payload.dry_run,
            )
            results.append({"node_id": str(node_id), "status": "success", "result": result})
        except Exception as exc:
            results.append({"node_id": str(node_id), "status": "failed", "error": str(exc)})
    return {"results": results}


@app.get("/api/map/markers")
def api_map_markers(
    provider: str | None = None,
    favorite: bool | None = None,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_session_user),
) -> list[dict]:
    return MapService(db).markers(provider=provider, favorite=favorite)


@app.get("/api/providers/capabilities")
def api_provider_capabilities(user: CurrentUser = Depends(require_session_user)) -> dict:
    _ = user
    return {
        p.name: {
            "experimental": p.experimental,
            "capabilities": p.capabilities().model_dump(),
            "operation_matrix": p.operation_matrix().model_dump(),
            "health": p.health().model_dump(),
            "transport": "tcp-only",
        }
        for p in registry.all()
    }


@app.get("/api/jobs")
def api_jobs(db: Session = Depends(get_db), user: CurrentUser = Depends(require_session_user)) -> list[dict]:
    _ = user
    return [
        {
            "id": str(job.id),
            "job_type": job.job_type,
            "status": job.status,
            "requested_by": job.requested_by,
            "source": job.source,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }
        for job in JobsService(db).list_jobs()
    ]


@app.post("/api/groups")
def api_groups_create(
    payload: GroupCreateRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_role("operator")),
) -> dict:
    group = GroupsService(db).create_group(
        name=payload.name,
        description=payload.description,
        dynamic_filter=payload.dynamic_filter,
        desired_config_template=payload.desired_config_template,
    )
    AuditService(db).log(actor=user.username, source="api", action="group.create", group_id=group.id, after_state=payload.model_dump())
    return {
        "id": str(group.id),
        "name": group.name,
        "description": group.description,
        "dynamic_filter": group.dynamic_filter,
        "desired_config_template": group.desired_config_template,
    }
