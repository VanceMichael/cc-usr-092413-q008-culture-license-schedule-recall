"""端到端场景测试：授权提前终止 → 精准影响面 → 替换审批分离 →
原子换版 → 凭据门控 → 任务中断续办 → 学校通告五要素。
"""
from __future__ import annotations

from datetime import date

import pytest

from domain import (
    Attachment, Clip, ClipRef, Material, School, User, DomainError,
    ROLE_COACH, ROLE_MAINTAINER, ROLE_PUBLISHER, ROLE_REVIEWER,
    USE_BREAK_EXERCISE,
)
from service import Service
from store import Store


@pytest.fixture
def world():
    store = Store()
    svc = Service(store)

    maintainer = svc.register_user(User("u_mei", "小梅", ROLE_MAINTAINER))
    coach = svc.register_user(User("u_wang", "王教研", ROLE_COACH))
    reviewer = svc.register_user(User("u_li", "李审核", ROLE_REVIEWER))
    publisher = svc.register_user(User("u_zhou", "周发布", ROLE_PUBLISHER))

    s1 = svc.register_school(maintainer, School(
        "S1", "实验一小", "华东",
        {"一年级": (6, 7), "二年级": (7, 8), "三年级": (8, 9), "五年级": (10, 11)}))
    s2 = svc.register_school(maintainer, School(
        "S2", "实验二小", "华南", {"三年级": (8, 9)}))

    # 素材 M1：壁画动作参考（被撤销的授权链）
    svc.add_material(maintainer, Material("M1", "敦煌壁画动作参考", "mural",
                                          "L-MURAL", maintainer.id), [
        Clip("C1", "M1", "mural-01", 0, 30, "飞天展臂"),
        Clip("C2", "M1", "mural-02", 30, 60, "反弹琵琶"),
    ])
    # 素材 M2：节拍训练（独立授权链，不受 M1 撤销影响）
    svc.add_material(maintainer, Material("M2", "节拍训练", "rhythm",
                                          "L-RHYTHM", maintainer.id), [
        Clip("C3", "M2", "beat-01", 0, 40, "踏步击掌"),
        Clip("C4", "M2", "beat-02", 0, 40, "高抬腿组合"),
    ])

    lic_mural = svc.add_license_version(
        maintainer, "L-MURAL", "敦煌文创中心", [USE_BREAK_EXERCISE], ["*"],
        6, 12, date(2026, 1, 1), date(2026, 12, 31),
        "校园授权合同 M-2026-018 扫描件，允许课间操动作参考",
        ["EV-CONTRACT-018", "EV-EMAIL-20260105"])
    lic_rhythm = svc.add_license_version(
        maintainer, "L-RHYTHM", "体卫艺教研室", [USE_BREAK_EXERCISE], ["*"],
        6, 12, date(2026, 1, 1), date(2026, 12, 31),
        "公有节拍训练素材授权确认函", ["EV-LETTER-R01"])

    # 动作单元版本必须标明实际引用的片段
    u1 = svc.add_action_unit(maintainer, "U-ARM", "展臂运动",
                             [ClipRef("C1", "M1")], 240, 2)
    u2 = svc.add_action_unit(maintainer, "U-LUTE", "琵琶运动",
                             [ClipRef("C2", "M1")], 240, 3)
    u3 = svc.add_action_unit(maintainer, "U-BEAT", "踏步击掌",
                             [ClipRef("C3", "M2")], 480, 2)
    u_hard = svc.add_action_unit(maintainer, "U-HIGH", "高抬腿",
                                 [ClipRef("C4", "M2")], 480, 5)

    # 组合版本：壁画课间操（引用 C1/C2）；替代组合（只引用 M2）
    combo_mural = svc.add_combo(maintainer, "K-MURAL", "壁画课间操",
                                [(u1.id, 240), (u2.id, 240)])
    combo_alt = svc.add_combo(maintainer, "K-ALT", "节拍替代操",
                              [(u3.id, 480)])
    combo_hard = svc.add_combo(maintainer, "K-HARD", "高难度替代",
                               [(u_hard.id, 480)])

    return dict(store=store, svc=svc, maintainer=maintainer, coach=coach,
                reviewer=reviewer, publisher=publisher, s1=s1, s2=s2,
                lic_mural=lic_mural, lic_rhythm=lic_rhythm,
                u1=u1, u2=u2, u3=u3,
                combo_mural=combo_mural, combo_alt=combo_alt,
                combo_hard=combo_hard)


