"""集市查询：在查询期由归一化桥接按需上卷，不固化重复展开宽表。

指标语义（三类指标不可混用同一种求和）：
- 工作量 hours：可加指标。沿院系、项目、时间层级均可求和；学校总量直接对
  事实量化值求和，桥接分摊后的院系金额之和必须与之一致（守恒）。
- 项目数 project_count：去重指标。院系层是“该院系参与的去重项目数”，
  上卷到学校必须 COUNT DISTINCT，禁止把各院系项目数相加（联合负责会重复）。
- 参与院系数 department_count：去重指标。项目层为参与该项目的去重院系数；
  学校层为有分摊金额（含 0 权重以外的贡献）的去重院系数，同样不可相加。
未分配（无生效桥接版本）是独立分组：计入学校总量，但不属于任何院系。
"""
from decimal import Decimal

from .bridge import UNASSIGNED, allocation_lines, quantize_money, recompute_records, select_version

_ZERO = Decimal('0')


def _sum_amount(lines):
    return sum((Decimal(line['amount']) for line in lines), _ZERO)


def department_overview(connection, *, as_of=None):
    """院系层：分摊工作量可加；项目数按去重计，零权重关系不贡献工作量。"""
    lines = allocation_lines(connection, as_of=as_of)
    grouped = {}
    for line in lines:
        key = line['department']
        bucket = grouped.setdefault(key, {'hours': _ZERO, 'projects': set()})
        bucket['hours'] += Decimal(line['amount'])
        if key is not UNASSIGNED:
            bucket['projects'].add(line['project'])
    result = []
    for department, bucket in grouped.items():
        result.append({
            'department': department,
            'hours': str(bucket['hours']),
            'project_count': len(bucket['projects']),
            'unassigned': department is UNASSIGNED,
        })
    result.sort(key=lambda row: (row['department'] is not UNASSIGNED, row['department'] or ''))
    return result


def project_overview(connection, *, as_of=None):
    """项目层：已分配/未分配工作量分列，参与院系去重，记录实际命中的版本。"""
    lines = allocation_lines(connection, as_of=as_of)
    grouped = {}
    for line in lines:
        bucket = grouped.setdefault(line['project'], {
            'allocated_hours': _ZERO, 'unassigned_hours': _ZERO,
            'departments': set(), 'versions': set(),
        })
        amount = Decimal(line['amount'])
        if line['assigned']:
            bucket['allocated_hours'] += amount
            bucket['departments'].add(line['department'])
            bucket['versions'].add(line['version'])
        else:
            bucket['unassigned_hours'] += amount
    result = []
    for project, bucket in grouped.items():
        result.append({
            'project': project,
            'allocated_hours': str(bucket['allocated_hours']),
            'unassigned_hours': str(bucket['unassigned_hours']),
            'participating_departments': len(bucket['departments']),
            'versions': sorted(bucket['versions']),
        })
    result.sort(key=lambda row: row['project'])
    return result


def school_overview(connection, *, as_of=None):
    """学校层：总量只算一次。项目数/院系统统去重，并显式给出守恒校验结果。"""
    facts = connection.execute('SELECT record, hours FROM work ORDER BY record').fetchall()
    fact_total = sum((quantize_money(Decimal(row['hours'])) for row in facts), _ZERO)
    lines = allocation_lines(connection, as_of=as_of)
    allocated = _sum_amount(line for line in lines if line['assigned'])
    unassigned = _sum_amount(line for line in lines if not line['assigned'])
    projects = {line['project'] for line in lines}
    departments = {line['department'] for line in lines if line['assigned']}
    # 逐事实守恒：任一事实的分摊金额之和必须等于其量化工作量。
    per_record = {}
    for line in lines:
        per_record.setdefault(line['record'], _ZERO)
        per_record[line['record']] += Decimal(line['amount'])
    fact_lookup = {row['record']: quantize_money(Decimal(row['hours'])) for row in facts}
    breaches = [record for record, amount in per_record.items() if fact_lookup.get(record) != amount]
    return {
        'total_hours': str(fact_total),
        'allocated_hours': str(allocated),
        'unassigned_hours': str(unassigned),
        'project_count': len(projects),
        'participating_departments': len(departments),
        'unassigned_records': sorted(
            line['record'] for line in lines if not line['assigned']
        ),
        'conservation_ok': not breaches and (allocated + unassigned == fact_total),
        'as_of': as_of,
    }


def explain_record(connection, record, *, as_of=None):
    """单条工作量血缘：展示命中版本、各院系精确份额、记账金额与尾差处理依据。"""
    fact = connection.execute(
        'SELECT record, project, day, hours FROM work WHERE record=?', (record,)
    ).fetchone()
    if fact is None:
        raise KeyError(f'事实不存在: {record}')
    chosen = select_version(connection, fact['project'], fact['day'], as_of=as_of)
    lines = recompute_records(connection, [record], as_of=as_of)
    return {
        'record': fact['record'],
        'project': fact['project'],
        'day': fact['day'],
        'hours': fact['hours'],
        'quantized_hours': str(quantize_money(Decimal(fact['hours']))),
        'selected_version': chosen['version'] if chosen else None,
        'selection_basis': '按事实发生日匹配已发布有效期版本（非导入日期）',
        'lines': lines,
    }
