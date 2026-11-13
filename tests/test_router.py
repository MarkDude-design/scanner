import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from email.message import Message
from urllib.request import BaseHandler, build_opener
from urllib.response import addinfourl

from fsr.cli import main
from fsr.core import connect, fetch, parse, public_url, stamp, sync


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

    def test_stale_overlapping_watch_cannot_roll_back_fresh_item(self):
        a = {'name': 'a', 'kind': 'discourse', 'url': 'https://example.invalid/a.json'}
        b = dict(a, name='b', url='https://example.invalid/b.json')
        def payload(title, date):
            return json.dumps({'topic_list': {'topics': [
                {'id': 123, 'title': title, 'last_posted_at': date}]}}).encode()
        old = payload('Old', '2026-11-13T10:00:00Z')
        new = payload('New', '2026-11-13T11:00:00Z')
        sync(self.db, [a, b], self.fetcher(old))
        sync(self.db, [a], self.fetcher(new))
        before = self.db.execute('SELECT changed_at FROM items').fetchone()[0]
        self.assertEqual(sync(self.db, [b], self.fetcher(old))[0]['changed'], 0)
        sync(self.db, [a], lambda s, c: (None, c))
        row = self.db.execute('SELECT title,occurred_at,changed_at FROM items').fetchone()
        self.assertEqual(row['title'], 'New')
        self.assertEqual(row['occurred_at'], '2026-11-13T11:00:00+00:00')
        self.assertEqual(row['changed_at'], before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 2)

    def test_atom_xhtml_nested_content_and_edits(self):
        source = {'name': 'atom', 'kind': 'rss', 'url': 'https://example.invalid/feed'}
        def payload(word):
            return ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x</id>'
                    '<title type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">Actual title</div></title>'
                    '<content type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>Actual <b>'
                    + word + '</b></p></div></content></entry></feed>').encode()
        sync(self.db, [source], self.fetcher(payload('body')))
        row = self.db.execute('SELECT title,body FROM items').fetchone()
        self.assertEqual(row['title'], 'Actual title')
        self.assertEqual(row['body'], 'Actual body')
        self.assertEqual(sync(self.db, [source], self.fetcher(payload('revision')))[0]['changed'], 1)
        self.assertEqual(self.db.execute('SELECT body FROM items').fetchone()[0], 'Actual revision')

    def test_xml_declarations_refused_for_utf8_and_utf16_variants(self):
        source = {'name': 'feed', 'kind': 'rss', 'url': 'https://example.invalid/feed'}
        xml = '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel><item><guid>x</guid><title>&x;</title></item></channel></rss>'
        for encoding in ('utf-8', 'utf-16', 'utf-16-le', 'utf-16-be'):
            with self.subTest(encoding=encoding), self.assertRaises((ValueError, UnicodeDecodeError)):
                list(parse(source, xml.encode(encoding)))
        with self.assertRaises(ValueError):
            list(parse(source, b'<?xml version="1.0" encoding="iso-8859-1"?><rss/>'))

    def test_redirect_targets_are_rejected_before_transport(self):
        for target in ('http://example.invalid/target', 'https://example.invalid/target?token=private',
                       'https://user:pass@example.invalid/target', 'https://example.invalid/allowed'):
            sent = []
            class Transport(BaseHandler):
                handler_order = 400
                def https_open(self, req):
                    sent.append(req.full_url)
                    headers = Message()
                    if req.full_url.endswith('/origin'):
                        headers['Location'] = target
                        code, body = 302, b''
                    else:
                        code, body = 200, b'{"items":[]}'
                    response = addinfourl(io.BytesIO(body), headers, req.full_url, code)
                    response.msg = 'Found' if code == 302 else 'OK'
                    return response
                http_open = https_open
            source = {'kind': 'rss', 'url': 'https://example.invalid/origin'}
            with patch('fsr.core.build_opener', lambda handler: build_opener(handler, Transport())):
                if target.endswith('/allowed'):
                    self.assertEqual(fetch(source, {})[0], b'{"items":[]}')
                    self.assertEqual(sent, [source['url'], target])
                else:
                    with self.assertRaises(ValueError):
                        fetch(source, {})
                    self.assertEqual(sent, [source['url']])
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
