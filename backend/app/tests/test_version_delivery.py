"""版本发布与交付确认测试：覆盖黄金记录交付快照、渠道查询一致性与并发交接。"""
from __future__ import annotations

import os
import sys
import threading

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.golden_record import GoldenRecordService
from app.services.mdm_api import MdmApiService
from app.services.mdm_sync import MdmSyncService
from app.services.mdm_version import MdmVersionService
from app.store import store


def _new_golden(addr: str = "朝阳区建国路 1 号", district: str = "朝阳区") -> str:
    golden = GoldenRecordService().create_golden_record(
        "customer",
        {"name": "王五", "delivery_address": addr, "district": district},
        ["SRC-001", "SRC-002"],
    )
    assert golden["ok"] is True, golden
    return golden["golden_record"]["黄金ID"]


def test_version_snapshot_persists_fields_and_version():
    """版本写入时字段快照和版本编号同时持久化，不再只有摘要。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    updated = golden_svc.update_golden_record(
        golden_id, {"delivery_address": "海淀区中关村大街 2 号", "district": "海淀区"})
    assert updated["ok"] is True, updated
    version_svc = MdmVersionService()
    row = version_svc.query_version_history(golden_id)[0]
    assert row["版本号"] == updated["version_number"]
    assert row["快照字段"]["delivery_address"] == "海淀区中关村大街 2 号"
    assert row["快照字段"]["district"] == "海淀区"
    assert row["交付快照"]["版本号"] == row["版本号"]
    assert row["快照摘要"]


def test_only_one_confirmed_version_per_golden_id():
    """同一黄金ID只能有一个已确认版本，发布新版本自动把旧版本降级。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    version_svc = MdmVersionService()

    golden_svc.update_golden_record(golden_id, {"district": "海淀区",
                                                "delivery_address": "海淀区中关村大街 2 号"})
    second = version_svc.publish_golden_version(golden_id)
    assert second["ok"] is True
    assert second["version_number"] == "2.0"

    confirmed = [row for row in store.rows("mdm_version")
                 if row.get("黄金ID") == golden_id and row.get("status") == "当前版本"]
    assert len(confirmed) == 1, confirmed
    assert confirmed[0]["版本号"] == "2.0"
    assert version_svc.assert_single_confirmed(golden_id) is True
    assert version_svc.get_published_version(golden_id)["版本号"] == "2.0"


def test_channel_query_matches_publish_history_after_refresh():
    """刷新后接口返回的字段必须与发布历史中的版本完全一致。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    version_svc = MdmVersionService()
    api_svc = MdmApiService()

    golden_svc.update_golden_record(golden_id, {"delivery_address": "海淀区中关村大街 2 号",
                                                "district": "海淀区"})
    published = version_svc.publish_golden_version(golden_id)
    assert published["ok"] is True

    first = api_svc.query_published_fields_for_channel(golden_id, channel="配送渠道")
    assert first["ok"] is True
    assert first["fields"]["district"] == "海淀区"
    assert first["version_number"] == "2.0"

    history = version_svc.recompute_published_result(
        golden_id, publish_id=first["publish_id"])
    assert history["digest_matches"] is True
    assert history["fields"] == first["fields"]

    # 业务表再次被更新（尚未发布），渠道刷新仍应拿到上一版完整快照。
    golden_svc.update_golden_record(golden_id, {"delivery_address": "西城区金融街 3 号",
                                                "district": "西城区"})
    refreshed = api_svc.query_published_fields_for_channel(golden_id, channel="配送渠道")
    assert refreshed["fields"] == first["fields"], refreshed["fields"]
    assert refreshed["version_number"] == "2.0"
    assert version_svc.verify_publish_consistency(golden_id)["consistent"] is True


def test_channel_never_receives_mixed_version_fields():
    """渠道按一次完整版本安排配送，不会出现地址新值+区县旧值的拼接。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    version_svc = MdmVersionService()

    golden_svc.update_golden_record(golden_id, {"delivery_address": "海淀区中关村大街 2 号",
                                                "district": "海淀区"})
    version_svc.publish_golden_version(golden_id)
    delivered = version_svc.confirm_version_delivery(golden_id, channel="配送渠道")
    assert delivered["ok"] is True
    assert delivered["fields"]["district"] == "海淀区"
    assert delivered["fields"]["delivery_address"] == "海淀区中关村大街 2 号"

    # 只更新地址字段：新版本快照整体来自“上一版字段 + 本次地址”，
    # 不会把本次地址和未参与更新的旧值拼成半新半旧。
    golden_svc.update_golden_record(golden_id, {"delivery_address": "海淀区中关村大街 9 号"})
    version_svc.publish_golden_version(golden_id)
    result = version_svc.confirm_version_delivery(golden_id, channel="配送渠道")
    assert result["fields"]["district"] == "海淀区"
    assert result["fields"]["delivery_address"] == "海淀区中关村大街 9 号"

    # 另一段交接直接改了业务表但还没发布：渠道仍拿到完整上一版，
    # 不会出现“西城区金融街 3 号 + 海淀区”。
    live = golden_svc._find_row(golden_id)
    live["delivery_address"] = "西城区金融街 3 号"
    live["district"] = "西城区"
    still = version_svc.confirm_version_delivery(golden_id, channel="配送渠道")
    assert still["fields"] == result["fields"]
    assert still["version_number"] == result["version_number"]
    check = version_svc.verify_delivered_result(
        golden_id, result["fields"], publish_id=result["publish_id"])
    assert check["consistent"] is True, check["mismatches"]


