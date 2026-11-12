"""跨院系桥接集市的完整生命周期演示（全新临时目录，无预制日志）。

覆盖：事实导入 → 非法版本拒绝 → 初始发布 → 三级集市与单条血缘 →
追补预览 → 原子切换 → as-of 历史复算 → 未分配补档 → 局部重算与守恒校验 →
对象导出往返。任一步不符即非零退出。
"""
import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warehouse.store import ObjectStore


def execute(*arguments, expect_fail=False):
    command = [sys.executable, '-m', 'warehouse', *arguments]
    print('$ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if expect_fail:
        if result.returncode == 0:
            raise RuntimeError('预期失败的命令却成功了')
        print('[被拒绝] ' + result.stderr.strip(), flush=True)
        return None
    if result.returncode != 0:
        raise RuntimeError(f'命令失败: {result.stderr}')
    print(result.stdout.strip(), flush=True)
    return json.loads(result.stdout)


def amounts(lines):
    return {line['department']: line['amount'] for line in lines if line['assigned']}


def main():
    with tempfile.TemporaryDirectory() as temporary:
        database = str(Path(temporary) / 'warehouse.sqlite3')
        objects = str(Path(temporary) / 'objects')
        example = lambda name: str(ROOT / 'examples' / name)

        execute('import', '--db', database, '--input', example('source.json'), '--batch', 'facts')
        # 重复导入幂等
        if execute('import', '--db', database, '--input', example('source.json'), '--batch', 'facts')['changed']:
            raise RuntimeError('重复导入发生额外写入')

        # 1. 缺口/负数版本无法发布，库内不留半成品
        execute('bridge-publish', '--db', database, '--bridge', example('bridge-invalid-gap.json'),
                expect_fail=True)
        execute('bridge-publish', '--db', database, '--bridge', example('bridge-invalid-negative.json'),
                expect_fail=True)

        # 2. 初始三项目版本发布
        execute('bridge-publish', '--db', database, '--bridge', example('bridge-initial.json'),
                '--published-at', '2026-10-02T00:00:00+00:00')

        # 3. 学校总览：总量只算一次，未分配单列，守恒标志为真
        school = execute('mart-school', '--db', database)
        if not school['conservation_ok'] or school['total_hours'] != '70.01':
            raise RuntimeError('初始学校总览守恒异常')
        if set(school['unassigned_records']) != {'w2', 'w5'}:
            raise RuntimeError('未分配事实识别错误')

        # 4. 单条血缘：w1 = 10.01 按 0.7/0.3 守恒分摊，尾差 0.01 给余数最大的 d1
        explained = execute('explain', '--db', database, '--record', 'w1')
        if amounts(explained['lines']) != {'d1': '7.01', 'd2': '3.00'}:
            raise RuntimeError('w1 分摊结果错误')

        # 5. 追补比例先预览影响面，再发布；发布后院系与学校总览同时切换
        execute('bridge-preview', '--db', database, '--bridge', example('bridge-backfill.json'))
        execute('bridge-publish', '--db', database, '--bridge', example('bridge-backfill.json'),
                '--published-at', '2026-11-05T08:00:00+00:00')
        # 失败/重复重试：同内容再发一次，changed=false 且结果不变
        if execute('bridge-publish', '--db', database, '--bridge', example('bridge-backfill.json'),
                   '--published-at', '2026-11-05T08:00:00+00:00')['changed']:
            raise RuntimeError('追补版本重复发布发生额外写入')

        current_w3 = execute('recompute', '--db', database, '--record', 'w3')
        if amounts(current_w3) != {'d1': '5.00', 'd2': '3.00', 'd3': '2.00'}:
            raise RuntimeError('追补后 w3 分摊错误')
        # 历史时点仍按旧版本解析
        historical_w3 = execute('recompute', '--db', database, '--record', 'w3',
                                '--as-of', '2026-11-05T07:59:59+00:00')
        if amounts(historical_w3) != {'d1': '6.00', 'd2': '4.00'}:
            raise RuntimeError('as-of 历史复算错误')

        school_now = execute('mart-school', '--db', database)
        school_then = execute('mart-school', '--db', database, '--as-of', '2026-11-05T07:59:59+00:00')
        if school_now['total_hours'] != school_then['total_hours'] or not school_now['conservation_ok']:
            raise RuntimeError('跨期切换总量不守恒')
        if school_now['unassigned_hours'] != '10.00':  # w5 被回溯版接管，只剩 9 月的 w2
            raise RuntimeError('回溯接管未分配事实的结果错误')

        # 6. 未分配补档：零权重版本发布后，关系保留但不贡献金额
        execute('bridge-publish', '--db', database, '--bridge', example('bridge-zero-weight.json'),
                '--published-at', '2026-09-01T00:00:00+00:00')

        # 7. 血缘行导出对象存储并校验往返
        exported = execute('mart-lines', '--db', database, '--objects', objects)
        restored = ObjectStore(objects).read(exported['object'])
        if len(restored) != exported['rows']:
            raise RuntimeError('对象导出行数不一致')
        # 导出明细按事实求和必须等于学校总量（跨院系不膨胀）
        per_record = {}
        for line in restored:
            per_record[line['record']] = per_record.get(line['record'], Decimal('0')) + Decimal(line['amount'])
        if sum(per_record.values(), Decimal('0')) != Decimal(school_now['total_hours']):
            raise RuntimeError('展开明细汇总与学校总量不一致')

        print('全部生命周期断言通过：发布治理、守恒分摊、原子切换、历史复算、未分配补档与对象往返均正确',
              flush=True)


if __name__ == '__main__':
    main()
