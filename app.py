"""HTTP 入口：文化素材授权换版与课表撤回服务。"""
from __future__ import annotations

from datetime import date
from typing import Any

from flask import Flask, jsonify, request

from service.container import build_services
from service.errors import ServiceError
from service.serialization import to_json

app = Flask(__name__)
app.json.ensure_ascii = False
services = build_services()


# -- 基础与错误处理 ------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.errorhandler(ServiceError)
def handle_service_error(err: ServiceError):
    return jsonify(error=err.code, message=str(err)), err.status_code


def body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ServiceError("请求体必须是 JSON 对象")
    return data


def require(data: dict, key: str) -> Any:
    if key not in data:
        raise ServiceError(f"缺少字段: {key}")
    return data[key]


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ServiceError(f"日期格式应为 YYYY-MM-DD: {value!r}")


def parse_dates(values) -> list[date]:
    if not isinstance(values, list) or not values:
        raise ServiceError("日期列表不能为空")
    return sorted({parse_date(v) for v in values})


def ok(obj: Any, status: int = 200):
    return jsonify(to_json(obj)), status


# -- 学校 ----------------------------------------------------------------------

@app.post("/api/v1/schools")
def create_school():
    d = body()
    from service.models import School
    sid = require(d, "school_id")
    if sid in services.store.schools:
        from service.errors import Conflict
        raise Conflict(f"学校已存在: {sid}")
    s = School(school_id=sid, name=require(d, "name"),
               region=require(d, "region"))
    services.store.add_school(s)
    return ok(s, 201)


# -- 素材片段 ------------------------------------------------------------------

@app.post("/api/v1/clips")
def create_clip():
    d = body()
    clip = services.catalog.register_clip(
        clip_id=require(d, "clip_id"), title=require(d, "title"),
        duration_sec=int(require(d, "duration_sec")),
        source_ref=require(d, "source_ref"),
        created_by=require(d, "created_by"))
    return ok(clip, 201)


# -- 授权版本 ------------------------------------------------------------------

@app.post("/api/v1/licenses")
def grant_license():
    d = body()
    lv = services.catalog.grant_license(
        license_id=require(d, "license_id"),
        rights_holder=require(d, "rights_holder"),
        allowed_uses=require(d, "allowed_uses"),
        regions=d.get("regions", ["*"]),
        clip_ids=require(d, "clip_ids"),
        age_min=int(require(d, "age_min")), age_max=int(require(d, "age_max")),
        valid_from=parse_date(require(d, "valid_from")),
        valid_to=parse_date(d["valid_to"]) if d.get("valid_to") else None,
        evidence_summary=require(d, "evidence_summary"),
        evidence_ref=require(d, "evidence_ref"),
        created_by=require(d, "created_by"))
    return ok(lv, 201)


@app.post("/api/v1/licenses/<license_id>/revisions")
def revise_license(license_id: str):
    d = body()
    lv = services.catalog.revise_license(
        license_id=license_id, rights_holder=require(d, "rights_holder"),
        allowed_uses=require(d, "allowed_uses"),
        regions=d.get("regions", ["*"]),
        clip_ids=require(d, "clip_ids"),
        age_min=int(require(d, "age_min")), age_max=int(require(d, "age_max")),
        valid_from=parse_date(require(d, "valid_from")),
        valid_to=parse_date(d["valid_to"]) if d.get("valid_to") else None,
        evidence_summary=require(d, "evidence_summary"),
        evidence_ref=require(d, "evidence_ref"),
        created_by=require(d, "created_by"))
    return ok(lv, 201)


@app.get("/api/v1/licenses/<license_id>")
def get_license(license_id: str):
    return ok(services.catalog.get_version_chain(license_id))


# -- 动作单元与组合 ------------------------------------------------------------

@app.post("/api/v1/units")
def create_unit():
    d = body()
    uv = services.catalog.create_unit_version(
        unit_id=require(d, "unit_id"), name=require(d, "name"),
        clip_refs=require(d, "clip_refs"),
        duration_sec=int(require(d, "duration_sec")),
        difficulty=int(require(d, "difficulty")),
        age_min=int(require(d, "age_min")), age_max=int(require(d, "age_max")),
        created_by=require(d, "created_by"))
    return ok(uv, 201)


@app.post("/api/v1/combos")
def create_combo():
    d = body()
    cv = services.catalog.create_combo_version(
        combo_id=require(d, "combo_id"), name=require(d, "name"),
        members=require(d, "members"), created_by=require(d, "created_by"))
    return ok(cv, 201)


# -- 排课 ----------------------------------------------------------------------

@app.post("/api/v1/schedule-entries")
def create_entry():
    d = body()
    entry = services.schedule.schedule(
        school_id=require(d, "school_id"), grade=require(d, "grade"),
        dates=parse_dates(require(d, "dates")),
        venue_constraint=require(d, "venue_constraint"),
        student_age=int(require(d, "student_age")),
        use_code=require(d, "use_code"),
        content_type=require(d, "content_type"),
        content_id=require(d, "content_id"),
        content_version=int(require(d, "content_version")),
        required_level=int(require(d, "required_level")),
        granted_level=int(require(d, "granted_level")),
        scheduled_by=require(d, "scheduled_by"))
    return ok(entry, 201)


