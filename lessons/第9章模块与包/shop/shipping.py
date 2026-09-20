"""运费规则模块；调用者应通过 shop 包入口使用公开函数。"""

from decimal import Decimal


FREE_SHIPPING_THRESHOLD = Decimal("100.00")
STANDARD_SHIPPING_FEE = Decimal("12.00")


def calculate_shipping(order_amount: Decimal) -> Decimal:
    """满 100 元免运费，否则收取 12 元。"""
    if order_amount < 0:
        raise ValueError("订单金额不能为负")
    if order_amount >= FREE_SHIPPING_THRESHOLD:
        return Decimal("0.00")
    return STANDARD_SHIPPING_FEE
