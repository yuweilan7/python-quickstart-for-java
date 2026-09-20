"""第 11 章：只读诊断当前解释器、安装位置和仓库声明，不安装任何软件。"""

# 本文件按“解释器 -> 项目声明 -> 安装目录与导入路径 -> 包与模块 -> pip”的顺序收集证据。
# 所有检查都围绕当前 Python 进程展开：同一份脚本换一个解释器启动，看到的环境可能完全不同。
# 这里不会创建环境或安装依赖；唯一启动的子进程也只是执行一次只读的 pip 版本查询。

# 下列模块全来自标准库，不需要 pip 安装：
# importlib.util 查询导入名可能从哪里加载；os 读取进程环境变量；shutil 按 PATH 查命令；
# site 与 sysconfig 说明用户包开关和默认安装路径；subprocess 启动并观察子进程；
# sys 提供当前解释器、环境前缀、版本和模块搜索路径；tomllib 读取 TOML；
# importlib.metadata 读取已安装发行包的元数据；Path 以对象方式拼接和检查文件系统路径。
import importlib.util
import os
import shutil
import site
import subprocess
import sys
import sysconfig
import tomllib
from importlib import metadata
from pathlib import Path

# 独立还原后，章节目录就是本项目根；声明与环境位于 demo_project 中。
CHAPTER_DIRECTORY: Path = Path(__file__).resolve().parent
REPOSITORY_DIRECTORY: Path = CHAPTER_DIRECTORY / "demo_project"


# title: str 与 -> None 是类型提示：约定调用者传入字符串，函数只产生输出、不返回业务值；
# Python 运行时默认不会像 Java 编译器那样仅凭这些标注强制检查类型。
def print_section(title: str) -> None:
    """让终端输出可以逐段对应 README 中的诊断步骤。"""
    # f-string 会计算花括号里的 title 并嵌入字符串；开头的 \n 先留一个空行，分隔相邻章节。
    print(f"\n=== {title} ===")


# 这一段先确认“谁在执行”，因为后续看到的包、路径和 pip 都由这个解释器决定。
def show_interpreter() -> None:
    """用当前进程的信息判断环境，而不是猜测终端提示符。"""
    print_section("1. 谁在运行这份代码")
    # 不对 sys.executable 做 resolve()：环境解释器可能是指向基础解释器的符号链接。
    # 把链接解析到底，反而会丢失本次启动用的是哪一个环境路径的线索。
    print("sys.executable:", sys.executable)
    # sys.version 还包含构建信息；split() 按空白拆成列表，[0] 只取开头的版本号。
    print("Python 版本:", sys.version.split()[0])
    # 虚拟环境中，prefix 通常指向当前环境，base_prefix 指向创建它的基础 Python。
    # 二者不相等是比命令行提示符或 VIRTUAL_ENV 更直接的“当前进程在虚拟环境中”证据。
    print("sys.prefix:", sys.prefix)
    print("sys.base_prefix:", sys.base_prefix)
    print("是否处于虚拟环境:", sys.prefix != sys.base_prefix)
    # os.environ 像只含字符串键值的映射；get() 在键不存在时返回这里给定的说明文字，
    # 不会像 os.environ["VIRTUAL_ENV"] 那样抛 KeyError。直接调用 .venv/bin/python 时，
    # 解释器仍会识别虚拟环境，但这个主要由激活脚本设置的变量可以不存在。
    print("VIRTUAL_ENV:", os.environ.get("VIRTUAL_ENV", "未设置；直接调用环境解释器也能运行"))
    # cwd() 是进程的当前工作目录；CHAPTER_DIRECTORY 是源码所在目录，二者并非同一个概念。
    print("当前工作目录:", Path.cwd())
    print("脚本所在目录:", CHAPTER_DIRECTORY)
    # version_info 类似一个可按位置读取的版本记录；[:2] 取 major、minor 组成的新序列，
    # 再与二元组 (3, 14) 比较，因此 3.14 的任意补丁版本都会得到 True。
    print("是否为本章规划的 Python 3.14:", sys.version_info[:2] == (3, 14))


