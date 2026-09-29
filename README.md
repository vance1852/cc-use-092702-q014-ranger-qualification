# 建立巡护人员资格事件账本基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/qualification_ledger/：不可变的巡护人员资格事件账本与业务时刻资格投影，被以上三个服务共享；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 资格事件账本

森林消防、野生动物救护和高海拔夜巡对培训、体检与装备授权的要求不同。账号角色之外，
平台在只追加（append-only）的 `qual_events` 账本中记录七类不可变事件：
培训通过 `training_passed`、体检通过 `medical_passed`、装备授权
`equipment_authorized`、违规扣分 `demerit`、临时停权 `temporary_suspension`、
复核 `review`、恢复 `reinstatement`。

- 不可变：事件表带 SHA-256 哈希链，并由触发器拒绝任何 UPDATE/DELETE；
- 双时间：`recorded_at` 是登记时刻，`valid_from`/`valid_until` 是业务有效期；
  晚于查询时刻生效的事件不参与投影，因此后续修订只影响未来，不会回写历史排班事实；
- 规则版本：投影按业务时刻选择 `effective_from` 已生效的最新规则版本
  （如 `rules-2026-09-01` 与未来的 `rules-2027-01-01`），历史时刻的结论不因
  规则修订而改变；
- 幂等：同一 `(user_id, idempotency_key)` 重放返回原事件，扣分不重复累计，
  同一停权事件只能恢复一次；
- 可注入时钟：账本与三个服务都接受 `SystemClock`/`FrozenClock`，按任意业务时刻投影。

资格投影结论（`state`：granted / missing / expired / suspended /
demerit_suspended）会解释由哪些有效授权事件、扣分/复核事件以及哪个规则版本推导而来。

关键动作在发生时核对资格，核对结论也作为不可变事实落入 `qual_action_checks`：

| 业务动作 | 服务入口 | 所需资格 |
| --- | --- | --- |
| 任务领取 | `taxonomy_lab` 分析任务 `claim_job`（按协议任务族） | 样本复核 |
| 样本复核 | `taxonomy_lab` 排除申请 `review_exclusion` | 样本复核 |
| 风险处置 | `biosafety_ops` 派单与开始处置工单 | 森林消防 |
| 资源调拨 | `biosafety_ops` 调拨、`collection_logistics` 资源到场（按资源类型） | 森林消防/野生动物救护/样本复核 |

资格事件登记与投影查询接口（三个服务均提供）：

- `POST /qualification_events`：追加资格事件（需资格管理角色）；
- `GET /qualifications/{user_id}?competency=...&as_of=...`：某业务时刻的资格投影与解释；
- `GET /qualification_events/{user_id}`：该用户的不可变事件流水；
- `GET /qualifications/chain`：哈希链完整性校验。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
~~~

三条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析与风险处置，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。
