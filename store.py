"""带锁的内存存储。生产环境可替换为数据库实现，接口保持一致。"""
from __future__ import annotations

import itertools
import threading
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from domain import (
    ActionUnitVersion, Clip, ComboVersion, Credential, Job, LicenseVersion,
    Material, Notice, Replacement, RevocationNotice, School, ScheduleVersion, User,
)


class Clock:
    def __init__(self, today: date, now: datetime):
        self.today = today
        self.now = now

    def advance(self, days: int = 0, **now_kwargs) -> None:
        from datetime import timedelta
        self.today += timedelta(days=days)
        if now_kwargs:
            self.now = self.now.replace(**now_kwargs)
        elif days:
            self.now += timedelta(days=days)


class Store:
    def __init__(self, clock: Optional[Clock] = None):
        self.lock = threading.RLock()
        self.clock = clock or Clock(date(2026, 9, 25), datetime(2026, 9, 25, 9, 0, 0))
        self.users: dict[str, User] = {}
        self.schools: dict[str, School] = {}
        self.materials: dict[str, Material] = {}
        self.clips: dict[str, Clip] = {}
        self.clips_by_material: dict[str, list[Clip]] = defaultdict(list)
        # license_key -> [LicenseVersion...]（按 version_no 升序）
        self.license_versions: dict[str, list[LicenseVersion]] = defaultdict(list)
        self.licenses_by_id: dict[str, LicenseVersion] = {}
        self.revocations: dict[str, RevocationNotice] = {}
        # unit_key -> [ActionUnitVersion...]
        self.units: dict[str, list[ActionUnitVersion]] = defaultdict(list)
        self.units_by_id: dict[str, ActionUnitVersion] = {}
        # combo_key -> [ComboVersion...]
        self.combos: dict[str, list[ComboVersion]] = defaultdict(list)
        self.combos_by_id: dict[str, ComboVersion] = {}
        # schedule_key -> [ScheduleVersion...]，current_id 指向当前版本
        self.schedules: dict[str, list[ScheduleVersion]] = defaultdict(list)
        self.schedule_current: dict[str, str] = {}
        self.schedules_by_id: dict[str, ScheduleVersion] = {}
        self.credentials: dict[str, Credential] = {}
        self.credentials_by_schedule: dict[str, list[Credential]] = defaultdict(list)
        self.replacements: dict[str, Replacement] = {}
        self.jobs: dict[str, Job] = {}
        self.notices: dict[str, list[Notice]] = defaultdict(list)
        self._seq = itertools.count(1)

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._seq):04d}"

    # ---- 授权链 ----
    def add_license(self, lic: LicenseVersion) -> None:
        with self.lock:
            self.license_versions[lic.license_key].append(lic)
            self.license_versions[lic.license_key].sort(key=lambda x: x.version_no)
            self.licenses_by_id[lic.id] = lic

    def license_chain(self, license_key: str) -> list[LicenseVersion]:
        with self.lock:
            return list(self.license_versions.get(license_key, []))

    def license_on(self, license_key: str, day: date) -> Optional[LicenseVersion]:
        """某日适用的授权版本：effective_from <= day 的最新版本。"""
        with self.lock:
            cand = [v for v in self.license_versions.get(license_key, [])
                    if v.effective_from <= day]
            return cand[-1] if cand else None

    def revocations_for(self, license_version_id: str) -> list[RevocationNotice]:
        with self.lock:
            return [r for r in self.revocations.values()
                    if r.license_version_id == license_version_id]

    # ---- 动作单元 / 组合 ----
    def add_unit(self, u: ActionUnitVersion) -> None:
        with self.lock:
            self.units[u.unit_key].append(u)
            self.units[u.unit_key].sort(key=lambda x: x.version_no)
            self.units_by_id[u.id] = u

    def add_combo(self, c: ComboVersion) -> None:
        with self.lock:
            self.combos[c.combo_key].append(c)
            self.combos[c.combo_key].sort(key=lambda x: x.version_no)
            self.combos_by_id[c.id] = c

    # ---- 课表版本 ----
    def add_schedule(self, sv: ScheduleVersion) -> None:
        with self.lock:
            self.schedules[sv.schedule_key].append(sv)
            self.schedules[sv.schedule_key].sort(key=lambda x: x.version_no)
            self.schedules_by_id[sv.id] = sv
            if sv.schedule_key not in self.schedule_current:
                self.schedule_current[sv.schedule_key] = sv.id

    def current(self, schedule_key: str) -> Optional[ScheduleVersion]:
        with self.lock:
            sid = self.schedule_current.get(schedule_key)
            return self.schedules_by_id.get(sid) if sid else None

    def all_current_schedules(self) -> list[ScheduleVersion]:
        with self.lock:
            return [self.schedules_by_id[sid]
                    for sid in self.schedule_current.values()
                    if sid in self.schedules_by_id]

    def add_credential(self, c: Credential) -> None:
        with self.lock:
            self.credentials[c.id] = c
            self.credentials_by_schedule[c.schedule_version_id].append(c)

    def reset(self) -> None:
        """测试间清空全部数据。"""
        with self.lock:
            self.__init__(self.clock)  # type: ignore[misc]
