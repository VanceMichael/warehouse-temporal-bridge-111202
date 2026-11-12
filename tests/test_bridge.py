import copy
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from warehouse.bridge import (
    BridgeError,
    allocate,
    allocation_lines,
    expire_bridge,
    publish_bridge,
    recompute_records,
    select_version,
    spec_digest,
    submit_bridge,
    version_members,
)
from warehouse.catalog import connect, load_bundle, load_file
from warehouse.domain import overview
from warehouse.mart import department_overview, explain_record, project_overview, school_overview
from warehouse.store import ObjectStore

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'examples/source.json'
INITIAL = ROOT / 'examples/bridge-initial.json'
BACKFILL = ROOT / 'examples/bridge-backfill.json'
BAD_GAP = ROOT / 'examples/bridge-invalid-gap.json'
BAD_NEG = ROOT / 'examples/bridge-invalid-negative.json'
ZERO = ROOT / 'examples/bridge-zero-weight.json'


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class BridgeWarehouseTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'warehouse.sqlite3'
        self.connection = connect(self.path)
        self.bundle = load_json(SOURCE)
        load_bundle(self.connection, self.bundle, 'facts')
        self.initial = load_json(INITIAL)
        self.backfill = load_json(BACKFILL)[0]

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def publish_initial(self, published_at='2026-10-02T00:00:00+00:00'):
        for spec in self.initial:
            publish_bridge(self.connection, spec, published_at=published_at)


