const REFRESH_MS = 15 * 60 * 1000;

const state = {
  board: "光纤",
  lastPayload: null,
  notes: [],
  currentNoteId: "",
  activeMarketView: "top20",
  nextRefreshAt: Date.now() + REFRESH_MS,
  timer: null,
  reportLoading: false,
};

const $ = (id) => document.getElementById(id);

const MARKET_VIEWS = {
  top20: { title: "两市成交额前20", countLabel: "只" },
  fund_in_top15: { title: "今日资金流入前15名", countLabel: "只" },
  fund_out_top15: { title: "今日资金流出前15名", countLabel: "只" },
  fund_in_big_turnover_top10: { title: "今日资金流入前10且成交额大于50亿", countLabel: "只" },
  fund_out_top10: { title: "今日资金流出前10名", countLabel: "只" },
  big_turnover_drop5: { title: "今日成交额大于50亿且跌幅大于5%", countLabel: "只" },
};

const CACHE_MODE_LABELS = {
  report_cache: "报告缓存",
  market_snapshot: "行情快照",
  stockmonitor_snapshot: "StockMonitor快照",
  auction_external: "集合竞价抓取",
  empty: "暂无缓存",
};

async function parseJsonResponse(response) {
  const text = await response.text();
  try {
    return JSON.parse(text.replace(/^\uFEFF/, "").trim());
  } catch (error) {
    throw new Error("接口返回格式异常，请刷新后重试");
  }
}

function friendlyClientError(error) {
  const message = String(error?.message || error || "");
  if (message.includes("UTF-8 BOM") || message.includes("JSONDecodeError") || message.includes("Unexpected token")) {
    return "接口返回格式异常，请刷新后重试";
  }
  return message || "请求失败";
}

function formatDisplay(row, key) {
  return row[`${key}_display`] ?? row[key] ?? "-";
}

function changeClass(value) {
  const number = Number(value);
  if (Number.isNaN(number) || number === 0) return "";
  return number > 0 ? "up" : "down";
}

function signedClass(value) {
  const number = Number(value);
  if (Number.isNaN(number) || number === 0) return "neutral-value";
  return number > 0 ? "up" : "down";
}

function fetchTimeText(meta, payload) {
  const spotTime = meta?.spot_updated_at || "-";
  let fundText = "资金未触发";
  if (meta?.fund_flow_fetched_at) fundText = `资金 ${meta.fund_flow_fetched_at}`;
  else if (meta?.fund_flow_attempted_at) fundText = `资金尝试 ${meta.fund_flow_attempted_at}`;
  else if (meta?.fund_flow_mode === "cached") fundText = "资金缓存";
  const cacheMode = CACHE_MODE_LABELS[payload?.cacheMode] || payload?.cacheMode || "-";
  return `行情 ${spotTime} · ${fundText} · ${cacheMode}`;
}

function flowValue(row) {
  const key = Object.keys(row).find((name) => name.includes("净流入") && !name.endsWith("_display"));
  if (!key) return "未覆盖";
  return row[`${key}_display`] ?? "未覆盖";
}

function todayDateValue() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function renderRows(rows, body, columns) {
  body.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("tr");
    empty.innerHTML = `<td colspan="${columns.length}">暂无数据</td>`;
    body.appendChild(empty);
    return;
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = columns
      .map((column) => {
        if (column === "名称") {
          return `<td><strong>${formatDisplay(row, "名称")}</strong></td>`;
        }
        if (column === "代码") {
          return `<td>${formatDisplay(row, "代码")}</td>`;
        }
        if (column === "开盘价") {
          return `<td>${formatDisplay(row, "今开")}</td>`;
        }
        if (column === "主力净流入") {
          return `<td>${flowValue(row)}</td>`;
        }
        const value = formatDisplay(row, column);
        const cls = column === "涨跌幅" ? changeClass(row[column]) : "";
        return `<td class="${cls}">${value}</td>`;
      })
      .join("");
    body.appendChild(tr);
  });
}

function renderLoadingRows() {
  $("top20Body").innerHTML = '<tr><td colspan="7">正在抓取行情数据，首次加载可能需要约 1 分钟...</td></tr>';
  $("focusStockScoreGroups").innerHTML = '<p class="empty-focus">等待重点个股建议生成...</p>';
  $("stockScoreBody").innerHTML = '<tr><td colspan="11">等待评分生成...</td></tr>';
  const loadingScore = `
    <article class="score-item">
      <header>
        <strong>数据加载中</strong>
        <span class="miss">--</span>
      </header>
      <p>正在读取行情、资金流与板块评分，请稍等。</p>
    </article>
  `;
  $("ks11Status").innerHTML = loadingScore;
  $("boardScoreItems").innerHTML = loadingScore;
}

