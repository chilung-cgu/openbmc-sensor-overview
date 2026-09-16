# OpenBMC Sensor Overview

在 Linux 工作站持續查看 Yosemite4 的感測器狀態。透過 SSH 唯讀執行 `mfg-tool sensor-display`，將 JSON 顯示成可收合的分組樹，包含狀態計數、異常清單及最近變化。工具不需要安裝在 BMC，不改 firmware、threshold 或硬體設定。

第一版顯示的是 **mfg-tool 回報的狀態**，不是「硬體已驗證 ready」的保證。分組來自名稱規則，不是真實接線 topology。

## 先看示範（不連 BMC）

工作站需要 Python 3.10 以上及 curses，連線模式另需 OpenSSH。不需 pip 安裝第三方套件。

```bash
cd sensor-overview
python3 --version
python3 sensor-overview.py --demo
```

畫面會循環展示正常、critical、unavailable、消失與恢復，來源清楚標示 DEMO。按 `q` 離開。

若已有 uv，也可讓它選擇符合版本的 Python：

```bash
uv run sensor-overview.py --demo
```

無互動終端時可先看文字輸出：

```bash
python3 sensor-overview.py --demo --once
```

## Yosemite4 首次使用

在工作站執行以下步驟。`192.0.2.1` 是文件範例，請替換成你的 BMC IP，亦可使用既有 SSH config alias。

1. 先以 SSH 確認對方 host key，並確認可使用既有 SSH key／agent 登入。工具使用 BatchMode，無法在畫面中互動輸入密碼。只有密碼登入的環境，請先透過既有管理流程準備 key 登入，不要把密碼寫入指令或工具。

   ```bash
   ssh root@192.0.2.1 'mfg-tool sensor-display'
   ssh -o BatchMode=yes root@192.0.2.1 'mfg-tool sensor-display'
   ```

2. 執行一次查詢，確認資料與分組。

   ```bash
   python3 sensor-overview.py --host root@192.0.2.1 --once
   ```

3. 啟動持續更新介面。

   ```bash
   python3 sensor-overview.py --host root@192.0.2.1 --interval 2
   ```

指定非預設 SSH port 或 key：

```bash
python3 sensor-overview.py --host root@192.0.2.1 --port 2222 --identity ~/.ssh/id_ed25519
```

工具不會關閉 SSH host key 驗證。連線失敗時保留最後一次資料並標示未知，錯誤原因會顯示；不會把舊綠燈當成目前正常。

## 畫面與操作

| 顯示 | 意義 |
|---|---|
| 綠色 `O` / normal | 本次來源為 `ok` 且數值有限 |
| 紅色 `!` / attention | `warning`、`critical`、`high`、`low` |
| `?` / unknown | `unavailable`、`dbus error`、未知狀態、缺少／無效讀值 |
| `missing` | 曾出現在成功快照中，後續成功快照未包含它 |
| `stale` | 整次收集失敗，或最後成功收集距今超過 stale 門檻 |

`O`、`!`、`?` 使用 ASCII，無彩色能力時仍能辨認。父分組彙總所有子節點，收合與搜尋不會隱藏全域計數；數值不是主畫面重點。

| 按鍵 | 操作 |
|---|---|
| 上／下、`j`／`k` | 移動與捲動 |
| Enter、左／右 | 收合或展開群組 |
| `a` | 切換只看非正常項目 |
| `/`，輸入名稱，Enter | 名稱搜尋；Esc 清除／離開搜尋 |
| `q` | 離開並停止本機收集器 |

近期事件保留最多 100 筆於記憶體，包含狀態變化及恢復。畫面顯示最近幾筆；退出後不保存。第一次完整快照不會產生數百筆新增事件。

分組優先辨識名稱內明示的 `SENTINEL_DOME_SLOT_N`、`WAILUA_FALLS_SLOT_N`，及 FANBOARD、MEDUSA、MGNT、SPIDER。`CALIBRATED_` 前綴不改 sensor identity。未知名稱留在 Unclassified；不從 `_38_40` 等尾碼猜 slot。若平台命名不同，仍會顯示感測器，但分組可能不完整。

