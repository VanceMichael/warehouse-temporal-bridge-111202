"""带有效期的多对多桥接：版本治理、发布校验、按事实发生时刻选版本与守恒分摊。

设计要点：
- 桥接保持归一化（版本头 + 成员明细），不生成重复展开的宽表；集市按需重算。
- 同一项目的已发布版本有效期为半开区间 [valid_from, valid_to)，互不重叠。
- 发布版本成员权重为精确 Decimal，允许 0 权重（保留关系、不贡献），
  不允许负数；权重必须精确合计为 1，缺口与超配都不能发布。
- 事实按发生日匹配当时已发布且有效的版本，与导入/发布日期无关；
  无方案的事实保留在未分配组（department 为 None）。
- 分摊金额按 definition.QUANT 量化，最大余数法稳定处理尾差，组内严格守恒。
"""
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from .catalog import encode, parse_iso_day, transaction
from .definition import QUANT

UNASSIGNED = None
_QUANT = Decimal(QUANT)
_ONE = Decimal('1')
_ZERO = Decimal('0')


class BridgeError(ValueError):
    """桥接治理或分摊无法满足约定约束。"""


def _now_utc():
    return datetime.now(timezone.utc).isoformat()


def _parse_weight(raw):
    if not isinstance(raw, str):
        raise BridgeError('权重必须是十进制文本')
    try:
        value = Decimal(raw)
    except Exception as exc:  # Decimal 抛 InvalidOperation，属于 ArithmeticError
        raise BridgeError(f'权重不是合法十进制: {raw}') from exc
    if not value.is_finite():
        raise BridgeError('权重必须为有限十进制')
    return value


def validate_spec(spec):
    """校验单个桥接版本规格，返回规范化后的 (header, members)。"""
    if not isinstance(spec, dict):
        raise BridgeError('桥接规格必须是对象')
    for field in ('version', 'project', 'valid_from', 'members'):
        if field not in spec:
            raise BridgeError(f'桥接规格缺少字段: {field}')
    version = spec['version']
    project = spec['project']
    if not isinstance(version, str) or not version or not isinstance(project, str) or not project:
        raise BridgeError('版本号与项目标识必须是非空文本')
    valid_from = parse_iso_day(spec['valid_from'], 'valid_from')
    valid_to = None
    if spec.get('valid_to') is not None:
        valid_to = parse_iso_day(spec['valid_to'], 'valid_to')
        if valid_to <= valid_from:
            raise BridgeError('valid_to 必须晚于 valid_from（半开区间 [from, to)）')
    members = spec['members']
    if not isinstance(members, list) or not members:
        raise BridgeError('成员明细至少包含一个院系')
    normalized = []
    seen = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {'department', 'weight'}:
            raise BridgeError('成员明细只能包含 department 与 weight')
        department = member['department']
        if not isinstance(department, str) or not department:
            raise BridgeError('院系标识必须是非空文本')
        if department in seen:
            raise BridgeError(f'院系在同一版本中重复: {department}')
        seen.add(department)
        weight = _parse_weight(member['weight'])
        if weight < 0:
            raise BridgeError(f'院系 {department} 权重为负数: {weight}')
        normalized.append({'department': department, 'weight': weight})
    total = sum((member['weight'] for member in normalized), _ZERO)
    if total != _ONE:
        raise BridgeError(f'版本 {version} 的权重合计为 {total}，必须精确等于 1（缺口或超配不可发布）')
    header = {
        'version': version,
        'project': project,
        'valid_from': valid_from.isoformat(),
        'valid_to': valid_to.isoformat() if valid_to else None,
    }
    # 稳定顺序，保证摘要与分摊次序确定。
    normalized.sort(key=lambda item: item['department'])
    return header, normalized


def spec_digest(spec):
    """仅依据规范化业务内容计算摘要，发布时刻不参与，便于失败重试与重放。"""
    import hashlib

    header, members = validate_spec(spec)
    canonical = {'header': header, 'members': [
        {'department': item['department'], 'weight': str(item['weight'])} for item in members
    ]}
    return hashlib.sha256(encode(canonical)).hexdigest(), header, members


def _overlapping_published(connection, header, *, exclude_version=None):
    """返回与 header 在同一项目上有效期重叠的已发布版本（半开区间判定）。"""
    clauses = ['project = ?', "status = 'published'"]
    parameters = [header['project']]
    if exclude_version is not None:
        clauses.append('version <> ?')
        parameters.append(exclude_version)
    rows = connection.execute(
        f"SELECT version, valid_from, valid_to FROM bridge_versions WHERE {' AND '.join(clauses)}",
        parameters,
    ).fetchall()
    overlaps = []
    for row in rows:
        existing_to = row['valid_to']
        # 半开区间不重叠条件：existing.to <= new.from 或 existing.from >= new.to
        if existing_to is not None and existing_to <= header['valid_from']:
            continue
        if header['valid_to'] is not None and row['valid_from'] >= header['valid_to']:
            continue
        overlaps.append(dict(row))
    return overlaps


