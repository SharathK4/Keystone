"""The walk: one merchant's missed payment, followed from a failed PayPal payout
through who feels it, what waiting costs and the plan that covers it.

The quiet front page reads only from here and from the routes it already used.
Nothing in this package optimises or simulates on its own: it builds the shock
the existing pipeline already knows how to analyse (a failed payout), and runs
it through the existing signature check, state machine and bounded analysis.
"""
