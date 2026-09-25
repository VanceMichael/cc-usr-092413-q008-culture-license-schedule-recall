"""Flask 入口：文化素材授权换版与课表撤回服务。

身份通过 X-User-Id 头传递（演示/测试用）；角色规则在 service 层强制。
测试可使用 create_app(store=...) 取得隔离实例。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from flask import Flask, jsonify, request

import domain
from domain import (
    Attachment, Clip, ClipRef, DomainError, Material, School, User,
)
from service import Service
from store import Store


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError(f"日期格式错误：{value}（应为 YYYY-MM-DD）",
                          code="bad_request", status=400)


def create_app(store: Optional[Store] = None) -> Flask:
    app = Flask(__name__)
    store = store or Store()
    svc = Service(store)
    app.config["STORE"] = store
    app.config["SERVICE"] = svc

    @app.errorhandler(DomainError)
    def _domain_error(exc: DomainError):
        return jsonify(error=exc.code, message=str(exc)), exc.status

    def actor() -> User:
        uid = request.headers.get("X-User-Id")
        if not uid:
            raise DomainError("缺少 X-User-Id 头", code="unauthorized", status=401)
        user = store.users.get(uid)
        if user is None:
            raise DomainError(f"用户 {uid} 不存在", code="unauthorized", status=401)
        return user

    def body() -> dict[str, Any]:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise DomainError("请求体必须为 JSON 对象", code="bad_request",
                              status=400)
        return data

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    # -------- 用户与学校（引导数据） --------
    @app.post("/api/v1/users")
    def create_user():
        d = body()
        user = User(id=d["id"], name=d["name"], role=d["role"])
        return jsonify(svc.register_user(user).to_dict()), 201

    @app.post("/api/v1/schools")
    def create_school():
        d = body()
        grades = {g: (int(v["age_min"]), int(v["age_max"]))
                  for g, v in d["grades"].items()}
        school = School(id=d["id"], name=d["name"], region=d["region"],
                        grades=grades)
        return jsonify(svc.register_school(actor(), school).to_dict()), 201

    # -------- 素材与片段 --------
    @app.post("/api/v1/materials")
    def create_material():
        d = body()
        clips = [Clip(id=c["id"], material_id=d["id"], code=c["code"],
                      start_sec=int(c["start_sec"]), end_sec=int(c["end_sec"]),
                      description=c.get("description", ""))
                 for c in d.get("clips", [])]
        material = Material(id=d["id"], title=d["title"], kind=d["kind"],
                            license_key=d["license_key"],
                            maintainer_id=d["maintainer_id"])
        return jsonify(svc.add_material(actor(), material, clips).to_dict()), 201

    # -------- 授权版本 --------
    @app.post("/api/v1/licenses/<license_key>/versions")
    def create_license(license_key: str):
        d = body()
        lic = svc.add_license_version(
            actor(), license_key=license_key, rights_holder=d["rights_holder"],
            permitted_uses=d["permitted_uses"], regions=d["regions"],
            age_min=int(d["age_min"]), age_max=int(d["age_max"]),
            effective_from=_parse_date(d["effective_from"]),
            effective_to=_parse_date(d["effective_to"]),
            evidence_summary=d["evidence_summary"],
            evidence_refs=list(d["evidence_refs"]),
            supersedes_id=d.get("supersedes_id"))
        return jsonify(lic.to_dict()), 201

    @app.get("/api/v1/licenses/<license_key>/versions")
    def list_licenses(license_key: str):
        return jsonify(items=[v.to_dict()
                              for v in store.license_chain(license_key)])

    @app.post("/api/v1/revocations")
    def create_revocation():
        d = body()
        rev = svc.record_revocation(
            actor(), license_version_id=d["license_version_id"],
            covers_through=_parse_date(d["covers_through"]),
            reason=d["reason"], evidence_refs=list(d["evidence_refs"]))
        return jsonify(rev.to_dict()), 201

    # -------- 动作单元与组合版本 --------
    @app.post("/api/v1/action-units/<unit_key>/versions")
    def create_unit(unit_key: str):
        d = body()
        refs = [ClipRef(clip_id=r["clip_id"], material_id=r["material_id"])
                for r in d["clip_refs"]]
        uv = svc.add_action_unit(
            actor(), unit_key=unit_key, name=d["name"], clip_refs=refs,
            duration_sec=int(d["duration_sec"]), difficulty=int(d["difficulty"]))
        return jsonify(uv.to_dict()), 201

    @app.post("/api/v1/combos/<combo_key>/versions")
    def create_combo(combo_key: str):
        d = body()
        members = [(m["unit_version_id"], int(m["duration_sec"]))
                   for m in d["members"]]
        cv = svc.add_combo(actor(), combo_key=combo_key, name=d["name"],
                           members=members)
        return jsonify(cv.to_dict()), 201

    # -------- 排课 / 审批 / 发布 --------
    @app.post("/api/v1/schedules/<schedule_key>/versions")
    def submit_schedule(schedule_key: str):
        d = body()
        items = []
        for it in d["items"]:
            items.append({
                "date": _parse_date(it["date"]),
                "combo_version_id": it["combo_version_id"],
                "venue": it["venue"],
                "venue_limit": it.get("venue_limit", ""),
            })
        attachments = [Attachment(attachment_id=a["attachment_id"],
                                  name=a["name"], kind=a["kind"],
                                  content=a["content"])
                       for a in d.get("attachments", [])]
        sv = svc.submit_schedule(
            actor(), schedule_key=schedule_key, school_id=d["school_id"],
            grade=d["grade"], items=items, attachments=attachments,
            purpose=d.get("purpose", domain.USE_BREAK_EXERCISE))
        return jsonify(sv.to_dict()), 201

    @app.get("/api/v1/schedules/<schedule_key>/current")
    def current_schedule(schedule_key: str):
        cur = store.current(schedule_key)
        if cur is None:
            raise DomainError("课表不存在", code="not_found", status=404)
        return jsonify(cur.to_dict())

    @app.post("/api/v1/schedules/versions/<version_id>/approve")
    def approve(version_id: str):
        note = body().get("note", "")
        return jsonify(svc.approve_schedule(actor(), version_id, note).to_dict())

    @app.post("/api/v1/schedules/versions/<version_id>/publish")
    def publish(version_id: str):
        note = body().get("note", "")
        return jsonify(svc.publish_schedule(actor(), version_id, note).to_dict())

    @app.post("/api/v1/schedules/batch-publish")
    def batch_publish():
        ids = body()["schedule_version_ids"]
        out = svc.batch_publish(actor(), list(ids))
        return jsonify(items=[s.to_dict() for s in out])

    @app.post("/api/v1/schedules/versions/<version_id>/entries/<entry_id>/execute")
    def execute(version_id: str, entry_id: str):
        return jsonify(svc.mark_executed(actor(), version_id, entry_id).to_dict())

    # -------- 下载凭据 --------
    @app.post("/api/v1/schedules/versions/<version_id>/credentials")
    def issue_credential(version_id: str):
        cred = svc.issue_credential(actor(), version_id)
        return jsonify(cred.to_dict()), 201

    # -------- 到期检查 / 替换任务（可续办） --------
    @app.post("/api/v1/jobs/expiry-check")
    def create_expiry_job():
        scope = body().get("scope", "all")
        return jsonify(svc.create_expiry_job(actor(), scope).to_dict()), 201

    @app.post("/api/v1/jobs/replacement-run")
    def create_replacement_job():
        return jsonify(svc.create_replacement_job(actor()).to_dict()), 201

    @app.post("/api/v1/jobs/<job_id>/run")
    def run_job(job_id: str):
        limit_raw = request.args.get("limit")
        limit = int(limit_raw) if limit_raw else None
        return jsonify(svc.run_job(actor(), job_id, limit).to_dict())

    @app.get("/api/v1/jobs/<job_id>")
    def get_job(job_id: str):
        job = store.jobs.get(job_id)
        if job is None:
            raise DomainError("任务不存在", code="not_found", status=404)
        return jsonify(job.to_dict())

    # -------- 替换方案 --------
    @app.post("/api/v1/replacements")
    def propose_replacement():
        d = body()
        rep = svc.propose_replacement(
            actor(), old_schedule_version_id=d["old_schedule_version_id"],
            old_entry_ids=list(d["old_entry_ids"]),
            new_combo_version_id=d["new_combo_version_id"],
            revocation_id=d.get("revocation_id"),
            venue=d.get("venue"), venue_limit=d.get("venue_limit"))
        return jsonify(rep.to_dict()), 201

    @app.post("/api/v1/replacements/<replacement_id>/review")
    def review_replacement(replacement_id: str):
        d = body()
        rep = svc.review_replacement(actor(), replacement_id,
                                     approve=bool(d["approve"]),
                                     note=d.get("note", ""))
        return jsonify(rep.to_dict())

    @app.post("/api/v1/replacements/<replacement_id>/apply")
    def apply_replacement(replacement_id: str):
        d = body() or {}
        attachments = None
        if d.get("attachments") is not None:
            attachments = [Attachment(attachment_id=a["attachment_id"],
                                      name=a["name"], kind=a["kind"],
                                      content=a["content"])
                           for a in d["attachments"]]
        rep, new_sv = svc.apply_replacement(actor(), replacement_id, attachments)
        return jsonify(replacement=rep.to_dict(),
                       schedule_version=new_sv.to_dict())

    @app.get("/api/v1/replacements/<replacement_id>")
    def get_replacement(replacement_id: str):
        rep = store.replacements.get(replacement_id)
        if rep is None:
            raise DomainError("替换方案不存在", code="not_found", status=404)
        return jsonify(rep.to_dict())

    # -------- 学校通告 --------
    @app.get("/api/v1/schools/<school_id>/notices")
    def school_notices(school_id: str):
        out = svc.school_notices(actor(), school_id)
        return jsonify(items=[n.to_dict() for n in out])

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
