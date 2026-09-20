# 第 11 章：虚拟环境、pip 与 uv——让 Python 项目真正可重建

> 独立还原说明：课件原项目位于大仓库的 `java_to_python/chapters/11_environments_packages`。本目录已经把该章节提升为独立项目根，因此源码中的仓库定位改为本目录下的 `demo_project/`；课件中带大仓库前缀的命令仍按原文保留，便于与截图逐项对应。

一句话先说透：**Python 启动时会先选定一个具体解释器，`import` 只能看到这个解释器环境中的包。** `requirements.txt`、`pyproject.toml` 和 `uv.lock` 负责描述“应该安装什么”，但它们不会在 `python main.py` 启动时自动下载依赖。

这和 Java 项目最不一样的地方是：Maven/Gradle 通常会在构建或运行任务前解析依赖并组装 classpath；普通 Python 解释器只负责执行代码，不会先读取项目声明并补齐环境。

学完本章，你应该能回答四个问题：

1. 当前究竟是哪个 Python 在运行？
2. 一个第三方库安装到了哪个环境？
3. 项目声明、锁文件和 `.venv` 各自保存什么？
4. 为什么“明明安装过”仍然会出现 `ModuleNotFoundError`？

## 11.1 先建立 Java → Python 心智模型

下面的类比用于快速定位职责，但都不是严格的一一对应。

| Java 世界 | Python 世界 | 相似点与关键边界 |
| --- | --- | --- |
| 选定的 JDK / `java` 可执行文件 | Python 解释器，如 `.venv/bin/python` | 都决定实际运行时；Python 解释器还直接决定默认第三方包目录 |
| 运行时 classpath | `sys.path` | 都决定代码查找范围；Python 在运行时按顺序查找并执行模块 |
| Maven/Gradle 依赖声明 | `requirements.txt` 或 `pyproject.toml` | 都表达项目需要什么；普通 `python` 不会自动读取并安装它们 |
| Gradle dependency locking 等锁定结果 | `uv.lock` | 都保存一次解析后的精确依赖图；锁文件本身不包含已安装文件 |
| Maven 坐标中的 artifact | Python 发行包（distribution） | 都是安装工具处理的发布单元；发行包名不一定等于代码导入名 |
| 项目私有运行目录 | `.venv` 虚拟环境 | 都承载可丢弃的本机状态；`.venv` 不是容器，也不是可移植产物 |

一次完整运行可以理解成下面这条链路：

```text
选择 Python 解释器
  → 创建或找到虚拟环境
  → 根据依赖声明解析版本
  → 把发行包安装进这个环境
  → 用同一个环境的解释器启动程序
  → import 按 sys.path 查找模块
```

排错时不要把整条链路都归结为“pip 有问题”，而要先判断错误发生在哪一步。

## 11.2 六个名字相近、职责不同的对象

### 解释器：真正执行代码的程序

`python` 只是 shell 根据 `PATH` 找到的命令名。机器上可以同时有系统 Python、仓库根 `.venv` 的 Python、某个子项目 `.venv` 的 Python，以及 IDE 单独选中的 Python。

先用运行时证据确认它，而不是看终端提示符猜：

```bash
python -V
python -c 'import sys; print(sys.executable); print(sys.prefix); print(sys.base_prefix)'
```

- `sys.executable`：当前进程由哪个解释器入口启动。
- `sys.prefix`：当前环境的根目录。
- `sys.base_prefix`：创建虚拟环境所依赖的基础 Python 目录。
- `sys.prefix != sys.base_prefix`：通常表示当前进程正在虚拟环境中。

这类似于打印 Java 进程实际使用的 `java.home`，比“我记得 IDE 选过某个 SDK”可靠。

### 虚拟环境：项目专属的第三方包空间

`venv` 会基于一个已有 Python 创建环境入口、`pyvenv.cfg` 和独立的 `site-packages`。它通常复用基础解释器及标准库，不是复制一整套操作系统。

