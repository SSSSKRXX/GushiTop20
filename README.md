# A股盘中动态解读

这个工作区用于每 10-15 分钟跑一次 A 股盘中解读，默认关注 `光纤` 相关板块/概念。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

## 单次运行

```powershell
.\.venv\Scripts\python scripts\a_share_intraday_monitor.py --board-keyword 光纤
```

也可以换成更明确的关键词：

```powershell
.\.venv\Scripts\python scripts\a_share_intraday_monitor.py --board-keyword CPO
.\.venv\Scripts\python scripts\a_share_intraday_monitor.py --board-keyword 通信设备
```

## 网页前端

启动本地网页服务：

```powershell
.\run_web.bat
```

浏览器打开：

```text
http://127.0.0.1:8765
```

刚 clone 下来时不需要先准备数据库或 StockMonitor 快照，网页可以直接打开。首页第一次进入会优先读取缓存；如果没有缓存，点击 `手动刷新全盘` 会使用公开行情源抓取一次全盘数据。接入本地 StockMonitor 后，网页会优先读取本地快照，减少盘中外部 API 调用。

页面会展示：

- 两市成交额前 20 股票
- 前 20 股票的主力净流入字段：东方财富主源，腾讯兜底，仍缺失则显示 `未覆盖`
- 光纤相关高成交成分
- 市场评分、板块评分、个股建议与 10 分制信号
- 动态倒计时刷新：09:15 拉取一次，09:15-09:25 等到 09:25，09:25-09:30 每 60 秒刷新，09:30 后每 15 分钟刷新
- 板块关键词切换会复用当前全盘快照；`手动刷新全盘` 才会强制重新抓取并重置倒计时
- 右上角 `设置` 页面可编辑评分规则、信号阈值和 LLM 配置
- `设置` 页面可启用 LLM 混合个股建议，每天填写不同标准，让 LLM 在规则分基础上有限微调个股建议
- `收盘突破` 页面用于收盘后筛选：今日收盘价突破上市以来此前最高收盘价，且最近 3 个交易日每日成交额均大于 10 亿，总市值在 70-500 亿之间

## 当前评分口径

默认拆成三层：

- 市场评分：两市成交额前 20 的红绿、资金流扩散、核心高成交股强弱。
- 板块评分：目标板块高开、回流、新高、资金流、板块红绿、板块高成交扩散、涨幅中位数。
- 个股建议：两市成交额前 20 与当前板块高成交成分去重合并，按 `市场 35% / 板块 35% / 个股 30%` 生成建议；理由在表格第二行完整展示。

右上角 `设置` 页面可以调整市场/板块评分项、个股权重、个股基础规则、信号阈值和 LLM 混合建议。个股基础规则包括红盘、主力净流入、成交额阈值、高开、属于目标板块等加分项。启用 LLM 后，规则分会先生成基础个股建议，LLM 只在设定的最大调分幅度内微调个股综合分并补充理由、风险和观察点；LLM 调用失败时自动回退到规则建议。

| 项目 | 分值 |
|---|---:|
| 高开 | 1 |
| 开盘后高开低走，10 分钟后再回流 | 1 |
| 板块某个股创新高（动态） | 1 |
| 主力资金流入 | 1 |
| 全市场成交额前 20 的股票红盘数量大于绿盘数量 | 2 |
| 板块红盘数大于绿盘数 | 1 |
| 板块成交额前 5 股票红盘不少于 3 只 | 1 |
| 板块涨幅中位数大于 0 | 1 |
| 板块主力净流入家数大于净流出家数 | 1 |

信号解释：

| 10分制得分 | 信号 |
|---:|---|
| > 6 | 多头信号：择先买再卖，积极持有 |
| 4-6 | 震荡信号：冲高兑现，积极做差价 |
| < 4 | 偏弱信号：先卖再买，亦可延迟买入 |

## LLM 总结

`设置` 页面可以配置 OpenAI 兼容接口：

- `Base URL`: 默认 `https://api.openai.com/v1`
- `模型`: 默认 `gpt-4o-mini`
- `API Key`: 保存在本机 `data/settings.json`，页面不会明文回显

配置后，主页 `盘中点评` 区域的 `LLM总结` 会结合当前盘面数据和手写点评生成复盘内容。

## 本地知识库数据库

`收盘突破` 页面需要 SQLite 日线数据库 `knowledge.db`。数据库体积较大，不适合提交到 GitHub；仓库只打包采集/更新脚本。

默认读取路径优先级：

1. 环境变量 `KNOWLEDGE_DB_FILE`
2. 项目内 `data/knowledge.db`
3. 当前机器兼容旧路径 `D:\Agent_Prooogram\stock-knowledge-base\knowledge.db`

初始化数据库并抓取市值快照：

```powershell
.\.venv\Scripts\python.exe scripts\knowledge_base\collect_knowledge_db.py --init --market-cap
```

首次回填历史日线数据：

```powershell
.\.venv\Scripts\python.exe scripts\knowledge_base\collect_knowledge_db.py --backfill --resume --start 19900101 --delay 0.4
```

收盘后从 StockMonitor 快照写入当日数据：

```powershell
.\.venv\Scripts\python.exe scripts\knowledge_base\collect_knowledge_db.py --market-cap --from-spot D:\StockMonitor\data\spot.json
```

`data/knowledge.db`、`*.db-wal`、`*.db-shm` 已在 `.gitignore` 中忽略，不会被误提交。

## 备份与回滚

三层评分重构前已创建备份：

```text
backups/20260629-134949_scoring_refactor
```

如需回滚，先停止网页服务，再按备份目录里的 `restore_notes.md` 将文件复制回项目根目录。

## 数据来源

脚本通过 AkShare 调用东方财富公开行情/资金流接口：

- `stock_zh_a_spot_em`: A 股实时行情
- `stock_individual_fund_flow_rank`: 个股主力资金流
- `stock_sector_fund_flow_rank`: 行业/概念板块资金流
- `stock_board_industry_name_em` / `stock_board_concept_name_em`: 板块列表
- `stock_board_industry_cons_em` / `stock_board_concept_cons_em`: 板块成分股
- `stock_zh_a_hist_min_em`: 1 分钟行情，用于识别高开低走后回流
- `stock_zh_a_hist`: 日线行情，用于识别近 60 日动态新高

公开接口可能会受网络、交易时段、源站限流影响。若后续接入 Wind、同花顺、券商柜台或你自己的数据库，只需要替换脚本里的数据抓取函数，评分逻辑可以保留。
