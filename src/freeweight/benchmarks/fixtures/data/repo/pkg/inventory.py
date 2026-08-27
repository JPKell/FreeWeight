"""A fixture module. It is data, not code the application imports or runs."""


def total_units(rows):
    """Sum the unit counts in rows."""
    return sum(row["units"] for row in rows)


class InventoryReport:
    """Renders an inventory summary."""

    def render(self, rows):
        """Return one line per row."""
        return [f"{row['sku']}: {row['units']}" for row in rows]
