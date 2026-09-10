# tableseed

> 多表联合造数工具 —— 按「字段分组」描述数据规则，生成跨表一致、可直接入库的测试数据。

给一组表、它们之间的关联关系，以及每个字段所属**分组**的取值规则，产出可以直接入库、跨表对得上账、且规则覆盖可证明的测试数据。

```
表结构 + 表间关联 + 字段分组规则  ──►  tableseed  ──►  SQL / CSV / 直连入库
```

## 它要解决什么

单表随机造数很容易，难的是**多表联动**与**数据规则**：

| 痛点 | 具体表现 |
| --- | --- |
| 关联断裂 | 流水表的 `acct_no` 在主表里查不到，外键直接炸 |
| 口径不符 | 明细汇总与余额表对不上，数据核对场景无法使用 |
| 规则散落 | 枚举取值、码值描述、边界值写死在几十份 INSERT 脚本里，改一处漏三处 |
| 同组不同步 | `status_code` 改成 `02`，`status_desc` 还是「正常」，造出脏数据 |
| 覆盖不足 | 只造了「正常 + 人民币 + 柜面」，其余组合从未被验证，却报"已测" |
| 覆盖爆炸 | 想全组合覆盖，`4×3×5×8` 一乘就是几百上千行，人工排不动 |
| 不可复现 | 随机造数每次结果不同，缺陷无法稳定重现 |
| 状态跳跃 | 造出「已销户却仍有在途交易」这类业务上不可能存在的组合 |

## 核心模型

tableseed 用三层结构描述一份造数配置：

```
表（Table）  ──►  字段组（Field Group）  ──►  取值（Value）
```

### 1. 字段分组（Field Group）

一张表的字段被划分为若干**互不相交的组**：

- 每个字段**恰好属于一个组**，不重不漏（完备划分，配置校验会强制检查）；
- 一个组**可以包含多个字段**；
- **组是造数的最小规则单元** —— 造数时以组为单位决定取值，而不是以字段为单位。

组内多字段一次生成一个**取值元组**，因此组内字段天然同步，不可能出现「码值变了描述没变」：

```yaml
- type: enum
  name: g_status
  fields: [status_code, status_desc]      # 同一组的两个字段
  values:
    - ["01", "正常"]                       # 一个取值 = 两个字段的联合取值
    - ["02", "冻结"]
    - ["03", "销户"]
```

> `status_code = "02"` 时，`status_desc` 必然是「冻结」。同步关系由分组模型保证，不依赖人工维护。

### 2. 组类型（Group Type）

| 类型 | 语义 | 参与笛卡尔积 | 典型用例 |
| --- | :---: | :---: | --- |
| `enum` 枚举组 | 显式列举有限取值集合（元组列表） | ✅ | 状态、币种、渠道、产品码 |
| `boundary` 边界组 | 枚举的语义化变体，专列边界与异常值 | ✅ | 金额上限、超长字符串、`NULL`、负数 |
| `dict` 字典组 | 从外部字典/码表取有限取值 | ✅ | 行名行号、地区码、机构码 |
| `sequence` 自增组 | 按起始值/步长推进，支持格式化模板 | ❌ | 流水号、账号、序号 |
| `random` 随机组 | 按生成器随机取值：区间、正则、字典、加权分布 | ❌ | 金额、日期、姓名、备注 |
| `const` 不变组 | 全批数据取同一常量元组 | ❌ | 租户号、机构号、环境标识、版本号 |
| `derive` 派生组 | 由同表其他字段表达式计算得出 | ❌ | 余额 = 期初 + 发生额 |
| `ref` 引用组 | 取自其他表已生成的值（外键联动） | ❌ | 主表业务键 |

> 前四类（`enum` / `boundary` / `dict`，以及需要时把 `dict` 视作枚举）本质都是**有限取值集合**，因此都参与笛卡尔积展开；后四类是**每行现算**的值，不参与展开。

### 3. 生成语义

一条数据 = **所有组取值的组合**。两类组以不同方式参与：

**① 有限取值组之间 —— 笛卡尔积（全组合）**

配置里所有枚举性质的组，其取值集合做笛卡尔积，**每个组合生成一条数据**，从而保证组合覆盖是完备的、可计算的、可证明的。

**② 非枚举组 —— 在已展开的行上"附着"生成**

自增组按行推进一次，随机组按行采样一次，不变组填充常量，派生组/引用组按依赖顺序计算。它们**不放大**行数，只决定这些行上其余字段取什么值。

