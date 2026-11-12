# temporal-bridge

跨院系联合负责课题的**带有效期多对多桥接**集市。一个院系字段无法表达项目归属，
本项目用版本化桥接表描述「项目—院系—有效起止—精确权重」，事实按发生时刻选版、
按约定比例守恒分摊；全校总量始终只算一次，不固化重复展开的宽表。

## 数据模型（`schema.json` / `warehouse/definition.py`）

来源层：

- `projects`：`project TEXT PRIMARY KEY, department TEXT NOT NULL`（遗留单归属对照）
- `work`：`record TEXT PRIMARY KEY, project TEXT NOT NULL, day TEXT NOT NULL, hours TEXT NOT NULL`

桥接层（归一化，版本化）：

- `bridge_versions`：版本头，含 `valid_from`、半开 `valid_to`（NULL 表示开放）、
  `status`（draft/published/rejected）、内容摘要 `digest`、发布时刻 `published_at`
- `bridge_members`：版本内的 `(项目, 院系, 精确权重)`，允许 **0 权重**（保留关系、不贡献）
- `bridge_supersessions`：取代审计，记录被追补版本的原始区间、取代时刻与替代版本，
  支撑发布前口径的 `as-of` 历史复算
- 视图 `v_bridge_exact`：只做精确权重的关系解释，不产生汇总、不落盘展开

### 发布治理约束（`warehouse/bridge.py`）

- 权重为精确 `Decimal`：禁止负数；发布版必须**精确合计为 1**，缺口与超配都拒绝发布
- 同一项目的已发布版本有效期为半开区间 `[valid_from, valid_to)`，不得在同一时点并存
- 追补比例时，新版本在同一事务内原子取代重叠旧版（旧版收口并写取代审计）；
  会制造覆盖空洞或并存超配的方案直接拒绝，整事务回滚，可原样重试
- 同内容重发幂等（按规范化摘要），失败不留半成品
- 草稿（`bridge-submit`）不参与选版，可用于 `bridge-preview` 无副作用预览影响项目与日期

### 选版与分摊

- 事实按**发生日 `day`** 选择当时有效且已发布的版本，与导入/发布日期无关；
  无方案的事实保留在**未分配组**（`department = null`）
- 分摊金额按记账单位 `0.01` 量化：先按权重算精确份额与地板值，再用**最大余数法**
  稳定分配尾差（平手按院系标识排序），单事实分摊金额之和恒等于其量化工作量
- 双时态：`--as-of <UTC ISO 时间>` 只可见该时刻前发布的版本，并据取代审计恢复
  历史区间，因此发布后院系与学校总览同时切换，而历史版仍可复查

## 集市指标语义（`warehouse/mart.py`）

三类指标不能用同一种求和：

| 指标 | 类型 | 上卷规则 |
| --- | --- | --- |
| 工作量 `hours` | 可加 | 院系/项目/时间层级均可求和；分摊金额之和 = 学校总量（守恒） |
| 项目数 `project_count` | 去重 | 上卷必须 `COUNT DISTINCT`，禁止把各院系项目数相加（联合负责会重复） |
| 参与院系数 `participating_departments` | 去重 | 同理去重；零权重关系不产生贡献，不计入参与院系 |

未分配是独立分组：计入学校总量与未分配工作量，但不属于任何院系。
`mart-school` 显式返回 `conservation_ok` 与逐事实守恒校验结果。

## 命令与运行方式

Python 3.11 以上，仅标准库。

```bash
python3 -m compileall -q warehouse tests scripts
python3 -m unittest discover -s tests -v          # 47 个测试
python3 scripts/demo.py                           # 遗留单归属流程
python3 scripts/demo_bridge.py                    # 桥接全生命周期演示
```

分步 CLI（数据库与对象目录均由参数指定）：

```bash
# 1) 事实导入（内容寻址批次，重复导入不重复写）
python3 -m warehouse import --db /tmp/tb.sqlite3 --input examples/source.json --batch facts

# 2) 桥接治理：预览 / 草稿 / 发布（--published-at 可固定发布时刻以便复算）
python3 -m warehouse bridge-preview  --db /tmp/tb.sqlite3 --bridge examples/bridge-initial.json
python3 -m warehouse bridge-submit   --db /tmp/tb.sqlite3 --bridge examples/bridge-backfill.json
python3 -m warehouse bridge-publish  --db /tmp/tb.sqlite3 --bridge examples/bridge-initial.json \
  --published-at 2026-10-02T00:00:00+00:00
python3 -m warehouse bridge-expire   --db /tmp/tb.sqlite3 --version p2-v1 --project p2 --day-to 2026-11-01
# 非法方案（负数/缺口/重叠超配/覆盖空洞）以退出码 2 被拒绝，不写半成品
python3 -m warehouse bridge-publish  --db /tmp/tb.sqlite3 --bridge examples/bridge-invalid-gap.json

# 3) 集市查询
python3 -m warehouse mart-school     --db /tmp/tb.sqlite3
python3 -m warehouse mart-department --db /tmp/tb.sqlite3
python3 -m warehouse mart-project    --db /tmp/tb.sqlite3
python3 -m warehouse mart-lines      --db /tmp/tb.sqlite3 --objects /tmp/tb-objects  # 血缘粒度+对象导出

# 4) 复查单条工作量如何分给多个院系（精确份额、权重、尾差、命中版本与选版依据）
python3 -m warehouse explain --db /tmp/tb.sqlite3 --record w1

# 5) 局部重算（补档/重试后只重算指定事实）与历史时点复算
python3 -m warehouse recompute --db /tmp/tb.sqlite3 --record w3 --record w6
python3 -m warehouse mart-school --db /tmp/tb.sqlite3 --as-of 2026-11-05T07:59:59+00:00
```

`explain` 输出示例（`w1 = 10.01`，权重 0.70/0.30）：`d1` 精确份额 `7.0070`、
记账 `7.01`；`d2` 精确份额 `3.0030`、记账 `3.00`；尾差 `0.01` 按最大余数归 `d1`，
合计严格等于 `10.01`。

## 为什么不建展开宽表

直接把事实 join 到多条归属会让汇总随院系数膨胀。本方案只在**查询期**由
`allocation_lines` 按需展开「事实 × 院系」血缘行，上层三个集市均从其上卷；
比例变更、未分配补档或失败重试后通过 `recompute` 局部重算即可，无需重刷固化表。
所有展开行导出到内容寻址对象存储（SHA-256 校验），可独立复查每条金额去向。

## 输入与可信边界

`examples/` 为本地合成的虚构课题与院系数据，不含真实人员信息；
来源批次以内容 SHA-256 固定，相同批次重投不重复写入、内容不同则报错，
装载以完整输入包为事务，主键冲突不留下半批记录。桥接表只能经治理接口写入，
不能通过通用导入直接装载。