# 这一段把“项目声明的要求”和“机器上实际存在、当前正在使用的环境”并排展示。
# 声明文件只是可重建环境的输入；读取到 requires-python 或 dependencies 不等于已经安装。
def show_repository() -> None:
    """根据脚本位置读取仓库资料，所以切换工作目录不会读错项目。"""
    print_section("2. 仓库要求与已有 .venv")
    # Path 的 / 运算符是路径拼接，不是字符串除法；结果仍是一个 Path 对象。
    project_path: Path = REPOSITORY_DIRECTORY / "pyproject.toml"
    # with 是上下文管理器语法，代码块结束时会自动关闭文件，作用类似 Java 的
    # try-with-resources。tomllib.load() 要求二进制文件对象，所以这里使用 "rb"。
    with project_path.open("rb") as file:
        # TOML 的表会转成嵌套字典，例如 [project] 对应 configuration["project"]。
        configuration: dict[str, Any] = tomllib.load(file)
    # 方括号取必需项，缺失时抛 KeyError，能暴露仓库结构不符合本章预期；
    # get() 则用于可选项，并分别以空列表、空字典表达“当前没有声明”。
    project: Any = configuration["project"]
    print("项目文件:", project_path)
    print("项目名:", project["name"])
    print("requires-python:", project["requires-python"])
    print("直接运行依赖:", project.get("dependencies", []))
    print("开发依赖组:", configuration.get("dependency-groups", {}))

    # is_dir() 只回答仓库根目录下是否真的存在 .venv 目录，不会创建它。
    environment_path: Path = REPOSITORY_DIRECTORY / ".venv"
    print("仓库 .venv 存在:", environment_path.is_dir())
    # 两边都 resolve() 后再按路径值比较，避免相对路径或符号链接形式不同造成误判。
    # 这里比较的是当前进程的环境前缀，并不是根据 VIRTUAL_ENV 是否设置来猜测。
    print("当前进程使用仓库 .venv:", Path(sys.prefix).resolve() == environment_path.resolve())
    environment_config: Path = environment_path / "pyvenv.cfg"
    # pyvenv.cfg 是创建虚拟环境时生成的文本配置；只有文件真实存在才读取。
    if environment_config.is_file():
        # read_text() 一次读入字符串，splitlines() 再按行组成列表供 for 循环逐项处理。
        # 只显示与解释器隔离相关的字段，不执行文件中的任何内容。
        for line in environment_config.read_text(encoding="utf-8").splitlines():
            # partition("=") 总返回三部分，并按位置解包给三个名字；若没有等号，
            # separator 会是空字符串。这里只在分隔符存在且键属于白名单时输出。
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"home", "version", "include-system-site-packages"}:
                # strip() 去掉等号两侧可能存在的空白，但不会改动磁盘上的配置文件。
                print(f"  {key.strip()}: {value.strip()}")
    else:
        # else 与上面的 if 配对：缺少配置时给出诊断信息，而不是擅自修复环境。
        print("没有找到 pyvenv.cfg；本脚本只报告，不创建或修复环境。")


# 安装工具把文件放在哪里，与 import 最终按什么顺序查找，是两个相关但不同的问题。
def show_installation_paths() -> None:
    """区分默认安装目录和实际导入搜索路径。"""
    print_section("3. 依赖通常装在哪里，import 又到哪里找")
    # purelib 通常放纯 Python 包，platlib 面向平台相关扩展；在当前平台二者也可能相同。
    # get_path() 给出当前解释器安装方案的默认位置，不保证每个模块实际都来自那里。
    print("纯 Python 包默认安装位置 purelib:", sysconfig.get_path("purelib"))
    print("平台相关扩展默认安装位置 platlib:", sysconfig.get_path("platlib"))
    # ENABLE_USER_SITE 表示当前解释器是否会启用用户级 site-packages，不表示其中一定有包。
    print("用户 site-packages 是否启用:", site.ENABLE_USER_SITE)
    print("sys.path（仅观察，不修改）:")
    # sys.path 是 import 的模块搜索路径列表，前面的条目通常比后面的优先。
    # enumerate() 每轮同时给出从 0 开始的位置和路径，不必手工维护计数器。
    for position, entry in enumerate(sys.path):
        # “entry or 说明文字”利用空字符串为假值的规则，仅把空条目换成易读说明；
        # 非空路径保持原值。这里没有修改 entry，更没有修改 sys.path。
        print(f"  [{position}] {entry or '(空字符串表示当前工作目录)'}")
    print("上述安装位置是默认方案；实际模块来源仍须检查 origin 或 __file__。")


