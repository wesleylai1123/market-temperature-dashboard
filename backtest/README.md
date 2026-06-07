# 回測：市場溫度分數訊號是否有效？

用 **backtrader** 驗證儀表板那套燈號：把合成分數當作「股票目標權重」做月度再平衡，
對比 100% 買進持有，看歷史上能不能**用較小的回撤換取相近報酬**（風險調整後更好）。

> 與儀表板共用同一套因子設定（`../data.json` 的 `cal_min/cal_max/invert/weights`），
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
| `scores.py` | 因子歷史 → 月頻目標權重（共用 `../data.json` 設定，含 lookahead 防護） |
| `run_backtest.py` | backtrader 月度再平衡 vs 買進持有，印 KPI |

## 資料來源與驗證狀態

| 市場 | 因子/價格 | 來源 | 狀態 |
| --- | --- | --- | --- |
| 美股 | 估值 CAPE | multpl by-month 表 | ✅ 已驗證可抓 |
| 美股 | 情緒 恐懼貪婪 | CNN historical（帶 Referer） | ✅ 端點已驗證（歷史約 2020+，早期自動重分配權重） |
| 美股 | 景氣/利率 2Y·10Y | 美國財政部逐年 CSV | ✅ 已驗證可抓 |
| 美股 | 價格 SPY | stooq `spy.us` | ⚠ 本機首次跑確認 |
| 台股 | 估值 2330 PER | FinMind `TaiwanStockPER` | ✅ 已驗證可抓 |
| 台股 | 情緒 融資餘額 | FinMind 整體融資 | ✅ 已驗證可抓 |
| 台股 | 外資買賣超 | FinMind `TaiwanStockTotalInstitutionalInvestors` | ⚠ 資料集名稱待確認，失敗自動略過 |
| 台股 | 價格 0050 | FinMind `TaiwanStockPrice` | ⚠ 本機首次跑確認 |

> 回測「運算」部分（`scores.py` + `run_backtest.py`）已用合成資料離線測過：分數策略
> 的最大回撤明顯小於買進持有，符合「低分抱現金降曝險」的預期行為。「抓取」部分因
> 沙箱無網路未對線上實測，第一次在你機器上跑會知道哪些端點要微調。

## 已知限制 / 下一步

- 台股「景氣對策信號」尚未接線，故 TW 回測目前用 估值+情緒(+外資) 重分配權重。
- `score_of` 用固定校準區間線性映射（與儀表板一致、無 lookahead）。進階版可改「擴張窗
  百分位」讓標準化更貼合歷史分布（見根 README 坑 1）。
- 可加交易成本/滑價、改變再平衡頻率、或把分數做成連續 vs 三檔燈號比較。
