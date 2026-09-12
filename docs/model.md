# 数据模型详解：字段分组 × 表间关系

> 本文档是 tableseed 两大核心模型的完整定义与设计推演。
> 快速上手请看 [README](../README.md)；背景与需求见 [PRD.md](PRD.md)。

## 模型一：字段分组（Field Group）

描述**单表**的造数规则，三层结构：

```
表（Table）  ──►  字段组（Field Group）  ──►  取值（Value）
```

### 1.1 分组是完备划分

一张表的字段被划分为若干**互不相交的组**：

- 每个字段**恰好属于一个组**，不重不漏（`check` 命令强制校验）；
- 一个组**可以包含多个字段**；
- **组是造数的最小规则单元** —— 造数时以组为单位决定取值，而不是以字段为单位。

组内多字段一次生成一个**取值元组**，因此组内字段天然同步，不可能出现「码值变了描述没变」：

```yaml
- type: enum
  name: g_status
  fields: [status_code, status_desc]      # 同一组的两个字段
  values:
    - ["01", "正常"]                       # 一个取值 = 多个字段的联合取值
    - ["02", "冻结"]
    - ["03", "销户"]
```

> `status_code = "02"` 时，`status_desc` 必然是「冻结」。同步关系由分组模型保证，不依赖人工维护。

### 1.2 组类型

九种组类型，按**参与生成的方式**分为三类：

| 类型 | 语义 | 参与笛卡尔积 | 阶段 | 典型用例 |
| --- | --- | :---: | :---: | --- |
| `enum` 枚举组 | 显式列举有限取值集合（元组列表） | ✅ | P1 | 状态、币种、渠道、产品码 |
| `boundary` 边界组 | 枚举的语义化变体，专列边界与异常值 | ✅ | P1 | 金额上限、超长字符串、`NULL` |
| `dict` 字典组 | 从外部字典/码表取有限取值 | ✅ | P1 | 行名行号、地区码、机构码 |
| `sequence` 自增组 | 按起始值/步长推进，支持格式化模板 | ❌ | P1 | 流水号、账号、序号 |
| `random` 随机组 | 按生成器随机取值：区间、正则、加权分布 | ❌ | P1 | 金额、日期、姓名 |
| `const` 不变组 | 全批数据取同一常量元组 | ❌ | P1 | 租户号、机构号、环境标识 |
| `derive` 派生组 | 由**同表**其他字段表达式计算得出 | ❌ | P1 | 手续费 = 金额 × 费率 |
| `ref` 引用组 | 取自其他表已生成的值（外键联动） | ❌ | P1 | 主表业务键 |
| `aggregate` 汇总组 | 由**子表**汇总回填到父表 | ❌ | **P2** | 笔数 = count(子行)、余额 = Σ 流水 |

**三类之分**（理解生成语义的关键）：

- **有限取值组**（`enum` / `boundary` / `dict`）—— 有确定的取值集合，参与笛卡尔积展开；
- **每行附着组**（`sequence` / `random` / `const` / `derive` / `ref`）—— 没有固定取值集，在每条已展开的行上现算，**不放大行数**；
- **跨表汇总组**（`aggregate`）—— 依赖子表结果，必须等到 Phase 2 回填；单独成类是为了让调度器能自动排期。

### 1.3 生成语义

一条数据 = **所有组取值的组合**。两类组以不同方式参与：

**① 有限取值组之间 —— 笛卡尔积（全组合）**

配置里所有有限取值组的取值集合做笛卡尔积，**每个组合生成一条数据**，从而保证组合覆盖是完备的、可计算的、可证明的。

**② 其余组 —— 在已展开的行上"附着"生成**

自增组按行推进一次，随机组按行采样一次，不变组填充常量，派生组/引用组按依赖顺序计算。它们**不放大**行数，只决定这些行上其余字段取什么值。

```
                     ┌──────── 组（字段） ────────┐
 行数 =  Π  |有限取值组取值集|      ×      基数倍数
         └ 笛卡尔积 ┘                    └ 来自父表的 1:N 关系，可选
```

**展开示例** —— 一张账户表，字段分成 3 个枚举组 + 若干非枚举组：

