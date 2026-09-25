"""文化素材授权换版与课表撤回 —— 领域模型。

所有模型均为不可变/追加式语义的载体：授权版本一经写入不再修改，
课表的状态变化通过状态字段与关联单据留痕，保证每个结论都可追溯。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Optional


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


class Model:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- 授权版本 ----------------------------------------------------------------

KIND_GRANT = "grant"          # 首次授权
KIND_REVISION = "revision"    # 收窄/变更后的新版本
KIND_TERMINATION = "termination"  # 提前终止


@dataclass
class LicenseVersion(Model):
    """一条授权版本：权利方、允许用途、地区、年龄范围、生效区间、证据摘要。"""
    license_id: str
    version: int
    kind: str
    rights_holder: str
    allowed_uses: list[str]
    regions: list[str]                 # ["*"] 表示不限地区
    clip_ids: list[str]                # 本版本覆盖的素材片段
    age_min: int
    age_max: int
    valid_from: date
    valid_to: Optional[date]
    evidence_summary: str
    evidence_ref: str
    supersedes: Optional[int]
    created_by: str
    created_at: datetime
    notice_id: Optional[str] = None    # 由撤销通知产生时留痕


# ---- 素材目录 ----------------------------------------------------------------

@dataclass
class Clip(Model):
    """壁画动作参考片段（授权标的）。"""
    clip_id: str
    title: str
    duration_sec: int
    source_ref: str
    created_by: str
    created_at: datetime


@dataclass
class ClipRef(Model):
    """对片段的实际引用：标明引用了哪个片段以及使用的时间区间。"""
    clip_id: str
    start_sec: int
    end_sec: int


@dataclass
class UnitVersion(Model):
    """动作单元的一个版本，必须标明实际引用的片段。"""
    unit_id: str
    version: int
    name: str
    clip_refs: list[ClipRef]
    duration_sec: int
    difficulty: int                    # 1-5
    age_min: int
    age_max: int
    supersedes: Optional[int]
    created_by: str
    created_at: datetime


@dataclass
class UnitMember(Model):
    """组合对某个动作单元【确定版本】的引用。"""
    unit_id: str
    version: int


@dataclass
class ComboVersion(Model):
    """动作组合的一个版本；实际引用片段在创建时解析并冻结。"""
    combo_id: str
    version: int
    name: str
    members: list[UnitMember]
    resolved_clip_ids: list[str]       # 传递闭包：成员单元引用到的全部片段
    duration_sec: int
    difficulty: int
    age_min: int
    age_max: int
    supersedes: Optional[int]
    created_by: str
    created_at: datetime


# ---- 学校与排课 --------------------------------------------------------------

@dataclass
class School(Model):
    school_id: str
    name: str
    region: str


@dataclass
class LicenseBasisSegment(Model):
    """排课当时锁定的授权依据：某片段在一段日期内适用的授权版本。"""
    clip_id: str
    license_id: str
    version: int
    date_from: date
    date_to: date


# 课表状态
ENTRY_SCHEDULED = "scheduled"        # 已排课未执行
ENTRY_EXECUTED = "executed"          # 已完成
ENTRY_WITHDRAWN = "withdrawn"        # 整个安排撤回
ENTRY_REPLACED = "replaced"          # 已被替换单接管


@dataclass
class ScheduleEntry(Model):
    entry_id: str
    school_id: str
    grade: str
    dates: list[date]
    venue_constraint: str
    student_age: int
    use_code: str
    content_type: str                  # "unit" | "combo"
    content_id: str
    content_version: int
    resolved_clip_ids: list[str]
    required_level: int
    granted_level: int
    approval_policy_version: str
    scheduled_by: str
    created_at: datetime
    license_basis: list[LicenseBasisSegment]
    status: str = ENTRY_SCHEDULED
    executed_dates: list[date] = field(default_factory=list)
    withdrawn_dates: list[date] = field(default_factory=list)
    batch_id: Optional[str] = None
    replaced_by_entry_id: Optional[str] = None
    replaces_entry_id: Optional[str] = None
    withdrawal_reason: Optional[str] = None
    notice_id: Optional[str] = None


# ---- 课表文档（主课表 + 附件）------------------------------------------------

@dataclass
class Attachment(Model):
    name: str
    ref: str


@dataclass
class ScheduleDocVersion(Model):
    """主课表与附件在同一次换版中一起生成，要么全部成功要么不存在。"""
    doc_id: str
    version: int
    school_id: str
    batch_id: str
    entry_ids: list[str]
    main_ref: str
    attachments: list[Attachment]
    published_by: str
    published_at: datetime
    supersedes: Optional[int] = None


# ---- 撤销通知 ----------------------------------------------------------------

@dataclass
class RevocationNotice(Model):
    notice_id: str
    license_id: str
    effective_date: date
    reason: str
    detail: str
    created_by: str
    created_at: datetime


# ---- 替换单 ------------------------------------------------------------------

REPL_SUBMITTED = "submitted"
REPL_APPROVED = "approved"
REPL_REJECTED = "rejected"
REPL_PUBLISHED = "published"


@dataclass
class ReplacementChecks(Model):
    old_duration_sec: int
    new_duration_sec: int
    duration_diff_pct: float
    old_difficulty: int
    new_difficulty: int
    age_min: int
    age_max: int
    new_age_min: int
    new_age_max: int


@dataclass
class Replacement(Model):
    replacement_id: str
    original_entry_id: str
    school_id: str
    grade: str
    dates: list[date]
    venue_constraint: str
    content_type: str
    content_id: str
    content_version: int
    reason: str
    notice_id: str
    checks: ReplacementChecks
    status: str
    submitted_by: str
    submitted_at: datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_note: str = ""
    published_by: Optional[str] = None
    published_at: Optional[datetime] = None
    new_entry_id: Optional[str] = None
    new_doc_version: Optional[int] = None


# ---- 下载凭据 ----------------------------------------------------------------

@dataclass
class DownloadCredential(Model):
    token: str
    entry_id: str
    date: date
    issued_by: str
    issued_at: datetime
    expires_at: datetime


# ---- 可续办任务 --------------------------------------------------------------

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_INTERRUPTED = "interrupted"
JOB_DONE = "done"


@dataclass
class Job(Model):
    job_id: str
    job_type: str                       # "expiry_scan" | "replacement_process"
    params: dict[str, Any]
    status: str
    total: int
    processed_keys: list[str]
    findings: list[dict[str, Any]] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    attempts: int = 0
    last_error: str = ""


# ---- 学校告知 ----------------------------------------------------------------

@dataclass
class SchoolNotice(Model):
    """学校最终看到的说明：采用版本、授权依据、受影响日期、撤回原因、新安排去向。"""
    notice_id: str
    school_id: str
    entry_id: str
    source_notice_id: str
    affected_dates: list[date]
    withdrawal_reason: str
    license_basis: list[LicenseBasisSegment]
    adopted_content: Optional[str]      # 采用的动作单元/组合及版本
    new_entry_id: Optional[str]
    new_doc_version: Optional[int]
    new_arrangement: str               # 新安排去向（文字）
    status: str                        # "pending_arrangement" | "arranged"
    created_at: datetime
    updated_at: datetime
