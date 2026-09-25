"""排课、审批水位、批量发布与下载凭据。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from . import coverage
from .errors import InvalidState, LicenseCoverageError, ServiceError
from .models import (
    Attachment,
    ENTRY_EXECUTED,
    ENTRY_REPLACED,
    ENTRY_SCHEDULED,
    ENTRY_WITHDRAWN,
    ScheduleDocVersion,
    ScheduleEntry,
)
from .roles import ROLE_PUBLISHER, RoleDirectory
from .store import Store, utcnow

APPROVAL_POLICY_VERSION = "approval-policy-2026.09"


class ScheduleService:
    def __init__(self, store: Store, catalog, roles: RoleDirectory) -> None:
        self.store = store
        self.catalog = catalog
        self.roles = roles

    # -- 排课 -----------------------------------------------------------------

    def schedule(self, *, school_id: str, grade: str,
                 dates: list, venue_constraint: str, student_age: int,
                 use_code: str, content_type: str, content_id: str,
                 content_version: int, required_level: int,
                 granted_level: int, scheduled_by: str) -> ScheduleEntry:
        school = self.store.get_school(school_id)
        dates = sorted(set(dates))
        if not dates:
            raise ServiceError("排课日期不能为空")
        _, clip_ids = self.catalog.resolve_content(content_type, content_id,
                                                   content_version)
        # 审批水位：排课当时要求与实批都留痕，未达水位不允许排入。
        if granted_level < required_level:
            raise ServiceError(
                f"审批水位不足：要求 {required_level}，实批 {granted_level}")

        def _tx():
            basis = coverage.require_full_coverage(
                self.store, clip_ids, use_code=use_code,
                region=school.region, student_age=student_age, dates=dates)
            entry = ScheduleEntry(
                entry_id=self.store.next_entry_id(),
                school_id=school_id, grade=grade, dates=dates,
                venue_constraint=venue_constraint, student_age=student_age,
                use_code=use_code, content_type=content_type,
                content_id=content_id, content_version=content_version,
                resolved_clip_ids=clip_ids,
                required_level=required_level, granted_level=granted_level,
                approval_policy_version=APPROVAL_POLICY_VERSION,
                scheduled_by=scheduled_by, created_at=utcnow(),
                license_basis=basis)
            self.store.add_entry(entry)
            return entry

        return self.store.transaction(_tx)

    # -- 执行登记 -------------------------------------------------------------

    def mark_executed(self, entry_id: str, executed_date,
                      actor: str) -> ScheduleEntry:
        def _tx():
            entry = self.store.get_entry(entry_id)
            if entry.status == ENTRY_WITHDRAWN:
                raise InvalidState("已撤回安排不能登记执行")
            if executed_date not in entry.dates:
                raise ServiceError("执行日期不在课表窗口内")
            if executed_date not in entry.executed_dates:
                entry.executed_dates.append(executed_date)
            if set(entry.executed_dates) >= set(entry.dates):
                entry.status = ENTRY_EXECUTED
            self.store.update_entry(entry)
            return entry
        return self.store.transaction(_tx)

    # -- 批量发布：主课表与附件一次换版 --------------------------------------

    def publish_batch(self, *, entry_ids: list[str],
                      attachments: list[dict],
                      published_by: str) -> list[ScheduleDocVersion]:
        """把一批安排按学校换版发布。

        与撤销通知共用同一把事务锁：在锁内重新复核授权，
        已失效的安排不进入新课表、也拿不到下载凭据；
        主课表与全部附件在同一次写入中生成，要么整版成功要么不留半成品。
        """
        self.roles.require_role(published_by, ROLE_PUBLISHER)

        def _tx():
            entries = [self.store.get_entry(eid) for eid in entry_ids]
            by_school: dict[str, list[ScheduleEntry]] = {}
            for e in entries:
                # 已被替换单接管的旧安排不再单独进入发布。
                if e.status == ENTRY_REPLACED:
                    raise InvalidState(f"安排已被替换版本接管: {e.entry_id}")
                by_school.setdefault(e.school_id, []).append(e)

            docs: list[ScheduleDocVersion] = []
            batch_id = f"batch_{utcnow().strftime('%Y%m%d%H%M%S%f')}"
            atts = [Attachment(name=a["name"], ref=a["ref"]) for a in attachments]
            for school_id, school_entries in by_school.items():
                school = self.store.get_school(school_id)
                valid: list[ScheduleEntry] = []
                for e in school_entries:
                    bad = coverage.uncovered_dates(
                        self.store, e.resolved_clip_ids, use_code=e.use_code,
                        region=school.region, student_age=e.student_age,
                        dates=e.dates)
                    if bad:
                        raise LicenseCoverageError(
                            f"安排 {e.entry_id} 在发布时已失去授权覆盖 "
                            f"({', '.join(str(d) for d in bad)})，"
                            f"须先走撤回/替换，不能进入新课表")
                    valid.append(e)

                current = self.store.latest_doc(school_id)
                new_version = (current.version + 1) if current else 1
                doc_id = f"doc:{school_id}"
                # 主课表与附件同属一个版本对象，单次写入即整版生效。
                doc = ScheduleDocVersion(
                    doc_id=doc_id, version=new_version, school_id=school_id,
                    batch_id=batch_id, entry_ids=[e.entry_id for e in valid],
                    main_ref=f"refs://{doc_id}/v{new_version}/main.pdf",
                    attachments=atts, published_by=published_by,
                    published_at=utcnow(),
                    supersedes=current.version if current else None)
                self.store.add_doc_version(doc)
                for e in valid:
                    e.batch_id = batch_id
                    self.store.update_entry(e)
                docs.append(doc)
            return docs

        return self.store.transaction(_tx)

    # -- 下载凭据 -------------------------------------------------------------

    def issue_credential(self, entry_id: str, d, *, issued_by: str):
        """失效课表不得继续生成下载凭据：签发瞬间复核当日授权。"""
        def _tx():
            entry = self.store.get_entry(entry_id)
            if d not in entry.dates:
                raise ServiceError("日期不在该安排窗口内")
            school = self.store.get_school(entry.school_id)
            # 失效课表不得继续生成下载凭据：先复核当日授权覆盖。
            bad = coverage.uncovered_dates(
                self.store, entry.resolved_clip_ids, use_code=entry.use_code,
                region=school.region, student_age=entry.student_age, dates=[d])
            if bad:
                raise LicenseCoverageError(
                    f"安排 {entry_id} 在 {d} 已失效，不得生成下载凭据")
            if d in entry.executed_dates:
                raise InvalidState("该日课程已完成，无需下载")
            if d in entry.withdrawn_dates or entry.status == ENTRY_WITHDRAWN:
                raise InvalidState("该日安排已撤回")
            from .models import DownloadCredential
            cred = DownloadCredential(
                token=self.store.next_token(), entry_id=entry_id, date=d,
                issued_by=issued_by, issued_at=utcnow(),
                expires_at=utcnow() + timedelta(hours=6))
            self.store.add_credential(cred)
            return cred
        return self.store.transaction(_tx)

    def redeem_credential(self, token: str):
        """下载时再校验一次：撤销在签发之后到达，凭据同样立即失效。"""
        cred = self.store.get_credential(token)
        if cred is None:
            raise ServiceError("凭据不存在")
        if utcnow() > cred.expires_at:
            raise InvalidState("凭据已过期")
        entry = self.store.get_entry(cred.entry_id)
        self._assert_date_usable(entry, cred.date)
        school = self.store.get_school(entry.school_id)
        bad = coverage.uncovered_dates(
            self.store, entry.resolved_clip_ids, use_code=entry.use_code,
            region=school.region, student_age=entry.student_age,
            dates=[cred.date])
        if bad:
            raise InvalidState(f"授权已撤销，凭据对应的 {cred.date} 课表已失效")
        return entry

    @staticmethod
    def _assert_date_usable(entry: ScheduleEntry, d) -> None:
        if d not in entry.dates:
            raise ServiceError("日期不在该安排窗口内")
        if d in entry.executed_dates:
            raise InvalidState("该日课程已完成，无需下载")
        if d in entry.withdrawn_dates or entry.status == ENTRY_WITHDRAWN:
            raise InvalidState("该日安排已撤回")
