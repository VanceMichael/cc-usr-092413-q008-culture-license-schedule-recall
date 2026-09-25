"""领域模型：授权版本、素材片段、动作/组合版本、课表版本、替换方案、任务、通告。

所有记录均为可追溯的不可变版本：授权收窄或提前终止不修改旧版本，
而是追加新版本或撤销通知；课表换版时旧版本保留、指针整体切换。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

# 角色
ROLE_MAINTAINER = "material_maintainer"  # 素材维护者
ROLE_REVIEWER = "curriculum_reviewer"    # 课程审核人
ROLE_PUBLISHER = "publisher"             # 发布人
ROLE_COACH = "regional_coach"            # 区域教研员

# 审批水位
LEVEL_SCHEDULED = 1   # 区域教研员排课/提交
LEVEL_REVIEWED = 2    # 课程审核人审核通过
LEVEL_PUBLISHED = 3   # 发布人发布

# 用途
USE_BREAK_EXERCISE = "break_exercise"  # 课间操


class DomainError(Exception):
    """业务规则错误，HTTP 层映射为 4xx。"""

    def __init__(self, message: str, code: str = "domain_error", status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class User:
    id: str
    name: str
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "role": self.role}


@dataclass
class School:
    id: str
    name: str
    region: str
    # 年级 -> 学生年龄区间（用于授权年龄范围校验）
    grades: dict[str, tuple[int, int]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "region": self.region,
            "grades": {g: {"age_min": a, "age_max": b} for g, (a, b) in self.grades.items()},
        }


@dataclass
class Material:
    id: str
    title: str
    kind: str                 # 例如 mural=壁画动作参考
    license_key: str          # 授权链编号
    maintainer_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "kind": self.kind,
            "license_key": self.license_key, "maintainer_id": self.maintainer_id,
        }


@dataclass
class Clip:
    """素材内可被动作单元实际引用的片段。"""
    id: str
    material_id: str
    code: str
    start_sec: int
    end_sec: int
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "material_id": self.material_id, "code": self.code,
            "start_sec": self.start_sec, "end_sec": self.end_sec,
            "description": self.description,
        }


@dataclass
class LicenseVersion:
    """授权版本：权利方、允许用途、地区、年龄、生效区间、证据摘要。"""
    id: str
    license_key: str
    version_no: int
    rights_holder: str
    permitted_uses: list[str]
    regions: list[str]                # ["*"] 表示不限地区
    age_min: int
    age_max: int
    effective_from: date
    effective_to: date
    evidence_summary: str
    evidence_refs: list[str]
    created_by: str
    created_at: datetime
    supersedes_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "license_key": self.license_key, "version_no": self.version_no,
            "rights_holder": self.rights_holder, "permitted_uses": list(self.permitted_uses),
            "regions": list(self.regions), "age_min": self.age_min, "age_max": self.age_max,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat(),
            "evidence_summary": self.evidence_summary,
            "evidence_refs": list(self.evidence_refs),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "supersedes_id": self.supersedes_id,
        }


@dataclass
class RevocationNotice:
    """素材馆发出的提前终止（撤销）通知。"""
    id: str
    license_version_id: str
    notified_at: datetime
    covers_through: date   # 该授权版本在该日（含）之后不再覆盖使用
    reason: str
    evidence_refs: list[str]
    received_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "license_version_id": self.license_version_id,
            "notified_at": self.notified_at.isoformat(),
            "covers_through": self.covers_through.isoformat(),
            "reason": self.reason, "evidence_refs": list(self.evidence_refs),
            "received_by": self.received_by,
        }


@dataclass
class ClipRef:
    """动作单元版本对素材片段的实际引用。"""
    clip_id: str
    material_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"clip_id": self.clip_id, "material_id": self.material_id}


@dataclass
class ActionUnitVersion:
    id: str
    unit_key: str
    version_no: int
    name: str
    clip_refs: list[ClipRef]
    duration_sec: int
    difficulty: int          # 1-5
    maintainer_id: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "unit_key": self.unit_key, "version_no": self.version_no,
            "name": self.name, "clip_refs": [r.to_dict() for r in self.clip_refs],
            "duration_sec": self.duration_sec, "difficulty": self.difficulty,
            "maintainer_id": self.maintainer_id, "created_at": self.created_at.isoformat(),
        }


@dataclass
class ComboMember:
    unit_version_id: str
    duration_sec: int

    def to_dict(self) -> dict[str, Any]:
        return {"unit_version_id": self.unit_version_id, "duration_sec": self.duration_sec}


@dataclass
class ComboVersion:
    id: str
    combo_key: str
    version_no: int
    name: str
    members: list[ComboMember]
    maintainer_id: str
    created_at: datetime
    difficulty: int = 1   # 创建时由 service 按成员最高难度计算

    @property
    def duration_sec(self) -> int:
        return sum(m.duration_sec for m in self.members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "combo_key": self.combo_key, "version_no": self.version_no,
            "name": self.name, "members": [m.to_dict() for m in self.members],
            "duration_sec": self.duration_sec, "difficulty": self.difficulty,
            "maintainer_id": self.maintainer_id, "created_at": self.created_at.isoformat(),
        }


@dataclass
class ClipBasis:
    """排课时留存的逐项授权依据快照。"""
    clip_id: str
    material_id: str
    license_version_id: str
    on_date: date
    checked_at: datetime
    water_level: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id, "material_id": self.material_id,
            "license_version_id": self.license_version_id,
            "on_date": self.on_date.isoformat(),
            "checked_at": self.checked_at.isoformat(),
            "water_level": self.water_level,
        }


@dataclass
class ScheduleEntry:
    entry_id: str
    date: date
    combo_version_id: str
    venue: str
    venue_limit: str
    age_min: int
    age_max: int
    status: str = "scheduled"          # scheduled | executed | swapped
    executed_at: Optional[datetime] = None
    basis_revoked: bool = False
    license_basis: list[ClipBasis] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id, "date": self.date.isoformat(),
            "combo_version_id": self.combo_version_id, "venue": self.venue,
            "venue_limit": self.venue_limit, "age_min": self.age_min, "age_max": self.age_max,
            "status": self.status,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "basis_revoked": self.basis_revoked,
            "license_basis": [b.to_dict() for b in self.license_basis],
        }


@dataclass
class Attachment:
    attachment_id: str
    name: str
    kind: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"attachment_id": self.attachment_id, "name": self.name,
                "kind": self.kind, "content": self.content}


@dataclass
class TraceEvent:
    level: int
    action: str
    actor_id: str
    at: datetime
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level, "action": self.action, "actor_id": self.actor_id,
            "at": self.at.isoformat(), "note": self.note,
        }


@dataclass
class ScheduleVersion:
    """主课表 + 附件的一次整体版本；换版时二者一起切换。"""
    id: str
    schedule_key: str
    version_no: int
    school_id: str
    region: str
    grade: str
    purpose: str
    entries: list[ScheduleEntry]
    attachments: list[Attachment]
    status: str                        # pending_approval|approved|published|affected|replaced|withdrawn
    supersedes_id: Optional[str] = None
    origin_replacement_id: Optional[str] = None
    origin_schedule_version_id: Optional[str] = None
    submitted_by: Optional[str] = None
    approved_by: Optional[str] = None
    published_by: Optional[str] = None
    withdrawal_reason: Optional[str] = None
    approval_trace: list[TraceEvent] = field(default_factory=list)
    created_at: Optional[datetime] = None
    published_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "schedule_key": self.schedule_key, "version_no": self.version_no,
            "school_id": self.school_id, "region": self.region, "grade": self.grade,
            "purpose": self.purpose,
            "entries": [e.to_dict() for e in self.entries],
            "attachments": [a.to_dict() for a in self.attachments],
            "status": self.status, "supersedes_id": self.supersedes_id,
            "origin_replacement_id": self.origin_replacement_id,
            "origin_schedule_version_id": self.origin_schedule_version_id,
            "submitted_by": self.submitted_by, "approved_by": self.approved_by,
            "published_by": self.published_by, "withdrawal_reason": self.withdrawal_reason,
            "approval_trace": [t.to_dict() for t in self.approval_trace],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


@dataclass
class Credential:
    """课表下载凭据：仅对已发布且授权仍然有效的版本签发。"""
    id: str
    schedule_version_id: str
    schedule_key: str
    school_id: str
    issued_at: datetime
    expires_at: date
    active: bool = True
    deactivate_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "schedule_version_id": self.schedule_version_id,
            "schedule_key": self.schedule_key, "school_id": self.school_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "active": self.active, "deactivate_reason": self.deactivate_reason,
        }


@dataclass
class Replacement:
    """替换方案：必须引用原课表，并通过时长/难度/适龄验证。"""
    id: str
    school_id: str
    grade: str
    old_schedule_version_id: str
    old_entry_ids: list[str]
    new_combo_version_id: str
    revocation_id: Optional[str]
    submitted_by: str
    created_at: datetime
    status: str = "proposed"           # proposed|submitted|approved|rejected|applied
    venue: str = ""
    venue_limit: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
    reviewed_by: Optional[str] = None
    review_note: str = ""
    new_schedule_version_id: Optional[str] = None
    processed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "school_id": self.school_id, "grade": self.grade,
            "old_schedule_version_id": self.old_schedule_version_id,
            "old_entry_ids": list(self.old_entry_ids),
            "new_combo_version_id": self.new_combo_version_id,
            "revocation_id": self.revocation_id,
            "submitted_by": self.submitted_by,
            "created_at": self.created_at.isoformat(), "status": self.status,
            "venue": self.venue, "venue_limit": self.venue_limit,
            "checks": dict(self.checks), "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
            "new_schedule_version_id": self.new_schedule_version_id,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
        }


@dataclass
class Job:
    """可续办任务：到期检查 / 替换处理，按条目幂等推进并记录游标。"""
    id: str
    type: str                          # expiry_check | replacement_run
    scope: str                         # revocation:<id> | license:<id> | all
    status: str = "pending"            # pending|interrupted|completed
    processed: list[str] = field(default_factory=list)
    affected: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "scope": self.scope, "status": self.status,
            "processed": list(self.processed), "affected": list(self.affected),
            "result": dict(self.result), "attempts": self.attempts,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class Notice:
    """学校最终看到的说明：采用版本、授权依据、受影响日期、撤回原因、新安排去向。"""
    id: str
    school_id: str
    revocation_id: Optional[str]
    schedule_key: str
    adopted_versions: list[dict[str, Any]]
    license_basis: list[dict[str, Any]]
    affected_dates: list[str]
    withdrawal_reason: str
    new_arrangement_ref: dict[str, Any]
    created_at: datetime
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "school_id": self.school_id,
            "revocation_id": self.revocation_id, "schedule_key": self.schedule_key,
            "adopted_versions": list(self.adopted_versions),
            "license_basis": list(self.license_basis),
            "affected_dates": list(self.affected_dates),
            "withdrawal_reason": self.withdrawal_reason,
            "new_arrangement_ref": dict(self.new_arrangement_ref),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