function renderScoreItems(items, targetId) {
  const list = $(targetId);
  list.innerHTML = "";
  items.forEach((item) => {
    const card = document.createElement("article");
    card.className = "score-item";
    const points = Number(item.points || 0);
    const scoreClass = points > 0 ? "up" : points < 0 ? "down" : "neutral-value";
    card.innerHTML = `
      <header>
        <strong>${item.name}</strong>
        <span class="${scoreClass}">${item.points}/${item.max_points}</span>
      </header>
      <p>${item.evidence || "-"}</p>
    `;
    list.appendChild(card);
  });
}

function renderKs11Status(status) {
  const list = $("ks11Status");
  list.innerHTML = "";
  const card = document.createElement("article");
  card.className = "score-item ks11-card";
  if (!status) {
    card.innerHTML = `
      <header>
        <strong>KS11 韩国综合指数</strong>
        <span class="neutral-value">--</span>
      </header>
      <p>等待下一轮全盘刷新读取 KS11 前置条件。</p>
    `;
    list.appendChild(card);
    return;
  }

  const triggered = Boolean(status.triggered);
  const available = status.available !== false;
  const statusClass = triggered ? "down" : available ? "up" : "neutral-value";
  const price = status.price_display || "-";
  const previousClose = status.previous_close_display || "-";
  const change = status.change_pct_display || "-";
  const lowChange = status.day_low_change_pct_display || "-";
  const priceClass = signedClass(status.change_pct);
  const changeClassName = signedClass(status.change_pct);
  const lowChangeClass = signedClass(status.day_low_change_pct);
  const threshold = status.threshold_pct_display || "-";
  const source = status.source || "-";
  const updated = status.updated_at && status.updated_at !== "-" ? ` · 更新时间 ${status.updated_at}` : "";
  card.innerHTML = `
    <header>
      <strong>${status.code || "KS11"} ${status.name || "韩国综合指数"}</strong>
      <span class="${statusClass}">${status.status || "-"}</span>
    </header>
    <p>最新 <span class="${priceClass}">${price}</span>；昨收 <span class="neutral-value">${previousClose}</span>；涨跌幅 <span class="${changeClassName}">${change}</span>；日内低点 <span class="${lowChangeClass}">${lowChange}</span>；触发阈值 <span class="neutral-value">${threshold}</span>；${source}${updated}</p>
    <p>${status.evidence || "-"}</p>
  `;
  list.appendChild(card);
}

function fundMatchClass(value) {
  if (value === "达标") return "pass";
  if (value === "不达标") return "fail";
  if (value === "未覆盖") return "missing";
  return "neutral";
}

function renderFundMatchCell(row) {
  const status = row["资金匹配"] || "-";
  const ratio = row["资金净流入比例_display"] || "-";
  const required = row["修正后要求_display"] || "-";
  const model = row["资金模型"] || "-";
  return `
    <div class="fund-match-cell ${fundMatchClass(status)}">
      <strong>${status}</strong>
      <span>${model} ${ratio}/${required}</span>
    </div>
  `;
}

function renderBcCell(row) {
  const enabled = Boolean(row["ABC联动启用"]);
  const state = row["BC联动状态"] || (enabled ? "-" : "未启用");
  const avg = row["BC平均资金净流入比例_display"] || "-";
  const coefficient = row["联动系数_display"] || "-";
  return `
    <div class="bc-cell ${enabled ? "enabled" : "disabled"}">
      <strong>${state}</strong>
      <span>${enabled ? `均值 ${avg} / 系数 ${coefficient}` : "普通个股"}</span>
    </div>
  `;
}

function renderScoreTraceCell(row) {
  const finalScore = row["综合分_display"] ?? row["综合分"] ?? "-";
  const ruleScore = row["规则分_display"] ?? row["规则分"] ?? "-";
  const llmAdjustment = row["LLM调整_display"] ?? (row["LLM调整"] == null ? "-" : Number(row["LLM调整"]).toFixed(2));
  const llmClass = llmAdjustment === "-" ? "neutral-value" : signedClass(row["LLM调整"]);
  return `
    <div class="score-trace">
      <strong>${finalScore}</strong>
      <span>规则 ${ruleScore}</span>
      <span class="${llmClass}">LLM ${llmAdjustment}</span>
    </div>
  `;
}