def submit_bridge(connection, spec):
    """以草稿状态暂存桥接版本；草稿不参与集市选版，也不阻塞区间校验之外的治理。"""
    digest, header, members = spec_digest(spec)
    with transaction(connection):
        existing = connection.execute(
            'SELECT status, digest FROM bridge_versions WHERE version=? AND project=?',
            (header['version'], header['project']),
        ).fetchone()
        if existing:
            if existing['digest'] != digest:
                raise BridgeError('同版本号已存在且内容不同')
            return {'changed': False, 'version': header['version'], 'status': existing['status']}
        connection.execute(
            'INSERT INTO bridge_versions (version, project, valid_from, valid_to, status, digest, published_at) '
            "VALUES (?, ?, ?, ?, 'draft', ?, NULL)",
            (header['version'], header['project'], header['valid_from'], header['valid_to'], digest),
        )
        connection.executemany(
            'INSERT INTO bridge_members (version, project, department, weight) VALUES (?, ?, ?, ?)',
            [(header['version'], header['project'], item['department'], str(item['weight'])) for item in members],
        )
    return {'changed': True, 'version': header['version'], 'status': 'draft'}


def publish_bridge(connection, spec, *, published_at=None, batch=None):
    """完整校验后原子发布；返回受影响事实预览与发布时刻。失败不留半成品，可原样重试。"""
    digest, header, members = spec_digest(spec)
    moment = published_at or _now_utc()
    with transaction(connection):
        existing = connection.execute(
            'SELECT status, digest, published_at FROM bridge_versions WHERE version=? AND project=?',
            (header['version'], header['project']),
        ).fetchone()
        if existing and existing['status'] == 'published':
            if existing['digest'] != digest:
                raise BridgeError('同版本号已发布且内容不同')
            return {'changed': False, 'version': header['version'], 'published_at': existing['published_at'],
                    'impact': preview_impact(connection, header)}
        overlap = _overlapping_published(connection, header, exclude_version=header['version'])
        # 预览必须在任何区间变更前计算，才能反映“发布前”的当前版本归属。
        impact = preview_impact(connection, header)
        # 追补/替代语义：同项目内新版本原子取代与其重叠的旧版本。
        # 每次取代写入 bridge_supersessions（原始区间 + 取代时刻 + 新版本），
        # 旧版本行同步收口；as-of 查询依据审计表恢复历史区间，历史版仍可查。
        for old in overlap:
            old_to = old['valid_to']
            if old['valid_from'] == header['valid_from']:
                connection.execute(
                    'INSERT OR REPLACE INTO bridge_supersessions '
                    '(version, project, superseded_at, replaced_by, original_valid_to) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (old['version'], header['project'], moment, header['version'], old_to),
                )
                connection.execute(
                    'UPDATE bridge_versions SET valid_to=valid_from WHERE version=? AND project=?',
                    (old['version'], header['project']),
                )
            elif old['valid_from'] < header['valid_from'] and (
                    old_to is None or old_to > header['valid_from']):
                if header['valid_to'] is None:
                    connection.execute(
                        'INSERT OR REPLACE INTO bridge_supersessions '
                        '(version, project, superseded_at, replaced_by, original_valid_to) '
                        'VALUES (?, ?, ?, ?, ?)',
                        (old['version'], header['project'], moment, header['version'], old_to),
                    )
                    connection.execute(
                        'UPDATE bridge_versions SET valid_to=? WHERE version=? AND project=?',
                        (header['valid_from'], old['version'], header['project']),
                    )
                elif old_to is None or old_to > header['valid_to']:
                    # 旧版被截为两段会产生覆盖空洞，要求先显式收口，拒绝自动发布
                    raise BridgeError(
                        f"版本 {old['version']} 长于待发布区间，自动取代会产生覆盖空洞，"
                        "请先用 bridge-expire 显式收口旧版本"
                    )
            else:
                raise BridgeError(
                    f"有效期与已发布版本 {old['version']} 重叠超配: "
                    f"[{old['valid_from']}, {old['valid_to']})"
                )
        if existing is None:
            connection.execute(
                'INSERT INTO bridge_versions (version, project, valid_from, valid_to, status, digest, published_at) '
                "VALUES (?, ?, ?, ?, 'published', ?, ?)",
                (header['version'], header['project'], header['valid_from'], header['valid_to'], digest, moment),
            )
            connection.executemany(
                'INSERT INTO bridge_members (version, project, department, weight) VALUES (?, ?, ?, ?)',
                [(header['version'], header['project'], item['department'], str(item['weight'])) for item in members],
            )
        else:
            connection.execute(
                "UPDATE bridge_versions SET valid_from=?, valid_to=?, status='published', digest=?, published_at=? "
                'WHERE version=? AND project=?',
                (header['valid_from'], header['valid_to'], digest, moment, header['version'], header['project']),
            )
        if batch:
            # 治理动作同样内容寻址；幂等重放不会产生第二条批次记录。
            connection.execute(
                'INSERT OR IGNORE INTO ingest_batches (batch, digest, rows) VALUES (?, ?, ?)',
                (batch, digest, len(impact['records'])),
            )
    return {'changed': True, 'version': header['version'], 'published_at': moment, 'impact': impact}


