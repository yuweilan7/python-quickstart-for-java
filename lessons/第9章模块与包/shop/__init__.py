"""商店领域包的稳定公开入口。"""

from .discounts import apply_discount
from .shipping import calculate_shipping


__all__: list[str] = ["apply_discount", "calculate_shipping"]
