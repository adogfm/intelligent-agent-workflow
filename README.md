# Intelligent Agent Workflow

一个基于多 Agent 协作的代码审查工作流系统，实现代码生成、代码审查、自动化测试和迭代修复的完整流程。

## 一、项目简介

本项目旨在探索多 Agent 协作与 Workflow 编排机制，通过将代码生成、代码审查、测试执行和代码修复组织为一个完整的工作流，展示 Agent 之间的协作、状态共享和流程控制。

系统以 `add(a, b)` 函数为演示场景，故意引入初始代码缺陷，通过代码审查、自动化测试和条件分支判断，触发后续修复流程，直到审查无阻断问题且测试通过，或因修复预算耗尽、代码无变化、执行异常而停止。

## 二、主要功能

- **多 Agent 协作：** 由编码、审查、测试和修复等 Agent 协同完成任务。
- **共享状态管理：** 在工作流执行过程中共享代码、审查结果、测试结果和修复记录。
- **条件分支：** 根据代码审查与测试结果动态决定下一步执行阶段。
- **迭代修复：** 当测试失败或审查发现问题时，进入修复流程，并重新测试和审查。
- **执行日志：** 记录工作流阶段、路由选择、状态更新和 Agent 执行事件。
- **执行结果追踪：** 汇总阶段执行情况、修复轮数、测试结果和最终审查结果。
- **有限循环与终止机制：** 设置最大修复轮数，避免工作流无限循环。
- **自动化测试：** 提供测试用例，验证 Agent 和工作流的关键行为。

## 三、工作流程

下图对应 `demo.py` 和显式设置 `max_fix_attempts` 的自动修复模式：

```mermaid
flowchart TD
    Coding[编码 Agent] --> Review[基线代码审查]
    Review --> Test[基线测试]
    Test --> Passed{审查无阻断问题且测试通过?}
    Passed -->|是| Resolved[结束：resolved]
    Passed -->|否| Budget{仍有修复预算?}
    Budget -->|否| Limit[结束：max_fix_attempts]
    Budget -->|是| Fix[修复 Agent]
    Fix --> Changed{代码有变化?}
    Changed -->|否| NoChange[结束：no_change]
    Changed -->|是| Retest[测试 Agent 复测]
    Retest --> ReReview[审查 Agent 复审]
    ReReview --> Passed
```

清洁基线按“编码 → 审查 → 测试 → 结束”执行，不额外复审。测试失败或审查仍有阻断问题，都会在预算允许时进入修复。改码后必须先复测、再复审；任何节点异常都可能以 `failed` 提前终止。

未设置 `max_fix_attempts` 的单轮模式只修复一次，改码后按已配置节点执行复验；同时启用审查和测试时，顺序为“复审 → 复测”。只有 `create_fix_workflow()` 的单节点修复不自动复验，报告会将尚缺当前验收证据的结果标记为 `unresolved`。

## 四、项目结构

```text
intelligent-agent-workflow/
├── workflow/          # 工作流编排与流程控制
├── tests/             # 自动化测试
├── demo.py            # 完整工作流演示
├── coding_agent.py    # 编码 Agent
├── fix_agent.py       # 代码修复 Agent
├── code_checker.py    # 代码检查与验证
├── planner.py         # 任务规划
├── models.py          # 数据模型与状态定义
├── agent.py           # Agent 相关实现
├── test_agent.py      # 测试 Agent
├── README.md          # 项目说明
└── Design.md          # 架构与设计文档
```

## 五、环境要求

- Python >=3.10。
- 运行依赖见 `requirements.txt`；pytest 测试依赖由 `pyproject.toml` 的 `test` 可选依赖组声明。

## 六、安装与运行

### 1. 克隆仓库

```bash
git clone https://github.com/adogfm/intelligent-agent-workflow.git
cd intelligent-agent-workflow
```

### 2. 安装依赖