```bash
# 在一个新项目目录中创建环境；本仓库要求 Python 3.14。
python3.14 -m venv .venv

# macOS / Linux：激活后，python 会优先指向 .venv。
source .venv/bin/activate
python -c 'import sys; print(sys.executable)'
deactivate

# 不激活也完全可以，直接指定解释器最明确。
.venv/bin/python -c 'import sys; print(sys.executable)'
```

Windows PowerShell 对应的入口通常是 `.venv\Scripts\python.exe`，激活脚本是 `.venv\Scripts\Activate.ps1`。

激活的本质只是修改当前 shell 的 `PATH`，并设置 `VIRTUAL_ENV` 等辅助信息。它不会修改系统 Python，不会改变已经启动的 IDE 或 Python 进程，也不会自动安装项目依赖。虚拟环境还不会隔离环境变量、网络、业务文件或操作系统进程，所以它不能与 Docker 容器等同。

直接运行 `.venv/bin/python` 时，即使 `VIRTUAL_ENV` 没有设置，虚拟环境仍然有效。

`.venv` 应被视为可重建的本机产物。里面的启动脚本可能记录绝对路径，二进制扩展还依赖 Python 版本、操作系统和 CPU，所以不要复制 `.venv` 或 `site-packages` 给同事。应该提交源码、Python 版本要求和依赖声明，让对方重新创建环境。

### 发行包与导入包：安装名不等于代码中的名字

本章安装的发行包是：

```text
python-dotenv==1.2.3
```

代码使用的导入包却是：

```python
from dotenv import dotenv_values
```

`python-dotenv` 是 pip/uv 和包索引认识的发行包名，`dotenv` 是 Python 运行时认识的导入名。二者没有机械转换规则。连字符不能出现在 Python 标识符中，而擅自改成 `python_dotenv` 仍然是错的；一个发行包也可能提供多个导入包。

这可以类比为“artifactId 不等于 Java package”，但 Python 没有要求二者遵守同一套命名规则。应以库的官方用法或安装元数据为准。

### 包索引：只参与安装，不参与 import

PyPI 是常见的公共包索引，也可以配置公司私服。pip/uv 在安装阶段查询索引、选择兼容版本并下载分发文件。`import` 阶段不会访问索引，更不会发现缺包后自动联网安装。

### `sys.path` 与 `site-packages`：查找范围和安装位置

第三方包通常安装进环境的 `site-packages`，而 `sys.path` 是当前进程实际使用的模块搜索路径列表。前者是常见安装位置，后者才决定本次 `import` 会到哪里找。

```python
import sys

print(sys.executable)
for position, path in enumerate(sys.path):
    print(position, path)
```

脚本目录、当前工作目录、`-I` / `-P` 参数和环境配置都可能影响搜索路径。因此“文件确实在磁盘上”不代表当前进程一定能导入它。

## 11.3 黄金规则：始终让 pip 绑定到目标解释器

推荐这样安装：

```bash
python -m pip install "python-dotenv==1.2.3"
```

`python -m pip` 的意思是“让刚才选中的这个 Python 执行它能看到的 pip 模块”。如果只写 `pip install ...`，shell 会再次根据 `PATH` 独立寻找 `pip`，它可能属于另一套 Python。

用下面三条命令可以把解释器、pip 和安装目录对齐：

```bash
python -c 'import sys; print(sys.executable)'
python -m pip --version
python -m pip show python-dotenv
```

`pip --version` 的输出同时包含 pip 所在路径和对应的 Python 版本。两个解释器都显示“Python 3.14.7”，也不代表它们共享已安装包；必须继续比较路径。

| 命令 | 它实际证明什么 | 它不能证明什么 |
| --- | --- | --- |
| `python -m pip install X` | 把 X 及所需依赖安装到目标环境 | 不会自动把 X 写回项目声明 |
| `python -m pip install -r requirements.txt` | 满足清单中的安装要求 | 通常不会删除清单之外的额外包 |
| `python -m pip show X` | 当前环境存在发行包 X 的元数据 | 不代表导入名也叫 X |
| `python -m pip list` | 当前环境安装了哪些发行包 | 不区分直接依赖和间接依赖的业务意图 |
| `python -m pip check` | 已安装包声明的依赖是否缺失或冲突 | 不会运行应用，也检查不了本地文件遮蔽 |

