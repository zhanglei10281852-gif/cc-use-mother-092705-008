from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Role = Literal["editor", "reviewer", "publisher", "admin", "viewer"]


class RegionCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(..., min_length=1, max_length=60)


class CitationCreate(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(default="reference", max_length=24)
    title: str = Field(..., min_length=1, max_length=200)
    authors: str = Field(default="", max_length=200)
    year: Optional[int] = Field(default=None, ge=1800, le=2200)
    url: str = Field(default="", max_length=300)
    detail: str = Field(default="", max_length=500)


class SensitiveGrant(BaseModel):
    scope: str = Field(..., min_length=1, max_length=48)
    region_code: str = Field(..., min_length=1, max_length=32)


class Alias(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    name_type: Literal["synonym", "common_name", "former_name"] = "synonym"
    language: str = Field(default="zh", min_length=2, max_length=8)
    source_citation_key: Optional[str] = Field(default=None, max_length=64)


class NameRegister(BaseModel):
    taxon_id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=120)
    name_type: Literal["synonym", "common_name", "former_name"] = "synonym"
    language: str = Field(default="zh", min_length=2, max_length=8)
    source_citation_key: Optional[str] = Field(default=None, max_length=64)


class ChecklistCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=48, pattern=r"^[a-z0-9_\-]+$")
    title: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class DraftCreate(BaseModel):
    note: str = Field(default="", max_length=500)


class SpeciesUpsert(BaseModel):
    taxon_id: str = Field(..., min_length=1, max_length=64)
    scientific_name: str = Field(..., min_length=1, max_length=140)
    canonical_name: str = Field(..., min_length=1, max_length=140)
    authorship: str = Field(default="", max_length=140)
    taxon_rank: str = Field(default="species", max_length=24)
    parent_taxon_id: str = Field(default="", max_length=64)
    protection_level: str = Field(default="", max_length=48)
    is_sensitive: bool = False
    sensitivity_scope: str = Field(default="", max_length=48)
    visible_region_codes: list[str] = Field(default_factory=list)
    citation_refs: list[str] = Field(default_factory=list)
    occurrence_note: str = Field(default="", max_length=500)
    change_reason: str = Field(..., min_length=1, max_length=500, description="本次名称/信息变化原因，随版本永久留痕")
    aliases: list[Alias] = Field(default_factory=list)


class SpeciesRemove(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class ReviewRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class ReviewDecision(BaseModel):
    decision: Literal["passed", "rejected"]
    notes: str = Field(default="", max_length=1000)


class PublishRequest(BaseModel):
    effective_at: Optional[str] = Field(default=None, description="生效时间（ISO8601），缺省为发布时刻")
    note: str = Field(default="", max_length=500)


class WithdrawRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
