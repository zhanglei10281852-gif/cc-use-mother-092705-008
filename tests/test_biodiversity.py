from __future__ import annotations

import pytest


@pytest.fixture()
def bio(client):
    """准备两个区域、两个引用、一组 research 敏感授权。"""
    def post(path, payload, actor="tester", role="admin"):
        headers = {"X-Role": role}
        return client.post(f"{path}?actor={actor}", json=payload, headers=headers)

    post("/api/biodiversity/regions", {"code": "north", "name": "城北"})
    post("/api/biodiversity/regions", {"code": "south", "name": "城南"})
    post("/api/biodiversity/citations", {"key": "ref1", "title": "文献一", "year": 2020})
    post("/api/biodiversity/citations", {"key": "ref2", "title": "文献二", "year": 2023})
    client.put("/api/biodiversity/sensitive-grants?actor=admin",
               json={"scope": "research", "region_code": "north"}, headers={"X-Role": "admin"})
    client.put("/api/biodiversity/sensitive-grants?actor=admin",
               json={"scope": "research", "region_code": "south"}, headers={"X-Role": "admin"})
    return client


EDITOR = {"X-Role": "editor"}
REVIEWER = {"X-Role": "reviewer"}
PUBLISHER = {"X-Role": "publisher"}
ADMIN = {"X-Role": "admin"}


def _species(client, code, taxon, *, reason="测试收录", **overrides):
    payload = {
        "taxon_id": taxon,
        "scientific_name": taxon,
        "canonical_name": taxon,
        "visible_region_codes": ["north"],
        "citation_refs": ["ref1"],
        "change_reason": reason,
    }
    payload.update(overrides)
    return client.put(f"/api/biodiversity/checklists/{code}/species?actor=editor", json=payload, headers=EDITOR)


def _create_published_v1(bio, code="wetland"):
    bio.post("/api/biodiversity/checklists?actor=editor",
             json={"code": code, "title": "湿地清单"}, headers=EDITOR)
    _species(bio, code, "Cls", taxon_rank="class")
    _species(bio, code, "Ord", taxon_rank="order", parent_taxon_id="Cls")
    _species(bio, code, "Sp1", parent_taxon_id="Ord")
    bio.post(f"/api/biodiversity/checklists/{code}/review-request?actor=editor", json={}, headers=EDITOR)
    bio.post(f"/api/biodiversity/checklists/{code}/review?actor=reviewer",
             json={"decision": "passed", "notes": "ok"}, headers=REVIEWER)
    resp = bio.post(f"/api/biodiversity/checklists/{code}/publish?actor=publisher",
                    json={"effective_at": "2026-01-10T00:00:00+00:00"}, headers=PUBLISHER)
    assert resp.status_code == 200, resp.text
    return code


# ---------------------------------------------------------------------------
# 角色权限
# ---------------------------------------------------------------------------

def test_roles_are_enforced(bio):
    # viewer 不能建清单
    resp = bio.post("/api/biodiversity/checklists?actor=v", json={"code": "x", "title": "x"},
                    headers={"X-Role": "viewer"})
    assert resp.status_code == 403
    # editor 不能管理区域、不能复核、不能发布
    assert bio.post("/api/biodiversity/regions?actor=e", json={"code": "z", "name": "z"},
                    headers=EDITOR).status_code == 403
    bio.post("/api/biodiversity/checklists?actor=editor", json={"code": "c1", "title": "t"}, headers=EDITOR)
    assert bio.post("/api/biodiversity/checklists/c1/review?actor=editor",
                    json={"decision": "passed"}, headers=EDITOR).status_code == 403
    assert bio.post("/api/biodiversity/checklists/c1/publish?actor=editor", json={},
                    headers=EDITOR).status_code == 403


def test_change_reason_is_mandatory(bio):
    bio.post("/api/biodiversity/checklists?actor=editor", json={"code": "c", "title": "t"}, headers=EDITOR)
    resp = _species(bio, "c", "S")
    resp = bio.put("/api/biodiversity/checklists/c/species?actor=editor",
                   json={"taxon_id": "S", "scientific_name": "S", "canonical_name": "S"},
                   headers=EDITOR)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 发布前三类校验
