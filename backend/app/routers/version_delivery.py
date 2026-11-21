"""版本发布与交付接口：发布、交付确认、渠道查询和发布历史复算。

路由层只做参数校验和分派，所有状态判断在 app/services 领域服务里完成。
本路由必须注册在通用资源路由之前，否则 /api/mdm_version/publish 会被
/api/{module}/{entry_id} 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.mdm_api import MdmApiService
from app.services.mdm_version import MdmVersionService

router = APIRouter(prefix="/api", tags=["版本发布与交付"])


class PublishPayload(BaseModel):
    golden_id: str
    version_number: str | None = None
    changed_by: str | None = None
    channel: str | None = None


class DeliveryPayload(BaseModel):
    golden_id: str
    publish_id: str | None = None
    version_number: str | None = None
    channel: str = "默认渠道"


class VerifyPayload(BaseModel):
    golden_id: str
    delivered: dict[str, Any] | None = None
    publish_id: str | None = None
    version_number: str | None = None


class ChannelQueryPayload(BaseModel):
    golden_id: str
    channel: str = "默认渠道"
    filters: dict[str, str] = Field(default_factory=dict)


class SyncPublishPayload(BaseModel):
    golden_id: str
    version_number: str | None = None
    channel: str | None = None


def _version_service() -> MdmVersionService:
    return MdmVersionService()


@router.post("/mdm_version/publish")
def publish_version(payload: PublishPayload) -> dict[str, Any]:
    """确认发布一个黄金记录版本；同一黄金ID只保留一个当前发布版本。"""
    result = _version_service().publish_golden_version(
        payload.golden_id, payload.version_number,
        changed_by=payload.changed_by, channel=payload.channel)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "版本发布失败"))
    return result


@router.post("/mdm_version/delivery")
def confirm_delivery(payload: DeliveryPayload) -> dict[str, Any]:
    """确认交付：把当前发布版本的交付快照交给渠道并返回回执。"""
    result = _version_service().confirm_version_delivery(
        payload.golden_id, payload.publish_id,
        channel=payload.channel, version_number=payload.version_number)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "交付确认失败"))
    return result


@router.get("/mdm_version/{golden_id}/published")
def get_published_version(golden_id: str) -> dict[str, Any]:
    """当前发布版本 + 交付快照字段。"""
    service = _version_service()
    version = service.get_published_version(golden_id)
    if version is None:
        raise HTTPException(status_code=404, detail="没有已确认发布的版本")
    return {
        "ok": True,
        "golden_id": golden_id,
        "publish_id": version.get("发布编号"),
        "version_number": version.get("版本号"),
        "fields": service.get_published_fields(golden_id) or {},
        "snapshot": version.get("交付快照"),
        "delivery_status": version.get("交付状态"),
    }


@router.get("/mdm_version/{golden_id}/history")
def get_publish_history(golden_id: str) -> dict[str, Any]:
    """发布历史：按版本号降序返回，供渠道复算。"""
    history = _version_service().version_publish_history(golden_id)
    return {"ok": True, "golden_id": golden_id, "items": history, "total": len(history)}


@router.get("/mdm_version/{golden_id}/snapshot")
def get_delivery_snapshot(
    golden_id: str,
    publish_id: str | None = Query(default=None),
    version_number: str | None = Query(default=None),
) -> dict[str, Any]:
    """查询黄金版本交付快照：可按发布编号或版本号定位。"""
    snapshot = _version_service().query_delivery_snapshot(
        golden_id, publish_id=publish_id, version_number=version_number)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="交付快照不存在")
    return {"ok": True, "golden_id": golden_id, "snapshot": snapshot}


@router.post("/mdm_version/verify")
def verify_delivered(payload: VerifyPayload) -> dict[str, Any]:
    """核对一次实际交付结果能否按发布历史逐字段复算。"""
    return _version_service().verify_delivered_result(
        payload.golden_id, payload.delivered,
        publish_id=payload.publish_id, version_number=payload.version_number)


@router.post("/mdm_api/query")
def query_by_api(payload: ChannelQueryPayload) -> dict[str, Any]:
    """渠道查询：只读当前发布快照，返回接口约定的标准化字段。"""
    result = MdmApiService().query_published_fields_for_channel(
        payload.golden_id, channel=payload.channel, filters=payload.filters)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "未查询到发布结果"))
    return result


@router.post("/mdm_api/publish")
def publish_for_channel(payload: SyncPublishPayload) -> dict[str, Any]:
    """发布并交付：查询接口用同一发布结果返回给渠道。"""
    result = MdmApiService().publish_golden_version_for_channel(
        payload.golden_id, payload.version_number, channel=payload.channel)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("reason", "版本发布失败"))
    return result
