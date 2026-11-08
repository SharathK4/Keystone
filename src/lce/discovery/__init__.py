"""The rule lab: a language model writes the rule for who gets the money.

The problem is the one Keystone exists for, stated as a puzzle: a merchant has
missed a payment and there is a fixed amount of money. In what order do you
fund the merchants who are short, each up to what it needs, so the week loses
the least? Fund the biggest gap first and a huge failure swallows a small
budget; fund whoever fits the budget and a large budget is wasted. No rule a
person writes wins everywhere.

So the lab lets a model write the rule. It proposes one-line formulas over
what Keystone knows about each merchant (``clues``). Each formula is parsed
into a whitelisted expression (``formula``; nothing the model writes is ever
executed), turned into a plan and scored by replaying the week on every test
scenario (``world``). The best rules are shown back to the model, which writes
better ones (``propose``, ``lab``): an evolutionary search in the style of
FunSearch, with Keystone's replay as the judge.

The result is a rule a person can read, that decides in microseconds, checked
on failures it never saw and compared with Keystone's own search at the same
money. A plan from it is paid through the same PayPal approval as every plan.
"""