def _submit_october(world, key, school_id, grade, combo=None):
    svc = world["svc"]
    combo = combo or world["combo_mural"]
    att = Attachment("A1", "安全提示", "note", "操场湿滑暂停")
    sv = svc.submit_schedule(world["coach"], key, school_id, grade, [
        {"date": date(2026, 10, 8), "combo_version_id": combo.id,
         "venue": "操场", "venue_limit": "每班40人"},
        {"date": date(2026, 10, 9), "combo_version_id": combo.id,
         "venue": "操场", "venue_limit": "每班40人"},
        {"date": date(2026, 10, 10), "combo_version_id": combo.id,
         "venue": "操场", "venue_limit": "每班40人"},
    ], [att])
    return sv


def _approve_publish(world, sv):
    world["svc"].approve_schedule(world["reviewer"], sv.id, "审核通过")
    return world["svc"].publish_schedule(world["publisher"], sv.id, "发布")


# ---------- 1. 授权版本与片段引用可追溯 ----------

def test_license_version_is_traceable(world):
    lic = world["lic_mural"]
    assert lic.rights_holder == "敦煌文创中心"
    assert lic.permitted_uses == [USE_BREAK_EXERCISE]
    assert lic.regions == ["*"]
    assert (lic.age_min, lic.age_max) == (6, 12)
    assert lic.effective_from == date(2026, 1, 1)
    assert lic.evidence_summary and lic.evidence_refs
    chain = world["store"].license_chain("L-MURAL")
    assert [v.version_no for v in chain] == [1]


def test_narrowing_appends_new_version_without_touching_old(world):
    v2 = world["svc"].add_license_version(
        world["maintainer"], "L-MURAL", "敦煌文创中心",
        [USE_BREAK_EXERCISE], ["华东"], 8, 11,
        date(2026, 10, 1), date(2026, 12, 31),
        "授权地区年龄收窄补充函", ["EV-ADDENDUM-02"])
    chain = world["store"].license_chain("L-MURAL")
    assert [v.version_no for v in chain] == [1, 2]
    assert v2.supersedes_id == world["lic_mural"].id
    # 旧版本证据原样保留
    assert chain[0].regions == ["*"] and chain[0].evidence_refs


def test_unit_and_combo_declare_actual_clip_refs(world):
    assert [r.clip_id for r in world["u1"].clip_refs] == ["C1"]
    refs = world["svc"]._combo_clip_refs(world["combo_mural"])
    assert {r.clip_id for r in refs} == {"C1", "C2"}
    assert world["combo_mural"].duration_sec == 480
    assert world["combo_mural"].difficulty == 3


def test_unit_cannot_reference_unknown_clip(world):
    with pytest.raises(DomainError):
        world["svc"].add_action_unit(
            world["maintainer"], "U-X", "悬空引用",
            [ClipRef("NOPE", "M1")], 60, 1)


# ---------- 2. 排课即校验全窗口 + 留存审批水位 ----------

def test_scheduling_records_school_grade_dates_venue_watermark(world):
    sv = _submit_october(world, "SCH-S1-G3", "S1", "三年级")
    e = sv.entries[0]
    assert (e.age_min, e.age_max) == (8, 9)
    assert e.venue == "操场" and e.venue_limit == "每班40人"
    # 每天每片段的授权依据均已快照，水位=排课
    assert {b.license_version_id for b in e.license_basis} == {world["lic_mural"].id}
    assert all(b.water_level == 1 for b in e.license_basis)
    assert len(e.license_basis) == 2  # C1、C2 两个片段
    assert sv.approval_trace[0].level == 1


def test_scheduling_rejected_when_window_not_fully_covered(world):
    # 12 月的安排落在壁画授权终止日之后：排课阶段即拒绝（窗口校验）
    with pytest.raises(DomainError) as ei:
        world["svc"].submit_schedule(
            world["coach"], "SCH-BAD", "S1", "三年级", [
                {"date": date(2026, 10, 10),
                 "combo_version_id": world["combo_mural"].id,
                 "venue": "操场", "venue_limit": ""},
                {"date": date(2027, 1, 15),
                 "combo_version_id": world["combo_mural"].id,
                 "venue": "操场", "venue_limit": ""},
            ], [])
    assert ei.value.code == "coverage_gap"


