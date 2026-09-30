from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_biodiversity_demo() -> int:
    """多角色（编辑/复核/发布/管理）走查物种清单：草稿→复核→发布→修订→撤回。"""
    steps: list[dict] = []

    def record(title: str, response) -> dict:
        try:
            body = response.json()
        except Exception:
            body = response.text
        step = {"title": title, "status": response.status_code, "body": body}
        steps.append(step)
        return step

    with TestClient(app) as client:
        admin = {"X-Role": "admin"}
        editor = {"X-Role": "editor"}
        reviewer = {"X-Role": "reviewer"}
        publisher = {"X-Role": "publisher"}

        def call(method: str, path: str, actor: str, role_headers: dict | None = None, **kwargs):
            sep = "&" if "?" in path else "?"
            return client.request(method, f"{path}{sep}actor={actor}", headers=role_headers or {}, **kwargs)

        # 0) 基础资料：区域、引用、敏感物种授权
        call("POST", "/api/biodiversity/regions", "管理员老周", admin,
             json={"code": "wetland-north", "name": "城北湿地"})
        call("POST", "/api/biodiversity/regions", "管理员老周", admin,
             json={"code": "wetland-south", "name": "城南湿地"})
        r = call("POST", "/api/biodiversity/citations", "资料员",
                 json={"key": "chen2021", "title": "城市湿地鸟类名录修订", "authors": "陈默 等", "year": 2021})
        record("登记引用 chen2021", r)
        r = call("POST", "/api/biodiversity/citations", "资料员",
                 json={"key": "iucn2023", "title": "IUCN 红色名录 2023-2", "year": 2023})
        record("登记引用 iucn2023", r)
        call("PUT", "/api/biodiversity/sensitive-grants", "管理员老周", admin,
             json={"scope": "research", "region_code": "wetland-north"})
        call("PUT", "/api/biodiversity/sensitive-grants", "管理员老周", admin,
             json={"scope": "research", "region_code": "wetland-south"})

        # 1) 编辑者建立清单与首版草稿
        r = call("POST", "/api/biodiversity/checklists", "编辑小林", editor,
                 json={"code": "city-birds", "title": "城市生境鸟类清单", "description": "自然教育讲解员用"})
        record("编辑建立清单（自动创建首版草稿）", r)

        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-AVES", "scientific_name": "Aves", "canonical_name": "鸟纲",
                 "taxon_rank": "class", "change_reason": "建立高阶分类骨架",
             })
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-ARDEIDAE", "scientific_name": "Ardeidae", "canonical_name": "鹭科",
                 "taxon_rank": "family", "parent_taxon_id": "T-AVES",
                 "change_reason": "建立鹭科分类节点",
             })
        r = call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
                 json={
                     "taxon_id": "T-EGRET", "scientific_name": "Egretta garzetta",
                     "canonical_name": "小白鹭", "parent_taxon_id": "T-ARDEIDAE",
                     "protection_level": "三有", "visible_region_codes": ["wetland-north", "wetland-south"],
                     "citation_refs": ["chen2021"], "occurrence_note": "常见留鸟",
                     "change_reason": "首版收录：城北湿地稳定观测记录",
                     "aliases": [{"name": "白鹭", "name_type": "common_name", "source_citation_key": "chen2021"}],
                 })
        record("收录小白鹭（含别名与引用）", r)

        # 2) 敏感物种：先演示越权发布被拦截
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-RAIL", "scientific_name": "Lewinia striata",
                 "canonical_name": "蓝胸秧鸡", "parent_taxon_id": "T-ARDEIDAE",
                 "protection_level": "省重点", "is_sensitive": True, "sensitivity_scope": "research",
                 "visible_region_codes": ["wetland-east"], "citation_refs": ["chen2021"],
                 "change_reason": "记录疑似繁殖点，限制公开精度",
             })
        r = call("GET", "/api/biodiversity/checklists/city-birds/validation", "编辑小林", editor)
        record("发布前校验（区域不存在+未授权，预期失败）", r)

        # 修正到已授权区域
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-RAIL", "scientific_name": "Lewinia striata",
                 "canonical_name": "蓝胸秧鸡", "parent_taxon_id": "T-ARDEIDAE",
                 "protection_level": "省重点", "is_sensitive": True, "sensitivity_scope": "research",
                 "visible_region_codes": ["wetland-north"], "citation_refs": ["chen2021"],
                 "change_reason": "限制为研究范围可见的城北授权区域",
             })

        # 3) 复核流程：编辑不能自审，复核者先驳回一次（分类链需补正）
        r = call("POST", "/api/biodiversity/checklists/city-birds/review", "编辑小林", editor,
                 json={"decision": "passed", "notes": "自审"})
        record("编辑尝试自审（预期 403）", r)
        call("POST", "/api/biodiversity/checklists/city-birds/review-request", "编辑小林", editor,
             json={"note": "首版请复核"})
        r = call("POST", "/api/biodiversity/checklists/city-birds/review", "复核专家沈老师", reviewer,
                 json={"decision": "rejected", "notes": "鸟纲到鹭科之间缺少鹳形目节点，请补正分类链"})
        record("复核驳回（分类链不完整需补正）", r)
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-CICONIIFORMES", "scientific_name": "Ciconiiformes",
                 "canonical_name": "鹳形目", "taxon_rank": "order", "parent_taxon_id": "T-AVES",
                 "change_reason": "按复核意见补鹳形目节点，闭合分类链",
             })
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-ARDEIDAE", "scientific_name": "Ardeidae", "canonical_name": "鹭科",
                 "taxon_rank": "family", "parent_taxon_id": "T-CICONIIFORMES",
                 "change_reason": "按复核意见将鹭科挂到鹳形目",
             })
        call("POST", "/api/biodiversity/checklists/city-birds/review-request", "编辑小林", editor,
             json={"note": "已补正分类链"})
        r = call("POST", "/api/biodiversity/checklists/city-birds/review", "复核专家沈老师", reviewer,
                 json={"decision": "passed", "notes": "分类链闭合，引用齐备，通过"})
        record("复核通过", r)

        # 4) 发布首版（指定生效时间，便于历史演示）
        v1_effective = "2026-03-01T09:00:00+00:00"
        r = call("POST", "/api/biodiversity/checklists/city-birds/publish", "发布员老韩", publisher,
                 json={"effective_at": v1_effective, "note": "2026 春季讲义版"})
        record("发布 v1（2026 春季讲义版）", r)

        # 5) 公众/讲解员查询：敏感物种对 public 不可见，对 research 可见
        r = call("GET", "/api/biodiversity/checklists/city-birds/species?viewer_scope=public", "讲解员")
        record("讲解员（public）查询当前清单，敏感种被隐藏", r)
        r = call("GET",
                 "/api/biodiversity/checklists/city-birds/species?viewer_scope=research&region=wetland-north",
                 "研究员")
        record("研究员（research）查询城北，可见敏感种", r)

        # 6) 专家修订：保护等级调整 + 引用增补，形成 v2
        call("POST", "/api/biodiversity/checklists/city-birds/drafts", "编辑小林", editor,
             json={"note": "2026 秋季专家修订"})

        # 6a) 演示分类环检测：误把鸟纲父级挂到自己的下级，形成 Aves→Ciconiiformes→Aves 环
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-AVES", "scientific_name": "Aves", "canonical_name": "鸟纲",
                 "taxon_rank": "class", "parent_taxon_id": "T-CICONIIFORMES",
                 "change_reason": "误操作：尝试把鸟纲挂到鹳形目下",
             })
        r = call("GET", "/api/biodiversity/checklists/city-birds/validation", "复核专家沈老师", reviewer)
        record("分类环检测（鸟纲↔鹳形目成环，预期校验失败）", r)
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-AVES", "scientific_name": "Aves", "canonical_name": "鸟纲",
                 "taxon_rank": "class", "parent_taxon_id": "",
                 "change_reason": "修正分类环：鸟纲恢复为顶层分类",
             })

        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-EGRET", "scientific_name": "Egretta garzetta",
                 "canonical_name": "小白鹭", "parent_taxon_id": "T-ARDEIDAE",
                 "protection_level": "三有", "visible_region_codes": ["wetland-north", "wetland-south"],
                 "citation_refs": ["chen2021", "iucn2023"],
                 "occurrence_note": "常见留鸟，2023 评估无危",
                 "change_reason": "按 IUCN2023-2 补证据引用，确认名称维持",
                 "aliases": [{"name": "Egretta nigripes", "name_type": "former_name",
                              "source_citation_key": "iucn2023"}],
             })
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-RAIL", "scientific_name": "Lewinia striata",
                 "canonical_name": "蓝胸秧鸡", "parent_taxon_id": "T-ARDEIDAE",
                 "protection_level": "国家二级", "is_sensitive": True, "sensitivity_scope": "research",
                 "visible_region_codes": ["wetland-north", "wetland-south"],
                 "citation_refs": ["chen2021", "iucn2023"],
                 "change_reason": "保护等级由省重点调整为国家二级；城南取得研究授权后开放",
             })
        # 新增一种后再撤编，演示撤编留痕
        call("PUT", "/api/biodiversity/checklists/city-birds/species", "编辑小林", editor,
             json={
                 "taxon_id": "T-MAGPIE", "scientific_name": "Pica serica", "canonical_name": "东方喜鹊",
                 "parent_taxon_id": "T-AVES", "protection_level": "三有",
                 "visible_region_codes": ["wetland-north"], "citation_refs": ["chen2021"],
                 "change_reason": "秋季补充城区广布种",
             })
        call("POST", "/api/biodiversity/checklists/city-birds/species/T-MAGPIE/remove", "编辑小林", editor,
             json={"reason": "专家复核发现与本湿地主题不符，本版撤编，留待城区分册"})
        call("POST", "/api/biodiversity/checklists/city-birds/review-request", "编辑小林", editor,
             json={"note": "秋季修订请复核"})
        call("POST", "/api/biodiversity/checklists/city-birds/review", "复核专家沈老师", reviewer,
             json={"decision": "passed", "notes": "等级调整有据，同意"})
        v2_effective = "2026-09-15T09:00:00+00:00"
        r = call("POST", "/api/biodiversity/checklists/city-birds/publish", "发布员老韩", publisher,
                 json={"effective_at": v2_effective, "note": "2026 秋季修订版"})
        record("发布 v2（等级调整+引用增补）", r)
        r = call("GET", "/api/biodiversity/checklists/city-birds/name-changes/2", "复核专家沈老师", reviewer)
        record("v2 名称变化与受影响区域", r)

        # 7) 历史版本按日期查询：旧讲义仍可追溯
        r = call("GET",
                 "/api/biodiversity/checklists/city-birds/as-of?date=2026-06-01T00:00:00%2B00:00",
                 "讲解员")
        record("按发布日期追溯（2026-06-01 应得 v1）", r)

        # 8) 撤回 v2：阻止后续使用，但历史不抹除
        r = call("POST", "/api/biodiversity/checklists/city-birds/versions/2/withdraw", "管理员老周", admin,
                 json={"reason": "发现城南观测证据待补，暂停新版使用，讲义暂用 v1"})
        record("撤回 v2（当前生效指针回退 v1，历史保留）", r)
        r = call("GET", "/api/biodiversity/checklists/city-birds/versions/2", "讲解员")
        record("撤回后 v2 仍可按版本号查询", r)
        r = call("GET", "/api/biodiversity/checklists/city-birds/species?viewer_scope=public", "讲解员")
        record("撤回后讲解员查询当前生效版本（回到 v1）", r)
        r = call("GET", "/api/biodiversity/checklists/city-birds/timeline", "管理员老周", admin)
        record("完整事件时间线", r)

    # 断言关键预期，失败则进程非零
    def status_of(title: str) -> int:
        for step in steps:
            if step["title"] == title:
                return step["status"]
        raise AssertionError(title)

    assert status_of("编辑尝试自审（预期 403）") == 403
    validation_step = next(s for s in steps if s["title"].startswith("发布前校验"))
    assert validation_step["body"]["passed"] is False
    cycle_step = next(s for s in steps if s["title"].startswith("分类环检测"))
    taxonomy = next(c for c in cycle_step["body"]["checks"] if c["code"] == "taxonomy_closed")
    assert taxonomy["passed"] is False and any("环" in d["problem"] for d in taxonomy["details"])
    v2_changes = next(s for s in steps if s["title"] == "v2 名称变化与受影响区域")
    change_types = {item["change_type"] for item in v2_changes["body"]["items"]}
    assert "updated" in change_types
    asof = next(s for s in steps if s["title"].startswith("按发布日期追溯"))
    assert asof["body"]["version_no"] == 1

    print(json.dumps({"demo": "biodiversity-checklist", "steps": steps}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("biodiversity-demo", help="执行物种清单多角色编辑/复核/发布/撤回演示")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "biodiversity-demo": command_biodiversity_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
