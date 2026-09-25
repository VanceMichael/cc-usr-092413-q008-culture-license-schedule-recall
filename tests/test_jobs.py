"""可续办任务：到期检查、替换处理，中断后续办，幂等。"""
from datetime import date

from service.models import ENTRY_WITHDRAWN

D = date.fromisoformat
MAINTAINER = "m_mural"

from tests.test_scenarios import _build_world, _schedule


def test_expiry_scan_resumes_after_interruption(svc):
    _build_world(svc)
    e1 = _schedule(svc, "S1", "三年级", 8,
                   [D("2026-10-09"), D("2026-10-16")])
    e2 = _schedule(svc, "S2", "五年级", 10, [D("2026-10-16")])
    e3 = _schedule(svc, "S3", "四年级", 9, [D("2026-10-16")],
                   content=("unit", "U_drum", 1))
    svc.schedule.mark_executed(e1.entry_id, D("2026-10-09"), MAINTAINER)
    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权", detail="", created_by=MAINTAINER)

    job = svc.jobs.create_expiry_scan(as_of=D("2026-10-01"))
    assert job.total == 3

    # 第一次运行处理 1 条后中断
    partial = svc.jobs.run(job.job_id, fail_after=1)
    assert partial.status == "interrupted"
    assert len(partial.processed_keys) == 1

    # 续办直到完成：已处理项不重复
    done = svc.jobs.resume(job.job_id)
    assert done.status == "done"
    assert len(done.processed_keys) == 3
    # 再次续办已完成任务直接返回，结果不变
    again = svc.jobs.resume(job.job_id)
    assert again.status == "done"
    assert len(again.processed_keys) == 3

    results = {f["entry_id"]: f["result"] for f in done.findings}
    # e1 未执行的 10-16 失效撤回；e2 失效撤回；e3（鼓舞）仍覆盖
    assert results[e1.entry_id] == "withdrawn"
    assert results[e2.entry_id] == "withdrawn"
    assert results[e3.entry_id] == "covered"

    assert svc.store.get_entry(e2.entry_id).status == ENTRY_WITHDRAWN
    # 已完成日期保留
    assert D("2026-10-09") in svc.store.get_entry(e1.entry_id).executed_dates


def test_replacement_process_job_tracks_state(svc):
    _build_world(svc)
    e2 = _schedule(svc, "S2", "五年级", 10, [D("2026-10-16")])
    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权", detail="", created_by=MAINTAINER)
    svc.revocation.analyze_impact("N1")

    job = svc.jobs.create_replacement_process("N1")
    run1 = svc.jobs.run(job.job_id)
    assert run1.status == "done"
    assert run1.findings[0]["result"] == "needs_action"

    # 提交并走完审核发布后再建处理任务，结论反映已发布
    repl = svc.revocation.submit_replacement(
        original_entry_id=e2.entry_id, dates=[D("2026-10-16")],
        content_type="unit", content_id="U_ribbon", content_version=1,
        submitted_by=MAINTAINER)
    svc.revocation.review_replacement(
        repl.replacement_id, reviewer="r_curriculum", approve=True)
    svc.revocation.publish_replacement(
        repl.replacement_id, publisher="p_district")
    job2 = svc.jobs.create_replacement_process("N1")
    svc.jobs.run(job2.job_id)
    assert job2.findings[0]["result"] == "published"
    assert job2.findings[0]["new_doc_version"] == 1