建议先创建并激活 Python 虚拟环境。运行源码和离线 Demo 只需运行依赖：

```bash
python -m pip install -r requirements.txt
```

如需运行 pytest、开发项目或使用安装后的命令行入口，安装项目及测试依赖：

```bash
python -m pip install -e ".[test]"
```

`test` 依赖组包含 `pytest>=9.1.1,<10.0.0`，并同时安装项目运行依赖。默认 Demo 使用确定性离线供应商，无需 API Key；真实模型接口通过 `.env.example` 中的配置启用，本次验证未调用真实模型。

### 3. 运行工作流演示

```bash
python demo.py
```

程序将展示 Agent 执行过程、条件分支选择、自动修复循环、执行日志以及最终工作流报告。

### 4. 运行自动化测试

安装 `.[test]` 后运行 pytest：

```bash
python -m pytest -v
```

也可只使用运行依赖，通过 Python 标准库运行相同测试方法：

```bash
python -m unittest discover -s tests -v
```

pytest 会额外单独统计 `unittest.subTest` 子测试；unittest 的测试方法总数不含这些子测试的单独计数。

## 七、演示结果

演示程序以存在缺陷的 `add(a, b)` 函数为起点，展示系统如何通过测试与审查发现问题，并执行迭代修复。

本次已验证的演示结果：

- 工作流执行成功。
- 共执行 9 个阶段。
- 完成 2 轮代码修复。
- 最终代码审查评分为 100，未发现问题。
- 最终测试通过，3/3 项测试成功。
- 记录 56 条工作流日志。

最终代码如下：

```python
def add(a, b):
    return a + b
```

2026-10-23 本地验证结果（Windows、Python 3.14.3、pytest 9.1.1）：

- 232 项测试通过。
- 704 个子测试通过。
- 存在 39 条 Pytest 警告，主要涉及测试类的自动收集提示。

其他 Demo 入口：

```bash
python demo.py --max-fix-attempts 0
python demo.py --max-fix-attempts 1
python demo.py --json
```

| 模式 | 业务结果 | 退出码 | 阶段 / 修复轮数 / 日志数 |
|---|---|---|---|
| 默认 | succeeded | 0 | 9 / 2 / 56 |
| 预算 0 | unresolved | 1 | 3 / 0 / 20 |
| 预算 1 | unresolved | 1 | 6 / 1 / 38 |
| `--json` | succeeded | 0 | 9 / 2 / 56 |

`--json` 在人类可读输出后附加 JSON，不是只输出 JSON。Demo 的生成、审查和修复供应商按预设场景返回结果；真实测试由 TestRunner 子进程执行。演示说明流程控制，不代表真实模型对任意任务的修复效果。

### 实现边界

- TestRunner 的超时控制与报告输出截断是不同机制。`max_output_bytes` 默认对 stdout、stderr 各读取前 64 KiB，再附加截断提示；运行时日志文件大小没有该上限，子进程也不是操作系统安全沙箱。
- WorkflowReport 顶层采用冻结 dataclass，内容复制后与原状态隔离；嵌套模型和容器仍可能可变，不保证深度不可变。
- 通用 Workflow 的 `succeeded` 只反映已配置检查与待复验要求，不表示未配置的检查也已执行。`main.py` 的非循环入口退出码也不等同于业务验收结果，详见 Design.md。
- 当前 AST 检查、代码生成校验和特定密码修复提示词仍有已知限制，详见 Design.md 第 7、14 节。

## 八、设计文档

详细的系统架构、工作流编排、状态管理、条件分支和修复循环设计，请参阅 [Design.md](Design.md)。

## 九、项目目标总结

本项目通过一个可运行的多 Agent 工作流示例，展示 Agent 协作、状态共享、条件分支、循环控制、执行日志和自动化测试等核心机制，并为后续增加 Agent 类型、扩展流程节点和完善错误恢复机制提供基础。
