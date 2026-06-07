# 市場溫度儀表板 · Market Temperature Dashboard

一個**規則化、可解釋**的多因子分數卡，回答「現在該偏積極還是防禦」——而不是預測點位。台股、美股各算一套，再彙總成總燈號。

> ⚠️ 本工具僅供研究與紀律參考，**不是投資建議**，最終判斷請自己拍板。

## 它長怎樣

- **總結燈號** + **台美雙燈** + 每個因子的**積極度 bar**
- 可即時編輯任何因子數值、拖曳**權重滑桿**與**台美配置滑桿**看彙總怎麼變
- 讀 `data.json`；抓不到就用內建範例值並標「⚠ 沿用舊值」，**永不開天窗**

## 四個因子（台美各一套，等權重起步）

| 維度 | 美股 | 台股 | 方向（contrarian） |
| --- | --- | --- | --- |
| 估值 | Shiller CAPE ✅ | 大盤本益比 ⛏ TODO | 越貴 → 越防禦 |
| 情緒/風險 | CNN 恐懼貪婪 ✅ | 波動/融資餘額 ⛏ TODO | 貪婪 → 防禦；恐懼 → 偏積極 |
| 景氣 | 殖利率曲線 10Y–2Y ✅ | 國發會景氣對策信號 ⛏ TODO | 倒掛/紅燈 → 防禦；藍燈 → 逆向加碼 |
| 利率/資金 | 10年期殖利率 ✅ | 外資買賣超 ✅(單日 proxy) | 殖利率高/外資賣超 → 防禦 |

美股四項已接好（FRED 免金鑰最穩；CAPE、恐懼貪婪是解析非官方來源，較脆弱但已包 try）。台股目前只有外資接上，其餘三項為 `NotImplementedError`，抓取時會自動沿用舊值。**補齊台股最快的路是接 [FinMind](https://finmindtrade.com/)（免費 token）**，填上 `fetch_tw_valuation / fetch_tw_sentiment / fetch_tw_cycle` 即可。

## 本機執行

```bash
# 用 http server 開（直接 file:// 會被 CORS 擋掉 fetch('data.json')）
python3 -m http.server
# 瀏覽器開 http://localhost:8000

# 手動更新一次資料
python3 fetch_data.py
```

`fetch_data.py` 零外部相依（純標準函式庫）。

## 自動更新（GitHub Actions）

`.github/workflows/update-dashboard.yml` 每天 UTC 22:30（美股收盤後）跑 `fetch_data.py` 並把 `data.json` commit 回 repo，也可在 Actions 頁手動觸發。

> 第一次啟用前，到 **Settings → Actions → General → Workflow permissions** 設成 **Read and write**，git push 才不會被擋。

## 兩個工程上的坑（決定分數卡可不可信）

1. **標準化（最關鍵）**：原始數值沒有意義（CAPE=30 高不高要對照自身歷史）。目前 `index.html` 的 `scoreOf()` 用「校準區間線性映射」當佔位，正式版只要把**這一個函式**換成「對過去 N 年取百分位 (percentile rank)」，其餘 UI 不用動。
2. **權重別過度調參**：彙總權重最容易自欺。v1 用等權重，別拿近期行情去 tune（那就是 overfitting）。

回測注意：景氣燈號、PMI 這類總經數據有發布落差且會事後修正，回測要用「當時可得」的值，避免 lookahead bias。每個因子的 `scoreOf()` 輸出就是未來回測的一個 feature，資料結構已對齊 vectorbt。

## 路線圖

- [ ] 用 FinMind 補齊台股估值/情緒/景氣三因子
- [ ] `scoreOf()` 換成歷史百分位（取代線性映射）
- [ ] 把訊號接上回測（vectorbt）驗證歷史有效性
- [ ] 燈號翻轉/跨閾值時推 Telegram 通知

## 檔案結構

```
.
├── index.html                 # 互動 widget（讀 data.json，抓不到用範例值）
├── fetch_data.py              # 抓各因子原始值 → data.json（零相依）
├── data.json                  # 種子數據，Actions 持續更新
├── requirements.txt
└── .github/workflows/
    └── update-dashboard.yml   # 排程抓取並 commit 回 repo
```