function appendStockDetailRow(body, row, colspan) {
  const detail = document.createElement("tr");
  detail.className = "stock-reason-row";
  const reasonText = row["理由"] || "暂无明确理由";
  const riskText = row["风险"] || "暂无额外风险";
  const watchText = row["观察点"] || "观察下一轮价格、成交额和资金流变化";
  const fundText = [
    `比例 ${row["资金净流入比例_display"] || "-"}`,
    `基础 ${row["基础资金要求_display"] || "-"}`,
    `修正 ${row["修正后要求_display"] || "-"}`,
    `达标率 ${row["A达标率_display"] || "-"}`,
    row["资金结论"] || "暂无资金匹配结论",
  ].join("；");
  detail.innerHTML = `
    <td colspan="${colspan}">
      <div class="stock-detail-grid">
        <div class="stock-detail-block">
          <span>理由</span>
          <p>${reasonText}</p>
        </div>
        <div class="stock-detail-block risk">
          <span>风险</span>
          <p>${riskText}</p>
        </div>
        <div class="stock-detail-block watch">
          <span>观察点</span>
          <p>${watchText}</p>
        </div>
        <div class="stock-detail-block fund">
          <span>资金模型</span>
          <p>${fundText}</p>
        </div>
      </div>
    </td>
  `;
  body.appendChild(detail);
}

function renderFocusStockScores(rows) {
  const container = $("focusStockScoreGroups");
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<p class="empty-focus">暂无重点三剑客建议</p>';
    return;
  }

  const grouped = rows.reduce((acc, row) => {
    const group = row["组合"] || "未分组";
    if (!acc.has(group)) acc.set(group, []);
    acc.get(group).push(row);
    return acc;
  }, new Map());
  const groupOrder = ["光纤三剑客", "科技三剑客"];
  const groupNames = [
    ...groupOrder.filter((name) => grouped.has(name)),
    ...Array.from(grouped.keys()).filter((name) => !groupOrder.includes(name)),
  ];

  groupNames.forEach((groupName) => {
    const groupRows = grouped.get(groupName) || [];
    const article = document.createElement("article");
    article.className = "focus-stock-group";
    const names = groupRows.map((row) => row["名称"]).filter(Boolean).join("、");
    article.innerHTML = `
      <div class="focus-group-head">
        <div>
          <h3>${groupName}</h3>
          <p>A=当前行，B/C=同组另外两只；当前覆盖：${names || "-"}</p>
        </div>
        <span class="pill">${groupRows.length} 只</span>
      </div>
      <div class="table-wrap">
        <table class="stock-score-table focus-stock-score-table">
          <thead>
            <tr>
              <th>名称</th>
              <th>代码</th>
              <th>开/现</th>
              <th>涨跌幅</th>
              <th>主力净流入</th>
              <th>环境分</th>
              <th>个股分</th>
              <th>达标率</th>
              <th>资金匹配</th>
              <th>BC联动</th>
              <th>综合分</th>
              <th>建议</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    `;
    const body = article.querySelector("tbody");
    groupRows.forEach((row) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><strong>${row["名称"] ?? "-"}</strong></td>
        <td>${row["代码"] ?? "-"}</td>
        <td>${row["今开_display"] ?? "-"} / ${row["最新价_display"] ?? "-"}</td>
        <td class="${changeClass(row["涨跌幅"])}">${row["涨跌幅_display"] ?? "-"}</td>
        <td>${row["主力净流入_display"] ?? "未覆盖"}</td>
        <td>${row["环境分_display"] ?? row["环境分"] ?? "-"}</td>
        <td>${row["个股分_display"] ?? row["个股分"] ?? row["个股基础_display"] ?? "-"}</td>
        <td>${row["A达标率_display"] ?? "-"}</td>
        <td>${renderFundMatchCell(row)}</td>
        <td>${renderBcCell(row)}</td>
        <td>${renderScoreTraceCell(row)}</td>
        <td><strong>${row["建议"] ?? "-"}</strong></td>
      `;
      body.appendChild(tr);
      appendStockDetailRow(body, row, 12);
    });
    container.appendChild(article);
  });
}

function renderStockScores(rows) {
  const body = $("stockScoreBody");
  body.innerHTML = "";
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="11">暂无个股建议</td></tr>';
    return;
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${row["名称"] ?? "-"}</strong></td>
      <td>${row["代码"] ?? "-"}</td>
      <td>${row["来源"] ?? "-"}</td>
      <td>${row["环境分_display"] ?? row["环境分"] ?? "-"}</td>
      <td>${row["规则分_display"] ?? row["规则分"] ?? "-"}</td>
      <td>${row["个股分_display"] ?? row["个股分"] ?? row["个股基础_display"] ?? "-"}</td>
      <td>${row["主力净流入_display"] ?? "未覆盖"}</td>
      <td>${renderFundMatchCell(row)}</td>
      <td>${row["成交额_display"] ?? "-"}</td>
      <td>${renderScoreTraceCell(row)}</td>
      <td><strong>${row["建议"] ?? "-"}</strong></td>
    `;
    body.appendChild(tr);
    appendStockDetailRow(body, row, 11);
  });
}

