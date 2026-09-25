"""线程安全的内存仓储（追加式记录 + 索引）。

生产中可替换为数据库实现；当前服务只依赖本模块暴露的接口。
所有写入均在 RLock 内完成，保证并发到达的撤销与批量发布互不踩数据。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, TypeVar

from .errors import Conflict, NotFound
from .models import (
    KIND_TERMINATION,
    Clip,
    ComboVersion,
    DownloadCredential,
    Job,
    LicenseVersion,
    RevocationNotice,
    ScheduleDocVersion,
    ScheduleEntry,
    School,
    SchoolNotice,
    UnitVersion,
)

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()

        self.schools: dict[str, School] = {}

        self.clips: dict[str, Clip] = {}

        # license_id -> 按版本顺序排列的授权版本
        self.licenses: dict[str, list[LicenseVersion]] = {}
        # clip_id -> 覆盖该片段的授权版本
        self.clip_licenses: dict[str, list[LicenseVersion]] = {}

        self.units: dict[str, list[UnitVersion]] = {}
        self.combos: dict[str, list[ComboVersion]] = {}

        self.entries: dict[str, ScheduleEntry] = {}
        # clip_id -> {entry_id}：实际依赖该片段的安排
        self.entries_by_clip: dict[str, set[str]] = {}
        # (content_key, version) -> {entry_id}
        self.entries_by_content: dict[tuple[str, int], set[str]] = {}

        # doc_id -> 版本；主课表与附件同版
        self.docs: dict[str, list[ScheduleDocVersion]] = {}

        self.notices_by_id: dict[str, RevocationNotice] = {}

        # 替换单
        self.replacements: dict[str, Replacement] = {}

        # 失效课表不得继续生成下载凭据：token -> 凭据
        self.credentials: dict[str, DownloadCredential] = {}

        # 可续办任务
        self.jobs: dict[str, Job] = {}

        # 学校告知
        self.school_notices: dict[str, SchoolNotice] = {}

        self._seq = 0

    # -- 通用 -----------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:05d}"

    def next_entry_id(self) -> str:
        return self._next_id("ent")

    def next_token(self) -> str:
        return self._next_id("dl")

    def next_replacement_id(self) -> str:
        return self._next_id("repl")

    def next_school_notice_id(self) -> str:
        return self._next_id("snotice")

    def next_job_id(self) -> str:
        return self._next_id("job")

    def transaction(self, fn: "Callable[[], T]") -> T:
        """撤销换版与批量发布共用的串行化点。"""
        with self._lock:
            return fn()

    # -- School ---------------------------------------------------------------

    def add_school(self, school: School) -> None:
        self.schools[school.school_id] = school

    def get_school(self, school_id: str) -> School:
        school = self.schools.get(school_id)
        if school is None:
            raise NotFound(f"学校不存在: {school_id}")
        return school

    # -- Clip -----------------------------------------------------------------

    def add_clip(self, clip: Clip) -> None:
        self.clips[clip.clip_id] = clip

    def get_clip(self, clip_id: str) -> Clip | None:
        return self.clips.get(clip_id)

    def require_clip(self, clip_id: str) -> Clip:
        clip = self.get_clip(clip_id)
        if clip is None:
            raise NotFound(f"片段不存在: {clip_id}")
        return clip

    # -- License --------------------------------------------------------------

    def add_license_version(self, lv: LicenseVersion) -> None:
        self.licenses.setdefault(lv.license_id, []).append(lv)
        for cid in lv.clip_ids:
            self.clip_licenses.setdefault(cid, []).append(lv)

    def get_license_versions(self, license_id: str) -> list[LicenseVersion]:
        return self.licenses.get(license_id, [])

    def get_latest_version(self, license_id: str) -> LicenseVersion | None:
        vs = self.licenses.get(license_id)
        return vs[-1] if vs else None

    def license_version_at(self, license_id: str, version: int) -> LicenseVersion:
        for lv in self.licenses.get(license_id, []):
            if lv.version == version:
                return lv
        raise KeyError((license_id, version))

    def latest_covering_version(self, clip_id: str) -> LicenseVersion | None:
        """覆盖该片段的、每个授权链上最新的非终止版本。"""
        result: LicenseVersion | None = None
        for lv in self.clip_licenses.get(clip_id, []):
            chain = [v for v in self.licenses[lv.license_id]
                     if v.kind != KIND_TERMINATION and clip_id in v.clip_ids]
            if chain:
                candidate = chain[-1]
                if result is None or candidate.created_at > result.created_at:
                    result = candidate
        return result

    def all_active_versions_for_clip(self, clip_id: str) -> list[LicenseVersion]:
        result: list[LicenseVersion] = []
        seen: set[str] = set()
        for lv in self.clip_licenses.get(clip_id, []):
            chain = [v for v in self.licenses[lv.license_id]
                     if v.kind != KIND_TERMINATION and clip_id in v.clip_ids]
            if chain and chain[-1].license_id not in seen:
                seen.add(chain[-1].license_id)
                result.append(chain[-1])
        return result

    def termination_for(self, license_id: str) -> LicenseVersion | None:
        for lv in self.licenses.get(license_id, []):
            if lv.kind == KIND_TERMINATION:
                return lv
        return None

    # -- Units / Combos -------------------------------------------------------

    def add_unit_version(self, uv: UnitVersion) -> None:
        self.units.setdefault(uv.unit_id, []).append(uv)

    def get_unit_versions(self, unit_id: str) -> list[UnitVersion]:
        return self.units.get(unit_id, [])

    def unit_version_at(self, unit_id: str, version: int) -> UnitVersion:
        for uv in self.units.get(unit_id, []):
            if uv.version == version:
                return uv
        raise KeyError((unit_id, version))

    def latest_unit(self, unit_id: str) -> UnitVersion | None:
        vs = self.units.get(unit_id)
        return vs[-1] if vs else None

    def add_combo_version(self, cv: ComboVersion) -> None:
        self.combos.setdefault(cv.combo_id, []).append(cv)

    def get_combo_versions(self, combo_id: str) -> list[ComboVersion]:
        return self.combos.get(combo_id, [])

    def latest_combo(self, combo_id: str) -> ComboVersion | None:
        vs = self.combos.get(combo_id)
        return vs[-1] if vs else None

    def combo_version_at(self, combo_id: str, version: int) -> ComboVersion:
        for cv in self.combos.get(combo_id, []):
            if cv.version == version:
                return cv
        raise KeyError((combo_id, version))

    # -- Entries --------------------------------------------------------------

    def add_entry(self, entry: ScheduleEntry) -> None:
        self.entries[entry.entry_id] = entry
        for cid in entry.resolved_clip_ids:
            self.entries_by_clip.setdefault(cid, set()).add(entry.entry_id)
        key = (f"{entry.content_type}:{entry.content_id}", entry.content_version)
        self.entries_by_content.setdefault(key, set()).add(entry.entry_id)

    def update_entry(self, entry: ScheduleEntry) -> None:
        # 依赖集合在创建时按 resolved_clip_ids 建立，状态变更不改集合。
        self.entries[entry.entry_id] = entry

    def all_entries(self) -> list[ScheduleEntry]:
        return list(self.entries.values())

    def get_entry(self, entry_id: str) -> ScheduleEntry:
        entry = self.entries.get(entry_id)
        if entry is None:
            raise NotFound(f"课表安排不存在: {entry_id}")
        return entry

    def entries_referencing_clips(self, clip_ids: set[str]) -> list[ScheduleEntry]:
        ids: set[str] = set()
        for cid in clip_ids:
            ids |= self.entries_by_clip.get(cid, set())
        return [self.entries[i] for i in sorted(ids) if i in self.entries]

    # -- Docs -----------------------------------------------------------------

    def add_doc_version(self, dv: ScheduleDocVersion) -> None:
        self.docs.setdefault(dv.doc_id, []).append(dv)

    def latest_doc(self, school_id: str) -> ScheduleDocVersion | None:
        versions = [v for vs in self.docs.values() for v in vs
                    if v.school_id == school_id]
        if not versions:
            return None
        return max(versions, key=lambda v: v.version)

    def doc_version_at(self, school_id: str, version: int) -> ScheduleDocVersion | None:
        for vs in self.docs.values():
            for v in vs:
                if v.school_id == school_id and v.version == version:
                    return v
        return None

    # -- Notices --------------------------------------------------------------

    def add_notice(self, notice: RevocationNotice) -> None:
        if notice.notice_id in self.notices_by_id:
            raise Conflict(f"撤销通知已存在: {notice.notice_id}")
        self.notices_by_id[notice.notice_id] = notice

    def get_notice(self, notice_id: str) -> RevocationNotice:
        notice = self.notices_by_id.get(notice_id)
        if notice is None:
            raise NotFound(f"撤销通知不存在: {notice_id}")
        return notice

    def all_notices(self) -> list[RevocationNotice]:
        return list(self.notices_by_id.values())

    # -- Replacements ---------------------------------------------------------

    def add_replacement(self, repl: Replacement) -> None:
        self.replacements[repl.replacement_id] = repl

    def update_replacement(self, repl: Replacement) -> None:
        self.replacements[repl.replacement_id] = repl

    def get_replacement(self, replacement_id: str) -> Replacement:
        repl = self.replacements.get(replacement_id)
        if repl is None:
            raise NotFound(f"替换单不存在: {replacement_id}")
        return repl

    def replacements_for_entry(self, entry_id: str) -> list[Replacement]:
        return [r for r in self.replacements.values()
                if r.original_entry_id == entry_id]

    # -- Credentials ----------------------------------------------------------

    def add_credential(self, cred: DownloadCredential) -> None:
        self.credentials[cred.token] = cred

    def get_credential(self, token: str) -> DownloadCredential | None:
        return self.credentials.get(token)

    # -- Jobs -----------------------------------------------------------------

    def add_job(self, job: Job) -> None:
        self.jobs[job.job_id] = job

    def update_job(self, job: Job) -> None:
        self.jobs[job.job_id] = job

    def get_job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise NotFound(f"任务不存在: {job_id}")
        return job

    def all_jobs(self) -> list[Job]:
        return list(self.jobs.values())

    # -- School notices -------------------------------------------------------

    def add_school_notice(self, sn: SchoolNotice) -> None:
        self.school_notices[sn.notice_id] = sn

    def update_school_notice(self, sn: SchoolNotice) -> None:
        self.school_notices[sn.notice_id] = sn

    def school_notices_for(self, school_id: str) -> list[SchoolNotice]:
        return [sn for sn in self.school_notices.values() if sn.school_id == school_id]