def test_delivered_result_recomputable_for_every_handoff():
    """对照更新前后地址、区县、发布编号，每个交付结果都能按发布历史复算。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    version_svc = MdmVersionService()
    receipts = []

    receipts.append(version_svc.confirm_version_delivery(golden_id, channel="配送渠道"))

    golden_svc.update_golden_record(golden_id, {"delivery_address": "海淀区中关村大街 2 号",
                                                "district": "海淀区"})
    version_svc.publish_golden_version(golden_id)
    receipts.append(version_svc.confirm_version_delivery(golden_id, channel="配送渠道"))

    golden_svc.update_golden_record(golden_id, {"delivery_address": "西城区金融街 3 号",
                                                "district": "西城区"})
    version_svc.publish_golden_version(golden_id)
    receipts.append(version_svc.confirm_version_delivery(golden_id, channel="配送渠道"))

    assert [r["version_number"] for r in receipts] == ["1.0", "2.0", "3.0"]
    assert [r["fields"]["district"] for r in receipts] == ["朝阳区", "海淀区", "西城区"]
    assert len({r["publish_id"] for r in receipts}) == 3

    for receipt in receipts:
        check = version_svc.verify_delivered_result(
            golden_id, receipt["fields"], publish_id=receipt["publish_id"])
        assert check["ok"] is True
        assert check["consistent"] is True, check["mismatches"]
        assert check["digest_matches"] is True
        assert check["fields"]["发布编号"] if "发布编号" in check["fields"] else True

    # 历史版本交付后仍可读取各自的快照，新发布不会改写旧交付内容。
    historical = version_svc.query_delivery_snapshot(golden_id, publish_id=receipts[0]["publish_id"])
    assert historical["字段"]["district"] == "朝阳区"


def test_concurrent_handoffs_leave_single_published_version():
    """两段交接并发发布，最终只保留一个当前版本，且字段整体一致。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    version_svc = MdmVersionService()

    golden_svc.update_golden_record(golden_id, {"delivery_address": "海淀区中关村大街 2 号",
                                                "district": "海淀区"})
    golden_svc.update_golden_record(golden_id, {"delivery_address": "西城区金融街 3 号",
                                                "district": "西城区"})
    versions = [row["版本号"] for row in version_svc.query_version_history(golden_id)]

    results: list[dict] = []
    lock = threading.Lock()

    def handoff(version_number: str) -> None:
        result = version_svc.publish_golden_version(golden_id, version_number)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=handoff, args=(v,)) for v in versions[:2]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(r["ok"] for r in results), results
    assert version_svc.assert_single_confirmed(golden_id) is True
    current = version_svc.get_published_version(golden_id)
    channel = version_svc.get_published_fields(golden_id)
    assert channel["district"] in ("海淀区", "西城区")
    if channel["district"] == "海淀区":
        assert channel["delivery_address"] == "海淀区中关村大街 2 号"
    else:
        assert channel["delivery_address"] == "西城区金融街 3 号"
    assert current["版本号"] in versions