# ---------------------------------------------------------------------------

def test_missing_citation_blocks_publish(bio):
    bio.post("/api/biodiversity/checklists?actor=editor", json={"code": "c", "title": "t"}, headers=EDITOR)
    _species(bio, "c", "S", citation_refs=["ghost-ref"])
    report = bio.get("/api/biodiversity/checklists/c/validation").json()
    assert report["passed"] is False
    codes = {check["code"]: check for check in report["checks"]}
    assert codes["citations_exist"]["passed"] is False
    assert any("ghost-ref" in d["problem"] for d in codes["citations_exist"]["details"])


def test_dangling_parent_blocks_publish(bio):
    bio.post("/api/biodiversity/checklists?actor=editor", json={"code": "c", "title": "t"}, headers=EDITOR)
    _species(bio, "c", "S", parent_taxon_id="GHOST")
    report = bio.get("/api/biodiversity/checklists/c/validation").json()
    taxonomy = next(c for c in report["checks"] if c["code"] == "taxonomy_closed")
    assert taxonomy["passed"] is False
    assert any("不在本版本中" in d["problem"] for d in taxonomy["details"])


def test_taxonomy_cycle_is_detected(bio):
    bio.post("/api/biodiversity/checklists?actor=editor", json={"code": "c", "title": "t"}, headers=EDITOR)
    _species(bio, "c", "A", taxon_rank="class", parent_taxon_id="B")
    _species(bio, "c", "B", taxon_rank="order", parent_taxon_id="A")
    report = bio.get("/api/biodiversity/checklists/c/validation").json()
    taxonomy = next(c for c in report["checks"] if c["code"] == "taxonomy_closed")
    assert taxonomy["passed"] is False
    assert any("环" in d["problem"] for d in taxonomy["details"])
    # 解环后通过
    _species(bio, "c", "A", taxon_rank="class", parent_taxon_id="", reason="解环")
    report = bio.get("/api/biodiversity/checklists/c/validation").json()
    assert report["passed"] is True


def test_sensitive_species_outside_grant_blocks_publish(bio):
    bio.post("/api/biodiversity/checklists?actor=editor", json={"code": "c", "title": "t"}, headers=EDITOR)
    # 未标记 scope
    _species(bio, "c", "S", is_sensitive=True, sensitivity_scope="", reason="敏感")
    report = bio.get("/api/biodiversity/checklists/c/validation").json()
    sensitive = next(c for c in report["checks"] if c["code"] == "sensitive_authorized")
    assert sensitive["passed"] is False
    # scope 已授权 north，改投未授权区域 south 之外的新区域会失败；直接使用越权 scope
    _species(bio, "c", "S", is_sensitive=True, sensitivity_scope="poaching-invest",
             visible_region_codes=["north"], reason="敏感-越权范围")
    report = bio.get("/api/biodiversity/checklists/c/validation").json()
    sensitive = next(c for c in report["checks"] if c["code"] == "sensitive_authorized")
    assert sensitive["passed"] is False
    assert any("超出授权范围" in d["problem"] for d in sensitive["details"])
    # 取得授权后通过
    bio.put("/api/biodiversity/sensitive-grants?actor=admin",
            json={"scope": "poaching-invest", "region_code": "north"}, headers=ADMIN)
    report = bio.get("/api/biodiversity/checklists/c/validation").json()
    assert report["passed"] is True


def test_publish_requires_review_pass_and_validation(bio):
    bio.post("/api/biodiversity/checklists?actor=editor", json={"code": "c", "title": "t"}, headers=EDITOR)
    _species(bio, "c", "S", citation_refs=["ghost"])
    bio.post("/api/biodiversity/checklists/c/review-request?actor=editor", json={}, headers=EDITOR)
    bio.post("/api/biodiversity/checklists/c/review?actor=reviewer",
             json={"decision": "passed"}, headers=REVIEWER)
    # 复核通过但校验不过，仍拒绝发布
    resp = bio.post("/api/biodiversity/checklists/c/publish?actor=publisher", json={}, headers=PUBLISHER)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# 发布快照、名称变化、历史追溯、撤回