## 兩秒更新的精確含義

預設 `--interval 2` 是查詢開始時間的目標間隔。只允許一輪本機查詢，結束後至少休息 0.2 秒；若一輪耗時 4 秒，下輪不會在第 2 秒重疊啟動。TUI 持續處理按鍵，並顯示實際查詢耗時與上次成功資料年齡。

預設單次 `--timeout 15` 秒；預設 `--stale-after 10` 秒未更新就標示 stale。這兩個門檻獨立：正在查詢也可能先顯示 stale。若現場正常查詢較慢，可根據實測調整：

```bash
python3 sensor-overview.py --host root@192.0.2.1 --interval 5 --timeout 30 --stale-after 20
```

資料年齡是「取得 mfg-tool 快照」距今多久，**不是硬體最後採樣時間**。相同數值不代表 sensor 停止工作；取得新 JSON 也不證明底層 cache 已更新。輪詢之間發生又恢復的短暫事件可能漏掉，不能取代 SEL 或 waveform capture。

逾時或退出會清理本機 SSH process group；SSH 斷線後遠端 mfg-tool 是否立即退出，取決於遠端環境，工具不保證。若持續 timeout，先退出總覽並單次排查 BMC，再調整週期。

## 其他來源與錯誤排查

將原始 JSON 存成檔案後，可持續重讀或單次顯示；不要餵入 table-sensor-display 的表格文字。

```bash
ssh root@192.0.2.1 'mfg-tool sensor-display' > snapshot.json
python3 sensor-overview.py --file snapshot.json
python3 sensor-overview.py --file snapshot.json --once
```

`--local` 直接執行本機 mfg-tool，僅適用於**已確認有 Python 3.10+、curses 及 mfg-tool** 的環境；不預設 Yosemite4 image 具備它們。

```bash
python3 sensor-overview.py --local
```

| 狀況 | 檢查方式 |
|---|---|
| Permission denied／host key 錯誤 | 先以相同 user、port、key 執行 `ssh -o BatchMode=yes ...`；不要關閉驗證 |
| mfg-tool not found／非 JSON | 在 BMC shell 單次執行原始指令，stdout 應是 JSON，stderr 可另存排查 |
| 沒有 sensor／空 JSON | 工具視為無有效快照；不會呈現零顆 sensor 的假正常 |
| Terminal 太小／TERM 不正確 | 放大至至少 60 欄 × 15 列，使用實際互動終端；pipeline 請用 `--once` |
| No module named curses | 使用含 curses 的 Linux Python；`--once` 不需要 curses |

單次模式退出碼：成功取得快照為 0（即使有 critical）、收集／JSON 失敗為 1、命令列參數錯誤為 2。**退出碼 0 不表示全體 sensor 正常。**

## 開發與驗收

```bash
uv venv --python 3.12 .venv
.venv/bin/python -m unittest discover -s tests -v
```

本機測試使用 synthetic 資料、模擬 SSH executable 及 pseudo-terminal，不代替 Yosemite4 現場驗證。首次現場驗收請確認：

- `--once` 的 sensor 名稱及 raw status 與同時段 mfg-tool 相符（動態資料不要求逐筆數值相等）。
- TUI 能操作收合、搜尋，且連續查詢期間按鍵有回應。
- 觀察查詢耗時與資料年齡；確認兩秒目標是否在該台 BMC 可達成。
- 以 `--demo` 驗證 critical、missing、恢復顯示，不必為測 UI 刻意製造硬體異常。

初版限制：沒有 expected sensor inventory，因此從未出現的 sensor 無法判 missing；JSON basename 上游已碰撞的 sensor 無法在此恢復；不另外讀取 Available/Functional 或 threshold alarm bits，完全沿用 mfg-tool 狀態加上資料有效性判定。
