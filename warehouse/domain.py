from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal


def overview(connection):
    rows = [dict(record) for record in connection.execute('SELECT p.department, w.hours FROM work w JOIN projects p ON w.project=p.project ORDER BY w.record')]
    totals = defaultdict(lambda: Decimal("0"))
    for record in rows:
        key = tuple(record[field] for field in ['department'])
        value = Decimal(record['hours'])
        if not value.is_finite():
            raise ValueError("数量必须为有限十进制")
        totals[key] += value
    return [dict(zip(['department'], key), **{'hours': str(value)}) for key, value in sorted(totals.items())]
