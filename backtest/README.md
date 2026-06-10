# 回測：市場溫度分數訊號是否有效？

用 **backtrader** 驗證儀表板那套燈號：把合成分數當作「股票目標權重」做月度再平衡，
對比 100% 買進持有，看歷史上能不能**用較小的回撤換取相近報酬**（風險調整後更好）。

> 與儀表板共用同一套因子設定（`../data.json` 的 `invert/weights`，標準化用歷史百分位排名），
> 所以回測驗證的就是你在儀表板上看到的邏輯，兩邊不會不一致。

## 策略（v1）

- 每月初，股票目標權重 = `合成分數 / 100`（0–100% 在股票、其餘現金），`order_target_percent` 再平衡。
- **防 lookahead**：因子先 resample 到月底（月內最後已知值），合成分數再整體往後 shift 1 個月才交易（「這個月用上月底已知分數」）。
- **缺資料容錯**：某月若缺某因子（如美股早期沒有恐懼貪婪歷史），自動對當月有值的因子重新分配權重，不讓整月作廢。
- 對照組：100% 買進持有同一標的（解析計算）。
- KPI：CAGR、Sharpe、MaxDD、期末資產。

## 怎麼跑

```bash
cd backtest
python3 -m venv .venv && source .venv/bin/activate      # 或用 repo 既有 .venv
pip install -r requirements.txt

# 1) 抓歷史資料（需網路；台股建議設 FinMind token 提高限額）
FINMIND_TOKEN=你的token python fetch_history.py --start 2010-01-01

# 2) 跑回測
python run_backtest.py --market US --price data/us_price.csv --factors data/us_factors.csv
python run_backtest.py --market TW --price data/tw_price.csv --factors data/tw_factors.csv
```

## 檔案

| 檔案 | 作用 |
| --- | --- |
| `fetch_history.py` | 抓因子+價格歷史 → `data/*.csv`（需網路，每源獨立 try） |
| `scores.py` | 因子歷史 → 月頻目標權重（共用 `../data.json` 設定，歷史百分位排名 + lookahead 防護） |
| `update_percentiles.py` | `data/*_factors.csv` → 寫回 `../data.json` 的 `pctile_ref`（供儀表板 `scoreOf()` 用） |
| `run_backtest.py` | backtrader 月度再平衡 vs 買進持有，印 KPI |

## 資料來源與驗證狀態

（已於 GitHub Actions runner 實跑全部驗證）

| 市場 | 因子/價格 | 來源 | 狀態 |
| --- | --- | --- | --- |
| 美股 | 估值 CAPE | — | ❌ 未解：multpl 全頁 JS、nasdaq 需金鑰。待改抓 Shiller `ie_data.xls` |
| 美股 | 情緒 恐懼貪婪 | CNN historical（帶 Referer） | ✅ 可抓（僅約近 1 年，早期自動重分配權重） |
| 美股 | 景氣/利率 2Y·10Y | 美國財政部逐年 CSV | ✅ 可抓（4110 筆 / 自 ~1990） |
| 美股 | 價格 SPY | Yahoo chart API（stooq 備援） | ✅ 可抓 |
| 台股 | 估值 2330 PER | FinMind `TaiwanStockPER` | ✅ 可抓 |
| 台股 | 情緒 融資餘額 | FinMind 整體融資 | ✅ 可抓 |
| 台股 | 景氣對策信號 | FinMind `TaiwanBusinessIndicator` | ✅ 可抓 |
| 台股 | 外資買賣超 | FinMind `TaiwanStockTotalInstitutionalInvestors` | ✅ 可抓 |
| 台股 | 價格 0050 | FinMind `TaiwanStockPrice` | ✅ 可抓 |

> 整條管線已在 GitHub Actions（`.github/workflows/backtest.yml`，手動觸發）端到端跑通並
> 印出 KPI；運算部分另用合成資料離線驗證過。
>
> **唯一缺口：美股 CAPE**。multpl 表格是 JS 動態載入(靜態抓到的是 CSS)，nasdaq/quandl 需
> 金鑰。所以目前美股回測缺最重要的估值因子，結論偏「利率/景氣擇時」，請保留解讀空間。
> 補法：抓 Robert Shiller 官方 `ie_data.xls`（月頻 CAPE，自 1871；需 `pip install xlrd`、
> 解析分數年份欄）。

## 已知限制 / 下一步

- `score_of` 用「擴張窗百分位排名」（與儀表板一致、無 lookahead），早期樣本數少時百分位
  較不穩定，但會隨歷史資料增加自動收斂。
- 可加交易成本/滑價、改變再平衡頻率、或把分數做成連續 vs 三檔燈號比較。

## 成長股選股 Agent（`growth_agent.py`）

規則式（非 LLM）量化選股 agent，每月依因子排名候選池，選出 Top-N：

- **TW**：月營收年增率（FinMind `TaiwanStockMonthRevenue`），`shift(lag_months)` 防公布落差。
- **US**：12-1 月價格動能（排除最近 1 個月，Jegadeesh-Titman 動能因子）。

`run_growth_backtest.py` 驗證選股是否真的有 alpha：每月用「上一期」因子排名決定本期持股
（雙重防 lookahead），比較三條曲線：

1. Top-N 選股（等權重、每月換股）
2. 候選池等權重買進持有（不選股，看選股本身的價值）
3. 大盤 benchmark（TW: 0050 / US: SPY）

```bash
python run_growth_backtest.py --market TW --start 2025-12-01 --top-n 5
python run_growth_backtest.py --market US --start 2025-12-01 --top-n 5 --output-json results/us_growth.json
```

`--skip-fetch` 可改用本地快取（`data/price_*.csv`、`data/revenue_*.csv`），搭配
`make_demo_data.py` 產生的合成資料做離線測試。`merge_results.py` 加 `--growth-us`/
`--growth-tw` 可把結果併入 `backtest_results.json`，儀表板會顯示「最新選股」與
Top-N vs 等權重 vs 大盤的權益曲線/KPI。

## 儀表板的回測互動（index.html）

`backtest_results.json` 存在時，儀表板會在主頁下方顯示一個可互動的回測區塊：

- **市場切換 Tab**：美股／台股分頁，三個子區塊（單資產回測、Portfolio、成長股 Agent）
  跟著切換，避免雙市場結果全部疊在同一頁。
- **策略/曲線開關**：單資產回測圖表上方有每條曲線（買進持有＋各策略）的勾選框，
  可單獨顯示/隱藏，方便比較。
- **Portfolio (Universe × Strategy)**：若 `backtest_results.json` 含 `portfolio` 欄位
  （由 `merge_results.py --portfolio-us/--portfolio-tw` 併入），會顯示 Universe 與
  策略的下拉選單，即時切換對應的權益曲線與 KPI。
- **成長股 Top-N 即時調整**：拖動滑桿調整 Top-N（1 ~ 候選池大小），前端會用
  `monthly_detail[].ranking`（每月完整排名 + 當月報酬）即時重算 Top-N 權益曲線與
  KPI（CAGR/Sharpe/MaxDD），不必重跑 Python。換手成本沿用 `tx_cost_oneway`。

以上皆為純前端計算，`run_growth_backtest.py` 需確保輸出含 `monthly_detail[].ranking`、
`top_n`、`tx_cost_oneway`、`candidates` 等欄位（已內建）。
