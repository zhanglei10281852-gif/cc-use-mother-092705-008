"""物种清单发布服务的 SQLite 存储边界。

表结构与服务逻辑解耦：服务层只依赖本模块提供的只读查询和建表语句，
所有写操作集中在 service 的事务中完成，保证草稿、快照与审计一致提交。
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS bio_regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bio_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('文献','标本','影像','数据库','专家意见')),
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '',
    published_on TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '有效' CHECK (status IN ('有效','停用')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 敏感物种授权：某区域（region_code 为 * 表示全局）被授权展示的敏感等级。
-- 清单发布时逐区域比对：物种敏感等级 > 区域授权等级即越权。
CREATE TABLE IF NOT EXISTS bio_sensitive_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    region_code TEXT NOT NULL,
    max_sensitive_level INTEGER NOT NULL CHECK (max_sensitive_level BETWEEN 0 AND 3),
    granted_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(region_code)
);

-- 物种主档：保存跨版本稳定的分类键与当前在册状态。
CREATE TABLE IF NOT EXISTS bio_taxa (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taxon_key TEXT NOT NULL UNIQUE,
    current_scientific_name TEXT NOT NULL,
    parent_key TEXT,
    taxon_rank TEXT NOT NULL CHECK (taxon_rank IN ('界','门','纲','目','科','属','种')),
    protection_level TEXT NOT NULL DEFAULT '',
    sensitive_level INTEGER NOT NULL DEFAULT 0 CHECK (sensitive_level BETWEEN 0 AND 3),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged','removed')),
    merged_into_key TEXT,
    first_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 别名（含曾用名）：名称变更可追溯的基础。
CREATE TABLE IF NOT EXISTS bio_taxon_names (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taxon_key TEXT NOT NULL REFERENCES bio_taxa(taxon_key),
    name TEXT NOT NULL,
    name_kind TEXT NOT NULL CHECK (name_kind IN ('学名','别名','曾用名')),
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
    change_reason TEXT NOT NULL DEFAULT '',
    first_version INTEGER,
    last_version INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(taxon_key, name)
);
CREATE INDEX IF NOT EXISTS idx_bio_names_name ON bio_taxon_names(name);

-- 清单版本：草稿/复核中/已发布/已撤回的生命周期载体。
CREATE TABLE IF NOT EXISTS bio_checklists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','in_review','approved','published','withdrawn')),
    title TEXT NOT NULL DEFAULT '',
    change_summary TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    submitted_by TEXT NOT NULL DEFAULT '',
    submitted_at TEXT,
    review_conclusion TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    published_at TEXT,
    withdrawn_at TEXT,
    withdraw_reason TEXT NOT NULL DEFAULT '',
    withdrawn_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 草稿/复核态的条目工作副本。
CREATE TABLE IF NOT EXISTS bio_draft_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_id INTEGER NOT NULL REFERENCES bio_checklists(id) ON DELETE CASCADE,
    taxon_key TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    parent_key TEXT,
    taxon_rank TEXT NOT NULL,
    protection_level TEXT NOT NULL DEFAULT '',
    sensitive_level INTEGER NOT NULL DEFAULT 0,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    region_codes_json TEXT NOT NULL DEFAULT '[]',
    reference_codes_json TEXT NOT NULL DEFAULT '[]',
    change_type TEXT NOT NULL CHECK (change_type IN ('新增','修订','拆分','合并','保留','移除')),
    change_reason TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    UNIQUE(checklist_id, taxon_key)
);

-- 已发布版本的不可变条目快照。撤回不会删除这些记录，因此历史讲义永久可追溯。
CREATE TABLE IF NOT EXISTS bio_published_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_id INTEGER NOT NULL REFERENCES bio_checklists(id),
    version_no INTEGER NOT NULL,
    taxon_key TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    parent_key TEXT,
    taxon_rank TEXT NOT NULL,
    protection_level TEXT NOT NULL DEFAULT '',
    sensitive_level INTEGER NOT NULL DEFAULT 0,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    region_codes_json TEXT NOT NULL DEFAULT '[]',
    reference_codes_json TEXT NOT NULL DEFAULT '[]',
    change_type TEXT NOT NULL,
    change_reason TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bio_published_version ON bio_published_entries(version_no);
CREATE INDEX IF NOT EXISTS idx_bio_published_taxon ON bio_published_entries(taxon_key, version_no);

-- 名称变更轨迹：发布时为每个发生变化的名称写一行，记录原因与受影响区域。
CREATE TABLE IF NOT EXISTS bio_name_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no INTEGER NOT NULL,
    taxon_key TEXT NOT NULL,
    change_kind TEXT NOT NULL CHECK (change_kind IN ('新增物种','改名','新增别名','弃用别名','保护等级调整','区域调整','移除')),
    old_name TEXT NOT NULL DEFAULT '',
    new_name TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    affected_regions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bio_name_changes_version ON bio_name_changes(version_no);

-- 版本级事件流（编辑/提交/复核/发布/撤回）。
CREATE TABLE IF NOT EXISTS bio_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checklist_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bio_events_version ON bio_events(version_no, id);
"""


