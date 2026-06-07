"""Shared types and the service registry.

Every service imports `SERVICES` to discover where its peers live. Keeping
the registry centralized means we have exactly one place to edit if a port
moves.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


SERVICES = {
    "catalog":         "http://localhost:15001",
    "account":         "http://localhost:15002",
    "cart":            "http://localhost:15003",
    "order":           "http://localhost:15004",
    "payment":         "http://localhost:15005",
    "fulfillment":     "http://localhost:15006",
    "review":          "http://localhost:15007",
    "search":          "http://localhost:15008",
    # PearCare sibling system
    "pearcare_plan":   "http://localhost:15101",
    "pearcare_claim":  "http://localhost:15102",
}


@dataclass
class App:
    id: str
    name: str
    developer: str
    category: str
    price_cents: int            # 0 means free
    description: str
    icon_emoji: str
    rating: float = 0.0
    rating_count: int = 0
    bundle_id: str = ""
    version: str = "1.0.0"
    size_mb: int = 50

    def to_dict(self):
        return asdict(self)


@dataclass
class User:
    id: str
    email: str
    display_name: str
    # NOTE: passwords are stored in plaintext on purpose — this is a synthetic
    # demo, not a real auth system. Do not copy this pattern into production.
    password: str = ""
    payment_method_token: Optional[str] = None


@dataclass
class CartItem:
    app_id: str
    added_at: str


@dataclass
class Order:
    id: str
    user_id: str
    items: list = field(default_factory=list)   # list of app_id
    total_cents: int = 0
    status: str = "pending"                     # pending|paid|fulfilled|failed|refunded
    payment_id: Optional[str] = None
    created_at: str = ""