class GovernanceTests(BridgeWarehouseTestCase):
    def test_negative_and_overweight_rejected(self):
        with self.assertRaises(BridgeError):
            spec_digest(load_json(BAD_NEG)[0])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bridge_versions").fetchone()[0], 0)

    def test_gap_rejected(self):
        with self.assertRaises(BridgeError) as caught:
            publish_bridge(self.connection, load_json(BAD_GAP)[0])
        self.assertIn('精确等于 1', str(caught.exception))

    def test_overlapping_published_ranges_rejected(self):
        self.publish_initial()
        # 起点落在 p2-v1 区间内部且不能整体取代 -> 自动取代会制造空洞，拒绝发布
        overlap = copy.deepcopy(self.backfill)
        overlap['valid_from'] = '2026-10-15'
        overlap['valid_to'] = '2026-10-20'
        with self.assertRaises(BridgeError) as caught:
            publish_bridge(self.connection, overlap, published_at='2026-11-05T00:00:00+00:00')
        self.assertIn('覆盖空洞', str(caught.exception))

    def test_backfill_replaces_old_version_but_history_remains(self):
        self.publish_initial()
        # 整体追补：v1 在 11-01 自然结束，v2 从 10-01 起取代，旧行保留
        publish_bridge(self.connection, self.backfill, published_at='2026-11-05T00:00:00+00:00')
        old = self.connection.execute(
            "SELECT valid_to, status FROM bridge_versions WHERE version='p2-v1'").fetchone()
        # 同起点整体替代：v1 收口为零长度区间，仍为 published 并留有取代审计
        self.assertEqual(old['valid_to'], '2026-10-01')
        self.assertEqual(old['status'], 'published')
        audit = self.connection.execute(
            "SELECT replaced_by, original_valid_to FROM bridge_supersessions WHERE version='p2-v1'").fetchone()
        self.assertEqual(audit['replaced_by'], 'p2-v2-backfill')
        self.assertEqual(audit['original_valid_to'], '2026-11-01')
        self.assertEqual(select_version(self.connection, 'p2', '2026-10-15')['version'], 'p2-v2-backfill')
        # 历史时点仍解析到旧版（取代尚未发生，审计表恢复原始区间）
        self.assertEqual(
            select_version(self.connection, 'p2', '2026-10-15',
                           as_of='2026-11-04T00:00:00+00:00')['version'], 'p2-v1')

    def test_draft_does_not_block_and_is_not_selected(self):
        changed = submit_bridge(self.connection, self.backfill)
        self.assertTrue(changed['changed'])
        # 草稿不可被事实选中
        self.assertIsNone(select_version(self.connection, 'p2', '2026-10-05'))
        # 同版本重复提交幂等
        again = submit_bridge(self.connection, self.backfill)
        self.assertFalse(again['changed'])

    def test_publish_is_idempotent_and_retryable(self):
        spec = self.initial[0]
        first = publish_bridge(self.connection, spec, published_at='2026-10-02T00:00:00+00:00')
        self.assertTrue(first['changed'])
        second = publish_bridge(self.connection, spec, published_at='2026-10-02T00:00:00+00:00')
        self.assertFalse(second['changed'])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM bridge_versions WHERE project='p1'").fetchone()[0], 1)

    def test_failed_publish_leaves_no_partial_state(self):
        # 起点早于现存版本且无法自动取代（会产生覆盖空洞）-> 发布失败，整事务回滚
        self.publish_initial()
        before = self.connection.execute('SELECT COUNT(*) FROM bridge_members').fetchone()[0]
        impossible = copy.deepcopy(self.backfill)
        impossible['valid_from'] = '2026-09-01'
        impossible['valid_to'] = '2026-10-15'
        with self.assertRaises(BridgeError):
            publish_bridge(self.connection, impossible, published_at='2026-11-05T00:00:00+00:00')
        after = self.connection.execute('SELECT COUNT(*) FROM bridge_members').fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM bridge_versions WHERE status='published'").fetchone()[0], 3)
        self.assertEqual(
            self.connection.execute('SELECT COUNT(*) FROM bridge_supersessions').fetchone()[0], 0)

    def test_retry_after_failure_succeeds_and_conserves(self):
        self.publish_initial()
        impossible = copy.deepcopy(self.backfill)
        impossible['valid_from'] = '2026-09-01'
        impossible['valid_to'] = '2026-10-15'
        with self.assertRaises(BridgeError):
            publish_bridge(self.connection, impossible, published_at='2026-11-05T00:00:00+00:00')
        # 原样重试合法的追补版本成功，总量守恒
        publish_bridge(self.connection, self.backfill, published_at='2026-11-05T00:00:00+00:00')
        self.assertTrue(school_overview(self.connection)['conservation_ok'])

    def test_expire_then_backfill_succeeds_on_same_day_boundary(self):
        self.publish_initial()
        # 显式收口 v1 到 2026-11-01，再发布追补版；半开边界上 11-01 当日由 v2 接管
        expire_bridge(self.connection, 'p2-v1', 'p2', '2026-11-01')
        publish_bridge(self.connection, self.backfill, published_at='2026-11-05T00:00:00+00:00')
        self.assertEqual(select_version(self.connection, 'p2', '2026-10-31')['version'], 'p2-v2-backfill')
        self.assertEqual(select_version(self.connection, 'p2', '2026-11-01')['version'], 'p2-v2-backfill')
        self.assertEqual(select_version(self.connection, 'p2', '2026-11-02')['version'], 'p2-v2-backfill')
        # 取代审计保留 v1 的收口区间，发布前 as-of 仍解析 v1
        self.assertEqual(
            select_version(self.connection, 'p2', '2026-10-31',
                           as_of='2026-11-04T00:00:00+00:00')['version'], 'p2-v1')

    def test_hole_creating_replacement_rejected(self):
        self.publish_initial()
        # 新版本在 v1 区间内部开始且更早结束，自动取代会制造空洞，必须拒绝
        hole = copy.deepcopy(self.backfill)
        hole['valid_from'] = '2026-10-15'
        hole['valid_to'] = '2026-10-20'
        with self.assertRaises(BridgeError):
            publish_bridge(self.connection, hole, published_at='2026-11-05T00:00:00+00:00')

    def test_zero_weight_kept_but_contributes_nothing(self):
        publish_bridge(self.connection, load_json(ZERO)[0], published_at='2026-09-01T00:00:00+00:00')
        members = version_members(self.connection, 'p3-zero', 'p3')
        self.assertEqual([m['department'] for m in members], ['d1', 'd4'])
        _, pieces = allocate(Decimal('10.00'), members)
        amounts = {p['department']: p['amount'] for p in pieces}
        self.assertEqual(amounts, {'d1': '10.00', 'd4': '0.00'})

    def test_invalid_iso_day_rejected(self):
        bad = copy.deepcopy(self.initial[0])
        bad['valid_from'] = '2026-1-1'
        with self.assertRaises((BridgeError, ValueError)):
            spec_digest(bad)