def ensure_schema(connection) -> None:
    connection.executescript(SCHEMA)


def get_region(connection, code: str):
    return connection.execute("SELECT * FROM bio_regions WHERE code=?", (code,)).fetchone()


def list_regions(connection, *, active_only: bool = False):
    sql = "SELECT * FROM bio_regions"
    if active_only:
        sql += " WHERE is_active=1"
    return connection.execute(sql + " ORDER BY code").fetchall()


def get_reference(connection, code: str):
    return connection.execute("SELECT * FROM bio_references WHERE code=?", (code,)).fetchone()


def list_references(connection):
    return connection.execute("SELECT * FROM bio_references ORDER BY code").fetchall()


def grant_map(connection) -> dict[str, int]:
    """返回 区域代码 -> 该区域允许的最高敏感等级；'*' 为兜底全局授权。"""
    rows = connection.execute("SELECT region_code, max_sensitive_level FROM bio_sensitive_grants").fetchall()
    return {row["region_code"]: int(row["max_sensitive_level"]) for row in rows}


def latest_version_no(connection) -> int:
    row = connection.execute("SELECT MAX(version_no) AS m FROM bio_checklists").fetchone()
    return int(row["m"] or 0)


def get_checklist(connection, checklist_id: int):
    return connection.execute("SELECT * FROM bio_checklists WHERE id=?", (checklist_id,)).fetchone()


def get_checklist_by_version(connection, version_no: int):
    return connection.execute("SELECT * FROM bio_checklists WHERE version_no=?", (version_no,)).fetchone()


def list_checklists(connection, *, status: str | None = None):
    if status:
        return connection.execute(
            "SELECT * FROM bio_checklists WHERE status=? ORDER BY version_no DESC", (status,)
        ).fetchall()
    return connection.execute("SELECT * FROM bio_checklists ORDER BY version_no DESC").fetchall()


def draft_entries(connection, checklist_id: int):
    return connection.execute(
        "SELECT * FROM bio_draft_entries WHERE checklist_id=? ORDER BY taxon_key", (checklist_id,)
    ).fetchall()


def get_draft_entry(connection, checklist_id: int, taxon_key: str):
    return connection.execute(
        "SELECT * FROM bio_draft_entries WHERE checklist_id=? AND taxon_key=?",
        (checklist_id, taxon_key),
    ).fetchone()


def published_entries(connection, version_no: int):
    return connection.execute(
        "SELECT * FROM bio_published_entries WHERE version_no=? ORDER BY taxon_key", (version_no,)
    ).fetchall()


def name_changes(connection, version_no: int):
    return connection.execute(
        "SELECT * FROM bio_name_changes WHERE version_no=? ORDER BY id", (version_no,)
    ).fetchall()


def checklist_events(connection, checklist_id: int):
    return connection.execute(
        "SELECT * FROM bio_events WHERE checklist_id=? ORDER BY id", (checklist_id,)
    ).fetchall()
