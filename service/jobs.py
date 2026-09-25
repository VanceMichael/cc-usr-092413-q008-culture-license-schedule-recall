"""可续办任务：到期检查与替换处理。

任务把工作切成以业务键标识的最小单元逐条处理：
- 中断后重跑会跳过 processed_keys，已处理项天然幂等；
- 失败只记录 last_error 并把任务置为 interrupted，不产生半截状态；
- findings 记录每个单元的处理结论，供学校告知与人工核对。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from . import coverage
from .models import ENTRY_EXECUTED, ENTRY_REPLACED, ENTRY_WITHDRAWN, Job
from .store import Store, utcnow

JOB_EXPIRY_SCAN = "expiry_scan"
JOB_REPLACEMENT_PROCESS = "replacement_process"


class JobService:
    def __init__(self, store: Store, revocation) -> None:
        self.store = store
        self.revocation = revocation

    # -- 建任务 ---------------------------------------------------------------

    def create_expiry_scan(self, *, as_of: Optional[date] = None) -> Job:
        as_of = as_of or utcnow().date()
        keys = sorted(e.entry_id for e in self.store.all_entries()
                      if any(d >= as_of and d not in e.executed_dates
                             for d in e.dates))
        job = Job(job_id=self.store.next_job_id(), job_type=JOB_EXPIRY_SCAN,
                  params={"as_of": as_of.isoformat()}, status="pending",
                  total=len(keys), processed_keys=[], findings=[],
                  created_at=utcnow(), updated_at=utcnow())
        self.store.add_job(job)
        return job

    def create_replacement_process(self, notice_id: str) -> Job:
        self.store.get_notice(notice_id)
        keys = sorted({e.entry_id for e in self.store.all_entries()
                       if e.notice_id == notice_id and e.withdrawn_dates})
        job = Job(job_id=self.store.next_job_id(),
                  job_type=JOB_REPLACEMENT_PROCESS,
                  params={"notice_id": notice_id}, status="pending",
                  total=len(keys), processed_keys=[], findings=[],
                  created_at=utcnow(), updated_at=utcnow())
        self.store.add_job(job)
        return job

    # -- 运行/续办 ------------------------------------------------------------

    def run(self, job_id: str, *, fail_after: Optional[int] = None) -> Job:
        """运行任务；fail_after 用于注入中断，再次调用即从断点续办。"""
        job = self.store.get_job(job_id)
        keys = self._unit_keys(job)
        job.total = len(keys)
        done_this_run = 0

        def _tx():
            nonlocal done_this_run
            job.status = "running"
            job.attempts += 1
            job.updated_at = utcnow()
            self.store.update_job(job)
            for key in keys:
                if key in job.processed_keys:
                    continue
                try:
                    if job.job_type == JOB_EXPIRY_SCAN:
                        finding = self._process_expiry_unit(job, key)
                    else:
                        finding = self._process_replacement_unit(job, key)
                except Exception as exc:  # 中断：保留进度，供续办
                    job.status = "interrupted"
                    job.last_error = f"{type(exc).__name__}: {exc}"
                    job.updated_at = utcnow()
                    self.store.update_job(job)
                    raise
                job.processed_keys.append(key)
                if finding is not None:
                    job.findings.append(finding)
                job.updated_at = utcnow()
                self.store.update_job(job)
                done_this_run += 1
                if fail_after is not None and done_this_run >= fail_after:
                    job.status = "interrupted"
                    job.last_error = "模拟中断：达到本次处理上限"
                    job.updated_at = utcnow()
                    self.store.update_job(job)
                    return job
            job.status = "done"
            job.last_error = ""
            job.updated_at = utcnow()
            self.store.update_job(job)
            return job

        return self.store.transaction(_tx)

    def resume(self, job_id: str) -> Job:
        """续办：等价于带断点重跑；已完成任务直接返回。"""
        job = self.store.get_job(job_id)
        if job.status == "done":
            return job
        return self.run(job_id)

    # -- 单元 -----------------------------------------------------------------

    def _unit_keys(self, job: Job) -> list[str]:
        cached = job.__dict__.get("_keys")
        if cached is not None:
            return cached
        if job.job_type == JOB_EXPIRY_SCAN:
            as_of = date.fromisoformat(job.params["as_of"])
            keys = sorted(e.entry_id for e in self.store.all_entries()
                          if any(d >= as_of and d not in e.executed_dates
                                 for d in e.dates))
        else:
            notice_id = job.params["notice_id"]
            keys = sorted({e.entry_id for e in self.store.all_entries()
                           if e.notice_id == notice_id
                           and e.withdrawn_dates})
        return keys

    def _process_expiry_unit(self, job: Job, entry_id: str):
        as_of = date.fromisoformat(job.params["as_of"])
        entry = self.store.get_entry(entry_id)
        if entry.status in (ENTRY_WITHDRAWN, ENTRY_REPLACED, ENTRY_EXECUTED):
            return {"entry_id": entry_id, "result": "skip",
                    "reason": f"状态 {entry.status}"}
        candidates = [d for d in entry.dates
                      if d >= as_of and d not in entry.executed_dates
                      and d not in entry.withdrawn_dates]
        if not candidates:
            return {"entry_id": entry_id, "result": "skip",
                    "reason": "没有待执行日期"}
        school = self.store.get_school(entry.school_id)
        affected = coverage.uncovered_dates(
            self.store, entry.resolved_clip_ids, use_code=entry.use_code,
            region=school.region, student_age=entry.student_age,
            dates=candidates)
        if not affected:
            return {"entry_id": entry_id, "result": "covered"}
        uncovered_clips = self._uncovered_clip_ids(entry, school, affected)
        sn = self.revocation.apply_withdrawal(
            entry, affected, reason="授权到期/失效（到期检查）",
            source_notice_id=None, basis_clip_ids=set(uncovered_clips))
        return {"entry_id": entry_id, "result": "withdrawn",
                "affected_dates": [d.isoformat() for d in affected],
                "school_notice_id": sn.notice_id}

    def _uncovered_clip_ids(self, entry, school, dates) -> list[str]:
        hit, _ = coverage.evaluate(
            self.store, entry.resolved_clip_ids, use_code=entry.use_code,
            region=school.region, student_age=entry.student_age, dates=dates)
        result: list[str] = []
        for cid in entry.resolved_clip_ids:
            if any(cid not in hit[d] for d in dates) and cid not in result:
                result.append(cid)
        return result

    def _process_replacement_unit(self, job: Job, entry_id: str):
        notice_id = job.params["notice_id"]
        entry = self.store.get_entry(entry_id)
        replacements = [r for r in self.store.replacements.values()
                        if r.original_entry_id == entry_id]
        latest = replacements[-1] if replacements else None
        if latest is None:
            return {"entry_id": entry_id, "result": "needs_action",
                    "withdrawn_dates": [d.isoformat()
                                        for d in entry.withdrawn_dates]}
        return {"entry_id": entry_id, "result": latest.status,
                "replacement_id": latest.replacement_id,
                "new_entry_id": latest.new_entry_id,
                "new_doc_version": latest.new_doc_version}
