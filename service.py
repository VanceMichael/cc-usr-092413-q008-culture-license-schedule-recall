"""服务层：授权覆盖校验、影响面分析、替换审批、原子换版、可续办任务。

所有改变状态的方法都在 store.lock 关键区内完成，保证撤销通知与发布
并发到达时判定与落库一致；课表换版（主表 + 附件 + 指针）在同一关键区内一次完成。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

from domain import (
    LEVEL_PUBLISHED, LEVEL_REVIEWED, LEVEL_SCHEDULED, USE_BREAK_EXERCISE,
    ActionUnitVersion, Attachment, Clip, ClipBasis, ClipRef, ComboMember,
    ComboVersion, Credential, DomainError, Job, LicenseVersion, Material,
    Notice, Replacement, RevocationNotice, ScheduleEntry, ScheduleVersion,
    School, TraceEvent, User,
    ROLE_COACH, ROLE_MAINTAINER, ROLE_PUBLISHER, ROLE_REVIEWER,
)
from store import Store

# 替换时长容忍度：±10%
DURATION_TOLERANCE = 0.10
# 替换难度不得高于原安排
DIFFICULTY_CAP = True


def _require(actor: User, role: str) -> None:
    if actor.role != role:
        raise DomainError(f"需要角色 {role}，当前为 {actor.role}",
                          code="forbidden", status=403)


def _require_any(actor: User, *roles: str) -> None:
    if actor.role not in roles:
        raise DomainError(f"需要角色之一 {roles}，当前为 {actor.role}",
                          code="forbidden", status=403)


class Service:
    def __init__(self, store: Store):
        self.store = store

    # ============ 基础资料 ============
    def register_user(self, user: User) -> User:
        with self.store.lock:
            self.store.users[user.id] = user
            return user

    def register_school(self, actor: User, school: School) -> School:
        _require_any(actor, ROLE_MAINTAINER, ROLE_COACH)
        with self.store.lock:
            self.store.schools[school.id] = school
            return school

    def add_material(self, actor: User, material: Material,
                     clips: list[Clip]) -> Material:
        _require(actor, ROLE_MAINTAINER)
        if material.maintainer_id != actor.id:
            raise DomainError("只能登记自己维护的素材", code="forbidden", status=403)
        with self.store.lock:
            self.store.materials[material.id] = material
            for c in clips:
                if c.end_sec <= c.start_sec:
                    raise DomainError(f"片段 {c.code} 时长非法")
                self.store.clips[c.id] = c
                self.store.clips_by_material[material.id].append(c)
            return material

    def add_license_version(self, actor: User, license_key: str,
                            rights_holder: str, permitted_uses: list[str],
                            regions: list[str], age_min: int, age_max: int,
                            effective_from: date, effective_to: date,
                            evidence_summary: str, evidence_refs: list[str],
                            supersedes_id: Optional[str] = None) -> LicenseVersion:
        """登记可追溯的授权版本。收窄/续期一律追加新版本，不改旧版本。"""
        _require(actor, ROLE_MAINTAINER)
        if effective_to < effective_from:
            raise DomainError("生效区间倒置：effective_to 早于 effective_from")
        if age_max < age_min:
            raise DomainError("年龄范围倒置")
        if not evidence_summary or not evidence_refs:
            raise DomainError("证据摘要与证据编号必填，保证授权可追溯")
        with self.store.lock:
            chain = self.store.license_chain(license_key)
            version_no = (chain[-1].version_no + 1) if chain else 1
            if supersedes_id is None and chain:
                supersedes_id = chain[-1].id
            lic = LicenseVersion(
                id=self.store.new_id("lic"), license_key=license_key,
                version_no=version_no, rights_holder=rights_holder,
                permitted_uses=list(permitted_uses), regions=list(regions),
                age_min=age_min, age_max=age_max,
                effective_from=effective_from, effective_to=effective_to,
                evidence_summary=evidence_summary, evidence_refs=list(evidence_refs),
                created_by=actor.id, created_at=self.store.clock.now,
                supersedes_id=supersedes_id,
            )
            self.store.add_license(lic)
            return lic

    def record_revocation(self, actor: User, license_version_id: str,
                          covers_through: date, reason: str,
                          evidence_refs: list[str]) -> RevocationNotice:
        """登记素材馆的提前终止通知。covers_through=授权仍覆盖的最后日期（含）。"""
        with self.store.lock:
            lic = self.store.licenses_by_id.get(license_version_id)
            if lic is None:
                raise DomainError("授权版本不存在", code="not_found", status=404)
            if covers_through < lic.effective_from or covers_through > lic.effective_to:
                raise DomainError("撤销边界必须落在授权生效区间内")
            if not reason or not evidence_refs:
                raise DomainError("撤回原因与证据编号必填")
            notice = RevocationNotice(
                id=self.store.new_id("rev"), license_version_id=license_version_id,
                notified_at=self.store.clock.now, covers_through=covers_through,
                reason=reason, evidence_refs=list(evidence_refs),
                received_by=actor.id,
            )
            self.store.revocations[notice.id] = notice
            return notice

    def add_action_unit(self, actor: User, unit_key: str, name: str,
                        clip_refs: list[ClipRef], duration_sec: int,
                        difficulty: int) -> ActionUnitVersion:
        """动作单元版本必须标明实际引用的片段；不允许引用不存在的片段。"""
        _require(actor, ROLE_MAINTAINER)
        if duration_sec <= 0:
            raise DomainError("动作时长必须为正")
        if not 1 <= difficulty <= 5:
            raise DomainError("难度取值 1-5")
        with self.store.lock:
            for ref in clip_refs:
                if ref.clip_id not in self.store.clips:
                    raise DomainError(f"片段 {ref.clip_id} 不存在，无法标明引用")
                mat = self.store.materials.get(ref.material_id)
                if mat is None:
                    raise DomainError(f"素材 {ref.material_id} 不存在")
                clip = self.store.clips[ref.clip_id]
                if clip.material_id != ref.material_id:
                    raise DomainError("片段与素材不匹配")
            chain = self.store.units[unit_key]
            uv = ActionUnitVersion(
                id=self.store.new_id("uv"), unit_key=unit_key,
                version_no=(chain[-1].version_no + 1) if chain else 1,
                name=name, clip_refs=list(clip_refs),
                duration_sec=duration_sec, difficulty=difficulty,
                maintainer_id=actor.id, created_at=self.store.clock.now,
            )
            self.store.add_unit(uv)
            return uv

    def add_combo(self, actor: User, combo_key: str, name: str,
                  members: list[tuple[str, int]]) -> ComboVersion:
        """组合版本：(动作单元版本id, 该单元在组合内的秒数) 列表。"""
        _require(actor, ROLE_MAINTAINER)
        if not members:
            raise DomainError("组合至少包含一个动作单元")
        with self.store.lock:
            resolved: list[ComboMember] = []
            diff = 0
            for unit_version_id, secs in members:
                uv = self.store.units_by_id.get(unit_version_id)
                if uv is None:
                    raise DomainError(f"动作单元版本 {unit_version_id} 不存在",
                                      code="not_found", status=404)
                if secs <= 0:
                    raise DomainError("组合成员时长必须为正")
                resolved.append(ComboMember(unit_version_id, secs))
                diff = max(diff, uv.difficulty)
            chain = self.store.combos[combo_key]
            cv = ComboVersion(
                id=self.store.new_id("cv"), combo_key=combo_key,
                version_no=(chain[-1].version_no + 1) if chain else 1,
                name=name, members=resolved, maintainer_id=actor.id,
                created_at=self.store.clock.now, difficulty=diff,
            )
            self.store.add_combo(cv)
            return cv

    # ============ 授权覆盖判定 ============
    def _clip_license_on(self, clip: Clip, day: date) -> Optional[LicenseVersion]:
        mat = self.store.materials[clip.material_id]
        return self.store.license_on(mat.license_key, day)

    def _covers(self, lic: LicenseVersion, day: date, region: str,
                age_min: int, age_max: int, purpose: str) -> bool:
        if not (lic.effective_from <= day <= lic.effective_to):
            return False
        if purpose not in lic.permitted_uses:
            return False
        if "*" not in lic.regions and region not in lic.regions:
            return False
        if age_min < lic.age_min or age_max > lic.age_max:
            return False
        for rev in self.store.revocations_for(lic.id):
            if day > rev.covers_through:
                return False
        return True

    def _combo_clip_refs(self, combo: ComboVersion) -> list[ClipRef]:
        """组合版本传递依赖到的全部片段（去重）。"""
        seen: set[str] = set()
        refs: list[ClipRef] = []
        for m in combo.members:
            uv = self.store.units_by_id[m.unit_version_id]
            for ref in uv.clip_refs:
                if ref.clip_id not in seen:
                    seen.add(ref.clip_id)
                    refs.append(ref)
        return refs

    def _check_combo_on_day(self, combo: ComboVersion, day: date, region: str,
                            age_min: int, age_max: int, purpose: str,
                            water_level: int
                            ) -> tuple[list[ClipBasis], list[dict]]:
        """校验单个组合在单个使用日的全部引用片段，返回当日依据。"""
        basis: list[ClipBasis] = []
        failures: list[dict] = []
        for ref in self._combo_clip_refs(combo):
            clip = self.store.clips[ref.clip_id]
            lic = self._clip_license_on(clip, day)
            ok = lic is not None and self._covers(
                lic, day, region, age_min, age_max, purpose)
            if not ok:
                failures.append({
                    "date": day.isoformat(),
                    "combo_version_id": combo.id, "clip_id": ref.clip_id,
                    "material_id": ref.material_id,
                    "reason": ("无生效授权版本" if lic is None
                               else "当前授权版本不覆盖该用途/地区/年龄/日期"),
                })
                continue
            basis.append(ClipBasis(
                clip_id=ref.clip_id, material_id=ref.material_id,
                license_version_id=lic.id, on_date=day,
                checked_at=self.store.clock.now, water_level=water_level,
            ))
        return basis, failures

    def validate_window(self, items: Iterable[tuple[str, date]], region: str,
                        age_min: int, age_max: int, purpose: str,
                        water_level: int
                        ) -> tuple[list[ClipBasis], list[dict]]:
        """校验授权覆盖整个使用窗口。

        items 为 (组合版本id, 使用日) 逐条安排；任何一条安排当日所引用的
        任一一片段无有效授权即失败——排课时调用方见到非空 failures 必须拒绝。
        返回的依据按 (日期, 片段) 去重。
        """
        basis: list[ClipBasis] = []
        failures: list[dict] = []
        seen: set[tuple[date, str]] = set()
        for combo_id, day in items:
            combo = self.store.combos_by_id.get(combo_id)
            if combo is None:
                raise DomainError(f"组合版本 {combo_id} 不存在",
                                  code="not_found", status=404)
            b, f = self._check_combo_on_day(
                combo, day, region, age_min, age_max, purpose, water_level)
            failures.extend(f)
            for x in b:
                key = (x.on_date, x.clip_id)
                if key not in seen:
                    seen.add(key)
                    basis.append(x)
        return basis, failures

    # ============ 排课 / 审批 / 发布 ============
    def submit_schedule(self, actor: User, schedule_key: str, school_id: str,
                        grade: str, items: list[dict],
                        attachments: list[Attachment],
                        purpose: str = USE_BREAK_EXERCISE) -> ScheduleVersion:
        """区域教研员排课。items: {date, combo_version_id, venue, venue_limit}。

        排课即校验授权覆盖整个使用窗口，并留存学校/年级/日期/场地/审批水位。
        """
        _require(actor, ROLE_COACH)
        with self.store.lock:
            school = self.store.schools.get(school_id)
            if school is None:
                raise DomainError("学校不存在", code="not_found", status=404)
            if grade not in school.grades:
                raise DomainError(f"学校未开设年级 {grade}")
            age_min, age_max = school.grades[grade]
            entries: list[ScheduleEntry] = []
            for i, it in enumerate(items):
                cv = self.store.combos_by_id.get(it["combo_version_id"])
                if cv is None:
                    raise DomainError(f"组合版本 {it['combo_version_id']} 不存在",
                                      code="not_found", status=404)
                day = it["date"] if isinstance(it["date"], date) else date.fromisoformat(it["date"])
                if not it.get("venue"):
                    raise DomainError("场地必填")
                entries.append(ScheduleEntry(
                    entry_id=f"ent_{schedule_key}_{i+1:02d}", date=day,
                    combo_version_id=cv.id, venue=it["venue"],
                    venue_limit=it.get("venue_limit", ""),
                    age_min=age_min, age_max=age_max,
                ))
            check_items = [(e.combo_version_id, e.date) for e in entries]
            basis, failures = self.validate_window(
                check_items, school.region, age_min, age_max, purpose,
                LEVEL_SCHEDULED)
            if failures:
                raise DomainError("授权未覆盖整个使用窗口，禁止排课",
                                  code="coverage_gap", status=422)
            chain = self.store.schedules[schedule_key]
            sv = ScheduleVersion(
                id=self.store.new_id("sv"), schedule_key=schedule_key,
                version_no=(chain[-1].version_no + 1) if chain else 1,
                school_id=school_id, region=school.region, grade=grade,
                purpose=purpose, entries=entries, attachments=list(attachments),
                status="pending_approval", submitted_by=actor.id,
                created_at=self.store.clock.now,
            )
            # 依据按条目各自的组合归集
            for e in entries:
                e_basis, _ = self._check_combo_on_day(
                    self.store.combos_by_id[e.combo_version_id], e.date,
                    school.region, age_min, age_max, purpose, LEVEL_SCHEDULED)
                e.license_basis = e_basis
            sv.approval_trace.append(TraceEvent(
                level=LEVEL_SCHEDULED, action="submitted", actor_id=actor.id,
                at=self.store.clock.now,
                note=f"学校{school_id} 年级{grade} 排课并完成全窗口授权校验"))
            self.store.add_schedule(sv)
            return sv

    def approve_schedule(self, actor: User, schedule_version_id: str,
                         note: str = "") -> ScheduleVersion:
        _require(actor, ROLE_REVIEWER)
        with self.store.lock:
            sv = self.store.schedules_by_id.get(schedule_version_id)
            if sv is None:
                raise DomainError("课表版本不存在", code="not_found", status=404)
            if sv.status != "pending_approval":
                raise DomainError(f"当前状态 {sv.status} 不可审核")
            if sv.submitted_by == actor.id:
                raise DomainError("提交人与审核人不可为同一人", code="segregation",
                                  status=403)
            sv.status = "approved"
            sv.approved_by = actor.id
            sv.approval_trace.append(TraceEvent(
                level=LEVEL_REVIEWED, action="approved", actor_id=actor.id,
                at=self.store.clock.now, note=note))
            return sv

    def _publish_one(self, actor: User, sv: ScheduleVersion, note: str) -> None:
        """在已持锁的关键区内完成发布前复核与状态翻转。"""
        if sv.status != "approved":
            raise DomainError(f"课表 {sv.schedule_key} 当前状态 {sv.status}，不可发布")
        age_min = min(e.age_min for e in sv.entries)
        age_max = max(e.age_max for e in sv.entries)
        pending = [e for e in sv.entries if e.status == "scheduled"]
        _, failures = self.validate_window(
            [(e.combo_version_id, e.date) for e in pending],
            sv.region, age_min, age_max, sv.purpose, LEVEL_PUBLISHED)
        if failures:
            raise DomainError(
                f"课表 {sv.schedule_key} 授权已失效，发布中止",
                code="coverage_gap", status=422)
        # 追加发布水位（3）的依据快照；排课/审核水位的原始快照保留
        for e in pending:
            e_basis, _ = self._check_combo_on_day(
                self.store.combos_by_id[e.combo_version_id], e.date,
                sv.region, e.age_min, e.age_max, sv.purpose, LEVEL_PUBLISHED)
            e.license_basis.extend(e_basis)
        sv.status = "published"
        sv.published_by = actor.id
        sv.published_at = self.store.clock.now
        sv.approval_trace.append(TraceEvent(
            level=LEVEL_PUBLISHED, action="published", actor_id=actor.id,
            at=self.store.clock.now, note=note))

    def publish_schedule(self, actor: User, schedule_version_id: str,
                         note: str = "") -> ScheduleVersion:
        _require(actor, ROLE_PUBLISHER)
        with self.store.lock:
            sv = self.store.schedules_by_id.get(schedule_version_id)
            if sv is None:
                raise DomainError("课表版本不存在", code="not_found", status=404)
            self._publish_one(actor, sv, note)
            return sv

    def batch_publish(self, actor: User, schedule_version_ids: list[str]
                      ) -> list[ScheduleVersion]:
        """批量发布：先全部复核通过，再一次翻转；任一失效则全部不发布。"""
        _require(actor, ROLE_PUBLISHER)
        with self.store.lock:
            svs = []
            for sid in schedule_version_ids:
                sv = self.store.schedules_by_id.get(sid)
                if sv is None:
                    raise DomainError(f"课表版本 {sid} 不存在",
                                      code="not_found", status=404)
                if sv.status != "approved":
                    raise DomainError(f"课表 {sv.schedule_key} 未通过审核，整批中止")
                pending = [e for e in sv.entries if e.status == "scheduled"]
                _, failures = self.validate_window(
                    [(e.combo_version_id, e.date) for e in pending],
                    sv.region,
                    min(e.age_min for e in sv.entries),
                    max(e.age_max for e in sv.entries),
                    sv.purpose, LEVEL_PUBLISHED)
                if failures:
                    raise DomainError(
                        f"课表 {sv.schedule_key} 授权已失效（撤销与发布同时到达），整批中止",
                        code="coverage_gap", status=422)
                svs.append(sv)
            for sv in svs:
                self._publish_one(actor, sv, "批量发布")
            return svs

    def mark_executed(self, actor: User, schedule_version_id: str,
                      entry_id: str) -> ScheduleEntry:
        """标记课程已完成——已完成课程在授权收窄后保留原依据，不撤回。"""
        with self.store.lock:
            sv = self.store.schedules_by_id.get(schedule_version_id)
            if sv is None:
                raise DomainError("课表版本不存在", code="not_found", status=404)
            for e in sv.entries:
                if e.entry_id == entry_id:
                    e.status = "executed"
                    e.executed_at = self.store.clock.now
                    return e
            raise DomainError("条目不存在", code="not_found", status=404)

    # ============ 下载凭据 ============
    def issue_credential(self, actor: User, schedule_version_id: str) -> Credential:
        """失效课表不得继续生成下载凭据：签发瞬间重新核验授权。"""
        with self.store.lock:
            sv = self.store.schedules_by_id.get(schedule_version_id)
            if sv is None:
                raise DomainError("课表版本不存在", code="not_found", status=404)
            current = self.store.current(sv.schedule_key)
            if current is None or current.id != sv.id:
                raise DomainError("该版本已不是当前采用版本", code="stale", status=409)
            if sv.status != "published":
                raise DomainError(f"课表状态 {sv.status}，不可签发凭据",
                                  code="not_publishable", status=409)
            future = [e for e in sv.entries if e.date >= self.store.clock.today
                      and e.status == "scheduled"]
            _, failures = self.validate_window(
                [(e.combo_version_id, e.date) for e in future],
                sv.region,
                min((e.age_min for e in future), default=0),
                max((e.age_max for e in future), default=0),
                sv.purpose, LEVEL_PUBLISHED)
            if failures:
                raise DomainError("授权已失效，不得生成下载凭据",
                                  code="coverage_gap", status=409)
            cred = Credential(
                id=self.store.new_id("cred"), schedule_version_id=sv.id,
                schedule_key=sv.schedule_key, school_id=sv.school_id,
                issued_at=self.store.clock.now,
                expires_at=max((e.date for e in future), default=self.store.clock.today),
            )
            self.store.add_credential(cred)
            return cred

    def _deactivate_credentials(self, sv: ScheduleVersion, reason: str) -> None:
        for cred in self.store.credentials_by_schedule.get(sv.id, []):
            if cred.active:
                cred.active = False
                cred.deactivate_reason = reason

    # ============ 影响面分析与可续办任务 ============
    def _entry_depends_on_revocation(self, entry: ScheduleEntry,
                                     rev: RevocationNotice) -> bool:
        """确实依赖：排课时留存的依据中包含被撤销的授权版本，且使用日超出覆盖末日。

        已完成（executed）的课程一律保留原依据，不算受影响。
        """
        if entry.status != "scheduled":
            return False
        if entry.date <= rev.covers_through:
            return False
        return any(b.license_version_id == rev.license_version_id
                   for b in entry.license_basis)

    def _mark_affected(self, sv: ScheduleVersion, entry_ids: set[str],
                       reason: str, revocation_id: Optional[str]) -> list[str]:
        affected_dates: list[str] = []
        for e in sv.entries:
            if e.entry_id in entry_ids:
                e.basis_revoked = True
                affected_dates.append(e.date.isoformat())
        if affected_dates:
            sv.status = "affected"
            sv.withdrawal_reason = reason
            self._deactivate_credentials(sv, reason)
        return sorted(affected_dates)

    def create_expiry_job(self, actor: User, scope: str = "all") -> Job:
        """到期检查任务。scope=all 或 revocation:<id> 精准处理某次撤销。"""
        with self.store.lock:
            job = Job(id=self.store.new_id("job"), type="expiry_check", scope=scope,
                      created_at=self.store.clock.now, updated_at=self.store.clock.now)
            self.store.jobs[job.id] = job
            return job

    def _expiry_candidates(self, job: Job) -> list[tuple[ScheduleVersion, set[str], str, Optional[str]]]:
        """返回待处理 (课表版本, 受影响条目id集合, 原因, 撤销id)。"""
        out = []
        rev = None
        if job.scope.startswith("revocation:"):
            rev_id = job.scope.split(":", 1)[1]
            rev = self.store.revocations.get(rev_id)
            if rev is None:
                raise DomainError("撤销通知不存在", code="not_found", status=404)
        for sv in self.store.all_current_schedules():
            if sv.id in job.processed:
                continue
            if sv.status not in ("published", "affected"):
                continue
            hit: set[str] = set()
            reason = ""
            rev_id = None
            if rev is not None:
                for e in sv.entries:
                    if self._entry_depends_on_revocation(e, rev):
                        hit.add(e.entry_id)
                if hit:
                    reason = f"授权提前终止：{rev.reason}"
                    rev_id = rev.id
            else:
                # 全量到期检查：对未执行条目按当前授权链重新核验
                future = [e for e in sv.entries
                          if e.status == "scheduled" and e.date >= self.store.clock.today]
                if future:
                    _, failures = self.validate_window(
                        [(e.combo_version_id, e.date) for e in future],
                        sv.region,
                        min(e.age_min for e in future),
                        max(e.age_max for e in future),
                        sv.purpose, LEVEL_PUBLISHED)
                    bad_keys = {(f["date"], f["combo_version_id"]) for f in failures}
                    for e in future:
                        if (e.date.isoformat(), e.combo_version_id) in bad_keys:
                            hit.add(e.entry_id)
                    if hit:
                        reason = "授权到期或收窄，使用窗口内存在无授权日期"
            # 撤销精准处理：只把确实依赖失效片段的课表列入任务；
            # 全量到期检查则审阅所有已发布/受影响课表（无命中即记为已核查）。
            if hit or (rev is None and sv.status == "published"):
                out.append((sv, hit, reason, rev_id))
        return out

    def run_job(self, actor: User, job_id: str, limit: Optional[int] = None) -> Job:
        """推进任务。limit 用于模拟中断：处理满 limit 个目标后状态记为 interrupted，
        再次调用同 job_id 即从游标续办；已处理条目不重复处理（幂等）。
        """
        with self.store.lock:
            job = self.store.jobs.get(job_id)
            if job is None:
                raise DomainError("任务不存在", code="not_found", status=404)
            job.attempts += 1
            try:
                if job.type == "expiry_check":
                    self._run_expiry(job, limit)
                elif job.type == "replacement_run":
                    self._run_replacements(job, limit)
                else:
                    raise DomainError(f"未知任务类型 {job.type}")
            except _JobInterrupted:
                job.status = "interrupted"
            except Exception as exc:
                job.status = "interrupted"
                job.last_error = str(exc)
                raise
            job.updated_at = self.store.clock.now
            return job

    def _run_expiry(self, job: Job, limit: Optional[int]) -> None:
        candidates = self._expiry_candidates(job)
        processed_this_run = 0
        for sv, hit, reason, rev_id in candidates:
            if limit is not None and processed_this_run >= limit:
                raise _JobInterrupted()
            affected_dates = self._mark_affected(sv, hit, reason, rev_id) if hit else []
            if hit:
                rec = {"schedule_version_id": sv.id, "schedule_key": sv.schedule_key,
                       "school_id": sv.school_id, "grade": sv.grade,
                       "affected_entry_ids": sorted(hit),
                       "affected_dates": affected_dates,
                       "revocation_id": rev_id}
                job.affected.append(rec)
                self._upsert_notice(sv, rec)
            job.processed.append(sv.id)
            processed_this_run += 1
        remaining = [c for c in self._expiry_candidates(job) if c[0].id not in job.processed]
        job.status = "interrupted" if remaining else "completed"
        job.result = {"processed_schedules": len(job.processed),
                      "affected_schedules": len(job.affected)}

    # ============ 替换方案 ============
    def propose_replacement(self, actor: User, old_schedule_version_id: str,
                            old_entry_ids: list[str], new_combo_version_id: str,
                            revocation_id: Optional[str] = None,
                            venue: Optional[str] = None,
                            venue_limit: Optional[str] = None) -> Replacement:
        """提交替换：必须引用原课表；验证时长、难度、适龄（授权）条件。"""
        _require_any(actor, ROLE_COACH, ROLE_MAINTAINER)
        with self.store.lock:
            old = self.store.schedules_by_id.get(old_schedule_version_id)
            if old is None:
                raise DomainError("原课表版本不存在", code="not_found", status=404)
            new_combo = self.store.combos_by_id.get(new_combo_version_id)
            if new_combo is None:
                raise DomainError("新组合版本不存在", code="not_found", status=404)
            targets = [e for e in old.entries if e.entry_id in set(old_entry_ids)]
            if len(targets) != len(set(old_entry_ids)):
                raise DomainError("存在不属于原课表的替换条目")
            if not targets:
                raise DomainError("替换至少包含一个条目")
            if any(e.status != "scheduled" for e in targets):
                raise DomainError("只能替换尚未执行的安排；已完成课程保留原依据")
            if any(not e.basis_revoked for e in targets):
                raise DomainError("只能替换确实依赖失效片段的安排，不得扩大撤回范围")
            old_combos = [self.store.combos_by_id[e.combo_version_id] for e in targets]
            old_total = sum(c.duration_sec for c in old_combos)
            new_total = new_combo.duration_sec * len(targets)
            checks: dict = {
                "duration_old_sec": old_total, "duration_new_sec": new_total,
                "duration_within_tolerance": abs(new_total - old_total) <=
                max(1, int(old_total * DURATION_TOLERANCE)),
                "difficulty_old_max": max(c.difficulty for c in old_combos),
                "difficulty_new_max": new_combo.difficulty,
                "difficulty_ok": (new_combo.difficulty <=
                                  max(c.difficulty for c in old_combos))
                if DIFFICULTY_CAP else True,
            }
            age_min = min(e.age_min for e in targets)
            age_max = max(e.age_max for e in targets)
            days = [e.date for e in targets]
            _, failures = self.validate_window(
                [(new_combo.id, d) for d in days],
                old.region, age_min, age_max,
                old.purpose, LEVEL_SCHEDULED)
            checks["age_range"] = {"age_min": age_min, "age_max": age_max}
            checks["age_appropriate"] = not failures
            checks["coverage_failures"] = failures
            if not checks["duration_within_tolerance"]:
                raise DomainError("替换时长超出原安排 ±10%，验证不通过")
            if not checks["difficulty_ok"]:
                raise DomainError("替换难度高于原安排，验证不通过")
            if failures:
                raise DomainError("替换组合不满足适龄/授权条件，验证不通过",
                                  code="coverage_gap")
            rep = Replacement(
                id=self.store.new_id("rep"), school_id=old.school_id,
                grade=old.grade, old_schedule_version_id=old.id,
                old_entry_ids=list(old_entry_ids),
                new_combo_version_id=new_combo.id,
                revocation_id=revocation_id, submitted_by=actor.id,
                created_at=self.store.clock.now, venue=venue or targets[0].venue,
                venue_limit=venue_limit if venue_limit is not None
                else targets[0].venue_limit, checks=checks)
            self.store.replacements[rep.id] = rep
            return rep

    def review_replacement(self, actor: User, replacement_id: str,
                           approve: bool, note: str = "") -> Replacement:
        """课程审核人放行；提交替换的人不能为自己放行。"""
        _require(actor, ROLE_REVIEWER)
        with self.store.lock:
            rep = self.store.replacements.get(replacement_id)
            if rep is None:
                raise DomainError("替换方案不存在", code="not_found", status=404)
            if rep.status not in ("proposed", "submitted"):
                raise DomainError(f"替换状态 {rep.status}，不可审核")
            if rep.submitted_by == actor.id:
                raise DomainError("提交替换的人不能为自己放行",
                                  code="segregation", status=403)
            rep.reviewed_by = actor.id
            rep.review_note = note
            rep.status = "approved" if approve else "rejected"
            return rep

    def apply_replacement(self, actor: User, replacement_id: str,
                          new_attachments: Optional[list[Attachment]] = None
                          ) -> tuple[Replacement, ScheduleVersion]:
        """发布人执行换版：主课表与附件在同一关键区内一次换版成功。"""
        _require(actor, ROLE_PUBLISHER)
        with self.store.lock:
            return self._apply_replacement(actor, replacement_id, new_attachments)

    def _apply_replacement(self, actor: User, replacement_id: str,
                           new_attachments: Optional[list[Attachment]]
                           ) -> tuple[Replacement, ScheduleVersion]:
        rep = self.store.replacements.get(replacement_id)
        if rep is None:
            raise DomainError("替换方案不存在", code="not_found", status=404)
        if rep.status != "approved":
            raise DomainError(f"替换状态 {rep.status}，未放行不可换版")
        old = self.store.schedules_by_id[rep.old_schedule_version_id]
        current = self.store.current(old.schedule_key)
        if current is None or current.id != old.id:
            raise DomainError("原课表已不是当前版本，请基于现版本重新提交",
                              code="stale", status=409)
        target_ids = set(rep.old_entry_ids)
        new_combo = self.store.combos_by_id[rep.new_combo_version_id]
        new_entries: list[ScheduleEntry] = []
        for e in old.entries:
            if e.entry_id in target_ids:
                ne = ScheduleEntry(
                    entry_id=f"{e.entry_id}_r", date=e.date,
                    combo_version_id=new_combo.id, venue=rep.venue,
                    venue_limit=rep.venue_limit, age_min=e.age_min,
                    age_max=e.age_max)
                new_entries.append(ne)
                e.status = "swapped"
            else:
                new_entries.append(ScheduleEntry(
                    entry_id=e.entry_id, date=e.date,
                    combo_version_id=e.combo_version_id, venue=e.venue,
                    venue_limit=e.venue_limit, age_min=e.age_min,
                    age_max=e.age_max, status=e.status,
                    executed_at=e.executed_at,
                    basis_revoked=e.basis_revoked,
                    license_basis=list(e.license_basis)))
        # 新版本全窗口重新校验授权（已完成条目保留原依据，不重新校验）
        pending_entries = [e for e in new_entries if e.status == "scheduled"]
        _, failures = self.validate_window(
            [(e.combo_version_id, e.date) for e in pending_entries],
            old.region, min(e.age_min for e in new_entries),
            max(e.age_max for e in new_entries), old.purpose, LEVEL_PUBLISHED)
        if failures:
            raise DomainError("换版后授权仍未覆盖整个窗口，换版中止",
                              code="coverage_gap")
        for e in pending_entries:
            e_basis, _ = self._check_combo_on_day(
                self.store.combos_by_id[e.combo_version_id], e.date,
                old.region, e.age_min, e.age_max, old.purpose, LEVEL_PUBLISHED)
            e.license_basis = e_basis
        attachments = list(new_attachments) if new_attachments is not None \
            else list(old.attachments)
        chain = self.store.schedules[old.schedule_key]
        sv = ScheduleVersion(
            id=self.store.new_id("sv"), schedule_key=old.schedule_key,
            version_no=chain[-1].version_no + 1, school_id=old.school_id,
            region=old.region, grade=old.grade, purpose=old.purpose,
            entries=new_entries, attachments=attachments, status="published",
            supersedes_id=old.id, origin_replacement_id=rep.id,
            origin_schedule_version_id=old.id, submitted_by=rep.submitted_by,
            approved_by=rep.reviewed_by, published_by=actor.id,
            created_at=self.store.clock.now, published_at=self.store.clock.now)
        sv.approval_trace.extend(old.approval_trace)
        sv.approval_trace.append(TraceEvent(
            level=LEVEL_PUBLISHED, action="republished", actor_id=actor.id,
            at=self.store.clock.now,
            note=f"按替换方案 {rep.id} 一次换版（主课表+附件）"))
        # ---- 原子换版关键区：旧版本落 replaced、指针切换、旧凭据失效 ----
        old.status = "replaced"
        self._deactivate_credentials(old, f"已由替换方案 {rep.id} 换版")
        self.store.add_schedule(sv)
        self.store.schedule_current[sv.schedule_key] = sv.id
        rep.status = "applied"
        rep.new_schedule_version_id = sv.id
        rep.processed_at = self.store.clock.now
        self._refresh_notice_after_replacement(rep, old, sv)
        return rep, sv

    def create_replacement_job(self, actor: User) -> Job:
        with self.store.lock:
            job = Job(id=self.store.new_id("job"), type="replacement_run",
                      scope="approved_replacements",
                      created_at=self.store.clock.now, updated_at=self.store.clock.now)
            self.store.jobs[job.id] = job
            return job

    def _run_replacements(self, job: Job, limit: Optional[int]) -> None:
        pending = [r for r in self.store.replacements.values()
                   if r.status == "approved" and r.id not in job.processed]
        done = 0
        for rep in pending:
            if limit is not None and done >= limit:
                raise _JobInterrupted()
            _, sv = self._apply_replacement(
                self._publisher_actor(), rep.id, None)
            job.processed.append(rep.id)
            job.affected.append({
                "replacement_id": rep.id,
                "new_schedule_version_id": sv.id,
                "school_id": rep.school_id, "grade": rep.grade})
            done += 1
        remain = [r for r in self.store.replacements.values()
                  if r.status == "approved" and r.id not in job.processed]
        job.status = "interrupted" if remain else "completed"
        job.result = {"applied": len(job.processed)}

    def _publisher_actor(self) -> User:
        # 批量任务以系统内发布人身份执行；实际部署由任务队列携带发布人身份。
        publishers = [u for u in self.store.users.values() if u.role == ROLE_PUBLISHER]
        if not publishers:
            raise DomainError("系统中无发布人，无法执行替换换版任务")
        return publishers[0]

    # ============ 学校通告 ============
    def _upsert_notice(self, sv: ScheduleVersion, rec: dict) -> Notice:
        notices = [n for n in self.store.notices[sv.school_id]
                   if n.schedule_key == sv.schedule_key]
        rev = None
        if rec.get("revocation_id"):
            rev = self.store.revocations.get(rec["revocation_id"])
        basis = []
        for e in sv.entries:
            if e.entry_id in set(rec["affected_entry_ids"]):
                for b in e.license_basis:
                    lic = self.store.licenses_by_id.get(b.license_version_id)
                    basis.append({
                        "clip_id": b.clip_id, "material_id": b.material_id,
                        "license_version_id": b.license_version_id,
                        "rights_holder": lic.rights_holder if lic else None,
                        "on_date": b.on_date.isoformat(),
                        "evidence_summary": lic.evidence_summary if lic else None,
                    })
        if notices:
            notice = notices[-1]
            notice.affected_dates = sorted(set(notice.affected_dates +
                                               rec["affected_dates"]))
            notice.updated_at = self.store.clock.now
            if rev:
                notice.withdrawal_reason = rev.reason
                notice.revocation_id = rev.id
        else:
            notice = Notice(
                id=self.store.new_id("ntc"), school_id=sv.school_id,
                revocation_id=rev.id if rev else None,
                schedule_key=sv.schedule_key,
                adopted_versions=[{"schedule_version_id": sv.id,
                                   "version_no": sv.version_no,
                                   "state": "affected_pending_replacement"}],
                license_basis=basis, affected_dates=list(rec["affected_dates"]),
                withdrawal_reason=rev.reason if rev else
                (sv.withdrawal_reason or "授权到期或收窄"),
                new_arrangement_ref={"status": "pending"},
                created_at=self.store.clock.now)
            self.store.notices[sv.school_id].append(notice)
        return notice

    def _refresh_notice_after_replacement(self, rep: Replacement,
                                          old: ScheduleVersion,
                                          sv: ScheduleVersion) -> None:
        notices = [n for n in self.store.notices[sv.school_id]
                   if n.schedule_key == sv.schedule_key]
        new_basis = []
        for e in sv.entries:
            if e.entry_id in {f"{x}_r" for x in rep.old_entry_ids}:
                for b in e.license_basis:
                    lic = self.store.licenses_by_id.get(b.license_version_id)
                    new_basis.append({
                        "clip_id": b.clip_id, "material_id": b.material_id,
                        "license_version_id": b.license_version_id,
                        "rights_holder": lic.rights_holder if lic else None,
                        "on_date": b.on_date.isoformat(),
                        "evidence_summary": lic.evidence_summary if lic else None,
                    })
        adopted = [{"schedule_version_id": sv.id, "version_no": sv.version_no,
                    "combo_version_ids": sorted({e.combo_version_id
                                                 for e in sv.entries}),
                    "state": "published"}]
        ref = {"status": "arranged", "replacement_id": rep.id,
               "schedule_key": sv.schedule_key,
               "new_schedule_version_id": sv.id}
        if notices:
            notice = notices[-1]
            notice.adopted_versions = adopted
            notice.license_basis = new_basis
            notice.new_arrangement_ref = ref
            notice.updated_at = self.store.clock.now
        else:
            date_by_entry = {e.entry_id: e.date for e in old.entries}
            notice = Notice(
                id=self.store.new_id("ntc"), school_id=sv.school_id,
                revocation_id=rep.revocation_id, schedule_key=sv.schedule_key,
                adopted_versions=adopted, license_basis=new_basis,
                affected_dates=sorted(date_by_entry[x].isoformat()
                                      for x in rep.old_entry_ids),
                withdrawal_reason=(self.store.revocations[rep.revocation_id].reason
                                   if rep.revocation_id and
                                   rep.revocation_id in self.store.revocations
                                   else "授权收窄"),
                new_arrangement_ref=ref, created_at=self.store.clock.now)
            self.store.notices[sv.school_id].append(notice)

    def school_notices(self, actor: User, school_id: str) -> list[Notice]:
        with self.store.lock:
            if school_id not in self.store.schools:
                raise DomainError("学校不存在", code="not_found", status=404)
            return list(self.store.notices.get(school_id, []))


class _JobInterrupted(Exception):
    """内部信号：本批次处理额度用完，任务保持 interrupted 等待续办。"""
