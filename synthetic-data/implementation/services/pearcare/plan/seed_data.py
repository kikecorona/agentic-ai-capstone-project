"""PearCare plan service — seed data.

These are the warranty plans the storefront can offer for an app.
Coverage tiers loosely mirror AppleCare / AppleCare+:

  * `pearcare`        — accidental damage and limited support
  * `pearcare_plus`   — adds priority support and unlimited claims
  * `pearcare_loss`   — loss/theft protection (rare; a few apps only)

The mapping `APP_PLAN_MAP` decides which plans show up on which app
detail pages. Most apps in the catalog don't have plans. Premium
productivity / pro creative apps do.
"""
from dataclasses import dataclass, field, asdict
from typing import List


@dataclass
class Plan:
    id: str
    name: str
    tier: str
    price_cents: int
    term_months: int
    coverage: List[str] = field(default_factory=list)
    monthly_billing: bool = False

    def to_dict(self): return asdict(self)


PLANS = [
    Plan("plan.basic.12",  "PearCare 12-month",
         tier="pearcare",       price_cents=1999,  term_months=12,
         coverage=["accidental damage", "support"]),
    Plan("plan.basic.24",  "PearCare 24-month",
         tier="pearcare",       price_cents=3499,  term_months=24,
         coverage=["accidental damage", "support"]),
    Plan("plan.plus.12",   "PearCare+ 12-month",
         tier="pearcare_plus",  price_cents=3999,  term_months=12,
         coverage=["accidental damage", "priority support",
                   "unlimited claims", "data recovery"]),
    Plan("plan.plus.24",   "PearCare+ 24-month",
         tier="pearcare_plus",  price_cents=6999,  term_months=24,
         coverage=["accidental damage", "priority support",
                   "unlimited claims", "data recovery"]),
    Plan("plan.loss.12",   "PearCare+ with Loss 12-month",
         tier="pearcare_loss",  price_cents=8999,  term_months=12,
         coverage=["accidental damage", "priority support",
                   "unlimited claims", "loss / theft", "data recovery"]),
    Plan("plan.plus.month","PearCare+ Monthly",
         tier="pearcare_plus",  price_cents=499,   term_months=1,
         coverage=["accidental damage", "priority support",
                   "unlimited claims", "data recovery"],
         monthly_billing=True),
]
PLANS_BY_ID = {p.id: p for p in PLANS}

# which apps offer which plans — premium / pro apps get the full
# ladder; everything else gets nothing.
APP_PLAN_MAP = {
    # pro creative
    "app.008": ["plan.plus.12", "plan.plus.24", "plan.basic.12"],     # Velocity DAW
    "app.029": ["plan.plus.12", "plan.plus.24"],                       # Carve
    "app.036": ["plan.plus.24", "plan.plus.12", "plan.basic.24"],      # Spire Architect
    # pro dev
    "app.015": ["plan.basic.12", "plan.plus.12"],                      # Beacon Dev
    "app.034": ["plan.plus.12", "plan.basic.12"],                      # Cinder
    "app.045": ["plan.basic.12"],                                      # Anvil Builds
    # finance / productivity
    "app.002": ["plan.basic.12", "plan.basic.24"],                     # Pocket Ledger
    "app.022": ["plan.basic.12"],                                      # Tessera Tasks
    "app.035": ["plan.basic.12", "plan.plus.12"],                      # Loomwork
    # premium games
    "app.016": ["plan.basic.12"],                                      # Threadbare RPG
    "app.038": ["plan.basic.12", "plan.plus.12", "plan.loss.12"],      # Hollow Knight Mobile
    # navigation
    "app.004": ["plan.basic.12", "plan.basic.24"],                     # Pixel Atlas
}
