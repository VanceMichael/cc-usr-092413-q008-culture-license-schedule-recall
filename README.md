# 文化素材授权换版与课表撤回

服务管理文化素材授权版本、动作单元/组合的片段引用与年级课间操课表，支持：
授权提前终止时的**精准影响面**分析、可追溯的替换审批、主课表与附件的**原子换版**、
下载凭据门控，以及中断后可续办的到期检查/替换处理任务。

## 模块

- `domain.py` — 不可变版本化领域记录：授权版本、撤销通知、素材片段、动作单元版本、
  组合版本、课表版本（含附件、授权依据快照、审批水位轨迹）、替换方案、任务、学校通告。
- `store.py` — 带全局锁的内存存储与授权链/课表版本指针查询（可替换为数据库实现）。
- `service.py` — 全部业务规则。
- `app.py` — Flask HTTP 入口（`/api/v1/...`，身份经 `X-User-Id` 头传递）。

## 关键规则

1. **授权版本可追溯**：登记权利方、允许用途、地区、年龄范围、生效区间、证据摘要/编号；
   收窄或续期一律追加新版本（`supersedes_id` 指向旧版），旧版本与证据不修改。
2. **片段引用显式化**：动作单元版本必须标明实际引用的素材片段，组合版本传递依赖；
   引用不存在的片段拒绝登记。
3. **排课即校验全窗口**：逐条安排（组合 × 使用日）校验授权在当日覆盖
   用途/地区/年龄/生效区间，并快照每个片段当日的授权版本与审批水位
   （1 排课 → 2 审核 → 3 发布），同时留存学校、年级、日期、场地与场地限制。
4. **收窄后精准撤回**：只处理**尚未执行且确实依赖**失效授权版本的安排；
   已完成课程保留原依据，覆盖窗口内的安排不动，不依赖该片段的课表不撤回（不撤回全部课表）。
5. **替换方案**：必须引用原课表；验证时长（±10%）、难度（不高于原安排）和适龄/授权条件。
6. **职责分离**：素材维护者、课程审核人、发布人、区域教研员各守其责；
   提交排课或替换的人不能为自己放行。
7. **原子换版**：主课表与附件在同一锁关键区内一次换版成功——新版本发布、
   旧版本落 `replaced`、当前版本指针切换、旧凭据失活，要么全部成功要么不变。
8. **凭据门控**：签发瞬间重新核验授权；撤销与批量发布同时到达时整批中止，
   失效课表不得生成下载凭据。
9. **可续办任务**：到期检查与替换处理按条目幂等推进，`limit` 模拟中断，
   再次运行从游标续办，已处理条目不重复。
10. **学校通告五要素**：采用版本、授权依据（权利方+证据）、受影响日期、
    撤回原因、新安排去向。

## 主要接口

| 方法 | 路径 | 角色 |
|---|---|---|
| POST | `/api/v1/licenses/<key>/versions` | 素材维护者 |
| POST | `/api/v1/revocations` | 素材维护者 |
| POST | `/api/v1/action-units/<key>/versions` | 素材维护者 |
| POST | `/api/v1/combos/<key>/versions` | 素材维护者 |
| POST | `/api/v1/schedules/<key>/versions` | 区域教研员 |
| POST | `/api/v1/schedules/versions/<id>/approve` | 课程审核人 |
| POST | `/api/v1/schedules/versions/<id>/publish`、`/schedules/batch-publish` | 发布人 |
| POST | `/api/v1/schedules/versions/<id>/credentials` | 任意（按状态与授权门控） |
| POST | `/api/v1/jobs/expiry-check`、`/api/v1/jobs/<id>/run?limit=N` | 区域教研员 |
| POST | `/api/v1/replacements` | 区域教研员/维护者 |
| POST | `/api/v1/replacements/<id>/review` | 课程审核人（不可为提交者本人） |
| POST | `/api/v1/replacements/<id>/apply`、`/api/v1/jobs/replacement-run` | 发布人 |
| GET | `/api/v1/schools/<id>/notices` | 任意登录用户 |

## 开发命令

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest -q tests`
- 编译检查：`python3 -m compileall -q app.py domain.py store.py service.py tests`
- 启动服务：`python3 app.py`

测试和构建只使用仓库内数据，不需要连接外部业务服务。