def test_api_and_sync_use_same_published_result():
    """主数据查询接口与分发同步返回同一份发布结果。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    version_svc = MdmVersionService()
    api_svc = MdmApiService()

    golden_svc.update_golden_record(golden_id, {"delivery_address": "海淀区中关村大街 2 号",
                                                "district": "海淀区"})
    version_svc.publish_golden_version(golden_id)

    channel = api_svc.query_published_fields_for_channel(golden_id)
    entity = golden_svc.query_golden_record(golden_id)["实体模型"]
    published_list = api_svc.published_records_for_entity(entity)
    record = next(r for r in published_list if r["黄金ID"] == golden_id)
    assert record["delivery_address"] == channel["fields"]["delivery_address"]
    assert record["district"] == channel["fields"]["district"]
    assert record["版本号"] == channel["version_number"]

    api_def = {"返回字段": "黄金ID,delivery_address,district", "实体模型": entity}
    projected = api_svc._project(record, api_def)
    assert projected["delivery_address"] == "海淀区中关村大街 2 号"
    assert projected["版本号"] == channel["version_number"]

    sync_svc = MdmSyncService()
    sync_cfg = next((row for row in store.rows("mdm_sync")
                     if row.get("status") == "同步中"), None)
    if sync_cfg is None:
        sync_cfg = store.rows("mdm_sync")[0]
        sync_svc.run_action(int(sync_cfg["id"]), "启用同步")
    sync_cfg["黄金实体"] = entity
    sync_cfg["字段范围"] = "黄金ID,delivery_address,district,版本号"
    synced = sync_svc.sync_golden_to_target(int(sync_cfg["id"]))
    assert synced["ok"] is True
    synced_record = next(r for r in synced["records"] if r.get("黄金ID") == golden_id)
    assert synced_record["district"] == channel["fields"]["district"]


def test_rollback_restores_snapshot_fields():
    """回退到历史版本时恢复该版本完整字段，并保持唯一当前版本。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    version_svc = MdmVersionService()

    golden_svc.update_golden_record(golden_id, {"delivery_address": "海淀区中关村大街 2 号",
                                                "district": "海淀区"})
    version_svc.publish_golden_version(golden_id)
    result = version_svc.rollback_to_version(golden_id, "1.0")
    assert result["ok"] is True
    assert result["fields"]["district"] == "朝阳区"
    record = golden_svc.query_golden_record(golden_id)
    assert record["district"] == "朝阳区"
    assert record["记录版本"] == "1.0"
    assert version_svc.assert_single_confirmed(golden_id) is True


def test_api_query_uses_published_snapshot_fields():
    """接口按返回字段约定输出，且每个结果都带发布编号/版本号。"""
    golden_svc = GoldenRecordService()
    golden_id = _new_golden()
    version_svc = MdmVersionService()
    golden_svc.update_golden_record(golden_id, {"delivery_address": "海淀区中关村大街 2 号",
                                                "district": "海淀区"})
    version_svc.publish_golden_version(golden_id)

    api_svc = MdmApiService()
    entity = golden_svc.query_golden_record(golden_id)["实体模型"]
    api_row = next(row for row in store.rows("mdm_api") if row.get("status") == "已发布")
    api_row["实体模型"] = entity
    api_row["查询字段"] = "黄金ID"
    api_row["返回字段"] = "黄金ID,delivery_address,district"

    result = api_svc.query_golden_records_by_api(int(api_row["id"]))
    assert result["ok"] is True, result
    record = next(r for r in result["records"] if r["黄金ID"] == golden_id)
    assert record["district"] == "海淀区"
    assert record["版本号"] == "2.0"
    assert record["发布编号"].startswith("PUB-")
    assert "2.0" in result["version_numbers"]

    filtered = api_svc.query_golden_records_by_api(
        int(api_row["id"]), {"黄金ID": golden_id[-6:]})
    assert filtered["count"] >= 1

    exported = api_svc.export_api_response(int(api_row["id"]))
    exported_record = next(r for r in exported if r["黄金ID"] == golden_id)
    assert exported_record["delivery_address"] == record["delivery_address"]


def test_seed_versions_are_consistent_and_recomputable():
    """示例数据本身满足：唯一已确认版本、字段可复算、指针一致。"""
    version_svc = MdmVersionService()
    golden_svc = GoldenRecordService()
    for row in store.rows("mdm_version"):
        golden_id = row.get("黄金ID")
        if not golden_id:
            continue
        recomputed = version_svc.recompute_published_result(
            golden_id, publish_id=row.get("发布编号"))
        if row.get("发布编号"):
            assert recomputed["ok"] is True
            assert recomputed["digest_matches"] is True, golden_id
        assert version_svc.assert_single_confirmed(golden_id) is True
    for golden_id in ("黄金记录管理样例1", "黄金记录管理样例2"):
        fields = version_svc.get_published_fields(golden_id)
        assert fields, golden_id
        assert fields["district"]
        assert fields["delivery_address"]
        assert version_svc.verify_publish_consistency(golden_id)["consistent"] is True
        assert golden_svc.get_published_fields(golden_id) == fields


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS: {test.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001 - 单文件自检脚本
            import traceback
            traceback.print_exc()
            print(f"  FAIL: {test.__name__} -> {exc}")
            failed += 1
    print(f"\n结果: {passed}/{len(tests)} 通过, {failed} 失败")
    raise SystemExit(1 if failed else 0)
