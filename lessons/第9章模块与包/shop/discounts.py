"""折扣计算：演示包内模块如何通过相对导入协作。"""

from decimal import ROUND_HALF_UP, Decimal

from .rules import MAX_DISCOUNT_RATE


_CENT = Decimal("0.01")


def apply_discount(unit_price: Decimal, discount_rate: Decimal) -> Decimal:
    """返回折后单价。

    为了让示例包含可观察的错误边界，本商店最多允许五折优惠。实际项目中的规则通常来自
    配置或领域对象，而不是散落在函数中的魔法数字。
    """
    if unit_price < 0:
        raise ValueError("商品单价不能为负")
    if not Decimal("0") <= discount_rate <= MAX_DISCOUNT_RATE:
        raise ValueError(f"折扣率必须在 0 到 {MAX_DISCOUNT_RATE} 之间")

    discounted_price: Decimal = unit_price * (Decimal("1") - discount_rate)
    return discounted_price.quantize(_CENT, ROUND_HALF_UP)
