"""查询接口领域服务：黄金记录查询、字段标准化输出与调用统计。

渠道查询只读“已确认发布版本的交付快照”，不读黄金记录表的实时字段：
同一黄金ID只有一个发布版本，返回的每个字段都属于同一版本，刷新前后一致。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 随交付字段一起返回的版本标识别名，便于渠道按发布历史复算。
_PUBLISH_META_FIELDS = ("发布编号", "版本号", "快照编号")


class MdmApiService(BaseModuleService):
    MODULE = "mdm_api"
    ENTITY = "查询接口"
    OPERATION_TERM = "标准化"
    LIST_FIELDS = ["接口编号", "实体模型", "查询字段", "返回字段", "缓存时长", "限流阈值", "调用量统计", "接口版本"]
    REQUIRED_FIELDS = ["接口编号", "实体模型", "查询字段"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
    ACTION_RULES = {"提交校验": "待校验", "发布接口": "已发布", "下线接口": "已下线"}
    NEGATIVE_ACTIONS = ["下线接口"]

    def query_golden_records_by_api(self, api_id: int,
                                      filters: dict[str, str] | None = None) -> dict[str, Any]:
        """按查询接口定义查询黄金记录：标准化字段输出。

        读取查询接口配置，按查询字段标准对**已发布交付快照**执行检索，
        标准化返回字段格式并记录调用量统计。
        """
        api_def = self.get_entry(api_id)
        if not api_def or api_def.get("status") != "已发布":
            return {"ok": False, "reason": "查询接口不可用"}
        entity_model = api_def.get("实体模型", "")
        query_field = api_def.get("查询字段", "")
        records = self.published_records_for_entity(entity_model)
        if filters and query_field:
            records = [
                r for r in records
                if filters.get(query_field, "").casefold() in str(r.get(query_field, "")).casefold()
            ]
        records = [self._project(r, api_def) for r in records]
        self._record_api_call(api_id)
        return {
            "ok": True,
            "records": records,
            "count": len(records),
            "api_id": api_def.get("接口编号"),
            "source": "发布快照",
            "version_numbers": sorted({str(r.get("版本号", "")) for r in records}),
        }

    def published_records_for_entity(self, entity_model: str | None = None) -> list[dict[str, Any]]:
        """返回指定实体模型下每个黄金记录当前发布版本的完整字段。

        同一条黄金ID只取唯一的已确认版本，字段整体来自该版本的交付快照，
        不会把不同版本的地址、区县拼在一起。
        """
        from app.services.mdm_version import MdmVersionService
        version_svc = MdmVersionService()
        results: list[dict[str, Any]] = []
        for record in store.rows("golden_record"):
            if entity_model and record.get("实体模型") != entity_model:
                continue
            golden_id = record.get("黄金ID")
            if not golden_id:
                continue
            version = version_svc.get_published_version(golden_id)
            if version is None:
                continue
            fields = version_svc.get_version_fields(version)
            fields.setdefault("黄金ID", golden_id)
            fields["发布编号"] = version.get("发布编号", "")
            fields["版本号"] = version.get("版本号", "")
            fields["快照编号"] = version.get("快照编号", "")
            results.append(fields)
        return results

    def publish_golden_version_for_channel(self, golden_id: str,
                                           version_number: str | None = None,
                                           channel: str | None = None) -> dict[str, Any]:
        """把一次版本发布结果直接交付给渠道（发布 + 渠道读取同一快照）。"""
        from app.services.mdm_version import MdmVersionService
        version_svc = MdmVersionService()
        published = version_svc.publish_golden_version(
            golden_id, version_number, changed_by="查询接口发布", channel=channel)
        if not published["ok"]:
            return published
        return {
            "ok": True,
            "golden_id": golden_id,
            "publish_id": published["publish_id"],
            "version_number": published["version_number"],
            "channel": channel or "",
            "fields": published["fields"],
            "snapshot": published["snapshot"],
        }

    def query_published_fields_for_channel(self, golden_id: str,
                                           channel: str = "默认渠道",
                                           filters: dict[str, str] | None = None) -> dict[str, Any]:
        """渠道查询入口：只返回当前发布版本的字段并给出复算依据。"""
        from app.services.mdm_version import MdmVersionService
        version_svc = MdmVersionService()
        fields = version_svc.get_published_fields(golden_id)
        if fields is None:
            return {"ok": False, "golden_id": golden_id, "reason": "没有已确认发布的版本"}
        version = version_svc.get_published_version(golden_id) or {}
        if filters and any(str(fields.get(k, "")) != str(v) for k, v in filters.items()):
            return {"ok": False, "golden_id": golden_id, "reason": "未匹配到记录"}
        return {
            "ok": True,
            "golden_id": golden_id,
            "channel": channel,
            "publish_id": version.get("发布编号"),
            "version_number": version.get("版本号"),
            "snapshot_id": version.get("快照编号"),
            "fields": fields,
            "snapshot": version.get("交付快照"),
            "source": "发布快照",
        }

    def _project(self, record: dict[str, Any], api_def: dict[str, Any]) -> dict[str, Any]:
        """按接口约定裁剪字段，并保留发布标识以便复算。"""
        return_fields = [
            f.strip() for f in api_def.get("返回字段", "").split(",") if f.strip()
        ]
        if not return_fields:
            return dict(record)
        projected = {f: record.get(f) for f in return_fields if f in record}
        for meta_field in _PUBLISH_META_FIELDS:
            if meta_field not in projected and meta_field in record:
                projected[meta_field] = record[meta_field]
        return projected


    def _record_api_call(self, api_id: int) -> None:
        """记录查询接口调用量统计。"""
        api_def = self.get_entry(api_id)
        if api_def:
            current = api_def.get("调用量统计", "0")
            api_def["调用量统计"] = str(int(current) + 1)

    def query_list_active_apis(self, entity_model: str | None = None) -> list[dict[str, Any]]:
        """查询活跃查询接口列表：按实体模型筛选已发布接口。

        遍历查询接口存储，检索已发布状态的查询接口定义，
        包含查询字段、返回字段、缓存时长和限流阈值配置。
        """
        rows = store.rows(self.MODULE)
        apis = [row for row in rows if row.get("status") == "已发布"]
        if entity_model:
            apis = [a for a in apis if a.get("实体模型") == entity_model]
        # 查询活跃查询接口列表：筛选已发布且匹配实体模型的接口
        return apis

    def validate_api_rate_limit(self, api_id: int) -> dict[str, Any]:
        """校验查询接口限流状态：检查当前调用量是否超过限流阈值。

        读取查询接口的调用量统计和限流阈值，校验是否触发
        限流保护并输出当前调用状态报告。
        """
        api_def = self.get_entry(api_id)
        if not api_def:
            return {"ok": False, "reason": "接口不存在"}
        call_count = int(api_def.get("调用量统计", "0"))
        rate_limit = int(api_def.get("限流阈值", "100"))
        is_limited = call_count >= rate_limit
        return {
            "api_id": api_def.get("接口编号"),
            "call_count": call_count,
            "rate_limit": rate_limit,
            "is_limited": is_limited,
            "remaining": max(rate_limit - call_count, 0),
        }

    def export_api_response(self, api_id: int,
                              format_type: str = "json") -> list[dict[str, Any]]:
        """导出查询接口响应：导出的是同一份发布快照结果。

        标准化查询接口的输出格式，将已发布版本的交付字段按字段约定
        转换为JSON/CSV格式供下游系统消费。
        """
        api_def = self.get_entry(api_id)
        if not api_def:
            return []
        records = self.published_records_for_entity(api_def.get("实体模型", ""))
        return [self._project(record, api_def) for record in records]