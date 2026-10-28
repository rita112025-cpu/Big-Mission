# Big-Mission｜Windows 自動化任務中控台

使用 FastAPI + React / TypeScript + SQLite 的**單機本機程式執行器**。**請勿開放至公網**：任何能存取本機 API 的程式均可建立主機執行任務。預設只監聽 127.0.0.1。不是沙箱，也沒有使用者登入或細粒度權限控制。

## 安裝與啟動（Windows 11）

1. 安裝 Python 3.12、Node.js 22 LTS、Git。請確保 `py -3.12` 和 `npm` 可使用。
2. `git clone https://github.com/rita112025-cpu/Big-Mission.git`，進入 `Big-Mission`。
3. 在命令提示字元執行 `install_windows.bat`。
4. 執行 `start_windows.bat`，開啟 http://127.0.0.1:8000 。
5. 請勿同時啟動第二份服務，SQLite 及排程只設計單一 worker。

注意：Windows Smart App Control 可能阻擋未簽章工具。不要為此關閉保護；使用可信來源的 Python 和 Node 正式安裝版。

## 手動開發

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
cd frontend
npm install
npm run build
cd ..
$env:PYTHONPATH='backend'
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

## Docker Compose

`docker compose up --build`，連線 http://127.0.0.1:8000 。**Linux 容器無法直接啟動 Windows 主機的 BAT、PowerShell 或存取任意主機磁碟**。需要本機腳本時請使用 Windows 原生模式；映射路徑只適用於容器內檔案。容器版 Python 腳本需放在容器內可存取的位置。

## 任務規則

- 類型：Python (`.py`)、PowerShell (`.ps1`)、BAT/CMD、HTTP GET。HTTP 的 `env` 欄位用作請求標頭；HTTP 目前只支援 GET，參數不參與 HTTP 請求。
- 腳本路徑必須存在。參數使用 JSON 字串陣列，例如 `["--dry-run","true"]`；不經 shell 插入。BAT 經 `cmd.exe /d /c`，**其參數仍受 cmd 特殊字元解析影響**，勿執行不可信輸入。
- 排程：`none` 無排程；`once` ISO 8601（無時區視為台北）；`interval` 秒數（至少 10）；`cron` 5 欄 cron。Cron 以 Asia/Taipei 評估。
- **錯過排程**：重啟後已到期的任務最多執行一次；間隔排程跳過其餘錯過的 tick，從當下重新計算；一次性排程執行後停用。重疊或並行上限時跳過，不排入待執行佇列。
- 每個任務預設不可重疊；全域並行數 `MISSION_CONCURRENCY` 預設 2。
- 在 Windows 上以 taskkill /T /F 終止子程序樹；Linux 使用程序群組。因 OS 與程序逃逸特性，不能保證終止所有自行脫離程序群組的後代。
- 程序輸出要求 UTF-8；Python 自動設 `PYTHONIOENCODING=utf-8`。其他執行檔若輸出 CP950 可能有替代字元，請在腳本內設 UTF-8。
- stdout/stderr 逐塊寫入檔案，透過 SSE 即時顯示。日誌上限預設 1 MiB／Run，保留 30 天。
- 任務環境變數寫入**本機 SQLite 明文**，API 不回傳值。但程式自身若輸出機密，仍可能進入日誌；現階段**不提供保證式機密遮罩**，請勿將密碼或 token 放入任務環境變數或輸出。HTTP header 僅本機保管，不回傳前端。
- 編輯任務時前端不會載入原機密；儲存 `{}` 會清除舊值。停用或啟用目前也會清除既有環境變數，操作前應留意。
- 失敗自動重試最多 5 次；一次 Run 包含多次 attempt。

## 範例與驗收

示範：`examples/success.py`、`examples/failure.py`、`examples/slow.py`，全為無破壞性。建立任務時使用**絕對路徑**，例如 `D:\github\Big-Mission\examples\success.py`。

```powershell
$env:PYTHONPATH="backend"
.venv\Scripts\python.exe -m pytest tests -v
```

測試涵蓋成功、失敗退出碼、逾時、重疊拒絕、取消、重啟標示、排程計算。Windows 真機需額外人工驗收 PowerShell、BAT 子程序終止，以及排程跨越休眠。

## 備份、還原、遷移

停止服務後，備份整個 `data/` 資料夾（含 SQLite、WAL／SHM、logs）。停止服務後以備份覆蓋 `data/` 可還原。資料庫由啟動時 `CREATE TABLE IF NOT EXISTS` 建立**初版 schema**，目前沒有 Alembic 或後續 migration 機制；未來修改 schema 前必須先新增版本化遷移並備份，不能直接覆蓋正式庫。

## 限制與已知問題

單機、單 worker、無帳密、無 TLS、無權限隔離；HTTP 僅 GET；環境變數明文；SSE 重連不支援 offset；歷史畫面只顯示最新 500 筆；無排程衝突持久化列隊；網頁無獨立啟用密碼。Windows 真機驗證狀態應以 GitHub Actions 的 Windows job 與實際本機執行紀錄為準。不能把 Linux 測試通過當 Windows 通過。
