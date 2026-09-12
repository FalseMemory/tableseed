# tableseed 🌱

> 多表联合造数工具 —— 用「字段分组」描述单表规则，用「表间关系」描述跨表联动，
> 生成**一致、可入库、覆盖可证明**的测试数据。

```
表结构 + 表间关系 + 字段分组规则  ──►  tableseed  ──►  SQL / CSV / 直连入库
```

[![tests](https://img.shields.io/badge/tests-235%20passed-34d399)]()
[![python](https://img.shields.io/badge/python-3.13-4d9fff)]()
[![license](https://img.shields.io/badge/license-private-8b98ad)]()

---

## ✨ 核心能力

| | 能力 | 说明 |
| --- | --- | --- |
| 🧩 | **字段分组模型** | 组是造数最小规则单元，组内字段共进退（码值变了描述绝不会丢） |
| 🔗 | **表间关系模型** | 四要素 + 六种传播（copy / derive / map / split / aggregate / free）|
| 📐 | **覆盖可证明** | 笛卡尔积 / pairwise / sample 三策略，组合数可计算、覆盖度可展示 |
| ⚖️ | **守恒保证** | split 割点法零误差：Σ子 = 父，50 份 999999.99 也分毫不差 |
| ✅ | **不变量校验** | 跨表断言（Σ明细 = 金额 − 手续费），生成后自检逐条求值 |
| 🖥️ | **WebUI 全流程** | 配置 → 预演 → 关系图 → 生成 → 预览 → 确认入库 → SQL 台 → 操作日志 |
| 🔁 | **可复现** | 同配置 + 同 seed = 同数据，缺陷可稳定重现 |
| 🔌 | **多库多连接** | config.ini 管理多个数据库连接，页面随时切换；密码走环境变量 |

## 🚀 快速开始

```bash
# 安装依赖后，启动 WebUI（默认载入示例配置）
tableseed ui -c samples/txn.yaml
```

CLI 全家桶：

| 命令 | 作用 |
| --- | --- |
| `tableseed ui -c xxx.yaml` | 启动 WebUI：配置、预演、生成、预览、入库、查数据都在浏览器里 |
| `tableseed check -c xxx.yaml` | 校验配置：分组不重不漏、依赖无环、组合规模 |
| `tableseed plan -c xxx.yaml` | 预演：各表组合数 / 行数 / 生成顺序 |
| `tableseed verify -c xxx.yaml` | 校验不变量：内存数据逐条断言，列出违例行 |
| `tableseed gen -c xxx.yaml` | 生成（无连接 → 只生成不落盘） |
| `tableseed gen -c xxx.yaml --dsn ...` | 生成并直接入库 |
| `tableseed gen -c xxx.yaml -o out/` | 落盘 SQL / CSV |

示例配置：[samples/account.yaml](samples/account.yaml)（单表 12 组合）、
[samples/txn.yaml](samples/txn.yaml)（交易 9 行 + 详情 1:1 + 日志 1:N + 分录守恒）。

## 🖥️ WebUI 功能

```
配置（YAML / 表格编辑器 / 快速生成） → 预演 → 关系图 → 结果预览 → 确认插入 → SQL 查询台 → 操作日志
```

- **多配置文件**：左栏顶部下拉切换 / 另存为 / 移除，清单存于 config.ini
- **多数据库连接**：config.ini 管理，页面随时切换；「测试连接」一键验证
- **两段式入库**：生成只在内存预览，点「确认插入」才写库；同批数据重复插入会被拦截
- **SQL 查询台**：默认只读（SELECT / WITH / SHOW / EXPLAIN / DESC），结果分页
- **操作日志**：生成 / 插入 / 查询全记录，分页筛选搜索，JSONL 落盘跨重启恢复

## ⚙️ 配置说明

### 造数配置（YAML）

```yaml
seed: 20260910            # 随机种子，可复现
limits:
  max_rows: 100000        # 单表硬上限
  total_rows: 100000      # 全部表总行数上限（默认 10 万）
  strategy: full          # full | pairwise | sample
tables:
  - name: t_account
    groups:               # 九种组类型，见 docs/model.md
      - type: enum
        name: g_status
        fields: [status_code, status_desc]   # 组内字段共进退
        values: [["01", "正常"], ["02", "冻结"]]
```

完整字段说明、九种组类型、表间关系、split 守恒、条件枚举与分配策略 ——
见 **[docs/model.md](docs/model.md)**。

### 数据库连接（config.ini）

连接信息与业务配置分离（造数 YAML 可以随便分享、入库）：

```ini
[general]
active = demo

[demo]
type = mysql
host = 127.0.0.1
password_env = TABLESEED_DB_PASSWORD   ; 密码走环境变量（推荐）
database = tableseed_demo

[prod]
type = mysql
host = 10.0.0.5
database = biz_db
```

- 密码优先取环境变量，配置文件可安全入库；页面回显一律脱敏
- 特殊字符（含 `%`）自动正确处理；密码里的 `@ : / #` 做 URL 编码
- 样例：[config.ini.example](config.ini.example)

## 📚 文档

| 文档 | 内容 |
| --- | --- |
| [docs/model.md](docs/model.md) | 数据模型详解：字段分组、表间关系、split 守恒、条件枚举、分配策略 |
| [docs/PRD.md](docs/PRD.md) | 需求规格：功能清单（FR-1 ~ FR-10）、非功能需求、验收标准 |
| [docs/tech-design.md](docs/tech-design.md) | 技术方案：选型、架构分层、IR 数据结构、关键算法、表达式引擎 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

## 🧪 测试

```bash
pytest           # 235 条用例（单元 / e2e / WebUI），pytest + allure
```

## 🛣️ Roadmap

- [x] **M1** 分组模型 + 单表展开 + 执行策略 + CLI + WebUI 最小闭环 + SQL 查询台
- [x] **M2** 表间关系内核 + 关系图：四要素 + copy/derive/map/free + 拓扑排序 + 1:1 分配策略
- [x] **M3** aggregate 两阶段回填 + invariants 不变量 + 覆盖策略（full / pairwise / sample）
- [x] **M4** split 拆分传播（Σ子=父 零误差守恒）+ 生成后自检
- [x] **WebUI 2.0**：多连接 / 多配置文件 / 两段式入库 / 操作日志 / 关系图 / 配置编辑器 / 单元格编辑器
- [ ] M4 收尾：多父（N:M）
- [ ] **M5** 打磨与集成：打包分发、Python API 稳定化、与测试平台集成

## 🎨 设计原则

1. **声明式** —— 规则写在配置里，不写脚本；配置可 diff、可评审、可版本化。
2. **覆盖可证明** —— 组合数可计算，造出多少、漏了哪些说得清。
3. **组是原子的** —— 同组字段共进退，杜绝组内不同步。
4. **覆盖方式随行数形态而变** —— 有空间就笛卡尔积放大，没空间就分配保覆盖。
5. **只读元数据** —— 不写目标库结构，只读表定义。
6. **可复现** —— 同配置 + 同 seed = 同数据。

## 🧰 技术栈

Python 3.13 · Typer（CLI）· FastAPI + uvicorn（SSE）· 原生 JS WebUI ·
SQLAlchemy Inspector（可选依赖）· 自研 ast 沙箱表达式引擎 · pytest + allure

> 选型理由与备选对比见 [docs/tech-design.md](docs/tech-design.md)。

---

本仓库仅包含通用示例，不含任何具体机构的表结构、字段命名或业务规则。
