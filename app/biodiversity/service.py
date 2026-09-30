from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import to_storage, utc_now
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction

# ---------------------------------------------------------------------------
# 结构定义
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS bio_regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bio_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'reference',
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '',
    year INTEGER,
    url TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bio_sensitive_grants (
    scope TEXT NOT NULL,
    region_code TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, region_code)
);

CREATE TABLE IF NOT EXISTS bio_species_names (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taxon_id TEXT NOT NULL,
    name TEXT NOT NULL,
    name_type TEXT NOT NULL CHECK(name_type IN ('canonical','scientific','synonym','common_name','former_name')),
    language TEXT NOT NULL DEFAULT 'zh',
    source_citation_key TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(taxon_id, name, name_type, language)
);
CREATE INDEX IF NOT EXISTS idx_bio_names_taxon ON bio_species_names(taxon_id, created_at);

CREATE TABLE IF NOT EXISTS bio_checklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    current_version_id INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bio_checklist_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_id INTEGER NOT NULL REFERENCES bio_checklists(id) ON DELETE RESTRICT,
    version_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('editing','review_pending','review_passed','review_rejected','published','withdrawn')),
    based_on_version_id INTEGER REFERENCES bio_checklist_versions(id),
    created_by TEXT NOT NULL,
    review_requested_at TEXT,
    review_requested_by TEXT,
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_decision TEXT CHECK(review_decision IN ('passed','rejected') OR review_decision IS NULL),
    review_notes TEXT NOT NULL DEFAULT '',
    validation_json TEXT NOT NULL DEFAULT '{}',
    published_at TEXT,
    published_by TEXT,
    effective_at TEXT,
    withdrawn_at TEXT,
    withdrawn_by TEXT,
    withdraw_reason TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(checklist_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_bio_versions_effective ON bio_checklist_versions(checklist_id, effective_at);

-- version_id 为空表示“当前草稿”行；发布时整组复制到新版本
CREATE TABLE IF NOT EXISTS bio_checklist_species (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_id INTEGER NOT NULL REFERENCES bio_checklists(id) ON DELETE RESTRICT,
    version_id INTEGER REFERENCES bio_checklist_versions(id) ON DELETE CASCADE,
    taxon_id TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    authorship TEXT NOT NULL DEFAULT '',
    taxon_rank TEXT NOT NULL DEFAULT 'species',
    parent_taxon_id TEXT NOT NULL DEFAULT '',
    protection_level TEXT NOT NULL DEFAULT '',
    is_sensitive INTEGER NOT NULL DEFAULT 0 CHECK(is_sensitive IN (0,1)),
    sensitivity_scope TEXT NOT NULL DEFAULT '',
    visible_region_codes TEXT NOT NULL DEFAULT '[]',
    citation_refs TEXT NOT NULL DEFAULT '[]',
    occurrence_note TEXT NOT NULL DEFAULT '',
    is_removed INTEGER NOT NULL DEFAULT 0 CHECK(is_removed IN (0,1)),
    change_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bio_species_draft_unique
    ON bio_checklist_species(checklist_id, taxon_id) WHERE version_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_bio_species_version_unique
    ON bio_checklist_species(version_id, taxon_id);

CREATE TABLE IF NOT EXISTS bio_checklist_name_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES bio_checklist_versions(id) ON DELETE CASCADE,
    taxon_id TEXT NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('added','removed','renamed','reclassified','updated')),
    from_canonical TEXT NOT NULL DEFAULT '',
    to_canonical TEXT NOT NULL DEFAULT '',
    from_parent TEXT NOT NULL DEFAULT '',
    to_parent TEXT NOT NULL DEFAULT '',
    from_protection TEXT NOT NULL DEFAULT '',
    to_protection TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    affected_regions TEXT NOT NULL DEFAULT '[]',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bio_changes_version ON bio_checklist_name_changes(version_id);

CREATE TABLE IF NOT EXISTS bio_checklist_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_id INTEGER,
    version_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bio_events_checklist ON bio_checklist_events(checklist_id, id);
"""

EDITABLE_STATES = {"editing", "review_rejected"}
ROLE_PERMISSIONS = {
    "editor": {"draft.create", "draft.edit", "review.request"},
    "reviewer": {"review.act"},
    "publisher": {"version.publish"},
    "admin": {"region.manage", "grant.manage", "version.withdraw", "version.publish"},
    "viewer": set(),
}


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _now() -> str:
    return to_storage(utc_now())


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class BiodiversityService:
    """物种清单草稿、复核、发布与撤回的事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema()

    # -- 权限 ---------------------------------------------------------------

    @staticmethod
    def _require(role: str, permission: str) -> None:
        if permission not in ROLE_PERMISSIONS.get(role, set()):
            raise PermissionDeniedError(
                f"角色 {role or 'viewer'} 无权执行 {permission}",
                context={"role": role, "required_permission": permission},
            )

    # -- 基础资料：区域 / 引用 / 敏感授权 ------------------------------------

    def create_region(self, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require(role, "region.manage")
        now = _now()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO bio_regions(code,name,is_active,created_at) VALUES(?,?,1,?)",
                    (payload["code"], payload["name"], now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"区域代码已存在: {payload['code']}") from exc
            self._record_event(connection, None, None, "region.create", actor, payload, now)
            return dict(connection.execute("SELECT * FROM bio_regions WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_regions(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM bio_regions"
        if not include_inactive:
            sql += " WHERE is_active=1"
        return [dict(row) for row in self.connection.execute(sql + " ORDER BY code").fetchall()]

    def create_citation(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO bio_citations(key,kind,title,authors,year,url,detail,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        payload["key"],
                        payload.get("kind", "reference"),
                        payload["title"],
                        payload.get("authors", ""),
                        payload.get("year"),
                        payload.get("url", ""),
                        payload.get("detail", ""),
                        actor,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"引用键已存在: {payload['key']}") from exc
            return dict(connection.execute("SELECT * FROM bio_citations WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_citations(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM bio_citations ORDER BY key").fetchall()]

    def grant_sensitive_scope(self, scope: str, region_code: str, actor: str, role: str) -> dict[str, Any]:
        self._require(role, "grant.manage")
        now = _now()
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM bio_regions WHERE code=? AND is_active=1", (region_code,)).fetchone() is None:
                raise NotFoundError(f"区域不存在或已停用: {region_code}")
            connection.execute(
                "INSERT OR IGNORE INTO bio_sensitive_grants(scope,region_code,granted_by,created_at) VALUES(?,?,?,?)",
                (scope, region_code, actor, now),
            )
            self._record_event(connection, None, None, "grant.upsert", actor, {"scope": scope, "region_code": region_code}, now)
        return {"scope": scope, "region_code": region_code, "granted": True}

    def list_sensitive_grants(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM bio_sensitive_grants ORDER BY scope, region_code"
        ).fetchall()]

    # -- 别名 / 曾用名 -------------------------------------------------------

    def register_name(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        citation_key = payload.get("source_citation_key")
        if citation_key and self.connection.execute("SELECT 1 FROM bio_citations WHERE key=?", (citation_key,)).fetchone() is None:
            raise ValidationError(f"引用不存在: {citation_key}", context={"citation_key": citation_key})
        now = _now()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO bio_species_names(taxon_id,name,name_type,language,source_citation_key,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        payload["taxon_id"],
                        payload["name"],
                        payload.get("name_type", "synonym"),
                        payload.get("language", "zh"),
                        citation_key,
                        actor,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该名称已登记") from exc
            return dict(connection.execute("SELECT * FROM bio_species_names WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_names(self, taxon_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM bio_species_names WHERE taxon_id=? ORDER BY created_at,id", (taxon_id,)
        ).fetchall()]

    # -- 清单与草稿 ----------------------------------------------------------

    def create_checklist(self, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require(role, "draft.create")
        now = _now()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO bio_checklists(code,title,description,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (payload["code"], payload["title"], payload.get("description", ""), actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"清单代码已存在: {payload['code']}") from exc
            checklist_id = cursor.lastrowid
            self._create_draft_version(connection, checklist_id, None, actor, now)
            self._record_event(connection, checklist_id, None, "checklist.create", actor, payload, now)
            return self.get_checklist(payload["code"])

    def _create_draft_version(
        self, connection: sqlite3.Connection, checklist_id: int, basis_version_id: int | None, actor: str, now: str
    ) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 AS next_no FROM bio_checklist_versions WHERE checklist_id=?",
            (checklist_id,),
        ).fetchone()
        cursor = connection.execute(
            "INSERT INTO bio_checklist_versions(checklist_id,version_no,status,based_on_version_id,created_by,created_at,updated_at)"
            " VALUES(?,?, 'editing', ?,?,?,?)",
            (checklist_id, row["next_no"], basis_version_id, actor, now, now),
        )
        version_id = cursor.lastrowid
        if basis_version_id is not None:
            # 以旧版为起点：把旧版有效物种复制进新草稿，编辑在其之上进行
            connection.execute(
                "INSERT INTO bio_checklist_species(checklist_id,version_id,taxon_id,scientific_name,canonical_name,"
                "authorship,taxon_rank,parent_taxon_id,protection_level,is_sensitive,sensitivity_scope,"
                "visible_region_codes,citation_refs,occurrence_note,is_removed,change_reason,created_at,updated_at)"
                " SELECT checklist_id,NULL,taxon_id,scientific_name,canonical_name,authorship,taxon_rank,parent_taxon_id,"
                "protection_level,is_sensitive,sensitivity_scope,visible_region_codes,citation_refs,occurrence_note,0,'',?,? "
                "FROM bio_checklist_species WHERE version_id=? AND is_removed=0",
                (now, now, basis_version_id),
            )
        connection.execute("UPDATE bio_checklists SET updated_at=? WHERE id=?", (now, checklist_id))
        return version_id

    def list_checklists(self) -> list[dict[str, Any]]:
        items = [dict(row) for row in self.connection.execute(
            """
            SELECT c.*, v.id AS draft_version_id, v.version_no AS draft_version_no, v.status AS draft_status
            FROM bio_checklists c
            LEFT JOIN bio_checklist_versions v
              ON v.checklist_id=c.id AND v.status IN ('editing','review_pending','review_passed','review_rejected')
            ORDER BY c.id
            """
        ).fetchall()]
        for item in items:
            current = self.connection.execute(
                "SELECT id,version_no,status,published_at,effective_at FROM bio_checklist_versions WHERE id=?",
                (item["current_version_id"],),
            ).fetchone() if item["current_version_id"] else None
            item["current_version"] = dict(current) if current else None
        return items

    def get_checklist(self, code: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM bio_checklists WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError(f"清单不存在: {code}")
        result = dict(row)
        versions = [dict(item) for item in self.connection.execute(
            "SELECT id,version_no,status,based_on_version_id,created_by,review_requested_at,reviewed_at,"
            "review_decision,published_at,effective_at,withdrawn_at,withdraw_reason "
            "FROM bio_checklist_versions WHERE checklist_id=? ORDER BY version_no",
            (row["id"],),
        ).fetchall()]
        result["versions"] = versions
        return result

    def _get_checklist_id(self, connection: sqlite3.Connection, code: str) -> int:
        row = connection.execute("SELECT id FROM bio_checklists WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError(f"清单不存在: {code}")
        return row["id"]

    def _get_draft(self, connection: sqlite3.Connection, checklist_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM bio_checklist_versions WHERE checklist_id=? AND status IN ('editing','review_pending','review_passed','review_rejected')"
            " ORDER BY version_no DESC LIMIT 1",
            (checklist_id,),
        ).fetchone()
        if row is None:
            raise ConflictError("该清单当前没有开放草稿（上一版已发布，需创建新草稿）")
        return row

    def create_next_draft(self, code: str, note: str, actor: str, role: str) -> dict[str, Any]:
        self._require(role, "draft.create")
        now = _now()
        with transaction(immediate=True) as connection:
            checklist_id = self._get_checklist_id(connection, code)
            open_draft = connection.execute(
                "SELECT id FROM bio_checklist_versions WHERE checklist_id=? AND status IN ('editing','review_pending','review_passed','review_rejected')",
                (checklist_id,),
            ).fetchone()
            if open_draft is not None:
                raise ConflictError("已有未完成的草稿，请先完成或放弃它")
            current_id = connection.execute(
                "SELECT current_version_id FROM bio_checklists WHERE id=?", (checklist_id,)
            ).fetchone()[0]
            if current_id is None:
                raise ConflictError("清单尚未发布过任何版本，首版草稿已随清单创建")
            version_id = self._create_draft_version(connection, checklist_id, current_id, actor, now)
            self._record_event(connection, checklist_id, version_id, "draft.create_next", actor, {"basis_version_id": current_id, "note": note}, now)
            return self._version_detail(connection, version_id)

    def get_draft(self, code: str) -> dict[str, Any]:
        checklist_id = self._get_checklist_id(self.connection, code)
        draft = self._get_draft(self.connection, checklist_id)
        return self._version_detail(self.connection, draft["id"])

    def upsert_species(self, code: str, payload: dict[str, Any], actor: str, role: str) -> dict[str, Any]:
        self._require(role, "draft.edit")
        if not payload.get("change_reason", "").strip():
            raise ValidationError("必须填写本次名称/信息变化原因（change_reason），供讲义追溯")
        regions = payload.get("visible_region_codes") or []
        refs = payload.get("citation_refs") or []
        now = _now()
        with transaction(immediate=True) as connection:
            checklist_id = self._get_checklist_id(connection, code)
            draft = self._get_draft(connection, checklist_id)
            if draft["status"] not in EDITABLE_STATES:
                raise ConflictError(f"草稿处于 {draft['status']} 状态，不可编辑（请先经复核驳回）")
            existing = connection.execute(
                "SELECT * FROM bio_checklist_species WHERE checklist_id=? AND version_id IS NULL AND taxon_id=?",
                (checklist_id, payload["taxon_id"]),
            ).fetchone()
            fields = (
                payload["scientific_name"],
                payload["canonical_name"],
                payload.get("authorship", ""),
                payload.get("taxon_rank", "species"),
                payload.get("parent_taxon_id", ""),
                payload.get("protection_level", ""),
                1 if payload.get("is_sensitive") else 0,
                payload.get("sensitivity_scope", ""),
                json.dumps(regions, ensure_ascii=False),
                json.dumps(refs, ensure_ascii=False),
                payload.get("occurrence_note", ""),
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO bio_checklist_species(checklist_id,version_id,taxon_id,scientific_name,canonical_name,"
                    "authorship,taxon_rank,parent_taxon_id,protection_level,is_sensitive,sensitivity_scope,"
                    "visible_region_codes,citation_refs,occurrence_note,is_removed,change_reason,created_at,updated_at)"
                    " VALUES(?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
                    (checklist_id, payload["taxon_id"], *fields, payload["change_reason"], now, now),
                )
            else:
                connection.execute(
                    "UPDATE bio_checklist_species SET scientific_name=?,canonical_name=?,authorship=?,taxon_rank=?,"
                    "parent_taxon_id=?,protection_level=?,is_sensitive=?,sensitivity_scope=?,visible_region_codes=?,"
                    "citation_refs=?,occurrence_note=?,is_removed=0,change_reason=?,updated_at=? WHERE id=?",
                    (*fields, payload["change_reason"], now, existing["id"]),
                )
            for alias in payload.get("aliases") or []:
                key = alias.get("source_citation_key")
                if key and connection.execute("SELECT 1 FROM bio_citations WHERE key=?", (key,)).fetchone() is None:
                    raise ValidationError(f"别名引用不存在: {key}", context={"taxon_id": payload["taxon_id"], "citation_key": key})
                connection.execute(
                    "INSERT OR IGNORE INTO bio_species_names(taxon_id,name,name_type,language,source_citation_key,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        payload["taxon_id"],
                        alias["name"],
                        alias.get("name_type", "synonym"),
                        alias.get("language", "zh"),
                        key,
                        actor,
                        now,
                    ),
                )
            self._record_event(connection, checklist_id, draft["id"], "draft.upsert_species", actor,
                              {"taxon_id": payload["taxon_id"], "reason": payload["change_reason"]}, now)
            return self._draft_species(connection, checklist_id, payload["taxon_id"])

    def remove_species(self, code: str, taxon_id: str, reason: str, actor: str, role: str) -> dict[str, Any]:
        self._require(role, "draft.edit")
        if not reason.strip():
            raise ValidationError("撤编物种必须填写原因")
        now = _now()
        with transaction(immediate=True) as connection:
            checklist_id = self._get_checklist_id(connection, code)
            draft = self._get_draft(connection, checklist_id)
            if draft["status"] not in EDITABLE_STATES:
                raise ConflictError(f"草稿处于 {draft['status']} 状态，不可编辑")
            existing = connection.execute(
                "SELECT * FROM bio_checklist_species WHERE checklist_id=? AND version_id IS NULL AND taxon_id=?",
                (checklist_id, taxon_id),
            ).fetchone()
            if existing is None:
                raise NotFoundError(f"草稿中没有该物种: {taxon_id}")
            connection.execute(
                "UPDATE bio_checklist_species SET is_removed=1,change_reason=?,updated_at=? WHERE id=?",
                (reason, now, existing["id"]),
            )
            self._record_event(connection, checklist_id, draft["id"], "draft.remove_species", actor,
                              {"taxon_id": taxon_id, "reason": reason}, now)
            return {"taxon_id": taxon_id, "is_removed": True, "reason": reason}

    # -- 复核 ---------------------------------------------------------------

    def request_review(self, code: str, note: str, actor: str, role: str) -> dict[str, Any]:
        self._require(role, "review.request")
        now = _now()
        with transaction(immediate=True) as connection:
            checklist_id = self._get_checklist_id(connection, code)
            draft = self._get_draft(connection, checklist_id)
            if draft["status"] not in EDITABLE_STATES:
                raise ConflictError(f"草稿当前状态 {draft['status']} 不能提请复核")
            has_entries = connection.execute(
                "SELECT COUNT(*) AS n FROM bio_checklist_species WHERE checklist_id=? AND version_id IS NULL AND is_removed=0",
                (checklist_id,),
            ).fetchone()["n"]
            if has_entries == 0:
                raise ConflictError("草稿没有任何有效物种，不能提请复核")
            connection.execute(
                "UPDATE bio_checklist_versions SET status='review_pending',review_requested_at=?,review_requested_by=?,"
                "review_decision=NULL,review_notes='',updated_at=? WHERE id=?",
                (now, actor, now, draft["id"]),
            )
            self._record_event(connection, checklist_id, draft["id"], "review.request", actor, {"note": note}, now)
            return self._version_detail(connection, draft["id"])

    def review(self, code: str, decision: str, notes: str, actor: str, role: str) -> dict[str, Any]:
        self._require(role, "review.act")
        if decision not in {"passed", "rejected"}:
            raise ValidationError("复核结论必须是 passed 或 rejected")
        now = _now()
        with transaction(immediate=True) as connection:
            checklist_id = self._get_checklist_id(connection, code)
            draft = self._get_draft(connection, checklist_id)
            if draft["status"] != "review_pending":
                raise ConflictError(f"草稿处于 {draft['status']} 状态，不能复核")
            next_status = "review_passed" if decision == "passed" else "review_rejected"
            connection.execute(
                "UPDATE bio_checklist_versions SET status=?,reviewed_at=?,reviewed_by=?,review_decision=?,"
                "review_notes=?,updated_at=? WHERE id=?",
                (next_status, now, actor, decision, notes, now, draft["id"]),
            )
            self._record_event(connection, checklist_id, draft["id"], f"review.{decision}", actor, {"notes": notes}, now)
            return self._version_detail(connection, draft["id"])

    # -- 发布校验 ------------------------------------------------------------

    def validate_draft(self, code: str) -> dict[str, Any]:
        checklist_id = self._get_checklist_id(self.connection, code)
        draft = self._get_draft(self.connection, checklist_id)
        result = self._validate(self.connection, draft["id"])
        return result

    def _validate(self, connection: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        version = connection.execute(
            "SELECT checklist_id,status FROM bio_checklist_versions WHERE id=?", (version_id,)
        ).fetchone()
        if version is None:
            raise NotFoundError("版本不存在")
        # 草稿物种行的 version_id 为 NULL，按清单读取；已发布版本按版本快照读取
        if version["status"] in {"editing", "review_pending", "review_passed", "review_rejected"}:
            rows = connection.execute(
                "SELECT * FROM bio_checklist_species WHERE checklist_id=? AND version_id IS NULL",
                (version["checklist_id"],),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM bio_checklist_species WHERE version_id=?", (version_id,)
            ).fetchall()
        active = [row for row in rows if row["is_removed"] == 0]

        checks: list[dict[str, Any]] = []

        # 1) 分类关系闭合：父级必须存在于同版本，且链路无环
        taxonomy_errors: list[dict[str, Any]] = []
        nodes = {row["taxon_id"]: row["parent_taxon_id"] for row in active}
        for taxon_id, parent in nodes.items():
            if parent and parent not in nodes:
                taxonomy_errors.append({"taxon_id": taxon_id, "problem": f"父级分类 {parent} 不在本版本中，分类链未闭合"})
        color: dict[str, str] = {}

        def visit(node: str, stack: list[str]) -> None:
            color[node] = "gray"
            stack.append(node)
            parent = nodes.get(node, "")
            if parent:
                if color.get(parent) == "gray":
                    cycle = stack[stack.index(parent):] + [parent]
                    taxonomy_errors.append({"taxon_id": node, "problem": "分类环闭合错误：检测到环 " + " → ".join(cycle)})
                elif color.get(parent) is None and parent in nodes:
                    visit(parent, stack)
            stack.pop()
            color[node] = "black"

        for taxon_id in nodes:
            if color.get(taxon_id) is None:
                visit(taxon_id, [])
        checks.append({"code": "taxonomy_closed", "passed": not taxonomy_errors, "details": taxonomy_errors})

        # 2) 引用必须存在
        citation_errors: list[dict[str, Any]] = []
        known = {row["key"] for row in connection.execute("SELECT key FROM bio_citations").fetchall()}
        for row in active:
            for key in _loads(row["citation_refs"], []):
                if key not in known:
                    citation_errors.append({"taxon_id": row["taxon_id"], "problem": f"引用 {key} 不存在"})
        for name_row in connection.execute("SELECT * FROM bio_species_names").fetchall():
            if name_row["source_citation_key"] and name_row["source_citation_key"] not in known:
                citation_errors.append({"taxon_id": name_row["taxon_id"], "problem": f"名称 “{name_row['name']}” 的引用 {name_row['source_citation_key']} 不存在"})
        checks.append({"code": "citations_exist", "passed": not citation_errors, "details": citation_errors})

        # 3) 区域存在 + 敏感物种不得超出授权范围
        region_errors: list[dict[str, Any]] = []
        active_regions = {row["code"] for row in connection.execute("SELECT code FROM bio_regions WHERE is_active=1").fetchall()}
        grants = {(row["scope"], row["region_code"]) for row in connection.execute("SELECT scope,region_code FROM bio_sensitive_grants").fetchall()}
        for row in active:
            regions = _loads(row["visible_region_codes"], [])
            for region in regions:
                if region not in active_regions:
                    region_errors.append({"taxon_id": row["taxon_id"], "problem": f"可见区域 {region} 不存在或已停用"})
            if row["is_sensitive"]:
                scope = row["sensitivity_scope"]
                if not scope:
                    region_errors.append({"taxon_id": row["taxon_id"], "problem": "敏感物种未指定授权范围 sensitivity_scope"})
                for region in regions:
                    if (scope, region) not in grants:
                        region_errors.append({
                            "taxon_id": row["taxon_id"],
                            "problem": f"敏感物种在区域 {region} 超出授权范围（scope={scope} 未获该区域授权）",
                        })
        checks.append({"code": "sensitive_authorized", "passed": not region_errors, "details": region_errors})

        passed = all(item["passed"] for item in checks)
        return {
            "status": "passed" if passed else "failed",
            "passed": passed,
            "checks": checks,
            "species_count": len(active),
        }

    # -- 发布 ---------------------------------------------------------------

    def publish(self, code: str, actor: str, role: str, effective_at: str | None = None, note: str = "") -> dict[str, Any]:
        self._require(role, "version.publish")
        now = _now()
        effective = effective_at or now
        with transaction(immediate=True) as connection:
            checklist_id = self._get_checklist_id(connection, code)
            draft = self._get_draft(connection, checklist_id)
            if draft["status"] != "review_passed":
                raise ConflictError("只有复核通过（review_passed）的草稿才能发布")
            report = self._validate(connection, draft["id"])
            connection.execute(
                "UPDATE bio_checklist_versions SET validation_json=? WHERE id=?",
                (json.dumps(report, ensure_ascii=False), draft["id"]),
            )
            if not report["passed"]:
                raise ValidationError("发布校验未通过", context={"validation": report})

            # 复制草稿有效物种行为不可变版本快照（撤编条目不进快照，撤编原因留痕于名称变化表）
            species_rows = connection.execute(
                "SELECT * FROM bio_checklist_species WHERE checklist_id=? AND version_id IS NULL", (checklist_id,)
            ).fetchall()
            columns = [
                "taxon_id", "scientific_name", "canonical_name", "authorship", "taxon_rank", "parent_taxon_id",
                "protection_level", "is_sensitive", "sensitivity_scope", "visible_region_codes", "citation_refs",
                "occurrence_note",
            ]
            placeholders = ",".join("?" for _ in columns)
            for row in species_rows:
                if row["is_removed"] == 1:
                    continue
                values = [row[column] for column in columns]
                connection.execute(
                    f"INSERT INTO bio_checklist_species(checklist_id,version_id,{','.join(columns)},created_at,updated_at)"
                    f" VALUES(?,?,{placeholders},?,?)",
                    (checklist_id, draft["id"], *values, now, now),
                )

            # 与基础版本对比，生成名称变化记录
            self._build_name_changes(connection, draft, species_rows, actor, now)

            active_count = sum(1 for row in species_rows if row["is_removed"] == 0)
            snapshot = {"active_count": active_count, "note": note}
            connection.execute(
                "UPDATE bio_checklist_versions SET status='published',published_at=?,published_by=?,"
                "effective_at=?,snapshot_json=?,updated_at=? WHERE id=?",
                (now, actor, effective, json.dumps(snapshot, ensure_ascii=False), now, draft["id"]),
            )
            connection.execute(
                "UPDATE bio_checklists SET current_version_id=?,updated_at=? WHERE id=?",
                (draft["id"], now, checklist_id),
            )
            connection.execute(
                "DELETE FROM bio_checklist_species WHERE checklist_id=? AND version_id IS NULL", (checklist_id,)
            )
            self._record_event(connection, checklist_id, draft["id"], "version.publish", actor,
                              {"version_no": draft["version_no"], "effective_at": effective, "note": note}, now)
            return self._version_detail(connection, draft["id"])

    def _build_name_changes(
        self,
        connection: sqlite3.Connection,
        draft: sqlite3.Row,
        species_rows: list[sqlite3.Row],
        actor: str,
        now: str,
    ) -> None:
        old_rows: dict[str, sqlite3.Row] = {}
        if draft["based_on_version_id"] is not None:
            for row in connection.execute(
                "SELECT * FROM bio_checklist_species WHERE version_id=? AND is_removed=0",
                (draft["based_on_version_id"],),
            ).fetchall():
                old_rows[row["taxon_id"]] = row

        for row in species_rows:
            old = old_rows.get(row["taxon_id"])
            new_regions = _loads(row["visible_region_codes"], [])
            if row["is_removed"] == 1:
                if old is None:
                    continue
                self._insert_change(connection, draft["id"], row, "removed", old, None, actor, now,
                                    _loads(old["visible_region_codes"], []))
                continue
            if old is None:
                self._insert_change(connection, draft["id"], row, "added", None, row, actor, now, new_regions)
                continue
            if row["canonical_name"] != old["canonical_name"]:
                change_type = "renamed"
            elif row["parent_taxon_id"] != old["parent_taxon_id"]:
                change_type = "reclassified"
            elif any(row[field] != old[field] for field in (
                "scientific_name", "protection_level", "is_sensitive", "sensitivity_scope",
                "visible_region_codes", "citation_refs", "occurrence_note",
            )):
                change_type = "updated"
            else:
                continue
            affected = sorted(set(new_regions) | set(_loads(old["visible_region_codes"], [])))
            self._insert_change(connection, draft["id"], row, change_type, old, row, actor, now, affected)

    def _insert_change(
        self,
        connection: sqlite3.Connection,
        version_id: int,
        species_row: sqlite3.Row,
        change_type: str,
        old: sqlite3.Row | None,
        new: sqlite3.Row | None,
        actor: str,
        now: str,
        regions: list[str],
    ) -> None:
        connection.execute(
            "INSERT INTO bio_checklist_name_changes(version_id,taxon_id,change_type,from_canonical,to_canonical,"
            "from_parent,to_parent,from_protection,to_protection,reason,affected_regions,actor,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                species_row["taxon_id"],
                change_type,
                old["canonical_name"] if old else "",
                new["canonical_name"] if new else "",
                old["parent_taxon_id"] if old else "",
                new["parent_taxon_id"] if new else "",
                old["protection_level"] if old else "",
                new["protection_level"] if new else "",
                species_row["change_reason"],
                json.dumps(regions, ensure_ascii=False),
                actor,
                now,
            ),
        )

    # -- 撤回 ---------------------------------------------------------------

    def withdraw_version(self, code: str, version_no: int, reason: str, actor: str, role: str) -> dict[str, Any]:
        self._require(role, "version.withdraw")
        if not reason.strip():
            raise ValidationError("撤回必须填写原因")
        now = _now()
        with transaction(immediate=True) as connection:
            checklist_id = self._get_checklist_id(connection, code)
            version = connection.execute(
                "SELECT * FROM bio_checklist_versions WHERE checklist_id=? AND version_no=?",
                (checklist_id, version_no),
            ).fetchone()
            if version is None:
                raise NotFoundError(f"版本不存在: v{version_no}")
            if version["status"] != "published":
                raise ConflictError(f"版本状态为 {version['status']}，只有已发布版本可以撤回")
            connection.execute(
                "UPDATE bio_checklist_versions SET status='withdrawn',withdrawn_at=?,withdrawn_by=?,withdraw_reason=?,"
                "updated_at=? WHERE id=?",
                (now, actor, reason, now, version["id"]),
            )
            # 撤回只阻止后续使用：若撤回的是当前生效版本，指针回退到上一有效版本；历史快照保留
            if version["id"] == connection.execute(
                "SELECT current_version_id FROM bio_checklists WHERE id=?", (checklist_id,)
            ).fetchone()[0]:
                fallback = connection.execute(
                    "SELECT id FROM bio_checklist_versions WHERE checklist_id=? AND status='published' AND effective_at<=?"
                    " ORDER BY effective_at DESC, version_no DESC LIMIT 1",
                    (checklist_id, now),
                ).fetchone()
                connection.execute(
                    "UPDATE bio_checklists SET current_version_id=?,updated_at=? WHERE id=?",
                    (fallback["id"] if fallback else None, now, checklist_id),
                )
            self._record_event(connection, checklist_id, version["id"], "version.withdraw", actor,
                              {"version_no": version_no, "reason": reason}, now)
            return self._version_detail(connection, version["id"])

    # -- 读取 / 历史查询 -----------------------------------------------------

    def list_versions(self, code: str) -> list[dict[str, Any]]:
        checklist_id = self._get_checklist_id(self.connection, code)
        return [dict(row) for row in self.connection.execute(
            "SELECT id,version_no,status,based_on_version_id,published_at,published_by,effective_at,"
            "withdrawn_at,withdrawn_by,withdraw_reason,review_decision,review_notes,snapshot_json "
            "FROM bio_checklist_versions WHERE checklist_id=? ORDER BY version_no",
            (checklist_id,),
        ).fetchall()]

    def get_version(self, code: str, version_no: int) -> dict[str, Any]:
        checklist_id = self._get_checklist_id(self.connection, code)
        row = self.connection.execute(
            "SELECT id FROM bio_checklist_versions WHERE checklist_id=? AND version_no=?",
            (checklist_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"版本不存在: v{version_no}")
        return self._version_detail(self.connection, row["id"])

    def version_at_date(self, code: str, date: str) -> dict[str, Any]:
        """按发布/生效日期查询当时的版本（含日后被撤回的版本，历史不抹除）。"""
        checklist_id = self._get_checklist_id(self.connection, code)
        row = self.connection.execute(
            "SELECT id FROM bio_checklist_versions WHERE checklist_id=? AND status IN ('published','withdrawn')"
            " AND effective_at<=? ORDER BY effective_at DESC, version_no DESC LIMIT 1",
            (checklist_id, date),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{date} 之前该清单没有已发布版本")
        detail = self._version_detail(self.connection, row["id"])
        detail["query_context"] = {"at": date, "note": "按生效日期追溯，撤回版本仍可查"}
        return detail

    def name_changes(self, code: str, version_no: int) -> list[dict[str, Any]]:
        version = self.get_version(code, version_no)
        return version["name_changes"]

    def event_timeline(self, code: str) -> list[dict[str, Any]]:
        checklist_id = self._get_checklist_id(self.connection, code)
        return [dict(row) for row in self.connection.execute(
            "SELECT id,version_id,action,actor,detail_json,created_at FROM bio_checklist_events"
            " WHERE checklist_id=? ORDER BY id", (checklist_id,)
        ).fetchall()]

    def query_species(
        self,
        code: str,
        *,
        version_no: int | None = None,
        at: str | None = None,
        region: str | None = None,
        viewer_scope: str = "public",
        keyword: str | None = None,
    ) -> dict[str, Any]:
        checklist_id = self._get_checklist_id(self.connection, code)
        if version_no is not None:
            row = self.connection.execute(
                "SELECT * FROM bio_checklist_versions WHERE checklist_id=? AND version_no=?",
                (checklist_id, version_no),
            ).fetchone()
            context = f"v{version_no}"
        elif at is not None:
            row = self.connection.execute(
                "SELECT * FROM bio_checklist_versions WHERE checklist_id=? AND status IN ('published','withdrawn')"
                " AND effective_at<=? ORDER BY effective_at DESC, version_no DESC LIMIT 1",
                (checklist_id, at),
            ).fetchone()
            context = f"at:{at}"
        else:
            current_id = self.connection.execute(
                "SELECT current_version_id FROM bio_checklists WHERE id=?", (checklist_id,)
            ).fetchone()[0]
            row = self.connection.execute(
                "SELECT * FROM bio_checklist_versions WHERE id=?", (current_id,)
            ).fetchone() if current_id else None
            context = "current"
        if row is None:
            raise NotFoundError("没有可查询的已发布版本")
        if row["status"] not in {"published", "withdrawn"}:
            raise ConflictError(f"版本 v{row['version_no']} 尚未发布")

        grants = {(item["scope"], item["region_code"]) for item in self.connection.execute(
            "SELECT scope,region_code FROM bio_sensitive_grants"
        ).fetchall()}
        cutoff = row["published_at"]
        items: list[dict[str, Any]] = []
        hidden_sensitive = 0
        for sp in self.connection.execute(
            "SELECT * FROM bio_checklist_species WHERE version_id=? AND is_removed=0 ORDER BY taxon_id",
            (row["id"],),
        ).fetchall():
            regions = _loads(sp["visible_region_codes"], [])
            if region and region not in regions:
                continue
            if sp["is_sensitive"]:
                scope = sp["sensitivity_scope"]
                allowed_regions = [item for item in regions if (scope, item) in grants]
                if region is not None:
                    if viewer_scope != scope or (scope, region) not in grants:
                        hidden_sensitive += 1
                        continue
                elif not allowed_regions or viewer_scope != scope:
                    hidden_sensitive += 1
                    continue
            aliases = [dict(item) for item in self.connection.execute(
                "SELECT name,name_type,language,source_citation_key,created_at FROM bio_species_names"
                " WHERE taxon_id=? AND created_at<=? ORDER BY created_at",
                (sp["taxon_id"], cutoff),
            ).fetchall()]
            item = {key: sp[key] for key in (
                "taxon_id", "scientific_name", "canonical_name", "authorship", "taxon_rank",
                "parent_taxon_id", "protection_level", "is_sensitive", "sensitivity_scope",
                "occurrence_note",
            )}
            item["visible_region_codes"] = regions
            item["citation_refs"] = _loads(sp["citation_refs"], [])
            item["aliases"] = aliases
            items.append(item)

        if keyword:
            needle = keyword.strip().lower()
            items = [
                item for item in items
                if needle in item["canonical_name"].lower()
                or needle in item["scientific_name"].lower()
                or any(needle in alias["name"].lower() for alias in item["aliases"])
            ]

        return {
            "checklist": code,
            "context": context,
            "version_no": row["version_no"],
            "version_status": row["status"],
            "effective_at": row["effective_at"],
            "region": region,
            "viewer_scope": viewer_scope,
            "hidden_sensitive_count": hidden_sensitive,
            "items": items,
        }

    # -- 序列化 --------------------------------------------------------------

    def _draft_species(self, connection: sqlite3.Connection, checklist_id: int, taxon_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM bio_checklist_species WHERE checklist_id=? AND version_id IS NULL AND taxon_id=?",
            (checklist_id, taxon_id),
        ).fetchone()
        result = dict(row)
        result["visible_region_codes"] = _loads(result["visible_region_codes"], [])
        result["citation_refs"] = _loads(result["citation_refs"], [])
        result.pop("version_id", None)
        return result

    def _version_detail(self, connection: sqlite3.Connection, version_id: int) -> dict[str, Any]:
        version = connection.execute("SELECT * FROM bio_checklist_versions WHERE id=?", (version_id,)).fetchone()
        if version is None:
            raise NotFoundError("版本不存在")
        result = dict(version)
        for key in ("validation_json", "snapshot_json"):
            result[key] = _loads(result[key], {})
        is_draft = result["status"] in {"editing", "review_pending", "review_passed", "review_rejected"}
        species_sql = (
            "SELECT * FROM bio_checklist_species WHERE checklist_id=? AND version_id IS NULL ORDER BY taxon_id"
            if is_draft
            else "SELECT * FROM bio_checklist_species WHERE version_id=? ORDER BY taxon_id"
        )
        arg = result["checklist_id"] if is_draft else version_id
        species: list[dict[str, Any]] = []
        for row in connection.execute(species_sql, (arg,)).fetchall():
            item = dict(row)
            item["visible_region_codes"] = _loads(item["visible_region_codes"], [])
            item["citation_refs"] = _loads(item["citation_refs"], [])
            species.append(item)
        result["species"] = species
        changes = []
        for row in connection.execute(
            "SELECT * FROM bio_checklist_name_changes WHERE version_id=? ORDER BY id", (version_id,)
        ).fetchall():
            item = dict(row)
            item["affected_regions"] = _loads(item["affected_regions"], [])
            changes.append(item)
        result["name_changes"] = changes
        return result

    def _record_event(
        self,
        connection: sqlite3.Connection,
        checklist_id: int | None,
        version_id: int | None,
        action: str,
        actor: str,
        detail: dict[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO bio_checklist_events(checklist_id,version_id,action,actor,detail_json,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (checklist_id, version_id, action, actor, json.dumps(detail, ensure_ascii=False), now),
        )
