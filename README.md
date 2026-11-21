# 主数据清洗与黄金记录平台

面向多源主数据的匹配识别、冲突消解、合并清洗与黄金记录管理的主数据治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 来源系统注册 | `source_system` | 来源系统 | 系统编号、系统名称、系统类型 |
| 实体模型定义 | `entity_model` | 实体模型 | 模型编号、模型名称、业务域 |
| 匹配规则引擎 | `match_rule` | 匹配规则 | 规则编号、实体模型、匹配字段 |
| 疑似重复簇 | `dup_cluster` | 重复簇 | 簇编号、实体模型、簇大小 |
| 合并追溯日志 | `merge_log` | 合并记录 | 记录编号、黄金ID、来源记录 |
| 黄金记录管理 | `golden_record` | 黄金记录 | 黄金ID、实体模型、关联来源 |
| 字段标准化清洗 | `field_cleanse` | 清洗规则 | 规则编号、实体模型、目标字段 |
| 优胜策略配置 | `survivorship_rule` | 优胜策略 | 策略编号、实体模型、选择字段 |
| 数据入库规则 | `data_import_rule` | 入库规则 | 规则编号、来源系统、实体模型 |
| 批次导入管理 | `import_batch` | 导入批次 | 批次编号、来源系统、实体模型 |
| 记录级血缘 | `record_lineage` | 血缘记录 | 血缘编号、黄金ID、来源记录 |
| 主数据分发同步 | `mdm_sync` | 同步配置 | 配置编号、黄金实体、目标系统 |
| 黄金记录版本 | `mdm_version` | 版本记录 | 版本编号、黄金ID、版本号 |
| 自动匹配任务 | `auto_match_job` | 匹配任务 | 任务编号、实体模型、匹配规则 |
| 主数据质量统计 | `mdm_quality` | 质量统计 | 统计编号、实体模型、完备率 |
| 参考数据管理 | `mdm_reference` | 参考数据 | 数据编号、数据类别、标准编码 |
| 主数据查询接口 | `mdm_api` | 查询接口 | 接口编号、实体模型、查询字段 |
| 主数据治理看板 | `mdm_dashboard` | 看板配置 | 看板编号、看板名称、监控实体 |

## 黄金记录版本发布与交付确认

黄金记录版本的写入口径统一在 `backend/app/services/mdm_version.py`：

- **完整快照**：每次版本写入都把整条黄金记录的字段与版本编号一起持久化
  （`快照字段`），并在 `交付快照.字段` 中冻结一份不可变副本；`旧值摘要/新值摘要`
  只作为变更说明，渠道不再依赖它取数。
- **唯一发布版本**：同一黄金ID最多只有一个 `发布状态=已发布` 的版本，发布新版本
  会把旧版本降为 `历史发布`；发布、回退、交付确认和黄金记录更新共用一把锁，
  两段交接并发时不会各自发布出一个“当前版本”。
- **交付确认**：只有当前发布版本可以确认交付，交付后写入 `交付状态/交付渠道/交付时间`
  并返回带 `发布编号`、`版本号`、`快照编号`、`快照摘要` 的回执。
- **渠道查询**：主数据查询接口和分发同步都只读当前发布版本的交付快照，
  每条结果都带发布标识；同一黄金记录不会把新地址和旧区县拼在一起。
- **可复算**：`recompute_published_result` 仅凭发布历史重建交付字段并校验摘要，
  `verify_delivered_result` 逐字段核对渠道实际拿到的记录能否复算。

对应接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/mdm_version/publish` | 确认发布（可指定 `version_number`） |
| POST | `/api/mdm_version/delivery` | 交付确认并返回回执 |
| GET | `/api/mdm_version/{golden_id}/published` | 当前发布版本及交付字段 |
| GET | `/api/mdm_version/{golden_id}/history` | 发布历史（版本号降序） |
| GET | `/api/mdm_version/{golden_id}/snapshot` | 按 `publish_id`/`version_number` 查交付快照 |
| POST | `/api/mdm_version/verify` | 核对实际交付结果能否按发布历史复算 |
| POST | `/api/mdm_api/query` | 渠道查询：只读当前发布快照 |
| POST | `/api/mdm_api/publish` | 发布并用同一发布结果返回给渠道 |

“黄金记录版本”工作台也提供 `确认发布`、`交付确认`、`标记历史`、`执行回滚` 动作。

测试：`backend/.venv/bin/python app/tests/test_version_delivery.py`（版本发布与交付链路）
与 `backend/.venv/bin/python app/tests/test_business_chains.py`（原有 6 条业务链路）。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
