"""第 9 章：观察模块对象、导入缓存、包和 ``__name__`` 入口守卫。"""

import importlib
import math
import subprocess
import sys
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pricing
from pricing import calculate_subtotal
from shop import apply_discount, calculate_shipping


def print_section(title: str) -> None:
    """让终端输出与教程小节对应，便于逐段观察。"""
    print(f"\n--- {title} ---")


def demonstrate_import_styles() -> None:
    """对比模块导入和成员导入，并展示模块是运行时对象。"""
    print_section("1. 导入方式与模块对象")

    # `import math` 保留模块前缀，调用处可以直接看出 sqrt 来自哪里。
    print("math.sqrt(81):", math.sqrt(81))
    print("math 是模块对象:", isinstance(math, ModuleType))

    # `from collections import Counter` 把成员直接绑定到当前模块。
    # Counter 高频且含义明确，直接导入能让业务表达更紧凑。
    order_statuses: list[str] = ["paid", "pending", "paid", "cancelled", "paid"]
    print("订单状态计数:", Counter(order_statuses))


def demonstrate_custom_module() -> None:
    """调用无导入副作用的自定义模块，并观察它的命名空间。"""
    print_section("2. 自定义模块与公开 API")

    total: Decimal = pricing.calculate_total(
        Decimal("19.90"),
        3,
        tax_rate=Decimal("0.06"),
    )
    print("模块名:", pricing.__name__)
    print("公开 API:", pricing.__all__)
    print("模块导入调用小计:", pricing.calculate_subtotal(Decimal("19.90"), 3))
    print("成员导入调用小计:", calculate_subtotal(Decimal("19.90"), 3))
    print("含税总价:", pricing.format_cny(total))

    # 类型提示不会在运行时自动校验参数；真正阻止负数的是函数内的条件判断。
    try:
        pricing.calculate_total(
            Decimal("-1.00"),
            1,
            tax_rate=Decimal("0.06"),
        )
    except ValueError as error:
        print("错误路径:", error)


def demonstrate_import_cache() -> None:
    """证明重复导入通常复用 ``sys.modules`` 中的同一模块对象。"""
    print_section("3. 导入缓存")

    loaded_again: ModuleType = importlib.import_module("pricing")
    print("重复导入是同一对象:", loaded_again is pricing)
    print("sys.modules 保存该对象:", sys.modules["pricing"] is pricing)


def demonstrate_package() -> None:
    """通过包的稳定入口组合折扣与计价模块。"""
    print_section("4. 包与相对导入")

    list_price = Decimal("19.90")
    discounted_price: Decimal = apply_discount(list_price, Decimal("0.10"))
    total: Decimal = pricing.calculate_total(
        discounted_price,
        3,
        tax_rate=Decimal("0.06"),
    )
    print("原单价:", pricing.format_cny(list_price))
    print("九折单价:", pricing.format_cny(discounted_price))
    print("折后含税总价:", pricing.format_cny(total))
    print("通过 shop 稳定入口计算运费:", pricing.format_cny(calculate_shipping(total)))

    # 教学反例会被窄范围捕获，因此整份脚本仍能继续执行。
    try:
        apply_discount(list_price, Decimal("0.80"))
    except ValueError as error:
        print("折扣边界:", error)


def demonstrate_readme_exercises() -> None:
    """运行模块拆分、导入副作用、包入口和名称遮蔽练习。"""
    print_section("5. README 小练习")

    print("练习 1：calculate_subtotal 已加入 pricing.__all__")
    print("  模块前缀能看出来源:", pricing.calculate_subtotal(Decimal("10.00"), 3))
    print("  成员导入调用更短:", calculate_subtotal(Decimal("10.00"), 3))

    print("练习 2：税额模块的单向依赖")
    print("  依赖箭头: example.py -> pricing.py -> tax.py")
    print("  tax.py 不导入 pricing.py，calculate_total 复用 calculate_subtotal 和 tax")

    print("练习 3：顶层代码、导入缓存与入口守卫")
    temporary_module_name = "_chapter09_import_once_demo"
    with tempfile.TemporaryDirectory(prefix="python-module-") as directory:
        module_path: Path = Path(directory) / f"{temporary_module_name}.py"
        module_path.write_text('print("  临时模块顶层代码执行")\n', encoding="utf-8")
        sys.path.insert(0, directory)
        try:
            first_import: ModuleType = importlib.import_module(temporary_module_name)
            second_import: ModuleType = importlib.import_module(temporary_module_name)
        finally:
            sys.path.remove(directory)
            sys.modules.pop(temporary_module_name, None)
        print("  连续导入复用同一对象:", first_import is second_import)

    chapter_directory: Path = Path(__file__).resolve().parent
    imported_process: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-B", "-c", "import example"],
        cwd=chapter_directory,
        check=True,
        capture_output=True,
        text=True,
    )
    print("  子进程只 import example 的输出:", repr(imported_process.stdout))
    print("  当前直接运行会继续输出，因为 __name__ == '__main__'")

    print("练习 4：shop 包只暴露稳定入口")
    for amount in (Decimal("99.99"), Decimal("100.00")):
        shipping_fee: Decimal = calculate_shipping(amount)
        print(f"  订单 {amount:.2f} 元 -> 运费 {shipping_fee:.2f} 元")

    print("练习 5：临时 decimal.py 造成标准库名称遮蔽")
    with tempfile.TemporaryDirectory(prefix="python-shadow-") as directory:
        shadow_path: Path = Path(directory) / "decimal.py"
        shadow_path.write_text("# 故意遮蔽标准库，但没有 Decimal\n", encoding="utf-8")
        shadowed_process: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, "-B", "-c", "from decimal import Decimal"],
            cwd=directory,
            capture_output=True,
            text=True,
        )
        error_lines: list[str] = [line for line in shadowed_process.stderr.splitlines() if line]
        print("  导入失败:", shadowed_process.returncode != 0)
        print("  错误末行:", error_lines[-1])
    print("  临时遮蔽文件已清理:", not shadow_path.exists())


def main() -> None:
    demonstrate_import_styles()
    demonstrate_custom_module()
    demonstrate_import_cache()
    demonstrate_package()
    demonstrate_readme_exercises()

    print_section("6. __name__ 入口守卫")
    print("当前模块名:", __name__)
    print("只有直接运行 example.py 时才会调用 main()")


if __name__ == "__main__":
    main()
