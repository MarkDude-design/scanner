# Fedora Signal Router — first working slice

Read selected activity. Keep the evidence. Show what changed.

A personal, local-first collector for public Fedora activity. It makes GET
requests to explicitly configured sources and writes only to a local SQLite
database. It has no posting, editing, sending, model-call or account-management
commands. This is an independent prototype, not an official Fedora service.

## Try it without an account or network

Python 3.11+; no runtime dependencies or installation required. From the repo:

```sh
python -m fsr --config demo.toml doctor
python -m fsr --config demo.toml sync
python -m fsr --config demo.toml sync
python -m fsr --config demo.toml recent --since 7d
```

The second sync should report zero changes. The demo is **synthetic**, not real
Fedora activity. Optional installation: `python -m pip install -e .`, then `fsr`.

## Watch selected public activity

Copy `config.example.toml` to the ignored `config.toml`. It starts with one
public Docs tag, tested as a read-only topic-list fetch on 2026-11-13. You can
choose another public Discussion category/tag JSON endpoint or RSS/Atom feed:

```sh
python -m fsr sources
python -m fsr sync
python -m fsr recent --since 24h --limit 10
python -m fsr status
python -m fsr recent --json > /tmp/fsr-export.json
```

`--config PATH` goes before the command. Relative database/fixture paths resolve
beside that config. `sync SOURCE_NAME` and `recent --source SOURCE_NAME` restrict
the view. The example is a starting point, not a subscription to all Fedora
activity. Choose the small set of watches you actually want.
Live sources can rate-limit or be unreachable; no automatic retries are made.
`doctor` checks local configuration/database only; `sync` checks remote access.

## What this slice does

- Fixture, public Discourse topic-list JSON, UTF-8 RSS and Atom adapters (including
  Atom XHTML text constructs). Other XML encodings are explicitly refused.
- Incremental conditional GET using persisted ETag/Last-Modified when supplied.
- Stable native IDs, idempotent repeated syncs, and deduplication of an item
  appearing in overlapping watches of the same adapter kind/origin.
- A delayed watch cannot overwrite an item with a strictly older known source
  activity timestamp. Its raw evidence is still retained. Equal/unknown source
  timestamps cannot establish freshness; that ambiguity remains a limitation.
- Local raw snapshots, item revisions and source links. Updates retain prior
  evidence; source data and local fetch/change times remain distinct.
- Counter-only changes (views, likes, reply counts) are retained in raw evidence
  but do not become new reading tasks. A changed title/body/link or source
  activity timestamp does. A new reply updates Discourse's last-posted time.
- Transactional item/cursor updates; a malformed source cannot advance the
  successful cursor or erase its old records. One source failure does not block
  another. Status records failures without printing response bodies.
- A short recent-change view and JSON export, with no model summaries.

## Honest limits

This is the first **storage + public-source** slice, not the complete P0 design.
Discussion data are topic metadata, not full posts/private messages. Feed and
topic-list snapshots are bounded windows: activity that disappears between
syncs may be missed. Conditional GET is not a complete-history cursor. The tool
cannot prove no loss, and does not yet paginate history or detect deleted items.
`recent --since` uses local observed-change time, not historical source time.

Matrix sessions/decryption, Forgejo notifications/issues, project routing,
digest checkpoints, inline decisions, scheduling and a web UI are **not built**.
No unattended monitoring or twice-weekly delivery is enabled by this repository.
The fixture tests do not validate Fedora-hosted live behavior.

Collected data, personal config and credentials stay out of Git. Read only
sources you intend to watch; this version accepts clean HTTPS URLs without
embedded credentials or query parameters. Public source content is untrusted
data. Don't publish an aggregate personal activity database merely because its
individual source pages were public.

Each redirect is checked against that URL policy before it is followed. XML
is UTF-8 only and DTD/entity declarations are refused before parsing.

## Validation and next build

```sh
python -m unittest discover -s tests -v
```

Tests cover restart/deduplication, revision retention, failed-batch rollback,
source failure isolation, not-modified responses, overlapping watches, feed
parsing, source identity changes, URL policy and CLI flow. All fixtures are
synthetic. No test calls a live service, downloads weights or invokes a model.

Next: verify one selected real Discussion watch on the target machine; add
complete-history pagination/gap reporting; then an explicitly chosen Matrix
room with a locally kept read-only session. Keep the first human feedback about
whether the short view is useful before expanding integrations.

## Source/API references

- Fedora's [communication entry point](https://fedoraproject.org/start/)
- [Discourse API documentation](https://docs.discourse.org/)
- [Discourse feed documentation](https://meta.discourse.org/t/finding-discourse-rss-feeds/264134)

The repo name `scanner` is retained; the application working name is Fedora
Signal Router. Work started 2026-11-13. No Fedora account or group membership
claims are inferred by this software.
