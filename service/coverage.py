"""授权覆盖判定（纯函数）。

排课与撤回影响分析共用同一套规则：
对每个使用日、每个实际引用的片段，都必须存在一条生效的授权版本，
其允许用途、地区、年龄范围同时命中。授权链上的终止版本自终止日起切断覆盖。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .errors import LicenseCoverageError
from .models import KIND_TERMINATION, LicenseBasisSegment, LicenseVersion
from .store import Store


def applicable_version(chain: list[LicenseVersion],
                       d: date) -> Optional[LicenseVersion]:
    """某授权链在日期 d 上适用的版本；终止日（含）之后返回 None。"""
    term = next((v for v in chain if v.kind == KIND_TERMINATION), None)
    if term is not None and d >= term.valid_from:
        return None
    active = [v for v in chain if v.kind != KIND_TERMINATION and v.valid_from <= d]
    if not active:
        return None
    v = active[-1]
    if v.valid_to is not None and d > v.valid_to:
        return None
    return v


def version_covers(lv: LicenseVersion, *, use_code: str, region: str,
                   student_age: int, d: date) -> bool:
    if use_code not in lv.allowed_uses:
        return False
    if "*" not in lv.regions and region not in lv.regions:
        return False
    if not (lv.age_min <= student_age <= lv.age_max):
        return False
    if d < lv.valid_from:
        return False
    if lv.valid_to is not None and d > lv.valid_to:
        return False
    return True


def evaluate(store: Store, clip_ids: list[str], *, use_code: str,
             region: str, student_age: int, dates: list[date]
             ) -> tuple[dict[date, dict[str, LicenseVersion]], list[str]]:
    """逐日逐片段判定。返回 (命中版本表, 未覆盖原因列表)。"""
    hit: dict[date, dict[str, LicenseVersion]] = {}
    problems: list[str] = []
    # 每个片段可能挂在多条授权链上，取链当日适用版本来判定，命中任一即可。
    chains_by_clip = {
        cid: {lv.license_id: store.get_license_versions(lv.license_id)
              for lv in store.clip_licenses.get(cid, [])}
        for cid in clip_ids
    }
    for d in dates:
        hit[d] = {}
        for cid in clip_ids:
            chosen: Optional[LicenseVersion] = None
            for chain in chains_by_clip.get(cid, {}).values():
                av = applicable_version(chain, d)
                if av is None or cid not in av.clip_ids:
                    continue
                if version_covers(av, use_code=use_code, region=region,
                                  student_age=student_age, d=d):
                    chosen = av
                    break
            if chosen is None:
                problems.append(f"{d} 片段 {cid} 无有效授权覆盖")
            else:
                hit[d][cid] = chosen
    return hit, problems


def require_full_coverage(store: Store, clip_ids: list[str], *, use_code: str,
                          region: str, student_age: int,
                          dates: list[date]) -> list[LicenseBasisSegment]:
    """排课时校验授权覆盖整个使用窗口，通过则返回可锁定的授权依据分段。"""
    if not dates:
        raise LicenseCoverageError("使用窗口不能为空")
    hit, problems = evaluate(store, clip_ids, use_code=use_code,
                             region=region, student_age=student_age, dates=dates)
    if problems:
        raise LicenseCoverageError("授权未覆盖整个使用窗口: " + "; ".join(problems))
    return build_basis_segments(hit, dates)


def uncovered_dates(store: Store, clip_ids: list[str], *, use_code: str,
                    region: str, student_age: int,
                    dates: list[date]) -> list[date]:
    """撤销后复核：返回当前失去授权覆盖的日期（保持入参顺序去重）。"""
    hit, _ = evaluate(store, clip_ids, use_code=use_code, region=region,
                      student_age=student_age, dates=dates)
    result: list[date] = []
    for d in dates:
        if any(cid not in hit[d] for cid in clip_ids) and d not in result:
            result.append(d)
    return result


def build_basis_segments(hit: dict[date, dict[str, LicenseVersion]],
                         dates: list[date]) -> list[LicenseBasisSegment]:
    """把逐日命中结果压缩成 (片段, 授权, 版本, 起止) 连续分段，便于追溯。"""
    segments: list[LicenseBasisSegment] = []
    for cid in {c for d in hit.values() for c in d}:
        current: Optional[LicenseBasisSegment] = None
        prev_date: Optional[date] = None
        for d in dates:
            lv = hit.get(d, {}).get(cid)
            if lv is None:
                current = None
                prev_date = d
                continue
            if (current is not None and current.license_id == lv.license_id
                    and current.version == lv.version
                    and prev_date is not None):
                current.date_to = d
            else:
                current = LicenseBasisSegment(
                    clip_id=cid, license_id=lv.license_id, version=lv.version,
                    date_from=d, date_to=d)
                segments.append(current)
            prev_date = d
    return segments
