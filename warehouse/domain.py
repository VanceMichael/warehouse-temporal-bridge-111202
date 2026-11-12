from collections import defaultdict
from decimal import Decimal


def overview(connection):
    """遗留的单归属来源视图：直接按 projects.department 汇总 work。

    跨院系联合负责的归属与分摊请使用 warehouse.mart 与 warehouse.bridge；
    本函数不经过桥接，join 多条归属会膨胀的问题不在此处处理，仅保留来源对照。
    """
    rows = [dict(record) for record in connection.execute('SELECT p.department, w.hours FROM work w JOIN projects p ON w.project=p.project ORDER BY w.record')]
    totals = defaultdict(lambda: Decimal("0"))
    for record in rows:
        key = tuple(record[field] for field in ['department'])
        value = Decimal(record['hours'])
        if not value.is_finite():
            raise ValueError("数量必须为有限十进制")
        totals[key] += value
    return [dict(zip(['department'], key), **{'hours': str(value)}) for key, value in sorted(totals.items())]