function marketRowsForView(report, viewKey) {
  if (viewKey === "top20") return report.top20_records || [];
  return report.market_tables?.[viewKey] || [];
}

function renderMarketTabs() {
  document.querySelectorAll(".market-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.marketView === state.activeMarketView);
  });
}

function renderActiveMarketTable(report) {
  const viewKey = MARKET_VIEWS[state.activeMarketView] ? state.activeMarketView : "top20";
  const rows = marketRowsForView(report, viewKey);
  const view = MARKET_VIEWS[viewKey];
  $("topTableTitle").textContent =
    viewKey === "top20" ? report.meta?.primary_table_title || view.title : view.title;

  if (viewKey === "top20") {
    const red = rows.filter((row) => Number(row["涨跌幅"]) > 0).length;
    const green = rows.filter((row) => Number(row["涨跌幅"]) < 0).length;
    const covered = report.meta?.top20_fund_covered ?? 0;
    const total = report.meta?.top20_fund_total ?? rows.length;
    const coverageText = report.meta?.auction_ready ? "" : ` · 资金覆盖 ${covered}/${total}`;
    $("marketBreadth").textContent = report.meta?.auction_ready
      ? `竞价红 ${red} / 绿 ${green}`
      : `红 ${red} / 绿 ${green}${coverageText}`;
  } else {
    $("marketBreadth").textContent = `${rows.length} ${view.countLabel}`;
  }

  renderRows(rows, $("top20Body"), ["名称", "代码", "开盘价", "最新价", "涨跌幅", "主力净流入", "成交额"]);
  renderMarketTabs();
}

function renderReport(payload) {
  state.lastPayload = payload;
  const report = payload.report;
  const meta = report.meta || {};
  const boardScore = Number(report.board_score?.score_10 ?? report.score_10 ?? 0);
  const focusStockScores = report.focus_stock_scores || [];
  const stockScores = report.stock_scores || [];

  $("boardScoreValue").textContent = boardScore.toFixed(2);
  $("boardScoreValue").classList.remove("up", "down", "neutral-value");
  $("boardScoreValue").classList.add(signedClass(boardScore));
  $("boardScoreBar").style.width = `${Math.max(0, Math.min(boardScore, 10)) * 10}%`;
  const llmError = meta?.llm_scoring_error ? `（${meta.llm_scoring_error}）` : "";
  $("boardSignalText").textContent = `${report.board_score?.signal || report.signal || "-"}${llmError}`;
  $("timestamp").textContent = meta?.report_refreshed_at || report.timestamp || "-";
  $("fetchTimeMeta").textContent = fetchTimeText(meta, payload);
  $("boardMatch").textContent = meta?.board_match || "-";
  $("rawScore").textContent = `得分 ${report.raw_score}/${meta?.score_max ?? 10}`;
  $("focusStockScoreCount").textContent = `${focusStockScores.length} 只`;
  $("stockScoreCount").textContent = `${stockScores.length} 只`;

  renderActiveMarketTable(report);
  renderFocusStockScores(focusStockScores);
  renderStockScores(stockScores);
  renderKs11Status(report.ks11_status);
  renderScoreItems(report.board_score?.items || [], "boardScoreItems");
}

async function loadNote() {
  const noteDate = $("noteDate").value || todayDateValue();
  const status = $("noteStatus");
  status.textContent = "读取中";
  try {
    const url = new URL("/api/notes", window.location.origin);
    url.searchParams.set("date", noteDate);
    const response = await fetch(url);
    const payload = await parseJsonResponse(response);
    if (!response.ok || !payload.ok) throw new Error(payload.error || "点评读取失败");
    state.notes = payload.entries || [];
    renderNoteOptions();
    const first = state.notes[0] || null;
    selectNote(first?.id || "");
    status.textContent = state.notes.length ? `已读取 ${state.notes.length} 条` : "无记录";
  } catch (error) {
    status.textContent = friendlyClientError(error);
  }
}

function renderNoteOptions() {
  const select = $("noteEntrySelect");
  select.innerHTML = "";
  const newOption = document.createElement("option");
  newOption.value = "";
  newOption.textContent = "新点评";
  select.appendChild(newOption);
  state.notes.forEach((entry) => {
    const option = document.createElement("option");
    option.value = entry.id;
    option.textContent = `${entry.title || "点评"} · ${entry.updatedAt || entry.createdAt || ""}`;
    select.appendChild(option);
  });
}

