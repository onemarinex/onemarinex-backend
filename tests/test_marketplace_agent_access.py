"""The RFQ/quote/order marketplace treated "agent" as a superuser.

Five branches, each with a comment admitting it was a placeholder:

    elif me.role == "agent":
        # Agents see all; change if needed
    # agent allowed (or restrict if needed)
    # (allow agent if you want them to see on behalf; remove role check if not desired)

An Order carries line items, shipping cost, discount, tax and grand total, so
any authenticated agent could read every shipping company's negotiated pricing
with every vendor across all tenants, read every sealed vendor bid on any RFQ,
and — through the status route — cancel anyone's order.

Neither Order nor RFQ carries an agency key, so there is nothing to scope an
agent *to*. These pin the fail-closed state until someone decides the rule,
and they are what should fail when that rule arrives, so it cannot be added by
widening the hole again.

Runs inside a transaction that is always rolled back.
"""

import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1 import routes_orders, routes_quotes
from app.db.models.order import Order
from app.db.models.rfq import RFQ
from app.db.models.user import User
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class MarketplaceAgentAccessTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.buyer = self.make_user("shipping_company")
        self.vendor = self.make_user("vendor")
        self.agent = self.make_user("agent")

        self.rfq = RFQ(
            title="Provisions", buyer_company="Alpha Shipping",
            port="Visakhapatnam", user_id=self.buyer.id,
        )
        self.db.add(self.rfq)
        self.db.flush()

        self.order = Order(
            order_number=_uniq("ORD"), rfq_id=self.rfq.id,
            buyer_user_id=self.buyer.id, vendor_user_id=self.vendor.id,
            vendor_company="Vendor Co", port="Visakhapatnam",
            items=[{"name": "Rice", "quantity": 50, "unit_price": 10, "line_total": 500}],
            grand_total=12345,
        )
        self.db.add(self.order)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_user(self, role):
        user = User(email=_uniq(role) + "@example.com", hashed_password="x", role=role)
        self.db.add(user)
        self.db.flush()
        return SimpleNamespace(id=user.id, role=role)

    # --- the buyer and vendor are unaffected ---

    def test_the_buyer_still_sees_their_own_order(self):
        rows = routes_orders.list_orders(status=None, db=self.db, me=self.buyer)
        self.assertEqual([r.order_number for r in rows], [self.order.order_number])

    def test_the_vendor_still_sees_their_own_order(self):
        rows = routes_orders.list_orders(status=None, db=self.db, me=self.vendor)
        self.assertEqual(len(rows), 1)

    def test_the_rfq_owner_still_sees_its_quotes(self):
        self.assertEqual(
            routes_quotes.list_quotes_for_rfq(rfq_id=self.rfq.id, db=self.db, me=self.buyer),
            [],
        )

    # --- the agent sees nothing ---

    def test_an_agent_lists_no_orders(self):
        self.assertEqual(
            routes_orders.list_orders(status=None, db=self.db, me=self.agent), []
        )

    def test_an_agent_cannot_read_one_order(self):
        with self.assertRaises(HTTPException) as error:
            routes_orders.get_order(order_id=self.order.id, db=self.db, me=self.agent)
        self.assertEqual(error.exception.status_code, 403)

    def test_an_agent_cannot_read_an_order_s_events(self):
        with self.assertRaises(HTTPException) as error:
            routes_orders.list_order_events(order_id=self.order.id, db=self.db, me=self.agent)
        self.assertEqual(error.exception.status_code, 403)

    def test_an_agent_cannot_change_an_order_s_status(self):
        with self.assertRaises(HTTPException) as error:
            routes_orders.update_order_status(
                order_id=self.order.id,
                body=SimpleNamespace(status="cancelled"),
                db=self.db, me=self.agent,
            )
        self.assertEqual(error.exception.status_code, 403)
        self.db.refresh(self.order)
        self.assertNotEqual(self.order.status, "cancelled")

    def test_an_agent_cannot_read_sealed_vendor_quotes(self):
        with self.assertRaises(HTTPException) as error:
            routes_quotes.list_quotes_for_rfq(rfq_id=self.rfq.id, db=self.db, me=self.agent)
        self.assertEqual(error.exception.status_code, 403)

    def test_a_rival_shipping_company_sees_nothing_of_this_order(self):
        rival = self.make_user("shipping_company")
        self.assertEqual(
            routes_orders.list_orders(status=None, db=self.db, me=rival), []
        )


if __name__ == "__main__":
    unittest.main()
