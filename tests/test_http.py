"""HTTP 层：关键链路与错误码。"""
from datetime import date

from tests.test_scenarios import _build_world, _schedule

D = date.fromisoformat


def _post(client, path, payload):
    return client.post(path, json=payload)


def test_health(client):
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_full_http_flow(client, svc):
    _build_world(svc)
    e2 = _schedule(svc, "S2", "五年级", 10,
                   [D("2026-10-16"), D("2026-10-20")])

    # 排课时授权不足 -> 422
    bad = _post(client, "/api/v1/schedule-entries", {
        "school_id": "S1", "grade": "三年级", "dates": ["2027-03-01"],
        "venue_constraint": "操场", "student_age": 8, "use_code": "课间操",
        "content_type": "unit", "content_id": "U_mural", "content_version": 1,
        "required_level": 2, "granted_level": 2, "scheduled_by": "p_district"})
    assert bad.status_code == 422
    assert bad.get_json()["error"] == "license_not_covered"

    # 登记撤销并分析影响
    r = _post(client, "/api/v1/revocation-notices", {
        "notice_id": "N1", "license_id": "L1",
        "effective_date": "2026-10-10",
        "reason": "权利方提前终止校园授权", "detail": "素材馆通知",
        "created_by": "m_mural"})
    assert r.status_code == 201
    impact = client.post("/api/v1/revocation-notices/N1/impact").get_json()
    assert {n["entry_id"] for n in impact} == {e2.entry_id}

    # 失效课表批量发布 -> 422，且无新课表版本
    pub = _post(client, "/api/v1/batch-publications", {
        "entry_ids": [e2.entry_id], "attachments": [],
        "published_by": "p_district"})
    assert pub.status_code == 422

    # 提交替换 -> 自审被拒 403 -> 他人审核 -> 发布
    repl = _post(client, "/api/v1/replacements", {
        "original_entry_id": e2.entry_id,
        "dates": ["2026-10-16", "2026-10-20"],
        "content_type": "unit", "content_id": "U_ribbon",
        "content_version": 1, "submitted_by": "m_mural"}).get_json()
    self_review = _post(client, f"/api/v1/replacements/{repl['replacement_id']}/review",
                        {"reviewer": "m_mural", "approve": True})
    assert self_review.status_code == 403
    assert _post(client, f"/api/v1/replacements/{repl['replacement_id']}/review",
                 {"reviewer": "r_curriculum", "approve": True}).status_code == 200
    published = _post(client, f"/api/v1/replacements/{repl['replacement_id']}/publish",
                      {"publisher": "p_district",
                       "attachments": [{"name": "动作图示", "ref": "refs://r.png"}]})
    assert published.status_code == 200

    # 学校告知包含五个必备要素
    notices = client.get("/api/v1/schools/S2/notices").get_json()
    [sn] = notices
    assert sn["adopted_content"] == "unit U_ribbon v1"
    # 授权依据 = 排课当时锁定的原授权（可追溯，含被终止的 L1）
    assert sn["license_basis"][0]["license_id"] == "L1"
    assert sn["affected_dates"] == ["2026-10-16", "2026-10-20"]
    assert sn["withdrawal_reason"] == "权利方提前终止校园授权"
    assert sn["new_arrangement"] and sn["new_doc_version"] == 1

    # 课表列表反映换版
    listed = client.get("/api/v1/schedules").get_json()["items"]
    statuses = {i["entry_id"]: i["status"] for i in listed}
    assert statuses[e2.entry_id] == "replaced"


def test_expiry_scan_job_resume_over_http(client, svc):
    _build_world(svc)
    _schedule(svc, "S2", "五年级", 10, [D("2026-10-16")])
    _post(client, "/api/v1/revocation-notices", {
        "notice_id": "N1", "license_id": "L1",
        "effective_date": "2026-10-10", "reason": "提前终止",
        "detail": "", "created_by": "m_mural"})

    created = _post(client, "/api/v1/jobs/expiry-scan",
                    {"as_of": "2026-10-01"}).get_json()
    jid = created["job_id"]
    partial = client.post(f"/api/v1/jobs/{jid}/run?fail_after=1").get_json()
    assert partial["status"] == "interrupted"
    done = client.post(f"/api/v1/jobs/{jid}/resume").get_json()
    assert done["status"] == "done"
    assert len(done["processed_keys"]) == done["total"] == 1
