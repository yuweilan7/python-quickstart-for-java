"""可复用的价格模块。

模块顶层只创建常量和函数，不打印、不读取文件，也不连接外部服务。因此其他代码可以安全地
导入本模块，而不会意外启动演示或产生外部副作用。
"""

from decimal import ROUND_HALF_UP, Decimal

import tax


__all__: list[str] = ["calculate_subtotal", "calculate_total", "format_cny"]

CENT = Decimal("0.01")


def calculate_subtotal(unit_price: Decimal, quantity: int) -> Decimal:
    """计算税前小计；该公开函数同时被 calculate_total 复用。"""
    if unit_price < 0 or quantity < 0:
        raise ValueError("价格和数量不能为负")
    return (unit_price * quantity).quantize(CENT, ROUND_HALF_UP)


def calculate_total(
    unit_price: Decimal,
    quantity: int,
    *,
    tax_rate: Decimal,
) -> Decimal:
    """计算含税总价，并按人民币分使用四舍五入取整。

    ``tax_rate`` 是仅限关键字参数，让调用处明确写出税率含义。类型提示服务于阅读和静态检查，
    运行时的非负约束仍由函数体显式维护。
    """
    if tax_rate < 0:
        raise ValueError("税率不能为负")

    subtotal: Decimal = calculate_subtotal(unit_price, quantity)
    tax_amount: Decimal = tax.calculate_tax(subtotal, tax_rate)
    return subtotal + tax_amount


def format_cny(amount: Decimal) -> str:
    """把金额格式化为带人民币符号和两位小数的文本。"""
    return f"¥{amount:.2f}"
