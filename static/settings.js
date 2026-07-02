const $ = (id) => document.getElementById(id);

let currentSettings = null;

function showStatus(message, isError = false) {
  const box = $("settingsStatus");
  box.textContent = message;
  box.hidden = false;
  box.style.borderColor = isError ? "#efb5b5" : "#c8ded8";
  box.style.background = isError ? "#fff4f4" : "#f1faf7";
  box.style.color = isError ? "#9a3030" : "#1d6d53";
}

function asNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function prettyJson(value) {
  return JSON.stringify(value ?? [], null, 2);
}

function renderSettings(settings) {
  currentSettings = settings;
  const llm = settings.llm || {};
  $("llmBaseUrl").value = llm.baseUrl || "";
  $("llmModel").value = llm.model || "";
  $("llmApiKey").value = "";
  $("clearLlmApiKey").checked = false;
  $("llmKeyStatus").textContent = llm.apiKeySet ? `Key ${llm.apiKeyMasked}` : "未保存 Key";
  const llmScoring = settings.llmScoring || {};
  $("llmScoringEnabled").checked = Boolean(llmScoring.enabled);
  $("llmMaxAdjustment").value = llmScoring.maxAdjustment ?? 1;
  $("llmScoringStandard").value = llmScoring.standard || "";
  const stockWeights = settings.scoring?.stockWeights || {};
  $("stockWeightMarket").value = stockWeights.market ?? 0.3;
  $("stockWeightBoard").value = stockWeights.board ?? 0.3;
  $("stockWeightSelf").value = stockWeights.stock ?? 0.3;
  $("stockWeightRisk").value = stockWeights.risk ?? 0.1;
  const stockRules = settings.scoring?.stockRules || {};
  $("stockRuleRedChange").value = stockRules.redChangePoints ?? 2;
  $("stockRuleFundInflow").value = stockRules.fundInflowPoints ?? 3;
  $("stockRuleHighOpen").value = stockRules.highOpenPoints ?? 1;
  $("stockRuleHighTurnoverThreshold").value = stockRules.highTurnoverThresholdYi ?? 100;
  $("stockRuleHighTurnover").value = stockRules.highTurnoverPoints ?? 2;
  $("stockRuleMediumTurnoverThreshold").value = stockRules.mediumTurnoverThresholdYi ?? 50;
  $("stockRuleMediumTurnover").value = stockRules.mediumTurnoverPoints ?? 1;
  $("stockRuleBoardMember").value = stockRules.boardMemberPoints ?? 2;
  const boardTheme = settings.scoring?.boardThemeScoring || {};
  const precondition = boardTheme.precondition || {};
  $("boardThemeIndexCode").value = precondition.indexCode || "KS11";
  $("boardThemeLimitDown").value = precondition.limitDownPct ?? -8;
  $("boardThemeGroupsJson").value = prettyJson(boardTheme.groups || []);
  const breakout = settings.breakout || {};
  $("breakoutDays").value = breakout.days ?? 3;
  $("breakoutMinAmount").value = breakout.minAmountYi ?? 10;
  $("breakoutMinCap").value = breakout.minMarketCapYi ?? 70;
  $("breakoutMaxCap").value = breakout.maxMarketCapYi ?? 700;

  const signals = boardTheme.signals || {};
  $("bullishAbove").value = signals.bullishAbove ?? 7;
  $("neutralMin").value = signals.neutralMin ?? 4;
  $("neutralMax").value = signals.neutralMax ?? 7;
  $("bullishText").value = signals.bullishText || "";
  $("neutralText").value = signals.neutralText || "";
  $("weakText").value = signals.weakText || "";
}

function collectBoardThemeGroups() {
  const text = $("boardThemeGroupsJson").value.trim();
  if (!text) return [];
  const groups = JSON.parse(text);
  if (!Array.isArray(groups)) throw new Error("板块组合 JSON 必须是数组");
  return groups;
}

function collectSettings() {
  const apiKey = $("clearLlmApiKey").checked ? "__CLEAR__" : $("llmApiKey").value.trim();
  return {
    llm: {
      baseUrl: $("llmBaseUrl").value.trim(),
      model: $("llmModel").value.trim(),
      apiKey,
    },
    llmScoring: {
      enabled: $("llmScoringEnabled").checked,
      maxAdjustment: asNumber($("llmMaxAdjustment").value, 1),
      standard: $("llmScoringStandard").value.trim(),
    },
    breakout: {
      days: Math.max(1, Math.round(asNumber($("breakoutDays").value, 3))),
      minAmountYi: asNumber($("breakoutMinAmount").value, 10),
      minMarketCapYi: asNumber($("breakoutMinCap").value, 70),
      maxMarketCapYi: asNumber($("breakoutMaxCap").value, 700),
    },
    scoring: {
      boardThemeScoring: {
        enabled: true,
        precondition: {
          indexCode: $("boardThemeIndexCode").value.trim() || "KS11",
          indexName: "韩国综合指数",
          limitDownPct: asNumber($("boardThemeLimitDown").value, -8),
          triggerScore: 0,
          signal: "韩国综合指数触及跌停，科技股日内全线看空，冲高卖出",
        },
        signals: {
          bullishAbove: asNumber($("bullishAbove").value, 7),
          neutralMin: asNumber($("neutralMin").value, 4),
          neutralMax: asNumber($("neutralMax").value, 7),
          bullishText: $("bullishText").value.trim(),
          neutralText: $("neutralText").value.trim(),
          weakText: $("weakText").value.trim(),
        },
        groups: collectBoardThemeGroups(),
      },
      stockWeights: {
        market: asNumber($("stockWeightMarket").value, 0.3),
        board: asNumber($("stockWeightBoard").value, 0.3),
        stock: asNumber($("stockWeightSelf").value, 0.3),
        risk: asNumber($("stockWeightRisk").value, 0.1),
      },
      stockRules: {
        redChangePoints: asNumber($("stockRuleRedChange").value, 2),
        fundInflowPoints: asNumber($("stockRuleFundInflow").value, 3),
        highOpenPoints: asNumber($("stockRuleHighOpen").value, 1),
        highTurnoverThresholdYi: asNumber($("stockRuleHighTurnoverThreshold").value, 100),
        highTurnoverPoints: asNumber($("stockRuleHighTurnover").value, 2),
        mediumTurnoverThresholdYi: asNumber($("stockRuleMediumTurnoverThreshold").value, 50),
        mediumTurnoverPoints: asNumber($("stockRuleMediumTurnover").value, 1),
        boardMemberPoints: asNumber($("stockRuleBoardMember").value, 2),
      },
    },
  };
}

async function loadSettings() {
  const response = await fetch("/api/settings");
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || "设置读取失败");
  renderSettings(payload.settings);
}

async function saveSettings() {
  const button = $("saveSettingsButton");
  button.disabled = true;
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: collectSettings() }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "设置保存失败");
    renderSettings(payload.settings);
    showStatus("设置已保存，回主页手动刷新全盘后生效");
  } catch (error) {
    showStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
}

$("saveSettingsButton").addEventListener("click", saveSettings);

loadSettings().catch((error) => showStatus(error.message, true));
