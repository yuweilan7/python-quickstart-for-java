"""税额计算模块；依赖方向由 pricing 指向这里，不反向导入 pricing。"""

from decimal import ROUND_HALF_UP, Decimal


CENT = Decimal("0.01")


def calculate_tax(subtotal: Decimal, tax_rate: Decimal) -> Decimal:
    """按税前小计和税率计算税额，并四舍五入到分。"""
    if subtotal < 0 or tax_rate < 0:
        raise ValueError("税前小计和税率不能为负")
    return (subtotal * tax_rate).quantize(CENT, ROUND_HALF_UP)
