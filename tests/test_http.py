"""HTTP 端到端集成：从授权登记到撤销、精准撤回、替换换版、学校说明。"""
from __future__ import annotations

from datetime import date

import pytest

from app import create_app
from store import Store


@pytest.fixture
def client():
    store = Store()
    app = create_app(store)
    app.testing = True
    with app.test_client() as c:
        yield c, store


def _post(c, uid, path, payload):
    return c.post(path, json=payload, headers={"X-User-Id": uid})


def _bootstrap(c):
    for uid, name, role in [
        ("mei", "小梅", "material_maintainer"),
        ("wang", "王教研", "regional_coach"),
        ("li", "李审核", "curriculum_reviewer"),
        ("zhou", "周发布", "publisher"),
    ]:
        assert _post(c, uid, "/api/v1/users",
                     {"id": uid, "name": name, "role": role}).status_code == 201

    assert _post(c, "mei", "/api/v1/schools", {
        "id": "S1", "name": "实验一小", "region": "华东",
        "grades": {"三年级": {"age_min": 8, "age_max": 9}}}).status_code == 201

    assert _post(c, "mei", "/api/v1/materials", {
        "id": "M1", "title": "敦煌壁画动作参考", "kind": "mural",
        "license_key": "L-MURAL", "maintainer_id": "mei",
        "clips": [{"id": "C1", "code": "mural-01", "start_sec": 0,
                   "end_sec": 30, "description": "飞天展臂"}]}).status_code == 201

    r = _post(c, "mei", "/api/v1/licenses/L-MURAL/versions", {
        "rights_holder": "敦煌文创中心", "permitted_uses": ["break_exercise"],
        "regions": ["*"], "age_min": 6, "age_max": 12,
        "effective_from": "2026-01-01", "effective_to": "2026-12-31",
        "evidence_summary": "校园授权合同 M-2026-018",
        "evidence_refs": ["EV-CONTRACT-018"]})
    assert r.status_code == 201
    lic = r.get_json()

    r = _post(c, "mei", "/api/v1/action-units/U-ARM/versions", {
        "name": "展臂运动",
        "clip_refs": [{"clip_id": "C1", "material_id": "M1"}],
        "duration_sec": 480, "difficulty": 2})
    assert r.status_code == 201
    unit = r.get_json()

    r = _post(c, "mei", "/api/v1/combos/K-MURAL/versions", {
        "name": "壁画课间操",
        "members": [{"unit_version_id": unit["id"], "duration_sec": 480}]})
    assert r.status_code == 201
    combo = r.get_json()
    return lic, combo


