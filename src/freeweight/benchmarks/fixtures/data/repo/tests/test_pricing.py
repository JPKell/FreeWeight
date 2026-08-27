"""A fixture test module. It is data; nothing here is collected or executed."""

from pkg.pricing import restock_cost


def test_restock_cost():
    """restock_cost multiplies total units by the unit price."""
    assert restock_cost([{"sku": "A1", "units": 3}], {"A1": 5}) == 15
