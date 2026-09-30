"""城市生境物种清单发布服务：多角色端到端 API 演示。

运行：
    .venv/bin/python -m app.cli bio-demo

演示使用独立的 SQLite 文件 data/bio-demo.db（每次重建），
通过 FastAPI TestClient 走完整 HTTP 接口，覆盖：
管理员维护区域/文献/敏感授权 → 编辑员起草 → 复核员复核（含发布三关阻断）
→ 发布新版本 → 查看每个名称变化的原因与受影响区域
→ 按发布日期回溯旧版本 → 撤回（阻止后续使用但保留历史）。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient


def _pp(title: str, payload) -> None:
    print(f"\n── {title}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_demo() -> int:
    demo_db = Path(__file__).resolve().parents[1] / "data" / "bio-demo.db"
    demo_db.parent.mkdir(parents=True, exist_ok=True)
    demo_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(demo_db) + suffix).unlink(missing_ok=True)
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(demo_db)

    from app.database import close_connection
    close_connection()
    from app.main import app

    # 用冻结时钟拉开真实的发布日期：v1 在春季、v2 在秋季，便于演示“按发布日期回溯”。
    from app.biodiversity.service import override_clock
    from app.core.clock import FrozenClock
    clock = FrozenClock(datetime(2026, 3, 20, 9, 0, tzinfo=UTC))
    override_clock(clock)

    with TestClient(app) as client:
        # ── 0. 三个角色：管理员 / 编辑员 / 复核员 ─────────────────────────────
        client.post("/api/auth/bootstrap", json={"username": "admin", "password": "Admin!234567"})

        def login(username: str, password: str) -> dict:
            token = client.post("/api/auth/login",
                                json={"username": username, "password": password}).json()["token"]
            return {"Authorization": f"Bearer {token}"}

        admin = login("admin", "Admin!234567")

        def make_user(username: str, role: str) -> dict:
            result = client.post("/api/users", headers=admin, json={
                "username": username, "password": "Biodiv!23456",
                "display_name": {"editor": "李编辑", "reviewer": "王复核"}[username],
                "role_codes": [role],
            })
            assert result.status_code == 201, result.text
            return login(username, "Biodiv!23456")

        editor = make_user("editor", "biodiversity_editor")
        reviewer = make_user("reviewer", "biodiversity_reviewer")
        print("角色就绪：admin（管理员）、editor（李编辑）、reviewer（王复核）")

        # ── 1. 管理员维护基础资料与敏感授权 ───────────────────────────────────
        for region in [
            {"code": "WETLAND", "name": "城市湿地", "description": "湿地公园巡护片区"},
            {"code": "HILL", "name": "城北丘陵", "description": "次生林巡护片区"},
        ]:
            client.post("/api/biodiversity/regions", headers=admin, json=region)
        for ref in [
            {"code": "REF-FLORA-2025", "kind": "文献", "title": "城市植物名录(2025)", "authors": "市植物学会"},
            {"code": "REF-BIRD-IOC-141", "kind": "数据库", "title": "IOC 世界鸟类名录 v14.1", "authors": "IOC"},
            {"code": "REF-HERON-SURVEY", "kind": "标本", "title": "湿地鹭科鸟类调查标本记录", "authors": "湿地站"},
        ]:
            client.post("/api/biodiversity/references", headers=admin, json=ref)
        # 敏感授权：全局 0（默认不公开），湿地到 2，丘陵到 1
        for grant in [
            {"region_code": "*", "max_sensitive_level": 0, "note": "默认不公开受胁物种位点"},
            {"region_code": "WETLAND", "max_sensitive_level": 2, "note": "湿地团队培训授权"},
            {"region_code": "HILL", "max_sensitive_level": 1, "note": "丘陵团队培训授权"},
        ]:
            client.put("/api/biodiversity/sensitive-grants", headers=admin, json=grant)
        print("基础资料就绪：2 个区域、3 条引用证据、3 条敏感授权")

        def entry(key, **over):
            base = {
                "taxon_key": key, "scientific_name": key, "taxon_rank": "种",
                "parent_key": None, "protection_level": "", "sensitive_level": 0,
                "aliases": [], "region_codes": [], "reference_codes": [],
                "change_type": "新增", "change_reason": "",
            }
            base.update(over)
            return base

        # ── 2. 编辑员起草 v1 ─────────────────────────────────────────────────
        c1 = client.post("/api/biodiversity/checklists", headers=editor,
                         json={"title": "2026 春季自然教育讲解清单", "change_summary": "首版"}).json()
        cid1 = c1["id"]
        client.put(f"/api/biodiversity/checklists/{cid1}/entries/ardea-purpurea", headers=editor, json=entry(
            "ardea-purpurea", scientific_name="草鹭 Ardea purpurea",
            aliases=["紫鹭"], region_codes=["WETLAND", "HILL"],
            reference_codes=["REF-BIRD-IOC-141", "REF-HERON-SURVEY"],
            change_reason="2026 春季湿地调查稳定记录"))
        client.put(f"/api/biodiversity/checklists/{cid1}/entries/ixobrychus-minutus", headers=editor, json=entry(
            "ixobrychus-minutus", scientific_name="小苇鳽 Ixobrychus minutus",
            aliases=["小苇鸻"], protection_level="国家二级", sensitive_level=1,
            region_codes=["WETLAND"], reference_codes=["REF-HERON-SURVEY"],
            change_reason="繁殖羽影像证据确证"))
        print(f"\n编辑员完成草稿 #{cid1}（2 个物种）并提交复核")
        client.post(f"/api/biodiversity/checklists/{cid1}/submit", headers=editor,
                    json={"note": "证据已附，请复核"})

        # ── 3. 复核员复核并发布 v1 ───────────────────────────────────────────
        report = client.get(f"/api/biodiversity/checklists/{cid1}/validation", headers=reviewer).json()
        print(f"发布前三关校验（分类环/引用/敏感授权）：{'通过' if report['ok'] else '存在阻断'}")
        client.post(f"/api/biodiversity/checklists/{cid1}/review", headers=reviewer,
                    json={"approved": True, "comment": "证据齐备，同意发布"})
        published1 = client.post(f"/api/biodiversity/checklists/{cid1}/publish", headers=reviewer,
                                 json={"note": "春季讲义开印"}).json()
        print(f"v{published1['version_no']} 已发布，发布时间 {published1['published_at']}")
        _pp("v1 每个名称的变化（原因 + 受影响区域）",
            client.get("/api/biodiversity/versions/1/name-changes", headers=reviewer).json())

        # ── 4. 编辑员起草 v2：专家修订名称与分级 ─────────────────────────────
        clock.advance(days=180)  # 时间推进到 2026 年 9 月（秋季）
        c2 = client.post("/api/biodiversity/checklists?base_version=1", headers=editor,
                         json={"title": "2026 秋季自然教育讲解清单", "change_summary": "专家修订"}).json()
        cid2 = c2["id"]
        # 草鹭：学名修订、新增别名、保护等级升级、丘陵区域下线
        client.put(f"/api/biodiversity/checklists/{cid2}/entries/ardea-purpurea", headers=editor, json=entry(
            "ardea-purpurea", scientific_name="草鹭 Ardea purpurea（东方种群）",
            aliases=["紫鹭", "红庄"], protection_level="国家二级", region_codes=["WETLAND"],
            reference_codes=["REF-BIRD-IOC-141", "REF-HERON-SURVEY"],
            change_type="修订", change_reason="专家委员会2026年9月修订：独立东方种群表述"))
        # 小苇鳽：敏感等级升至 2，但仍投放在只授权到 1 的丘陵 → 故意制造越权；
        # 同时引用一条尚未登记的证据 → 故意制造引用缺失
        client.put(f"/api/biodiversity/checklists/{cid2}/entries/ixobrychus-minutus", headers=editor, json=entry(
            "ixobrychus-minutus", scientific_name="小苇鳽 Ixobrychus minutus",
            aliases=["小苇鸻"], protection_level="国家二级", sensitive_level=2,
            region_codes=["WETLAND", "HILL"], reference_codes=["REF-HERON-SURVEY", "REF-PENDING-2026"],
            change_type="修订", change_reason="位点受干扰加剧，提升敏感管控"))
        client.post(f"/api/biodiversity/checklists/{cid2}/submit", headers=editor,
                    json={"note": "秋季修订，请复核"})

        # ── 5. 复核员在复核环节拦下两类阻断 ──────────────────────────────────
        blocked = client.post(f"/api/biodiversity/checklists/{cid2}/review", headers=reviewer,
                              json={"approved": True, "comment": "先过校验"})
        print(f"\n复核员尝试通过 v2：HTTP {blocked.status_code}（发布阻断）")
        _pp("阻断项（一次性聚合：引用缺失 + 敏感物种越权）",
            blocked.json()["error"]["context"]["violations"])

        # 编辑员补登记证据；管理员为丘陵追加敏感等级 2 的授权
        client.post("/api/biodiversity/references", headers=admin, json={
            "code": "REF-PENDING-2026", "kind": "专家意见",
            "title": "小苇鳽种群评估专家意见(2026)", "authors": "市鸟类专家组"})
        client.put("/api/biodiversity/sensitive-grants", headers=admin,
                   json={"region_code": "HILL", "max_sensitive_level": 2,
                         "note": "秋季小苇鳽巡护培训专项授权"})
        # 复核驳回 → 编辑修改后重新提交（草稿仍是同一份）
        client.post(f"/api/biodiversity/checklists/{cid2}/review", headers=reviewer,
                    json={"approved": False, "comment": "请补证据并完成敏感授权后重新提交"})
        client.post(f"/api/biodiversity/checklists/{cid2}/submit", headers=editor,
                    json={"note": "证据与授权已补齐"})
        client.post(f"/api/biodiversity/checklists/{cid2}/review", headers=reviewer,
                    json={"approved": True, "comment": "复核通过"})
        published2 = client.post(f"/api/biodiversity/checklists/{cid2}/publish", headers=reviewer,
                                 json={"note": "秋季讲义开印"}).json()
        print(f"v{published2['version_no']} 已发布")
        _pp("v2 名称变化（可见每条变化的原因与受影响区域）",
            client.get("/api/biodiversity/versions/2/name-changes", headers=editor).json())

        # ── 6. 分区域可见性 ──────────────────────────────────────────────────
        _pp("讲解员视角 · 当前生效版本（丘陵区域，敏感授权过滤后）",
            client.get("/api/biodiversity/effective", params={"region": "HILL"},
                       headers=editor).json())

        # ── 7. 按发布日期查询旧版本；别名/曾用名溯源 ───────────────────────────
        v1_published_at = published1["published_at"]
        _pp(f"按发布日期回溯（as_of={v1_published_at}）→ 仍是旧讲义引用的 v1",
            client.get("/api/biodiversity/effective", params={"as_of": v1_published_at},
                       headers=editor).json())
        _pp("名称溯源：检索旧讲义上的别名“小苇鸻”",
            client.get("/api/biodiversity/names", params={"q": "小苇鸻"}, headers=editor).json())

        # ── 8. 撤回 v2：阻止后续使用，但不抹去历史 ───────────────────────────
        clock.advance(days=7)  # 讲义开印一周后发现问题
        withdrawn = client.post(f"/api/biodiversity/checklists/{cid2}/withdraw", headers=reviewer,
                                json={"reason": "发现小苇鳽鉴定存疑，暂停秋季讲义使用，待专家复核"}).json()
        current = client.get("/api/biodiversity/effective", headers=editor).json()
        print(f"\nv2 已撤回（{withdrawn['withdraw_reason']}）；后续使用自动回落：当前生效版本 = "
              f"v{current['version_no']}")
        history = client.get("/api/biodiversity/versions/2", headers=editor).json()
        print(f"但 v2 历史快照仍可按版本号查询：status={history['status']}，条目数={len(history['entries'])}，"
              f"撤回原因保留：{history['withdraw_reason']}")

    override_clock(None)
    close_connection()
    print("\n演示完成。")
    return 0
