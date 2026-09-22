# ADA Study Server — API Reference

Simple FastAPI backend with two jobs sharing one process:
1. **Extension telemetry** — logs events/pings from the Chrome extension, issues completion codes
2. **Survey backend** — stores Prolific participant progress/submissions, verifies codes

Base URL for local testing: `http://localhost:8000`

---

## Health

### `GET /healthz`
Basic liveness check.

```bash
curl -s http://localhost:8000/healthz
```
→ `{"ok": true}`

---

## Extension Telemetry

These endpoints are called by the Chrome extension. No auth — identified by `participant_uuid`.

### `GET /actions`
Lists all allowed `action` values accepted by `/events`.

```bash
curl -s http://localhost:8000/actions
```

### `POST /install`
Logs that the extension was installed.

```bash
curl -s -X POST http://localhost:8000/install \
  -H "Content-Type: application/json" \
  -d '{"participant_uuid": "test-uuid-1", "t_flag": 1}'
```

### `POST /enable`
Logs that the extension was enabled/toggled on.

```bash
curl -s -X POST http://localhost:8000/enable \
  -H "Content-Type: application/json" \
  -d '{"participant_uuid": "test-uuid-1", "t_flag": 1}'
```

### `POST /events`
Generic event logger. `action` must be one of the values from `/actions` (if provided). Strips sensitive keys (`password`, `email`, `token`, etc.) from `params`/`extras` before saving.

```bash
curl -s -X POST http://localhost:8000/events \
  -H "Content-Type: application/json" \
  -d '{
    "participant_uuid": "test-uuid-1",
    "event_type": "user_action",
    "action": "delete_activity_clicked"
  }'
```

### `POST /ping`
Heartbeat from the extension. **On the first ping ever for a given UUID**, generates and returns a 6-character completion code (used later in the survey to verify participation). Subsequent pings return `code: null`.

```bash
curl -s -X POST http://localhost:8000/ping \
  -H "Content-Type: application/json" \
  -d '{"participant_uuid": "test-uuid-1"}'
```
→ first call: `{"ok": true, "code": "AB3X9K"}`
→ later calls: `{"ok": true, "code": null}`

### `GET /tail`
Returns the last `n` events for a UUID (debugging aid).

```bash
curl -s "http://localhost:8000/tail?uuid=test-uuid-1&n=20"
```

### `GET /uninstall`
Called when the extension is removed. Logs the uninstall and shows a thank-you HTML page in the browser (this is a page, not a JSON API — meant to be opened as a link, not curled for data).

```bash
curl -s "http://localhost:8000/uninstall?participant_uuid=test-uuid-1"
```

---

## Survey Backend

Used by the React survey frontend. Identified by `prolific_id`.

### `GET /survey/check`
Checks if this Prolific worker has already submitted (used on the landing page to block re-entry).

```bash
curl -s "http://localhost:8000/survey/check?prolific_id=ABC123"
```
→ `{"exists": false}`

### `POST /survey/start`
Records first arrival. Idempotent — safe to call again. Returns `409` if already completed.

```bash
curl -s -X POST http://localhost:8000/survey/start \
  -H "Content-Type: application/json" \
  -d '{"prolific_id": "ABC123"}'
```

### `POST /survey/progress`
Saves answers after each subsection. Merges into existing progress — never wipes previous answers. Safe to call repeatedly.

```bash
curl -s -X POST http://localhost:8000/survey/progress \
  -H "Content-Type: application/json" \
  -d '{
    "prolific_id": "ABC123",
    "subsection": "section2",
    "answers": {"q1": "yes", "q2": "sometimes"}
  }'
```

### `POST /survey/verify-code`
Verifies the extension's completion code (from `/ping`) against the participant. On success, links `prolific_id` ↔ `participant_uuid` in the progress file.

```bash
curl -s -X POST http://localhost:8000/survey/verify-code \
  -H "Content-Type: application/json" \
  -d '{"prolific_id": "ABC123", "code": "AB3X9K"}'
```
→ success: `{"ok": true, "message": "Code verified successfully.", "uuid": "test-uuid-1"}`
→ failure: `{"ok": false, "message": "Code not recognised. Please check and try again."}`

### `POST /survey/submit`
Final submission. Merges all saved progress + final page answers into `submissions.ndjson`, then deletes the progress file. Idempotent — calling twice is a no-op the second time.

```bash
curl -s -X POST http://localhost:8000/survey/submit \
  -H "Content-Type: application/json" \
  -d '{"prolific_id": "ABC123", "answers": {"final_q": "done"}}'
```

---

## Admin — Downloading Data

### `GET /download-logs`
Downloads **everything** as one zip: event logs, ping logs, survey data (participants/submissions/progress), and `codes.json`.

```bash
curl -sJO http://localhost:8000/download-logs
```

### `GET /download-logs/{folder}`
Downloads just **one** folder as a zip. `{folder}` must be one of:

| Value    | What you get                                  |
|----------|------------------------------------------------|
| `events` | Extension event logs (`logs/`)                |
| `pings`  | Extension ping logs (`ping_logs/`)             |
| `survey` | All survey data (`survey_data/`)               |
| `codes`  | Just `codes.json`                              |

```bash
curl -sJO http://localhost:8000/download-logs/events
curl -sJO http://localhost:8000/download-logs/pings
curl -sJO http://localhost:8000/download-logs/survey
curl -sJO http://localhost:8000/download-logs/codes
```

An invalid folder name returns `422 Unprocessable Entity`, not a silent empty zip:
```bash
curl -i http://localhost:8000/download-logs/nonsense
```

### `GET /download-logs/participant/{uuid}` *(if added)*
Downloads just one participant's events + pings.

```bash
curl -sJO http://localhost:8000/download-logs/participant/test-uuid-1
```

---

## curl Cheat Sheet

| Flag | Meaning |
|------|---------|
| `-s` | Silent — hides progress bar (does **not** save to a file by itself) |
| `-o file.zip` | Save response body to `file.zip` |
| `-O` | Save using the filename from the URL |
| `-J` | Use the server's `Content-Disposition` filename (combine as `-JO`) |
| `-i` | Show response headers + body |
| `-I` | Show headers only (no body) — good for checking status/filename without downloading |
| `-X POST` | Use POST method |
| `-H "Content-Type: application/json"` | Required for all POST endpoints here |
| `-d '{...}'` | JSON request body |

**Verify a downloaded zip isn't corrupt/empty:**
```bash
unzip -l events_logs.zip
```