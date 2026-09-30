"""城市生境物种清单发布服务测试。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.biodiversity.service import override_clock
from app.core.clock import FrozenClock, to_storage, utc_now


PASSWORD = "Biodiv!23456"


@pytest.fixture()
def roles_client(client):
    """返回带 admin/editor/reviewer 三个令牌的客户端与创建用户辅助函数。"""
    bootstrap = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!234567"})
    assert bootstrap.status_code == 201, bootstrap.text

    def make(username: str, role: str) -> dict:
        created = client.post(
            "/api/users",
            headers={"Authorization": f"Bearer {_login(client, 'admin', 'Admin!234567')}"},
            json={"username": username, "password": PASSWORD, "display_name": username,
                  "role_codes": [role]},
        )
        assert created.status_code == 201, created.text
        token = _login(client, username, PASSWORD)
        return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}

    admin_token = _login(client, "admin", "Admin!234567")
    return {
        "client": client,
        "admin": {"token": admin_token, "headers": {"Authorization": f"Bearer {admin_token}"}},
        "editor": make("editor1", "biodiversity_editor"),
        "reviewer": make("reviewer1", "biodiversity_reviewer"),
    }


def _login(client, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _entry(key: str, **overrides) -> dict:
    base = {
        "taxon_key": key,
        "scientific_name": key,
        "parent_key": None,
        "taxon_rank": "种",
        "protection_level": "",
        "sensitive_level": 0,
        "aliases": [],
        "region_codes": [],
        "reference_codes": [],
        "change_type": "新增",
        "change_reason": "首次调查记录",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _reset_clock():
    override_clock(None)
    yield
    override_clock(None)


# --------------------------------------------------------------------- 基础资料

def _seed_foundation(ctx):
    client = ctx["client"]
    admin = ctx["admin"]
    regions = [
        {"code": "WETLAND", "name": "城市湿地", "description": "湿地公园片区"},
        {"code": "HILL", "name": "城北丘陵", "description": "次生林片区"},
    ]
    for region in regions:
        response = client.post("/api/biodiversity/regions", headers=admin["headers"], json=region)
        assert response.status_code == 201, response.text
    for ref in [
        {"code": "REF-FLORA-2025", "kind": "文献", "title": "城市植物名录2025", "authors": "专家组"},
        {"code": "REF-BIRD-IOC", "kind": "数据库", "title": "IOC 世界鸟类名录", "authors": "IOC"},
    ]:
        response = client.post("/api/biodiversity/references", headers=admin["headers"], json=ref)
        assert response.status_code == 201, response.text
    # 湿地允许展示到敏感等级 2，丘陵只允许 1，全局兜底 0。
    for grant in [
        {"region_code": "*", "max_sensitive_level": 0, "note": "默认不公开敏感物种"},
        {"region_code": "WETLAND", "max_sensitive_level": 2, "note": "湿地团队授权"},
        {"region_code": "HILL", "max_sensitive_level": 1, "note": "丘陵团队授权"},
    ]:
        response = client.put("/api/biodiversity/sensitive-grants", headers=admin["headers"], json=grant)
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------- 发布三关

def test_publish_gates_collect_all_violations(roles_client):
    ctx = roles_client
    client, editor = ctx["client"], ctx["editor"]
    _seed_foundation(ctx)

    created = client.post("/api/biodiversity/checklists", headers=editor["headers"],
                          json={"title": "2026 春季清单", "change_summary": "初稿"})
    assert created.status_code == 201, created.text
    checklist_id = created.json()["id"]

    # 两个物种互相把对方设为父分类 → 分类环。
    client.put(f"/api/biodiversity/checklists/{checklist_id}/entries/heron-a",
               headers=editor["headers"],
               json=_entry("heron-a", scientific_name="苍鹭甲", parent_key="heron-b",
                           region_codes=["WETLAND"], reference_codes=["REF-FLORA-2025"]))
    client.put(f"/api/biodiversity/checklists/{checklist_id}/entries/heron-b",
               headers=editor["headers"],
               json=_entry("heron-b", scientific_name="苍鹭乙", parent_key="heron-a",
                           region_codes=["WETLAND"], reference_codes=["REF-NOPE"]))
    # 敏感等级 2 的物种投放到只授权到 1 的丘陵 → 越权。
    client.put(f"/api/biodiversity/checklists/{checklist_id}/entries/orchid-x",
               headers=editor["headers"],
               json=_entry("orchid-x", scientific_name="狭叶兰", sensitive_level=2,
                           region_codes=["HILL"], reference_codes=["REF-FLORA-2025"]))

    report = client.get(f"/api/biodiversity/checklists/{checklist_id}/validation",
                        headers=editor["headers"]).json()
    categories = {item["category"] for item in report["violations"]}
    assert report["ok"] is False
    assert {"分类环", "引用缺失", "敏感物种越权"} <= categories

    # 复核员直接批准也应被同样的阻断项拦下。
    client.post(f"/api/biodiversity/checklists/{checklist_id}/submit",
                headers=editor["headers"], json={"note": "请复核"})
    approved = client.post(f"/api/biodiversity/checklists/{checklist_id}/review",
                           headers=ctx["reviewer"]["headers"], json={"approved": True})
    assert approved.status_code == 422
    assert {item["category"] for item in approved.json()["error"]["context"]["violations"]} >= \
        {"分类环", "引用缺失", "敏感物种越权"}


def test_editor_cannot_review_own_draft_and_anonymous_forbidden(roles_client):
    ctx = roles_client
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    _seed_foundation(ctx)
    cid = client.post("/api/biodiversity/checklists", headers=editor["headers"],
                      json={"title": "职责分离测试"}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{cid}/entries/t1", headers=editor["headers"],
               json=_entry("t1"))
    client.post(f"/api/biodiversity/checklists/{cid}/submit", headers=editor["headers"], json={})

    # 未认证访问被拒
    assert client.get("/api/biodiversity/checklists").status_code == 401
    # 编辑者没有复核权限
    assert client.post(f"/api/biodiversity/checklists/{cid}/review",
                       headers=editor["headers"], json={"approved": True}).status_code == 403

    # 提交者本人即便拥有全部权限（管理员通配）也不能自审：
    # 管理员自建草稿并提交，再尝试自己复核
    own = client.post("/api/biodiversity/checklists", headers=ctx["admin"]["headers"],
                      json={"title": "管理员草稿"}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{own}/entries/t9", headers=ctx["admin"]["headers"],
               json=_entry("t9"))
    client.post(f"/api/biodiversity/checklists/{own}/submit", headers=ctx["admin"]["headers"], json={})
    self_review = client.post(f"/api/biodiversity/checklists/{own}/review",
                              headers=ctx["admin"]["headers"], json={"approved": True})
    assert self_review.status_code == 403

    # 复核员可以驳回，驳回后回到草稿
    rejected = client.post(f"/api/biodiversity/checklists/{cid}/review",
                           headers=reviewer["headers"], json={"approved": False, "comment": "证据不足"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "draft"
    assert rejected.json()["review_conclusion"] == "证据不足"


# --------------------------------------------------------------------- 完整发布流

def _publish_first_version(ctx) -> int:
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    cid = client.post("/api/biodiversity/checklists", headers=editor["headers"],
                      json={"title": "2026 春季清单", "change_summary": "首版"}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{cid}/entries/ardea-purpurea",
               headers=editor["headers"],
               json=_entry("ardea-purpurea", scientific_name="草鹭 Ardea purpurea",
                           aliases=["紫鹭"], region_codes=["WETLAND", "HILL"],
                           reference_codes=["REF-BIRD-IOC"]))
    client.put(f"/api/biodiversity/checklists/{cid}/entries/orchid-x",
               headers=editor["headers"],
               json=_entry("orchid-x", scientific_name="狭叶兰", sensitive_level=1,
                           protection_level="国家二级", region_codes=["WETLAND", "HILL"],
                           reference_codes=["REF-FLORA-2025"]))
    client.post(f"/api/biodiversity/checklists/{cid}/submit", headers=editor["headers"],
                json={"note": "请复核"})
    decision = client.post(f"/api/biodiversity/checklists/{cid}/review",
                           headers=reviewer["headers"], json={"approved": True, "comment": "证据齐备"})
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "approved"
    # 未发布前不能直接把草稿当生效版本
    assert client.get("/api/biodiversity/effective", headers=reviewer["headers"]).status_code == 404
    published = client.post(f"/api/biodiversity/checklists/{cid}/publish",
                            headers=reviewer["headers"], json={"note": "发布首版"})
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    return cid


def test_full_publish_name_changes_and_region_visibility(roles_client):
    ctx = roles_client
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    _seed_foundation(ctx)
    _publish_first_version(ctx)

    # 当前生效版本为 v1
    effective = client.get("/api/biodiversity/effective", headers=editor["headers"]).json()
    assert effective["version_no"] == 1
    assert len(effective["entries"]) == 2

    # 按区域可见性：丘陵能看到两种（授权等级 1，兰花敏感等级 1）
    hill = client.get("/api/biodiversity/effective?region=HILL", headers=editor["headers"]).json()
    assert {entry["taxon_key"] for entry in hill["entries"]} == {"ardea-purpurea", "orchid-x"}

    # 发布后授权收紧（丘陵从等级 1 下调到 0）：历史快照不动，
    # 但后续按区域查询时兰花被敏感授权过滤，并给出 restricted_count
    client.put("/api/biodiversity/sensitive-grants", headers=ctx["admin"]["headers"],
               json={"region_code": "HILL", "max_sensitive_level": 0, "note": "授权收紧"})
    hill_after = client.get("/api/biodiversity/effective?region=HILL", headers=editor["headers"]).json()
    hill_keys = {entry["taxon_key"] for entry in hill_after["entries"]}
    assert hill_keys == {"ardea-purpurea"}
    assert hill_after["restricted_count"] == 1
    # 湿地授权未变，两种仍可见
    wetland = client.get("/api/biodiversity/effective?region=WETLAND", headers=editor["headers"]).json()
    assert {entry["taxon_key"] for entry in wetland["entries"]} == {"ardea-purpurea", "orchid-x"}

    # 名称变更记录：首版两条新增物种
    changes = client.get("/api/biodiversity/versions/1/name-changes", headers=editor["headers"]).json()
    assert {item["change_kind"] for item in changes} == {"新增物种"}
    assert any(item["affected_regions"] == ["WETLAND", "HILL"] for item in changes)

    # 别名检索：用别名“紫鹭”能找到现用学名
    found = client.get("/api/biodiversity/names?q=紫鹭", headers=editor["headers"]).json()
    assert found and found[0]["taxon_key"] == "ardea-purpurea"


def test_second_version_revision_diff_and_as_of(roles_client):
    ctx = roles_client
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    _seed_foundation(ctx)
    _publish_first_version(ctx)

    # 冻结时钟，确保两版发布日期可区分
    first_moment = to_storage(utc_now())
    clock = FrozenClock(datetime.fromisoformat(first_moment))
    override_clock(clock)

    created = client.post("/api/biodiversity/checklists?base_version=1", headers=editor["headers"],
                          json={"title": "2026 秋季清单", "change_summary": "分类修订"})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert len(created.json()["entries"]) == 2  # 基准条目全部拷贝

    # 草鹭：改学名表述、去掉丘陵可见性、新增别名、保护等级升级
    client.put(f"/api/biodiversity/checklists/{cid}/entries/ardea-purpurea",
               headers=editor["headers"],
               json=_entry("ardea-purpurea", scientific_name="草鹭（修订）Ardea purpurea",
                           aliases=["紫鹭", "红庄"], region_codes=["WETLAND"],
                           reference_codes=["REF-BIRD-IOC"], change_type="修订",
                           protection_level="国家二级", change_reason="专家委员会2026年修订"))
    # 兰花本版移除（显式标记）
    client.put(f"/api/biodiversity/checklists/{cid}/entries/orchid-x",
               headers=editor["headers"],
               json=_entry("orchid-x", scientific_name="狭叶兰", sensitive_level=2,
                           protection_level="国家二级", region_codes=["WETLAND"],
                           reference_codes=["REF-FLORA-2025"], change_type="移除",
                           change_reason="误鉴定，本区域无分布"))

    client.post(f"/api/biodiversity/checklists/{cid}/submit", headers=editor["headers"], json={})
    client.post(f"/api/biodiversity/checklists/{cid}/review", headers=reviewer["headers"],
                json={"approved": True})
    clock.advance(days=120)
    second_published_at = to_storage(clock.now())
    published = client.post(f"/api/biodiversity/checklists/{cid}/publish",
                            headers=reviewer["headers"], json={}).json()
    assert published["version_no"] == 2

    changes = client.get("/api/biodiversity/versions/2/name-changes", headers=editor["headers"]).json()
    kinds = {item["change_kind"]: item for item in changes}
    assert "改名" in kinds and kinds["改名"]["reason"] == "专家委员会2026年修订"
    assert kinds["改名"]["affected_regions"] == ["WETLAND"]
    assert "新增别名" in kinds and kinds["新增别名"]["new_name"] == "红庄"
    assert "区域调整" in kinds
    assert set(kinds["区域调整"]["affected_regions"]) == {"HILL"}
    assert "保护等级调整" in kinds
    assert kinds["移除"]["old_name"] == "狭叶兰"
    assert set(kinds["移除"]["affected_regions"]) == {"WETLAND", "HILL"}

    # 按发布日期回溯：首版发布之后、二版发布之前 → v1
    before = client.get("/api/biodiversity/effective", params={"as_of": first_moment},
                        headers=editor["headers"]).json()
    assert before["version_no"] == 1
    after = client.get("/api/biodiversity/effective", params={"as_of": second_published_at},
                       headers=editor["headers"]).json()
    assert after["version_no"] == 2
    assert {entry["taxon_key"] for entry in after["entries"]} == {"ardea-purpurea"}

    # 旧版学名仍是曾用名，可溯源
    history = client.get("/api/biodiversity/taxa/ardea-purpurea", headers=editor["headers"]).json()
    name_kinds = {row["name"]: row["name_kind"] for row in history["names"]}
    assert name_kinds["草鹭 Ardea purpurea"] == "曾用名"
    assert history["published_versions"] == [1, 2]


def test_withdraw_blocks_future_but_keeps_history(roles_client):
    ctx = roles_client
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    _seed_foundation(ctx)
    first_cid = _publish_first_version(ctx)

    clock = FrozenClock(datetime.fromisoformat(to_storage(utc_now())))
    override_clock(clock)
    cid = client.post("/api/biodiversity/checklists?base_version=1", headers=editor["headers"],
                      json={"title": "2026 秋季清单"}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{cid}/entries/ardea-purpurea",
               headers=editor["headers"],
               json=_entry("ardea-purpurea", scientific_name="草鹭 Ardea purpurea",
                           aliases=["紫鹭"], region_codes=["WETLAND", "HILL"],
                           reference_codes=["REF-BIRD-IOC"], change_type="保留"))
    client.put(f"/api/biodiversity/checklists/{cid}/entries/orchid-x",
               headers=editor["headers"],
               json=_entry("orchid-x", scientific_name="狭叶兰", sensitive_level=2,
                           protection_level="国家二级", region_codes=["WETLAND"],
                           reference_codes=["REF-FLORA-2025"], change_type="保留"))
    client.post(f"/api/biodiversity/checklists/{cid}/submit", headers=editor["headers"], json={})
    client.post(f"/api/biodiversity/checklists/{cid}/review", headers=reviewer["headers"],
                json={"approved": True})
    clock.advance(days=30)
    v2_published_at = to_storage(clock.now())
    client.post(f"/api/biodiversity/checklists/{cid}/publish", headers=reviewer["headers"], json={})
    assert client.get("/api/biodiversity/effective", headers=editor["headers"]).json()["version_no"] == 2

    # 旧版本（v1）已被新版本自然取代，不能撤回
    old = client.post(f"/api/biodiversity/checklists/{first_cid}/withdraw",
                      headers=reviewer["headers"], json={"reason": "想撤旧版"})
    assert old.status_code == 409

    # v2 生效一段时间后才撤回（发布时刻与撤回时刻需可区分）
    clock.advance(days=10)

    # 撤回 v2
    withdrawn = client.post(f"/api/biodiversity/checklists/{cid}/withdraw",
                            headers=reviewer["headers"],
                            json={"reason": "发现鉴定错误，暂停使用等待专家复核"})
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "withdrawn"

    # 后续使用自动回落到上一版 v1
    assert client.get("/api/biodiversity/effective", headers=editor["headers"]).json()["version_no"] == 1

    # 撤回后继续推进时间：
    clock.advance(days=5)
    now_after_withdraw = to_storage(clock.now())
    # 查询撤回之后的时刻 → v2 已失效，回落到 v1
    rolled = client.get("/api/biodiversity/effective", params={"as_of": now_after_withdraw},
                        headers=editor["headers"]).json()
    assert rolled["version_no"] == 1
    # 但按 v2 发布当时回溯，历史不变：v2 在其发布时刻确实生效
    historical = client.get("/api/biodiversity/effective", params={"as_of": v2_published_at},
                            headers=editor["headers"]).json()
    assert historical["version_no"] == 2
    snapshot = client.get("/api/biodiversity/versions/2", headers=editor["headers"]).json()
    assert snapshot["status"] == "withdrawn"
    assert snapshot["withdraw_reason"].startswith("发现鉴定错误")
    assert len(snapshot["entries"]) == 2
    # 名称变更轨迹也未被抹去
    assert client.get("/api/biodiversity/versions/2/name-changes",
                      headers=editor["headers"]).status_code == 200

    # 已撤回版本不能再次撤回
    again = client.post(f"/api/biodiversity/checklists/{cid}/withdraw",
                        headers=reviewer["headers"], json={"reason": "再撤一次"})
    assert again.status_code == 409


def _publish_simple_version(ctx, name: str, key: str = "sp-one", **entry_overrides) -> int:
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    cid = client.post("/api/biodiversity/checklists", headers=editor["headers"],
                      json={"title": name}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{cid}/entries/{key}", headers=editor["headers"],
               json=_entry(key, scientific_name=name, **entry_overrides))
    client.post(f"/api/biodiversity/checklists/{cid}/submit", headers=editor["headers"], json={})
    client.post(f"/api/biodiversity/checklists/{cid}/review", headers=reviewer["headers"],
                json={"approved": True})
    client.post(f"/api/biodiversity/checklists/{cid}/publish", headers=reviewer["headers"], json={})
    return cid


def test_scientific_name_roundtrip_a_b_a(roles_client):
    ctx = roles_client
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    _seed_foundation(ctx)
    _publish_simple_version(ctx, "物种 A", reference_codes=["REF-FLORA-2025"])

    # v2: A → B
    cid2 = client.post("/api/biodiversity/checklists?base_version=1", headers=editor["headers"],
                       json={"title": "v2"}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{cid2}/entries/sp-one", headers=editor["headers"],
               json=_entry("sp-one", scientific_name="物种 B", reference_codes=["REF-FLORA-2025"],
                           change_type="修订", change_reason="第一次改名"))
    client.post(f"/api/biodiversity/checklists/{cid2}/submit", headers=editor["headers"], json={})
    client.post(f"/api/biodiversity/checklists/{cid2}/review", headers=reviewer["headers"],
                json={"approved": True})
    client.post(f"/api/biodiversity/checklists/{cid2}/publish", headers=reviewer["headers"], json={})

    # v3: B → A（恢复曾用学名）
    cid3 = client.post("/api/biodiversity/checklists?base_version=2", headers=editor["headers"],
                       json={"title": "v3"}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{cid3}/entries/sp-one", headers=editor["headers"],
               json=_entry("sp-one", scientific_name="物种 A", reference_codes=["REF-FLORA-2025"],
                           change_type="修订", change_reason="恢复最初接受名"))
    client.post(f"/api/biodiversity/checklists/{cid3}/submit", headers=editor["headers"], json={})
    client.post(f"/api/biodiversity/checklists/{cid3}/review", headers=reviewer["headers"],
                json={"approved": True})
    published = client.post(f"/api/biodiversity/checklists/{cid3}/publish",
                            headers=reviewer["headers"], json={})
    assert published.status_code == 200, published.text

    detail = client.get("/api/biodiversity/taxa/sp-one", headers=editor["headers"]).json()
    primary = [n for n in detail["names"] if n["is_primary"]]
    assert len(primary) == 1 and primary[0]["name"] == "物种 A"
    # 历次改名都可追溯
    changes = client.get("/api/biodiversity/effective", headers=editor["headers"]).json()
    assert changes["version_no"] == 3
    renames = client.get("/api/biodiversity/versions/3/name-changes", headers=editor["headers"]).json()
    assert renames[0]["old_name"] == "物种 B" and renames[0]["new_name"] == "物种 A"


def test_removed_species_names_become_former(roles_client):
    ctx = roles_client
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    _seed_foundation(ctx)
    _publish_simple_version(ctx, "待移除物种", key="sp-gone",
                            aliases=["旧别名"], reference_codes=["REF-FLORA-2025"])

    cid = client.post("/api/biodiversity/checklists?base_version=1", headers=editor["headers"],
                      json={"title": "移除版"}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{cid}/entries/sp-gone", headers=editor["headers"],
               json=_entry("sp-gone", scientific_name="待移除物种", aliases=["旧别名"],
                           reference_codes=["REF-FLORA-2025"], change_type="移除",
                           change_reason="本区域无分布记录"))
    client.post(f"/api/biodiversity/checklists/{cid}/submit", headers=editor["headers"], json={})
    client.post(f"/api/biodiversity/checklists/{cid}/review", headers=reviewer["headers"],
                json={"approved": True})
    client.post(f"/api/biodiversity/checklists/{cid}/publish", headers=reviewer["headers"], json={})

    detail = client.get("/api/biodiversity/taxa/sp-gone", headers=editor["headers"]).json()
    assert detail["taxon"]["status"] == "removed"
    assert all(n["name_kind"] == "曾用名" and n["is_primary"] == 0 for n in detail["names"])


def test_approved_checklist_can_be_rejected_back_to_draft(roles_client):
    ctx = roles_client
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    _seed_foundation(ctx)
    cid = client.post("/api/biodiversity/checklists", headers=editor["headers"],
                      json={"title": "发布前反悔"}).json()["id"]
    client.put(f"/api/biodiversity/checklists/{cid}/entries/t1", headers=editor["headers"],
               json=_entry("t1"))
    client.post(f"/api/biodiversity/checklists/{cid}/submit", headers=editor["headers"], json={})
    client.post(f"/api/biodiversity/checklists/{cid}/review", headers=reviewer["headers"],
                json={"approved": True})
    # approved 态不能直接再批准；但发布前可以驳回回草稿
    again = client.post(f"/api/biodiversity/checklists/{cid}/review", headers=reviewer["headers"],
                        json={"approved": True})
    assert again.status_code == 409
    rejected = client.post(f"/api/biodiversity/checklists/{cid}/review", headers=reviewer["headers"],
                           json={"approved": False, "comment": "发布前发现新问题"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "draft"
    # 编辑改完重新走流程
    client.post(f"/api/biodiversity/checklists/{cid}/submit", headers=editor["headers"], json={})
    client.post(f"/api/biodiversity/checklists/{cid}/review", headers=reviewer["headers"],
                json={"approved": True})
    published = client.post(f"/api/biodiversity/checklists/{cid}/publish",
                            headers=reviewer["headers"], json={})
    assert published.status_code == 200


def test_event_stream_records_multi_role_actions(roles_client):
    ctx = roles_client
    client, editor, reviewer = ctx["client"], ctx["editor"], ctx["reviewer"]
    _seed_foundation(ctx)
    cid = _publish_first_version(ctx)
    events = client.get(f"/api/biodiversity/checklists/{cid}/events", headers=editor["headers"]).json()
    actions = [(item["action"], item["actor"]) for item in events]
    assert ("create", "editor1") in actions
    assert ("submit", "editor1") in actions
    assert ("review.approve", "reviewer1") in actions
    assert ("publish", "reviewer1") in actions