class AllocationTests(BridgeWarehouseTestCase):
    def test_largest_remainder_conservation_and_tail(self):
        # 10.01 × 0.7/0.3 -> 精确 7.007 / 3.003，地板 7.00/3.00，尾差 0.01 给余数大的 d1
        members = [{'department': 'd1', 'weight': Decimal('0.70')},
                   {'department': 'd2', 'weight': Decimal('0.30')}]
        total, pieces = allocate(Decimal('10.01'), members)
        self.assertEqual(str(total), '10.01')
        self.assertEqual({p['department']: p['amount'] for p in pieces}, {'d1': '7.01', 'd2': '3.00'})
        self.assertEqual(sum((Decimal(p['amount']) for p in pieces), Decimal('0')), total)

    def test_three_way_tie_break_is_stable(self):
        members = [{'department': d, 'weight': Decimal('0.34') if d == 'd1' else Decimal('0.33')}
                   for d in ('d1', 'd2', 'd3')]
        total, pieces = allocate(Decimal('10.00'), members)
        amounts = {p['department']: Decimal(p['amount']) for p in pieces}
        # 3.40/3.30/3.30 -> 地板 3.40/3.30/3.30 已守恒
        self.assertEqual(amounts, {'d1': Decimal('3.40'), 'd2': Decimal('3.30'), 'd3': Decimal('3.30')})
        self.assertEqual(sum(amounts.values(), Decimal('0')), total)

    def test_equal_split_remainder_tie_uses_department_order(self):
        members = [{'department': d, 'weight': Decimal('0.5')} for d in ('d1', 'd2')]
        total, pieces = allocate(Decimal('10.01'), members)
        # 5.005/5.005 余数相同，院系序 d1 得尾差
        self.assertEqual([p['amount'] for p in pieces], ['5.01', '5.00'])
        self.assertEqual(sum((Decimal(p['amount']) for p in pieces), Decimal('0')), total)

    def test_zero_weight_zero_amount_conservation(self):
        members = [{'department': 'd1', 'weight': Decimal('1')},
                   {'department': 'd2', 'weight': Decimal('0')}]
        total, pieces = allocate(Decimal('0.07'), members)
        self.assertEqual([p['amount'] for p in pieces], ['0.07', '0.00'])

    def test_half_even_quantization_of_total(self):
        # 总量先按银行家舍入量化到 0.01：2.005 -> 2.00，再由权重守恒分摊
        members = [{'department': 'd1', 'weight': Decimal('0.5')},
                   {'department': 'd2', 'weight': Decimal('0.5')}]
        total, pieces = allocate(Decimal('2.005'), members)
        self.assertEqual(str(total), '2.00')
        self.assertEqual(sum((Decimal(p['amount']) for p in pieces), Decimal('0')), total)

    def test_many_departments_remainder_distribution(self):
        members = [{'department': f'd{i}', 'weight': Decimal('0.1')} for i in range(10)]
        total, pieces = allocate(Decimal('0.03'), members)
        amounts = sorted(p['amount'] for p in pieces)
        # 10 个院系各精确 0.003，地板 0.00，3 个尾差单位按院系序分给 d0..d2
        self.assertEqual(amounts, ['0.00'] * 7 + ['0.01'] * 3)
        self.assertEqual(sum((Decimal(p['amount']) for p in pieces), Decimal('0')), total)


