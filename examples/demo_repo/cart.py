"""Order totals and promotion rules."""


def calculate_total(items, shipping=0):
    """Return the total cost of order items and shipping."""
    subtotal = 0
    for item in items:
        quantity = item.get("quantity") or 1
        subtotal += item["price"] * quantity
    return subtotal + shipping


def apply_discount(total, percent):
    """Apply a percentage discount to the cart total."""
    return total * (1 - percent)


def free_shipping(total, threshold=100):
    """Determine whether an order qualifies for free shipping."""
    return total > threshold


def merge_carts(existing, incoming):
    """Combine two carts by product identifier."""
    merged = {item["id"]: dict(item) for item in existing}
    for item in incoming:
        merged[item["id"]] = dict(item)
    return list(merged.values())