function selectNote(id) {
  state.currentNoteId = id;
  $("noteEntrySelect").value = id;
  const entry = state.notes.find((item) => item.id === id);
  $("noteTitle").value = entry?.title || "";
  $("noteText").value = entry?.content || "";
}

function newNote() {
  state.currentNoteId = "";
  $("noteEntrySelect").value = "";
  $("noteTitle").value = "";
  $("noteText").value = "";
  $("noteStatus").textContent = "新点评";
}

async function saveNote() {
  const button = $("saveNoteButton");
  const status = $("noteStatus");
  button.disabled = true;
  status.textContent = "保存中";
  try {
    const response = await fetch("/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        date: $("noteDate").value || todayDateValue(),
        id: state.currentNoteId,
        title: $("noteTitle").value,
        content: $("noteText").value,
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok || !payload.ok) throw new Error(payload.error || "点评保存失败");
    state.notes = payload.entries || [];
    renderNoteOptions();
    selectNote(payload.entry?.id || "");
    status.textContent = payload.content.trim() ? "已保存" : "已保存空点评";
  } catch (error) {
    status.textContent = friendlyClientError(error);
  } finally {
    button.disabled = false;
  }
}

async function summarizeNote() {
  const button = $("summarizeNoteButton");
  const status = $("noteStatus");
  button.disabled = true;
  status.textContent = "LLM总结中";
  try {
    const response = await fetch("/api/notes/summarize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        date: $("noteDate").value || todayDateValue(),
        content: $("noteText").value,
        report: state.lastPayload?.report || {},
      }),
    });
    const payload = await parseJsonResponse(response);
    if (!response.ok || !payload.ok) throw new Error(payload.error || "LLM总结失败");
    $("noteText").value = payload.summary || "";
    status.textContent = "已生成，记得保存";
  } catch (error) {
    status.textContent = friendlyClientError(error);
  } finally {
    button.disabled = false;
  }
}

async function loadReport(force = false) {
  if (state.reportLoading) return;
  state.reportLoading = true;
  const button = $("refreshButton");
  const boardButton = $("boardButton");
  const errorBox = $("errorBox");
  button.disabled = true;
  boardButton.disabled = true;
  errorBox.hidden = true;
  $("boardSignalText").textContent = "正在抓取行情数据，首次加载可能需要约 1 分钟...";
  if (force) renderLoadingRows();
  const loadingText = force ? "正在抓取全盘数据..." : "正在读取缓存...";
  $("boardSignalText").textContent = loadingText;

  const url = new URL("/api/report", window.location.origin);
  url.searchParams.set("board", state.board);
  if (force) url.searchParams.set("force", "1");

  try {
    const response = await fetch(url);
    const payload = await parseJsonResponse(response);
    if (!response.ok || !payload.ok) throw new Error(payload.error || "数据获取失败");
    renderReport(payload);
    const loadedAt = payload.loadedAtEpoch ? payload.loadedAtEpoch * 1000 : Date.now();
    state.nextRefreshAt = loadedAt + (payload.refreshSeconds || 900) * 1000;
  } catch (error) {
    errorBox.textContent = friendlyClientError(error);
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
    boardButton.disabled = false;
    state.reportLoading = false;
  }
}

function updateCountdown() {
  const remaining = Math.max(0, state.nextRefreshAt - Date.now());
  const minutes = Math.floor(remaining / 60000);
  const seconds = Math.floor((remaining % 60000) / 1000);
  $("countdown").textContent = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  if (remaining === 0) loadReport(true);
}

$("controlForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const nextBoard = $("boardInput").value.trim() || "光纤";
  state.board = nextBoard;
  $("boardInput").value = nextBoard;
  loadReport(false);
});

$("refreshButton").addEventListener("click", () => {
  loadReport(true);
});

document.querySelectorAll(".market-tab").forEach((button) => {
  button.addEventListener("click", () => {
    state.activeMarketView = button.dataset.marketView || "top20";
    if (state.lastPayload?.report) renderActiveMarketTable(state.lastPayload.report);
  });
});

$("noteDate").value = todayDateValue();
$("noteDate").addEventListener("change", loadNote);
$("noteEntrySelect").addEventListener("change", (event) => selectNote(event.target.value));
$("newNoteButton").addEventListener("click", newNote);
$("saveNoteButton").addEventListener("click", saveNote);
$("summarizeNoteButton").addEventListener("click", summarizeNote);

loadReport(false);
loadNote();
state.timer = window.setInterval(updateCountdown, 1000);
