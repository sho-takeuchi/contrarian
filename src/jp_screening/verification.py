"""Independent reference implementation for stage-one arithmetic and price parity."""
from datetime import date
from decimal import Decimal

from .core import ScreeningError, years_before, shift_month, month_key


def compare_prices(left, right):
    a = {b.day: b for b in left}
    b = {b.day: b for b in right}
    diffs = []
    for day in sorted(a.keys() | b.keys()):
        if day not in a or day not in b:
            diffs.append(f"{day}:日付不一致")
            continue
        if a[day].code != b[day].code:
            diffs.append(f"{day}:銘柄コード不一致")
        for field in ("close", "low", "high", "open"):
            if getattr(a[day], field) != getattr(b[day], field):
                diffs.append(f"{day}:{field}")
    return diffs


def reference_check(result, months, bars, listing, asof, cal):
    """Calculate directly from month dictionaries, not from the main evaluator."""
    if listing is None or listing > years_before(asof, 8):
        return []
    expected, m = [], (shift_month(asof.replace(day=1), -120)
        if listing <= years_before(asof, 10) else listing.replace(day=1))
    while m < asof.replace(day=1):
        expected.append(month_key(m))
        m = shift_month(m, 1)
    errors = []
    closes = [months[k].close if k in months else None for k in expected]
    current = next((b.close for b in bars if b.day == asof), None)
    if all(v is not None for v in closes) and closes:
        floor = sorted(closes)[0]
        threshold = floor * Decimal("1.5")
        basic = max(floor * Decimal("1.2"), floor + 100)
        ceiling = min(threshold, basic)
        for field, value in (("lowest_close", floor), ("upper_limit", ceiling)):
            if result.get(field) != value:
                errors.append(field)
        if current is not None and result.get("condition_a") != (current <= ceiling):
            errors.append("condition_a")
    last = cal.last_complete_month(asof)
    four = [months.get(month_key(shift_month(last, -i))) for i in range(4)]
    if all(b and b.low is not None for b in four):
        expected_b = all(b.low >= four[3].low for b in four[:3])
        if result.get("condition_b") != expected_b:
            errors.append("condition_b")
    if all(b and b.close is not None for b in four[:3]):
        # Equivalent two-prior-month form avoids sharing the main comparison.
        expected_c = four[0].close * 2 > four[1].close + four[2].close
        if result.get("condition_c") != expected_c:
            errors.append("condition_c")
    return errors