@app.post("/api/v1/schedule-entries/<entry_id>/execute")
def execute_entry(entry_id: str):
    d = body()
    entry = services.schedule.mark_executed(
        entry_id, parse_date(require(d, "date")), require(d, "actor"))
    return ok(entry)


@app.get("/api/v1/schedules")
def schedules():
    return jsonify(items=[
        {"entry_id": e.entry_id, "school_id": e.school_id, "grade": e.grade,
         "dates": [d.isoformat() for d in e.dates], "status": e.status}
        for e in services.store.all_entries()])


# -- 批量发布与下载凭据 --------------------------------------------------------

@app.post("/api/v1/batch-publications")
def publish_batch():
    d = body()
    docs = services.schedule.publish_batch(
        entry_ids=require(d, "entry_ids"),
        attachments=d.get("attachments", []),
        published_by=require(d, "published_by"))
    return ok(docs, 201)


@app.post("/api/v1/schedule-entries/<entry_id>/credentials")
def issue_credential(entry_id: str):
    d = body()
    cred = services.schedule.issue_credential(
        entry_id, parse_date(require(d, "date")),
        issued_by=require(d, "issued_by"))
    return ok(cred, 201)


@app.get("/api/v1/downloads/<token>")
def redeem(token: str):
    entry = services.schedule.redeem_credential(token)
    return ok(entry)


# -- 撤销通知与影响分析 --------------------------------------------------------

@app.post("/api/v1/revocation-notices")
def create_notice():
    d = body()
    notice = services.revocation.register_notice(
        notice_id=require(d, "notice_id"),
        license_id=require(d, "license_id"),
        effective_date=parse_date(require(d, "effective_date")),
        reason=require(d, "reason"), detail=d.get("detail", ""),
        created_by=require(d, "created_by"))
    return ok(notice, 201)


@app.post("/api/v1/revocation-notices/<notice_id>/impact")
def analyze_impact(notice_id: str):
    as_of = parse_date(request.args["as_of"]) if request.args.get("as_of") else None
    notices = services.revocation.analyze_impact(notice_id, as_of=as_of)
    return ok(notices)


# -- 替换单 --------------------------------------------------------------------

@app.post("/api/v1/replacements")
def submit_replacement():
    d = body()
    repl = services.revocation.submit_replacement(
        original_entry_id=require(d, "original_entry_id"),
        dates=parse_dates(require(d, "dates")),
        content_type=require(d, "content_type"),
        content_id=require(d, "content_id"),
        content_version=int(require(d, "content_version")),
        submitted_by=require(d, "submitted_by"),
        reason=d.get("reason", ""))
    return ok(repl, 201)


@app.post("/api/v1/replacements/<replacement_id>/review")
def review_replacement(replacement_id: str):
    d = body()
    repl = services.revocation.review_replacement(
        replacement_id, reviewer=require(d, "reviewer"),
        approve=bool(require(d, "approve")), note=d.get("note", ""))
    return ok(repl)


@app.post("/api/v1/replacements/<replacement_id>/publish")
def publish_replacement(replacement_id: str):
    d = body()
    repl = services.revocation.publish_replacement(
        replacement_id, publisher=require(d, "publisher"),
        attachments=d.get("attachments", []))
    return ok(repl)


@app.get("/api/v1/replacements/<replacement_id>")
def get_replacement(replacement_id: str):
    return ok(services.store.get_replacement(replacement_id))


# -- 可续办任务 ----------------------------------------------------------------

@app.post("/api/v1/jobs/expiry-scan")
def create_expiry_scan():
    d = body() or {}
    as_of = parse_date(d["as_of"]) if d.get("as_of") else None
    job = services.jobs.create_expiry_scan(as_of=as_of)
    return ok(job, 201)


@app.post("/api/v1/jobs/replacement-process")
def create_replacement_process():
    d = body()
    job = services.jobs.create_replacement_process(require(d, "notice_id"))
    return ok(job, 201)


@app.post("/api/v1/jobs/<job_id>/run")
def run_job(job_id: str):
    fail_after = request.args.get("fail_after")
    job = services.jobs.run(job_id, fail_after=int(fail_after) if fail_after else None)
    return ok(job)


@app.post("/api/v1/jobs/<job_id>/resume")
def resume_job(job_id: str):
    return ok(services.jobs.resume(job_id))


@app.get("/api/v1/jobs/<job_id>")
def get_job(job_id: str):
    return ok(services.store.get_job(job_id))


# -- 学校告知 ------------------------------------------------------------------

@app.get("/api/v1/schools/<school_id>/notices")
def school_notices(school_id: str):
    services.store.get_school(school_id)
    return ok(services.store.school_notices_for(school_id))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
