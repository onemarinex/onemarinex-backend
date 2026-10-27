# What may an agent see in the RFQ / quote / order marketplace?

**Status: closed, pending a decision.** Agents currently get nothing. This
records why, and what has to be decided to give them something.

## What it was

Five authorization branches across `routes_orders.py` and `routes_quotes.py`
granted the agent role unconditional access, each with a comment saying it was
a placeholder:

```python
elif me.role == "agent":
    # Agents see all; change if needed
```
```python
# agent allowed (or restrict if needed)
```
```python
# (allow agent if you want them to see on behalf; remove role check if not desired)
```

An `Order` carries `items`, `shipping_cost`, `discount_pct`, `tax_pct`,
`subtotal`, `tax_amount` and `grand_total`. So any authenticated agent could:

- list **every** order across every tenant, with the line items and the
  negotiated totals;
- read any single order, and its full event history;
- read every vendor's bid on any RFQ — the one thing a sealed quote exists to
  prevent;
- and **change any order's status**, cancellation included.

These are mounted routes. There is no frontend that calls them, and the
development database has no rows, but `app/main.py` includes both routers, so
the surface was reachable by any signed-in agent with a shell.

## Why it is now nothing rather than something narrower

There is no key to narrow it *to*. `Order` has `buyer_user_id`,
`vendor_user_id`, `vendor_company` and `port`. `RFQ` has `user_id`,
`buyer_company` (a free-text string) and `port`. Neither carries an agency, and
`buyer_company` is not a foreign key to anything.

So an agent-scoping rule cannot be written today — it can only be invented. The
branches now fail closed, which is the same shape the file already used for an
unrecognised role, and which matches the owner's October decision about cab
aggregators: a role with no defined access gets a refusal, not an empty list
that reads as "none yet".

## What has to be decided

1. **Should agents be in this marketplace at all?** If not, the cleanest
   outcome is to remove the three routers rather than leave dead authorized
   surface — the same conclusion the aggregator incident screens reached.
2. **If yes, on what basis?** Plausible rules, each implying a different
   schema change:
   - the agent that serves the *port* on the order (`Order.port` exists, but
     port is not an agency);
   - the agency acting for the buying vessel at the time of the order, which
     needs an agency reference on `RFQ` or `Order`, written when it is created;
   - explicit delegation — a shipping company nominates an agent to act for it.
3. **Read or also write?** Status changes and order events are writes. "Act on
   behalf of" is a much larger grant than "see on behalf of", and the previous
   code gave both without distinguishing them.

Until (1) is answered there is nothing to build, and the current state is the
safe one.

## Tests

`tests/test_marketplace_agent_access.py` pins the closed state and leaves the
buyer and vendor paths covered. They are written to **fail** when agent access
is reintroduced, so whoever adds it has to state the rule rather than restore
the placeholder.
