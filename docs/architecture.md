# Architecture and operations

- One FastAPI/Uvicorn process, one worker.
- SQLite stores task configuration, run state and timestamps; UTC timestamps in database; Asia/Taipei evaluates Cron and naive once schedules.
- Scheduler polls every second. Start is asynchronous subprocess, logs persist under data/logs, browser consumes SSE.
- Cancellation terminates process group / Windows taskkill tree; eventual run state becomes cancelled.
- After restart, any queued/running run recorded in SQLite is marked interrupted; no automatic retry of interrupted executions.
- The host executes code under the identity that started Uvicorn. Do not expose the API beyond loopback.
- Docker is for Linux-based jobs and HTTP jobs; host Windows scripts must run in native mode.
- SQLite schema is currently at initial version and lacks versioned migrations. Changes require planned migration and backups.
- Backups: stop service, run backup_windows.ps1. Restore: stop service, run restore_windows.ps1.
- Log caps protect local disk per run; retention purges completed runs older than configured days.
- No guaranteed sensitive-data redaction: stdout/stderr and HTTP response bodies may contain secret values. Avoid secrets in task output.
- User input cannot select arbitrary shell expansion for Python or PowerShell, but BAT is executed by cmd.exe and arguments are parsed by cmd.
