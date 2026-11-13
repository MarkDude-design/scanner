"""Read-only snapshot adapters and transactional, evidence-preserving storage."""
import hashlib
import json
import re
import sqlite3
import tomllib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

MAX_BYTES = 2 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def public_url(value):
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('Use an explicit HTTPS URL without credentials, query or fragment')
    return value


class CleanRedirect(HTTPRedirectHandler):
    """Validate every resolved redirect target before urllib sends a request."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def config(path):
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding='utf-8'))
    sources = data.get('sources', [])
    if not sources:
        raise ValueError('Configure at least one source')
    names = set()
    for source in sources:
        name = source.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Source names must be nonempty and unique')
        names.add(name)
        if source.get('kind') not in ('fixture', 'rss', 'discourse'):
            raise ValueError('Supported kinds: fixture, rss, discourse')
        if source['kind'] == 'fixture':
            source['path'] = str((path.parent / source['path']).resolve())
        else:
            public_url(source['url'])
    return (path.parent / data.get('database', 'data/fsr.sqlite')).resolve(), sources


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS sources (
        name TEXT PRIMARY KEY, identity TEXT NOT NULL, cursor TEXT NOT NULL DEFAULT '{}',
        last_success TEXT, last_error TEXT);
      CREATE TABLE IF NOT EXISTS snapshots (
        source TEXT REFERENCES sources(name), hash TEXT, fetched_at TEXT, payload BLOB,
        PRIMARY KEY(source, hash));
      CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY, kind TEXT, origin TEXT, native_id TEXT,
        title TEXT, body TEXT, url TEXT, occurred_at TEXT, changed_at TEXT, hash TEXT,
        UNIQUE(kind, origin, native_id));
      CREATE TABLE IF NOT EXISTS source_items (
        source TEXT REFERENCES sources(name), item INTEGER REFERENCES items(id),
        PRIMARY KEY(source,item));
      CREATE TABLE IF NOT EXISTS revisions (
        item INTEGER REFERENCES items(id), hash TEXT, fetched_at TEXT, raw TEXT,
        PRIMARY KEY(item,hash));
    ''')
    return db


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value):
    parser = Text()
    parser.feed(value or '')
    return ' '.join(' '.join(parser.parts).split())


def atom_text(element):
    if element is None:
        return ''
    kind = element.get('type', 'text')
    if kind in ('xhtml', 'application/xhtml+xml'):
        return ' '.join(' '.join(element.itertext()).split())
    value = element.text or ''
    if kind in ('html', 'text/html'):
        return plain(value)
    return ' '.join(value.split())


def stamp(value):
    """Normalize a known source date; original date remains in raw evidence."""
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            return None
    if date.tzinfo is None:
        return None
    return date.astimezone(timezone.utc).isoformat()


def parse(source, payload):
    kind = source['kind']
    if kind in ('fixture', 'discourse'):
        data = json.loads(payload)
        if kind == 'fixture':
            rows = data['items']
            origin = source.get('origin', 'synthetic-fixture')
        else:
            rows = data['topic_list']['topics']
            p = urlsplit(source['url'])
            origin = urlunsplit((p.scheme, p.netloc, '', '', ''))
        if not isinstance(rows, list):
            raise ValueError('Expected a topic/item list')
        for row in rows:
            native = str(row['id'])
            if not native:
                raise ValueError('Missing stable item ID')
            yield dict(native_id=native, origin=origin, title=row.get('title', ''),
                       body=row.get('body', ''),
                       url=row.get('url', origin + '/t/' + native),
                       occurred_at=stamp(row.get('updated_at') or row.get('last_posted_at') or row.get('created_at')),
                       raw=row)
        return
    # Only UTF-8 XML is supported. Decode before declaration checks; refuse
    # NULs/other declared encodings so UTF-16 cannot bypass an ASCII-byte scan.
    xml = payload.decode('utf-8-sig')
    if '\x00' in xml:
        raise ValueError('Only UTF-8 XML is supported')
    declaration = re.match(r'\s*<\?xml\b[^?]*\?>', xml, re.IGNORECASE)
    if declaration:
        encoding = re.search(r'''\bencoding\s*=\s*["']([^"']+)["']''', declaration.group(), re.IGNORECASE)
        if encoding and encoding.group(1).lower() not in ('utf-8', 'utf8'):
            raise ValueError('Only UTF-8 XML is supported')
    upper = xml.upper()
    if '<!DOCTYPE' in upper or '<!ENTITY' in upper:
        raise ValueError('DTD/entity declarations are not accepted')
    root = ET.fromstring(xml)
    atom = '{http://www.w3.org/2005/Atom}'
    if root.tag == 'rss':
        rows = root.findall('./channel/item')
    elif root.tag == atom + 'feed':
        rows = root.findall(atom + 'entry')
    else:
        raise ValueError('Expected RSS 2.0 or Atom feed')
    for row in rows:
        is_atom = row.tag.startswith(atom)
        if is_atom:
            links = row.findall(atom + 'link')
            link = next((x.get('href', '') for x in links if x.get('rel', 'alternate') == 'alternate'), '')
            native = row.findtext(atom + 'id') or link
            title = atom_text(row.find(atom + 'title'))
            content = row.find(atom + 'content')
            if content is None:
                content = row.find(atom + 'summary')
            body = atom_text(content)
            date = row.findtext(atom + 'updated') or row.findtext(atom + 'published')
        else:
            link = row.findtext('link', '')
            native = row.findtext('guid') or link
            title, body, date = row.findtext('title', ''), row.findtext('description', ''), row.findtext('pubDate')
        if not native:
            raise ValueError('Feed item lacks a stable GUID/ID or link')
        p = urlsplit(source['url'])
        # Feed IDs are scoped to a channel, not to the hosting site.
        origin = urlunsplit((p.scheme, p.netloc, p.path or '/', '', ''))
        yield dict(native_id=native, origin=origin,
                   title=title if is_atom else plain(title), body=body if is_atom else plain(body),
                   url=link, occurred_at=stamp(date), raw=ET.tostring(row, encoding='unicode'))


def fetch(source, cursor):
    if source['kind'] == 'fixture':
        with Path(source['path']).open('rb') as handle:
            payload = handle.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError('Source exceeds size limit')
        return payload, {}
    headers = {'User-Agent': 'Fedora-Signal-Router/0.1 (read-only personal feed collector)'}
    if cursor.get('etag'):
        headers['If-None-Match'] = cursor['etag']
    if cursor.get('modified'):
        headers['If-Modified-Since'] = cursor['modified']
    try:
        opener = build_opener(CleanRedirect())
        with opener.open(Request(public_url(source['url']), headers=headers, method='GET'), timeout=15) as response:
            # Defense in depth: every redirect has already been checked.
            public_url(response.url)
            payload = response.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise ValueError('Source exceeds size limit')
            return payload, {k: v for k, v in (
                ('etag', response.headers.get('ETag')),
                ('modified', response.headers.get('Last-Modified'))) if v}
    except HTTPError as exc:
        if exc.code == 304:
            return None, cursor
        # Do not echo response bodies, request headers, or credential-like URLs.
        raise RuntimeError(f'HTTP {exc.code}; no retry performed') from None


def sync_one(db, source, fetcher=fetch):
    name = source['name']
    identity = json.dumps({k: v for k, v in source.items() if k != 'name'}, sort_keys=True)
    existing = db.execute('SELECT * FROM sources WHERE name=?', (name,)).fetchone()
    if existing and existing['identity'] != identity:
        raise ValueError('Source definition changed; use a new source name')
    if source['kind'] == 'rss':
        p = urlsplit(source['url'])
        legacy_origin = urlunsplit((p.scheme, p.netloc, '', '', ''))
        if db.execute('SELECT 1 FROM items WHERE kind=? AND origin=? LIMIT 1',
                      ('rss', legacy_origin)).fetchone():
            raise RuntimeError('Legacy feed namespace: back up the database and use a new database; '
                               'old feed IDs cannot be safely reassigned automatically')
    with db:
        db.execute('INSERT OR IGNORE INTO sources(name,identity) VALUES (?,?)', (name, identity))
    cursor = json.loads(existing['cursor']) if existing else {}
    payload, next_cursor = fetcher(source, cursor)
    changed = 0
    when = now()
    # Fetch/parse completes before one transaction advances data and cursor.
    entries = list(parse(source, payload)) if payload is not None else []
    with db:
        if payload is not None:
            digest = hashlib.sha256(payload).hexdigest()
            db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)', (name, digest, when, payload))
        for item in entries:
            raw = json.dumps(item['raw'], ensure_ascii=False, sort_keys=True)
            raw_digest = hashlib.sha256(raw.encode()).hexdigest()
            # Preserve counter-only changes as evidence, without making them
            # fresh reading tasks. last_posted_at is normalized to occurred_at,
            # so a new reply still counts even when the title is unchanged.
            fields = ('title', 'body', 'url', 'occurred_at')
            signal = {key: item[key] for key in fields}
            digest = hashlib.sha256(json.dumps(signal, sort_keys=True).encode()).hexdigest()
            key = (source['kind'], item['origin'], item['native_id'])
            old = db.execute('SELECT * FROM items WHERE kind=? AND origin=? AND native_id=?', key).fetchone()
            previous = {field: old[field] for field in fields} if old else None
            if previous:
                previous['occurred_at'] = stamp(previous['occurred_at'])
            # Delayed overlapping watches must not roll a known newer source
            # activity version backwards. Retain stale raw evidence below.
            is_stale = bool(previous and previous['occurred_at'] and item['occurred_at'] and
                            datetime.fromisoformat(item['occurred_at']) <
                            datetime.fromisoformat(previous['occurred_at']))
            if not is_stale and previous != signal:
                db.execute('''INSERT INTO items(kind,origin,native_id,title,body,url,occurred_at,changed_at,hash)
                  VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,origin,native_id) DO UPDATE SET
                  title=excluded.title,body=excluded.body,url=excluded.url,occurred_at=excluded.occurred_at,
                  changed_at=excluded.changed_at,hash=excluded.hash''',
                  (*key, item['title'], item['body'], item['url'], item['occurred_at'], when, digest))
                changed += 1
            elif not is_stale:
                # Upgrade the pre-review raw hash/date representation without
                # changing observed-change time or manufacturing new activity.
                db.execute('UPDATE items SET hash=?,occurred_at=? WHERE id=?',
                           (digest, item['occurred_at'], old['id']))
            item_id = db.execute('SELECT id FROM items WHERE kind=? AND origin=? AND native_id=?', key).fetchone()['id']
            db.execute('INSERT OR IGNORE INTO source_items VALUES (?,?)', (name, item_id))
            db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?,?)', (item_id, raw_digest, when, raw))
        db.execute('UPDATE sources SET cursor=?,last_success=?,last_error=NULL WHERE name=?',
                   (json.dumps(next_cursor), when, name))
    return changed


def sync(db, sources, fetcher=fetch):
    results = []
    for source in sources:
        try:
            count = sync_one(db, source, fetcher)
            results.append({'source': source['name'], 'changed': count, 'ok': True})
        except Exception as exc:
            # No raw exception body: local paths, URLs and tokens stay out of logs.
            message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            with db:
                db.execute('UPDATE sources SET last_error=? WHERE name=?', (message, source['name']))
            results.append({'source': source['name'], 'ok': False, 'error': message})
    return results
