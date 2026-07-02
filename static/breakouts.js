const $ = (id) => document.getElementById(id);

function value(id) {
  return $(id).value.trim();
}

function changeClass(value) {
  const number = Number(value);
  if (Number.isNaN(number) || number === 0) return "";
  return number > 0 ? "up" : "down";
}

function renderRows(records) {
  const body = $("breakoutBody");
  body.innerHTML = "";
  if (!records.length) {
    body.innerHTML = '<tr><td colspan="9">未筛选到最终符合条件的股票</td></tr>';
    return;
  }
  records.forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${row["名称"] || "-"}</strong></td>
      <td>${row["代码"] || "-"}</td>
      <td>${row["收盘价_display"] || "-"}</td>
      <td class="${changeClass(row["涨跌幅"])}">${row["涨跌幅_display"] || "-"}</td>
      <td>${row["总市值_display"] || "-"}</td>
      <td>${row["今日成交额_display"] || "-"}</td>
      <td title="${row["近3日成交额"] || ""}">${row["近3日成交额"] || "-"}</td>
      <td>${row["此前最高收盘_display"] || "-"}</td>
      <td class="up">${row["突破幅度_display"] || "-"}</td>
    `;
    body.appendChild(tr);
  });
}

function updateRecentAmountHeader() {
  $("recentAmountHeader").textContent = `近${value("daysInput") || "3"}日成交额`;
}

async function loadBreakoutDefaults() {
  try {
    const response = await fetch("/api/settings");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "设置读取失败");
    const config = payload.settings?.breakout || {};
    $("daysInput").value = config.days ?? 3;
    $("amountInput").value = config.minAmountYi ?? 10;
    $("minCapInput").value = config.minMarketCapYi ?? 70;
    $("maxCapInput").value = config.maxMarketCapYi ?? 700;
    updateRecentAmountHeader();
  } catch (error) {
    const status = $("breakoutStatus");
    status.textContent = error.message;
    status.hidden = false;
  }
}

async function scanBreakouts(force = true) {
  const button = $("scanButton");
  const status = $("breakoutStatus");
  button.disabled = true;
  status.hidden = true;
  updateRecentAmountHeader();
  $("breakoutSummary").textContent = "扫描中";
  $("breakoutBody").innerHTML = '<tr><td colspan="9">正在用本地快照筛候选，并检查候选股历史日线，可能需要 1-3 分钟...</td></tr>';

  const url = new URL("/api/breakouts", window.location.origin);
  url.searchParams.set("days", value("daysInput") || "3");
  url.searchParams.set("minAmountYi", value("amountInput") || "10");
  url.searchParams.set("minMarketCapYi", value("minCapInput") || "70");
  url.searchParams.set("maxMarketCapYi", value("maxCapInput") || "700");
  if (force) url.searchParams.set("force", "1");

  try {
    const response = await fetch(url);
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "筛选失败");
    renderRows(payload.records || []);
    const meta = payload.meta || {};
    const resultCount = meta.resultCount || 0;
    const candidateCount = meta.candidateCount || 0;
    $("breakoutSummary").textContent = `${resultCount} 只 / 候选 ${candidateCount} 只`;
    const explain = candidateCount === 0
      ? "候选0只表示：第一步按成交额和总市值过滤后，没有股票进入历史新高检查。"
      : `候选${candidateCount}只表示：这些股票先满足成交额和总市值条件，再检查是否上市以来最高收盘。`;
    status.className = "status-box";
    status.textContent = `${meta.loadedAt || ""}；${explain}${meta.error ? `；${meta.error}` : ""}${meta.rule ? `；${meta.rule}` : ""}`;
    status.hidden = false;
  } catch (error) {
    $("breakoutSummary").textContent = "失败";
    status.className = "error-box";
    status.textContent = error.message;
    status.hidden = false;
  } finally {
    button.disabled = false;
  }
}

$("scanButton").addEventListener("click", () => scanBreakouts(true));
loadBreakoutDefaults();
