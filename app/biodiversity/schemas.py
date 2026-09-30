"""物种清单发布服务的请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RANKS = ("界", "门", "纲", "目", "科", "属", "种")
Rank = Literal["界", "门", "纲", "目", "科", "属", "种"]
ChangeType = Literal["新增", "修订", "拆分", "合并", "保留", "移除"]
SensitiveLevel = Literal[0, 1, 2, 3]


class RegionCreate(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{2,32}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)


class ReferenceCreate(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_.:-]{2,48}$")
    kind: Literal["文献", "标本", "影像", "数据库", "专家意见"]
    title: str = Field(min_length=1, max_length=300)
    authors: str = Field(default="", max_length=300)
    published_on: str = Field(default="", max_length=32)
    url: str = Field(default="", max_length=500)


class SensitiveGrant(BaseModel):
    region_code: str = Field(min_length=1, max_length=32, description="区域代码，'*' 表示全局兜底授权")
    max_sensitive_level: SensitiveLevel = 0
    note: str = Field(default="", max_length=300)


class TaxonEntry(BaseModel):
    taxon_key: str = Field(pattern=r"^[A-Za-z0-9_.-]{2,64}$")
    scientific_name: str = Field(min_length=1, max_length=200)
    parent_key: str | None = Field(default=None, max_length=64)
    taxon_rank: Rank
    protection_level: str = Field(default="", max_length=60, description="如 国家二级 / IUCN 易危")
    sensitive_level: SensitiveLevel = 0
    aliases: list[str] = Field(default_factory=list, max_length=30)
    region_codes: list[str] = Field(default_factory=list, max_length=50)
    reference_codes: list[str] = Field(default_factory=list, max_length=50)
    change_type: ChangeType = "修订"
    change_reason: str = Field(default="", max_length=500)

    @field_validator("scientific_name", "protection_level")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("aliases", "region_codes", "reference_codes")
    @classmethod
    def _unique(cls, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("列表中存在重复项")
        return cleaned


class ChecklistCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    change_summary: str = Field(default="", max_length=1000)


class ChecklistUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    change_summary: str | None = Field(default=None, max_length=1000)


class SubmitRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class ReviewDecision(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=1000)


class PublishRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class WithdrawRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