## 11.4 `requirements.txt`：安装清单，不是自动执行的构建文件

本章的 `demo_project/requirements.txt` 只有一个直接运行依赖：

```text
python-dotenv==1.2.3
```

`==1.2.3` 固定这个直接依赖的版本。若写成 `>=1.2.3,<2`，以后重新解析时可以选择范围内的其他版本。依赖所需的其他包叫间接依赖，pip 会根据发行包元数据继续解析。

`requirements.txt` 可以先类比 Maven/Gradle 的依赖清单，但边界很重要：pip 读取它完成安装，普通 `python main.py` 不会读取它，也不会自动把当前环境同步成“只包含清单中的内容”。

### `pip freeze` 为什么不能替代依赖设计

```bash
python -m pip freeze > environment-snapshot.txt
```

`freeze` 记录当前环境的安装快照。它可能包含临时工具、间接依赖、本地路径，以及与当前机器有关的信息，却不说明“项目为什么需要这个包”。它更像对当前 classpath 结果做快照，而不是重新编写一份有业务意图的构建文件。

快照适合排查和临时复现；正式项目仍应维护清晰的直接依赖，并通过干净环境验证可重建性。

## 11.5 `pyproject.toml` 表达意图，`uv.lock` 保存解析结果

本章演示项目的核心声明是：

```toml
[project]
name = "chapter11-config-demo"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["python-dotenv>=1.2.3,<2"]

[dependency-groups]
dev = []

[tool.uv]
package = false
```

- `name` 和 `version` 是项目发行元信息，不保证存在同名导入包。
- `requires-python = ">=3.14"` 声明兼容范围，不会把当前 `python` 自动切换成 3.14。
- `dependencies` 是应用直接运行依赖，类似 Maven 的普通运行依赖。
- `dependency-groups.dev` 用于测试、格式化等开发依赖；本章暂时为空。
- `package = false` 告诉 uv：这是直接运行脚本的应用，不必把应用自身构建安装成发行包。

`pyproject.toml` 表达“允许并需要什么”，而 `demo_project/uv.lock` 保存 uv 已解析出的精确版本、来源和文件哈希。本章锁文件最终选择 `python-dotenv 1.2.3`。

| 状态 | 本章对应物 | 应否提交 Git |
| --- | --- | --- |
| 依赖意图 | `pyproject.toml` | 应提交 |
| 精确解析结果 | `uv.lock` | 本章这种应用项目应提交 |
| 解释器选择约定 | `.python-version` 中的 `3.14` | 可以提交 |
| 本机实际安装结果 | `.venv/` | 不提交，按声明与锁重建 |

这类似“构建声明 → 依赖锁定 → 本机产物”三层。`uv.lock` 不是 `.venv` 的压缩包，也不是 `pip install -r` 能直接读取的 requirements 文件；通常应由 uv 维护，不要手改哈希和版本。

`.python-version` 会被 uv 等工具用于选择 Python，但普通 Python 不会读取它。它只写了 `3.14`，没有固定补丁版本；项目是否允许某解释器仍以 `requires-python` 等约束为准。

## 11.6 uv 把声明、锁定和环境同步成一套工作流

uv 可以先类比为“环境管理 + 依赖解析 + 锁定 + 命令运行”的组合工具，但它不是 Maven 的 Python 语法版。它仍然要创建 Python 环境，运行时也仍然遵守 Python 的 import 规则。

下面以本章锁文件使用的 uv 0.12.10 行为为基线：

