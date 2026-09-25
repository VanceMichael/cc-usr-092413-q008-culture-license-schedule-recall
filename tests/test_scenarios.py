"""端到端业务场景：授权收窄 -> 影响分析 -> 替换换版 -> 告知。"""
from datetime import date

import pytest

from service.errors import (
    InvalidState,
    LicenseCoverageError,
    SegregationError,
)
from service.models import ENTRY_EXECUTED, ENTRY_REPLACED, ENTRY_WITHDRAWN, School

D = date.fromisoformat
MAINTAINER = "m_mural"
REVIEWER = "r_curriculum"
PUBLISHER = "p_district"
MULTI = "multi_user"


def _build_world(svc):
    cat = svc.catalog
    # 学校：三所在授权地区，一所在未授权地区
    for sid, name, region in [
        ("S1", "第一小学", "华东区"),
        ("S2", "第二小学", "华东区"),
        ("S3", "第三小学", "华东区"),
        ("S4", "华南小学", "华南区"),
    ]:
        svc.store.add_school(
            School(school_id=sid, name=name, region=region))

    cat.register_clip("clip_mural", "敦煌壁画飞天动作参考", 60,
                      "archive/mural#A", MAINTAINER)
    cat.register_clip("clip_drum", "鼓舞动作参考", 60,
                      "archive/drum#B", MAINTAINER)
    cat.register_clip("clip_ribbon", "绸带操动作参考", 60,
                      "archive/ribbon#C", MAINTAINER)

    cat.grant_license(
        "L1", "敦煌文化基金会", ["课间操"], ["华东区"], ["clip_mural"],
        7, 10, D("2026-01-01"), D("2026-12-31"),
        "校园课间操授权合同第 12 条，仅限华东区 7-10 岁",
        "contract/L1.pdf", MAINTAINER)
    cat.grant_license(
        "L2", "省非遗保护中心", ["课间操"], ["华东区"],
        ["clip_drum", "clip_ribbon"], 6, 12,
        D("2026-01-01"), D("2027-12-31"),
        "非遗动作校园使用授权", "contract/L2.pdf", MAINTAINER)

    cat.create_unit_version(
        "U_mural", "壁画飞天单元",
        [{"clip_id": "clip_mural", "start_sec": 0, "end_sec": 60}],
        120, 2, 7, 10, MAINTAINER)
    cat.create_unit_version(
        "U_drum", "鼓舞单元",
        [{"clip_id": "clip_drum", "start_sec": 0, "end_sec": 60}],
        120, 2, 7, 10, MAINTAINER)
    # 替换候选：绸带操，时长/难度/适龄与壁画单元一致
    cat.create_unit_version(
        "U_ribbon", "绸带操单元",
        [{"clip_id": "clip_ribbon", "start_sec": 0, "end_sec": 60}],
        120, 2, 7, 10, MAINTAINER)
    cat.create_combo_version(
        "K_morning", "晨间组合",
        [{"unit_id": "U_mural", "version": 1}], MAINTAINER)
    return svc


def _schedule(svc, school, grade, age, dates, content=("unit", "U_mural", 1)):
    return svc.schedule.schedule(
        school_id=school, grade=grade, dates=dates,
        venue_constraint="操场（雨天转体育馆）", student_age=age,
        use_code="课间操", content_type=content[0], content_id=content[1],
        content_version=content[2], required_level=2, granted_level=2,
        scheduled_by=PUBLISHER)


def test_license_version_chain_is_traceable(svc):
    _build_world(svc)
    lv2 = svc.catalog.revise_license(
        "L1", "敦煌文化基金会", ["课间操"], ["华东区"], ["clip_mural"],
        8, 9, D("2026-06-01"), D("2026-12-31"),
        "收窄：年龄范围调整为 8-9 岁", "contract/L1-v2.pdf", MAINTAINER)
    chain = svc.catalog.get_version_chain("L1")
    assert [v.version for v in chain] == [1, 2]
    assert lv2.supersedes == 1
    # 旧版本仍保留，证据可追溯
    assert chain[0].evidence_ref == "contract/L1.pdf"
    assert chain[0].rights_holder == "敦煌文化基金会"


def test_unit_and_combo_record_actual_clip_refs(svc):
    _build_world(svc)
    uv = svc.store.unit_version_at("U_mural", 1)
    assert [(r.clip_id, r.start_sec, r.end_sec) for r in uv.clip_refs] == [
        ("clip_mural", 0, 60)]
    cv = svc.store.combo_version_at("K_morning", 1)
    assert cv.resolved_clip_ids == ["clip_mural"]
    with pytest.raises(Exception):
        svc.catalog.create_unit_version(
            "U_empty", "无引用单元", [], 60, 1, 7, 10, MAINTAINER)