# ---------------------------------------------------------------------------

def test_published_versions_are_immutable_snapshots(bio):
    code = _create_published_v1(bio)
    # 发布后无开放草稿，不能再直接改物种
    resp = _species(bio, code, "Sp1", reason="试图改已发布版本")
    assert resp.status_code == 409
    # 已发布版本内容固定
    v1 = bio.get(f"/api/biodiversity/checklists/{code}/versions/1").json()
    assert {s["taxon_id"] for s in v1["species"] if not s["is_removed"]} == {"Cls", "Ord", "Sp1"}


def test_name_changes_capture_reasons_and_regions(bio):
    code = _create_published_v1(bio)
    bio.post(f"/api/biodiversity/checklists/{code}/drafts?actor=editor", json={"note": "修订"}, headers=EDITOR)
    _species(bio, code, "Sp1", parent_taxon_id="Ord", protection_level="国家二级",
             visible_region_codes=["north", "south"], reason="专家上调保护等级并扩展城南")
    _species(bio, code, "Sp2", parent_taxon_id="Ord", reason="新收录")
    # 撤编高阶节点前先把其子节点改挂顶层，保证分类链闭合
    _species(bio, code, "Ord", taxon_rank="order", parent_taxon_id="", reason="Cls 撤编后改挂顶层")
    bio.post(f"/api/biodiversity/checklists/{code}/species/Cls/remove?actor=editor",
             json={"reason": "高阶节点移至通用分册"}, headers=EDITOR)
    bio.post(f"/api/biodiversity/checklists/{code}/review-request?actor=editor", json={}, headers=EDITOR)
    bio.post(f"/api/biodiversity/checklists/{code}/review?actor=reviewer",
             json={"decision": "passed"}, headers=REVIEWER)
    bio.post(f"/api/biodiversity/checklists/{code}/publish?actor=publisher",
             json={"effective_at": "2026-06-01T00:00:00+00:00"}, headers=PUBLISHER)

    changes = bio.get(f"/api/biodiversity/checklists/{code}/name-changes/2").json()["items"]
    by_taxon = {c["taxon_id"]: c for c in changes}
    assert by_taxon["Sp1"]["change_type"] == "updated"
    assert by_taxon["Sp1"]["to_protection"] == "国家二级"
    assert by_taxon["Sp1"]["reason"] == "专家上调保护等级并扩展城南"
    assert set(by_taxon["Sp1"]["affected_regions"]) == {"north", "south"}
    assert by_taxon["Sp2"]["change_type"] == "added"
    assert by_taxon["Cls"]["change_type"] == "removed"
    assert by_taxon["Cls"]["reason"] == "高阶节点移至通用分册"


def test_as_of_queries_historical_version_even_after_withdrawal(bio):
    code = _create_published_v1(bio)
    # v2
    bio.post(f"/api/biodiversity/checklists/{code}/drafts?actor=editor", json={}, headers=EDITOR)
    _species(bio, code, "Sp2", parent_taxon_id="Ord", reason="v2 新增")
    bio.post(f"/api/biodiversity/checklists/{code}/review-request?actor=editor", json={}, headers=EDITOR)
    bio.post(f"/api/biodiversity/checklists/{code}/review?actor=reviewer",
             json={"decision": "passed"}, headers=REVIEWER)
    bio.post(f"/api/biodiversity/checklists/{code}/publish?actor=publisher",
             json={"effective_at": "2026-08-01T00:00:00+00:00"}, headers=PUBLISHER)

    # 撤回 v2
    resp = bio.post(f"/api/biodiversity/checklists/{code}/versions/2/withdraw?actor=admin",
                    json={"reason": "证据待补"}, headers=ADMIN)
    assert resp.status_code == 200
    # 撤回后 v2 仍可按版本号与历史日期查询（历史不抹除）
    assert bio.get(f"/api/biodiversity/checklists/{code}/versions/2").json()["status"] == "withdrawn"
    asof = bio.get(f"/api/biodiversity/checklists/{code}/as-of?date=2026-09-01T00:00:00%2B00:00").json()
    assert asof["version_no"] == 2 and asof["status"] == "withdrawn"
    # 当前生效指针回退到 v1
    current = bio.get(f"/api/biodiversity/checklists/{code}/species").json()
    assert current["version_no"] == 1
    taxons = {item["taxon_id"] for item in current["items"]}
    assert "Sp2" not in taxons
    # 撤回只阻止后续使用：withdrawn 版本不能作为“当前”被讲解员引用
    checklist = bio.get(f"/api/biodiversity/checklists/{code}").json()
    assert [v for v in checklist["versions"] if v["status"] == "published"][0]["version_no"] == 1


