"""服务装配：仓储、角色目录与各领域服务的组合根。"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import CatalogService
from .jobs import JobService
from .roles import (
    ROLE_MAINTAINER,
    ROLE_PUBLISHER,
    ROLE_REVIEWER,
    RoleDirectory,
)
from .revocation import RevocationService
from .schedule import ScheduleService
from .store import Store


@dataclass
class Services:
    store: Store
    roles: RoleDirectory
    catalog: CatalogService
    schedule: ScheduleService
    revocation: RevocationService
    jobs: JobService


def build_services(*, seed_roles: bool = True) -> Services:
    store = Store()
    roles = RoleDirectory()
    if seed_roles:
        # 三个职责由不同的人承担；演示与测试据此验证"提交人不能放行自己"。
        roles.register("m_mural", {ROLE_MAINTAINER})
        roles.register("r_curriculum", {ROLE_REVIEWER})
        roles.register("p_district", {ROLE_PUBLISHER})
        # 同时持多角色的人：仍不能自审自批（按身份而非角色判定）。
        roles.register("multi_user",
                       {ROLE_MAINTAINER, ROLE_REVIEWER, ROLE_PUBLISHER})
    catalog = CatalogService(store)
    schedule = ScheduleService(store, catalog, roles)
    revocation = RevocationService(store, catalog, roles)
    jobs = JobService(store, revocation)
    return Services(store=store, roles=roles, catalog=catalog,
                    schedule=schedule, revocation=revocation, jobs=jobs)
