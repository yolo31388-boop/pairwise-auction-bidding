"""拍卖行与竞价系统。

修复点：
1. 竞价前在锁内原子检查金币，不足直接拒绝（中标金额进入托管 escrow）。
2. 一口价使用版本号乐观锁(CAS)，并发下库存只够一个成交。
3. 到期未售出物品通过邮件返还卖家并记录日志。
4. 最低加价按当前价比例计算，且有保底加价。
5. 交易税仅在成交时扣除，竞价被超后托管金币全额退还。
6. 搜索支持页码/游标分页并带缓存，数据变更后缓存失效。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("auction.house")


@dataclass
class AuctionItem:
    iid: str
    name: str
    seller: str
    current_bid: int
    buyout_price: int
    stock: int = 1
    version: int = 0  # 一口价乐观锁版本号
    expires_at: float = 0
    highest_bidder: Optional[str] = None  # 当前最高出价者；None 表示无人出价


class SearchResults(list):
    """带游标信息的搜索结果，行为与普通 list 完全一致。"""

    def __init__(self, items, next_cursor: Optional[str] = None, has_more: bool = False):
        super().__init__(items)
        self.next_cursor = next_cursor
        self.has_more = has_more


class AuctionHouse:
    # 乐观锁冲突时的重试上限
    _CAS_RETRIES = 10

    def __init__(
        self,
        tax_rate: float = 0.05,
        increment_rate: float = 0.05,
        min_increment: int = 1,
    ):
        self.items: dict[str, AuctionItem] = {}
        self.gold: dict[str, int] = {}  # player -> 可用金币
        self.tax_rate: float = tax_rate
        self.increment_rate: float = increment_rate
        self.min_increment_floor: int = min_increment
        # 交易税账本：仅在成交时写入（按物品记录）
        self.tax_paid: dict[str, int] = {}
        self.tax_total: int = 0

        # 出价托管：(bidder, iid) -> 冻结的出价金额
        self._escrow: dict[tuple[str, str], int] = {}
        # 竞价被超后已退还的税款账本（成交前不收税，正常为 0）
        self._tax_refunded: dict[tuple[str, str], int] = {}

        # 到期返还 / 邮件 / 成交 / 日志
        self.returned_items: dict[str, AuctionItem] = {}
        self.mail: dict[str, list[dict]] = {}
        self.sales: list[dict] = []
        self.event_log: list[dict] = []

        # 搜索缓存：keyword -> (cache_version, len(items), 匹配的 iid 列表)
        self._search_cache: dict[str, tuple[int, int, list[str]]] = {}
        self._cache_version: int = 0

        # 竞价/结算全局锁，保证“检查 + 扣款 + 落账”原子完成
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _log(self, event: str, **detail) -> None:
        record = {"event": event, **detail}
        self.event_log.append(record)
        logger.info("%s %s", event, detail)

    def _bump_search_cache_locked(self) -> None:
        self._cache_version += 1
        self._search_cache.clear()

    def invalidate_search_cache(self) -> None:
        with self._lock:
            self._bump_search_cache_locked()

    def _send_mail_locked(self, player: str, payload: dict) -> None:
        self.mail.setdefault(player, []).append(payload)

    def _settle_locked(self, iid: str, seller: str, payer: str, amount: int) -> int:
        """成交结算：只在此时扣交易税，税后款项给卖家。"""
        tax = int(amount * self.tax_rate)
        proceeds = amount - tax
        self.gold[seller] = self.gold.get(seller, 0) + proceeds
        self.tax_paid[iid] = self.tax_paid.get(iid, 0) + tax
        self.tax_total += tax
        sale = {
            "iid": iid,
            "seller": seller,
            "buyer": payer,
            "amount": amount,
            "tax": tax,
            "proceeds": proceeds,
        }
        self.sales.append(sale)
        return tax

    # ------------------------------------------------------------------ #
    # Bug 1 / 4 / 5：竞价
    # ------------------------------------------------------------------ #
    def place_bid(self, iid: str, bidder: str, amount: int) -> tuple[bool, str]:
        if not isinstance(amount, int) or amount <= 0:
            return False, "invalid amount"

        with self._lock:
            item = self.items.get(iid)
            if item is None:
                return False, "item not found"
            if item.stock <= 0:
                return False, "item sold out"

            # Bug 4：最低可接受价 = 当前价 + 按比例的最低加价
            minimum = item.current_bid + self.get_min_increment(item.current_bid)
            if amount < minimum:
                return False, f"bid must be at least {minimum}"

            previous_bidder = item.highest_bidder
            # 同一出价者抬价时，其旧出价已在托管中，只需补差额
            own_escrow = 0
            if previous_bidder == bidder:
                own_escrow = self._escrow.pop((bidder, iid), 0)

            needed = amount - own_escrow
            available = self.gold.get(bidder, 0)

            # Bug 1：锁内原子检查金币，不足直接拒绝
            if available < needed:
                if own_escrow:
                    self._escrow[(bidder, iid)] = own_escrow
                return False, "not enough gold"

            # Bug 5：竞价被超，旧出价者的托管金币全额退还（不含税）
            if previous_bidder is not None and previous_bidder != bidder:
                refunded = self._escrow.pop((previous_bidder, iid), 0)
                if refunded:
                    self.gold[previous_bidder] = (
                        self.gold.get(previous_bidder, 0) + refunded
                    )
                    self._log(
                        "bid_outbid_refund",
                        iid=iid,
                        bidder=previous_bidder,
                        refund=refunded,
                    )

            # 冻结本次出价（成交前不扣税）
            self.gold[bidder] = available - needed
            self._escrow[(bidder, iid)] = amount

            item.current_bid = amount
            item.highest_bidder = bidder
            self._log("bid_placed", iid=iid, bidder=bidder, amount=amount)
            return True, "bid placed"

    def refund_tax(self, bidder: str, iid: str) -> int:
        """退还某出价者在某物品上被预扣的税款。

        新规则下税款只在成交时扣除，竞价阶段税额为 0；
        被超时托管的出价金额已在 place_bid 中全额退还。
        """
        with self._lock:
            return self._tax_refunded.pop((bidder, iid), 0)

    # ------------------------------------------------------------------ #
    # Bug 2：一口价乐观锁 + 库存版本号
    # ------------------------------------------------------------------ #
    def buyout(self, iid: str, buyer: str) -> tuple[bool, str]:
        item = self.items.get(iid)
        if item is None:
            return False, "item not found"

        price = item.buyout_price

        for _ in range(self._CAS_RETRIES):
            # 乐观读取：快照版本号与库存（不持锁）
            with self._lock:
                version = item.version
                stock = item.stock

            if stock <= 0:
                return False, "out of stock"
            if self.gold.get(buyer, 0) < price:
                return False, "not enough gold"

            with self._lock:
                # CAS：版本号或库存被其他线程改过则本轮回滚重试
                if item.version != version or item.stock != stock:
                    continue
                # 锁内再次确认，杜绝双花和超卖
                if item.stock <= 0:
                    return False, "out of stock"
                if self.gold.get(buyer, 0) < price:
                    return False, "not enough gold"

                # 一口价成交：退还原竞价者托管，买卖双方交割并扣税
                previous_bidder = item.highest_bidder
                if previous_bidder is not None and previous_bidder != buyer:
                    refunded = self._escrow.pop((previous_bidder, iid), 0)
                    if refunded:
                        self.gold[previous_bidder] = (
                            self.gold.get(previous_bidder, 0) + refunded
                        )

                self.gold[buyer] = self.gold.get(buyer, 0) - price
                if buyer == previous_bidder:
                    self._escrow.pop((buyer, iid), 0)
                tax = self._settle_locked(iid, item.seller, buyer, price)

                item.stock -= 1
                item.version += 1
                item.highest_bidder = None
                self._bump_search_cache_locked()
                self._log(
                    "buyout",
                    iid=iid,
                    buyer=buyer,
                    price=price,
                    tax=tax,
                    version=item.version,
                )
                return True, "bought"

        return False, "busy, please retry"

    # ------------------------------------------------------------------ #
    # Bug 3：到期处理
    # ------------------------------------------------------------------ #
    def expire_item(self, iid: str) -> tuple[bool, str]:
        with self._lock:
            item = self.items.pop(iid, None)
            if item is None:
                return False, "not found"

            winner = item.highest_bidder
            if winner is not None:
                # 已有中标者：托管金币结算给卖家，仅此时扣税
                amount = self._escrow.pop((winner, iid), 0) or item.current_bid
                tax = self._settle_locked(iid, item.seller, winner, amount)
                self._bump_search_cache_locked()
                self._log(
                    "expired_sold",
                    iid=iid,
                    seller=item.seller,
                    buyer=winner,
                    amount=amount,
                    tax=tax,
                )
                return True, "sold"

            # 无人出价：物品通过邮件自动返还卖家
            self.returned_items[iid] = item
            payload = {
                "type": "auction_return",
                "iid": iid,
                "name": item.name,
                "stock": item.stock,
                "reason": "auction expired, unsold",
            }
            self._send_mail_locked(item.seller, payload)
            self._bump_search_cache_locked()
            self._log(
                "expired_returned",
                iid=iid,
                seller=item.seller,
                name=item.name,
                stock=item.stock,
                mail=payload,
            )
            return True, "returned to seller"

    # ------------------------------------------------------------------ #
    # Bug 4：按比例的最低加价（含保底）
    # ------------------------------------------------------------------ #
    def get_min_increment(self, current_price: int) -> int:
        return max(
            self.min_increment_floor,
            math.ceil(current_price * self.increment_rate),
        )

    # ------------------------------------------------------------------ #
    # Bug 6：分页 + 游标 + 缓存搜索
    # ------------------------------------------------------------------ #
    @staticmethod
    def _encode_cursor(keyword: str, offset: int) -> str:
        raw = json.dumps({"k": keyword, "o": offset}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, int]:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return data["k"], int(data["o"])

    def _search_index(self, keyword: str, refresh: bool) -> list[str]:
        with self._lock:
            version = self._cache_version
            size = len(self.items)
            hit = self._search_cache.get(keyword)
            if not refresh and hit is not None and hit[0] == version and hit[1] == size:
                return hit[2]
            iids = sorted(
                iid
                for iid, item in self.items.items()
                if keyword in item.name and item.stock > 0
            )
            self._search_cache[keyword] = (version, size, iids)
            return iids

    def search(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 50,
        cursor: Optional[str] = None,
        refresh: bool = False,
    ) -> SearchResults:
        if page_size <= 0:
            raise ValueError("page_size must be positive")

        if cursor is not None:
            cursor_keyword, start = self._decode_cursor(cursor)
            if cursor_keyword != keyword:
                raise ValueError("cursor does not match keyword")
        else:
            if page < 1:
                raise ValueError("page must be >= 1")
            start = (page - 1) * page_size

        iids = self._search_index(keyword, refresh=refresh)
        total = len(iids)
        chunk = iids[start:start + page_size]
        results = SearchResults(
            [self.items[iid] for iid in chunk],
            has_more=start + len(chunk) < total,
        )
        if results.has_more:
            results.next_cursor = self._encode_cursor(keyword, start + len(chunk))
        return results