```
                     ┌──────── 组（字段） ────────┐
 行数 =  Π  |枚举组取值集|      ×      基数倍数
         └ 笛卡尔积 ┘                  └ 来自父表的 1:N 关系，可选
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

### 4. 规模治理

笛卡尔积是有意为之的"覆盖放大器"，但组合数会指数增长，因此必须可控：

| 机制 | 说明 |
| --- | --- |
| `max_rows` | 硬上限，超出即报错而非静默截断 |
| `exclude` | 声明非法组合并剔除。例：`status_code == "03" and balance > 0`（已销户不该有余额） |
| `strategy: full` | 全组合（默认）。适合中小规模，覆盖可证明 |
| `strategy: pairwise` | 两两组合覆盖。组合爆炸时用最少的行数覆盖所有字段对的取值，性价比最高 |
| `strategy: sample(n)` | 从全组合中抽样 n 条，并输出「未覆盖组合清单」 |

无论用哪种策略，`tableseed plan` 都会先算出**理论组合总数**并打印出来，让人在做之前就知道要造多少行。

### 5. 多表下的展开作用域（设计要点）

子表同时受"父表 1:N 关系"和"自身枚举组"影响，需要明确展开次序：

| 作用域 | 语义 | 行数 |
| --- | --- | --- |
| `per_parent` | 每条父行都完整覆盖子表枚举组合 | 父行数 × 子表组合数 |
| `global` | 子表枚举组合在全表范围内展开一次，再分配到各父行 | 子表组合数 × 基数 |

默认 `per_parent`（覆盖更彻底），可按表覆写。

## 生成流程

```mermaid
flowchart LR
    A[元数据扫描<br/>主键/唯一/非空/外键] --> B[分组装配与校验<br/>字段不重不漏]
    B --> C[依赖分析<br/>表间拓扑排序 + 组间依赖]
    C --> D[规模预演<br/>plan：组合数 / 行数]
    D --> E[逐表生成<br/>枚举组笛卡尔积<br/>非枚举组附着]
    E --> F[一致性自检<br/>外键 / 唯一 / 约束]
    F --> G[输出<br/>SQL / CSV / 直连入库]
```

## 配置样例（草案，字段名待定）

```yaml
seed: 20260910                      # 固定随机种子，结果可复现

limits:
  max_rows: 100000
  strategy: full                    # full | pairwise | sample
  exclude:
    - "t_account.status_code == '03' and t_account.balance > 0"

tables:
  - name: t_account
    scope: per_parent
    groups:
      # ── 枚举组：组内多字段联合取值 ──
      - type: enum
        name: g_status
        fields: [status_code, status_desc]
        values:
          - ["01", "正常"]
          - ["02", "冻结"]

      - type: enum
        name: g_currency
        fields: [currency]
        values: [["CNY"], ["USD"]]

      - type: enum
        name: g_channel
        fields: [channel]
        values: [["OTC"], ["EBANK"], ["MOBILE"]]

      # ── 非枚举组：在每条展开行上附着 ──
      - type: sequence
        name: g_acct_no
        fields: [acct_no]
        start: 1
        step: 1
        format: "6222{seq:012d}"

      - type: random
        name: g_balance
        fields: [balance]
        generator: weighted_int
        range: [0, 100000000]
        buckets: [0.80, 0.15, 0.05]    # 普通 / 大额 / 超大额
        scale: 2

      - type: const
        name: g_tenant
        fields: [tenant_id, branch_code]
        value: ["0001", "001"]        # 整批恒定

      - type: derive
        name: g_amt
        fields: [amount]
        expr: "balance * 0.01"

  - name: t_txn
    relation:
      parent: t_account
      foreign_key: acct_no
      cardinality: [1, 3]             # 每个账户 1~3 笔交易
    groups:
      - type: enum
        name: g_txn_type
        fields: [txn_type]
        values: [["DEPOSIT"], ["WITHDRAW"], ["TRANSFER"]]

      - type: ref
        name: g_ref_acct
        fields: [acct_no]
        from: t_account.acct_no

      - type: sequence
        name: g_txn_no
        fields: [txn_no]
        format: "T{seq:08d}"
```

## CLI 草案

```bash
tableseed plan  -c seed.yaml              # 预演：打印各表组合数/行数/依赖序，不产出数据
tableseed check -c seed.yaml              # 校验配置：分组是否不重不漏、有无依赖环、组合规模
tableseed gen   -c seed.yaml -o out/      # 生成 SQL / CSV
tableseed gen   -c seed.yaml --dsn ...    # 直连数据库批量入库
tableseed gen   -c seed.yaml --strategy pairwise   # 覆盖策略覆盖写
```

`plan` 示例输出：

```
t_account   枚举组: g_status(2) × g_currency(2) × g_channel(3) = 12 组合  → 12 行
t_txn       枚举组: g_txn_type(3) = 3 组合 × 每父行 1~3 笔 ≈ 24 行
组合总数 36，未超 max_rows(100000)                              ✓ 校验通过
```

## 设计原则

1. **声明式** —— 规则写在配置里，不写脚本；配置可 diff、可评审、可版本化。
2. **可证明的覆盖** —— 枚举组合数可计算，造出多少复合多少、漏了哪些说得清。
3. **组是原子的** —— 同组字段共进退，杜绝组内不同步。
4. **只读元数据** —— 不写目标库结构，只读表定义。
5. **可复现** —— 同配置 + 同 seed = 同数据。

## Roadmap

- [ ] **M1** 元数据扫描 + 分组模型 + 单表笛卡尔积展开 + SQL 输出（CLI）
- [ ] **M2** 全部组类型实现 + 多表关联拓扑排序 + 外键传播
- [ ] **M3** 覆盖策略（pairwise / sample）+ `plan` 预演 + `check` 校验
- [ ] **M4** 直连入库 + 生成后自检（外键 / 唯一 / 业务约束）
- [ ] **M5** 可视化配置界面（表关联图 + 分组拖拽 + 覆盖度热力图）

## 技术选型（待定）

- 主语言：Python 3.13
- CLI：Typer
- 数据库适配：PostgreSQL / MySQL / Oracle 兼容层（方言隔离）
- 可选前端：React + Vite（M5）

## 状态

项目刚立项，骨架待搭建。当前仓库只有这份 README。

---

本仓库仅包含通用示例，不含任何具体机构的表结构、字段命名或业务规则。