def test_region_and_age_narrowing_blocks_scheduling(world):
    # 收窄到华东、8-11 岁
    world["svc"].add_license_version(
        world["maintainer"], "L-RHYTHM", "体卫艺教研室",
        [USE_BREAK_EXERCISE], ["华东"], 8, 11,
        date(2026, 10, 1), date(2026, 12, 31),
        "地区年龄收窄", ["EV-R02"])
    # 华南学校（S2）在 10 月使用节拍组合：地区不匹配
    with pytest.raises(DomainError):
        world["svc"].submit_schedule(
            world["coach"], "SCH-S2", "S2", "三年级", [
                {"date": date(2026, 10, 9),
                 "combo_version_id": world["combo_alt"].id,
                 "venue": "操场", "venue_limit": ""}], [])
    # 一年级 6-7 岁：年龄不匹配
    with pytest.raises(DomainError):
        world["svc"].submit_schedule(
            world["coach"], "SCH-S1-G1X", "S1", "一年级", [
                {"date": date(2026, 10, 9),
                 "combo_version_id": world["combo_alt"].id,
                 "venue": "操场", "venue_limit": ""}], [])


# ---------- 3. 撤销后的精准影响面 ----------

@pytest.fixture
def published_world(world):
    scheds = {}
    scheds["g3"] = _submit_october(world, "SCH-S1-G3", "S1", "三年级")
    scheds["g1"] = _submit_october(world, "SCH-S1-G1", "S1", "一年级")
    scheds["s2"] = _submit_october(world, "SCH-S2-G3", "S2", "三年级")
    # 对照组：只依赖 M2 的课表，撤销不应触及
    scheds["m2"] = world["svc"].submit_schedule(
        world["coach"], "SCH-S1-G5", "S1", "五年级", [
            {"date": date(2026, 10, 10),
             "combo_version_id": world["combo_alt"].id,
             "venue": "室内馆", "venue_limit": "全员"}], [])
    for sv in scheds.values():
        _approve_publish(world, sv)

    # 10/8 的三年级课已完成
    world["svc"].mark_executed(world["coach"], scheds["g3"].id, "ent_SCH-S1-G3_01")

    # 撤销通知：授权覆盖至 10/9（含），10/10 起失效
    rev = world["svc"].record_revocation(
        world["maintainer"], world["lic_mural"].id,
        date(2026, 10, 9), "权利方提前终止校园壁画动作参考授权",
        ["EV-REVOKE-20260925"])
    world["scheds"] = scheds
    world["rev"] = rev
    return world