def test_full_http_lifecycle(client):
    c, store = client
    lic, combo = _bootstrap(c)

    # 排课（三天，其中一天已完成）
    r = _post(c, "wang", "/api/v1/schedules/SCH-S1-G3/versions", {
        "school_id": "S1", "grade": "三年级",
        "items": [
            {"date": "2026-10-08", "combo_version_id": combo["id"],
             "venue": "操场", "venue_limit": "每班40人"},
            {"date": "2026-10-09", "combo_version_id": combo["id"],
             "venue": "操场", "venue_limit": "每班40人"},
            {"date": "2026-10-10", "combo_version_id": combo["id"],
             "venue": "操场", "venue_limit": "每班40人"}],
        "attachments": [{"attachment_id": "A1", "name": "安全提示",
                         "kind": "note", "content": "操场湿滑暂停"}]})
    assert r.status_code == 201
    sv = r.get_json()
    assert sv["status"] == "pending_approval"
    assert len(sv["entries"][0]["license_basis"]) == 1
    assert sv["entries"][0]["license_basis"][0]["water_level"] == 1

    # 提交人不能自审
    assert _post(c, "wang", f"/api/v1/schedules/versions/{sv['id']}/approve",
                 {}).status_code == 403
    assert _post(c, "li", f"/api/v1/schedules/versions/{sv['id']}/approve",
                 {"note": "通过"}).status_code == 200
    assert _post(c, "zhou", f"/api/v1/schedules/versions/{sv['id']}/publish",
                 {}).status_code == 200

    # 10/8 已完成
    assert c.post(
        f"/api/v1/schedules/versions/{sv['id']}/entries/ent_SCH-S1-G3_01/execute",
        headers={"X-User-Id": "wang"}).status_code == 200

    # 撤销：覆盖至 10/9
    r = _post(c, "mei", "/api/v1/revocations", {
        "license_version_id": lic["id"], "covers_through": "2026-10-09",
        "reason": "权利方提前终止校园壁画动作参考授权",
        "evidence_refs": ["EV-REVOKE-20260925"]})
    assert r.status_code == 201
    rev = r.get_json()

    # 失效课表不得生成下载凭据
    r = _post(c, "zhou",
              f"/api/v1/schedules/versions/{sv['id']}/credentials", {})
    assert r.status_code == 409

    # 到期检查任务（只有一份课表，一次跑完；中断续办由服务层测试覆盖）
    r = _post(c, "wang", "/api/v1/jobs/expiry-check",
              {"scope": f"revocation:{rev['id']}"})
    job = r.get_json()
    r = _post(c, "wang", f"/api/v1/jobs/{job['id']}/run", {})
    assert r.get_json()["status"] == "completed"

    r = c.get("/api/v1/schedules/SCH-S1-G3/current",
              headers={"X-User-Id": "wang"})
    affected = r.get_json()
    by_date = {e["date"]: e for e in affected["entries"]}
    assert by_date["2026-10-08"]["status"] == "executed"
    assert by_date["2026-10-09"]["basis_revoked"] is False
    assert by_date["2026-10-10"]["basis_revoked"] is True

    # 替代素材：登记 M2 + 节拍组合
    _post(c, "mei", "/api/v1/materials", {
        "id": "M2", "title": "节拍训练", "kind": "rhythm",
        "license_key": "L-RHYTHM", "maintainer_id": "mei",
        "clips": [{"id": "C3", "code": "beat-01", "start_sec": 0,
                   "end_sec": 40}]})
    _post(c, "mei", "/api/v1/licenses/L-RHYTHM/versions", {
        "rights_holder": "体卫艺教研室", "permitted_uses": ["break_exercise"],
        "regions": ["*"], "age_min": 6, "age_max": 12,
        "effective_from": "2026-01-01", "effective_to": "2026-12-31",
        "evidence_summary": "授权确认函", "evidence_refs": ["EV-R01"]})
    r = _post(c, "mei", "/api/v1/action-units/U-BEAT/versions", {
        "name": "踏步击掌",
        "clip_refs": [{"clip_id": "C3", "material_id": "M2"}],
        "duration_sec": 480, "difficulty": 2})
    beat_unit = r.get_json()
    r = _post(c, "mei", "/api/v1/combos/K-ALT/versions", {
        "name": "节拍替代操",
        "members": [{"unit_version_id": beat_unit["id"], "duration_sec": 480}]})
    alt_combo = r.get_json()

    # 替换：提交与放行必须不同人
    r = _post(c, "wang", "/api/v1/replacements", {
        "old_schedule_version_id": affected["id"],
        "old_entry_ids": ["ent_SCH-S1-G3_03"],
        "new_combo_version_id": alt_combo["id"],
        "revocation_id": rev["id"]})
    assert r.status_code == 201
    rep = r.get_json()
    assert rep["checks"]["duration_within_tolerance"] is True

    assert _post(c, "wang", f"/api/v1/replacements/{rep['id']}/review",
                 {"approve": True}).status_code == 403
    assert _post(c, "li", f"/api/v1/replacements/{rep['id']}/review",
                 {"approve": True, "note": "合规"}).status_code == 200

    # 换版：主表 + 附件一次成功
    r = _post(c, "zhou", f"/api/v1/replacements/{rep['id']}/apply", {
        "attachments": [{"attachment_id": "A2", "name": "换版说明",
                         "kind": "note", "content": "10/10 起改用节拍替代操"}]})
    assert r.status_code == 200
    new_sv = r.get_json()["schedule_version"]
    assert new_sv["supersedes_id"] == affected["id"]
    assert [a["attachment_id"] for a in new_sv["attachments"]] == ["A2"]
    by_date = {e["date"]: e for e in new_sv["entries"]}
    assert by_date["2026-10-10"]["combo_version_id"] == alt_combo["id"]
    assert by_date["2026-10-08"]["status"] == "executed"

    # 新版本可签发凭据
    r = _post(c, "zhou",
              f"/api/v1/schedules/versions/{new_sv['id']}/credentials", {})
    assert r.status_code == 201 and r.get_json()["active"]

    # 学校说明五要素齐全
    r = c.get("/api/v1/schools/S1/notices", headers={"X-User-Id": "wang"})
    notice = r.get_json()["items"][0]
    assert notice["adopted_versions"][0]["schedule_version_id"] == new_sv["id"]
    assert notice["license_basis"][0]["rights_holder"] == "体卫艺教研室"
    assert notice["affected_dates"] == ["2026-10-10"]
    assert "提前终止" in notice["withdrawal_reason"]
    assert notice["new_arrangement_ref"]["status"] == "arranged"


def test_http_requires_identity_and_role(client):
    c, _ = client
    # 无身份头
    r = c.post("/api/v1/schools", json={"id": "S", "name": "x", "region": "华东",
                                        "grades": {}})
    assert r.status_code == 401
    _post(c, "wang", "/api/v1/users",
          {"id": "wang", "name": "王教研", "role": "regional_coach"})
    # 教研员无权登记素材
    _post(c, "wang", "/api/v1/users",
          {"id": "mei", "name": "小梅", "role": "material_maintainer"})
    r = _post(c, "wang", "/api/v1/materials", {
        "id": "M", "title": "t", "kind": "mural",
        "license_key": "L", "maintainer_id": "mei", "clips": []})
    assert r.status_code == 403
