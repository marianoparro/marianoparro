"""Private household settings, read from environment variables so they
never land in git. Shared by the dashboard (/config) and the assistant."""

import os


def env_money(name: str) -> float | None:
    value = os.environ.get(name)
    return float(value) if value else None


def fixed_costs() -> list[dict]:
    """FIXED_COSTS="Rent:2500,Domestic help:1561,..." -> [{name, amount}, ...].

    Itemized so the page can show what the fixed total is made of; change
    rent or add a line by editing this one variable, no code change.
    """
    items = []
    for entry in os.environ.get("FIXED_COSTS", "").split(","):
        name, sep, amount = entry.rpartition(":")
        if sep and name.strip():
            try:
                items.append({"name": name.strip(), "amount": float(amount)})
            except ValueError:
                pass  # skip a malformed entry rather than break the page
    return items


def plan() -> dict:
    items = fixed_costs()
    return {
        "monthly_net_income": env_money("MONTHLY_NET_INCOME"),
        "fixed_costs": items,
        # single total with no breakdown still works via MONTHLY_FIXED_COSTS
        "monthly_fixed_costs": round(sum(i["amount"] for i in items), 2) if items else env_money("MONTHLY_FIXED_COSTS"),
        "yearly_savings_goal": env_money("YEARLY_SAVINGS_GOAL"),
    }