| 命令 | 声明 | 锁文件 | `.venv` |
| --- | --- | --- | --- |
| `uv add "python-dotenv>=1.2.3,<2"` | 添加或调整直接依赖 | 更新 | 同步 |
| `uv remove python-dotenv` | 删除直接依赖 | 更新 | 同步；若仍是间接依赖则可能保留 |
| `uv add --dev pytest` | 写入开发依赖组 | 更新 | 默认同步开发依赖 |
| `uv lock` | 不改变直接依赖意图 | 创建或更新解析结果 | 不安装包 |
| `uv sync` | 不改变直接依赖意图 | 必要时更新 | 使环境匹配本次依赖选择，默认移除多余包 |
| `uv run python main.py` | 通常不改直接依赖 | 必要时更新 | 确保环境可运行后启动命令 |

两个常见误判：`uv lock` 成功不代表 `.venv` 已存在；`uv run` 也不是纯启动器，首次运行可能解析、下载和安装依赖。

用于 CI 或可重复验证时，可以加 `--locked`：

```bash
uv lock --check
uv sync --locked
uv run --locked python main.py
```

`--locked` 表示锁文件缺失或与声明不一致时失败，不允许命令悄悄改锁；但 `sync` / `run` 仍然可能创建或修改 `.venv`，也可能为了安装锁定依赖而联网。

uv 管理的虚拟环境可以没有 pip，因为 uv 自己能安装和检查包。此时 `python -m pip --version` 报 `No module named pip` 不代表环境坏了，可以使用：

```bash
uv pip list --python .venv/bin/python
uv pip check --python .venv/bin/python
```

不要在 uv 项目里随手用 pip 安装依赖却不更新声明。那只会改变本机环境；`uv run` 可能暂时还能看到这个额外包，而一次干净重建或精确 `uv sync` 会暴露声明缺失。正式增删依赖应使用 `uv add` / `uv remove`。

## 11.7 本章的小应用：像读取 Java Properties 一样读取 `.env`

本章用 `python-dotenv` 读取本地 Agent 配置。这可以先类比 Java 的 `Properties`：文件中的值读出来仍然是字符串，业务代码负责必填检查和类型转换。但 `.env` 的语法、插值规则和 API 并不等同于 `.properties`。

下面是一个无需预读项目代码也能理解的最小例子：

```python
from io import StringIO

from dotenv import dotenv_values

config = dotenv_values(
    stream=StringIO("AGENT_NAME=文档助手\nTOOL_LIMIT=3\n"),
    interpolate=False,
)

name = config.get("AGENT_NAME")
limit_text = config.get("TOOL_LIMIT")
if not name or limit_text is None:
    raise ValueError("配置缺少必填项")

limit = int(limit_text)
if limit <= 0:
    raise ValueError("TOOL_LIMIT 必须是正整数")

print(name, limit, type(limit).__name__)
```

执行顺序很直接：

1. `dotenv_values()` 解析文本并返回类似 `dict` 的映射，不修改 `os.environ`。
2. `get()` 在键不存在时返回 `None`，因此先做必填检查。
3. `TOOL_LIMIT` 原始值是字符串 `"3"`，`int()` 才把它转换成整数。
4. 类型转换成功不代表业务合法，所以还要检查是否为正数。

如果确实希望把配置写入当前进程环境，再考虑 `load_dotenv()`。只需要读取配置时，返回映射的 `dotenv_values()` 副作用更小，也更像显式加载一个 `Properties` 对象。

本章的 `demo_project/main.py` 读取同目录的 `settings.env.example`。它用 `Path(__file__)` 定位配置，所以无论从仓库根目录还是脚本目录启动，都不会把当前工作目录误当成配置目录。示例只展示 `STATUS_URL`，不会发送 HTTP 请求，也不需要 API Key。

## 11.8 两种完整运行方式：选一套即可

演示项目同时保留 requirements 和 uv 声明，只是为了比较两种工作流。真实项目应选定一份权威依赖声明，避免手工维护两套容易漂移的版本要求。

### 方式 A：venv + pip

以下命令在仓库根目录执行，适用于 macOS / Linux：