class SelectionAndMartTests(BridgeWarehouseTestCase):
    def test_fact_time_selection_not_ingest_time_and_unassigned(self):
        # 未发布任何版本：全部事实进入未分配组
        school = school_overview(self.connection)
        self.assertTrue(school['conservation_ok'])
        self.assertEqual(school['unassigned_hours'], school['total_hours'])
        self.assertEqual(len(school['unassigned_records']), 7)
        # w2 发生在 2026-09-20，早于所有版本起点，发布后仍未分配；
        # w5 发生在 2026-11-02，晚于 p2-v1 的 11-01 收口，初始发布后同样未分配
        self.publish_initial()
        self.assertIsNone(select_version(self.connection, 'p2', '2026-09-20'))
        self.assertIsNone(select_version(self.connection, 'p2', '2026-11-02'))
        self.assertEqual(select_version(self.connection, 'p2', '2026-10-05')['version'], 'p2-v1')

    def test_school_total_counted_once(self):
        self.publish_initial()
        school = school_overview(self.connection)
        self.assertTrue(school['conservation_ok'])
        # 70.01 总量；w2(9 月)与 w5(11-02，v1 已收口)共 20.00 未分配
        self.assertEqual(school['total_hours'], '70.01')
        self.assertEqual(school['unassigned_hours'], '20.00')
        self.assertEqual(school['allocated_hours'], '50.01')
        self.assertEqual(school['project_count'], 3)
        # 参与院系 d1/d2/d3（p3 的 d3 有正贡献），去重而非求和
        self.assertEqual(school['participating_departments'], 3)

    def test_distinct_metrics_are_not_summed(self):
        self.publish_initial()
        departments = {row['department']: row for row in department_overview(self.connection)}
        # 各院系 project_count 之和大于学校去重项目数（p2、p3 多院系共享）
        summed = sum(row['project_count'] for row in departments.values() if row['department'] is not None)
        self.assertGreater(summed, school_overview(self.connection)['project_count'])
        # 院系工作量之和 + 未分配 == 学校总量
        department_hours = sum(
            (Decimal(row['hours']) for row in departments.values()), Decimal('0'))
        self.assertEqual(department_hours, Decimal(school_overview(self.connection)['total_hours']))

    def test_department_allocation_expected_amounts(self):
        self.publish_initial()
        rows = {row['department']: row for row in department_overview(self.connection)}
        # w1: d1 7.01 d2 3.00；w3/w4: 各 d1 6.00 d2 4.00；
        # w6: d1 3.40 d2 3.30 d3 3.30；w7 同 w6；w5 在 v1 区间外(11-02)未分配；w2 未分配
        expected_d1 = Decimal('7.01') + Decimal('6.00') * 2 + Decimal('3.40') * 2
        expected_d2 = Decimal('3.00') + Decimal('4.00') * 2 + Decimal('3.30') * 2
        expected_d3 = Decimal('3.30') * 2
        self.assertEqual(Decimal(rows['d1']['hours']), expected_d1)
        self.assertEqual(Decimal(rows['d2']['hours']), expected_d2)
        self.assertEqual(Decimal(rows['d3']['hours']), expected_d3)
        unassigned = next(row for row in department_overview(self.connection) if row['unassigned'])
        # w2(10.00) + w5(10.00) = 20.00
        self.assertEqual(unassigned['hours'], '20.00')

    def test_project_overview_structure(self):
        self.publish_initial()
        rows = {row['project']: row for row in project_overview(self.connection)}
        self.assertEqual(rows['p3']['participating_departments'], 3)
        self.assertEqual(rows['p2']['unassigned_hours'], '20.00')
        self.assertEqual(rows['p2']['versions'], ['p2-v1'])

    def test_explain_record_lineage(self):
        self.publish_initial()
        explanation = explain_record(self.connection, 'w1')
        self.assertEqual(explanation['selected_version'], 'p1-v1')
        self.assertEqual(explanation['quantized_hours'], '10.01')
        self.assertEqual(sum((Decimal(line['amount']) for line in explanation['lines']), Decimal('0')),
                         Decimal('10.01'))
        self.assertTrue(all('exact_share' in line and 'weight' in line for line in explanation['lines']))

    def test_recompute_subset_matches_full_lines(self):
        self.publish_initial()
        subset = recompute_records(self.connection, ['w3', 'w6'])
        full = {line['record']: line for line in allocation_lines(self.connection)}
        for line in subset:
            matching = [item for item in allocation_lines(self.connection, records=[line['record']])]
            self.assertTrue(matching)
        self.assertEqual({line['record'] for line in subset}, {'w3', 'w6'})