| 字段 | 所属组 | 组类型 | 取值集合 |
| --- | --- | --- | --- |
| `status_code`, `status_desc` | `g_status` | enum | 正常 / 冻结 —— **2** |
| `currency` | `g_currency` | enum | CNY / USD —— **2** |
| `channel` | `g_channel` | enum | 柜面 / 网银 / 手机银行 —— **3** |

→ 笛卡尔积 `2 × 2 × 3 = 12` 条数据，且 12 种组合一条不漏：

```
(01,正常) (CNY) (柜面)      (02,冻结) (CNY) (柜面)
(01,正常) (CNY) (网银)      (02,冻结) (CNY) (网银)
(01,正常) (CNY) (手机银行)   (02,冻结) (CNY) (手机银行)
(01,正常) (USD) (柜面)      (02,冻结) (USD) (柜面)
(01,正常) (USD) (网银)      (02,冻结) (USD) (网银)
(01,正常) (USD) (手机银行)   (02,冻结) (USD) (手机银行)
```

这 12 条各自的 `acct_no` 由自增组推进、`balance` 由随机组采样、`tenant_id` 由不变组恒定填充、`amount` 由派生组计算。

### 1.4 规模治理

笛卡尔积是有意为之的"覆盖放大器"，但组合数会指数增长，因此必须可控：

| 机制 | 说明 | 状态 |
| --- | --- | :---: |
| `max_rows` | 硬上限，超出即报错而非静默截断 | ✅ |
| `total_rows` | 全部表的**总行数上限**（默认 10 万），超出拒绝生成并给出各表明细 | ✅ |
| `exclude` | 声明非法组合并剔除。例：`status_code == "03" and balance > 0`（已销户不该有余额） | ✅ |
| `strategy: full` | 全组合（默认）。行数 = 组合数，组合覆盖 100% | ✅ |
| `strategy: pairwise` | 两两配对覆盖。组合爆炸时用最少的行数覆盖**所有字段对的取值**（定向锚定贪心，行数 ≈ 组合数的 1/5 ~ 1/10，可复现） | ✅ |
| `strategy: sample` | 从全组合随机抽 `sample_size` 行，行数可控、覆盖不保证 | ✅ |

无论用哪种策略，`tableseed plan` 都会先算出**理论组合总数**并打印出来；`gen` 的结果表带「组合覆盖」列（如 `6/12（50%）`），实际覆盖了多少一目了然 —— **先声明覆盖，再证明覆盖**。

---

## 模型二：表间关系（Relation）

多表造数的难点不在"循环生成"，而在**字段如何对得上**与**行数如何配得齐**。

### 2.1 关系四要素

```yaml
relations:
  - parent: t_txn                 # 父表
    child: t_txn_detail           # 子表
    cardinality: 1:1              # ① 基数
    existence: required           # ② 存在性
    join: [[txn_no, txn_no]]      # ③ 关联锚点
    propagate: [ ... ]            # ④ 字段传播规则
```

**① 基数（cardinality）** 决定行数关系：

| 基数 | 语义 | 子表行数 | 例子 |
| --- | --- | --- | --- |
| `1:1` | 一一对应（主表 + 扩展表） | 等于父行数 | 交易表 + 交易详情表 |
| `1:0..1` | 可选扩展 | ≤ 父行数 | 只有转账交易才有对手行信息 |
| `1:N` | 一对多 | Σ 每个父行的 N | 交易表 + 交易流水明细 |
| `N:M` | 多对多 | 中间表 = 两父表行的组合取样 | 客户 × 产品 |

**② 存在性（existence）** 决定子行是否必然出现：

| 取值 | 语义 |
| --- | --- |
| `required` | 父有行则子必有行（默认） |
| `optional` | 随机决定有无，行数在 0~N 间波动 |
| `conditional` | 满足条件才有，条件表达式可引用父表字段 |

**③ 关联锚点（join）** 定义两表如何配对，同时完成主键/外键传播：

- 主键传播：`[[id, txn_id]]`，子表外键由父表主键派生；
- 业务键关联：`[[txn_no, txn_no]]`，用业务唯一键而非主键；
- 复合键：`[[branch_no, branch_no], [txn_date, txn_date], [seq_no, seq_no]]`。

**④ 字段传播（propagate）** 见下节。

