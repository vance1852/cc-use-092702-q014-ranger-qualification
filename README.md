# 建立巡护人员资格事件账本基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- src/qualification_ledger/：巡护人员不可变资格事件账本、时点资格投影、关键动作准入裁决与解释；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

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
PYTHONPATH=src python3 -m qualification_ledger.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析与风险处置，不访问外部网络。

## 资格事件账本

`qualification_ledger` 在账号只有角色与停用状态的基础上增加只追加的资格事件账本：

- 事件类型：培训通过（training_passed）、体检合格（medical_cleared）、装备授权（equipment_authorized）、违规扣分（violation_points）、临时停权（suspension）、复核（review）与恢复（reinstatement）。
- 不可变性：`qualification_events` 表由触发器禁止 UPDATE/DELETE，并以 previous_hash/event_hash 串成哈希链；`verify_chain` 可检测任何篡改。
- 修订只影响未来：修订授权只能新增 `amends` 事件且生效时刻不得早于记账时刻，历史时刻投影（含历史排班事实）永不被回写。
- 时点投影：`projection.project/evaluate` 是纯函数，只采用 `recorded_at <= 业务时刻` 的事件；规则集按版本管理（rules-2026.1），结论按业务时刻当时适用的规则版本推导。
- 关键动作核对：任务领取、样本复核、风险处置和资源调拨经 `authorize_action` 在动作发生时刻核对并将裁决快照（含规则版本、支撑事件 ID、缺口原因）落账；同一事件重放返回原裁决，扣分与恢复不重复生效。
- 解释接口：`GET /people/{id}/{action}/explain?as_of=...` 返回每个资格项的状态（satisfied/missing/expired）、支撑事件与规则版本。
- 接入方式：三个业务服务构造时可注入 `QualificationGate`（如 `LedgerGate`）；不注入时使用 `PermissiveGate`，既有行为完全不变。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m qualification_ledger.api --database qualification.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。
