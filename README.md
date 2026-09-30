# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 城市生境物种清单发布服务

`app/biodiversity/` 提供可信物种清单的版本化发布能力，服务于自然教育讲义的可追溯诉求：

- **别名与分类关系**：每个物种有接受名（canonical/scientific）与异名、俗名、曾用名（`bio_species_names`），分类通过 `parent_taxon_id` 形成纲-目-科-种的链路。
- **证据引用**：物种条目、别名都必须引用已登记的文献/观测证据（`bio_citations`）。
- **分区域可见性**：物种按区域（湿地/片区）可见；敏感物种带 `sensitivity_scope`，只有取得 `(scope, region)` 授权（`bio_sensitive_grants`）的查看者才能看到。
- **草稿 → 复核 → 发布**：编辑（editor）起草并强制填写每次名称变化原因；复核者（reviewer）通过或驳回；发布者（publisher）只能发布复核通过的版本。角色经 `X-Role` 请求头传递，操作者经 `?actor=` 传递。
- **发布前三项强制校验**：分类环是否闭合（父级缺失、成环均拒绝）、引用是否存在、敏感物种是否超出授权区域。
- **不可变版本与历史追溯**：发布时复制物种快照（`bio_checklist_species.version_id`），并生成名称变化台账（`bio_checklist_name_changes`，含变化原因与受影响区域）。旧版本可按版本号或生效日期（`/as-of`）查询。
- **撤回不抹历史**：撤回（仅 admin）只把版本标记为 `withdrawn` 并让当前生效指针回退到上一有效版本，阻止后续使用；已发布的快照与引用记录仍然可查。

多角色 API 演示（编辑 → 复核驳回/通过 → 发布 v1 → 专家修订发布 v2 → 按日期追溯 → 撤回，含分类环与敏感越权被拦截的场景）：

```bash
TOWNSHIP_DATABASE_PATH=data/bio-demo.db python -m app.cli biodiversity-demo
```

主要接口（前缀 `/api/biodiversity`）：

| 接口 | 角色 | 说明 |
| --- | --- | --- |
| `POST /regions`、`POST /citations`、`PUT /sensitive-grants` | admin | 区域、引用、敏感授权基础资料 |
| `POST /taxa/{taxon_id}/names` | 编辑 | 登记别名/曾用名及其证据引用 |
| `POST /checklists`、`PUT /checklists/{code}/species`、`POST .../species/{id}/remove` | editor | 建清单、维护草稿（必须给变化原因） |
| `POST /checklists/{code}/review-request`、`POST .../review` | editor / reviewer | 提请复核、复核通过或驳回 |
| `GET /checklists/{code}/validation` | 任意 | 预览分类环/引用/敏感授权三项校验 |
| `POST /checklists/{code}/publish` | publisher | 校验通过后发布为新版本 |
| `GET /checklists/{code}/versions`、`.../versions/{no}`、`.../as-of?date=` | 任意 | 版本列表、版本详情、按发布日期追溯 |
| `GET /checklists/{code}/name-changes/{no}`、`.../timeline` | 任意 | 名称变化原因与受影响区域、事件时间线 |
| `GET /checklists/{code}/species?region=&viewer_scope=&at=&keyword=` | 任意 | 查询当前或历史版本物种（自动过滤越权敏感种） |
| `POST /checklists/{code}/versions/{no}/withdraw` | admin | 撤回版本（阻止后续使用，保留历史） |