```bash
.venv/bin/python -m venv java_to_python/chapters/11_environments_packages/demo_project/.venv

CH11_PY="java_to_python/chapters/11_environments_packages/demo_project/.venv/bin/python"
"$CH11_PY" -m pip install \
  -r java_to_python/chapters/11_environments_packages/demo_project/requirements.txt
"$CH11_PY" -B java_to_python/chapters/11_environments_packages/demo_project/main.py
```

这里用仓库根 `.venv/bin/python` 创建子项目环境，是为了明确沿用仓库的 Python 3.14。安装和运行又使用同一个 `CH11_PY`，不会出现“装进 A、运行 B”的歧义。

预期核心输出包括：

```text
python-dotenv 版本: 1.2.3
助手名称: 文档与记录查询助手
原始 TOOL_LIMIT: '3' str
转换后的工具数量上限: 3 int
```

### 方式 B：uv

先确保终端中已有 uv；没有时可按 uv 官方安装说明安装。本章保存的锁文件由 uv 0.12.10 生成。

在仓库根目录执行：

```bash
uv lock --check --project java_to_python/chapters/11_environments_packages/demo_project
uv sync --locked --project java_to_python/chapters/11_environments_packages/demo_project
uv run --locked --project java_to_python/chapters/11_environments_packages/demo_project \
  python -B java_to_python/chapters/11_environments_packages/demo_project/main.py
```

`--project` 告诉 uv 到哪里找 `pyproject.toml`、`uv.lock` 和项目环境；它不会自动把 shell 的当前工作目录切换到那个目录，所以运行命令仍给出了从仓库根开始的脚本路径。

## 11.9 先读懂，再用仓库脚本验证

### 只读诊断：不需要安装 `python-dotenv`

```bash
# 仓库根目录
.venv/bin/python -B java_to_python/chapters/11_environments_packages/example.py
```

`example.py` 只使用标准库，不创建环境、不安装依赖。运行后重点观察：

- `sys.executable`、`sys.prefix` 与 `sys.base_prefix`；
- 根项目的 `requires-python` 和直接依赖声明；
- 当前环境的 `site-packages` 与 `sys.path`；
- 发行包元数据和 `dotenv` 模块位置；
- `python -m pip` 实际绑定到哪里。

本次改写时核对的仓库根环境是 Python 3.14.7、pip 26.2.1。换机器后路径和补丁版本可以不同，应该验证的是它们之间的对应关系，而不是照抄输出。

### 导入故障实验：需要已安装 `python-dotenv`

先完成上面的 pip 或 uv 安装，再从仓库根目录执行：

```bash
java_to_python/chapters/11_environments_packages/demo_project/.venv/bin/python -B \
  java_to_python/chapters/11_environments_packages/import_lab.py
```

`import_lab.py` 会在系统临时目录中验证三类问题：发行包名不等于导入名、包只安装在另一个环境、本地 `dotenv.py` 遮蔽正确包。实验会创建临时空环境和反例文件，并在结束时自动清理；它不会联网安装软件。

## 11.10 `ModuleNotFoundError` 的证据式排查顺序

遇到“终端能运行、IDE 不能运行”时，按下面顺序收集证据。

### 第一步：比较实际解释器

在成功入口和失败入口都打印：

```python
import sys

print(sys.executable)
print(sys.prefix)
```

路径不同，就先统一解释器，或者在真正要运行的环境中按项目声明安装依赖。

### 第二步：分别确认发行包和导入名

```text
目标解释器 -m pip show python-dotenv
目标解释器 -c 'import dotenv; print(dotenv.__file__)'
```

第一条检查安装元数据，第二条实际执行 import 并打印模块来源。请把“目标解释器”替换成真实路径，例如 `.venv/bin/python`，不要把这四个汉字直接复制到终端。

### 第三步：检查是否导入了错误文件

```python
from pathlib import Path
import sys
import dotenv

print("工作目录:", Path.cwd())
print("搜索路径:", sys.path)
print("模块来源:", dotenv.__file__)
```

如果项目里有 `dotenv.py`、`requests.py`、`json.py`、`typing.py` 等同名文件，它可能比已安装包更早被找到。此时不是“没找到模块”，而是“找到了错误模块”。正确做法是重命名自己的文件，再启动新进程验证；反复重装第三方包通常无效。

