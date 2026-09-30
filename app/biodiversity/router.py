"""物种清单发布服务 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.biodiversity.schemas import (
    ChecklistCreate,
    ChecklistUpdate,
    PublishRequest,
    ReferenceCreate,
    RegionCreate,
    ReviewDecision,
    SensitiveGrant,
    SubmitRequest,
    TaxonEntry,
    WithdrawRequest,
)
from app.biodiversity.service import BiodiversityService
from app.core.security import Principal

router = APIRouter(prefix="/api/biodiversity", tags=["城市生境物种清单"])


def service() -> BiodiversityService:
    return BiodiversityService()


# ---------------------------------------------------------------- 基础资料

@router.post("/regions", status_code=201)
def create_region(payload: RegionCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().create_region(principal, payload.model_dump())


@router.get("/regions")
def list_regions(principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_regions(principal)


@router.post("/references", status_code=201)
def create_reference(payload: ReferenceCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().create_reference(principal, payload.model_dump())


@router.get("/references")
def list_references(principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_references(principal)


@router.put("/sensitive-grants", status_code=200)
def put_grant(payload: SensitiveGrant, principal: Principal = Depends(current_principal)) -> dict:
    return service().set_grant(principal, payload.model_dump())


@router.get("/sensitive-grants")
def list_grants(principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_grants(principal)


# ---------------------------------------------------------------- 草稿生命周期

@router.post("/checklists", status_code=201)
def create_checklist(
    payload: ChecklistCreate,
    base_version: int | None = Query(default=None, description="以某已发布版本为基准拷贝草稿"),
    principal: Principal = Depends(current_principal),
) -> dict:
    return service().create_checklist(principal, payload.model_dump(), base_version=base_version)


@router.get("/checklists")
def list_checklists(status: str | None = Query(default=None),
                    principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_checklists(principal, status=status)


@router.get("/checklists/{checklist_id}")
def get_checklist(checklist_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().checklist_detail(principal, checklist_id)


@router.patch("/checklists/{checklist_id}")
def update_checklist(checklist_id: int, payload: ChecklistUpdate,
                     principal: Principal = Depends(current_principal)) -> dict:
    return service().update_checklist_meta(principal, checklist_id, payload.model_dump(exclude_unset=True))


@router.get("/checklists/{checklist_id}/validation")
def checklist_validation(checklist_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().validation_report(principal, checklist_id)


@router.get("/checklists/{checklist_id}/events")
def checklist_events(checklist_id: int, principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().checklist_events(principal, checklist_id)


@router.put("/checklists/{checklist_id}/entries/{taxon_key}")
def upsert_entry(checklist_id: int, taxon_key: str, payload: TaxonEntry,
                 principal: Principal = Depends(current_principal)) -> dict:
    data = payload.model_dump()
    if data["taxon_key"] != taxon_key:
        from app.core.errors import ValidationError
        raise ValidationError("路径中的 taxon_key 与请求体不一致")
    return service().upsert_entry(principal, checklist_id, data)


@router.delete("/checklists/{checklist_id}/entries/{taxon_key}")
def delete_entry(checklist_id: int, taxon_key: str,
                 principal: Principal = Depends(current_principal)) -> dict:
    return service().remove_entry(principal, checklist_id, taxon_key)


@router.post("/checklists/{checklist_id}/submit")
def submit_checklist(checklist_id: int, payload: SubmitRequest,
                     principal: Principal = Depends(current_principal)) -> dict:
    return service().submit(principal, checklist_id, payload.note)


@router.post("/checklists/{checklist_id}/review")
def review_checklist(checklist_id: int, payload: ReviewDecision,
                     principal: Principal = Depends(current_principal)) -> dict:
    return service().review(principal, checklist_id, payload.approved, payload.comment)


@router.post("/checklists/{checklist_id}/publish")
def publish_checklist(checklist_id: int, payload: PublishRequest,
                      principal: Principal = Depends(current_principal)) -> dict:
    return service().publish(principal, checklist_id, payload.note)


@router.post("/checklists/{checklist_id}/withdraw")
def withdraw_checklist(checklist_id: int, payload: WithdrawRequest,
                       principal: Principal = Depends(current_principal)) -> dict:
    return service().withdraw(principal, checklist_id, payload.reason)


# ---------------------------------------------------------------- 已发布版本查询

@router.get("/versions/{version_no}")
def get_version(version_no: int, region: str | None = Query(default=None),
                principal: Principal = Depends(current_principal)) -> dict:
    return service().version_snapshot(principal, version_no, region=region)


@router.get("/versions/{version_no}/name-changes")
def get_name_changes(version_no: int, principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().name_changes(principal, version_no)


@router.get("/effective")
def get_effective(as_of: str | None = Query(default=None, description="ISO 8601 时刻，按发布日期回溯"),
                  region: str | None = Query(default=None),
                  principal: Principal = Depends(current_principal)) -> dict:
    return service().effective_version(principal, as_of=as_of, region=region)


@router.get("/taxa/{taxon_key}")
def get_taxon(taxon_key: str, principal: Principal = Depends(current_principal)) -> dict:
    return service().taxon_detail(principal, taxon_key)


@router.get("/names")
def search_names(q: str = Query(..., min_length=1, description="按学名/别名/曾用名检索"),
                 principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().search_names(principal, q)
