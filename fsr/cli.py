import argparse
from datetime import datetime, timedelta, timezone
import json
from .core import config, connect, sync


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read selected activity into a local evidence store; never post.')
    parser.add_argument('--config', default='config.toml')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'sources', 'status'):
        sub.add_parser(name)
    run = sub.add_parser('sync')
    run.add_argument('source', nargs='?')
    recent = sub.add_parser('recent')
    recent.add_argument('--source')
    recent.add_argument('--since', default='7d', help='lookback by local observed-change time, e.g. 24h or 7d')
    recent.add_argument('--limit', type=int, default=20)
    recent.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        path, sources = config(args.config)
        with connect(path) as db:
            if args.command == 'doctor':
                print('Config and local database OK. Remote reachability is checked by sync; no requests made.')
                print('Snapshot window only; Matrix, Forgejo and complete-history cursors are not implemented.')
            elif args.command == 'sources':
                for source in sources:
                    print(source['name'] + ': ' + source['kind'])
            elif args.command == 'status':
                print(json.dumps([dict(r) for r in db.execute('SELECT name,last_success,last_error FROM sources')], indent=2))
            elif args.command == 'sync':
                selected = [s for s in sources if not args.source or s['name'] == args.source]
                if not selected:
                    raise ValueError('Unknown source')
                results = sync(db, selected)
                print(json.dumps(results, indent=2))
                return 0 if all(r['ok'] for r in results) else 1
            else:
                if not 1 <= args.limit <= 1000:
                    raise ValueError('limit must be 1..1000')
                span = args.since
                if len(span) < 2 or span[-1] not in 'hd' or not span[:-1].isdigit():
                    raise ValueError('since must be hours/days, e.g. 24h or 7d')
                hours = int(span[:-1]) * (24 if span[-1] == 'd' else 1)
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
                rows = db.execute('''SELECT DISTINCT i.* FROM items i JOIN source_items s ON s.item=i.id
                  WHERE i.changed_at>=? AND (? IS NULL OR s.source=?)
                  ORDER BY i.changed_at DESC, i.occurred_at DESC, i.id DESC LIMIT ?''', (cutoff, args.source, args.source, args.limit))
                items = [dict(r) for r in rows]
                if args.json:
                    print(json.dumps(items, ensure_ascii=False, indent=2))
                else:
                    for row in items:
                        print(f"{row['changed_at']}  {row['title']}\n  {row['url']}\n  source time: {row['occurred_at'] or 'unknown'}")
    except (OSError, ValueError, KeyError) as exc:
        print('Configuration/command error: ' + type(exc).__name__)
        return 2
    return 0
