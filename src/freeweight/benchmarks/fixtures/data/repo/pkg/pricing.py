"""A fixture module. It is data, not code the application imports or runs."""

from pkg.inventory import total_units


def price_of(sku, catalogue):
    """Return the unit price for sku."""
    return catalogue[sku]


def restock_cost(rows, catalogue):
    """Return the cost of restocking every row to its target."""
    units = total_units(rows)
    return units * price_of(rows[0]["sku"], catalogue)
