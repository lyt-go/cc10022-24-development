"""版本管理领域服务：黄金记录版本快照、发布确认、交付快照与版本回退。

黄金记录的每一次变更都会生成一份**完整**版本快照（不再只保存新旧值摘要），
版本发布后快照被冻结为“交付快照”。渠道只能读取已确认发布版本的交付快照，
因此同一黄金ID永远只存在一个当前发布版本，渠道拿到的字段要么全部来自旧版本，
要么全部来自新版本，不会出现地址取新值、区县取旧值的拼接结果。
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import threading
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 发布、回退、交付确认都是“读当前状态 -> 改多行 -> 写回”，必须串行化，
# 否则同一客户的两次交接可能各自发布出一个“当前版本”。
# 黄金记录的“改字段 + 生成快照”也复用同一把锁，保证快照是一次完整写入。
_PUBLISH_LOCK = threading.RLock()


def version_lock() -> threading.RLock:
    """跨服务共享的版本临界区（黄金记录更新与版本发布必须互斥）。"""
    return _PUBLISH_LOCK

# 框架字段与“当前发布指针”不进入交付快照：指针随版本发布变化，
# 若混进字段里，渠道会看到某版本的字段中写着另一版本的发布编号。
_FRAMEWORK_KEYS = {
    "id", "status", "pending", "abnormal",
    "发布编号", "当前版本编号", "记录版本",
}


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


def _business_fields(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    return {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if not key.startswith("_") and key not in _FRAMEWORK_KEYS
    }


def _digest(fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_version(value: Any) -> str:
    """把 2 / 2.0 / '2' 统一成 '2.0'，非数字版本号保持原样。"""
    text = str(value if value is not None else "").strip()
    try:
        return f"{float(text):.1f}"
    except (TypeError, ValueError):
        return text


def _version_sort_value(value: Any) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


class MdmVersionService(BaseModuleService):
    MODULE = "mdm_version"
    ENTITY = "版本记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "版本编号", "黄金ID", "版本号", "变更字段", "旧值摘要", "新值摘要", "变更批次",
        "发布编号", "发布状态", "交付状态", "版本时间",
    ]
    REQUIRED_FIELDS = ["版本编号", "黄金ID", "版本号"]
    STATUS_ORDER = ["草稿", "当前版本", "历史版本", "已回滚"]
    ACTION_RULES = {
        "设为当前": "当前版本",
        "确认发布": "当前版本",
        "发布版本": "当前版本",
        "交付确认": "当前版本",
        "标记历史": "历史版本",
        "执行回滚": "已回滚",
    }
    NEGATIVE_ACTIONS = []

    # ── 快照 ────────────────────────────────────────────────

    def snapshot_golden_version(self, golden_id: str,
                                record_data: dict[str, Any],
                                changed_by: str | None = None) -> dict[str, Any]:
        """存储黄金记录版本快照，字段内容与版本编号在同一条记录中持久化。

        以当前黄金记录为基线、叠加本次提交的字段，得到一份完整字段快照；
        同时生成变更字段的新旧值对比。快照写入时即冻结字段内容，后续业务表
        如何更新都不会影响这份快照，渠道可以按版本号复算。
        """
        with _PUBLISH_LOCK:
            from app.services.golden_record import GoldenRecordService
            current = GoldenRecordService().query_golden_record(golden_id)
            base_fields = _business_fields(current)
            incoming = _business_fields(record_data)
            snapshot_fields = {**copy.deepcopy(base_fields), **incoming}

            changed_fields: list[str] = []
            old_summary: dict[str, str] = {}
            new_summary: dict[str, str] = {}
            for key, new_val in snapshot_fields.items():
                old_val = base_fields.get(key)
                if str(old_val if old_val is not None else "") != str(new_val if new_val is not None else ""):
                    changed_fields.append(key)
                    old_summary[key] = str(old_val if old_val is not None else "")
                    new_summary[key] = str(new_val if new_val is not None else "")

            version_num = self._next_version(golden_id)
            version_id = f"VER-{golden_id}-{version_num}"
            snapshot_id = f"GVS-{golden_id}-{version_num}"
            created_at = _now()
            snapshot_digest = _digest(snapshot_fields)

            delivery_snapshot = {
                "快照编号": snapshot_id,
                "黄金ID": golden_id,
                "版本编号": version_id,
                "版本号": version_num,
                "字段": copy.deepcopy(snapshot_fields),
                "快照摘要": snapshot_digest,
                "生成时间": created_at,
                "发布编号": "",
                "发布状态": "未发布",
                "发布人": changed_by or "",
                "发布时间": "",
                "交付状态": "未交付",
                "交付渠道": "",
                "交付时间": "",
            }
            entry = {
                "id": max((int(r.get("id", 0)) for r in store.rows(self.MODULE)), default=0) + 1,
                "版本编号": version_id,
                "黄金ID": golden_id,
                "版本号": version_num,
                "变更字段": ",".join(changed_fields),
                "旧值摘要": json.dumps(old_summary, ensure_ascii=False) if old_summary else "",
                "新值摘要": json.dumps(new_summary, ensure_ascii=False) if new_summary else "",
                "变更批次": changed_by or golden_id,
                "版本时间": created_at,
                "快照编号": snapshot_id,
                "快照字段": copy.deepcopy(snapshot_fields),
                "快照摘要": snapshot_digest,
                "发布编号": "",
                "发布状态": "未发布",
                "是否当前发布": False,
                "已确认": False,
                "交付状态": "未交付",
                "交付渠道": "",
                "交付时间": "",
                "交付快照": delivery_snapshot,
                "status": "草稿",
                "pending": True,
                "abnormal": False,
            }
            # 字段与版本编号一次性写入，读取方不会看到“只有摘要的版本”。
            store.rows(self.MODULE).append(entry)
            return {
                "ok": True,
                "version": entry,
                "changed_fields": changed_fields,
                "version_number": version_num,
                "version_id": version_id,
                "snapshot_id": snapshot_id,
                "snapshot": copy.deepcopy(delivery_snapshot),
                "fields": copy.deepcopy(snapshot_fields),
            }

    def create_version_snapshot(self, golden_id: str,
                                fields: dict[str, Any] | None = None,
                                changed_by: str | None = None) -> dict[str, Any]:
        """按黄金记录当前数据（或显式字段）生成一份版本快照。"""
        if fields is None:
            from app.services.golden_record import GoldenRecordService
            record = GoldenRecordService().query_golden_record(golden_id)
            fields = _business_fields(record)
        return self.snapshot_golden_version(golden_id, fields, changed_by=changed_by)

    # ── 发布 ────────────────────────────────────────────────

    def publish_golden_version(self, golden_id: str,
                               version_number: str | None = None,
                               changed_by: str | None = None,
                               channel: str | None = None) -> dict[str, Any]:
        """确认发布一个版本：同一黄金ID最多只能有一个当前发布版本。

        发布动作在一个临界区里完成：冻结交付快照、生成发布编号、把旧的当前版本
        降级为历史版本、更新黄金记录上的记录版本。渠道随后读取本方法返回的
        同一份快照，因此刷新前后字段与发布历史严格一致。
        """
        with _PUBLISH_LOCK:
            rows = store.rows(self.MODULE)
            candidates = [row for row in rows if row.get("黄金ID") == golden_id]

            target: dict[str, Any] | None = None
            explicit_version = str(version_number).strip() if version_number not in (None, "") else ""
            if explicit_version:
                normalized = _normalize_version(explicit_version)
                target = next(
                    (row for row in candidates
                     if _normalize_version(row.get("版本号")) in (explicit_version, normalized)),
                    None,
                )
                if target is None:
                    return {"ok": False, "reason": "目标版本不存在", "golden_id": golden_id,
                            "version_number": explicit_version}
            else:
                # 未指定版本时优先发布最新一个尚未发布的版本（典型场景：字段更新
                # 生成新快照后发布）；没有待发布版本时才复用当前发布版本。
                unpublished = [
                    row for row in candidates
                    if row.get("发布状态") != "已发布"
                ]
                current = self._current_published_row(golden_id)
                if unpublished:
                    target = max(unpublished, key=lambda row: _version_sort_value(row.get("版本号")))
                elif current is not None:
                    return self._publish_result(current, republished=True)
                elif candidates:
                    target = max(candidates, key=lambda row: _version_sort_value(row.get("版本号")))
                else:
                    from app.services.golden_record import GoldenRecordService
                    if GoldenRecordService().query_golden_record(golden_id) is None:
                        return {"ok": False, "golden_id": golden_id,
                                "reason": "黄金记录不存在，无法发布"}
                    created = self.create_version_snapshot(golden_id, changed_by=changed_by)
                    target = created["version"]
                    candidates = [row for row in store.rows(self.MODULE)
                                  if row.get("黄金ID") == golden_id]

            publish_id = target.get("发布编号") or f"PUB-{golden_id}-{target.get('版本号')}"
            published_at = _now()
            snapshot = target.get("交付快照")
            if not isinstance(snapshot, dict):
                snapshot_fields = _business_fields(target.get("快照字段") or {})
                if not snapshot_fields:
                    # 手工登记的版本没有快照内容时，按黄金记录当前数据补齐，
                    # 保证发布出去的一定是一份完整字段快照而不是空壳。
                    from app.services.golden_record import GoldenRecordService
                    snapshot_fields = _business_fields(
                        GoldenRecordService().query_golden_record(golden_id))
                target["快照字段"] = copy.deepcopy(snapshot_fields)
                target["快照摘要"] = _digest(snapshot_fields)
                snapshot = {
                    "快照编号": target.get("快照编号") or f"GVS-{golden_id}-{target.get('版本号')}",
                    "黄金ID": golden_id,
                    "版本编号": target.get("版本编号"),
                    "版本号": target.get("版本号"),
                    "字段": copy.deepcopy(snapshot_fields),
                    "快照摘要": target.get("快照摘要") or _digest(snapshot_fields),
                    "生成时间": target.get("版本时间") or published_at,
                }
                target["交付快照"] = snapshot
            snapshot_fields = copy.deepcopy(snapshot.get("字段") or {})
            snapshot["字段"] = snapshot_fields
            if not snapshot.get("快照摘要"):
                snapshot["快照摘要"] = _digest(snapshot_fields)
            target["快照字段"] = copy.deepcopy(snapshot_fields)
            target["快照摘要"] = snapshot["快照摘要"]
            if not target.get("快照编号"):
                target["快照编号"] = snapshot.get("快照编号")

            # 唯一性：旧的当前版本全部降为历史版本，只有 target 保持已确认。
            for row in candidates:
                if row is target:
                    continue
                if row.get("status") == "当前版本":
                    row["status"] = "历史版本"
                if row.get("发布状态") == "已发布":
                    row["发布状态"] = "历史发布"
                row["是否当前发布"] = False
                row["已确认"] = False
                row_snapshot = row.get("交付快照")
                if isinstance(row_snapshot, dict) and row_snapshot.get("发布状态") == "已发布":
                    row_snapshot["发布状态"] = "历史发布"

            target["status"] = "当前版本"
            target["pending"] = False
            target["abnormal"] = False
            target["发布状态"] = "已发布"
            target["是否当前发布"] = True
            target["已确认"] = True
            target["发布编号"] = publish_id
            target["发布人"] = changed_by or target.get("发布人", "") or ""
            target["发布时间"] = published_at
            snapshot.update({
                "发布编号": publish_id,
                "发布状态": "已发布",
                "发布人": target["发布人"],
                "发布时间": published_at,
                "版本号": target.get("版本号"),
                "版本编号": target.get("版本编号"),
            })

            # 黄金记录只同步“当前记录版本/发布编号”两个指针，字段仍以快照为准。
            self._sync_golden_pointer(golden_id, target)
            return self._publish_result(target, republished=False,
                                        channel=channel)

    def publish_version(self, golden_id: str, version_number: str | None = None,
                        **kwargs: Any) -> dict[str, Any]:
        """publish_golden_version 的别名，供调用方按习惯命名使用。"""
        return self.publish_golden_version(golden_id, version_number, **kwargs)

    def confirm_publish(self, golden_id: str, version_number: str | None = None,
                        **kwargs: Any) -> dict[str, Any]:
        """确认发布别名。"""
        return self.publish_golden_version(golden_id, version_number, **kwargs)

    def _publish_result(self, version: dict[str, Any], *, republished: bool,
                        channel: str | None = None) -> dict[str, Any]:
        snapshot = copy.deepcopy(version.get("交付快照") or {})
        fields = self._fields_of_version(version)
        snapshot.setdefault("字段", copy.deepcopy(fields))
        snapshot.setdefault("快照编号", version.get("快照编号"))
        return {
            "ok": True,
            "golden_id": version.get("黄金ID"),
            "version": version,
            "version_number": version.get("版本号"),
            "version_id": version.get("版本编号"),
            "publish_id": version.get("发布编号"),
            "snapshot_id": version.get("快照编号") or snapshot.get("快照编号"),
            "snapshot": snapshot,
            "fields": fields,
            "republished": republished,
            "channel": channel or "",
        }

    def _sync_golden_pointer(self, golden_id: str, version: dict[str, Any]) -> None:
        from app.services.golden_record import GoldenRecordService
        record = GoldenRecordService()._find_row(golden_id)
        if record is None:
            return
        record["记录版本"] = version.get("版本号")
        record["当前版本编号"] = version.get("版本编号")
        record["发布编号"] = version.get("发布编号")

    # ── 交付确认 ────────────────────────────────────────────

    def confirm_version_delivery(self, golden_id: str,
                                 publish_id: str | None = None,
                                 channel: str = "默认渠道",
                                 version_number: str | None = None) -> dict[str, Any]:
        """确认交付：把当前发布版本的交付快照交给渠道并留下交付回执。

        交付确认只允许针对当前发布版本；快照字段不会被重新计算，
        渠道按发布编号、版本号可以完整复算本次交付内容。
        """
        with _PUBLISH_LOCK:
            target = None
            if publish_id:
                target = self._find_by_publish_id(golden_id, publish_id)
            elif version_number not in (None, ""):
                normalized = _normalize_version(version_number)
                target = next(
                    (row for row in store.rows(self.MODULE)
                     if row.get("黄金ID") == golden_id
                     and _normalize_version(row.get("版本号")) in (str(version_number), normalized)),
                    None,
                )
            else:
                target = self._current_published_row(golden_id)
            if target is None:
                return {"ok": False, "golden_id": golden_id,
                        "reason": "没有已发布的版本可确认交付"}
            if target.get("发布状态") != "已发布" or not target.get("已确认"):
                return {"ok": False, "golden_id": golden_id,
                        "reason": "仅当前发布版本可以确认交付，历史版本已冻结"}

            delivered_at = _now()
            snapshot = target.get("交付快照")
            if not isinstance(snapshot, dict):
                return {"ok": False, "golden_id": golden_id, "reason": "交付快照缺失"}
            snapshot.setdefault("字段", self._fields_of_version(target))
            target["交付状态"] = "已确认"
            target["交付渠道"] = channel
            target["交付时间"] = delivered_at
            snapshot["交付状态"] = "已确认"
            snapshot["交付渠道"] = channel
            snapshot["交付时间"] = delivered_at
            receipt = {
                "ok": True,
                "golden_id": golden_id,
                "publish_id": target.get("发布编号"),
                "version_number": target.get("版本号"),
                "snapshot_id": target.get("快照编号") or snapshot.get("快照编号"),
                "channel": channel,
                "delivered_at": delivered_at,
                "delivery_status": "已确认",
                "fields": copy.deepcopy(snapshot.get("字段") or {}),
                "snapshot": copy.deepcopy(snapshot),
                "version": target,
            }
            return receipt

    def confirm_delivery(self, golden_id: str, publish_id: str | None = None,
                         **kwargs: Any) -> dict[str, Any]:
        """confirm_version_delivery 的别名。"""
        return self.confirm_version_delivery(golden_id, publish_id, **kwargs)

    # ── 查询与复算 ──────────────────────────────────────────

    def query_version_history(self, golden_id: str) -> list[dict[str, Any]]:
        """查询版本历史：按版本号降序返回该黄金记录的全部版本。"""
        history = [row for row in store.rows(self.MODULE)
                   if row.get("黄金ID") == golden_id]
        history.sort(key=lambda r: _version_sort_value(r.get("版本号")), reverse=True)
        return history

    def get_published_version(self, golden_id: str) -> dict[str, Any] | None:
        """返回黄金ID当前唯一已确认的版本记录（不存在则返回 None）。"""
        return self._current_published_row(golden_id)

    def current_published_version(self, golden_id: str) -> dict[str, Any] | None:
        """get_published_version 的别名。"""
        return self._current_published_row(golden_id)

    def get_published_fields(self, golden_id: str) -> dict[str, Any] | None:
        """返回当前发布版本的完整交付字段，渠道查询应只读这里。"""
        row = self._current_published_row(golden_id)
        if row is None:
            return None
        return self._fields_of_version(row)

    def query_delivery_snapshot(self, golden_id: str,
                                publish_id: str | None = None,
                                version_number: str | None = None) -> dict[str, Any] | None:
        """查询黄金版本交付快照：可按发布编号或版本号定位历史快照。"""
        row = self._find_version_row(golden_id, publish_id=publish_id,
                                     version_number=version_number)
        if row is None:
            return None
        return copy.deepcopy(row.get("交付快照") or {})

    def list_delivery_snapshots(self, golden_id: str) -> list[dict[str, Any]]:
        """列出黄金记录的全部交付快照，按版本号降序。"""
        rows = [row for row in store.rows(self.MODULE)
                if row.get("黄金ID") == golden_id and row.get("交付快照")]
        rows.sort(key=lambda r: _version_sort_value(r.get("版本号")), reverse=True)
        return [copy.deepcopy(row["交付快照"]) for row in rows]

    def version_publish_history(self, golden_id: str) -> list[dict[str, Any]]:
        """返回发布历史：只看真正发布过的版本，按版本号降序。"""
        rows = [row for row in store.rows(self.MODULE)
                if row.get("黄金ID") == golden_id and row.get("发布编号")]
        rows.sort(key=lambda r: _version_sort_value(r.get("版本号")), reverse=True)
        return rows

    def query_publish_history(self, golden_id: str) -> list[dict[str, Any]]:
        """version_publish_history 的别名。"""
        return self.version_publish_history(golden_id)

    def recompute_published_result(self, golden_id: str,
                                   publish_id: str | None = None,
                                   version_number: str | None = None) -> dict[str, Any]:
        """仅依据发布历史复算交付结果，用于核对渠道实际拿到的数据。"""
        row = self._find_version_row(golden_id, publish_id=publish_id,
                                     version_number=version_number)
        if row is None:
            return {"ok": False, "golden_id": golden_id,
                    "reason": "未找到对应的发布版本"}
        snapshot = row.get("交付快照") or {}
        fields = copy.deepcopy(snapshot.get("字段") or row.get("快照字段") or {})
        digest = _digest(fields)
        expected_digest = snapshot.get("快照摘要") or row.get("快照摘要")
        return {
            "ok": True,
            "golden_id": golden_id,
            "publish_id": row.get("发布编号"),
            "version_number": row.get("版本号"),
            "version_id": row.get("版本编号"),
            "snapshot_id": row.get("快照编号") or snapshot.get("快照编号"),
            "fields": fields,
            "snapshot": copy.deepcopy(snapshot),
            "digest": digest,
            "expected_digest": expected_digest,
            "digest_matches": expected_digest == digest,
            "delivery_status": row.get("交付状态"),
        }

    def verify_delivered_result(self, golden_id: str,
                                delivered_record: dict[str, Any] | None = None,
                                publish_id: str | None = None,
                                version_number: str | None = None) -> dict[str, Any]:
        """核对一次实际交付结果能否按发布历史复算。

        逐字段对比渠道拿到的记录与发布快照；任何字段不一致都会被列出，
        从而识别“地址来自新版本、区县来自旧版本”的拼接状态。
        """
        recomputed = self.recompute_published_result(
            golden_id, publish_id=publish_id, version_number=version_number)
        if not recomputed["ok"]:
            return recomputed
        if delivered_record is None:
            return {**recomputed, "consistent": True, "mismatches": []}
        metadata = {"发布编号", "版本号", "快照编号", "publish_id", "version_number"}
        fields = recomputed["fields"]
        mismatches = []
        for key, value in delivered_record.items():
            if key in metadata or key not in fields:
                continue
            if str(value) != str(fields[key]):
                mismatches.append({
                    "field": key,
                    "delivered": value,
                    "published": fields[key],
                })
        return {
            **recomputed,
            "consistent": not mismatches,
            "mismatches": mismatches,
        }

    def verify_publish_consistency(self, golden_id: str) -> dict[str, Any]:
        """校验当前发布版本与黄金记录指针、交付快照三者是否一致。"""
        row = self._current_published_row(golden_id)
        if row is None:
            return {"ok": False, "golden_id": golden_id, "reason": "没有已确认版本"}
        snapshot = row.get("交付快照") or {}
        from app.services.golden_record import GoldenRecordService
        record = GoldenRecordService().query_golden_record(golden_id) or {}
        checks = {
            "snapshot_digest": _digest(snapshot.get("字段") or {}) == (
                snapshot.get("快照摘要") or row.get("快照摘要")),
            "record_version": str(record.get("记录版本", "")) == str(row.get("版本号", "")),
            "record_publish_id": str(record.get("发布编号", "")) == str(row.get("发布编号", "")),
        }
        return {"ok": True, "golden_id": golden_id, "consistent": all(checks.values()),
                "checks": checks, "publish_id": row.get("发布编号"),
                "version_number": row.get("版本号")}

    # ── 回退 ────────────────────────────────────────────────

    def rollback_to_version(self, golden_id: str,
                            target_version: str) -> dict[str, Any]:
        """版本回退：恢复指定历史版本的字段，并让它重新成为唯一当前版本。"""
        with _PUBLISH_LOCK:
            rows = store.rows(self.MODULE)
            normalized = _normalize_version(target_version)
            target = next(
                (r for r in rows if r.get("黄金ID") == golden_id
                 and _normalize_version(r.get("版本号")) in (str(target_version), normalized)),
                None,
            )
            if not target:
                return {"ok": False, "reason": "目标版本不存在"}

            for row in rows:
                if row.get("黄金ID") != golden_id or row is target:
                    continue
                if row.get("status") == "当前版本":
                    row["status"] = "已回滚"
                    row["rolled_back_at"] = _now()
                if row.get("发布状态") == "已发布":
                    row["发布状态"] = "历史发布"
                row["是否当前发布"] = False
                row["已确认"] = False

            publish_id = target.get("发布编号") or (
                f"PUB-{golden_id}-{target.get('版本号')}")
            target["status"] = "当前版本"
            target["发布状态"] = "已发布"
            target["是否当前发布"] = True
            target["已确认"] = True
            target["发布编号"] = publish_id
            target["pending"] = False
            target["abnormal"] = False
            snapshot = target.get("交付快照")
            if isinstance(snapshot, dict):
                snapshot["发布编号"] = publish_id
                snapshot["发布状态"] = "已发布"
            fields = self._fields_of_version(target)
            self._restore_golden_fields(golden_id, fields, target)
            return {
                "ok": True,
                "golden_id": golden_id,
                "restored_to": target.get("版本号"),
                "publish_id": publish_id,
                "fields": copy.deepcopy(fields),
            }

    def _restore_golden_fields(self, golden_id: str, fields: dict[str, Any],
                               version: dict[str, Any]) -> None:
        from app.services.golden_record import GoldenRecordService
        record = GoldenRecordService()._find_row(golden_id)
        if record is None:
            return
        for key, value in fields.items():
            if not key.startswith("_") and key not in _FRAMEWORK_KEYS:
                record[key] = copy.deepcopy(value)
        record["记录版本"] = version.get("版本号")
        record["当前版本编号"] = version.get("版本编号")
        record["发布编号"] = version.get("发布编号")

    # ── 动作入口 ────────────────────────────────────────────

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        golden_id = str(entry.get("黄金ID", ""))
        version_number = entry.get("版本号")

        if action in ("确认发布", "发布版本", "版本发布", "设为当前"):
            result = self.publish_golden_version(golden_id, version_number)
            if not result["ok"]:
                return None, result.get("reason", "版本发布失败")
            return self.get_entry(entry_id), (
                f"{self.ENTITY}已发布为当前版本（发布编号 {result['publish_id']}）")
        if action in ("交付确认", "确认交付"):
            result = self.confirm_version_delivery(golden_id,
                                                   version_number=version_number)
            if not result["ok"]:
                return None, result.get("reason", "交付确认失败")
            return self.get_entry(entry_id), (
                f"{self.ENTITY}已确认交付（发布编号 {result['publish_id']}）")
        if action == "执行回滚":
            result = self.rollback_to_version(golden_id, str(version_number))
            if not result["ok"]:
                return None, result.get("reason", "版本回退失败")
            return self.get_entry(entry_id), f"{self.ENTITY}已回退到版本 {result['restored_to']}"
        if action == "标记历史":
            with _PUBLISH_LOCK:
                if entry.get("发布状态") == "已发布":
                    entry["发布状态"] = "历史发布"
                entry["是否当前发布"] = False
                entry["已确认"] = False
                entry["status"] = "历史版本"
                entry["pending"] = False
                return entry, f"{self.ENTITY}已标记为历史版本"
        return super().run_action(entry_id, action)

    # ── 内部工具 ────────────────────────────────────────────

    def _next_version(self, golden_id: str) -> str:
        versions = [
            _version_sort_value(row.get("版本号"))
            for row in store.rows(self.MODULE)
            if row.get("黄金ID") == golden_id
        ]
        return f"{max(versions, default=0.0) + 1.0:.1f}"

    def _set_previous_history(self, golden_id: str) -> None:
        """保留兼容入口：只调整状态，不改变唯一发布版本。"""
        for row in store.rows(self.MODULE):
            if row.get("黄金ID") == golden_id and row.get("status") == "当前版本":
                row["status"] = "历史版本"

    def _current_published_row(self, golden_id: str) -> dict[str, Any] | None:
        rows = store.rows(self.MODULE)
        current = [
            row for row in rows
            if row.get("黄金ID") == golden_id
            and row.get("发布状态") == "已发布"
        ]
        if current:
            return max(current, key=lambda row: _version_sort_value(row.get("版本号")))
        # 兼容手工登记为“当前版本”的旧数据：没有发布记录时也视为当前版本。
        legacy = [
            row for row in rows
            if row.get("黄金ID") == golden_id and row.get("status") == "当前版本"
        ]
        return max(legacy, key=lambda row: _version_sort_value(row.get("版本号")),
                   default=None)

    def _find_by_publish_id(self, golden_id: str,
                            publish_id: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows(self.MODULE)
             if row.get("黄金ID") == golden_id
             and row.get("发布编号") == publish_id),
            None,
        )

    def _find_version_row(self, golden_id: str, *,
                          publish_id: str | None = None,
                          version_number: str | None = None) -> dict[str, Any] | None:
        if publish_id:
            row = self._find_by_publish_id(golden_id, publish_id)
            if row is not None:
                return row
        if version_number not in (None, ""):
            normalized = _normalize_version(version_number)
            return next(
                (row for row in store.rows(self.MODULE)
                 if row.get("黄金ID") == golden_id
                 and _normalize_version(row.get("版本号")) in (str(version_number), normalized)),
                None,
            )
        return self._current_published_row(golden_id)

    def get_version_fields(self, row: dict[str, Any]) -> dict[str, Any]:
        """读取某个版本的完整交付字段（公开入口，供查询接口/同步复用）。"""
        return self._fields_of_version(row)

    def _fields_of_version(self, row: dict[str, Any]) -> dict[str, Any]:
        snapshot = row.get("交付快照") or {}
        fields = snapshot.get("字段") or row.get("快照字段")
        if fields:
            return copy.deepcopy(fields)
        # 手工登记且从未生成快照的版本：退回黄金记录当前业务字段，
        # 既不返回空对象，也不会把版本行上的摘要列当成交付字段。
        from app.services.golden_record import GoldenRecordService
        return _business_fields(
            GoldenRecordService().query_golden_record(str(row.get("黄金ID", ""))))

    def assert_single_confirmed(self, golden_id: str) -> bool:
        """断言同一黄金ID最多只有一个已确认版本。"""
        confirmed = [
            row for row in store.rows(self.MODULE)
            if row.get("黄金ID") == golden_id
            and (row.get("已确认") or row.get("status") == "当前版本")
        ]
        return len(confirmed) <= 1

    def compare_versions(self, golden_id: str,
                         version_a: str, version_b: str) -> dict[str, Any]:
        """对比两个版本的黄金记录差异：逐字段展示变更内容。"""
        rows = store.rows(self.MODULE)
        ver_a = next(
            (r for r in rows if r.get("黄金ID") == golden_id
             and _normalize_version(r.get("版本号")) == _normalize_version(version_a)),
            None)
        ver_b = next(
            (r for r in rows if r.get("黄金ID") == golden_id
             and _normalize_version(r.get("版本号")) == _normalize_version(version_b)),
            None)
        if not ver_a or not ver_b:
            return {"ok": False, "reason": "版本不存在"}
        fields_a = self._fields_of_version(ver_a)
        fields_b = self._fields_of_version(ver_b)
        changes = []
        for key in sorted(set(fields_a) | set(fields_b)):
            if str(fields_a.get(key, "")) != str(fields_b.get(key, "")):
                changes.append({
                    "field": key,
                    "a": fields_a.get(key),
                    "b": fields_b.get(key),
                })
        return {
            "golden_id": golden_id,
            "version_a": version_a,
            "version_b": version_b,
            "a_time": ver_a.get("版本时间"),
            "b_time": ver_b.get("版本时间"),
            "a_changes": ver_a.get("变更字段"),
            "b_changes": ver_b.get("变更字段"),
            "changes": changes,
        }