def load_spec_file(path):
    import json
    from pathlib import Path

    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise BridgeError('桥接文件必须是版本对象或版本数组')
    return payload


def select_version(connection, project, day, *, as_of=None):
    """按事实发生日选择有效版本（业务时间），as_of 限定可见的发布时点（系统时间）。

    双时态规则：
    - 不带 as_of：使用当前区间（被取代版本已在 bridge_versions 收口）。
    - 带 as_of：只接受 published_at <= as_of 的版本；若版本在 as_of 之后才被
      取代，则按 bridge_supersessions 恢复其原始有效期，从而复算发布前的历史口径。
    """
    rows = connection.execute(
        "SELECT version, valid_from, valid_to, published_at FROM bridge_versions "
        "WHERE project=? AND status='published' ORDER BY version",
        (project,),
    ).fetchall()
    matches = []
    for row in rows:
        if as_of is not None and row['published_at'] > as_of:
            continue
        valid_from = row['valid_from']
        valid_to = row['valid_to']
        if as_of is not None:
            supersession = connection.execute(
                'SELECT superseded_at, original_valid_to FROM bridge_supersessions '
                'WHERE version=? AND project=?',
                (row['version'], project),
            ).fetchone()
            if supersession is not None and supersession['superseded_at'] > as_of:
                # 取代发生在 as_of 之后：恢复取代前的原始区间
                valid_to = supersession['original_valid_to']
        if valid_from <= day and (valid_to is None or valid_to > day):
            matches.append(dict(row) | {'effective_valid_to': valid_to})
    if not matches:
        return None
    if len(matches) > 1:  # 发布期已保证区间不重叠，出现多行即数据完整性被破坏。
        raise BridgeError(f'项目 {project} 在 {day} 命中多个有效版本，区间约束失效')
    return {'version': matches[0]['version'], 'valid_from': matches[0]['valid_from'],
            'valid_to': matches[0]['effective_valid_to'], 'published_at': matches[0]['published_at']}


def version_members(connection, version, project):
    rows = connection.execute(
        'SELECT department, weight FROM bridge_members WHERE version=? AND project=? ORDER BY department',
        (version, project),
    ).fetchall()
    return [{'department': row['department'], 'weight': Decimal(row['weight'])} for row in rows]


def quantize_money(value):
    """按记账单位量化；ROUND_HALF_EVEN 为确定的银行家舍入，规则对复查者公开。"""
    return value.quantize(_QUANT, rounding=ROUND_HALF_EVEN)


def allocate(hours, members):
    """把单个事实的工作量按权重守恒分摊到院系。

    返回每个院系一行：含精确份额 exact_share、记账金额 amount（文本，QUANT 粒度）
    与余数信息；amount 之和恒等于量化后的总工作量。零权重保留关系且金额为 0。
    """
    total = quantize_money(hours)
    if total < 0:
        raise BridgeError('工作量不能为负数')
    floors = []
    for member in members:
        exact = hours * member['weight']
        # 向负无穷取地板，保证余数落在 [0, QUANT)，尾差全部走最大余数法。
        floor = (exact / _QUANT).to_integral_value(rounding='ROUND_FLOOR') * _QUANT
        remainder = exact - floor
        floors.append({
            'department': member['department'],
            'weight': member['weight'],
            'exact_share': exact,
            'amount': floor,
            'remainder': remainder,
        })
    leftover = int((total - sum((item['amount'] for item in floors), _ZERO)) / _QUANT)
    # 稳定的最大余数法：余数大者先补一个记账单位，平手按院系标识排序，结果可复查。
    order = sorted(range(len(floors)), key=lambda index: (-floors[index]['remainder'], floors[index]['department']))
    for index in order[:leftover]:
        floors[index]['amount'] += _QUANT
    allocated = sum((item['amount'] for item in floors), _ZERO)
    if allocated != total:
        raise BridgeError(f'分摊不守恒: 分配 {allocated} != 总量 {total}')
    for item in floors:
        item['weight'] = str(item['weight'])
        item['exact_share'] = str(item['exact_share'])
        item['amount'] = str(item['amount'])
        item['remainder'] = str(item['remainder'])
    return total, floors


