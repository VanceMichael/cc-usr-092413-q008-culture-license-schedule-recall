"""撤销通知、影响分析与替换流程。

规则要点：
- 授权提前终止以"终止版本"追加到授权链上，旧版本与已完成课程的依据都保留；
- 只处理【尚未执行】且【确实引用该片段】的安排，已完成课程保留原依据；
- 替换必须引用原课表，并重新验证时长、难度、适龄与授权覆盖；
- 提交替换的人不能为自己放行：审核人、发布人都不得是提交人本人。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from . import coverage
from .errors import InvalidState, LicenseCoverageError, SegregationError, ServiceError
from .models import (
    ENTRY_EXECUTED,
    ENTRY_REPLACED,
    ENTRY_SCHEDULED,
    ENTRY_WITHDRAWN,
    KIND_TERMINATION,
    Attachment,
    LicenseVersion,
    Replacement,
    ReplacementChecks,
    RevocationNotice,
    ScheduleDocVersion,
    ScheduleEntry,
    SchoolNotice,
)
from .roles import ROLE_PUBLISHER, ROLE_REVIEWER, RoleDirectory
from .schedule import APPROVAL_POLICY_VERSION
from .store import Store, utcnow

# 替换方案允许的时长偏差与难度差
DURATION_TOLERANCE = 0.10
DIFFICULTY_TOLERANCE = 1


class RevocationService:
    def __init__(self, store: Store, catalog, roles: RoleDirectory) -> None:
        self.store = store
        self.catalog = catalog
        self.roles = roles

    # -- 撤销通知登记 ---------------------------------------------------------

    def register_notice(self, *, notice_id: str, license_id: str,
                        effective_date: date, reason: str, detail: str,
                        created_by: str) -> RevocationNotice:
        def _tx():
            existing = self.store.notices_by_id.get(notice_id)
            if existing is not None:
                # 通知可能重复送达（如与批量发布同时到达后的重试），幂等返回。
                if existing.license_id != license_id:
                    raise ServiceError("同号通知指向不同授权，拒绝处理")
                return existing
            chain = self.store.get_license_versions(license_id)
            active = [v for v in chain if v.kind != KIND_TERMINATION]
            if not active:
                raise ServiceError(f"授权不存在: {license_id}")
            last = active[-1]
            notice = RevocationNotice(
                notice_id=notice_id, license_id=license_id,
                effective_date=effective_date, reason=reason, detail=detail,
                created_by=created_by, created_at=utcnow())
            self.store.add_notice(notice)
            # 在授权链上追加终止版本：自终止日起覆盖判定被切断，历史版本仍保留。
            term = LicenseVersion(
                license_id=license_id, version=len(chain) + 1,
                kind=KIND_TERMINATION, rights_holder=last.rights_holder,
                allowed_uses=[], regions=[], clip_ids=list(last.clip_ids),
                age_min=last.age_min, age_max=last.age_max,
                valid_from=effective_date, valid_to=None,
                evidence_summary=f"权利方提前终止：{reason}",
                evidence_ref=notice_id, supersedes=last.version,
                created_by=created_by, created_at=utcnow(), notice_id=notice_id)
            self.store.add_license_version(term)
            return notice
        return self.store.transaction(_tx)

    # -- 影响分析 -------------------------------------------------------------

    def analyze_impact(self, notice_id: str, *, as_of: Optional[date] = None
                       ) -> list[SchoolNotice]:
        """找出必须换动作的学校安排，并生成/更新学校告知。

        筛选条件同时满足：尚未执行、日期不早于终止日、确实依赖被终止片段、
        按当前授权复核确实失去覆盖。已执行日期一律不动。
        """
        notice = self.store.get_notice(notice_id)

        def _tx():
            term = self.store.termination_for(notice.license_id)
            clip_ids = set(term.clip_ids if term else [])
            candidates = self.store.entries_referencing_clips(clip_ids)
            results: list[SchoolNotice] = []
            for entry in candidates:
                self.process_entry(notice_id, entry.entry_id, as_of)
                # 无论本次是否有新日期，已生成的告知都属于影响范围（可幂等重读）。
                sn = self._find_notice(entry.entry_id, notice_id)
                if sn is not None:
                    results.append(sn)
            return results
        return self.store.transaction(_tx)

    def process_entry(self, notice_id: str, entry_id: str,
                      as_of: Optional[date] = None) -> Optional[SchoolNotice]:
        """处理单条安排的撤回影响（供影响分析与可续办任务共用，幂等）。"""
        notice = self.store.get_notice(notice_id)
        entry = self.store.get_entry(entry_id)
        affected = self._affected_dates(entry, notice, as_of)
        if not affected:
            return None
        term = self.store.termination_for(notice.license_id)
        clip_ids = set(term.clip_ids if term else [])
        return self.apply_withdrawal(
            entry, affected, reason=notice.reason,
            source_notice_id=notice_id, basis_clip_ids=clip_ids)

    def apply_withdrawal(self, entry: ScheduleEntry, affected: list[date],
                         *, reason: str, source_notice_id: Optional[str],
                         basis_clip_ids: set[str]) -> SchoolNotice:
        """把一批日期标记撤回并生成/更新学校告知（幂等）。"""
        fresh = [d for d in affected if d not in entry.withdrawn_dates]
        for d in fresh:
            entry.withdrawn_dates.append(d)
        if source_notice_id:
            entry.notice_id = source_notice_id
        entry.withdrawal_reason = reason
        still_open = [d for d in entry.dates
                      if d not in entry.executed_dates
                      and d not in entry.withdrawn_dates]
        if not still_open:
            entry.status = (ENTRY_REPLACED
                            if entry.status == ENTRY_REPLACED
                            else ENTRY_WITHDRAWN)
        self.store.update_entry(entry)

        basis = [b for b in entry.license_basis
                 if any(b.clip_id == c for c in basis_clip_ids)]
        sn = self._find_notice(entry.entry_id, source_notice_id) \
            if source_notice_id else None
        if sn is None:
            sn = SchoolNotice(
                notice_id=self.store.next_school_notice_id(),
                school_id=entry.school_id, entry_id=entry.entry_id,
                source_notice_id=source_notice_id or "",
                affected_dates=affected,
                withdrawal_reason=reason,
                license_basis=basis, adopted_content=None,
                new_entry_id=None, new_doc_version=None,
                new_arrangement="原动作授权失效，相关日期暂停使用，"
                                "等待替换方案审核发布",
                status="pending_arrangement",
                created_at=utcnow(), updated_at=utcnow())
            self.store.add_school_notice(sn)
        else:
            sn.affected_dates = sorted(set(sn.affected_dates + affected))
            sn.updated_at = utcnow()
            self.store.update_school_notice(sn)
        return sn

    def _open_dates(self, entry: ScheduleEntry, effective: date,
                    as_of: Optional[date]) -> list[date]:
        return [d for d in entry.dates
                if d >= effective and (as_of is None or d >= as_of)
                and d not in entry.executed_dates
                and d not in entry.withdrawn_dates]

    def _affected_dates(self, entry: ScheduleEntry, notice: RevocationNotice,
                        as_of: Optional[date]) -> list[date]:
        school = self.store.get_school(entry.school_id)
        candidates = self._open_dates(entry, notice.effective_date, as_of)
        if not candidates:
            return []
        bad = set(coverage.uncovered_dates(
            self.store, entry.resolved_clip_ids, use_code=entry.use_code,
            region=school.region, student_age=entry.student_age,
            dates=candidates))
        return [d for d in candidates if d in bad]

    def _find_notice(self, entry_id: str, source_notice_id: str
                     ) -> Optional[SchoolNotice]:
        for sn in self.store.school_notices.values():
            if (sn.entry_id == entry_id
                    and sn.source_notice_id == source_notice_id):
                return sn
        return None

    # -- 替换单：提交 ---------------------------------------------------------

    def submit_replacement(self, *, original_entry_id: str, dates: list[date],
                           content_type: str, content_id: str,
                           content_version: int, submitted_by: str,
                           reason: str = "") -> Replacement:
        """提交替换方案：引用原课表，并在提交时完成时长/难度/适龄/授权校验。"""
        self.roles.roles_of(submitted_by)  # 提交人必须是登记用户

        def _tx():
            original = self.store.get_entry(original_entry_id)
            repl_dates = sorted(set(dates))
            if not repl_dates:
                raise ServiceError("替换日期不能为空")
            not_affected = [d for d in repl_dates
                            if d not in original.withdrawn_dates
                            or d in original.executed_dates]
            if not_affected:
                raise InvalidState(
                    "只能替换已撤回且未执行的日期: "
                    + ", ".join(str(d) for d in not_affected))
            new_content, new_clips = self.catalog.resolve_content(
                content_type, content_id, content_version)
            old_content, _ = self.catalog.resolve_content(
                original.content_type, original.content_id,
                original.content_version)
            school = self.store.get_school(original.school_id)

            # 授权必须覆盖替换方案的整个使用窗口（发布时还会再复核一次）
            coverage.require_full_coverage(
                self.store, new_clips, use_code=original.use_code,
                region=school.region, student_age=original.student_age,
                dates=repl_dates)

            diff_pct = abs(new_content.duration_sec - old_content.duration_sec) \
                / max(old_content.duration_sec, 1)
            checks = ReplacementChecks(
                old_duration_sec=old_content.duration_sec,
                new_duration_sec=new_content.duration_sec,
                duration_diff_pct=round(diff_pct, 4),
                old_difficulty=old_content.difficulty,
                new_difficulty=new_content.difficulty,
                age_min=old_content.age_min, age_max=old_content.age_max,
                new_age_min=new_content.age_min, new_age_max=new_content.age_max)
            self._assert_replacement_fit(checks, original.student_age)
            repl = Replacement(
                replacement_id=self.store.next_replacement_id(),
                original_entry_id=original_entry_id,
                school_id=original.school_id, grade=original.grade,
                dates=repl_dates, venue_constraint=original.venue_constraint,
                content_type=content_type, content_id=content_id,
                content_version=content_version,
                reason=reason or original.withdrawal_reason or "授权终止",
                notice_id=original.notice_id or "", checks=checks,
                status="submitted", submitted_by=submitted_by,
                submitted_at=utcnow())
            self.store.add_replacement(repl)
            return repl
        return self.store.transaction(_tx)

    @staticmethod
    def _assert_replacement_fit(checks: ReplacementChecks,
                                student_age: int) -> None:
        problems = []
        if checks.duration_diff_pct > DURATION_TOLERANCE:
            problems.append(
                f"时长偏差 {checks.duration_diff_pct:.0%} 超过 "
                f"{DURATION_TOLERANCE:.0%}")
        if abs(checks.new_difficulty - checks.old_difficulty) > \
                DIFFICULTY_TOLERANCE:
            problems.append(
                f"难度差 {checks.new_difficulty - checks.old_difficulty:+d} "
                f"超过 ±{DIFFICULTY_TOLERANCE}")
        if not (checks.new_age_min <= student_age <= checks.new_age_max):
            problems.append(
                f"新方案适龄 {checks.new_age_min}-{checks.new_age_max} "
                f"不覆盖参训年龄 {student_age}")
        if problems:
            raise LicenseCoverageError("替换方案条件不满足: " + "；".join(problems))

    # -- 替换单：审核 ---------------------------------------------------------

    def review_replacement(self, replacement_id: str, *, reviewer: str,
                           approve: bool, note: str = "") -> Replacement:
        self.roles.require_role(reviewer, ROLE_REVIEWER)

        def _tx():
            repl = self.store.get_replacement(replacement_id)
            # 提交替换的人不能为自己放行
            self.roles.require_not_same_person(reviewer, repl.submitted_by,
                                               "替换审核")
            if repl.status != "submitted":
                raise InvalidState(f"替换单状态为 {repl.status}，不能审核")
            if approve:
                repl.status = "approved"
            else:
                repl.status = "rejected"
            repl.reviewed_by = reviewer
            repl.reviewed_at = utcnow()
            repl.review_note = note
            self.store.update_replacement(repl)
            return repl
        return self.store.transaction(_tx)

    # -- 替换单：发布（原子换版）---------------------------------------------

    def publish_replacement(self, replacement_id: str, *,
                            publisher: str,
                            attachments: Optional[list[dict]] = None
                            ) -> Replacement:
        self.roles.require_role(publisher, ROLE_PUBLISHER)

        def _tx():
            repl = self.store.get_replacement(replacement_id)
            self.roles.require_not_same_person(publisher, repl.submitted_by,
                                               "替换发布")
            if repl.status != "approved":
                raise InvalidState(
                    f"替换单状态为 {repl.status}，须先经他人审核通过")
            original = self.store.get_entry(repl.original_entry_id)
            school = self.store.get_school(repl.school_id)
            new_content, new_clips = self.catalog.resolve_content(
                repl.content_type, repl.content_id, repl.content_version)

            # 发布瞬间重新验证条件，防止审核后授权再度变化
            basis = coverage.require_full_coverage(
                self.store, new_clips, use_code=original.use_code,
                region=school.region, student_age=original.student_age,
                dates=repl.dates)
            self._assert_replacement_fit(repl.checks, original.student_age)
            for d in repl.dates:
                if d in original.executed_dates:
                    raise InvalidState(f"{d} 课程已完成，不能被替换")

            # 1) 生成替换后的新课表条目（引用原课表）
            new_entry = ScheduleEntry(
                entry_id=self.store.next_entry_id(),
                school_id=repl.school_id, grade=repl.grade,
                dates=list(repl.dates),
                venue_constraint=repl.venue_constraint,
                student_age=original.student_age, use_code=original.use_code,
                content_type=repl.content_type, content_id=repl.content_id,
                content_version=repl.content_version,
                resolved_clip_ids=new_clips,
                required_level=original.required_level,
                granted_level=original.required_level,
                approval_policy_version=APPROVAL_POLICY_VERSION,
                scheduled_by=publisher, created_at=utcnow(),
                license_basis=basis, status=ENTRY_SCHEDULED,
                replaces_entry_id=original.entry_id)
            self.store.add_entry(new_entry)

            # 2) 旧条目：被替换日期留痕；全部日期有着落时整体转为 replaced
            original.replaced_by_entry_id = new_entry.entry_id
            remaining = [d for d in original.dates
                         if d not in original.executed_dates
                         and d not in self._replaced_date_set(original)
                         and d not in repl.dates]
            if not remaining:
                original.status = ENTRY_REPLACED
            self.store.update_entry(original)

            # 3) 主课表与全部附件一次换版：同一版本对象，失败则前面对象不外提
            current = self.store.latest_doc(repl.school_id)
            new_version = (current.version + 1) if current else 1
            doc_id = f"doc:{repl.school_id}"
            atts = [Attachment(name=a["name"], ref=a["ref"])
                    for a in (attachments or [])]
            # 继承旧版中未受影响的条目，用新条目顶替被替换的那条，
            # 保证换版后的主课表仍是该校的完整课表。
            carried: list[str] = []
            if current is not None:
                for eid in current.entry_ids:
                    if eid == original.entry_id:
                        continue
                    e = self.store.entries.get(eid)
                    if e is not None and e.status not in (
                            ENTRY_WITHDRAWN, ENTRY_REPLACED):
                        carried.append(eid)
            doc = ScheduleDocVersion(
                doc_id=doc_id, version=new_version, school_id=repl.school_id,
                batch_id=f"replbatch_{replacement_id}",
                entry_ids=carried + [new_entry.entry_id],
                main_ref=f"refs://{doc_id}/v{new_version}/main.pdf",
                attachments=atts, published_by=publisher,
                published_at=utcnow(),
                supersedes=current.version if current else None)
            self.store.add_doc_version(doc)

            # 4) 替换单与学校告知收口
            repl.status = "published"
            repl.published_by = publisher
            repl.published_at = utcnow()
            repl.new_entry_id = new_entry.entry_id
            repl.new_doc_version = new_version
            self.store.update_replacement(repl)

            sn = self._find_notice(original.entry_id, repl.notice_id)
            adopted = (f"{repl.content_type} {repl.content_id} "
                       f"v{repl.content_version}")
            if sn is not None:
                sn.adopted_content = adopted
                sn.new_entry_id = new_entry.entry_id
                sn.new_doc_version = new_version
                sn.new_arrangement = (
                    f"已改用{adopted}，纳入课表第 {new_version} 版"
                    f"（主课表与附件同步换版）")
                sn.status = "arranged"
                sn.updated_at = utcnow()
                self.store.update_school_notice(sn)
            return repl
        return self.store.transaction(_tx)

    def _replaced_date_set(self, entry: ScheduleEntry) -> set[date]:
        dates: set[date] = set()
        for r in self.store.replacements.values():
            if (r.original_entry_id == entry.entry_id
                    and r.status == "published"):
                dates.update(r.dates)
        return dates
