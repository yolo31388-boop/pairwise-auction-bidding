"""拍卖行与竞价系统"""
import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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
    highest_bidder: str = ""


class AuctionHouse:
    MIN_INCREMENT_FLOOR = 1      # 最低加价
    INCREMENT_RATIO = 0.05       # 按当前价比例加价
    SEARCH_CACHE_TTL = 5.0       # 搜索缓存秒数

    def __init__(self):
        self.items: dict[str, AuctionItem] = {}
        self.gold: dict[str, int] = {}  # player -> gold
        self.tax_rate: float = 0.05
        self.tax_paid: dict[str, int] = {}  # 竞价时预扣的税(被超/流拍全额退还)
        self.locked_gold: dict[str, int] = {}  # 竞价托管锁定的金币
        self.returned_items: dict[str, str] = {}  # iid -> seller 到期返还记录
        self.mailbox: dict[str, list] = {}  # seller -> 返还邮件
        self._search_cache: dict[tuple, tuple[float, list]] = {}
        self._lock = threading.RLock()

    def place_bid(self, iid: str, bidder: str, amount: int) -> tuple[bool, str]:
        with self._lock:
            item = self.items.get(iid)
            if item is None:
                return False, "not found"
            if amount <= item.current_bid:
                return False, "too low"
            if amount < item.current_bid + self.get_min_increment(item.current_bid):
                return False, "below min increment"
            tax = int(amount * self.tax_rate)
            # 原子检查并锁定金币(出价+预扣税)
            if self.gold.get(bidder, 0) < amount + tax:
                return False, "insufficient gold"
            # 全额退还上一出价者的托管金币与预扣税
            prev = item.highest_bidder
            if prev:
                self.locked_gold[prev] = self.locked_gold.get(prev, 0) - item.current_bid
                self.gold[prev] = self.gold.get(prev, 0) + item.current_bid
            self.gold[bidder] -= tax  # 预扣税,成交时结算,被超时退还
            self.tax_paid[bidder] = self.tax_paid.get(bidder, 0) + tax
            self.gold[bidder] -= amount
            self.locked_gold[bidder] = self.locked_gold.get(bidder, 0) + amount
            item.current_bid = amount
            item.highest_bidder = bidder
            return True, "bid placed"

    def buyout(self, iid: str, buyer: str) -> tuple[bool, str]:
        with self._lock:
            item = self.items.get(iid)
            if item is None:
                return False, "not found"
            # 乐观锁:读取版本号,校验库存后原子更新
            version = item.version
            if item.stock <= 0:
                return False, "out of stock"
            if self.gold.get(buyer, 0) < item.buyout_price:
                return False, "no gold"
            if item.version != version:  # 版本被并发修改则失败
                return False, "version conflict"
            item.stock -= 1
            item.version += 1
            self.gold[buyer] -= item.buyout_price
            tax = int(item.buyout_price * self.tax_rate)  # 税只在成交时扣
            self.gold[item.seller] = self.gold.get(item.seller, 0) + item.buyout_price - tax
            return True, "bought"

    def expire_item(self, iid: str) -> tuple[bool, str]:
        with self._lock:
            item = self.items.pop(iid, None)
            if item is None:
                return False, "not found"
            # 到期未卖出:邮件返还卖家并记录日志
            self.returned_items[iid] = item.seller
            self.mailbox.setdefault(item.seller, []).append(
                {"iid": iid, "name": item.name, "stock": item.stock,
                 "reason": "expired", "time": time.time()})
            # 有最高出价者时退还其托管金币与预扣税
            if item.highest_bidder:
                self.locked_gold[item.highest_bidder] = \
                    self.locked_gold.get(item.highest_bidder, 0) - item.current_bid
                self.gold[item.highest_bidder] = \
                    self.gold.get(item.highest_bidder, 0) + item.current_bid
                self.refund_tax(item.highest_bidder, iid)
            logger.info("auction %s (%s) expired, returned to seller %s",
                        iid, item.name, item.seller)
            return True, "expired"

    def get_min_increment(self, current_price: int) -> int:
        # 按当前价比例计算,且有最低加价
        return max(self.MIN_INCREMENT_FLOOR, int(current_price * self.INCREMENT_RATIO))

    def refund_tax(self, bidder: str, iid: str) -> int:
        with self._lock:
            # 竞价被超/流拍:预扣税全额退还
            tax = self.tax_paid.get(bidder, 0)
            if tax:
                self.tax_paid[bidder] = 0
                self.gold[bidder] = self.gold.get(bidder, 0) + tax
            return tax

    def search(self, keyword: str, page: int = 1, page_size: int = 50,
               cursor: str = "") -> list:
        # 游标分页:cursor 为上一页最后一个 iid,传空串从头开始
        cache_key = (keyword, page_size)
        with self._lock:
            cached = self._search_cache.get(cache_key)
            if cached and time.time() - cached[0] < self.SEARCH_CACHE_TTL:
                matched = cached[1]
            else:
                matched = sorted(
                    (item for item in self.items.values() if keyword in item.name),
                    key=lambda it: it.iid)
                self._search_cache[cache_key] = (time.time(), matched)
        if cursor:
            start = next((i + 1 for i, it in enumerate(matched) if it.iid == cursor), 0)
        else:
            start = (max(page, 1) - 1) * page_size
        return matched[start:start + page_size]
