from __future__ import annotations

from fastapi import APIRouter, Header, Query

from app.biodiversity.schemas import (
    ChecklistCreate,
    CitationCreate,
    DraftCreate,
    PublishRequest,
    RegionCreate,
    ReviewDecision,
    ReviewRequest,
    SensitiveGrant,
    SpeciesRemove,
    SpeciesUpsert,
    WithdrawRequest,
    NameRegister,
)
from app.biodiversity.service import BiodiversityService

router = APIRouter(prefix="/api/biodiversity", tags=["城市生境物种清单"])


def service() -> BiodiversityService:
    return BiodiversityService()


RoleHeader = Header(default="viewer", alias="X-Role")
ActorQuery = Query(..., min_length=1, max_length=64, description="操作者标识")


# ---------------------------------------------------------------------------
# 基础资料
# ---------------------------------------------------------------------------

@router.post("/regions", status_code=201)
def create_region(payload: RegionCreate, actor: str = ActorQuery, role: str = RoleHeader):
    return service().create_region(payload.model_dump(), actor, role)


@router.get("/regions")
def list_regions(include_inactive: bool = False):
    return {"items": service().list_regions(include_inactive=include_inactive)}


@router.post("/citations", status_code=201)
def create_citation(payload: CitationCreate, actor: str = ActorQuery):
    return service().create_citation(payload.model_dump(), actor)


@router.get("/citations")
def list_citations():
    return {"items": service().list_citations()}


@router.put("/sensitive-grants")
def put_sensitive_grant(payload: SensitiveGrant, actor: str = ActorQuery, role: str = RoleHeader):
    return service().grant_sensitive_scope(payload.scope, payload.region_code, actor, role)


@router.get("/sensitive-grants")
def list_sensitive_grants():
    return {"items": service().list_sensitive_grants()}


@router.post("/taxa/{taxon_id}/names", status_code=201)
def register_name(taxon_id: str, payload: NameRegister, actor: str = ActorQuery):
    data = payload.model_dump()
    data["taxon_id"] = taxon_id
    return service().register_name(data, actor)


@router.get("/taxa/{taxon_id}/names")
def list_names(taxon_id: str):
    return {"items": service().list_names(taxon_id)}


# ---------------------------------------------------------------------------
# 清单 / 草稿 / 复核 / 发布
# ---------------------------------------------------------------------------

@router.post("/checklists", status_code=201)
def create_checklist(payload: ChecklistCreate, actor: str = ActorQuery, role: str = RoleHeader):
    return service().create_checklist(payload.model_dump(), actor, role)


@router.get("/checklists")
def list_checklists():
    return {"items": service().list_checklists()}


@router.get("/checklists/{code}")
def get_checklist(code: str):
    return service().get_checklist(code)


@router.post("/checklists/{code}/drafts", status_code=201)
def create_next_draft(code: str, payload: DraftCreate, actor: str = ActorQuery, role: str = RoleHeader):
    return service().create_next_draft(code, payload.note, actor, role)


@router.get("/checklists/{code}/draft")
def get_draft(code: str):
    return service().get_draft(code)


@router.put("/checklists/{code}/species")
def upsert_species(code: str, payload: SpeciesUpsert, actor: str = ActorQuery, role: str = RoleHeader):
    return service().upsert_species(code, payload.model_dump(), actor, role)


@router.post("/checklists/{code}/species/{taxon_id}/remove")
def remove_species(code: str, taxon_id: str, payload: SpeciesRemove, actor: str = ActorQuery, role: str = RoleHeader):
    return service().remove_species(code, taxon_id, payload.reason, actor, role)


@router.post("/checklists/{code}/review-request")
def request_review(code: str, payload: ReviewRequest, actor: str = ActorQuery, role: str = RoleHeader):
    return service().request_review(code, payload.note, actor, role)


@router.post("/checklists/{code}/review")
def review(code: str, payload: ReviewDecision, actor: str = ActorQuery, role: str = RoleHeader):
    return service().review(code, payload.decision, payload.notes, actor, role)


@router.get("/checklists/{code}/validation")
def validation(code: str):
    return service().validate_draft(code)


@router.post("/checklists/{code}/publish")
def publish(code: str, payload: PublishRequest, actor: str = ActorQuery, role: str = RoleHeader):
    return service().publish(code, actor, role, effective_at=payload.effective_at, note=payload.note)


@router.post("/checklists/{code}/versions/{version_no}/withdraw")
def withdraw(code: str, version_no: int, payload: WithdrawRequest, actor: str = ActorQuery, role: str = RoleHeader):
    return service().withdraw_version(code, version_no, payload.reason, actor, role)


@router.get("/checklists/{code}/versions")
def list_versions(code: str):
    return {"items": service().list_versions(code)}


@router.get("/checklists/{code}/versions/{version_no}")
def get_version(code: str, version_no: int):
    return service().get_version(code, version_no)


@router.get("/checklists/{code}/as-of")
def version_at_date(code: str, date: str = Query(..., description="ISO8601 时间点，按生效日期追溯版本")):
    return service().version_at_date(code, date)


@router.get("/checklists/{code}/name-changes/{version_no}")
def name_changes(code: str, version_no: int):
    return {"items": service().name_changes(code, version_no)}


@router.get("/checklists/{code}/timeline")
def timeline(code: str):
    return {"items": service().event_timeline(code)}


@router.get("/checklists/{code}/species")
def query_species(
    code: str,
    version_no: int | None = None,
    at: str | None = Query(default=None, description="按历史日期查询"),
    region: str | None = None,
    viewer_scope: str = Query(default="public", description="查看者授权范围，决定敏感物种可见性"),
    keyword: str | None = None,
):
    return service().query_species(
        code, version_no=version_no, at=at, region=region, viewer_scope=viewer_scope, keyword=keyword
    )
