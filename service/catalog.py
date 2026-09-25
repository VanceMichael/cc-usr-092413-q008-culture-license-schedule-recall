"""素材目录：授权版本、片段、动作单元、动作组合。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from .errors import Conflict, NotFound, ServiceError
from .models import (
    KIND_GRANT,
    KIND_REVISION,
    Clip,
    ClipRef,
    ComboVersion,
    LicenseVersion,
    UnitMember,
    UnitVersion,
)
from .store import Store, utcnow


class CatalogService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # -- 片段 -----------------------------------------------------------------

    def register_clip(self, clip_id: str, title: str, duration_sec: int,
                      source_ref: str, created_by: str) -> Clip:
        if duration_sec <= 0:
            raise ServiceError("片段时长必须为正数")
        if clip_id in self.store.clips:
            raise Conflict(f"片段已存在: {clip_id}")
        clip = Clip(clip_id=clip_id, title=title, duration_sec=duration_sec,
                    source_ref=source_ref, created_by=created_by,
                    created_at=utcnow())
        self.store.add_clip(clip)
        return clip

    # -- 授权版本 -------------------------------------------------------------

    def grant_license(self, license_id: str, rights_holder: str,
                      allowed_uses: list[str], regions: list[str],
                      clip_ids: list[str], age_min: int, age_max: int,
                      valid_from: date, valid_to: Optional[date],
                      evidence_summary: str, evidence_ref: str,
                      created_by: str) -> LicenseVersion:
        if license_id in self.store.licenses:
            raise Conflict(f"授权已存在，请用换版接口收窄: {license_id}")
        self._validate_clip_ids(clip_ids)
        self._validate_terms(allowed_uses, age_min, age_max, valid_from, valid_to)
        lv = LicenseVersion(
            license_id=license_id, version=1, kind=KIND_GRANT,
            rights_holder=rights_holder, allowed_uses=list(allowed_uses),
            regions=list(regions) or ["*"], clip_ids=list(clip_ids),
            age_min=age_min, age_max=age_max,
            valid_from=valid_from, valid_to=valid_to,
            evidence_summary=evidence_summary, evidence_ref=evidence_ref,
            supersedes=None, created_by=created_by, created_at=utcnow(),
        )
        self.store.add_license_version(lv)
        return lv

    def revise_license(self, license_id: str, rights_holder: str,
                       allowed_uses: list[str], regions: list[str],
                       clip_ids: list[str], age_min: int, age_max: int,
                       valid_from: date, valid_to: Optional[date],
                       evidence_summary: str, evidence_ref: str,
                       created_by: str) -> LicenseVersion:
        """授权收窄/变更：追加一个可追溯的新版本，旧版本保留不删。"""
        chain = self.store.get_license_versions(license_id)
        active = [v for v in chain if v.kind != "termination"]
        if not active:
            raise NotFound(f"授权不存在: {license_id}")
        prev = active[-1]
        self._validate_clip_ids(clip_ids)
        self._validate_terms(allowed_uses, age_min, age_max, valid_from, valid_to)
        if valid_from < prev.valid_from:
            raise ServiceError("新版本生效日不得早于首授生效日")
        lv = LicenseVersion(
            license_id=license_id, version=len(chain) + 1, kind=KIND_REVISION,
            rights_holder=rights_holder, allowed_uses=list(allowed_uses),
            regions=list(regions) or ["*"], clip_ids=list(clip_ids),
            age_min=age_min, age_max=age_max,
            valid_from=valid_from, valid_to=valid_to,
            evidence_summary=evidence_summary, evidence_ref=evidence_ref,
            supersedes=prev.version, created_by=created_by, created_at=utcnow(),
        )
        self.store.add_license_version(lv)
        return lv

    def get_version_chain(self, license_id: str) -> list[LicenseVersion]:
        chain = self.store.get_license_versions(license_id)
        if not chain:
            raise NotFound(f"授权不存在: {license_id}")
        return chain

    # -- 动作单元 -------------------------------------------------------------

    def create_unit_version(self, unit_id: str, name: str,
                            clip_refs: list[dict], duration_sec: int,
                            difficulty: int, age_min: int, age_max: int,
                            created_by: str) -> UnitVersion:
        if not clip_refs:
            raise ServiceError("动作单元必须标明实际引用的片段")
        refs = [self._build_clip_ref(r) for r in clip_refs]
        self._validate_content_terms(duration_sec, difficulty, age_min, age_max)
        prev = self.store.latest_unit(unit_id)
        uv = UnitVersion(
            unit_id=unit_id, version=(prev.version + 1) if prev else 1,
            name=name, clip_refs=refs, duration_sec=duration_sec,
            difficulty=difficulty, age_min=age_min, age_max=age_max,
            supersedes=prev.version if prev else None,
            created_by=created_by, created_at=utcnow(),
        )
        self.store.add_unit_version(uv)
        return uv

    # -- 动作组合 -------------------------------------------------------------

    def create_combo_version(self, combo_id: str, name: str,
                             members: list[dict], created_by: str) -> ComboVersion:
        if not members:
            raise ServiceError("组合至少包含一个动作单元")
        resolved_members: list[UnitMember] = []
        clip_set: list[str] = []
        total_duration = 0
        diffs: list[int] = []
        age_mins: list[int] = []
        age_maxes: list[int] = []
        for m in members:
            uv = self.store.unit_version_at(m["unit_id"], m["version"])
            resolved_members.append(UnitMember(unit_id=uv.unit_id, version=uv.version))
            for ref in uv.clip_refs:
                if ref.clip_id not in clip_set:
                    clip_set.append(ref.clip_id)
            total_duration += uv.duration_sec
            diffs.append(uv.difficulty)
            age_mins.append(uv.age_min)
            age_maxes.append(uv.age_max)
        age_min = max(age_mins)
        age_max = min(age_maxes)
        if age_min > age_max:
            raise ServiceError("组合内单元的适龄区间没有交集，不能组成同一组合")
        prev = self.store.latest_combo(combo_id)
        cv = ComboVersion(
            combo_id=combo_id, version=(prev.version + 1) if prev else 1,
            name=name, members=resolved_members, resolved_clip_ids=clip_set,
            duration_sec=total_duration, difficulty=max(diffs),
            age_min=age_min, age_max=age_max,
            supersedes=prev.version if prev else None,
            created_by=created_by, created_at=utcnow(),
        )
        self.store.add_combo_version(cv)
        return cv

    def resolve_content(self, content_type: str, content_id: str,
                        version: int):
        if content_type == "unit":
            uv = self.store.unit_version_at(content_id, version)
            clip_ids = [r.clip_id for r in uv.clip_refs]
            return uv, clip_ids
        if content_type == "combo":
            cv = self.store.combo_version_at(content_id, version)
            return cv, list(cv.resolved_clip_ids)
        raise ServiceError("内容类型必须是 unit 或 combo")

    # -- 校验 -----------------------------------------------------------------

    def _validate_clip_ids(self, clip_ids: list[str]) -> None:
        if not clip_ids:
            raise ServiceError("授权必须覆盖至少一个片段")
        for cid in clip_ids:
            self.store.require_clip(cid)

    @staticmethod
    def _validate_terms(allowed_uses, age_min, age_max, valid_from, valid_to):
        if not allowed_uses:
            raise ServiceError("允许用途不能为空")
        if age_min < 0 or age_min > age_max:
            raise ServiceError("年龄范围不合法")
        if valid_to is not None and valid_to < valid_from:
            raise ServiceError("生效区间不合法：止日早于起日")

    @staticmethod
    def _validate_content_terms(duration_sec, difficulty, age_min, age_max):
        if duration_sec <= 0:
            raise ServiceError("时长必须为正数")
        if not 1 <= difficulty <= 5:
            raise ServiceError("难度必须在 1-5 之间")
        if age_min > age_max:
            raise ServiceError("适龄区间不合法")

    def _build_clip_ref(self, raw: dict) -> ClipRef:
        clip = self.store.require_clip(raw["clip_id"])
        start, end = int(raw["start_sec"]), int(raw["end_sec"])
        if start < 0 or end <= start or end > clip.duration_sec:
            raise ServiceError(
                f"片段引用区间不合法: {clip.clip_id}[{start},{end}]"
            )
        return ClipRef(clip_id=clip.clip_id, start_sec=start, end_sec=end)
