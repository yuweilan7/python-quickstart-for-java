"""显式运行的离线排错实验：当前环境须已安装 python-dotenv，临时资源自动清理。"""

import subprocess
import sys
import tempfile
from pathlib import Path

GOOD_IMPORT = """
import sys
import dotenv
from importlib.metadata import version
print("解释器:", sys.executable)
print("发行包版本:", version("python-dotenv"))
print("模块来源:", dotenv.__file__)
"""


def run_probe(
    interpreter: str,
    code: str,
    directory: Path,
    *,
    expected_error: str | None = None,
    isolated: bool = True,
) -> None:
    """每次使用新进程，让导入缓存与反例文件不会影响下一次检查。"""
    # -I 排除当前目录、用户包目录及 PYTHON* 环境变量对导入的干扰。
    # 名称遮蔽实验改用 -E -s，保留 -c 启动时的当前目录搜索入口。
    flags: list[str] = ["-I"] if isolated else ["-E", "-s"]
    result: CompletedProcess[str] = subprocess.run(
        [interpreter, "-B", *flags, "-c", code],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    print(result.stdout.strip())
    print("退出码:", result.returncode)
    if expected_error is None:
        if result.returncode != 0:
            raise RuntimeError(f"预期导入成功，却失败: \n{result.stderr}")
    else:
        if result.returncode == 0 or expected_error not in result.stderr:
            raise RuntimeError(f"未出现预期的 {expected_error!r}:\n{result.stderr}")
        print("已观察到预期错误:", result.stderr.strip().splitlines()[-1])


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="chapter11-import-") as directory:
        root = Path(directory)
        print("\n=== 1. 环境 A 已安装，实际导入成功 ===")
        run_probe(sys.executable, GOOD_IMPORT, root)

        print("\n=== 2. 反例：把发行包名机械地改成下划线作为导入名 ===")
        run_probe(
            sys.executable,
            "import python_dotenv",
            root,
            expected_error="No module named 'python_dotenv'",
        )

        print("\n=== 3. 反例：安装在 A，却用空环境 B 运行 ===")
        empty_environment: Path = root / "empty-environment"
        # B 只负责证明环境隔离，不负责安装，所以不需要 pip；创建过程不联网。
        subprocess.run(
            [sys.executable, "-B", "-m", "venv", "--without-pip", str(empty_environment)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        executable_parts: tuple[Literal["bin"], Literal["python"]] = (
            ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
        )
        empty_python: Path = empty_environment.joinpath(*executable_parts)
        run_probe(
            str(empty_python),
            "import sys; print('解释器:', sys.executable); import dotenv",
            root,
            expected_error="No module named 'dotenv'",
        )

        print("\n=== 4. 反例：同一环境里，本地同名文件遮蔽了正确的包 ===")
        shadow_directory: Path = root / "shadow"
        shadow_directory.mkdir()
        (shadow_directory / "dotenv.py").write_text(
            'SOURCE = "这是反例文件，不是已安装的 dotenv 包"\n', encoding="utf-8"
        )
        run_probe(
            sys.executable,
            "import dotenv; print('实际来源:', dotenv.__file__); from dotenv import dotenv_values",
            shadow_directory,
            expected_error="cannot import name 'dotenv_values'",
            isolated=False,
        )

        print("\n=== 5. 回到无同名文件的目录，环境 A 再次导入成功 ===")
        run_probe(sys.executable, GOOD_IMPORT, root)

    print("临时空环境与反例文件已清理:", not root.exists())


if __name__ == "__main__":
    main()
