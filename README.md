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

面向自然教育讲解员的可信物种清单边界（`app/biodiversity/`，路由前缀 `/api/biodiversity`）。

### 发布工作流

清单版本生命周期为 `draft → in_review → approved → published`，复核驳回回到 `draft`；当前生效版本可 `withdrawn`（撤回）。

1. 管理员先维护基础资料：区域（`/regions`）、证据引用（`/references`）与分区域敏感物种授权（`/sensitive-grants`，`region_code='*'` 为全局兜底）。
2. 编辑员（`biodiversity_editor`）创建草稿，可选择 `?base_version=<版本号>` 以某已发布版本为基准整体拷贝；逐条维护学名、别名、父分类、保护等级、敏感等级、可见区域和证据引用，每个条目必须写明变更原因。
3. 编辑员提交复核；复核员（`biodiversity_reviewer`）不能复核自己提交的草稿（职责分离，管理员也不豁免）。
4. 复核与发布时执行三道发布关，违例一次性聚合返回（422）：
   - **分类环闭合**：沿父分类边做环检测，自指与闭环均拒绝（如 A→B→A）；
   - **引用存在**：每条证据引用必须先在引用库登记；
   - **敏感物种授权**：物种敏感等级不得高于其每个可见区域的授权等级。
5. 发布时把草稿整体快照为不可变版本（`bio_published_entries`），并与上一版本逐项 diff，把每个名称变化（改名、新增/弃用别名、保护等级调整、区域调整、新增、移除）连同**原因与受影响区域**写入 `bio_name_changes`。
6. 撤回只把版本标记为 `withdrawn`，**不删除任何快照与变更轨迹**：`/effective` 自动回落到上一版本，已引用该版本的讲义仍可通过 `/versions/{no}` 与 `/effective?as_of=<ISO时间>` 原样追溯。

### 多角色 API 演示

```bash
python -m app.cli bio-demo
```

演示在独立库 `data/bio-demo.db` 上走完整流程：管理员建档授权 → 编辑员起草 v1 → 复核员发布 → 起草 v2 时故意制造“引用缺失 + 敏感越权 + 分类修订”，展示复核环节的聚合阻断、补证与授权后重新发布、分区域可见性、按发布日期回溯旧版本、名称（含别名/曾用名）溯源，以及撤回后“阻止后续使用但保留历史”。

### 主要接口

| 接口 | 说明 |
| --- | --- |
| `POST/GET /regions`、`/references` | 区域与证据引用维护 |
| `PUT/GET /sensitive-grants` | 分区域敏感物种授权 |
| `POST /checklists?base_version=` | 创建草稿（可基于旧版） |
| `PUT/DELETE /checklists/{id}/entries/{key}` | 维护草稿条目 |
| `POST /checklists/{id}/submit` `/review` `/publish` `/withdraw` | 提交、复核、发布、撤回 |
| `GET /checklists/{id}/validation` `/events` | 发布三关预检、版本事件流 |
| `GET /versions/{no}` `/versions/{no}/name-changes` | 历史版本快照与名称变更轨迹 |
| `GET /effective?as_of=&region=` | 当前（或某时刻、某区域）生效版本 |
| `GET /taxa/{key}`、`/names?q=` | 物种名称溯源与别名检索 |
