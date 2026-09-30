"""物种清单发布服务核心逻辑。

生命周期：draft（草稿）→ in_review（复核中）→ approved（复核通过）
→ published（已发布）；复核驳回回到 draft；当前生效版本可 withdraw（撤回）。

设计要点：
- 发布是唯一的落版时刻：草稿在 bio_draft_entries，发布时整体快照到
  bio_published_entries。快照不可变，撤回也不删除，历史讲义永久可追溯；
  撤回后上一版自动恢复为生效版本（回滚而非留空）。
- 发布前三道校验：分类环闭合、引用存在、敏感物种不超区域授权，
  违例一次性聚合并以 422 返回，便于编辑者一次修完。
- 每个名称的变化在发布时与上一版本逐项 diff，写入 bio_name_changes，
  记录变化原因与受影响区域。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from app.biodiversity import store
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction

LIST_JSON_FIELDS = ("aliases", "region_codes", "reference_codes")

# 演示与测试可注入时钟（需要让两次发布落在不同日期）。
_clock_override: Clock | None = None


def override_clock(clock: Clock | None) -> None:
    global _clock_override
    _clock_override = clock


def _clock() -> Clock:
    return _clock_override or SystemClock()


def _now() -> str:
    return to_storage(_clock().now())


def _loads(value: str) -> list[str]:
    return json.loads(value or "[]")


def entry_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for field in LIST_JSON_FIELDS:
        data[field] = _loads(data.pop(f"{field}_json", "[]"))
    return data


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["detail"] = json.loads(data.pop("detail_json", "{}"))
    return data


def _change_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["affected_regions"] = _loads(data.pop("affected_regions_json", "[]"))
    return data


class BiodiversityService:
    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        store.ensure_schema(self.connection)

    # ------------------------------------------------------------------ 基础资料

    def create_region(self, principal: Principal, data: dict) -> dict:
        principal.require("biodiversity.manage")
        with transaction(immediate=True) as connection:
            if store.get_region(connection, data["code"]):
                raise ConflictError("区域代码已存在")
            now = _now()
            connection.execute(
                "INSERT INTO bio_regions(code,name,description,created_at,updated_at) VALUES(?,?,?,?,?)",
                (data["code"], data["name"].strip(), data.get("description", ""), now, now),
            )
            return dict(store.get_region(connection, data["code"]))

    def list_regions(self, principal: Principal, active_only: bool = False) -> list[dict]:
        principal.require("biodiversity.read")
        return [dict(row) for row in store.list_regions(self.connection, active_only=active_only)]

    def create_reference(self, principal: Principal, data: dict) -> dict:
        principal.require("biodiversity.manage")
        with transaction(immediate=True) as connection:
            if store.get_reference(connection, data["code"]):
                raise ConflictError("引用代码已存在")
            now = _now()
            connection.execute(
                "INSERT INTO bio_references(code,kind,title,authors,published_on,url,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (data["code"], data["kind"], data["title"].strip(), data.get("authors", ""),
                 data.get("published_on", ""), data.get("url", ""), now, now),
            )
            return dict(store.get_reference(connection, data["code"]))

    def list_references(self, principal: Principal) -> list[dict]:
        principal.require("biodiversity.read")
        return [dict(row) for row in store.list_references(self.connection)]

    def set_grant(self, principal: Principal, data: dict) -> dict:
        principal.require("biodiversity.manage")
        region_code = data["region_code"]
        with transaction(immediate=True) as connection:
            if region_code != "*" and not store.get_region(connection, region_code):
                raise NotFoundError("区域不存在，无法授权；'*' 代表全局兜底授权")
            now = _now()
            if connection.execute(
                "SELECT id FROM bio_sensitive_grants WHERE region_code=?", (region_code,)
            ).fetchone():
                connection.execute(
                    "UPDATE bio_sensitive_grants SET max_sensitive_level=?,granted_by=?,note=?,updated_at=? "
                    "WHERE region_code=?",
                    (data["max_sensitive_level"], principal.username, data.get("note", ""), now, region_code),
                )
            else:
                connection.execute(
                    "INSERT INTO bio_sensitive_grants(region_code,max_sensitive_level,granted_by,note,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (region_code, data["max_sensitive_level"], principal.username, data.get("note", ""), now, now),
                )
            return dict(connection.execute(
                "SELECT * FROM bio_sensitive_grants WHERE region_code=?", (region_code,)
            ).fetchone())

    def list_grants(self, principal: Principal) -> list[dict]:
        principal.require("biodiversity.read")
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM bio_sensitive_grants ORDER BY region_code").fetchall()]

    # ------------------------------------------------------------------ 草稿编辑

    def create_checklist(self, principal: Principal, data: dict, base_version: int | None = None) -> dict:
        principal.require("biodiversity.edit")
        with transaction(immediate=True) as connection:
            now = _now()
            version_no = store.latest_version_no(connection) + 1
            cursor = connection.execute(
                "INSERT INTO bio_checklists(version_no,status,title,change_summary,created_by,created_at,updated_at) "
                "VALUES(?, 'draft',?,?,?,?,?)",
                (version_no, data["title"].strip(), data.get("change_summary", ""),
                 principal.username, now, now),
            )
            checklist_id = int(cursor.lastrowid)
            seeded: list[str] = []
            if base_version is not None:
                base = store.get_checklist_by_version(connection, base_version)
                if base is None or base["published_at"] is None:
                    raise NotFoundError("基准版本不存在或尚未发布过")
                for row in store.published_entries(connection, base_version):
                    entry = entry_dict(row)
                    assert entry is not None
                    connection.execute(
                        "INSERT INTO bio_draft_entries(checklist_id,taxon_key,scientific_name,parent_key,taxon_rank,"
                        "protection_level,sensitive_level,aliases_json,region_codes_json,reference_codes_json,"
                        "change_type,change_reason,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?, '保留','',?)",
                        (checklist_id, entry["taxon_key"], entry["scientific_name"], entry["parent_key"],
                         entry["taxon_rank"], entry["protection_level"], entry["sensitive_level"],
                         json.dumps(entry["aliases"], ensure_ascii=False),
                         json.dumps(entry["region_codes"], ensure_ascii=False),
                         json.dumps(entry["reference_codes"], ensure_ascii=False), principal.username),
                    )
                    seeded.append(entry["taxon_key"])
            self._event(connection, checklist_id, version_no, "create", principal,
                        {"base_version": base_version, "seeded": seeded})
        return self.checklist_detail(principal, checklist_id)

    def update_checklist_meta(self, principal: Principal, checklist_id: int, changes: dict) -> dict:
        principal.require("biodiversity.edit")
        with transaction(immediate=True) as connection:
            checklist = self._require_checklist(connection, checklist_id)
            self._require_state(checklist, {"draft"})
            fields = {key: value for key, value in changes.items()
                      if key in {"title", "change_summary"} and value is not None}
            if not fields:
                raise ValidationError("没有可更新的清单字段")
            assignments = ", ".join(f"{key}=?" for key in fields)
            connection.execute(
                f"UPDATE bio_checklists SET {assignments}, updated_at=? WHERE id=?",
                (*fields.values(), _now(), checklist_id),
            )
        return self.checklist_detail(principal, checklist_id)

    def upsert_entry(self, principal: Principal, checklist_id: int, data: dict) -> dict:
        principal.require("biodiversity.edit")
        with transaction(immediate=True) as connection:
            checklist = self._require_checklist(connection, checklist_id)
            self._require_state(checklist, {"draft"})
            existing = store.get_draft_entry(connection, checklist_id, data["taxon_key"])
            now = _now()
            params = (
                data["scientific_name"], data.get("parent_key"), data["taxon_rank"],
                data.get("protection_level", ""), int(data.get("sensitive_level", 0)),
                json.dumps(data.get("aliases", []), ensure_ascii=False),
                json.dumps(data.get("region_codes", []), ensure_ascii=False),
                json.dumps(data.get("reference_codes", []), ensure_ascii=False),
                data.get("change_type", "修订"), data.get("change_reason", ""), principal.username,
            )
            if existing:
                connection.execute(
                    "UPDATE bio_draft_entries SET scientific_name=?,parent_key=?,taxon_rank=?,"
                    "protection_level=?,sensitive_level=?,aliases_json=?,region_codes_json=?,"
                    "reference_codes_json=?,change_type=?,change_reason=?,updated_by=? "
                    "WHERE checklist_id=? AND taxon_key=?",
                    (*params, checklist_id, data["taxon_key"]),
                )
            else:
                connection.execute(
                    "INSERT INTO bio_draft_entries(checklist_id,taxon_key,scientific_name,parent_key,taxon_rank,"
                    "protection_level,sensitive_level,aliases_json,region_codes_json,reference_codes_json,"
                    "change_type,change_reason,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (checklist_id, data["taxon_key"], *params),
                )
            connection.execute("UPDATE bio_checklists SET updated_at=? WHERE id=?", (now, checklist_id))
            self._event(connection, checklist_id, checklist["version_no"], "entry.upsert", principal,
                        {"taxon_key": data["taxon_key"], "change_type": data.get("change_type", "修订"),
                         "reason": data.get("change_reason", "")})
            result = entry_dict(store.get_draft_entry(connection, checklist_id, data["taxon_key"]))
        assert result is not None
        return result

    def remove_entry(self, principal: Principal, checklist_id: int, taxon_key: str) -> dict:
        principal.require("biodiversity.edit")
        with transaction(immediate=True) as connection:
            checklist = self._require_checklist(connection, checklist_id)
            self._require_state(checklist, {"draft"})
            if not store.get_draft_entry(connection, checklist_id, taxon_key):
                raise NotFoundError("草稿中没有该物种条目")
            connection.execute(
                "DELETE FROM bio_draft_entries WHERE checklist_id=? AND taxon_key=?", (checklist_id, taxon_key)
            )
            self._event(connection, checklist_id, checklist["version_no"], "entry.remove", principal,
                        {"taxon_key": taxon_key})
        return {"deleted": taxon_key, "checklist_id": checklist_id}

    def submit(self, principal: Principal, checklist_id: int, note: str) -> dict:
        principal.require("biodiversity.edit")
        with transaction(immediate=True) as connection:
            checklist = self._require_checklist(connection, checklist_id)
            self._require_state(checklist, {"draft"})
            if not store.draft_entries(connection, checklist_id):
                raise ValidationError("空清单不能提交复核")
            now = _now()
            connection.execute(
                "UPDATE bio_checklists SET status='in_review',submitted_by=?,submitted_at=?,"
                "review_conclusion='',reviewed_by='',reviewed_at=?,updated_at=? WHERE id=?",
                (principal.username, now, now, now, checklist_id),
            )
            self._event(connection, checklist_id, checklist["version_no"], "submit", principal, {"note": note})
        return self.checklist_detail(principal, checklist_id)

    def review(self, principal: Principal, checklist_id: int, approved: bool, comment: str) -> dict:
        principal.require("biodiversity.review")
        with transaction(immediate=True) as connection:
            checklist = self._require_checklist(connection, checklist_id)
            # in_review：首次复核；approved：发布前复核员可再次驳回退回草稿。
            self._require_state(checklist, {"in_review", "approved"})
            # 职责分离：提交者不能复核自己的草稿（管理员通配权限也不豁免此工作流约束）。
            if checklist["submitted_by"] == principal.username:
                raise PermissionDeniedError("提交者不能复核自己的清单草稿")
            now = _now()
            if approved:
                if checklist["status"] == "approved":
                    raise ConflictError("该版本已经复核通过，可直接发布或驳回")
                violations = self._collect_violations(connection, checklist_id)
                if violations:
                    raise ValidationError("复核未通过：存在必须修复的发布阻断项",
                                          context={"violations": violations})
                connection.execute(
                    "UPDATE bio_checklists SET status='approved',review_conclusion=?,reviewed_by=?,"
                    "reviewed_at=?,updated_at=? WHERE id=?",
                    (comment or "复核通过", principal.username, now, now, checklist_id),
                )
                action, conclusion = "review.approve", comment or "复核通过"
            else:
                connection.execute(
                    "UPDATE bio_checklists SET status='draft',review_conclusion=?,reviewed_by=?,"
                    "reviewed_at=?,updated_at=? WHERE id=?",
                    (comment or "复核驳回", principal.username, now, now, checklist_id),
                )
                action, conclusion = "review.reject", comment or "复核驳回"
            self._event(connection, checklist_id, checklist["version_no"], action, principal,
                        {"comment": conclusion})
        return self.checklist_detail(principal, checklist_id)

    # ------------------------------------------------------------------ 发布/撤回

    def publish(self, principal: Principal, checklist_id: int, note: str) -> dict:
        principal.require("biodiversity.publish")
        with transaction(immediate=True) as connection:
            checklist = self._require_checklist(connection, checklist_id)
            self._require_state(checklist, {"approved"})
            violations = self._collect_violations(connection, checklist_id)
            if violations:
                raise ValidationError("发布被阻断", context={"violations": violations})

            version_no = checklist["version_no"]
            now = _now()
            entries = [entry_dict(row) for row in store.draft_entries(connection, checklist_id)]
            prev_version = connection.execute(
                "SELECT MAX(version_no) AS m FROM bio_checklists WHERE published_at IS NOT NULL "
                "AND version_no < ?",
                (version_no,),
            ).fetchone()["m"]
            prev_rows = store.published_entries(connection, int(prev_version)) if prev_version else []
            prev_map = {row["taxon_key"]: entry_dict(row) for row in prev_rows}

            connection.execute(
                "UPDATE bio_checklists SET status='published',published_at=?,updated_at=? WHERE id=?",
                (now, now, checklist_id),
            )
            changes: list[dict] = []
            for entry in entries:
                if entry["change_type"] == "移除":
                    prev = prev_map.get(entry["taxon_key"])
                    if prev is None:
                        raise ValidationError(
                            f"条目 {entry['taxon_key']} 标记为移除，但上一版本中不存在"
                        )
                    changes.append(self._remove_taxon(connection, entry, prev, version_no, now))
                    continue
                if entry["change_type"] == "新增" and entry["taxon_key"] in prev_map:
                    raise ValidationError(f"条目 {entry['taxon_key']} 已存在于上一版本，不能标记为新增")
                connection.execute(
                    "INSERT INTO bio_published_entries(checklist_id,version_no,taxon_key,scientific_name,"
                    "parent_key,taxon_rank,protection_level,sensitive_level,aliases_json,region_codes_json,"
                    "reference_codes_json,change_type,change_reason,published_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (checklist_id, version_no, entry["taxon_key"], entry["scientific_name"], entry["parent_key"],
                     entry["taxon_rank"], entry["protection_level"], entry["sensitive_level"],
                     json.dumps(entry["aliases"], ensure_ascii=False),
                     json.dumps(entry["region_codes"], ensure_ascii=False),
                     json.dumps(entry["reference_codes"], ensure_ascii=False),
                     entry["change_type"], entry["change_reason"], now),
                )
                self._upsert_taxon_master(connection, entry, version_no, now)
                changes.extend(self._diff_entry(connection, entry, prev_map.get(entry["taxon_key"]), version_no, now))
            # 上一版有、本版草稿没有且未显式标记移除的条目不会被静默删除，
            # 避免编辑者漏拷贝基准版本造成误删。
            for change in changes:
                connection.execute(
                    "INSERT INTO bio_name_changes(version_no,taxon_key,change_kind,old_name,new_name,reason,"
                    "affected_regions_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version_no, change["taxon_key"], change["change_kind"], change["old_name"],
                     change["new_name"], change["reason"],
                     json.dumps(change["affected_regions"], ensure_ascii=False), now),
                )
            self._event(connection, checklist_id, version_no, "publish", principal,
                        {"note": note, "changes": len(changes)})
        result = self.checklist_detail(principal, checklist_id)
        result["changes"] = [_change_dict(row) for row in store.name_changes(self.connection, version_no)]
        return result

    def withdraw(self, principal: Principal, checklist_id: int, reason: str) -> dict:
        principal.require("biodiversity.publish")
        with transaction(immediate=True) as connection:
            checklist = self._require_checklist(connection, checklist_id)
            self._require_state(checklist, {"published"})
            # 撤回针对当前生效版本；旧版本已被新版本自然取代，不存在“撤回”一说。
            latest_published = connection.execute(
                "SELECT MAX(version_no) AS m FROM bio_checklists WHERE status='published'"
            ).fetchone()["m"]
            if latest_published != checklist["version_no"]:
                raise ConflictError("只能撤回当前最新生效的清单版本")
            now = _now()
            connection.execute(
                "UPDATE bio_checklists SET status='withdrawn',withdrawn_at=?,withdrawn_by=?,withdraw_reason=?,"
                "updated_at=? WHERE id=?",
                (now, principal.username, reason, now, checklist_id),
            )
            # 不删除 bio_published_entries / bio_name_changes：
            # 撤回只阻止后续使用，已引用的历史必须保留；上一已发布版本自动恢复生效。
            self._event(connection, checklist_id, checklist["version_no"], "withdraw", principal,
                        {"reason": reason})
        return self.checklist_detail(principal, checklist_id)

    # ------------------------------------------------------------------ 发布校验

    def validate_checklist(self, checklist_id: int) -> list[dict]:
        return self._collect_violations(self.connection, checklist_id)

    def _collect_violations(self, connection: sqlite3.Connection, checklist_id: int) -> list[dict]:
        """聚合三道发布关的全部违例；空列表表示通过。"""
        self._require_checklist(connection, checklist_id)
        entries = [entry_dict(row) for row in store.draft_entries(connection, checklist_id)]
        violations: list[dict] = []
        violations.extend(self._violations_taxonomy(connection, entries))
        violations.extend(self._violations_references(connection, entries))
        violations.extend(self._violations_sensitive(connection, entries))
        return violations

    def _violations_taxonomy(self, connection: sqlite3.Connection, entries: list[dict]) -> list[dict]:
        violations: list[dict] = []
        draft_parent = {entry["taxon_key"]: entry.get("parent_key") for entry in entries}
        master = {
            row["taxon_key"]: row["parent_key"]
            for row in connection.execute("SELECT taxon_key,parent_key FROM bio_taxa").fetchall()
        }
        keys = set(draft_parent)

        def resolve_parent(node: str) -> str | None:
            if node in draft_parent:
                return draft_parent[node]
            return master.get(node)

        for key, parent in draft_parent.items():
            if not parent:
                continue
            if parent == key:
                violations.append({"category": "分类环", "taxon_key": key,
                                   "detail": "物种的父分类不能指向自身"})
            elif parent not in keys and parent not in master:
                violations.append({"category": "分类关系", "taxon_key": key,
                                   "detail": f"父分类 {parent} 不存在于草稿或既有分类中"})

        # 环检测：沿 parent 边 DFS，遇到灰色祖先即成环。
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {}
        seen_cycles: set[frozenset[str]] = set()

        def visit(node: str, stack: list[str]) -> None:
            color[node] = GRAY
            stack.append(node)
            parent = resolve_parent(node)
            if parent and (parent in draft_parent or parent in master):
                state = color.get(parent, WHITE)
                if state == GRAY:
                    start = stack.index(parent)
                    cycle = stack[start:] + [parent]
                    marker = frozenset(cycle)
                    if marker not in seen_cycles:
                        seen_cycles.add(marker)
                        violations.append({
                            "category": "分类环", "taxon_key": parent,
                            "detail": "分类关系形成闭环：" + " → ".join(cycle),
                        })
                elif state == WHITE:
                    visit(parent, stack)
            color[node] = BLACK
            stack.pop()

        for key in draft_parent:
            if color.get(key, WHITE) == WHITE:
                visit(key, [])
        return violations

    def _violations_references(self, connection: sqlite3.Connection, entries: list[dict]) -> list[dict]:
        existing = {row["code"] for row in store.list_references(connection)}
        violations: list[dict] = []
        for entry in entries:
            if entry["change_type"] == "移除":
                continue
            for code in entry["reference_codes"]:
                if code not in existing:
                    violations.append({
                        "category": "引用缺失", "taxon_key": entry["taxon_key"],
                        "reference_code": code,
                        "detail": f"引用 {code} 不存在，证据必须先登记",
                    })
        return violations

    def _violations_sensitive(self, connection: sqlite3.Connection, entries: list[dict]) -> list[dict]:
        grants = store.grant_map(connection)
        region_codes = {row["code"] for row in store.list_regions(connection)}
        default_level = grants.get("*", 0)
        violations: list[dict] = []
        for entry in entries:
            if entry["change_type"] == "移除":
                continue
            level = int(entry["sensitive_level"])
            if level == 0:
                continue
            targets = entry["region_codes"] or ["*"]
            for region_code in targets:
                if region_code != "*" and region_code not in region_codes:
                    violations.append({
                        "category": "区域无效", "taxon_key": entry["taxon_key"],
                        "region_code": region_code, "detail": "可见区域不存在，需先登记区域",
                    })
                    continue
                allowed = 0 if region_code == "*" else grants.get(region_code, default_level)
                if level > allowed:
                    violations.append({
                        "category": "敏感物种越权", "taxon_key": entry["taxon_key"],
                        "region_code": region_code, "sensitive_level": level,
                        "authorized_level": allowed,
                        "detail": f"敏感等级 {level} 超出区域 {region_code} 的授权等级 {allowed}",
                    })
        return violations

    # ------------------------------------------------------------------ 主档与名称

    def _upsert_taxon_master(self, connection: sqlite3.Connection, entry: dict, version_no: int, now: str) -> None:
        if connection.execute("SELECT 1 FROM bio_taxa WHERE taxon_key=?", (entry["taxon_key"],)).fetchone():
            connection.execute(
                "UPDATE bio_taxa SET current_scientific_name=?,parent_key=?,taxon_rank=?,protection_level=?,"
                "sensitive_level=?,status='active',merged_into_key=NULL,updated_at=? WHERE taxon_key=?",
                (entry["scientific_name"], entry.get("parent_key"), entry["taxon_rank"],
                 entry["protection_level"], entry["sensitive_level"], now, entry["taxon_key"]),
            )
        else:
            connection.execute(
                "INSERT INTO bio_taxa(taxon_key,current_scientific_name,parent_key,taxon_rank,protection_level,"
                "sensitive_level,status,first_version,created_at,updated_at) VALUES(?,?,?,?,?,?,'active',?,?,?)",
                (entry["taxon_key"], entry["scientific_name"], entry.get("parent_key"), entry["taxon_rank"],
                 entry["protection_level"], entry["sensitive_level"], version_no, now, now),
            )

    def _remove_taxon(self, connection: sqlite3.Connection, entry: dict, prev: dict,
                      version_no: int, now: str) -> dict:
        connection.execute(
            "UPDATE bio_taxa SET status='removed',updated_at=? WHERE taxon_key=?",
            (now, entry["taxon_key"]),
        )
        prev_ver = int(prev["version_no"])
        # 主用名与别名随移除降级为曾用名，保留最后在册版本以便溯源。
        connection.execute(
            "UPDATE bio_taxon_names SET is_primary=0,name_kind='曾用名',last_version=? "
            "WHERE taxon_key=? AND last_version>=?",
            (prev_ver, entry["taxon_key"], prev_ver),
        )
        return {
            "taxon_key": entry["taxon_key"], "change_kind": "移除",
            "old_name": prev["scientific_name"], "new_name": "",
            "reason": entry["change_reason"] or "本版移除",
            "affected_regions": prev["region_codes"],
        }

    def _diff_entry(self, connection: sqlite3.Connection, entry: dict, prev: dict | None,
                    version_no: int, now: str) -> list[dict]:
        reason = entry["change_reason"]
        regions = entry["region_codes"]
        changes: list[dict] = []
        if prev is None:
            changes.append({
                "taxon_key": entry["taxon_key"], "change_kind": "新增物种",
                "old_name": "", "new_name": entry["scientific_name"],
                "reason": reason or "首次纳入清单", "affected_regions": regions,
            })
            self._reconcile_names(connection, entry, prev, version_no, now)
            return changes

        if prev["scientific_name"] != entry["scientific_name"]:
            changes.append({
                "taxon_key": entry["taxon_key"], "change_kind": "改名",
                "old_name": prev["scientific_name"], "new_name": entry["scientific_name"],
                "reason": reason or "分类修订", "affected_regions": regions,
            })
        if prev["protection_level"] != entry["protection_level"]:
            changes.append({
                "taxon_key": entry["taxon_key"], "change_kind": "保护等级调整",
                "old_name": prev["protection_level"] or "（未列入）",
                "new_name": entry["protection_level"] or "（未列入）",
                "reason": reason or "保护名录更新", "affected_regions": regions,
            })
        added_regions = sorted(set(regions) - set(prev["region_codes"]))
        removed_regions = sorted(set(prev["region_codes"]) - set(regions))
        if added_regions or removed_regions:
            changes.append({
                "taxon_key": entry["taxon_key"], "change_kind": "区域调整",
                "old_name": "、".join(removed_regions) or "—",
                "new_name": "、".join(added_regions) or "—",
                "reason": reason or "分区域可见性调整",
                "affected_regions": sorted(set(added_regions) | set(removed_regions)),
            })
        for alias in sorted(set(entry["aliases"]) - set(prev["aliases"])):
            changes.append({
                "taxon_key": entry["taxon_key"], "change_kind": "新增别名",
                "old_name": "", "new_name": alias,
                "reason": reason or "补登别名", "affected_regions": regions,
            })
        for alias in sorted(set(prev["aliases"]) - set(entry["aliases"])):
            changes.append({
                "taxon_key": entry["taxon_key"], "change_kind": "弃用别名",
                "old_name": alias, "new_name": "",
                "reason": reason or "别名不再使用", "affected_regions": regions,
            })
        self._reconcile_names(connection, entry, prev, version_no, now)
        return changes

    def _reconcile_names(self, connection: sqlite3.Connection, entry: dict, prev: dict | None,
                         version_no: int, now: str) -> None:
        """把学名与别名变化落到 bio_taxon_names，供名称溯源。"""
        key = entry["taxon_key"]
        prev_ver = int(prev["version_no"]) if prev else None
        primary = connection.execute(
            "SELECT * FROM bio_taxon_names WHERE taxon_key=? AND is_primary=1", (key,)
        ).fetchone()
        same_name = connection.execute(
            "SELECT * FROM bio_taxon_names WHERE taxon_key=? AND name=?", (key, entry["scientific_name"])
        ).fetchone()
        if primary is None and same_name is not None:
            # 学名改回历史名称（A→B→A）：复用曾用名行恢复为主用名，避免唯一约束冲突。
            connection.execute(
                "UPDATE bio_taxon_names SET name_kind='学名',is_primary=1,change_reason=?,"
                "first_version=COALESCE(first_version,?),last_version=? WHERE id=?",
                (entry["change_reason"] or "恢复历史学名", version_no, version_no, same_name["id"]),
            )
        elif primary is None:
            connection.execute(
                "INSERT INTO bio_taxon_names(taxon_key,name,name_kind,is_primary,change_reason,first_version,"
                "last_version,created_at) VALUES(?,?,'学名',1,?,?,?,?)",
                (key, entry["scientific_name"], entry["change_reason"], version_no, version_no, now),
            )
        elif primary["name"] != entry["scientific_name"]:
            if same_name is not None:
                # 同一名称已存在（曾用名）：先删除该行，再把当前主用名降级，最后恢复旧行为主用名。
                connection.execute("DELETE FROM bio_taxon_names WHERE id=?", (same_name["id"],))
            connection.execute(
                "UPDATE bio_taxon_names SET is_primary=0,name_kind='曾用名',last_version=?,change_reason=? WHERE id=?",
                (prev_ver, entry["change_reason"] or "被新学名替代", primary["id"]),
            )
            connection.execute(
                "INSERT INTO bio_taxon_names(taxon_key,name,name_kind,is_primary,change_reason,first_version,"
                "last_version,created_at) VALUES(?,?,'学名',1,?,?,?,?)",
                (key, entry["scientific_name"], entry["change_reason"], version_no, version_no, now),
            )
        else:
            connection.execute(
                "UPDATE bio_taxon_names SET last_version=? WHERE id=?", (version_no, primary["id"])
            )

        existing = {
            row["name"]: row
            for row in connection.execute(
                "SELECT * FROM bio_taxon_names WHERE taxon_key=? AND name_kind IN ('别名','曾用名')", (key,)
            ).fetchall()
        }
        current_aliases = set(entry["aliases"])
        for alias in entry["aliases"]:
            row = existing.get(alias)
            if row is None:
                connection.execute(
                    "INSERT INTO bio_taxon_names(taxon_key,name,name_kind,is_primary,change_reason,first_version,"
                    "last_version,created_at) VALUES(?,?,'别名',0,?,?,?,?)",
                    (key, alias, entry["change_reason"], version_no, version_no, now),
                )
            else:
                connection.execute(
                    "UPDATE bio_taxon_names SET name_kind='别名',last_version=? WHERE id=?",
                    (version_no, row["id"]),
                )
        for name, row in existing.items():
            if name not in current_aliases and row["name_kind"] == "别名":
                connection.execute(
                    "UPDATE bio_taxon_names SET name_kind='曾用名',last_version=?,change_reason=? WHERE id=?",
                    (prev_ver, entry["change_reason"] or "别名弃用", row["id"]),
                )

    # ------------------------------------------------------------------ 查询

    def list_checklists(self, principal: Principal, status: str | None = None) -> list[dict]:
        principal.require("biodiversity.read")
        return [dict(row) for row in store.list_checklists(self.connection, status=status)]

    def checklist_detail(self, principal: Principal, checklist_id: int) -> dict[str, Any]:
        principal.require("biodiversity.read")
        checklist = self._require_checklist(self.connection, checklist_id)
        data = dict(checklist)
        if checklist["status"] in {"draft", "in_review", "approved"}:
            data["entries"] = [entry_dict(row) for row in store.draft_entries(self.connection, checklist_id)]
        else:
            data["entries"] = [entry_dict(row) for row in
                               store.published_entries(self.connection, checklist["version_no"])]
        return data

    def checklist_events(self, principal: Principal, checklist_id: int) -> list[dict]:
        principal.require("biodiversity.read")
        self._require_checklist(self.connection, checklist_id)
        return [_event_dict(row) for row in store.checklist_events(self.connection, checklist_id)]

    def validation_report(self, principal: Principal, checklist_id: int) -> dict[str, Any]:
        principal.require("biodiversity.read")
        violations = self.validate_checklist(checklist_id)
        return {"checklist_id": checklist_id, "ok": not violations, "violations": violations}

    def version_snapshot(self, principal: Principal, version_no: int,
                         region: str | None = None) -> dict[str, Any]:
        principal.require("biodiversity.read")
        checklist = self._require_version(self.connection, version_no)
        entries = [entry_dict(row) for row in store.published_entries(self.connection, version_no)]
        restricted_count = 0
        if region:
            if not store.get_region(self.connection, region):
                raise NotFoundError("区域不存在")
            grants = store.grant_map(self.connection)
            allowed = grants.get(region, grants.get("*", 0))
            visible: list[dict] = []
            for entry in entries:
                if entry["region_codes"] and region not in entry["region_codes"]:
                    continue
                if int(entry["sensitive_level"]) > allowed:
                    restricted_count += 1
                    continue
                visible.append(entry)
            entries = visible
        return {
            "version_no": version_no,
            "status": checklist["status"],
            "title": checklist["title"],
            "change_summary": checklist["change_summary"],
            "published_at": checklist["published_at"],
            "withdrawn_at": checklist["withdrawn_at"],
            "withdraw_reason": checklist["withdraw_reason"],
            "region": region,
            "restricted_count": restricted_count,
            "entries": entries,
        }

    def effective_version(self, principal: Principal, as_of: str | None = None,
                          region: str | None = None) -> dict[str, Any]:
        principal.require("biodiversity.read")
        if as_of:
            moment = _normalize_as_of(as_of)
            row = self.connection.execute(
                "SELECT version_no FROM bio_checklists WHERE published_at IS NOT NULL "
                "AND published_at<=? AND (withdrawn_at IS NULL OR withdrawn_at>?) "
                "ORDER BY published_at DESC, version_no DESC LIMIT 1",
                (moment, moment),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT version_no FROM bio_checklists WHERE status='published' "
                "ORDER BY published_at DESC, version_no DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise NotFoundError("该时刻没有生效中的清单版本")
        return self.version_snapshot(principal, int(row["version_no"]), region=region)

    def name_changes(self, principal: Principal, version_no: int) -> list[dict]:
        principal.require("biodiversity.read")
        self._require_version(self.connection, version_no)
        return [_change_dict(row) for row in store.name_changes(self.connection, version_no)]

    def search_names(self, principal: Principal, keyword: str) -> list[dict]:
        principal.require("biodiversity.read")
        keyword = keyword.strip()
        if not keyword:
            raise ValidationError("检索词不能为空")
        rows = self.connection.execute(
            "SELECT n.taxon_key,n.name,n.name_kind,n.is_primary,n.first_version,n.last_version,"
            "n.change_reason,t.current_scientific_name,t.status "
            "FROM bio_taxon_names n JOIN bio_taxa t ON t.taxon_key=n.taxon_key "
            "WHERE n.name LIKE ? ORDER BY n.taxon_key,n.first_version",
            (f"%{keyword}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def taxon_detail(self, principal: Principal, taxon_key: str) -> dict[str, Any]:
        principal.require("biodiversity.read")
        taxon = self.connection.execute("SELECT * FROM bio_taxa WHERE taxon_key=?", (taxon_key,)).fetchone()
        if taxon is None:
            raise NotFoundError("物种主档不存在")
        names = self.connection.execute(
            "SELECT name,name_kind,is_primary,first_version,last_version,change_reason "
            "FROM bio_taxon_names WHERE taxon_key=? ORDER BY first_version,id", (taxon_key,)
        ).fetchall()
        versions = [row["version_no"] for row in self.connection.execute(
            "SELECT DISTINCT version_no FROM bio_published_entries WHERE taxon_key=? ORDER BY version_no",
            (taxon_key,),
        ).fetchall()]
        return {"taxon": dict(taxon), "names": [dict(row) for row in names], "published_versions": versions}

    # ------------------------------------------------------------------ 辅助

    def _require_checklist(self, connection: sqlite3.Connection, checklist_id: int) -> sqlite3.Row:
        checklist = store.get_checklist(connection, checklist_id)
        if checklist is None:
            raise NotFoundError("清单版本不存在")
        return checklist

    def _require_version(self, connection: sqlite3.Connection, version_no: int) -> sqlite3.Row:
        checklist = store.get_checklist_by_version(connection, version_no)
        if checklist is None or checklist["published_at"] is None:
            raise NotFoundError("该版本不存在或尚未发布")
        return checklist

    @staticmethod
    def _require_state(checklist: sqlite3.Row, allowed: set[str]) -> None:
        if checklist["status"] not in allowed:
            raise ConflictError(
                f"清单 v{checklist['version_no']} 当前状态为 {checklist['status']}，"
                f"允许该操作的状态：{'、'.join(sorted(allowed))}"
            )

    def _event(self, connection: sqlite3.Connection, checklist_id: int, version_no: int, action: str,
               principal: Principal, detail: dict) -> None:
        connection.execute(
            "INSERT INTO bio_events(checklist_id,version_no,action,actor,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (checklist_id, version_no, action, principal.username,
             json.dumps(detail, ensure_ascii=False), _now()),
        )


def _normalize_as_of(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("as_of 必须是 ISO 8601 时间，例如 2026-09-01T00:00:00+00:00") from exc
    return to_storage(parsed)
