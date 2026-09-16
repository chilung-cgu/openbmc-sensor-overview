# OpenBMC Sensor Overview

OpenBMC 感測器動態健康總覽工具。專為管理大量感測器（數百至上千顆）的 OpenBMC 系統設計，以純 Python 3 標準函式庫實作互動式 TUI（Terminal User Interface），支援密集矩陣視圖（Dense Matrix View）與層級樹狀視圖（Tree View）。

本工具在工作站或終端機執行，透過 SSH 唯讀查詢遠端 BMC，完全**獨立於 OpenBMC 建置系統**（無需 BitBake、Meson 或編譯 OpenBMC C++ 原始碼），不修改韌體、門檻值（Thresholds）或硬體暫存器。

---

## 主要特色

- **零外部套件依賴**：100% 使用 Python 3.10+ 標準函式庫（curses、subprocess、json、argparse 等），無需 pip 安裝任何第三方程式庫。
- **雙資料收集後端（Backends）**：
  - **標準 D-Bus 後端 (`--backend dbus`)**：直接透過 `busctl` 查詢上游標準 OpenBMC D-Bus 物件樹（`/xyz/openbmc_project/sensors`），讀取數值、警告／嚴重門檻、Alarm 旗標及 Functional／Available 狀態，適用於標準 OpenBMC 架構。
  - **mfg-tool 後端 (`--backend mfg-tool`)**：相容既有診斷工具 `mfg-tool sensor-display` 輸出的 JSON 資料格式。
- **雙展示模式（View Modes）**：
  - **密集矩陣視圖 (Matrix View)**：高密度網格呈現所有感測器狀態，提供 HUD 頂部狀態列、異常感測器 Tab 快速跳轉、嚴重視重自動鎖定與二維空間遊標導航。
  - **可收合樹狀視圖 (Tree View)**：依槽位與子系統（Slot、Fan、Power、Management 等）自動分類，支援展開／收合、異常快速過濾與名稱即時搜尋。
- **彈性連線機制**：
  - 預設採用 SSH BatchMode 金鑰登入。
  - 支援 `--password` 密碼參數（使用 sshpass）。
  - 若未指定密碼且金鑰認證被拒，自動降級嘗試 OpenBMC 官方標準預設密碼 `0penBmc`。
- **單次快照輸出 (`--once`)**：可在無互動終端或 CI/腳本環境下直接輸出格式化文字報告並正常退出。

---

## 快速開始

### 1. 本機展示模式（無需連線 BMC）

立即體驗矩陣總覽與動畫循環（展示正常、嚴重、失效、消失與恢復）：

```bash
# 預設啟動密集矩陣視圖 (Matrix View)
python3 sensor-overview.py --demo

# 啟動可收合樹狀視圖 (Tree View)
python3 sensor-overview.py --demo --view tree

# 單次文字輸出（非 TUI，適用於無終端環境）
python3 sensor-overview.py --demo --once
```

操作時隨時按 `m` 或 `v` 可在矩陣視圖與樹狀視圖之間切換，按 `q` 退出。

### 2. 連線遠端 OpenBMC

`192.0.2.1` 為文件範例 IP，請替換為目標 BMC 的 IP 或主機名稱。

#### 使用標準 OpenBMC D-Bus 後端（推薦）

```bash
# 持續監控遠端 OpenBMC 感測器
python3 sensor-overview.py --backend dbus --host root@192.0.2.1

# 指定輪詢週期（每 3 秒更新一次，15 秒逾時，10 秒無資料判定 stale）
python3 sensor-overview.py --backend dbus --host root@192.0.2.1 --interval 3 --timeout 15 --stale-after 10

# 單次抓取總覽並退出
python3 sensor-overview.py --backend dbus --host root@192.0.2.1 --once
```

#### 使用既有 mfg-tool 後端

```bash
python3 sensor-overview.py --backend mfg-tool --host root@192.0.2.1
```

#### 指定 SSH Port、Key 或密碼

```bash
# 指定非標準 Port 與私鑰
python3 sensor-overview.py --backend dbus --host root@192.0.2.1 --port 2222 --identity ~/.ssh/id_ed25519

# 明確指定 SSH 密碼（需安裝 sshpass）
python3 sensor-overview.py --backend dbus --host root@192.0.2.1 --password mypassword
```

> **密碼自動降級機制**：在以金鑰認證收到 `Permission denied` 時，若未提供 `--password`，工具會自動以 OpenBMC 預設密碼 `0penBmc` 進行一次回退嘗試。

### 3. 本機 JSON 檔案離線分析

可將 BMC 上抓取的 JSON 儲存為檔案後離線檢視：

```bash
# 離線即時互動總覽
python3 sensor-overview.py --file snapshot.json

# 離線單次文字輸出
python3 sensor-overview.py --file snapshot.json --once
```

---

## 畫面與按鍵操作

### 健康狀態符號對照

