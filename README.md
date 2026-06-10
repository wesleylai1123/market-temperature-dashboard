# 市場溫度儀表板 · Market Temperature Dashboard

一個**規則化、可解釋**的多因子分數卡，回答「現在該偏積極還是防禦」——而不是預測點位。台股、美股各算一套，再彙總成總燈號。

> ⚠️ 本工具僅供研究與紀律參考，**不是投資建議**，最終判斷請自己拍板。

---

## 目錄

- [它長怎樣](#它長怎樣)
- [數據字典（每一項數字的意義）](#數據字典每一項數字的意義)
- [分數怎麼算（計分邏輯）](#分數怎麼算計分邏輯)
- [本機執行](#本機執行)
- [GitHub Actions 自動更新（操作設定）](#github-actions-自動更新操作設定)
- [兩個工程上的坑](#兩個工程上的坑決定分數卡可不可信)
- [路線圖 / 檔案結構](#路線圖)

---

## 它長怎樣

- **總結燈號** + **台美雙燈** + 每個因子的**實際數值（大字呈現）** + 積極度 bar
- 每個因子都附**資料說明**與**資料來源**；抓不到的會標「⚠ 沿用舊值」
- 展開「▸ 試算這個數字」可手動改值做 what-if；拖曳**權重滑桿**與**台美配置滑桿**看彙總即時變化
- 讀 `data.json`；任何欄位抓不到就沿用上次值，**永不開天窗**

燈號門檻（積極度 0–100，數字越高越偏積極/逆向加碼）：

| 積極度 | 燈號 | 解讀 |
| --- | --- | --- |
| ≥ 60 | 🟢 偏積極 | 估值/訊號偏便宜，可依計畫加重配置（仍分批） |
| 40–60 | 🟡 中性 | 維持目標權重，按紀律再平衡 |
| < 40 | 🔴 偏防禦 | 風險偏貴，控制曝險、保留銀彈 |

---

## 數據字典（每一項數字的意義）

> 「方向」採**逆向(contrarian)**思維：貴/貪婪/過熱 → 防禦；便宜/恐懼/谷底 → 積極。
> 積極度優先用 `pctile_ref`（歷史百分位排名）計算；樣本不足 12 筆時退回 `cal_min/cal_max` 校準區間線性映射（見[計分邏輯](#分數怎麼算計分邏輯)）。

### 美股

| 因子 | 數字是什麼 | 單位 | 校準區間 | 方向 | 來源 | 狀態 |
| --- | --- | --- | --- | --- | --- | --- |
| **估值 · Shiller CAPE** | 席勒本益比（10年通膨調整 EPS），衡量大盤貴不貴，與長期預期報酬負相關 | x | 10–40 | 越貴→防禦 | multpl.com | ✅ |
| **情緒 · CNN 恐懼貪婪** | 0–100，0=極度恐懼、100=極度貪婪 | — | 0–100 | 貪婪→防禦 / 恐懼→積極 | CNN（非官方端點） | ✅ |
| **景氣 · 殖利率曲線 10Y–2Y** | 10年減2年公債殖利率；負值(倒掛)是經典衰退領先訊號 | % | -1.0–2.5 | 倒掛→防禦 / 正斜率→積極 | 美國財政部（免金鑰） | ✅ |
| **利率 · 10年期殖利率** | 美10年期公債殖利率，無風險利率錨，越高壓抑高估值股 | % | 1.0–5.5 | 越高→防禦 | 美國財政部（免金鑰） | ✅ |

### 台股

| 因子 | 數字是什麼 | 單位 | 校準區間 | 方向 | 來源 | 狀態 |
| --- | --- | --- | --- | --- | --- | --- |
| **估值 · 台積電本益比(大盤代理)** | 台積電(2330)本益比代理大盤估值（佔指數約三成的權值龍頭，非市值加權整體 P/E） | x | 12–32 | 越貴→防禦 | FinMind `TaiwanStockPER` data_id=2330 | ✅ |
| **情緒 · 融資餘額** | 整體市場融資餘額金額(億元)，代理散戶槓桿情緒 | 億 | 2000–8000 | 越高(過熱)→防禦 | FinMind `TaiwanStockTotalMarginPurchaseShortSale` | ✅ |
| **景氣 · 國發會景氣對策信號** | 綜合分數 9–45，藍燈(低)逆向加碼、紅燈(高)轉防禦；台灣特有 | 分 | 9–45 | 紅燈→防禦 / 藍燈→加碼 | FinMind `TaiwanBusinessIndicator`（monitoring） | ✅ |
| **資金 · 外資買賣超** | 外資當日買賣超金額，買超偏積極、賣超轉防禦（單日 proxy） | 億 | -300–300 | 買超→積極 | TWSE | ✅ |

> 美股四項已接好（殖利率改用美國財政部 .gov 免金鑰最穩，原 FRED 從 GitHub runner 會 timeout；CAPE、恐懼貪婪是解析非官方來源，較脆弱但已包重試）。
> 台股四項皆已接好：外資(TWSE)、估值(FinMind 2330 PER 代理)、情緒(FinMind 整體融資)、景氣(FinMind `TaiwanBusinessIndicator`)。
> [FinMind](https://finmindtrade.com/) 無 token 也能抓（限流較低）；設 `FINMIND_TOKEN` secret 可提高限額。

---

## 分數怎麼算（計分邏輯）

1. **單因子 → 積極度 0–100**（`index.html` 的 `scoreOf()`，與 `backtest/scores.py` 的
   `score_of()` 同一套邏輯）
   ```
   pct = 目前值在 pctile_ref（至今所有歷史月度數值，由小到大排序）中的百分位排名 0–1
   積極度 = (invert ? 1 - pct : pct) × 100
   ```
   `invert=true` 代表「值越大越防禦」（估值、情緒、利率）；`invert=false` 代表「值越大越積極」（殖利率曲線、外資買超）。

   `pctile_ref` 由 `backtest/update_percentiles.py` 從 `backtest/data/{us,tw}_factors.csv`
   算出並寫回 `data.json`。樣本數 < 12 筆時退回舊版「校準區間線性映射」當佔位：
   ```
   pct = clamp((value - cal_min) / (cal_max - cal_min), 0, 1)
   ```

2. **單市場分數** = 四因子積極度的**加權平均**（權重來自滑桿，預設等權重）。

3. **整體分數** = 美股分數 × 美股配置 + 台股分數 × 台股配置（配置滑桿，預設 50/50）。

> 回測（`backtest/scores.py`）用「擴張視窗百分位排名」：每個時點只用至今為止的樣本算百分位，
> 無 lookahead；儀表板（`index.html`）則用「至今全部歷史」算今天的百分位，邏輯一致、樣本範圍不同。

---

## 本機執行

```bash
# 用 http server 開（直接 file:// 會被瀏覽器 CORS 擋掉 fetch('data.json')）
python3 -m http.server
# 瀏覽器開 http://localhost:8000

# 手動更新一次資料（會去抓線上來源，更新 data.json）
python3 fetch_data.py
```

`fetch_data.py` 零外部相依（純標準函式庫），內建瀏覽器標頭與重試（指數退避），降低偶發 timeout/被擋。

---

## GitHub Actions 自動更新（操作設定）

排程設定在 `.github/workflows/update-dashboard.yml`：每天 **UTC 22:30 ≈ 台灣 06:30**（美股收盤後）跑 `fetch_data.py`，把 `data.json` 變動 commit 回 repo。

### 一次性設定（已完成）

- **Workflow 寫入權限**：Settings → Actions → General → Workflow permissions → **Read and write**（否則排程跑完無法 push）。
  *本 repo 已透過 API 設定為 write，無需再手動點。*
- **（可選）FinMind token**：Settings → Secrets and variables → Actions → New repository secret，名稱 `FINMIND_TOKEN`。未設則走匿名（限流較低，每日跑足夠）。

### 三種觸發方式

| 方式 | 怎麼做 |
| --- | --- |
| **自動排程** | 不用做事，每天台灣早上 06:30 自動跑 |
| **網頁手動** | repo 上方 **Actions** 分頁 → 左側「Update dashboard data」→ 右邊 **Run workflow** |
| **指令手動** | `gh workflow run update-dashboard.yml -R <owner>/<repo>` |

查狀態：`gh run list -R <owner>/<repo>`；看單次日誌：`gh run view <run-id> --log`。

> 非交易日（週末/假日）TWSE 無外資資料，會自動沿用舊值並標 stale，屬正常。

---

## 兩個工程上的坑（決定分數卡可不可信）

1. **標準化（最關鍵）**：原始數值沒有意義（CAPE=30 高不高要對照自身歷史）。已改成「對自身歷史取百分位排名」（`pctile_ref`），樣本不足時退回線性映射。
2. **權重別過度調參**：彙總權重最容易自欺。v1 用等權重，別拿近期行情去 tune（那就是 overfitting）。

回測注意：景氣燈號、PMI 這類總經數據有發布落差且會事後修正，回測要用「當時可得」的值，避免 lookahead bias。每個因子的 `scoreOf()` 輸出就是未來回測的一個 feature，資料結構已對齊 vectorbt。

---

## 回測（驗證燈號有沒有用）

`backtest/` 用 **backtrader** 把這套合成分數當「股票目標權重」做月度再平衡，對比買進持有，
看歷史上能不能用較小回撤換取相近報酬。與儀表板共用 `data.json` 的因子設定，兩邊一致。

```bash
cd backtest && pip install -r requirements.txt
python fetch_history.py --start 2010-01-01          # 抓因子+價格歷史(需網路)
python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv
python run_backtest.py --market TW --price data/tw_price.csv --factors data/tw_factors.csv
```

細節（策略、防 lookahead、資料源驗證狀態）見 [backtest/README.md](backtest/README.md)。

### 回測欄位 / 指標說明

儀表板下方「📈 回測策略比較 / 📊 Portfolio 回測 / 🌱 成長股選股 Agent」每個欄位、表頭、
策略名稱滑鼠移上去都有說明文字（亦彙整於區塊上方「📖 名詞解釋」收合區）。對照表：

| 欄位 / 名詞 | 意義 |
| --- | --- |
| **CAGR** | 年化報酬率：把整段回測期間的總報酬，換算成「等效的每年成長率」，方便不同長度的回測互相比較。 |
| **Sharpe** | 夏普比率：月報酬平均值 ÷ 月報酬標準差 × √12（年化）。每承擔一單位波動換到多少報酬，越高越好。 |
| **MaxDD** | 最大回撤：從歷史最高點到之後最低點的最大跌幅(%)，越接近 0 代表最壞情況虧損越小。 |
| **期末資產** | 以期初 100 為基準，回測結束時的資產淨值。 |
| **回測年數** | 依資料起訖日期計算的總年數。 |
| **買進持有** | 對照組：一開始全部資金買進並持有到底，不做再平衡，用來檢驗主動調整權重是否真的更好。 |
| **策略** | 把市場溫度合成分數(0–100)映射成股票目標權重(0–100%)的規則，每月依此再平衡（見 `backtest/strategies.py`）。 |
| **Universe** | Portfolio 回測涵蓋的資產池（如美股板塊/因子 ETF、台股 ETF），決定買進持有基準與輪動範圍。 |
| **Top-N** | 每月從候選池依因子排名挑出最被看好的前 N 檔，等權重持有、每月換股。 |
| **候選池** | 成長股選股 Agent 每月排名的股票清單，Top-N 從這裡面選出。 |
| **換手成本** | 每次買進/賣出的單邊交易成本估計(%)，用來扣抵頻繁換股的損耗。 |
| **lag_months** | 因子或排名數據延後幾個月才視為「當時可得」，避免回測用到未來資訊（lookahead bias）。 |
| **起始資金** | 回測開始時的虛擬本金，用來換算權益曲線。 |
| **市場溫度合成分數** | 估值/情緒/景氣/利率四個因子的積極度依權重加權平均，數字越高代表訊號越偏「積極/逆向加碼」。 |
| **積極度（百分位）** | 目前數值在「至今所有歷史月度數值」中的百分位排名，再依方向(invert)轉換成 0–100。 |

> 之後新增任何回測欄位/指標，請同步在 `index.html` 的 `KPI_INFO`/`KPI_LABELS` 與本表補上說明。

## 路線圖

- [x] 用 FinMind 接台股估值(2330 PER 代理)、情緒(融資餘額)
- [x] backtrader 回測驗證燈號（台美雙市場，含 lookahead 防護）
- [x] 接台股景氣對策信號（FinMind `TaiwanBusinessIndicator`）
- [x] `scoreOf()` 換成歷史百分位（取代線性映射）
- [ ] 燈號翻轉/跨閾值時推 Telegram 通知

## 檔案結構

```
.
├── index.html                 # 互動 widget（讀 data.json，大字呈現數值 + 說明 + 來源）
├── fetch_data.py              # 抓各因子原始值 → data.json（零相依，含重試/瀏覽器標頭）
├── data.json                  # 種子數據 + 每因子的 desc/cal/invert/source，Actions 持續更新 value
├── requirements.txt
├── backtest/                  # backtrader 回測（驗證燈號歷史有效性）
│   ├── fetch_history.py       #   抓因子+價格歷史 → data/*.csv
│   ├── scores.py             #   因子歷史 → 月頻目標權重（共用 data.json，歷史百分位排名）
│   ├── update_percentiles.py #   data/*_factors.csv → data.json 的 pctile_ref
│   ├── run_backtest.py       #   月度再平衡 vs 買進持有，印 KPI
│   └── README.md
└── .github/workflows/
    └── update-dashboard.yml   # 排程抓取並 commit 回 repo
```
