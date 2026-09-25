# 文化素材授权换版与课表撤回

管理文化素材（壁画动作参考等）的校园授权、动作单元/组合与年级课表，
处理授权提前终止时的影响分析、替换换版与学校告知。

## 领域模型

- **授权版本**（追加式版本链）：权利方、允许用途、地区、年龄范围、生效区间、
  证据摘要与证据引用。收窄走"换版"（旧版本保留），提前终止走"撤销通知"，
  在链上追加终止版本，自终止日起切断覆盖判定。
- **片段 / 动作单元 / 组合**：单元与组合版本必须标明实际引用的片段
  （含使用区间）；组合创建时把成员单元的引用片段解析冻结为传递闭包。
- **课表条目**：学校、年级、日期、场地限制、参训年龄、用途、审批水位
  （要求/实批）与排课当时锁定的授权依据分段。
- **课表文档版本**：主课表与全部附件同属一个版本对象，一次写入整版生效。
- **替换单**：引用原课表，记录时长/难度/适龄校验结果与审核、发布留痕。
- **可续办任务**：到期检查、替换处理，按业务键逐条处理，中断后续办。
- **学校告知**：采用版本、授权依据、受影响日期、撤回原因、新安排去向。

## 核心规则

- 排课时校验授权覆盖整个使用窗口（逐日逐片段复核用途/地区/年龄/生效区间）。
- 授权收窄后只处理尚未执行且确实依赖该片段的安排；已完成课程保留原依据。
- 替换方案重新验证授权覆盖、时长（±10%）、难度（±1）、适龄条件。
- 素材维护者、课程审核人、发布人各守其责；提交替换的人不能为自己放行
  （按用户身份判定，同一人持有全部角色也不能自审自批）。
- 撤销与批量发布共用一把事务锁，锁内复核授权：失效课表不进入新课表，
  已签发的下载凭据在下载时再次复核、立即失效。
- 到期检查/替换处理中断后可续办，已处理单元幂等不重复。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/schools` | 登记学校（含地区） |
| POST | `/api/v1/clips` | 登记素材片段 |
| POST | `/api/v1/licenses` | 首次授权（版本 1） |
| POST | `/api/v1/licenses/{id}/revisions` | 授权收窄/变更（追加版本） |
| GET | `/api/v1/licenses/{id}` | 授权版本链（可追溯） |
| POST | `/api/v1/units` / `/api/v1/combos` | 动作单元/组合版本 |
| POST | `/api/v1/schedule-entries` | 排课（校验覆盖窗口与审批水位） |
| POST | `/api/v1/schedule-entries/{id}/execute` | 登记执行 |
| POST | `/api/v1/batch-publications` | 批量发布（原子换版） |
| POST | `/api/v1/schedule-entries/{id}/credentials` | 签发下载凭据 |
| GET | `/api/v1/downloads/{token}` | 凭据下载（再复核） |
| POST | `/api/v1/revocation-notices` | 登记撤销通知（幂等） |
| POST | `/api/v1/revocation-notices/{id}/impact` | 影响分析（幂等） |
| POST | `/api/v1/replacements` | 提交替换方案 |
| POST | `/api/v1/replacements/{id}/review` | 审核（非提交人） |
| POST | `/api/v1/replacements/{id}/publish` | 发布（非提交人，原子换版） |
| POST | `/api/v1/jobs/expiry-scan` | 建到期检查任务 |
| POST | `/api/v1/jobs/replacement-process` | 建替换处理任务 |
| POST | `/api/v1/jobs/{id}/run` / `resume` | 运行/续办 |
| GET | `/api/v1/schools/{id}/notices` | 某校最终看到的说明 |

错误统一为 `{"error": code, "message": ...}`：
`license_not_covered` 422、`segregation_violation` 403、
`invalid_state`/`conflict` 409、`not_found` 404。

## 开发命令

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest -q tests/`
- 编译检查：`python3 -m compileall -q app.py service tests`
- 启动服务：`python3 app.py`

测试和构建只使用仓库内数据，不需要连接外部业务服务。