def test_withdraw_requires_reason_and_published_state(bio):
    code = _create_published_v1(bio)
    assert bio.post(f"/api/biodiversity/checklists/{code}/versions/1/withdraw?actor=admin",
                    json={"reason": "   "}, headers=ADMIN).status_code == 422
    # 撤回 v1 后不能重复撤回
    bio.post(f"/api/biodiversity/checklists/{code}/versions/1/withdraw?actor=admin",
             json={"reason": "停用"}, headers=ADMIN)
    resp = bio.post(f"/api/biodiversity/checklists/{code}/versions/1/withdraw?actor=admin",
                    json={"reason": "再次"}, headers=ADMIN)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 别名与分区域可见性
# ---------------------------------------------------------------------------

def test_aliases_and_region_visibility(bio):
    code = _create_published_v1(bio, code="vis")
    # v2：给 Sp1 增加南北区域，并登记敏感种
    bio.post("/api/biodiversity/checklists/vis/drafts?actor=editor", json={}, headers=EDITOR)
    _species(bio, "vis", "Sp1", parent_taxon_id="Ord",
             visible_region_codes=["north", "south"], reason="扩展区域",
             aliases=[{"name": "俗名一", "name_type": "common_name", "source_citation_key": "ref1"}])
    _species(bio, "vis", "Sens", parent_taxon_id="Ord", is_sensitive=True, sensitivity_scope="research",
             visible_region_codes=["north"], reason="敏感繁殖点")
    bio.post("/api/biodiversity/checklists/vis/review-request?actor=editor", json={}, headers=EDITOR)
    bio.post("/api/biodiversity/checklists/vis/review?actor=reviewer",
             json={"decision": "passed"}, headers=REVIEWER)
    bio.post("/api/biodiversity/checklists/vis/publish?actor=publisher",
             json={"effective_at": "2026-03-01T00:00:00+00:00"}, headers=PUBLISHER)

    north = bio.get("/api/biodiversity/checklists/vis/species?region=north&viewer_scope=research").json()
    south = bio.get("/api/biodiversity/checklists/vis/species?region=south").json()
    assert {"Sp1", "Sens"} <= {i["taxon_id"] for i in north["items"]}
    assert {"Sp1"} <= {i["taxon_id"] for i in south["items"]}
    assert "Sens" not in {i["taxon_id"] for i in south["items"]}
    # 同样是城北，public 讲解员看不到敏感种，research 研究员可以
    north_public = bio.get("/api/biodiversity/checklists/vis/species?region=north&viewer_scope=public").json()
    assert "Sens" not in {i["taxon_id"] for i in north_public["items"]}

    # 敏感种对 public 隐藏、对 research 可见
    public = bio.get("/api/biodiversity/checklists/vis/species?viewer_scope=public").json()
    research = bio.get("/api/biodiversity/checklists/vis/species?viewer_scope=research").json()
    assert "Sens" not in {i["taxon_id"] for i in public["items"]}
    assert public["hidden_sensitive_count"] >= 1
    assert "Sens" in {i["taxon_id"] for i in research["items"]}

    sp1 = next(i for i in north["items"] if i["taxon_id"] == "Sp1")
    assert any(a["name"] == "俗名一" for a in sp1["aliases"])

    # 别名引用不存在时被拒
    bio.post("/api/biodiversity/checklists/vis/drafts?actor=editor", json={}, headers=EDITOR)
    resp = bio.post("/api/biodiversity/taxa/Sp1/names?actor=editor",
                    json={"taxon_id": "Sp1", "name": "幽灵异名", "source_citation_key": "nope"},
                    headers=EDITOR)
    assert resp.status_code == 422