### 第四步：阅读完整 traceback

顶层包找到了，包内部仍可能因为间接依赖缺失、二进制扩展不兼容或初始化代码异常而失败。不要看到 import 失败就一律归为“没有安装”。完整 traceback 会告诉你失败发生在哪一层。

## 11.11 VS Code 中为什么尤其容易选错环境

本仓库 `.vscode/settings.json` 中有两个独立运行入口：

- Python 扩展默认指向仓库根 `.venv/bin/python`；可以通过 **Python: Select Interpreter** 选择其他环境。
- Code Runner 的 Python 命令被明确写成仓库根 `.venv/bin/python`，不会自动跟随 Python 扩展刚刚选择的解释器。

因此，即使 Python 扩展已经选择 `demo_project/.venv`，点击 Code Runner 的 Run Code 仍可能使用根环境，而根环境没有安装 `python-dotenv`。判断依据永远是程序打印的 `sys.executable`，不是状态栏看起来像选择了哪个环境。

长期在演示项目中开发时，IDE 应选择：

```text
java_to_python/chapters/11_environments_packages/demo_project/.venv/bin/python
```

## 11.12 Java 工程师最容易带过来的误判

| 误判 | 为什么错 | 正确做法 |
| --- | --- | --- |
| “版本号一样，就是同一个运行环境” | 两个 Python 3.14 可以拥有不同 `site-packages` | 比较 `sys.executable` 和 `sys.prefix` |
| “激活后所有工具都会切换环境” | 激活只修改当前 shell，已启动的 IDE 不受影响 | 在每个运行入口打印解释器路径 |
| “pip install 会更新项目声明” | pip 默认只改变当前环境 | 先维护 requirements，或使用 `uv add` |
| “pyproject 写了依赖，python 会自动安装” | 普通解释器不会执行依赖同步 | 显式运行 pip/uv 安装或同步命令 |
| “安装名就是 import 名” | 发行包和导入包使用不同命名空间 | 查官方示例，并打印模块 `__file__` |
| “pip freeze 就是设计好的锁文件” | 它只是当前环境快照，缺少直接依赖意图 | 维护声明，并在干净环境重建验证 |
| “复制 `.venv` 最省事” | 路径、解释器和二进制扩展可能与机器绑定 | 复制声明和锁，在目标机器重建 |
| “pip check 成功就说明应用没问题” | 它只检查安装元数据中的依赖关系 | 继续实际 import、运行和测试 |
| “相对路径总是相对源码文件” | `Path("x")` 默认相对当前工作目录 | 用 `Path(__file__)` 显式定位资源 |

## 11.13 版本管理与团队约定

本章的 `.gitignore` 已忽略 `.venv`、字节码、工具缓存和真实 `.env` 文件。

建议提交源码、项目选定的依赖声明、应用项目的 `uv.lock`、`.python-version`，以及不含密钥的示例配置。不要提交 `.venv/`、`__pycache__/`、下载缓存、真实 `.env`、API Key 或机器专属配置。

`.gitignore` 只影响尚未跟踪的文件。若敏感文件已经进入 Git 历史，新增忽略规则并不会自动移除它，也不能撤销泄露；应立即轮换密钥并按团队流程处理历史记录。

## 11.14 本章小结

记住下面四句话，就抓住了 Python 环境管理的主线：

1. **解释器决定环境。** 先看 `sys.executable`，再谈包是否安装。
2. **安装名不等于导入名。** `python-dotenv` 安装后使用 `dotenv`。
3. **声明、锁和安装结果是三层状态。** `pyproject.toml` 表达意图，`uv.lock` 固定解析，`.venv` 承载本机实际文件。
4. **环境应该重建，不应该复制。** 用同一个解释器完成安装和运行，并在干净环境中验证。

掌握这套思路后，后续安装 requests、Pydantic、HTTPX、FastAPI 或 pytest 时只是依赖名称发生变化，诊断方法仍然相同。