def allocation_lines(connection, *, day_from=None, day_to=None, as_of=None, records=None):
    """集市血缘粒度：一条事实 × 一个院系一行；无方案事实保留未分配行。

    这是唯一的展开动作，且发生在查询期，不落盘、不固化宽表；上层汇总均由它上卷。
    """
    clauses = []
    parameters = []
    if day_from is not None:
        clauses.append('w.day >= ?')
        parameters.append(day_from)
    if day_to is not None:
        clauses.append('w.day <= ?')
        parameters.append(day_to)
    if records:
        clauses.append(f"w.record IN ({', '.join('?' for _ in records)})")
        parameters.extend(records)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ''
    facts = connection.execute(
        f'SELECT w.record, w.project, w.day, w.hours FROM work w {where} ORDER BY w.record',
        parameters,
    ).fetchall()
    lines = []
    for fact in facts:
        hours = Decimal(fact['hours'])
        if not hours.is_finite():
            raise BridgeError(f"事实 {fact['record']} 工作量不是有限十进制")
        chosen = select_version(connection, fact['project'], fact['day'], as_of=as_of)
        if chosen is None:
            lines.append({
                'record': fact['record'],
                'project': fact['project'],
                'day': fact['day'],
                'department': UNASSIGNED,
                'version': None,
                'weight': None,
                'exact_share': None,
                'amount': str(quantize_money(hours)),
                'assigned': False,
            })
            continue
        members = version_members(connection, chosen['version'], fact['project'])
        total, pieces = allocate(hours, members)
        for piece in pieces:
            lines.append({
                'record': fact['record'],
                'project': fact['project'],
                'day': fact['day'],
                'department': piece['department'],
                'version': chosen['version'],
                'weight': piece['weight'],
                'exact_share': piece['exact_share'],
                'amount': piece['amount'],
                'assigned': True,
            })
    return lines


def preview_impact(connection, header):
    """发布前无副作用预览：该版本有效期内会受影响的事实、日期与分摊去向。"""
    facts = connection.execute(
        'SELECT record, project, day, hours FROM work WHERE project=? AND day>=? AND (day<? OR ? IS NULL) ORDER BY record',
        (header['project'], header['valid_from'], header['valid_to'], header['valid_to']),
    ).fetchall()
    rows = []
    for fact in facts:
        chosen = select_version(connection, fact['project'], fact['day'])
        rows.append({
            'record': fact['record'],
            'day': fact['day'],
            'hours': fact['hours'],
            'current_version': chosen['version'] if chosen else None,
        })
    return {
        'project': header['project'],
        'version': header['version'],
        'valid_from': header['valid_from'],
        'valid_to': header['valid_to'],
        'records': rows,
        'affected_days': sorted({row['day'] for row in rows}),
    }


def expire_bridge(connection, version, project, valid_to):
    """把已发布版本在 valid_to 处收口（半开），成员与摘要不变，可幂等重放。"""
    day = parse_iso_day(valid_to, 'valid_to')
    with transaction(connection):
        row = connection.execute(
            'SELECT status, valid_from FROM bridge_versions WHERE version=? AND project=?',
            (version, project),
        ).fetchone()
        if row is None:
            raise BridgeError(f'版本不存在: {version}/{project}')
        if row['status'] != 'published':
            raise BridgeError('只有已发布版本可以收口')
        if day.isoformat() <= row['valid_from']:
            raise BridgeError('收口日期必须晚于生效日期')
        connection.execute(
            'UPDATE bridge_versions SET valid_to=? WHERE version=? AND project=?',
            (day.isoformat(), version, project),
        )
    return {'changed': True, 'version': version, 'project': project, 'valid_to': day.isoformat()}


def recompute_records(connection, records, *, as_of=None):
    """局部重算：只对指定事实重新分摊，用于补档/重试后核对，无需全量刷新。"""
    if not records:
        return []
    return allocation_lines(connection, as_of=as_of, records=list(records))
