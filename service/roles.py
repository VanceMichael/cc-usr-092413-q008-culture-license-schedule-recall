"""角色目录与职责分离。

三类角色：素材维护者、课程审核人、发布人。
关键规则：提交替换的人不能为自己放行——按用户身份比对，而非角色比对，
因此同一人即使同时拥有多个角色也不能自审自批。
"""

from __future__ import annotations

from .errors import SegregationError

ROLE_MAINTAINER = "maintainer"   # 素材维护者
ROLE_REVIEWER = "reviewer"       # 课程审核人
ROLE_PUBLISHER = "publisher"     # 发布人

ALL_ROLES = {ROLE_MAINTAINER, ROLE_REVIEWER, ROLE_PUBLISHER}


class RoleDirectory:
    def __init__(self) -> None:
        self._users: dict[str, set[str]] = {}

    def register(self, user_id: str, roles: set[str] | list[str]) -> None:
        roles = set(roles)
        unknown = roles - ALL_ROLES
        if unknown:
            raise ValueError(f"未知角色: {sorted(unknown)}")
        self._users[user_id] = set(roles)

    def roles_of(self, user_id: str) -> set[str]:
        if user_id not in self._users:
            raise SegregationError(f"用户未登记角色: {user_id}")
        return self._users[user_id]

    def require_role(self, user_id: str, role: str) -> None:
        if role not in self.roles_of(user_id):
            raise SegregationError(
                f"用户 {user_id} 缺少角色 {role}，各角色只守自己的职责")

    def require_not_same_person(self, actor_id: str, submitter_id: str,
                                action: str) -> None:
        if actor_id == submitter_id:
            raise SegregationError(
                f"提交替换的人不能为自己放行（{action}）：{actor_id}")
