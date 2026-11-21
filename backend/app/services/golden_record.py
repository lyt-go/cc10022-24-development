"""黄金记录领域服务：黄金记录创建、关联来源、置信度计算与版本管理。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GoldenRecordService(BaseModuleService):
    MODULE = "golden_record"
    ENTITY = "黄金记录"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["黄金ID", "实体模型", "关联来源", "关键字段", "代表字段", "置信度", "最后合并", "记录版本"]
    REQUIRED_FIELDS = ["黄金ID", "实体模型", "关联来源"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用记录": "已停用"}
    NEGATIVE_ACTIONS = ["停用记录"]

    def create_golden_record(self, entity_model: str, merged_data: dict[str, Any],
                               source_ids: list[str]) -> dict[str, Any]:
        """创建黄金记录：将合并后的代表记录写入黄金记录存储。

        写入黄金记录到golden_records存储，关联来源批次与合并追溯信息，
        设置置信度、关联来源和最后合并时间，生成唯一黄金ID。
        """
        rows = store.rows(self.MODULE)
        golden_id = self._new_golden_id(entity_model)
        confidence = self._compute_confidence(len(source_ids))
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "黄金ID": golden_id,
            "实体模型": entity_model,
            "关联来源": "|".join(source_ids),
            "关键字段": ",".join(k for k in merged_data if not k.startswith("_")),
            "代表字段": str(merged_data.get("representative", {})),
            "置信度": f"{confidence:.2f}",
            "最后合并": datetime.datetime.utcnow().isoformat(),
            "记录版本": "1.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "source_count": len(source_ids),
        }
        for key, val in merged_data.items():
            if not key.startswith("_"):
                entry[key] = val
        rows.append(entry)
        # 首版即为交付基线：同步生成完整快照并确认发布，渠道从第一刻起
        # 读到的就是带版本编号的发布结果。
        from app.services.mdm_version import MdmVersionService, version_lock
        version_svc = MdmVersionService()
        with version_lock():
            version_result = version_svc.snapshot_golden_version(
                golden_id, dict(merged_data), changed_by="黄金记录创建")
            published = version_svc.publish_golden_version(
                golden_id, version_result["version_number"], changed_by="黄金记录创建")
        return {"ok": True, "golden_record": entry,
                "version": published.get("version"),
                "publish_id": published.get("publish_id"),
                "version_number": published.get("version_number")}

    def update_golden_record(self, golden_id: str, updates: dict[str, Any],
                             changed_by: str | None = None) -> dict[str, Any]:
        """更新黄金记录字段并生成新版本快照（默认不自动发布）。

        字段更新与快照写入在一次调用内完成，避免渠道读到地址已换、
        区县未换的中间态；是否对外生效由显式发布决定。
        """
        from app.services.mdm_version import MdmVersionService, version_lock
        with version_lock():
            record = self._find_row(golden_id)
            if record is None:
                return {"ok": False, "reason": "黄金记录不存在"}
            applied: dict[str, Any] = {}
            for key, value in updates.items():
                if key.startswith("_") or key in ("id", "status", "pending", "abnormal"):
                    continue
                record[key] = value
                applied[key] = value
            record["最后合并"] = datetime.datetime.utcnow().isoformat()

            version_svc = MdmVersionService()
            # 快照在锁内读取整条记录：同一客户的地址与区县要么一起进新版本，
            # 要么都不进，不会和其他交接的改动拼成半新半旧。
            snapshot = version_svc.snapshot_golden_version(
                golden_id, applied, changed_by=changed_by or "黄金记录更新")
        return {
            "ok": True,
            "golden_id": golden_id,
            "updated_fields": applied,
            "version": snapshot["version"],
            "version_number": snapshot["version_number"],
            "snapshot": snapshot["snapshot"],
            "published": False,
        }

    def get_published_fields(self, golden_id: str) -> dict[str, Any] | None:
        """读取黄金记录当前已确认发布版本的字段（渠道查询唯一数据源）。"""
        from app.services.mdm_version import MdmVersionService
        return MdmVersionService().get_published_fields(golden_id)

    def _new_golden_id(self, entity_model: str) -> str:
        """生成唯一黄金ID：同一秒内多次创建也不会撞号。"""
        rows = store.rows(self.MODULE)
        existing = {str(row.get("黄金ID", "")) for row in rows}
        base = f"GR-{entity_model}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        if base not in existing:
            return base
        suffix = 2
        while f"{base}-{suffix}" in existing:
            suffix += 1
        return f"{base}-{suffix}"

    def _compute_confidence(self, source_count: int) -> float:
        """计算黄金记录置信度：基于来源数量和质量指标综合评估。"""
        base = 0.5
        source_bonus = min(source_count * 0.1, 0.3)
        return min(round(base + source_bonus, 2), 0.99)

    def query_golden_record(self, golden_id: str) -> dict[str, Any] | None:
        """查询黄金记录：按黄金ID检索golden_records存储。

        查询黄金记录的完整字段信息、关联来源列表和置信度评分，
        返回黄金记录的完整快照数据。
        """
        row = self._find_row(golden_id)
        return dict(row) if row is not None else None

    def _find_row(self, golden_id: str) -> dict[str, Any] | None:
        """返回存储中的原始行，供需要写回字段的服务复用。"""
        for row in store.rows(self.MODULE):
            if row.get("黄金ID") == golden_id:
                return row
        return None

    def link_source_to_golden(self, golden_id: str, new_source_id: str) -> dict[str, Any]:
        """关联新来源记录到已有黄金记录。

        将新增来源记录关联到已有黄金记录，更新关联来源列表，
        重新计算置信度评分并更新最后合并时间。
        """
        record = self._find_row(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        sources = record.get("关联来源", "").split("|")
        if "" in sources:
            sources.remove("")
        if new_source_id in sources:
            return {"ok": False, "reason": "来源已关联"}
        sources.append(new_source_id)
        record["关联来源"] = "|".join(sources)
        record["source_count"] = len(sources)
        record["置信度"] = f"{self._compute_confidence(len(sources)):.2f}"
        # 关联新来源记录：更新黄金记录的来源列表和置信度
        return {"ok": True, "golden_id": golden_id, "source_count": len(sources)}

    def validate_golden_completeness(self, golden_id: str,
                                      required_fields: list[str]) -> dict[str, Any]:
        """校验黄金记录的字段完整性：检查所有必填字段是否已填充。

        查询黄金记录数据，读取golden_records存储中的字段值，
        校验必填字段完整性并输出完备率统计。
        """
        record = self._find_row(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        missing = [f for f in required_fields if not str(record.get(f, "")).strip()]
        complete_count = len(required_fields) - len(missing)
        return {
            "golden_id": golden_id,
            "total_required": len(required_fields),
            "filled": complete_count,
            "missing": missing,
            "completeness": round(complete_count / max(len(required_fields), 1) * 100, 2),
        }

    def search_golden_records(self, entity_model: str | None = None,
                                min_confidence: float = 0.0) -> list[dict[str, Any]]:
        """搜索黄金记录：按实体模型和最低置信度筛选。

        查询golden_records存储，按实体模型筛选并过滤置信度
        低于阈值的记录，返回符合条件的黄金记录列表。
        """
        rows = store.rows(self.MODULE)
        results = [
            row for row in rows
            if row.get("status") == "已生效"
        ]
        if entity_model:
            results = [r for r in results if r.get("实体模型") == entity_model]
        results = [r for r in results if float(r.get("置信度", "0")) >= min_confidence]
        # 查询黄金记录：筛选已生效、匹配实体模型且置信度达标的记录
        results.sort(key=lambda r: float(r.get("置信度", "0")), reverse=True)
        return results

    def export_golden_records(self, entity_model: str) -> list[dict[str, Any]]:
        """导出黄金记录：将指定实体模型的黄金记录导出供下游系统消费。

        查询golden_records存储中已生效的黄金记录，导出完整的
        记录数据，包含关键字段、代表字段和置信度信息。
        """
        records = self.search_golden_records(entity_model=entity_model, min_confidence=0.5)
        # 导出黄金记录：将已生效记录数据导出供下游系统分发同步
        exported = []
        for r in records:
            exported.append({
                "golden_id": r.get("黄金ID"),
                "entity_model": r.get("实体模型"),
                "confidence": float(r.get("置信度", "0")),
                "sources": (r.get("关联来源", "")).split("|"),
                "key_fields": (r.get("关键字段", "")).split(","),
            })
        return exported