# 发行包名属于安装工具和元数据系统，模块名属于 import 系统；二者没有机械的一一映射。
# 本例先按发行包名查安装记录，再按可能的导入名查位置，让两类证据可以直接对照。
def show_distribution_and_import_names() -> None:
    """分别检查安装元数据与模块位置，避免把安装名直接当成导入名。"""
    print_section("4. 发行包元数据与可导入模块")
    # 圆括号中的字符串构成 tuple；for 每轮把其中一个发行包名绑定到 distribution_name。
    for distribution_name in ("pip", "python-dotenv", "ruff", "pyright"):
        try:
            # distribution() 读取当前解释器可见的安装元数据，不会联网查询包索引。
            distribution: Distribution = metadata.distribution(distribution_name)
        except metadata.PackageNotFoundError:
            # 只捕获“没有该发行包元数据”这一种预期情况；其他异常仍会暴露出来。
            print(f"发行包 {distribution_name}: 当前解释器未发现安装元数据")
        else:
            # try 没有抛出上面的异常才进入 else，此时 distribution 一定已经成功绑定。
            # version 来自安装元数据；locate_file("") 把空的相对路径定位到分发根目录。
            print(f"发行包 {distribution_name}: {distribution.version}")
            print("  元数据所对应的安装根目录:", distribution.locate_file(""))

    # 只查顶层模块的位置，不 import 第三方包，不触发它们的顶层业务代码。
    # find_spec 对带点的名字可能先导入父包，本例故意只使用不带点的导入名。
    for module_name in ("dotenv", "python_dotenv"):
        # 找到时返回 ModuleSpec（模块加载说明），找不到时返回 None；这一步不会帮忙安装包。
        specification: ModuleSpec | None = importlib.util.find_spec(module_name)
        # 条件表达式按“满足条件时的值 if 条件 else 否则的值”阅读。
        # 先判断 None 很重要，否则对 None 读取 .origin 会抛 AttributeError。
        origin: str | None = specification.origin if specification is not None else "未找到"
        print(f"导入名 {module_name}: {origin}")
    print("元数据存在和位置可发现都不等于实际 import 已成功；完整运行见独立实验。")


# 这一段同时观察 PATH 中的独立命令，以及“当前解释器 -m pip”的绑定关系。
# 前者可能指向别的环境；后者明确要求 sys.executable 这一个 Python 运行 pip 模块。
def show_pip_binding() -> None:
    """用同一个解释器启动 pip 的只读版本查询，且为子进程设置时间上限。"""
    print_section("5. python -m pip 与解释器的对应关系")
    # which() 只按传给当前进程的 PATH 查找可执行文件，返回路径或 None；它看不到
    # shell 自己维护的 alias/function，因此终端中还要用 type 命令补充判断。
    print("当前 shell PATH 中的 python:", shutil.which("python"))
    print("当前 shell PATH 中的 pip:", shutil.which("pip"))
    print("当前 shell PATH 中的 uv:", shutil.which("uv"))
    print("以上是 PATH 查找结果；shell 的别名或函数还需在终端用 type 检查。")
    # 参数用 list 分开保存，不经过 shell 字符串解析：第一项锁定当前解释器；-B 禁止写
    # .pyc；-m pip 让该解释器按模块运行 pip；--version 只查询版本，不执行安装。
    command: list[str] = [sys.executable, "-B", "-m", "pip", "--version"]
    print("执行的参数列表:", command)
    try:
        # capture_output=True 分别收集 stdout/stderr；text=True 把结果解码为 str；
        # timeout=10 限制等待时间；check=False 让非零退出码保存在结果中而不是抛异常。
        result: CompletedProcess[str] = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
    except subprocess.TimeoutExpired:
        # 超时是 run() 会抛出的特定异常。打印原因后 return，只结束当前函数；
        # main() 随后仍会继续输出第 6 段总结。
        print("pip 版本查询超过 10 秒，已终止子进程；请检查本地环境。")
        return
    # returncode 为 0 通常表示命令成功。stdout or stderr 优先使用非空的标准输出，
    # 否则展示标准错误；strip() 仅移除展示文本首尾的空白与换行。
    print("退出码:", result.returncode)
    print((result.stdout or result.stderr).strip())
    # 因为 check=False，普通失败不会自动成为异常，要显式检查退出码并解释常见情况。
    if result.returncode != 0:
        print("uv 创建的环境可以没有 pip；应结合错误正文判断，不要立即重装 Python。")


# main() 是流程编排层：前五个函数按顺序完成只读采集，最后给出排查优先级。
def main() -> None:
    """默认入口只收集环境信息；安装实验必须由读者显式执行 README 的命令。"""
    # Python 普通函数调用是顺序执行的；上一段完成（或正常 return）后才进入下一段。
    show_interpreter()
    show_repository()
    show_installation_paths()
    show_distribution_and_import_names()
    show_pip_binding()
    print_section("6. 下一步如何判断")
    print("先比较终端与 IDE 的 sys.executable，再比较安装元数据、模块来源和工作目录。")
    print("本次没有创建环境、修改声明、联网安装或执行第三方业务模块。")


# Python 执行脚本时把 __name__ 设为 "__main__"，所以会调用 main()；若别的模块只做
# import example，__name__ 是模块名，这个入口不会自动运行，函数定义仍可被单独复用。
if __name__ == "__main__":
    main()
