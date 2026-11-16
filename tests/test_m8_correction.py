"""M8 更正发布：HTTP 全流程、版本冻结、过期/并发拒绝、续跑、切换、对账与导出。"""
from __future__ import annotations

from app.modules.m8_correction.service import CorrectionService
from app.modules.m8_correction.store import CorrectionStore


def _records(station="S1", day="2024-02-01", value="1.0", version=1, **extra):
    row = {"station": station, "date": day, "rainfall_mm": value, "version": version}
    row.update(extra)
    return [row]


def _stage(client, records, reason="测试", base_gen=None):
    body = {"records": records, "reason": reason}
    if base_gen is not None:
        body["base_gen"] = base_gen
    resp = client.post("/api/corrections/stage", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _publish(client, run_id):
    return client.post(f"/api/corrections/{run_id}/publish")


def test_first_publish_freezes_generation_and_monthly(client):
    staged = _stage(client, _records(value="3.0"), reason="首版")
    pub = _publish(client, staged["run_id"])
    assert pub.status_code == 200
    gen = pub.json()["gen_id"]
    assert client.get("/api/corrections/generations").json()["current_gen"] == gen
    view = client.get("/api/corrections/view").json()
    assert view["monthly"][0]["rainfall_mm"] == "3.0"


def test_correction_updates_current_but_old_report_reproduces(client):
    first = _stage(client, _records(day="2024-02-01", value="3.0"), "首版")
    gen1 = _publish(client, first["run_id"]).json()["gen_id"]
    second = _stage(client, _records(day="2024-02-01", value="8.0", version=2), "补传")
    gen2 = _publish(client, second["run_id"]).json()["gen_id"]
    assert gen1 != gen2
    # 当前月量反映更正
    assert client.get("/api/corrections/view").json()["monthly"][0]["rainfall_mm"] == "8.0"
    # 旧版月报原样复现
    old = client.get(f"/api/corrections/view?gen_id={gen1}").json()
    assert old["monthly"][0]["rainfall_mm"] == "3.0"


def test_reason_is_required_and_persisted(client):
    resp = client.post("/api/corrections/stage",
                       json={"records": _records(), "reason": "  "})
    assert resp.status_code == 400
    staged = _stage(client, _records(), reason="站点补传理由")
    gen = _publish(client, staged["run_id"]).json()["gen_id"]
    view = client.get("/api/corrections/view?gen_id=" + gen).json()
    assert view["gen"]["reason"] == "站点补传理由"


def test_stale_correction_rejected_but_old_version_stays_readable(client):
    first = _stage(client, _records(value="1.0"), "首版")
    gen1 = _publish(client, first["run_id"]).json()["gen_id"]
    second = _stage(client, _records(day="2024-02-02", value="2.0"), "推进一版")
    _publish(client, second["run_id"])
    # 第三任务仍以 gen1 为基准 → 已过期
    stale = _stage(client, _records(day="2024-02-03", value="3.0"), "过期",
                   base_gen=gen1)
    resp = _publish(client, stale["run_id"])
    assert resp.status_code == 409
    assert resp.json()["error"] == "E_CORR_STALE"
    # 任务仍待发布，重新定基后可成功
    rebase = client.post(f"/api/corrections/{stale['run_id']}/rebase")
    assert rebase.status_code == 200
    assert _publish(client, stale["run_id"]).status_code == 200


def test_concurrent_publish_is_rejected_with_busy(client, settings):
    r1 = _stage(client, _records(station="A", value="1.0"), "并发A")
    r2 = _stage(client, _records(station="B", value="2.0"), "并发B")
    store = CorrectionStore(settings.db_path)
    with store.immediate() as conn:
        store.set_state(conn, "publish_lock", r1["run_id"])
    try:
        resp = _publish(client, r2["run_id"])
        assert resp.status_code == 409
        assert resp.json()["error"] == "E_CORR_BUSY"
    finally:
        with store.connect() as conn:
            store.set_state(conn, "publish_lock", None)
    assert _publish(client, r2["run_id"]).status_code == 200


def test_resume_clears_half_generation_and_does_not_duplicate(client, settings):
    staged = _stage(client, _records(station="R", day="2024-03-01", value="4.0"),
                    "中断续跑")
    store = CorrectionStore(settings.db_path)
    with store.connect() as conn:
        conn.execute(
            "UPDATE corr_runs SET checkpoints_json=?, pending_gen=? WHERE run_id=?",
            ('["arbitrate"]', "gen-resume-1", staged["run_id"]))
    with store.immediate() as conn:
        conn.execute(
            "INSERT INTO corr_publishes(gen_id,run_id,base_gen,reason,source_digest,"
            "status,created_at) VALUES(?,?,?,?,?,?,?)",
            ("gen-resume-1", staged["run_id"], None, "中断续跑", "x", "frozen",
             "2026-01-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO corr_monthly(gen_id,station,month,rainfall_mm,observed_days,"
            "zero_days,missing_reports,missing_days,isolated_days,expected_days,"
            "missing_dates_json,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("gen-resume-1", "R", "2024-03", "99.9", 1, 0, 0, 30, 0, 31, "[]",
             "partial"))
    svc = CorrectionService(settings, store)
    result = svc.publish(staged["run_id"])
    assert result["resumed"] is True
    assert result["gen_id"] == "gen-resume-1"
    view = client.get("/api/corrections/view?gen_id=gen-resume-1").json()
    rows = [m for m in view["monthly"] if m["station"] == "R"]
    assert len(rows) == 1 and rows[0]["rainfall_mm"] == "4.0"


def test_republish_is_rejected_and_duplicate_file_replays(client):
    staged = _stage(client, _records(value="1.0"), "首版")
    _publish(client, staged["run_id"])
    again = _publish(client, staged["run_id"])
    assert again.status_code == 409
    assert again.json()["error"] == "E_CORR_RUN_DONE"
    # 同文件重复导入不新建任务、不重复插行
    dup = _stage(client, _records(value="1.0"), "首版")
    assert dup["replayed"] is True and dup["run_id"] == staged["run_id"]


def test_switch_failure_keeps_old_version_readable(client):
    g1 = _publish(client, _stage(client, _records(day="2024-02-01", value="1.0"),
                                 "v1")["run_id"]).json()["gen_id"]
    g2 = _publish(client, _stage(client, _records(day="2024-02-02", value="2.0"),
                                 "v2")["run_id"]).json()["gen_id"]
    before = client.get("/api/corrections/generations").json()["current_gen"]
    resp = client.post("/api/corrections/generations/gen-missing/switch")
    assert resp.status_code == 404
    assert client.get("/api/corrections/generations").json()["current_gen"] == before
    ok = client.post(f"/api/corrections/generations/{g1}/switch")
    assert ok.status_code == 200
    assert client.get("/api/corrections/view").json()["gen_id"] == g1
    client.post(f"/api/corrections/generations/{g2}/switch")


def test_compare_generations_highlights_delta(client):
    g1 = _publish(client, _stage(client, _records(day="2024-02-01", value="3.0"),
                                 "v1")["run_id"]).json()["gen_id"]
    g2 = _publish(client, _stage(
        client, _records(day="2024-02-01", value="8.0", version=2), "v2"
    )["run_id"]).json()["gen_id"]
    cmp = client.get(f"/api/corrections/compare?left={g1}&right={g2}").json()
    changed = [r for r in cmp["rows"] if r["changed"]]
    assert changed and changed[0]["delta"] == "5.0"


def test_four_account_reconcile_balances(client):
    # 一条采纳、一条低版本重复、一组同版异值（2 条隔离）
    _publish(client, _stage(client, _records(day="2024-02-01", value="1.0"),
                            "v1")["run_id"])
    _publish(client, _stage(client, [
        {"station": "S1", "date": "2024-02-02", "rainfall_mm": "1.0", "version": 1},
        {"station": "S1", "date": "2024-02-02", "rainfall_mm": "9.0", "version": 1},
    ], "冲突")["run_id"])
    rec = client.get("/api/corrections/reconcile").json()
    assert rec["balanced"] is True
    assert rec["total_source"] == rec["accepted"] + rec["duplicates"] + rec["isolated"]
    assert rec["isolated"] == 2


def test_export_follows_selected_generation(client):
    g1 = _publish(client, _stage(client, _records(value="3.0"), "v1")["run_id"]
                  ).json()["gen_id"]
    g2 = _publish(client, _stage(
        client, _records(value="8.0", version=2), "v2")["run_id"]).json()["gen_id"]
    current_csv = client.get("/api/corrections/monthly.csv").text
    old_csv = client.get(f"/api/corrections/monthly.csv?gen_id={g1}").text
    assert g2 in current_csv and "8.0" in current_csv
    assert g1 in old_csv and "3.0" in old_csv


def test_stale_lock_from_crashed_process_is_recovered(client, settings):
    from pathlib import Path
    staged = _stage(client, _records(value="1.0"), "首版")
    # 模拟进程崩溃：锁值残留，但任务仍是 staged（没有真正在跑）
    crashed = CorrectionStore(settings.db_path)
    with crashed.connect() as conn:
        crashed.set_state(conn, "publish_lock", staged["run_id"])
    # 模拟新进程：清掉进程级「已修复」标记后构造服务
    resolved = str(Path(settings.db_path).resolve())
    CorrectionService._lock_repaired_paths.discard(resolved)
    fresh = CorrectionStore(settings.db_path)
    CorrectionService(settings, fresh)
    assert fresh.get_state("publish_lock") is None
    assert _publish(client, staged["run_id"]).status_code == 200


def test_export_without_generation_returns_404(client):
    resp = client.get("/api/corrections/monthly.csv")
    assert resp.status_code == 404


def test_page_renders_with_and_without_data(client):
    assert client.get("/corrections").status_code == 200
    _publish(client, _stage(client, _records(), "v1")["run_id"])
    page = client.get("/corrections")
    assert page.status_code == 200 and "月量更正" in page.text


def test_upload_multipart_file(client):
    data = ("station,date,rainfall_mm,version\nS1,2024-02-01,1.0,1\n").encode("utf-8")
    resp = client.post(
        "/api/corrections/stage",
        files={"file": ("up.csv", data, "text/csv")},
        data={"reason": "文件上传"})
    assert resp.status_code == 200
    assert _publish(client, resp.json()["run_id"]).status_code == 200


def test_higher_version_in_later_run_resolves_earlier_conflict(client):
    # 第一代：同版异值，两条都隔离，该天计隔离日
    r1 = _stage(client, [
        {"station": "S", "date": "2024-02-01", "rainfall_mm": "1.0", "version": 1},
        {"station": "S", "date": "2024-02-01", "rainfall_mm": "9.0", "version": 1},
    ], "冲突")
    g1 = _publish(client, r1["run_id"]).json()["gen_id"]
    gen1 = client.get(f"/api/corrections/view?gen_id={g1}").json()
    assert len(gen1["conflicts"]) == 1
    assert gen1["monthly"][0]["isolated_days"] == 1
    # 第二代：更高版本权威值化解冲突；旧两条仍留存为隔离证据
    r2 = _stage(client, [
        {"station": "S", "date": "2024-02-01", "rainfall_mm": "5.0", "version": 2},
    ], "权威补传")
    _publish(client, r2["run_id"])
    gen2 = client.get("/api/corrections/view").json()
    assert gen2["monthly"][0]["rainfall_mm"] == "5.0"
    assert gen2["monthly"][0]["isolated_days"] == 0
    assert gen2["conflicts"] == []
    rec = client.get("/api/corrections/reconcile").json()
    assert rec["total_source"] == 3 and rec["isolated"] == 2 and rec["balanced"]


def test_empty_and_bad_header_files_return_422(client):
    empty = client.post("/api/corrections/stage",
                        files={"file": ("e.csv", b"", "text/csv")},
                        data={"reason": "x"})
    assert empty.status_code == 422 and empty.json()["error"] == "E_CORR_EMPTY"
    bad = client.post(
        "/api/corrections/stage",
        files={"file": ("h.csv", b"station,date\nS1,2024-02-01\n", "text/csv")},
        data={"reason": "x"})
    assert bad.status_code == 422 and bad.json()["error"] == "E_CORR_HEADER"
