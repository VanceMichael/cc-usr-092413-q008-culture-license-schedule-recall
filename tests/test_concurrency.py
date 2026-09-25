"""撤销通知与批量发布并发到达：必须串行化、失效不入版、凭据必拦。"""
import threading
from datetime import date

import pytest

from service.errors import LicenseCoverageError
from tests.test_scenarios import _build_world, _schedule

D = date.fromisoformat
MAINTAINER = "m_mural"
PUBLISHER = "p_district"


def test_revocation_racing_batch_publication(svc):
    _build_world(svc)
    entries = [
        _schedule(svc, f"S{i}", "五年级", 10,
                  [D("2026-10-16"), D("2026-10-20")])
        for i in (1, 2, 3)
    ]
    entry_ids = [e.entry_id for e in entries]
    outcomes: list[str] = []
    barrier = threading.Barrier(len(entry_ids) + 1)

    def publish(eid):
        barrier.wait()
        try:
            svc.schedule.publish_batch(
                entry_ids=[eid],
                attachments=[{"name": "安全须知", "ref": "refs://s.pdf"}],
                published_by=PUBLISHER)
            outcomes.append("published")
        except LicenseCoverageError:
            outcomes.append("rejected")
        except Exception as exc:  # 不允许出现第三种结果（如脏写/500）
            outcomes.append(f"error:{type(exc).__name__}")

    threads = [threading.Thread(target=publish, args=(eid,))
               for eid in entry_ids]
    for t in threads:
        t.start()
    barrier.wait()  # 与发布线程同时闯入
    svc.revocation.register_notice(
        notice_id="N1", license_id="L1", effective_date=D("2026-10-10"),
        reason="权利方提前终止校园授权", detail="并发到达",
        created_by=MAINTAINER)
    svc.revocation.analyze_impact("N1")
    for t in threads:
        t.join()

    assert outcomes, "应有发布尝试完成"
    assert all(o in ("published", "rejected") for o in outcomes), outcomes

    # 关键不变量：撤销完成后，任何已生成版本里的失效安排都不能再取下载凭据。
    for eid in entry_ids:
        with pytest.raises(Exception):
            svc.schedule.issue_credential(
                eid, D("2026-10-16"), issued_by=PUBLISHER)

    # 每个落地版本都必须整版完整：主课表与附件同版存在。
    for versions in svc.store.docs.values():
        for doc in versions:
            assert doc.main_ref
            assert doc.attachments and doc.attachments[0].name == "安全须知"

    # 版本号连续无重号（串行化写入的证据）。
    for school_id in ("S1", "S2", "S3"):
        docs = [v for vs in svc.store.docs.values() for v in vs
                if v.school_id == school_id]
        versions = sorted(v.version for v in docs)
        assert versions == list(range(1, len(versions) + 1))
