import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from fsr.cli import main
from fsr.core import connect, parse, public_url, stamp, sync


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.db = connect(self.path)
        self.source = {'name': 'demo', 'kind': 'fixture', 'path': 'unused'}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def payload(self, title='Original'):
        return json.dumps({'items': [{'id': 'one', 'title': title, 'url': 'https://example.invalid/1'}]}).encode()

    def fetcher(self, payload):
        return lambda source, cursor: (payload, {'etag': 'v1'})

    def test_second_sync_and_restart_do_not_duplicate(self):
        self.assertEqual(sync(self.db, [self.source], self.fetcher(self.payload()))[0]['changed'], 1)
        self.db.close()
        self.db = connect(self.path)
        cursors = []
        def second(source, cursor):
            cursors.append(cursor)
            return self.payload(), {'etag': 'v1'}
        self.assertEqual(sync(self.db, [self.source], second)[0]['changed'], 0)
        self.assertEqual(cursors, [{'etag': 'v1'}])
        self.assertEqual(self.db.execute('SELECT count(*) FROM items').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 1)

    def test_change_retains_old_evidence(self):
        sync(self.db, [self.source], self.fetcher(self.payload()))
        sync(self.db, [self.source], self.fetcher(self.payload('Revised')))
        self.assertEqual(self.db.execute('SELECT title FROM items').fetchone()[0], 'Revised')
        self.assertEqual(self.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 2)

    def test_bad_batch_does_not_store_partial_items_or_advance_cursor(self):
        sync(self.db, [self.source], self.fetcher(self.payload()))
        broken = b'{"items":[{"id":"two","title":"new"},{"title":"missing ID"}]}'
        result = sync(self.db, [self.source], lambda s, c: (broken, {'etag': 'v2'}))
        self.assertFalse(result[0]['ok'])
        self.assertEqual(self.db.execute('SELECT cursor FROM sources').fetchone()[0], '{"etag": "v1"}')
        self.assertEqual(self.db.execute('SELECT count(*) FROM items').fetchone()[0], 1)

    def test_failed_source_does_not_block_others_or_echo_error_body(self):
        bad = dict(self.source, name='bad')
        good = dict(self.source, name='good')
        def mixed(source, cursor):
            if source['name'] == 'bad':
                raise OSError('fake-private-token')
            return self.payload(), {}
        results = sync(self.db, [bad, good], mixed)
        self.assertFalse(results[0]['ok'])
        self.assertTrue(results[1]['ok'])
        self.assertNotIn('fake-private-token', json.dumps(results))

    def test_not_modified_preserves_data_and_cursor(self):
        sync(self.db, [self.source], self.fetcher(self.payload()))
        results = sync(self.db, [self.source], lambda s, c: (None, c))
        self.assertEqual(results[0]['changed'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)

    def test_different_watches_share_native_discourse_item(self):
        payload = b'{"topic_list":{"topics":[{"id":123,"title":"Public topic","last_posted_at":"2026-11-13T00:00:00Z"}]}}'
        sources = [{'name': n, 'kind': 'discourse', 'url': 'https://example.invalid/' + n + '.json'} for n in ('a', 'b')]
        sync(self.db, sources, self.fetcher(payload))
        self.assertEqual(self.db.execute('SELECT count(*) FROM items').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_items').fetchone()[0], 2)

    def test_counter_change_is_preserved_without_new_reading_task(self):
        source = {'name': 'topics', 'kind': 'discourse', 'url': 'https://example.invalid/latest.json'}
        topic = {'id': 123, 'title': 'Topic', 'views': 10,
                 'last_posted_at': '2026-11-13T10:00:00Z'}
        def payload():
            return json.dumps({'topic_list': {'topics': [topic]}}).encode()
        self.assertEqual(sync(self.db, [source], self.fetcher(payload()))[0]['changed'], 1)
        before = self.db.execute('SELECT changed_at FROM items').fetchone()[0]
        topic['views'] = 11
        self.assertEqual(sync(self.db, [source], self.fetcher(payload()))[0]['changed'], 0)
        self.assertEqual(self.db.execute('SELECT changed_at FROM items').fetchone()[0], before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 2)
        topic['last_posted_at'] = '2026-11-13T11:00:00Z'
        self.assertEqual(sync(self.db, [source], self.fetcher(payload()))[0]['changed'], 1)

    def test_old_raw_hash_does_not_create_a_false_migration_alert(self):
        sync(self.db, [self.source], self.fetcher(self.payload()))
        before = self.db.execute('SELECT changed_at FROM items').fetchone()[0]
        with self.db:
            self.db.execute("UPDATE items SET hash='legacy-raw-hash'")
        self.assertEqual(sync(self.db, [self.source], self.fetcher(self.payload()))[0]['changed'], 0)
        self.assertEqual(self.db.execute('SELECT changed_at FROM items').fetchone()[0], before)

    def test_rss_and_atom_and_entity_refusal(self):
        source = {'name': 'feed', 'kind': 'rss', 'url': 'https://example.invalid/feed'}
        rss = b'<rss version="2.0"><channel><item><guid>x</guid><title>Example</title><description>&lt;p&gt;Body&lt;/p&gt;</description></item></channel></rss>'
        item = list(parse(source, rss))[0]
        self.assertEqual(item['body'], 'Body')
        self.assertIsNone(item['occurred_at'])
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x</id><title>Atom</title><link href="https://example.invalid/x"/></entry></feed>'
        self.assertEqual(list(parse(source, atom))[0]['url'], 'https://example.invalid/x')
        with self.assertRaises(ValueError):
            list(parse(source, b'<!DOCTYPE rss><rss/>'))

    def test_source_identity_cannot_silently_change(self):
        sync(self.db, [self.source], self.fetcher(self.payload()))
        changed = dict(self.source, path='different')
        self.assertFalse(sync(self.db, [changed], self.fetcher(self.payload()))[0]['ok'])

    def test_credential_urls_rejected(self):
        for value in ('http://example.invalid/a', 'https://user:pass@example.invalid/a', 'https://example.invalid/a?token=secret'):
            with self.assertRaises(ValueError):
                public_url(value)

    def test_known_dates_normalize_without_inventing_missing_timezone(self):
        self.assertEqual(stamp('Fri, 13 Nov 2026 10:00:00 +0000'), '2026-11-13T10:00:00+00:00')
        self.assertEqual(stamp('2026-11-13T10:00:00Z'), '2026-11-13T10:00:00+00:00')
        self.assertIsNone(stamp('2026-11-13T10:00:00'))
        self.assertIsNone(stamp('unknown'))

    def test_cli_demo_sync_then_recent(self):
        root = Path(self.tmp.name)
        (root / 'data.json').write_bytes(self.payload())
        (root / 'config.toml').write_text('database="cli.sqlite"\n[[sources]]\nname="demo"\nkind="fixture"\npath="data.json"\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['--config', str(root / 'config.toml'), 'sync']), 0)
            self.assertEqual(main(['--config', str(root / 'config.toml'), 'recent', '--json']), 0)
        self.assertIn('Original', output.getvalue())


if __name__ == '__main__':
    unittest.main()