class BackfillAndBitemporalTests(BridgeWarehouseTestCase):
    def publish_with_expiry(self):
        self.publish_initial()
        publish_bridge(self.connection, self.backfill, published_at='2026-11-05T08:00:00+00:00')

    def test_preview_lists_affected_projects_and_days(self):
        self.publish_initial()
        impact = publish_bridge(self.connection, self.backfill,
                                published_at='2026-11-05T08:00:00+00:00')['impact']
        self.assertEqual(impact['project'], 'p2')
        self.assertEqual(impact['records'][0]['current_version'], 'p2-v1')
        # 新版本有效期开放，既覆盖 v1 内的 10 月事实，也接管原本未分配的 11-02
        self.assertEqual(impact['affected_days'], ['2026-10-05', '2026-10-31', '2026-11-02'])
        orphan = next(row for row in impact['records'] if row['record'] == 'w5')
        self.assertIsNone(orphan['current_version'])

    def test_switch_is_atomic_and_historical_as_of_still_queryable(self):
        self.publish_with_expiry()
        # 发布后：院系与学校总览同时切换到新比例
        lines_now = allocation_lines(self.connection, records=['w3'])
        self.assertEqual({line['department']: line['amount'] for line in lines_now},
                         {'d1': '5.00', 'd2': '3.00', 'd3': '2.00'})
        # 历史时刻（发布前）仍按旧版本解析
        lines_then = allocation_lines(self.connection, records=['w3'],
                                      as_of='2026-11-05T07:59:59+00:00')
        self.assertEqual({line['department']: line['amount'] for line in lines_then},
                         {'d1': '6.00', 'd2': '4.00'})
        # 两个时点总量都守恒
        for as_of in (None, '2026-11-05T07:59:59+00:00'):
            self.assertTrue(school_overview(self.connection, as_of=as_of)['conservation_ok'])

    def test_backfill_conservation_across_periods(self):
        self.publish_with_expiry()
        before = school_overview(self.connection, as_of='2026-11-05T07:59:59+00:00')
        after = school_overview(self.connection)
        self.assertEqual(before['total_hours'], after['total_hours'])
        self.assertTrue(after['conservation_ok'])
        # 分配结构变了：d3 在回溯后获得 p2 的份额
        dep_after = {r['department']: r for r in department_overview(self.connection)}
        self.assertIn('d3', dep_after)

    def test_unassigned_backfill_then_conserved(self):
        # 先给 p3 发布 9 月版本，w6/w7 之外补一档；再发布 10 月版本，未分配减少而总量不变
        publish_bridge(self.connection, load_json(ZERO)[0],
                       published_at='2026-09-01T00:00:00+00:00')
        before = school_overview(self.connection)
        self.publish_initial()
        after = school_overview(self.connection)
        self.assertEqual(before['total_hours'], after['total_hours'])
        self.assertLess(Decimal(after['unassigned_hours']), Decimal(before['unassigned_hours']))
        self.assertTrue(after['conservation_ok'])


class CatalogCompatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'warehouse.sqlite3'
        self.connection = connect(self.path)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_legacy_overview_and_replay_still_work(self):
        self.assertTrue(load_file(self.connection, SOURCE, 'daily'))
        result = overview(self.connection)
        # 遗留单归属视图仍可跑（projects.department 全量汇总，不经过桥接）
        self.assertEqual(sum((Decimal(row['hours']) for row in result), Decimal('0')), Decimal('70.01'))
        self.assertFalse(load_file(self.connection, SOURCE, 'daily'))

    def test_bridge_tables_reject_direct_bundle_load(self):
        with self.assertRaises(ValueError):
            load_bundle(self.connection, {'bridge_members': []}, 'sneaky')

    def test_duplicate_key_rolls_back(self):
        bundle = load_json(SOURCE)
        broken = copy.deepcopy(bundle)
        broken['work'].append(copy.deepcopy(broken['work'][0]))
        with self.assertRaises(sqlite3.IntegrityError):
            load_bundle(self.connection, broken, 'broken')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM work').fetchone()[0], 0)

    def test_export_roundtrip_of_mart_lines(self):
        load_bundle(self.connection, load_json(SOURCE), 'facts')
        for spec in load_json(INITIAL):
            publish_bridge(self.connection, spec)
        rows = allocation_lines(self.connection)
        store = ObjectStore(Path(self.temporary.name) / 'objects')
        self.assertEqual(store.read(store.put(rows)), rows)


if __name__ == '__main__':
    unittest.main()
