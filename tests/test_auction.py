"""拍卖行与竞价系统 - 红态测试"""
import pytest, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from auction.house import AuctionHouse, AuctionItem

class TestGoldValidation:
    def test_cannot_bid_more_than_gold(self):
        ah = AuctionHouse()
        ah.items["i1"] = AuctionItem("i1", "sword", "seller", current_bid=10, buyout_price=100)
        ah.gold["buyer"] = 5
        ok, msg = ah.place_bid("i1", "buyer", 50)
        assert ok == False  # bug1: 金币不足也成功

class TestBuyoutAtomic:
    def test_concurrent_buyout_only_one_succeeds(self):
        ah = AuctionHouse()
        ah.items["i1"] = AuctionItem("i1", "sword", "seller", current_bid=10, buyout_price=100, stock=1, version=0)
        ah.gold = {"b1": 200, "b2": 200}
        r1 = ah.buyout("i1", "b1")
        r2 = ah.buyout("i1", "b2")
        assert not (r1[0] and r2[0])  # bug2: 两个都成功

class TestExpireRefund:
    def test_expired_item_returned_to_seller(self):
        ah = AuctionHouse()
        ah.items["i1"] = AuctionItem("i1", "sword", "seller1", current_bid=10, buyout_price=100)
        ah.expire_item("i1")
        # 应该有返还记录
        assert hasattr(ah, "returned_items")
        assert "i1" in ah.returned_items  # bug3: 不返还

class TestMinIncrement:
    def test_increment_scales_with_price(self):
        ah = AuctionHouse()
        inc1 = ah.get_min_increment(10)
        inc1000 = ah.get_min_increment(1000)
        assert inc1000 > inc1  # bug4: 都是1

class TestTaxRefund:
    def test_outbid_tax_refunded(self):
        ah = AuctionHouse()
        ah.items["i1"] = AuctionItem("i1", "sword", "seller", current_bid=10, buyout_price=100)
        ah.gold = {"b1": 1000, "b2": 1000}
        ah.place_bid("i1", "b1", 100)
        tax_paid = ah.tax_paid.get("b1", 0)
        ah.place_bid("i1", "b2", 200)  # b1被超
        refund = ah.refund_tax("b1", "i1")
        assert refund == tax_paid  # bug5: 不退税=0

class TestSearchPagination:
    def test_search_supports_pagination(self):
        ah = AuctionHouse()
        for i in range(100):
            ah.items[f"i{i}"] = AuctionItem(f"i{i}", f"sword_{i}", "seller", 10, 100)
        page1 = ah.search("sword", page=1, page_size=20)
        page2 = ah.search("sword", page=2, page_size=20)
        assert len(page1) == 20
        assert len(page2) == 20
        assert page1[0].iid != page2[0].iid  # bug6: 无分页返回全部