def test_schedule_validates_use_region_age_and_window(svc):
    _build_world(svc)
    # 用途不匹配
    with pytest.raises(LicenseCoverageError):
        svc.schedule.schedule(
            school_id="S1", grade="三年级", dates=[D("2026-10-16")],
            venue_constraint="操场", student_age=8, use_code="商业演出",
            content_type="unit", content_id="U_mural", content_version=1,
            required_level=2, granted_level=2, scheduled_by=PUBLISHER)
    # 地区不匹配
    with pytest.raises(LicenseCoverageError):
        _schedule(svc, "S4", "三年级", 8, [D("2026-10-16")])
    # 年龄不匹配
    with pytest.raises(LicenseCoverageError):
        _schedule(svc, "S1", "五年级", 11, [D("2026-10-16")])
    # 日期超出授权窗口
    with pytest.raises(LicenseCoverageError):
        _schedule(svc, "S1", "三年级", 8, [D("2027-03-01")])
    # 审批水位不足
    with pytest.raises(Exception):
        svc.schedule.schedule(
            school_id="S1", grade="三年级", dates=[D("2026-10-16")],
            venue_constraint="操场", student_age=8, use_code="课间操",
            content_type="unit", content_id="U_mural", content_version=1,
            required_level=3, granted_level=2, scheduled_by=PUBLISHER)


def test_schedule_locks_basis_and_approval_watermark(svc):
    _build_world(svc)
    entry = _schedule(svc, "S1", "三年级", 8,
                      [D("2026-10-09"), D("2026-10-16"), D("2026-10-19")])
    assert entry.approval_policy_version
    assert entry.granted_level == entry.required_level == 2
    basis = {(b.clip_id, b.license_id, b.version) for b in entry.license_basis}
    assert basis == {("clip_mural", "L1", 1)}
    assert entry.resolved_clip_ids == ["clip_mural"]


def test_revocation_only_hits_unexecuted_dependent_arrangements(svc):
    _build_world(svc)
    e1 = _schedule(svc, "S1", "三年级", 8,
                   [D("2026-10-09"), D("2026-10-16"), D("2026-10-19")])
    e2 = _schedule(svc, "S2", "五年级", 10,
                   [D("2026-10-16"), D("2026-10-20")])
    # 另一所学校用鼓舞单元（不同授权、不同片段），不应受影响
    e3 = _schedule(svc, "S3", "四年级", 9, [D("2026-10-16")],
                   content=("unit", "U_drum", 1))
    # 10-09 的课在终止生效前已完成
    svc.schedule.mark_executed(e1.entry_id, D("2026-10-09"), PUBLISHER)

    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权",
        detail="素材馆邮件通知，授权下月起停止", created_by=MAINTAINER)
    notices = svc.revocation.analyze_impact("N1")

    affected_entries = {n.entry_id: n for n in notices}
    assert set(affected_entries) == {e1.entry_id, e2.entry_id}
    e1 = svc.store.get_entry(e1.entry_id)
    # 已完成日期保留原依据、不撤回；只撤回未执行且失效的日期
    assert D("2026-10-09") in e1.executed_dates
    assert D("2026-10-09") not in e1.withdrawn_dates
    assert set(e1.withdrawn_dates) == {D("2026-10-16"), D("2026-10-19")}
    assert e1.status != ENTRY_EXECUTED
    e2 = svc.store.get_entry(e2.entry_id)
    assert e2.status == ENTRY_WITHDRAWN
    # 鼓舞课不受影响
    assert svc.store.get_entry(e3.entry_id).withdrawn_dates == []

    # 通知重复送达幂等
    again = svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权", detail="重发", created_by=MAINTAINER)
    assert again.notice_id == "N1"
    assert len(svc.revocation.analyze_impact("N1")) == 2


def test_invalid_entries_cannot_publish_and_doc_version_is_atomic(svc):
    _build_world(svc)
    e2 = _schedule(svc, "S2", "五年级", 10, [D("2026-10-16")])
    e3 = _schedule(svc, "S3", "四年级", 9, [D("2026-10-16")],
                   content=("unit", "U_drum", 1))
    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权", detail="", created_by=MAINTAINER)
    svc.revocation.analyze_impact("N1")

    before = len(svc.store.docs)
    # 有效 + 失效混排：整批失败，不留半成品版本
    with pytest.raises(LicenseCoverageError):
        svc.schedule.publish_batch(
            entry_ids=[e2.entry_id, e3.entry_id],
            attachments=[{"name": "安全须知", "ref": "refs://safety.pdf"}],
            published_by=PUBLISHER)
    assert len(svc.store.docs) == before
    # 全部有效才整版成功，主课表与附件同版
    docs = svc.schedule.publish_batch(
        entry_ids=[e3.entry_id],
        attachments=[{"name": "安全须知", "ref": "refs://safety.pdf"}],
        published_by=PUBLISHER)
    assert docs[0].version == 1
    assert docs[0].main_ref and docs[0].attachments[0].name == "安全须知"
    assert docs[0].entry_ids == [e3.entry_id]


def test_credentials_blocked_once_invalid(svc):
    _build_world(svc)
    e2 = _schedule(svc, "S2", "五年级", 10, [D("2026-10-16")])
    # 撤销前签发成功
    cred = svc.schedule.issue_credential(
        e2.entry_id, D("2026-10-16"), issued_by=PUBLISHER)
    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权", detail="", created_by=MAINTAINER)
    svc.revocation.analyze_impact("N1")
    # 撤销与发布同时到达的语义：已签发凭据立即失效
    with pytest.raises(InvalidState):
        svc.schedule.redeem_credential(cred.token)
    # 失效日期不再签发新凭据
    with pytest.raises(LicenseCoverageError):
        svc.schedule.issue_credential(
            e2.entry_id, D("2026-10-16"), issued_by=PUBLISHER)


