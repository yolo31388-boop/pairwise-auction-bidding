"""拍卖行与竞价系统 - 含6个bug"""
from dataclasses import dataclass, field

@dataclass
class AuctionItem:
    iid: str
    name: str
    seller: str
    current_bid: int
    buyout_price: int
    stock: int = 1
    version: int = 0  # 乐观锁版本号
    expires_at: float = 0

class AuctionHouse:
    def __init__(self):
        self.items: dict[str, AuctionItem] = {}
        self.gold: dict[str, int] = {}  # player -> gold
        self.tax_rate: float = 0.05
        self.tax_paid: dict[str, int] = {}  # bug5: 竞价时扣税

    def place_bid(self, iid: str, bidder: str, amount: int) -> tuple[bool, str]:
        # bug1: 不检查金币
        item = self.items[iid]
        if amount <= item.current_bid:
            return False, "too low"
        # bug5: 竞价时扣税
        tax = int(amount * self.tax_rate)
        self.tax_paid[bidder] = self.tax_paid.get(bidder, 0) + tax
        self.gold[bidder] = self.gold.get(bidder, 0) - tax
        item.current_bid = amount
        return True, "bid placed"

    def buyout(self, iid: str, buyer: str) -> tuple[bool, str]:
        # bug2: 无库存原子校验
        item = self.items[iid]
        if self.gold.get(buyer, 0) < item.buyout_price:
            return False, "no gold"
        item.stock -= 1
        self.gold[buyer] -= item.buyout_price
        return True, "bought"

    def expire_item(self, iid: str) -> tuple[bool, str]:
        # bug3: 不返还卖家
        if iid in self.items:
            del self.items[iid]
            return True, "expired"
        return False, "not found"

    def get_min_increment(self, current_price: int) -> int:
        # bug4: 固定1金币
        return 1

    def refund_tax(self, bidder: str, iid: str) -> int:
        # bug5: 竞价被超不退税
        return 0

    def search(self, keyword: str, page: int = 1, page_size: int = 50) -> list:
        # bug6: 无分页
        return [item for item in self.items.values() if keyword in item.name]