def test_impact_only_unexecuted_and_actually_dependent(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")
    w["svc"].run_job(w["coach"], job.id)

    g3 = w["store"].schedules_by_id[w["scheds"]["g3"].id]
    by_date = {e.date: e for e in g3.entries}
    # 已完成课程保留原依据
    assert by_date[date(2026, 10, 8)].status == "executed"
    assert by_date[date(2026, 10, 8)].license_basis
    # 仍在覆盖窗口内（10/9）的安排不撤回
    assert by_date[date(2026, 10, 9)].status == "scheduled"
    assert by_date[date(2026, 10, 9)].basis_revoked is False
    # 只有未执行且确实依赖失效片段的 10/10 被标记
    assert by_date[date(2026, 10, 10)].status == "scheduled"
    assert by_date[date(2026, 10, 10)].basis_revoked is True
    assert g3.status == "affected"

    # 其他使用壁画的课表同样命中 10/10
    for key in ("g1", "s2"):
        sv = w["store"].schedules_by_id[w["scheds"][key].id]
        assert sv.status == "affected"

    # 对照组（只用 M2）不撤回
    m2 = w["store"].schedules_by_id[w["scheds"]["m2"].id]
    assert m2.status == "published"
    assert all(not e.basis_revoked for e in m2.entries)

    impact = {r["schedule_key"]: r for r in job.affected}
    assert set(impact) == {"SCH-S1-G3", "SCH-S1-G1", "SCH-S2-G3"}
    assert impact["SCH-S1-G3"]["affected_dates"] == ["2026-10-10"]


# ---------- 4. 替换方案：引用原课表 + 时长/难度/适龄 ----------

def test_replacement_validation(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")
    w["svc"].run_job(w["coach"], job.id)
    old = w["store"].schedules_by_id[w["scheds"]["g3"].id]
    target = "ent_SCH-S1-G3_03"  # 10/10

    # 时长不达标（高难度组合也是 480，但难度 5 > 原 3）
    with pytest.raises(DomainError) as ei:
        w["svc"].propose_replacement(
            w["coach"], old.id, [target], w["combo_hard"].id, w["rev"].id)
    assert "难度" in str(ei.value)

    # 不能借替换撤回未失效的安排
    with pytest.raises(DomainError):
        w["svc"].propose_replacement(
            w["coach"], old.id, ["ent_SCH-S1-G3_02"],  # 10/9 仍覆盖
            w["combo_alt"].id, w["rev"].id)

    # 不能替换已完成课程
    with pytest.raises(DomainError):
        w["svc"].propose_replacement(
            w["coach"], old.id, ["ent_SCH-S1-G3_01"],  # 10/8 executed
            w["combo_alt"].id, w["rev"].id)

    # 合规替换：同 480 秒、难度 2≤3、M2 授权覆盖
    rep = w["svc"].propose_replacement(
        w["coach"], old.id, [target], w["combo_alt"].id, w["rev"].id)
    assert rep.checks["duration_within_tolerance"] is True
    assert rep.checks["difficulty_ok"] is True
    assert rep.checks["age_appropriate"] is True
    assert rep.old_schedule_version_id == old.id  # 引用原课表


def test_replacement_using_revoked_material_fails_age_coverage(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")
    w["svc"].run_job(w["coach"], job.id)
    old = w["store"].schedules_by_id[w["scheds"]["g3"].id]
    # 仍用壁画组合替换 10/10：授权不覆盖 → 适龄/授权条件验证失败
    with pytest.raises(DomainError):
        w["svc"].propose_replacement(
            w["coach"], old.id, ["ent_SCH-S1-G3_03"],
            w["combo_mural"].id, w["rev"].id)


# ---------- 5. 职责分离：提交者不能放行自己 ----------

def test_submitter_cannot_approve_own_work(world):
    sv = _submit_october(world, "SCH-SEG", "S1", "三年级")
    # 排课人（教研员）不能审核
    with pytest.raises(DomainError) as ei:
        world["svc"].approve_schedule(world["coach"], sv.id)
    assert ei.value.status == 403

    # 纵深防御：即使构造同 id 的审核角色，也拒绝自审
    rogue = User(world["coach"].id, "王教研", ROLE_REVIEWER)
    with pytest.raises(DomainError) as ei:
        world["svc"].approve_schedule(rogue, sv.id)
    assert ei.value.code == "segregation"


def test_replacement_reviewer_must_differ_from_submitter(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")
    w["svc"].run_job(w["coach"], job.id)
    old = w["store"].schedules_by_id[w["scheds"]["g3"].id]
    rep = w["svc"].propose_replacement(
        w["coach"], old.id, ["ent_SCH-S1-G3_03"],
        w["combo_alt"].id, w["rev"].id)
    # 提交者本人（伪装审核角色）不得放行
    rogue = User(w["coach"].id, "王教研", ROLE_REVIEWER)
    with pytest.raises(DomainError) as ei:
        w["svc"].review_replacement(rogue, rep.id, True)
    assert ei.value.code == "segregation"
    # 维护者不能审核
    with pytest.raises(DomainError):
        w["svc"].review_replacement(w["maintainer"], rep.id, True)


# ---------- 6. 原子换版：主课表 + 附件一次成功 ----------

def test_apply_replacement_switches_main_and_attachments_atomically(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")
    w["svc"].run_job(w["coach"], job.id)
    old = w["store"].schedules_by_id[w["scheds"]["g3"].id]
    target = "ent_SCH-S1-G3_03"
    rep = w["svc"].propose_replacement(
        w["coach"], old.id, [target], w["combo_alt"].id, w["rev"].id)
    w["svc"].review_replacement(w["reviewer"], rep.id, True, "替代方案合规")

    new_att = Attachment("A2", "换版说明", "note", "10/10 起改用节拍替代操")
    rep_done, new_sv = w["svc"].apply_replacement(
        w["publisher"], rep.id, [new_att])

    # 指针整体切换；旧版本保留且标记 replaced
    assert w["store"].current("SCH-S1-G3").id == new_sv.id
    assert old.status == "replaced"
    assert new_sv.supersedes_id == old.id
    assert new_sv.origin_replacement_id == rep.id
    assert new_sv.origin_schedule_version_id == old.id

    by_date = {e.date: e for e in new_sv.entries}
    # 10/10 换成替代组合；其余日期保留原安排
    assert by_date[date(2026, 10, 10)].combo_version_id == w["combo_alt"].id
    assert by_date[date(2026, 10, 9)].combo_version_id == w["combo_mural"].id
    # 已完成课程原样保留（含原依据）
    assert by_date[date(2026, 10, 8)].status == "executed"
    assert by_date[date(2026, 10, 8)].license_basis
    # 主表与附件一起换版
    assert [a.attachment_id for a in new_sv.attachments] == ["A2"]
    assert [a.attachment_id for a in old.attachments] == ["A1"]
    # 新版本直接为已发布，水位链完整（排课/审核/发布/换版再发布）
    assert new_sv.status == "published"
    assert [t.level for t in new_sv.approval_trace] == [1, 2, 3, 3]
    assert rep_done.status == "applied"


def test_only_publisher_may_apply_replacement(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")
    w["svc"].run_job(w["coach"], job.id)
    old = w["store"].schedules_by_id[w["scheds"]["g3"].id]
    rep = w["svc"].propose_replacement(
        w["coach"], old.id, ["ent_SCH-S1-G3_03"],
        w["combo_alt"].id, w["rev"].id)
    with pytest.raises(DomainError):
        w["svc"].apply_replacement(w["coach"], rep.id)
    # 未放行不得换版
    with pytest.raises(DomainError):
        w["svc"].apply_replacement(w["publisher"], rep.id)


# ---------- 7. 撤销与发布同时到达 / 凭据门控 ----------

def test_batch_publish_aborts_when_revocation_arrives_concurrently(world):
    # 两份已审核待发布：一份依赖壁画，一份只用 M2
    sv_bad = _submit_october(world, "SCH-RACE-1", "S1", "二年级")
    sv_ok = world["svc"].submit_schedule(
        world["coach"], "SCH-RACE-2", "S1", "五年级", [
            {"date": date(2026, 10, 10),
             "combo_version_id": world["combo_alt"].id,
             "venue": "室内馆", "venue_limit": "全员"}], [])
    world["svc"].approve_schedule(world["reviewer"], sv_bad.id)
    world["svc"].approve_schedule(world["reviewer"], sv_ok.id)

    # 撤销通知恰好在批量发布前到达
    world["svc"].record_revocation(
        world["maintainer"], world["lic_mural"].id, date(2026, 10, 9),
        "提前终止", ["EV-REV"])

    with pytest.raises(DomainError) as ei:
        world["svc"].batch_publish(world["publisher"], [sv_bad.id, sv_ok.id])
    assert ei.value.code == "coverage_gap"
    # 整批中止：无一份发布
    assert world["store"].schedules_by_id[sv_bad.id].status == "approved"
    assert world["store"].schedules_by_id[sv_ok.id].status == "approved"
    # 失效课表不得生成下载凭据
    with pytest.raises(DomainError) as ei:
        world["svc"].issue_credential(world["publisher"], sv_bad.id)
    assert ei.value.code in ("coverage_gap", "not_publishable")
    # 未受影响的课表可单独发布
    world["svc"].publish_schedule(world["publisher"], sv_ok.id)
    cred = world["svc"].issue_credential(world["publisher"], sv_ok.id)
    assert cred.active


def test_credentials_deactivate_when_basis_revoked_or_replaced(world):
    w = world
    # 先发布并签发凭据（撤销通知尚未到达）
    g3 = _submit_october(w, "SCH-S1-G3", "S1", "三年级")
    _approve_publish(w, g3)
    cred = w["svc"].issue_credential(w["publisher"], g3.id)
    assert cred.active is True

    rev = w["svc"].record_revocation(
        w["maintainer"], w["lic_mural"].id, date(2026, 10, 9),
        "权利方提前终止校园壁画动作参考授权", ["EV-REVOKE-20260925"])
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{rev.id}")
    w["svc"].run_job(w["coach"], job.id)
    # 撤销处理后旧凭据立即失活，且不能再签发
    assert w["store"].credentials[cred.id].active is False
    old = w["store"].schedules_by_id[g3.id]
    with pytest.raises(DomainError):
        w["svc"].issue_credential(w["publisher"], old.id)

    # 换版后可对新版本签发
    rep = w["svc"].propose_replacement(
        w["coach"], old.id, ["ent_SCH-S1-G3_03"],
        w["combo_alt"].id, rev.id)
    w["svc"].review_replacement(w["reviewer"], rep.id, True)
    _, new_sv = w["svc"].apply_replacement(w["publisher"], rep.id)
    new_cred = w["svc"].issue_credential(w["publisher"], new_sv.id)
    assert new_cred.active and new_cred.schedule_version_id == new_sv.id


# ---------- 8. 任务中断续办（幂等） ----------

def test_job_interrupts_and_resumes(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")

    w["svc"].run_job(w["coach"], job.id, limit=1)
    assert job.status == "interrupted"
    assert len(job.processed) == 1

    w["svc"].run_job(w["coach"], job.id, limit=1)
    assert job.status == "interrupted"
    assert len(job.processed) == 2

    w["svc"].run_job(w["coach"], job.id)
    assert job.status == "completed"
    assert len(job.processed) == 3  # 三份壁画课表；M2 对照组不在 processed
    assert len(job.affected) == 3
    attempts_before = job.attempts

    # 续办幂等：再次运行不重复处理
    w["svc"].run_job(w["coach"], job.id)
    assert job.status == "completed"
    assert len(job.processed) == 3
    assert job.attempts == attempts_before + 1


def test_replacement_job_interrupts_and_resumes(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")
    w["svc"].run_job(w["coach"], job.id)

    reps = []
    for key, entry in [("g3", "ent_SCH-S1-G3_03"),
                       ("g1", "ent_SCH-S1-G1_03"),
                       ("s2", "ent_SCH-S2-G3_03")]:
        old = w["store"].schedules_by_id[w["scheds"][key].id]
        rep = w["svc"].propose_replacement(
            w["coach"], old.id, [entry], w["combo_alt"].id, w["rev"].id)
        w["svc"].review_replacement(w["reviewer"], rep.id, True)
        reps.append(rep)

    run = w["svc"].create_replacement_job(w["publisher"])
    w["svc"].run_job(w["publisher"], run.id, limit=2)
    assert run.status == "interrupted"
    assert len(run.processed) == 2
    w["svc"].run_job(w["publisher"], run.id)
    assert run.status == "completed"
    assert len(run.processed) == 3
    assert all(rep.status == "applied" for rep in reps)


# ---------- 9. 学校通告五要素 ----------

def test_school_notice_contains_five_required_parts(published_world):
    w = published_world
    job = w["svc"].create_expiry_job(w["coach"], scope=f"revocation:{w['rev'].id}")
    w["svc"].run_job(w["coach"], job.id)
    old = w["store"].schedules_by_id[w["scheds"]["g3"].id]

    notices = w["svc"].school_notices(w["coach"], "S1")
    assert notices  # 撤销后即生成说明
    n = [x for x in notices if x.schedule_key == "SCH-S1-G3"][0]
    assert n.affected_dates == ["2026-10-10"]              # 受影响日期
    assert "提前终止" in n.withdrawal_reason                # 撤回原因
    assert n.license_basis and n.license_basis[0]["rights_holder"]  # 授权依据
    assert n.new_arrangement_ref["status"] == "pending"    # 新安排去向

    # 换版后说明更新：采用版本 + 新安排去向
    rep = w["svc"].propose_replacement(
        w["coach"], old.id, ["ent_SCH-S1-G3_03"],
        w["combo_alt"].id, w["rev"].id)
    w["svc"].review_replacement(w["reviewer"], rep.id, True)
    _, new_sv = w["svc"].apply_replacement(w["publisher"], rep.id)

    notices = w["svc"].school_notices(w["coach"], "S1")
    n = [x for x in notices if x.schedule_key == "SCH-S1-G3"][0]
    assert n.adopted_versions[0]["schedule_version_id"] == new_sv.id  # 采用版本
    assert n.adopted_versions[0]["state"] == "published"
    assert n.new_arrangement_ref == {
        "status": "arranged", "replacement_id": rep.id,
        "schedule_key": "SCH-S1-G3", "new_schedule_version_id": new_sv.id}
    assert n.license_basis  # 新安排的授权依据（M2 链）
    assert {b["license_version_id"] for b in n.license_basis} == {w["lic_rhythm"].id}