### 2.2 字段传播的六种模式

「很多字段要对得上」不是一条规则，而是六种，必须分别声明：

| 模式 | 语义 | 阶段 | 例 |
| --- | --- | :---: | --- |
| `copy` | 父字段原值照抄到子字段 | ✅ M2 | `currency`、`txn_date`、`acct_no` 两表一致 |
| `derive` | 父字段经表达式/函数变换后写入子字段 | ✅ M2 | `detail_amt = parent.amount × 0.7` |
| `map` | 父表码值经映射表转换为子表码值 | ✅ M2 | 父 `txn_type` → 子 `detail_type` |
| `split` | 父的一个值拆分到 N 个子行，满足 Σ子 = 父 | ✅ M4 | 一笔 1000 拆成 3 笔明细，合计仍是 1000 |
| `aggregate` | **反向**：父字段由子表汇总得出 | ✅ M3 | 父 `detail_count` = 子表行数 |
| `free` | 子表自由生成，仅受自身分组规则约束 | ✅ M2 | 备注、随机附言 |

```yaml
propagate:
  - {mode: copy,    from: txn_date, to: txn_date}
  - {mode: copy,    from: acct_no,  to: acct_no}
  - {mode: copy,    from: currency, to: currency}
  - {mode: derive,  to: net_amount, expr: "parent.amount - parent.fee"}
  - {mode: map,     from: txn_type, to: detail_type,
     mapping: {T: TRANSFER, D: DEPOSIT, W: WITHDRAW}}
  - {mode: free,    to: remark}
```

#### `split` 的守恒保证（M4）

```yaml
- parent: t_txn
  child: t_txn_item
  cardinality: "1:N"
  join: [{parent_field: txn_no, child_field: txn_no}]
  propagate:
    - {mode: split, to: item_amt, from: amount, parts: 2}
    # 或按占比: {mode: split, to: item_amt, from: amount,
    #            ratio: [0.5, 0.3, 0.2]}
```

- **分单位整数 + 割点法**：金额先换成「分」（scale 取自列声明），取不重复割点切份 ——
  每份为正、Σ 恒等于父值，50 份 999999.99 也零误差
- `ratio` 模式最后一份兜差，占比声明得再怪守恒也不破
- 行数语义：split 模式下子表行数 = 父行数 × 份数，**子表不能再有有限取值组**
  （checker 会拦下这个冲突）；配合 invariants `sum(item_amt) = amount` 可自动证明守恒

两条省事的设计：

1. **`join` 锚点自动补齐 `copy`** —— 声明了 `{parent_field: txn_no, child_field: txn_no}`
   就不必再写一条 copy 规则；若已显式声明该子字段的规则，则以用户的为准。
2. **被传播覆盖的字段无需再归组** —— 分组完备性要求"每个字段有且仅有一个取值来源"，
   `propagate` 本身就是一种来源。写了 `to: currency` 就不必再造一个占位组。

另外 `ref` 组可在子表里直接引用父表字段（`- {type: ref, fields: [ref_no], from: parent.txn_no}`）。

### 2.3 1:1 关系下的覆盖分配 ⚠️

**这是最容易踩的坑。** 模型一规定"有限取值组之间做笛卡尔积"，但 1:1 关系锁定了子表行数 = 父表行数。若子表有个 3 取值的枚举组，笛卡尔积会把子表撑成 `父行数 × 3`，**基数当场就破**。

因此 1:1 下，子表的有限取值组改用**分配策略（allocation）**：

| 分配策略 | 语义 | 适用 |
| --- | --- | --- |
| `follow_parent` | 取值由父行决定（父枚举驱动子枚举） | **默认**。真实感优先：存款交易不可能「透支」 |
| `round_robin` | 在父行序列上轮转分配，100 行 3 取值 → 33/33/34 | 父未给约束时的兜底；行数不变但覆盖度仍可保证 |
| `random` | 随机分配 | 贴近生产数据的随机性 |
| `weighted` | 按权重分布分配 | 少量异常值（如 5% 冲正） |

**一句话概括这个分水岭**：

> 笛卡尔积是「放大行数换覆盖」，分配是「固定行数内保覆盖」。1:N 用前者，1:1 只能用后者。