def test_replacement_flow_segregation_and_notice(svc):
    _build_world(svc)
    e2 = _schedule(svc, "S2", "五年级", 10, [D("2026-10-16")])
    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权",
        detail="素材馆通知", created_by=MAINTAINER)
    svc.revocation.analyze_impact("N1")

    # 只能替换已撤回且未执行的日期
    with pytest.raises(InvalidState):
        svc.revocation.submit_replacement(
            original_entry_id=e2.entry_id, dates=[D("2026-10-09")],
            content_type="unit", content_id="U_mural", content_version=1,
            submitted_by=MAINTAINER)

    repl = svc.revocation.submit_replacement(
        original_entry_id=e2.entry_id, dates=[D("2026-10-16")],
        content_type="unit", content_id="U_ribbon", content_version=1,
        submitted_by=MAINTAINER, reason="改用授权有效的绸带操单元")
    # 校验留痕：时长、难度、适龄
    assert repl.checks.duration_diff_pct == 0
    assert repl.checks.new_difficulty == repl.checks.old_difficulty

    # 提交人不能审核/发布自己的替换单
    with pytest.raises(SegregationError):
        svc.revocation.review_replacement(
            repl.replacement_id, reviewer=MAINTAINER, approve=True)
    # 未通过审核不能发布
    with pytest.raises(InvalidState):
        svc.revocation.publish_replacement(
            repl.replacement_id, publisher=PUBLISHER)

    svc.revocation.review_replacement(
        repl.replacement_id, reviewer=REVIEWER, approve=True,
        note="时长难度适龄一致，授权覆盖窗口")
    # 发布人也不能是提交人
    with pytest.raises(SegregationError):
        svc.revocation.publish_replacement(
            repl.replacement_id, publisher=MAINTAINER)
    published = svc.revocation.publish_replacement(
        repl.replacement_id, publisher=PUBLISHER,
        attachments=[{"name": "动作图示", "ref": "refs://ribbon.png"}])
    assert published.status == "published"

    old = svc.store.get_entry(e2.entry_id)
    new = svc.store.get_entry(published.new_entry_id)
    assert old.status == ENTRY_REPLACED
    assert new.replaces_entry_id == old.entry_id          # 引用原课表
    assert new.content_id == "U_ribbon"
    assert {(b.clip_id, b.license_id) for b in new.license_basis} == {
        ("clip_ribbon", "L2")}

    # 主课表与附件一次换版
    doc = svc.store.doc_version_at("S2", published.new_doc_version)
    assert doc.entry_ids == [new.entry_id]
    assert doc.attachments[0].name == "动作图示"

    # 学校最终看到的说明
    [sn] = svc.store.school_notices_for("S2")
    assert sn.status == "arranged"
    assert sn.affected_dates == [D("2026-10-16")]
    assert sn.withdrawal_reason == "权利方提前终止校园授权"
    assert sn.adopted_content == "unit U_ribbon v1"
    assert sn.license_basis and sn.new_doc_version == doc.version
    assert f"第 {doc.version} 版" in sn.new_arrangement


def test_replacement_rejected_for_unfit_content(svc):
    _build_world(svc)
    e2 = _schedule(svc, "S2", "五年级", 10, [D("2026-10-16")])
    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权", detail="", created_by=MAINTAINER)
    svc.revocation.analyze_impact("N1")
    # 用壁画单元本身替换：授权已终止，窗口不覆盖
    with pytest.raises(LicenseCoverageError):
        svc.revocation.submit_replacement(
            original_entry_id=e2.entry_id, dates=[D("2026-10-16")],
            content_type="unit", content_id="U_mural", content_version=1,
            submitted_by=MAINTAINER)


def test_person_holding_all_roles_cannot_self_approve(svc):
    """同一人即使持有维护者+审核人+发布人三类角色，也不能为自己放行。"""
    _build_world(svc)
    e = _schedule(svc, "S2", "四年级", 9, [D("2026-10-16")])
    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权", detail="", created_by=MAINTAINER)
    svc.revocation.analyze_impact("N1")
    repl = svc.revocation.submit_replacement(
        original_entry_id=e.entry_id, dates=[D("2026-10-16")],
        content_type="unit", content_id="U_ribbon", content_version=1,
        submitted_by=MULTI)
    # 自审被拒；他人审核可以
    with pytest.raises(SegregationError):
        svc.revocation.review_replacement(
            repl.replacement_id, reviewer=MULTI, approve=True)
    svc.revocation.review_replacement(
        repl.replacement_id, reviewer=REVIEWER, approve=True)
    # 自发也被拒
    with pytest.raises(SegregationError):
        svc.revocation.publish_replacement(
            repl.replacement_id, publisher=MULTI)
