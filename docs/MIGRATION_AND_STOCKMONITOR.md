# 换电脑与 StockMonitor 配置指南

这份文档用于把 GushiTop20 从旧 Windows 电脑迁移到新电脑，并恢复盘中行情、收盘突破、点评和 LLM 配置。

## 1. 数据分别来自哪里

项目使用的本地数据分为两类：

| 数据 | 默认位置 | 生成方式 | 是否提交 GitHub |
|---|---|---|---|
| 盘中全市场快照 | `data\stockmonitor\spot.json` | 本仓库 StockMonitor 采集器 | 否，可重新生成 |
| 日线知识库 | `data\knowledge.db` | 知识库采集脚本或从旧电脑迁移 | 否，文件较大 |
| 设置和 API Key | `data\settings.json` | 网页设置页 | 否，包含私密配置 |
| 盘中点评 | `data\notes.json` | 网页点评区 | 否，属于个人数据 |
| 板块/市值缓存 | `data\sector_cache.json`、`data\market_cap_cache.json` | 旧数据任务或外部缓存 | 否，可选迁移 |

StockMonitor 已经随仓库提供，不需要再寻找或复制旧电脑的 `D:\StockMonitor` 目录：

- 采集器：`scripts\stockmonitor\fetch_spot.py`
- 计划任务安装器：`scripts\stockmonitor\install_task.ps1`
- 行情接口：新浪财经 `hq.sinajs.cn`
- A 股代码表：AkShare `stock_info_a_code_name`
- API Key：不需要
- 输出结构：`updated_at`、`source` 和 `data`；每只股票包含代码、名称、今开、昨收、最新价、最高、最低、成交量、成交额和涨跌幅

采集器写文件时会先生成临时文件，校验数量后再整体替换 `spot.json`，避免网页与采集器同时操作时读到不完整 JSON。

## 2. 旧电脑迁移前准备

先确认没有正在写入 `knowledge.db`，尤其是 15:10 的 `StockKB_Collect` 任务。建议收盘任务完成后再迁移。

需要保留的文件：

1. `D:\Agent_Prooogram\stock-knowledge-base\knowledge.db`，或当前实际使用的 `knowledge.db`。
2. 当前项目里的 `data\settings.json`。
3. 当前项目里的 `data\notes.json`。
4. 可选：`sector_cache.json`、`market_cap_cache.json`、`raw_indices.json`。

不需要迁移 `spot.json`，新电脑安装计划任务后会重新生成。不要把上述私密文件提交到 GitHub；仓库的 `.gitignore` 已经排除它们。

3GB 的 SQLite 数据库不要在写入过程中直接复制。如果无法确认写入任务是否结束，先在任务计划程序中暂时禁用 `StockKB_Collect`，确认其状态不是“正在运行”，再复制数据库。

## 3. 在新电脑安装项目

准备 Git 和 Python 3.11 或更高版本，然后在 PowerShell 中执行：

```powershell
git clone https://github.com/SSSSKRXX/GushiTop20.git
cd GushiTop20
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

将旧电脑的私有数据复制到新项目：

```text
GushiTop20\data\knowledge.db
GushiTop20\data\settings.json
GushiTop20\data\notes.json
```

如需保留板块和市值缓存，也复制到 `GushiTop20\data\`。这些文件不会被 Git 跟踪。

## 4. 安装 StockMonitor

从项目根目录执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stockmonitor\install_task.ps1
```

安装器会：

1. 创建 Windows 计划任务 `StockMonitor_Fetch`。
2. 在周一至周五 09:30-11:30、13:00-15:00 每 15 分钟采集一次。
3. 立即执行一次采集进行验证。
4. 默认写入 `data\stockmonitor\spot.json`。
5. 将日志写到 `data\stockmonitor\stockmonitor.log`。

任务采用当前 Windows 用户运行，因此需要保持该用户登录。重复执行安装命令会更新同名任务，不会创建多份。

手动测试采集：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stockmonitor\run_fetch.ps1
```

检查结果：

```powershell
$spot = Get-Content .\data\stockmonitor\spot.json -Raw -Encoding UTF8 | ConvertFrom-Json
$spot.updated_at
$spot.source
$spot.data.Count
Get-ScheduledTaskInfo -TaskName StockMonitor_Fetch
```

正常情况下股票数量应在数千只。计划任务第一次按时运行后，`LastTaskResult` 应为 `0`；刚安装、尚未到触发时间时可能显示“尚未运行”。采集失败不会覆盖上一份有效快照。

## 5. 把本地数据配置给网页

标准安装不需要配置路径。网页会按以下优先级寻找盘中快照：

1. `.env` 中的 `STOCKMONITOR_SPOT_FILE`。
2. 项目内 `data\stockmonitor\spot.json`。
3. 兼容旧电脑路径 `D:\StockMonitor\data\spot.json`。

如果数据放在其他磁盘，复制配置模板：

```powershell
Copy-Item .env.example .env
notepad .env
```

按实际路径修改：

```dotenv
STOCKMONITOR_SPOT_FILE=D:\MarketData\spot.json
KNOWLEDGE_DB_FILE=D:\MarketData\knowledge.db
SECTOR_CACHE_FILE=D:\MarketData\sector_cache.json
MARKET_CAP_CACHE_FILE=D:\MarketData\market_cap_cache.json
RAW_INDICES_FILE=D:\MarketData\raw_indices.json
STOCKMONITOR_READ_DELAY_SECONDS=60
```

`web_app.py` 启动时会自动读取项目根目录的 `.env`。`.env` 被 Git 忽略，不能在里面提交真实 API Key。

网页默认比 StockMonitor 的计划时间晚 60 秒读取，例如 10:45 的采集通常在 10:45 几秒完成，网页约在 10:46 读取。可以通过 `STOCKMONITOR_READ_DELAY_SECONDS` 调整，但不建议小于 30 秒。

## 6. 启动并验证网页

```powershell
.\run_web.bat
```

浏览器访问 `http://127.0.0.1:8765`。页面更新时间下方应显示行情快照时间；点击板块切换、刷新网页或进入设置页只读缓存，不会重新抓取全盘行情。

如果其他电脑只需要查看，不要在每台电脑都安装采集器和后台。只在一台数据主机运行 StockMonitor 和网页服务，其他电脑通过该主机的局域网 IP 或 Tailscale IP 访问 `http://主机IP:8765`，这样所有人看到同一份数据和点评，也不会重复访问行情接口。

## 7. 收盘突破数据库的后续更新

从 StockMonitor 收盘快照写入当日日线：

```powershell
.\.venv\Scripts\python.exe scripts\knowledge_base\collect_knowledge_db.py --market-cap --from-spot .\data\stockmonitor\spot.json
```

建议在交易日 15:10 后执行。首次没有历史库时，按 `scripts\knowledge_base\README.md` 初始化和回填；如果已经从旧电脑迁移 `knowledge.db`，只需继续每日增量更新。

## 8. 常见故障

- 页面提示“暂无缓存”：先手动运行 `run_fetch.ps1`，确认 `spot.json` 存在且股票数量正常，然后重启网页后台。
- 计划任务结果不是 `0`：查看 `data\stockmonitor\stockmonitor.log`。
- 新电脑路径不同：不要修改源码，使用 `.env` 覆盖路径。
- API Key 丢失：从旧电脑迁移 `data\settings.json`，或在新网页设置页重新填写。
- 数据库正在被写入：停止 `StockKB_Collect` 后再复制；不要把 SQLite 数据库放在网络共享目录中实时读写。
- 多台电脑数据不一致：只保留一台后台服务器，其他电脑访问该服务器网页。