| 符號 (ASCII) | 狀態 | 說明 |
|---|---|---|
| 綠色 `O` | normal | 數值正常且在安全門檻內，`Available` 與 `Functional` 為真 |
| 黃色 `!` | warning | 超過警告門檻（High/Low），或觸發 Warning Alarm |
| 紅色 `!` | critical | 超過嚴重門檻（High/Low）、觸發 Critical Alarm 或 `Functional: false` |
| 紫色 `?` | unavailable | `Available: false`、D-Bus 錯誤、無效數值（NaN/Inf）或缺少讀值 |
| 灰色 `?` | missing | 曾出現在歷史成功快照中，但在最新的快照中未回報 |
| 灰暗標示 | stale | 收集逾時或距上次成功資料超過 stale 門檻 |

### 全域快捷鍵

| 按鍵 | 功能說明 |
|---|---|
| `m` 或 `v` | 切換 **矩陣視圖 (Matrix View)** 與 **樹狀視圖 (Tree View)** |
| `q` | 離開工具並安全釋放所有背景收集執行緒與行程 |

### 矩陣視圖快捷鍵 (Matrix View)

| 按鍵 | 功能說明 |
|---|---|
| `h` / `j` / `k` / `l` 或 方向鍵 | 二維空間遊標移動，選取特定感測器 |
| `Tab` | 快速跳轉至下一個異常感測器（Critical ➔ Warning ➔ Unavailable 循環） |
| `Shift + Tab` | 反向跳轉至上一個異常感測器 |
| `a` | 切換 HUD 自動鎖定開關（開啟時自動追蹤當前最嚴重的異常項目） |
| `p` | 切換佈局緊湊模式（區塊模式 `blocks` 與緊湊模式 `compact` 切換） |

### 樹狀視圖快捷鍵 (Tree View)

| 按鍵 | 功能說明 |
|---|---|
| `k` / `j` 或 上／下方向鍵 | 移動選取列 |
| `PageUp` / `PageDown` | 快速翻頁滾動 |
| `Enter` / `空白鍵` / 左／右鍵 | 展開或收合目前所選的分組 |
| `a` | 切換過濾器：僅顯示非正常（異常與未知）項目 |
| `/` | 啟動名稱搜尋（輸入字串後按 Enter 生效，按 Esc 清除與退出） |

---

## 命令列參數一覽

```
usage: sensor-overview.py [-h] (--demo | --host HOST | --file FILE | --local)
                          [--backend {dbus,mfg-tool}] [--view {matrix,tree}]
                          [--interval INTERVAL] [--timeout TIMEOUT]
                          [--stale-after STALE_AFTER] [--port PORT]
                          [--identity IDENTITY] [--password PASSWORD] [--once]
```

| 參數 | 預設值 | 說明 |
|---|---|---|
| `--demo` | - | 啟動本機虛擬展示模式 |
| `--host HOST` | - | 遠端 SSH 目標主機（例如 `root@192.0.2.1`） |
| `--file FILE` | - | 讀取本機感測器快照 JSON 檔案 |
| `--local` | - | 於本機端執行收集指令（需本機環境支援） |
| `--backend` | `mfg-tool` | 資料來源後端：`dbus`（標準 OpenBMC）或 `mfg-tool`（既有工具） |
| `--view` | `matrix` | 啟動初始介面模式：`matrix`（矩陣視圖）或 `tree`（樹狀視圖） |
| `--interval` | `2.0` | 查詢目標週期（秒） |
| `--timeout` | `15.0` | 單次查詢逾時限制（秒） |
| `--stale-after` | `10.0` | 標記資料為過期（stale）的秒數門檻 |
| `--port` | `22` | SSH 連線連接埠 |
| `--identity` | - | SSH 私鑰檔案路徑 |
| `--password` | - | SSH 密碼（可選；若未填且認證被拒，自動以 `0penBmc` 降級重試） |
| `--once` | - | 單次輸出快照至標準輸出後結束（退出碼：0=成功, 1=失敗, 2=參數錯誤） |

---

## 運作機制與安全設計

1. **非同步安全收集**：
   - 收集執行緒在背景以串列非重疊方式執行。若查詢耗時 4 秒，不會在第 2 秒疊加啟動，保護 BMC 不受併發請求壓力。
   - 逾時或中斷時，會強制清理本機產生的 SSH child process group，不殘留孤兒殭屍行程。
2. **認識論狀態分離**：
   - 連線中斷或逾時時，畫面會保留上一輪歷史狀態但清楚標記為 `stale` 與 `Connection: disconnected`，避免將舊資料誤判為當前正常。
   - 只有通過所有檢查的感測器才會顯示為綠色正常。
3. **終端機相容性保護**：
   - TUI 內建防爆邊界控制，終端機小於 60 欄 × 15 列時會暫停渲染並提示放大，避免 curses 拋出座標越界例外。

---

## 單元測試與品質驗證

專案包含完整的單元測試與端對端互動測試：

```bash
# 執行全套測試套件（114 項測試）
python3 -m unittest discover -s tests -v
```
