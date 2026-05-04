# File Harbour

File Harbour is a one-way client/server file sync project. A client watches a local folder, queues file changes, and uploads them to a central HTTPS server. The server stores the latest uploaded copy of each file and applies deletes from the client.

## What This Version Demonstrates

- Real-time file watching with `watchdog`
- SQLite-backed file index and retry queue
- HTTP upload/delete sync flow with FastAPI
- Shared-secret API authentication
- Optional HTTPS with auto-generated self-signed certificates
- Conflict copies when the server detects a stale client upload

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the server

```bash
python main.py run --server
```

The first run creates `.env`, `cert.pem`, and `key.pem`.

### 3. Point a client at the server

Set the same `API_KEY` on the client machine and update `SERVER_URL` in `.env`:

```env
SERVER_URL=https://192.168.1.10:8443
API_KEY=use-the-same-secret-on-client-and-server
```

### 4. Start the client

```bash
python main.py run --client
```

Changes inside `SYNC_FOLDER` will be uploaded to the server.

## CLI

| Command | What it does |
|---|---|
| `python main.py run` | Start both the server and the local watching client |
| `python main.py run --server` | Run only the HTTPS server |
| `python main.py run --client` | Run only the watching client |
| `python main.py status` | Show the current config, indexed file count, and queue depth |
| `python main.py reset` | Delete `.env` and generated certificates |
| `python main.py --version` | Show the version |

## Configuration

Settings live in `.env`.

| Setting | Default | What it controls |
|---|---|---|
| `SYNC_FOLDER` | `./data/sync_folder` | Folder to watch locally or store uploaded files |
| `SERVER_URL` | `https://localhost:8443` | Central server URL the client uploads to |
| `PORT` | `8443` | HTTPS port used by the server |
| `API_KEY` | auto-generated | Shared secret used by both client and server |
| `NODE_ID` | auto-generated | Friendly node label for logs/conflicts |
| `SSL_CERT` | `./cert.pem` | TLS certificate path |
| `SSL_KEY` | `./key.pem` | TLS private key path |
| `DB_PATH` | `./data/sync.db` | SQLite database path |
| `LOG_LEVEL` | `INFO` | Console/file logging level |
| `MAX_UPLOAD_MB` | `500` | Maximum accepted upload size |
| `VERIFY_TLS` | `false` | Whether the client verifies the server certificate |

### `.syncignore`

Patterns in `.syncignore` are excluded from sync.

## How It Works

1. The client scans `SYNC_FOLDER` on startup and records files in SQLite.
2. New, changed, and deleted files are pushed into a local sync queue.
3. A background worker uploads or deletes files on the central server.
4. Failed tasks stay in the queue and retry with exponential backoff.
5. The server writes files atomically and updates its own file index.

## Running Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

## Building

```bash
pip install pyinstaller
pyinstaller synccore.spec --noconfirm --clean
```

## Project Layout

```text
SyncCore/
├── main.py              # CLI entry point
├── config.py            # Settings and .env management
├── core/
│   ├── server.py        # FastAPI upload/delete server
│   ├── client.py        # Upload/delete client for the central server
│   ├── engine.py        # Initial folder scan
│   ├── watcher.py       # Real-time file watching
│   ├── queue_worker.py  # Retry queue processor
│   └── orchestrator.py  # Lifecycle management
├── utils/
│   ├── auth.py          # API key auth dependency
│   ├── certs.py         # TLS certificate generation
│   ├── conflict.py      # Conflict-copy helper
│   ├── file_index.py    # SQLite metadata and queue
│   ├── file_ops.py      # Hashing and compression helpers
│   ├── filters.py       # .syncignore support
│   ├── logging.py       # Console/file logging
│   ├── paths.py         # Path safety checks
│   └── resilience.py    # Rename detection and atomic write helpers
└── tests/
    └── test_sync.py     # Test suite
```
