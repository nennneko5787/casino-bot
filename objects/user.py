from dataclasses import dataclass


@dataclass(kw_only=True, slots=True, weakref_slot=True)
class User:
    id: int
    amount: int
    debt: int = 0