### `follow_parent` 的确切语义（已实现）

「由父行决定」具体怎么定？规则是 —— **按驱动字段分组**：

- 驱动值**首次**出现 → 从组合池里轮转取下一个（不同父值拿到不同组合，**保覆盖**）
- 驱动值**再次**出现 → 复用上次那个组合（同父值得到同子值，**保一致**）

```yaml
relations:
  - parent: t_txn
    child: t_txn_detail
    cardinality: "1:1"
    drive_by: [txn_type]     # 同交易类型 → 同清算状态
```

驱动字段 `drive_by` 不填时的推断顺序：被 `copy`/`map` 的父字段 → `join` 的父字段。
写在 `GroupSpec.allocation` 上（默认 `follow_parent`），以**第一个有限取值组**为准。

### 2.4 条件枚举：父约束子

分组模型向表间延伸的关键一步 —— **组的取值集是动态的，可随父行变化**：

```yaml
- type: enum
  name: g_detail_status
  fields: [detail_status]
  when: "parent.txn_type == 'WITHDRAW'"        # 只在取款交易下生效
  allocation: follow_parent
  values: [["NORMAL"], ["OVERDRAFT"], ["REVERSED"]]
```

配合 `follow_parent`，父表 `txn_type` 的取值会驱动子表 `detail_status` 的取值集，造出的数据天然符合业务语义。

### 2.5 两阶段生成

`aggregate` 模式会在依赖图上形成**回边**（父 ← 子），因此生成分成两个阶段：

- **Phase 1 · 正向生成**：按表间拓扑序，父 → 子，完成 `copy` / `derive` / `map` / `split` / `free`；
- **Phase 2 · 反向回填**：按锚点聚合子表结果，更新父表的 `aggregate` 字段。

```yaml
# 父表侧的汇总字段
- type: aggregate
  name: g_detail_sum
  fields: [detail_amount_sum]
  from: t_txn_detail          # 源表；唯一子表时可省略
  expr: "sum(net_amount)"

- type: aggregate
  name: g_log_count
  fields: [log_count]
  from: t_txn_log
  expr: "count()"
```

**没有引入 `by` 语法** —— 回填发生在「每条父行」的上下文里，
按 `join` 锚点天然已经分好组，再写一次 `by` 是重复的。

支持的聚合函数：`count()` / `sum` / `avg` / `min` / `max` / `count_distinct`。
聚合之间可做四则运算（`sum(amount) / count()`），参数也可以是表达式（`sum(amount * 2)`）。
父行无匹配子行时按空集处理：`count`/`sum`/`avg` 得 `0`，`min`/`max` 得 `NULL`。

典型场景：A 的交易金额合计 = B 的明细之和；银行余额表由流水汇总得出。

### 2.6 多表链与多父

- **链式传播**：A → B → C，沿 DAG 逐级传播，C 可继承 A 的字段（`copy` 支持跨级引用）；
- **多父表**：B 同时引用 A 与 D，需保证 B 的每个外键在各自父表中都能找到（**引用完整性** —— `ref` 组只能从父表已生成的行里取值，不得凭空造）；
- **N:M 中间表**：本质是「两个父表行的组合取样」，可复用笛卡尔积机制后按 `sample` / 上限裁剪。

### 2.7 不变量：把「对得上」变成可校验的断言

传播规则只保证生成时对，还要能证明对。不变量分两种形态：

**行级断言** —— 在指定表的每一行上求值：

```yaml
invariants:
  - table: t_txn
    expr: "fee <= amount"
```

**跨表断言** —— 对 `table` 每行取其 `from` 子行集合，可用聚合函数（复用 aggregate 求值器）：

```yaml
invariants:
  - table: t_txn
    from: t_txn_detail
    expr: "sum(net_amount) = amount - fee"   # MySQL 风格的 = 也认

  - table: t_txn
    from: t_txn_log
    expr: "count() = 3"
```

`tableseed verify -c config.yaml` 生成一份内存数据逐条求值；
WebUI 里点「验证不变量」看同样结果。违例行会连同整行数据一起列出来 ——
**先声明口径，再验证口径**。裸字符串写法（`- "fee <= amount"`）兼容，作用域为全部表。

---
