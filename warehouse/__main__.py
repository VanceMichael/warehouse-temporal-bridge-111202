import argparse
import json
from pathlib import Path

from .bridge import (
    BridgeError,
    allocation_lines,
    expire_bridge,
    load_spec_file,
    preview_impact,
    publish_bridge,
    recompute_records,
    spec_digest,
    submit_bridge,
)
from .catalog import connect, load_file
from .domain import overview
from .mart import department_overview, explain_record, project_overview, school_overview
from .store import ObjectStore


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _maybe_export(rows, objects):
    if not objects:
        return rows
    digest = ObjectStore(objects).put(rows)
    return {'object': digest, 'rows': len(rows)}


def build_parser():
    parser = argparse.ArgumentParser(description='带有效期多对多桥接的本地数据集市')
    parser.add_argument('command', choices=[
        'import', 'inspect', 'export',
        'bridge-submit', 'bridge-preview', 'bridge-publish', 'bridge-expire',
        'mart-lines', 'mart-department', 'mart-school', 'mart-project',
        'explain', 'recompute',
    ])
    parser.add_argument('--db', required=True)
    parser.add_argument('--input')
    parser.add_argument('--batch')
    parser.add_argument('--objects')
    parser.add_argument('--bridge')
    parser.add_argument('--version')
    parser.add_argument('--project')
    parser.add_argument('--as-of')
    parser.add_argument('--published-at')
    parser.add_argument('--day-from')
    parser.add_argument('--day-to')
    parser.add_argument('--record', action='append')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'import' and (not args.input or not args.batch):
        parser.error('导入需要 --input 和 --batch')
    if args.command in {'bridge-submit', 'bridge-preview', 'bridge-publish'} and not args.bridge:
        parser.error('桥接命令需要 --bridge 规格文件')
    if args.command == 'bridge-expire' and not (args.version and args.project and args.day_to):
        parser.error('bridge-expire 需要 --version、--project 与 --day-to')
    if args.command == 'export' and not args.objects:
        parser.error('导出需要 --objects')
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(args.db)
    try:
        if args.command == 'import':
            _emit({'changed': load_file(connection, args.input, args.batch)})
        elif args.command in {'bridge-submit', 'bridge-preview', 'bridge-publish'}:
            specs = load_spec_file(args.bridge)
            results = []
            for spec in specs:
                digest, header, _members = spec_digest(spec)
                if args.command == 'bridge-submit':
                    results.append(submit_bridge(connection, spec))
                elif args.command == 'bridge-preview':
                    results.append(preview_impact(connection, header))
                else:
                    batch = f"publish:{digest}" if not args.batch else f"{args.batch}:{digest}"
                    results.append(publish_bridge(
                        connection, spec, published_at=args.published_at, batch=batch,
                    ))
            _emit(results if len(results) > 1 else results[0])
        elif args.command == 'bridge-expire':
            _emit(expire_bridge(connection, args.version, args.project, args.day_to))
        elif args.command == 'inspect':
            _emit(overview(connection))
        elif args.command == 'export':
            records = overview(connection)
            _emit({'object': ObjectStore(args.objects).put(records), 'rows': len(records)})
        elif args.command == 'mart-lines':
            rows = allocation_lines(
                connection, day_from=args.day_from, day_to=args.day_to,
                as_of=args.as_of, records=args.record,
            )
            _emit(_maybe_export(rows, args.objects))
        elif args.command == 'mart-department':
            _emit(_maybe_export(department_overview(connection, as_of=args.as_of), args.objects))
        elif args.command == 'mart-project':
            _emit(_maybe_export(project_overview(connection, as_of=args.as_of), args.objects))
        elif args.command == 'mart-school':
            _emit(school_overview(connection, as_of=args.as_of))
        elif args.command == 'explain':
            if not args.record or len(args.record) != 1:
                parser.error('explain 需要恰好一个 --record')
            _emit(explain_record(connection, args.record[0], as_of=args.as_of))
        elif args.command == 'recompute':
            if not args.record:
                parser.error('recompute 至少需要一个 --record')
            _emit(_maybe_export(recompute_records(connection, args.record, as_of=args.as_of), args.objects))
    except BridgeError as exc:
        parser.exit(2, f'桥接约束失败: {exc}\n')
    finally:
        connection.close()


if __name__ == '__main__':
    main()
