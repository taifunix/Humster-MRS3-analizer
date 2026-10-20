const portfolioSettingsHelpers = (() => {
  const profiles = ['AGGRESSIVE', 'BALANCED', 'CONSERVATIVE'];
  const riskFields = Object.freeze(['max_actual_equity_dd_pct', 'min_calculated_free_margin_reserve_pct', 'max_calculated_account_mm_load_pct']);
  const riskDefaults = Object.freeze({
    AGGRESSIVE: Object.freeze({ max_actual_equity_dd_pct: '20', min_calculated_free_margin_reserve_pct: '20', max_calculated_account_mm_load_pct: '50' }),
    BALANCED: Object.freeze({ max_actual_equity_dd_pct: '10', min_calculated_free_margin_reserve_pct: '40', max_calculated_account_mm_load_pct: '35' }),
    CONSERVATIVE: Object.freeze({ max_actual_equity_dd_pct: '5', min_calculated_free_margin_reserve_pct: '60', max_calculated_account_mm_load_pct: '20' }),
  });
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const settingsMoneyParts = (value) => {
    if (typeof value === 'number' && (!Number.isSafeInteger(value) || value <= 0)) return null;
    const text = typeof value === 'number' ? String(value) : value;
    if (typeof text !== 'string' || !/^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(text)) return null;
    const pieces = text.split('.'); const digits = `${pieces[0]}${pieces[1] || ''}`; const scaled = BigInt(digits || '0');
    return scaled > 0n ? { text, scale: (pieces[1] || '').length, scaled } : null;
  };
  const settingsIntegerValue = (raw, allowZero = false) => {
    const text = String(raw ?? '').trim();
    if (!(allowZero ? /^(?:0|[1-9][0-9]*)$/ : /^[1-9][0-9]*$/).test(text)) throw new Error('integer');
    const value = Number(text); if (!Number.isSafeInteger(value)) throw new Error('integer');
    return value;
  };
  const settingsDecimalParts = (value) => {
    const text = typeof value === 'number' ? String(value) : value;
    if (typeof text !== 'string' || !/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/.test(text)) return null;
    const pieces = text.replace('-', '').split('.'); const digits = `${pieces[0]}${pieces[1] || ''}`; const scaled = BigInt(digits || '0');
    return { text, negative: text.startsWith('-'), scale: (pieces[1] || '').length, scaled };
  };
  const settingsDecimalInRange = (value, minimum, maximum) => {
    if (typeof value !== 'string') return false;
    const parts = settingsDecimalParts(value); const low = settingsDecimalParts(minimum); const high = settingsDecimalParts(maximum);
    if (!parts || !low || !high || parts.negative) return false;
    const compare = (left, right) => {
      const scale = Math.max(left.scale, right.scale);
      const a = left.scaled * 10n ** BigInt(scale - left.scale); const b = right.scaled * 10n ** BigInt(scale - right.scale);
      return a < b ? -1 : a > b ? 1 : 0;
    };
    return compare(parts, low) >= 0 && compare(parts, high) <= 0;
  };
  const settingsDecimalValue = (raw, original, positive = false) => {
    const text = String(raw ?? '').trim(); const parts = settingsDecimalParts(text);
    if (!parts || (positive && (parts.negative || parts.scaled === 0n))) throw new Error('decimal');
    if (text === String(original)) return original;
    if (typeof original === 'string' || text.includes('.') || parts.negative) return text;
    const value = Number(text); if (!Number.isSafeInteger(value)) throw new Error('decimal');
    return value;
  };
  const settingsFeeRateValue = (raw, original) => {
    const text = String(raw ?? '').trim(); const parts = settingsDecimalParts(text);
    if (!parts || parts.negative || parts.scale > 12 || parts.scaled >= 10n ** BigInt(parts.scale)) throw new Error('fee-rate');
    return text === String(original) ? original : text;
  };
  const settingsRiskValue = (raw, original) => {
    const text = String(raw ?? '').trim(); const parts = settingsDecimalParts(text);
    const integerDigits = parts ? parts.text.replace('-', '').split('.')[0].length : 0;
    const maximum = parts ? 100n * (10n ** BigInt(parts.scale)) : -1n;
    if (!parts || parts.negative || parts.scale > 12 || integerDigits > 26 || parts.scaled > maximum) throw new Error('risk-decimal');
    return text === String(original) ? original : text;
  };
  const settingsWeekdayMinutes = (raw) => {
    if (typeof raw !== 'string') return null;
    const match = /^(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY) ([0-9]{2}):([0-9]{2})$/.exec(raw);
    if (!match || Number(match[2]) > 23 || Number(match[3]) > 59) return null;
    return ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY'].indexOf(match[1]) * 1440 + Number(match[2]) * 60 + Number(match[3]);
  };
  const weightedIntegerRules = Object.freeze({
    history_step_minutes: { positive: true }, lp_solutions_per_profile: { positive: true, maximum: 20 },
    max_targets: { positive: true, maximum: 8 },
    bootstrap_scenarios_per_block: { positive: true }, bootstrap_diagnostic_scenarios: { positive: true },
    wall_time_seconds: { positive: true }, solver_time_seconds: { positive: true },
    limiter_step: { positive: true }, limiter_controls: { maximum: 2 },
    priority_groups: { positive: true, maximum: 5 },
  });
  const weightedNumberRules = Object.freeze({
    limiter_stress_pct: { minimum: 0, maximumExclusive: 100 }, priority_beta: { minimum: 0, maximum: 1 },
    priority_close_ratio: { minimumExclusive: 1 },
  });
  const weightedSearchKeys = Object.freeze([...Object.keys(weightedIntegerRules), ...Object.keys(weightedNumberRules)]);
  const weightedSearchValid = (weighted) => {
    if (!weighted || typeof weighted !== 'object' || Array.isArray(weighted) || Object.keys(weighted).sort().join(',') !== [...weightedSearchKeys].sort().join(',')) return false;
    for (const [name, rule] of Object.entries(weightedIntegerRules)) {
      const value = weighted[name];
      if (!Number.isSafeInteger(value) || (rule.positive ? value <= 0 : value < 0) || (rule.maximum !== undefined && value > rule.maximum)) return false;
    }
    for (const [name, rule] of Object.entries(weightedNumberRules)) {
      const value = weighted[name];
      if (typeof value !== 'number' || !Number.isFinite(value) || !settingsDecimalParts(String(value)) || (rule.positive && value <= 0) || (rule.minimum !== undefined && value < rule.minimum) || (rule.minimumExclusive !== undefined && value <= rule.minimumExclusive) || (rule.maximum !== undefined && value > rule.maximum) || (rule.maximumExclusive !== undefined && value >= rule.maximumExclusive)) return false;
    }
    return weighted.bootstrap_diagnostic_scenarios <= weighted.bootstrap_scenarios_per_block;
  };
  const weightedIntegerValue = (raw, rule) => {
    const value = settingsIntegerValue(raw, !rule.positive);
    if (rule.maximum !== undefined && value > rule.maximum) throw new Error('integer');
    return value;
  };
  const weightedNumberValue = (raw, rule) => {
    const parts = settingsDecimalParts(String(raw ?? '').trim()); const value = Number(parts?.text);
    if (!parts || !Number.isFinite(value) || !settingsDecimalParts(String(value)) || (rule.positive && value <= 0) || (rule.minimum !== undefined && value < rule.minimum) || (rule.minimumExclusive !== undefined && value <= rule.minimumExclusive) || (rule.maximum !== undefined && value > rule.maximum) || (rule.maximumExclusive !== undefined && value >= rule.maximumExclusive)) throw new Error('number');
    return value;
  };
  const revealInvalidField = (field) => { const details = field?.closest?.('details'); if (details) details.open = !!1; field?.focus?.(); };
  const settingsValidationFeedback = (error, query, meta, reveal = revealInvalidField) => {
    const field = error?.field ? query(`#portfolio-settings-${error.field}`) : null;
    reveal(field);
    if (field) { field.setAttribute('aria-invalid', 'true'); field.setCustomValidity('Введите корректное значение для поля.'); }
    const label = field?.closest?.('.field-group')?.querySelector('label')?.textContent || 'Поле настроек';
    if (meta) meta.textContent = `${label}: Введите корректное значение для поля.`;
    return field;
  };
  const validMoney = (money) => !!money && typeof money === 'object' && !Array.isArray(money) && Object.keys(money).sort().join(',') === 'amount,currency' && typeof money.currency === 'string' && !!money.currency.trim() && !!settingsMoneyParts(money.amount);
  const validSettingsDocument = (doc) => {
    const liquidity = doc?.liquidity; const parameters = liquidity?.parameters;
    const weighted = doc?.search?.weighted_search;
    const compositionParameters = doc?.search?.composition?.parameters;
    const start = settingsWeekdayMinutes(liquidity?.weekend_start_utc); const end = settingsWeekdayMinutes(liquidity?.weekend_end_utc);
    const rounding = settingsDecimalParts(liquidity?.round_down_usdt);
    if (!doc || typeof doc !== 'object' || Array.isArray(doc) || doc.schema_version !== 3 || !Number.isSafeInteger(doc.search?.seed) || doc.search.seed < 0 || !Number.isSafeInteger(compositionParameters?.minimum_common_days) || compositionParameters.minimum_common_days <= 0 || !Number.isSafeInteger(doc.search?.total_test_budget) || doc.search.total_test_budget <= 0 || doc.search.sizing_mode !== 'liquidity_cap_single' || !Number.isSafeInteger(doc.search.max_enumerated_combinations) || doc.search.max_enumerated_combinations <= 0 || !weightedSearchValid(weighted) || Object.keys(parameters || {}).sort().join(',') !== 'lot_model_base_coefficient,lot_model_max_shift_bonus' || !settingsDecimalInRange(parameters?.lot_model_base_coefficient, '1', '20') || !settingsDecimalInRange(parameters?.lot_model_max_shift_bonus, '0', '2') || !rounding || rounding.negative || rounding.scaled === 0n || !Number.isSafeInteger(liquidity.minimum_coverage_pct) || liquidity.minimum_coverage_pct < 1 || liquidity.minimum_coverage_pct > 100 || !Number.isSafeInteger(liquidity.maximum_age_hours) || liquidity.maximum_age_hours <= 0 || start === null || end === null || start === end || !Number.isSafeInteger(liquidity.archive_publication_lag_hours) || liquidity.archive_publication_lag_hours < 0 || liquidity.archive_publication_lag_hours > 48 || typeof liquidity.backfill_write_enabled !== 'boolean' || typeof liquidity.spread_history_bypass_pretest !== 'boolean') return false;
    for (const profile of profiles) {
      const scenario = doc.scenarios?.[profile]; const profileDocument = doc.profiles?.[profile]; const ranking = profileDocument?.ranking;
      if (!scenario || !validMoney(scenario.deposit) || !validMoney(scenario.collateral) || !validMoney(scenario.max_balance) || !validMoney(scenario.sizing?.upper_bound) || scenario.sizing.grid !== undefined || !settingsDecimalParts(profileDocument?.individual_max_dd_pct) || settingsDecimalParts(profileDocument.individual_max_dd_pct).negative || settingsDecimalParts(profileDocument.individual_max_dd_pct).scaled === 0n || !settingsDecimalParts(profileDocument.individual_net_pnl_min_exclusive) || riskFields.some((field) => profileDocument[field] !== undefined && (() => { try { settingsRiskValue(profileDocument[field], profileDocument[field]); return false; } catch (_) { return true; } })()) || !Number.isSafeInteger(ranking?.top_n) || ranking.top_n <= 0) return false;
      if ([scenario.deposit, scenario.collateral, scenario.max_balance].some((money) => money.currency !== scenario.sizing.upper_bound.currency)) return false;
    }
    return true;
  };
  const settingsPatch = (document, values) => {
    if (!validSettingsDocument(document)) throw new Error('invalid-document');
    const payload = clone(document);
    if (values.exchange !== undefined) {   // Panel stage 5e-2: absent = Bybit, so an untouched Bybit keeps the digest
      if (!['Bybit', 'BingX'].includes(values.exchange)) { const error = new Error('exchange'); error.field = 'exchange'; throw error; }
      if (values.exchange !== 'Bybit' || (payload.inputs && 'exchange' in payload.inputs)) payload.inputs = { ...payload.inputs, exchange: values.exchange };
    }
    try { payload.search.seed = settingsIntegerValue(values.seed ?? document.search.seed, true); } catch (error) { error.field = 'seed'; throw error; }
    try { payload.search.composition.parameters.minimum_common_days = settingsIntegerValue(values.minimum_common_days ?? document.search.composition.parameters.minimum_common_days); } catch (error) { error.field = 'minimum-common-days'; throw error; }
    try { payload.search.max_enumerated_combinations = settingsIntegerValue(values.max_enumerated_combinations ?? document.search.max_enumerated_combinations); } catch (error) { error.field = 'max-enumerated-combinations'; throw error; }
    try { const value = settingsDecimalValue(values.lot_model_base_coefficient ?? document.liquidity.parameters.lot_model_base_coefficient, document.liquidity.parameters.lot_model_base_coefficient); if (!settingsDecimalInRange(value, '1', '20')) throw new Error('decimal'); payload.liquidity.parameters.lot_model_base_coefficient = value; } catch (error) { error.field = 'lot-model-base-coefficient'; throw error; }
    try { const value = settingsDecimalValue(values.lot_model_max_shift_bonus ?? document.liquidity.parameters.lot_model_max_shift_bonus, document.liquidity.parameters.lot_model_max_shift_bonus); if (!settingsDecimalInRange(value, '0', '2')) throw new Error('decimal'); payload.liquidity.parameters.lot_model_max_shift_bonus = value; } catch (error) { error.field = 'lot-model-max-shift-bonus'; throw error; }
    try { payload.liquidity.round_down_usdt = settingsDecimalValue(values.round_down_usdt, document.liquidity.round_down_usdt, true); } catch (error) { error.field = 'round-down-usdt'; throw error; }
    try { payload.liquidity.minimum_coverage_pct = settingsIntegerValue(values.minimum_coverage_pct); if (payload.liquidity.minimum_coverage_pct > 100) throw new Error('integer'); } catch (error) { error.field = 'minimum-coverage-pct'; throw error; }
    try { payload.liquidity.maximum_age_hours = settingsIntegerValue(values.maximum_age_hours); } catch (error) { error.field = 'maximum-age-hours'; throw error; }
    try { payload.liquidity.archive_publication_lag_hours = settingsIntegerValue(values.archive_publication_lag_hours, true); if (payload.liquidity.archive_publication_lag_hours > 48) throw new Error('integer'); } catch (error) { error.field = 'archive-publication-lag-hours'; throw error; }
    if (typeof values.backfill_write_enabled !== 'boolean') { const error = new Error('boolean'); error.field = 'backfill-write-enabled'; throw error; }
     payload.liquidity.backfill_write_enabled = values.backfill_write_enabled;
     if (typeof values.spread_history_bypass_pretest !== 'boolean') { const error = new Error('boolean'); error.field = 'spread-history-bypass-pretest'; throw error; }
     payload.liquidity.spread_history_bypass_pretest = values.spread_history_bypass_pretest;
    if (values.open_fee_rate !== undefined || values.close_fee_rate !== undefined) {
      const margin = payload.margin?.parameters;
      if (!margin || typeof margin !== 'object' || Array.isArray(margin)) { const error = new Error('margin'); error.field = 'open-fee-rate'; throw error; }
      if (values.open_fee_rate !== undefined) try { margin.open_fee_rate = settingsFeeRateValue(values.open_fee_rate, margin.open_fee_rate); } catch (error) { error.field = 'open-fee-rate'; throw error; }
      if (values.close_fee_rate !== undefined) try { margin.close_fee_rate = settingsFeeRateValue(values.close_fee_rate, margin.close_fee_rate); } catch (error) { error.field = 'close-fee-rate'; throw error; }
    }
    const weightedValues = values.weighted_search || {};
    for (const [name, rule] of Object.entries(weightedIntegerRules)) {
      try { payload.search.weighted_search[name] = weightedIntegerValue(weightedValues[name] ?? document.search.weighted_search[name], rule); } catch (error) { error.field = `weighted-${name.replaceAll('_', '-')}`; throw error; }
    }
    for (const [name, rule] of Object.entries(weightedNumberRules)) {
      try { payload.search.weighted_search[name] = weightedNumberValue(weightedValues[name] ?? document.search.weighted_search[name], rule); } catch (error) { error.field = `weighted-${name.replaceAll('_', '-')}`; throw error; }
    }
    const riskValues = values.risk_policy;
    if (riskValues !== undefined) {
      for (const profile of profiles) {
        for (const field of riskFields) {
          try {
            payload.profiles[profile][field] = settingsRiskValue(riskValues?.[profile]?.[field], payload.profiles[profile][field] ?? riskDefaults[profile][field]);
          } catch (error) { error.field = `risk-${profile.toLowerCase()}-${field.replaceAll('_', '-')}`; throw error; }
        }
      }
    }
    if (payload.search.weighted_search.bootstrap_diagnostic_scenarios > payload.search.weighted_search.bootstrap_scenarios_per_block) {
      const error = new Error('integer'); error.field = 'weighted-bootstrap-diagnostic-scenarios'; throw error;
    }
    return payload;
  };
  return { clone, profiles, riskFields, riskDefaults, settingsIntegerValue, settingsDecimalValue, settingsFeeRateValue, settingsRiskValue, revealInvalidField, settingsValidationFeedback, weightedSearchKeys, validSettingsDocument, settingsPatch };
})();
if (typeof globalThis !== 'undefined') globalThis.portfolioSettingsHelpers = portfolioSettingsHelpers;

const portfolioReasonHelpers = (() => {
  const labels = Object.freeze({
    INSUFFICIENT_DIRECTIONAL_UNIVERSE: 'после фильтров не осталось ни одного допустимого состава',
    SIZE_BELOW_MINIMUM_QTY: 'после округления количество ниже минимального размера ордера Bybit',
    SIZE_ROUNDED_TO_ZERO: 'расчётный размер позиции меньше шага округления; уменьшите шаг или исключите пару из universe',
  });
  const humanize = (value) => String(value ?? '').replace(/(^|[;\s])([A-Z][A-Z0-9_]*):([A-Z][A-Z0-9_]*)/g, (match, separator, prefix, code) => {
    const label = labels[code];
    return label ? `${separator}${prefix}: ${label}` : match;
  });
  return { humanize };
})();
if (typeof globalThis !== 'undefined') globalThis.portfolioReasonHelpers = portfolioReasonHelpers;

const portfolioResultHelpers = (() => {
  const missing = new Set(['', 'UNKNOWN', 'NOT_TESTED', 'NONE', 'NULL']);
  const profiles = Object.freeze({ AGGRESSIVE: 'Агрессивный', BALANCED: 'Сбалансированный', CONSERVATIVE: 'Консервативный' });
  const warnings = Object.freeze({
    SPREAD_HISTORY_BYPASSED_PRETEST: 'История спреда временно не участвует в предварительном расчёте.',
  });
  const known = (value) => value !== null && value !== undefined && !missing.has(String(value).trim().toUpperCase());
  const metric = (value, maximumFractionDigits = 2, suffix = '') => {
    if (!known(value)) return '';
    const number = Number(value);
    const text = Number.isFinite(number)
      ? new Intl.NumberFormat('ru-RU', { maximumFractionDigits }).format(number)
      : String(value);
    return `${text}${suffix}`;
  };
  const bank = (value) => String(value ?? '').trim().toUpperCase() === 'UNCAPPED' ? 'Без ограничения' : metric(value, 2, ' USDT');
  const profile = (value) => profiles[String(value || '').toUpperCase()] || String(value || 'Без профиля');
  const warning = (value) => warnings[String(value || '')] || portfolioReasonHelpers.humanize(value);
  const ratio = (value) => known(value) && String(value) !== '—' ? metric(value, 2, '%') : '—';
  const amountRatios = (amount, target, saturation) => known(amount) ? `${metric(amount, 2, ' USDT')} · ${ratio(target)} / ${ratio(saturation)}` : '';
  const orderPercentages = (value) => Array.isArray(value) && value.length ? value.map((item) => ratio(item)).join(' / ') : '—';
  return { known, metric, bank, ratio, amountRatios, orderPercentages, profile, warning };
})();
if (typeof globalThis !== 'undefined') globalThis.portfolioResultHelpers = portfolioResultHelpers;

// «Пары и стратегии» main screen (docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 3 contract»): the table
// filter and sort follow the same rules as the server's Excel export (panel_main.filter_rows / sort_rows).
const mainScreenHelpers = (() => {
  const empty = (value) => value === null || value === undefined || value === '';
  const DECIMAL = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
  const number = (value) => {           // numbers and decimal strings only (panel_main._number)
    if (typeof value === 'number') return Number.isFinite(value) ? value : null;
    if (typeof value === 'string' && DECIMAL.test(value.trim())) return Number(value);
    return null;
  };
  const filterRows = (rows, columns, text) => {
    const needle = String(text || '').trim().toLowerCase();
    if (!needle) return rows.slice();
    return rows.filter((row) => columns.some((key) => String(empty(row[key]) ? '' : row[key]).toLowerCase().includes(needle)));
  };
  const sortRows = (rows, key, descending = false) => {
    if (!key) return rows.slice();
    const filled = rows.filter((row) => !empty(row[key]));
    const blank = rows.filter((row) => empty(row[key]));
    const numeric = filled.every((row) => number(row[key]) !== null);
    const value = (row) => (numeric ? number(row[key]) : String(row[key]).toLowerCase());
    const sign = descending ? -1 : 1;
    const ordered = filled.slice().sort((a, b) => {
      const left = value(a);
      const right = value(b);
      return left < right ? -sign : left > right ? sign : 0;
    });
    return ordered.concat(blank);
  };
  const duration = (value) => {
    const seconds = Math.max(0, Math.round(Number(value) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return hours ? `${hours}ч ${minutes}м` : `${minutes}м ${seconds % 60}с`;
  };
  const taskSummary = (task) => {
    const state = String(task?.state || 'idle');
    const progress = task?.progress || {};
    const done = Number.isInteger(progress.done) ? progress.done : null;
    const total = Number.isInteger(progress.total) && progress.total > 0 ? progress.total : null;
    const percent = done !== null && total ? Math.min(100, Math.round((done / total) * 100)) : (state === 'done' ? 100 : 0);
    const parts = [];
    if (progress.step) parts.push([progress.step, progress.pair].filter(Boolean).join(' · '));
    if (done !== null && total) parts.push(`${done} из ${total}`);
    if (state !== 'idle') parts.push(`прошло ${duration(task.elapsed_s)}`);
    if (state === 'running' && task.eta_s !== null && task.eta_s !== undefined) parts.push(`осталось ≈ ${duration(task.eta_s)}`);
    if (state === 'running' && task.stopping) parts.push('остановка…');
    const words = { idle: 'Нет активной задачи.', running: 'Выполняется', done: 'Готово', failed: 'Ошибка', stopped: 'Остановлено' };
    const head = words[state] || state;
    return {
      state,
      badge: state.toUpperCase(),
      badgeClass: state === 'failed' ? 'state-error' : state === 'running' ? 'state-running' : state === 'stopped' ? 'state-pending' : 'state-ready',
      percent,
      text: state === 'idle' ? head : [head, ...parts].join(' · '),
    };
  };
  return { filterRows, sortRows, taskSummary, duration };
})();
if (typeof globalThis !== 'undefined') globalThis.mainScreenHelpers = mainScreenHelpers;

const panelRequestErrorHelpers = (() => {
  const safeStringPrefixes = [
    '/api/v2/strategies/fresh/',
    '/api/v2/main/',
    '/api/v2/strategies/store-ready/',
  ];
  const safeStringEndpoints = new Set([]);
  const allowsSafeString = (endpoint) => (
    safeStringEndpoints.has(endpoint) || safeStringPrefixes.some((prefix) => endpoint.startsWith(prefix))
  );
  return { allowsSafeString };
})();
if (typeof globalThis !== 'undefined') globalThis.panelRequestErrorHelpers = panelRequestErrorHelpers;

const ORDER_BUCKETS = ['1ORD', '2ORD', '3ORD', '4ORD'];
const testerCollectionUiHelpers = (() => {
  const endpoint = '/api/v2/strategies/tester/report-collection';
  const create = ({ request, apply = () => {}, onBusyChange = () => {} }) => {
    let busyAction = '';
    let epoch = 0;
    let loadPromise = null;
    const begin = (action) => {
      if (busyAction) return null;
      busyAction = action;
      epoch += 1;
      onBusyChange(true, action);
      return Object.freeze({ action, epoch });
    };
    const finish = (lease) => {
      if (busyAction !== lease?.action) return;
      busyAction = '';
      onBusyChange(false, lease.action);
    };
    const load = () => {
      if (loadPromise) return loadPromise;
      const requestEpoch = epoch;
      loadPromise = Promise.resolve()
        .then(() => request(endpoint))
        .then((value) => {
          if (requestEpoch !== epoch) return false;
          apply(value);
          return true;
        })
        .finally(() => { loadPromise = null; });
      return loadPromise;
    };
    const refresh = async () => {
      if (loadPromise) await loadPromise;
      return load();
    };
    const run = async (action, task) => {
      const lease = begin(action);
      if (!lease) return { blocked: true };
      try {
        const result = await task(lease);
        await refresh();
        return { blocked: false, result };
      } finally {
        finish(lease);
      }
    };
    return {
      begin,
      finish,
      load,
      refresh,
      run,
      canRun: () => !busyAction,
      isCurrent: (lease) => Boolean(lease) && lease.epoch === epoch && lease.action === busyAction,
      commit: (lease, callback) => {
        if (!Boolean(lease) || lease.epoch !== epoch || lease.action !== busyAction) return false;
        callback();
        return true;
      },
      invalidate: () => { epoch += 1; },
      get busy() { return Boolean(busyAction); },
      get action() { return busyAction; },
    };
  };
  return { create, endpoint };
})();
if (typeof globalThis !== 'undefined') globalThis.testerCollectionUiHelpers = testerCollectionUiHelpers;
/* portfolio-selection-helpers:start */
const portfolioSelectionHelpers = (() => {
  const DAY_MS = 86400000;
  const day = (value) => { const time = Date.parse(`${value}T00:00:00Z`); return Number.isFinite(time) ? time : null; };
  const limit = (requested, available) => (Number.isSafeInteger(requested) && requested > 0 && Number.isSafeInteger(available) && available > 0 ? Math.min(requested, available) : 0);
  // The optimizer keeps the top-N finalists by User Rank per Pair + Side.
  const ranked = (history, key, count) => (Array.isArray(history?.[key]) ? history[key] : []).slice(0, count);
  const selectedHistory = (row, history) => [
    ...ranked(history, `${row.pair}|LONG`, limit(row.long, row.finalistLong)),
    ...ranked(history, `${row.pair}|SHORT`, limit(row.short, row.finalistShort)),
  ];
  // Common history = latest start .. earliest end over whole UTC days, as the common pretest period.
  const commonWindow = (items) => {
    let start = null; let end = null;
    for (const item of items) {
      const first = day(item?.start); const last = day(item?.end);
      if (first === null || last === null) continue;
      start = start === null ? first : Math.max(start, first);
      end = end === null ? last : Math.min(end, last);
    }
    if (start === null) return null;
    return { start, end, days: Math.max(0, Math.round((end - start) / DAY_MS)) };
  };
  const short = (time) => { const date = new Date(time); return `${String(date.getUTCDate()).padStart(2, '0')}.${String(date.getUTCMonth() + 1).padStart(2, '0')}`; };
  const label = (value) => (!value ? '—' : (value.end < value.start ? 'нет общего периода' : `≈ ${value.days} д · ${short(value.start)}–${short(value.end)}`));
  // Rough runtime model fitted to the 2026-10-10 real runs (61 pairs, 117 finalists, 21 days, 10 levels: about 16 min).
  const etaSeconds = (pairs, finalists, days, profiles) => {
    const scale = Math.max(1, days || 21) / 21;
    const base = 30 + 1.6 * pairs + 0.5 * finalists * scale;
    return profiles.reduce((total, profile) => {
      const levels = Number.isSafeInteger(Number(profile?.candidates)) ? Math.max(1, Number(profile.candidates)) : 10;
      return total + (levels + 2) * 0.17 * finalists * scale + levels * 1.0 * pairs * scale;
    }, base);
  };
  const duration = (seconds) => {
    if (!Number.isFinite(seconds)) return '—';
    const minutes = Math.round(seconds / 60);
    if (minutes < 1) return '< 1 мин';
    return minutes < 60 ? `≈ ${minutes} мин` : `≈ ${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
  };
  const summary = (rows, history, profiles) => {
    let pairs = 0; let finalists = 0; const items = [];
    for (const row of rows) {
      const count = limit(row.long, row.finalistLong) + limit(row.short, row.finalistShort);
      if (!count) continue;
      pairs += 1; finalists += count; items.push(...selectedHistory(row, history));
    }
    const common = commonWindow(items);
    return { pairs, finalists, window: common, etaSeconds: pairs ? etaSeconds(pairs, finalists, common?.days, profiles) : 0 };
  };
  const labelParts = (value) => (!value ? null : (value.end < value.start ? { days: 'нет общего периода', range: '' } : { days: `≈ ${value.days} д`, range: `${short(value.start)}–${short(value.end)}` }));
  const pairWindow = (row, history) => commonWindow(selectedHistory(row, history));
  // Display order only: alpha A–Z, history shortest first (unknown last), finalists most first.
  const sortRows = (rows, history, mode) => {
    const byName = (left, right) => String(left.pair).localeCompare(String(right.pair));
    const days = (row) => { const value = pairWindow(row, history); return value && value.end >= value.start ? value.days : Number.POSITIVE_INFINITY; };
    const available = (row) => (Number(row.finalistLong) || 0) + (Number(row.finalistShort) || 0);
    const order = mode === 'history' ? (left, right) => (days(left) - days(right)) || byName(left, right)
      : mode === 'finalists' ? (left, right) => (available(right) - available(left)) || byName(left, right)
        : byName;
    return [...rows].sort(order);
  };
  // "Select N finalists": every pair, each side capped by its available finalists.
  const limitRow = (row, count) => {
    const cap = (available) => (Number.isSafeInteger(available) && available > 0 ? Math.min(count, available) : 0);
    const long = cap(row.finalistLong); const shortSide = cap(row.finalistShort);
    return { long, short: shortSide, selected: long > 0 || shortSide > 0 };
  };
  return { pairWindow, summary, label, labelParts, duration, etaSeconds, sortRows, limitRow };
})();
if (typeof globalThis !== 'undefined') globalThis.portfolioSelectionHelpers = portfolioSelectionHelpers;
/* portfolio-selection-helpers:end */
  const { selectCommittedRetestTester, selectRetestTester } = window.retestRecovery;
(() => {
  const screens = [...document.querySelectorAll('.screen')];
  const links = [...document.querySelectorAll('[data-screen-link]')];
  const breadcrumb = document.querySelector('#breadcrumb');
  const status = document.querySelector('#status');
  const panelReload = document.querySelector('#panel-reload');
  const showRequestError = (error) => {
    if (status) status.textContent = error?.message || 'Backend request failed.';
  };
  const formatErrorReason = (value, fallback = 'request failed') => {
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (value && typeof value === 'object') {
      const code = typeof value.code === 'string' ? value.code.trim() : '';
      const message = typeof value.message === 'string' ? value.message.trim() : '';
      if (code && message && code !== message) return `${code}: ${message}`;
      if (message || code) return message || code;
    }
    return fallback;
  };
  const requestJson = async (endpoint, options = {}) => {
    try {
      const response = await fetch(endpoint, options);
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.toLowerCase().includes('application/json')) {
        throw new Error(response.ok ? 'Backend returned invalid JSON.' : 'Server validation failed.');
      }
      let result;
      try { result = await response.json(); } catch (_) { throw new Error('Backend returned invalid JSON.'); }
      if (!response.ok) {
        const code = typeof result?.error === 'string' && (panelRequestErrorHelpers.allowsSafeString(endpoint) || /^[A-Z_]+$/.test(result.error)) ? result.error : 'Server validation failed.';
        const payload = result?.error;
        const typedCode = payload && typeof payload === 'object' && typeof payload.code === 'string' ? payload.code : code;
        const typedMessage = payload && typeof payload === 'object' && typeof payload.message === 'string'
          ? payload.message
          : typeof result?.message === 'string' ? result.message : code;
        const failure = new Error(typedMessage || typedCode);
        failure.code = typedCode;
        throw failure;
      }
      return result;
    } catch (error) {
      const safe = error instanceof TypeError ? new Error('Backend connection unavailable.') : error;
      showRequestError(safe);
      throw safe;
    }
  };
  panelReload?.addEventListener('click', async () => {
    panelReload.disabled = true;
    panelReload.textContent = 'Перезапуск…';
    try {
      const result = await requestJson('/api/v2/panel/restart', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      if (!result.restarting) throw new Error('Server validation failed.');
      window.setTimeout(() => {
        let attempts = 0;
        const poll = window.setInterval(async () => {
          try {
            await requestJson('/api/v2/bootstrap');
            window.clearInterval(poll);
            window.location.reload();
          } catch (_) {
            if (++attempts === 20) {
              window.clearInterval(poll);
              panelReload.disabled = false;
              panelReload.textContent = 'Перезапустить панель';
              panelReload.title = 'Панель не стала доступна после перезапуска.';
            }
          }
        }, 500);
      }, 500);
    } catch (_) {
      panelReload.disabled = false;
      panelReload.textContent = 'Перезапустить панель';
      panelReload.title = 'Перезапуск недоступен: завершите активные задачи панели.';
    }
  });

  function showScreen(id, moveFocus = false) {
    const active = screens.find((screen) => screen.id === id) || screens[0];
    screens.forEach((screen) => { screen.hidden = screen !== active; });
    links.forEach((link) => {
      if (link.dataset.screenLink === active.id) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    const title = active.querySelector('h1');
    if (breadcrumb && title) breadcrumb.textContent = title.textContent;
    if (moveFocus && title) {
      title.tabIndex = -1;
      title.focus({ preventScroll: true });
    }
  }

  function routeFromHash() {
    const id = window.location.hash.slice(1) || 'strategies-dd5';   // retired screens fall back to the first one
    showScreen(id);
    if (id === 'portfolio') loadPortfolioScreen();
    if (id === 'settings') { loadPortfolioSettings(); loadPerformanceDatabases(); }
  }

  links.forEach((link) => link.addEventListener('click', () => showScreen(link.dataset.screenLink, true)));
  document.querySelectorAll('[data-pending]').forEach((button) => {
    button.addEventListener('click', () => {
      const message = button.dataset.pending;
      if (status) status.textContent = message;
      const localStatus = button.closest('.card-body, .panel-card')?.querySelector('.card-status');
      if (localStatus) localStatus.textContent = message;
    });
  });
  window.addEventListener('hashchange', routeFromHash);
  routeFromHash();

  async function loadSafeDefaults() {
    try {
      const bootstrap = await requestJson('/api/v2/bootstrap');
      for (const [name, value] of Object.entries(bootstrap.defaults?.panel?.path_defaults || {})) {
        const input = document.querySelector(`[name="${name}"]`);
        if (input && typeof value === 'string') input.value = value;
      }
      const paths = bootstrap.defaults?.panel?.path_defaults || {};
      const operational = bootstrap.defaults?.operational || {};
      const workers = document.querySelector('#settings-import-workers');
      if (workers && operational.import_workers !== undefined) workers.value = operational.import_workers;
      for (const [id, key] of [
        ['settings-source-root', 'source_db_path'], ['settings-output-root', 'output_root'],
      ]) {
        const input = document.querySelector(`#${id}`);
        if (input && operational[key] !== undefined) input.value = operational[key];
      }
      for (const [id, key] of [
        ['settings-local-runner', 'local_runner_root'],
        ['settings-source-root', 'local_source_db_root'], ['settings-output-root', 'local_output_root'],
      ]) {
        const input = document.querySelector(`#${id}`);
        if (input && paths[key]) input.value = paths[key];
      }
      const connection = document.querySelector('.connection-status');
      if (connection) connection.lastChild.textContent = 'LOCAL BACKEND CONNECTED';
    } catch (_) {
      if (status) status.textContent = 'Backend connection is unavailable.';
    }
  }

  async function remoteRequest(endpoint, body = {}) {
    return requestJson(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
  }

  function formatDuration(value) {
    const seconds = Math.max(0, Math.floor(Number(value) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return hours ? `${hours}\u0447 ${minutes}\u043c` : `${minutes}\u043c ${seconds % 60}\u0441`;
  }

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0);
    if (bytes < 1024 ** 2) return `${Math.floor(bytes / 1024)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  }

  const strategyStack = document.querySelector('#strategies-dd5 .stack');
  const v2CardOrder = ['performance-v2-finalist-retest-card', 'performance-v2-retest-card'];
  const v2Cards = v2CardOrder
    .map((id) => document.getElementById(id))
    .filter((card) => card?.closest('#strategies-dd5'));
  if (!strategyStack || v2Cards.length !== v2CardOrder.length) {
    console.error('Performance v2 cards are not in the expected Strategies and DD5 layout.');
  } else {
    v2Cards.forEach((card) => strategyStack.append(card));
  }
  // The tester card by id: the main-screen section and dialogs share this screen since Panel rework stage 3.
  const strategyCards = [document.querySelector('#tester-card')].filter(Boolean);
  ['3. Test and Import to Performance DB'].forEach((label, index) => {
    const heading = strategyCards[index]?.querySelector('summary b');
    if (heading) heading.textContent = label;
  });
  let generatedBatchAnalysisId = '';
  let testerControlsBusy = false;
  let testerJobId = '';
  let testerPoller = 0;
  let testerRetryTimer = 0;
  let testerRetryable = false;
  let testerCommitted = false;
  let testerVerifiable = false;
  let normalInboxReady = false;
  let normalImportAuthorized = false;
  let authorizedTesterJobId = '';
  let normalVerifyEpoch = 0;
  let normalVerifyInFlight = false;
  let normalImportInFlight = false;
  const setTesterReadyCount = (count) => {
    const badge = strategyCards[0]?.querySelector('summary .state-badge');
    if (!badge) return;
    badge.className = `state-badge ${count ? 'state-ready' : 'state-pending'}`;
    badge.textContent = count ? `${count} READY` : 'WAITING';
  };
  const generateStatus = (message) => {
    const node = document.querySelector('#tester-status');
    if (node) node.textContent = message;
  };
  let storeReadyBatchChosen = false;   // a batch prepared on this page wins over the restored one
  const restoreGeneratedBatch = async () => {
    try {
      const batch = await requestJson('/api/v2/strategies/fresh/batch');
      if (storeReadyBatchChosen) return;
      if (!Number.isSafeInteger(batch.strategy_count) || batch.strategy_count < 0) {
        console.warn('READY JSON batch metadata has an invalid strategy count.');
        return;
      }
      if (typeof batch.analysis_run_id !== 'string' || !/^[0-9a-f]{64}$/.test(batch.analysis_run_id)) {
        console.warn('READY JSON batch metadata has an invalid analysis identity.');
        return;
      }
      const count = batch.strategy_count;
      generatedBatchAnalysisId = batch.analysis_run_id;
      setTesterReadyCount(count);
      generateStatus(`READY JSON restored: ${count}.`);
    } catch (error) {
      if (error?.message !== 'fresh strategy batch is not available') console.warn('READY JSON batch restore failed.', error);
    }
  };
  restoreGeneratedBatch();
  // Stage 1b (docs/specs/2026-10-18-panel-simulation-workflow.md): a trade-store READY folder becomes the batch
  // that the unchanged SINGLE_MODE tester starts.
  const storeReadySelect = document.querySelector('#store-ready-folder');
  const storeReadyStatus = (message) => {
    const node = document.querySelector('#store-ready-status');
    if (node) node.textContent = message;
  };
  const storeReadyOption = (value, text, disabled = false) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = text;
    option.disabled = disabled;
    return option;
  };
  const loadStoreReadyFolders = async () => {
    if (!storeReadySelect) return;
    try {
      const catalog = await requestJson('/api/v2/strategies/store-ready/catalog');
      const folders = Array.isArray(catalog.folders) ? catalog.folders : [];
      storeReadySelect.replaceChildren(...(folders.length
        ? folders.map((folder) => storeReadyOption(
          String(folder.name),
          folder.testable === true
            ? `${folder.name} · пар READY ${folder.ready_pairs} · стратегий ${folder.strategies}`
            : `${folder.name} · ${folder.reason || 'нельзя тестировать'}`,
          folder.testable !== true,
        ))
        : [storeReadyOption('', 'Папок READY нет')]));
      const first = folders.find((folder) => folder.testable === true);
      if (first) storeReadySelect.value = String(first.name);
      storeReadyStatus(folders.length ? '' : 'Соберите READY в карточке MRS2 STORE.');
    } catch (error) {
      storeReadyStatus(`Список READY недоступен: ${error?.message || 'ошибка запроса'}.`);
    }
  };
  document.querySelector('#store-ready-refresh')?.addEventListener('click', loadStoreReadyFolders);
  const storeReadyPrepare = document.querySelector('#store-ready-prepare');
  storeReadyPrepare?.addEventListener('click', async () => {
    const ready = storeReadySelect?.value || '';
    if (!ready) { storeReadyStatus('Выберите папку READY.'); return; }
    storeReadyStatus('Подготовка…');
    storeReadyPrepare.disabled = true;
    try {
      const result = await remoteRequest('/api/v2/strategies/store-ready/prepare', { ready });
      if (typeof result.analysis_run_id !== 'string' || !/^[0-9a-f]{64}$/.test(result.analysis_run_id)) {
        throw new Error('нет идентификатора пакета');
      }
      storeReadyBatchChosen = true;
      generatedBatchAnalysisId = result.analysis_run_id;
      setTesterReadyCount(result.strategy_count);
      storeReadyStatus(`Готово к тесту: стратегий ${result.strategy_count}, пар ${result.ready_pairs}`
        + `${result.reused ? ' (пакет уже был подготовлен)' : ''}.`);
    } catch (error) {
      storeReadyStatus(`Подготовка не удалась: ${error?.message || 'ошибка запроса'}.`);
    } finally {
      storeReadyPrepare.disabled = false;
    }
  });
  loadStoreReadyFolders();
  // Protocol v2 §4.3: MRS3 tests run on the base of the MRS3 tester template; there is no start-balance field.
  const testerCard = strategyCards[0];
  const testerText = testerCard?.querySelector('.progress-block p');
  const testerStatus = document.querySelector('#tester-status');
  const testerTrack = testerCard?.querySelector('.progress-track span');
  const testerStart = document.querySelector('#tester-start');
  const testerStop = document.querySelector('#tester-stop');
  const testerRetry = document.querySelector('#tester-retry');
  const testerStartDate = document.querySelector('#tester-start-date');
  const testerEndDate = document.querySelector('#tester-end-date');
  const testerCollectReports = document.querySelector('#tester-collect-reports');
  if (testerCollectReports) testerCollectReports.checked = false;
  const testerCollectionStatus = document.querySelector('#tester-collection-status');
  const testerCollectionClear = document.querySelector('#tester-collection-clear');
  let testerCollectionId = '';
  let testerCollectionState = 'EMPTY';
  let collectionCommittedPacks = 0;
  let collectionActivePacks = 0;
  let collectionFailedPacks = 0;
  let collectionReportCount = 0;
  let collectionActionBusy = false;
  let collectionImportJobId = '';
  const validIsoDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
  const testerMaxDate = () => { const value = new Date(); value.setHours(0, 0, 0, 0); value.setDate(value.getDate() - 1); return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`; };
  if (testerEndDate) testerEndDate.max = testerMaxDate();
  const shiftDateMonths = (value, months) => {
    if (!validIsoDate(value)) return '';
    const [year, month, day] = value.split('-').map(Number);
    const target = new Date(Date.UTC(year, month - 1 + months, 1));
    const lastDay = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
    target.setUTCDate(Math.min(day, lastDay));
    return target.toISOString().slice(0, 10);
  };
  [1, 2, 3].forEach((months) => document.querySelector(`#tester-range-${months}m`)?.addEventListener('click', () => {
    const maxDate = testerMaxDate();
    if (testerEndDate) testerEndDate.max = maxDate;
    const anchor = testerEndDate?.value && testerEndDate.value <= maxDate ? testerEndDate.value : maxDate;
    if (testerEndDate) testerEndDate.value = anchor;
    if (testerStartDate) testerStartDate.value = shiftDateMonths(anchor, -months);
  }));
  const testerIsTerminal = (job) => ['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state);
  const collectionIsActive = () => Boolean(testerCollectionId) && ['OPEN', 'VERIFIED'].includes(testerCollectionState);
  const collectionModeSelected = () => testerCollectReports?.checked === true && collectionIsActive();
  const collectionImportReady = () => collectionModeSelected() && testerCollectionState === 'VERIFIED' && collectionCommittedPacks > 0;
  const refreshCollectionActionControls = () => {
    if (testerStart) testerStart.disabled = collectionActionBusy || testerControlsBusy;
    if (testerRetry) testerRetry.disabled = collectionActionBusy || testerControlsBusy || !testerRetryable;
    if (testerCollectionClear) testerCollectionClear.disabled = collectionActionBusy || !collectionIsActive();
    if (inboxVerifyV2) {
      if (collectionActionBusy) inboxVerifyV2.disabled = true;
      else {
        const ordinaryReady = testerCommitted || testerVerifiable;
        const collectionReady = collectionModeSelected() && collectionCommittedPacks > 0;
        inboxVerifyV2.disabled = normalVerifyInFlight || Boolean(normalImportAuthorized && authorizedTesterJobId)
          || (collectionModeSelected() ? collectionActivePacks > 0 || !collectionReady : !ordinaryReady);
      }
    }
    if (importStartV2) importStartV2.disabled = collectionActionBusy
      || !(normalImportAuthorized && authorizedTesterJobId && (collectionImportReady() || normalInboxReady));
  };
  const setCollectionActionBusy = (busy) => {
    collectionActionBusy = busy;
    refreshCollectionActionControls();
  };
  testerCollectReports?.addEventListener('change', () => {
    normalImportAuthorized = false;
    authorizedTesterJobId = '';
    normalVerifyEpoch += 1;
    refreshCollectionActionControls();
  });
  const renderTesterCollection = (collection = {}) => {
    const state = typeof collection.state === 'string' ? collection.state : 'EMPTY';
    const id = typeof collection.collection_id === 'string' ? collection.collection_id : '';
    testerCollectionState = state;
    testerCollectionId = ['OPEN', 'VERIFIED'].includes(state) ? id : '';
    collectionCommittedPacks = Number.isSafeInteger(Number(collection.committed_packs)) ? Number(collection.committed_packs) : 0;
    collectionActivePacks = Number.isSafeInteger(Number(collection.active_packs)) ? Number(collection.active_packs) : 0;
    collectionFailedPacks = Number.isSafeInteger(Number(collection.failed_cancelled_packs)) ? Number(collection.failed_cancelled_packs) : 0;
    collectionReportCount = Number.isSafeInteger(Number(collection.exact_committed_report_count)) ? Number(collection.exact_committed_report_count) : 0;
    if (testerCollectionStatus) {
      const details = [];
      if (collectionActivePacks > 0) details.push(`активных: ${collectionActivePacks}`);
      if (collectionFailedPacks > 0) details.push(`исключено неудачных: ${collectionFailedPacks}`);
      if (state === 'VERIFIED') details.push('Проверено · импорт разрешён после проверки');
      if (state === 'IMPORTED') details.push('Импортировано · накопление закрыто');
      testerCollectionStatus.textContent = `Накоплено: ${Number(collection.total_registered_packs) || 0} пачек · ${collectionReportCount} отчётов${details.length ? ` · ${details.join(' · ')}` : ''}`;
    }
    refreshCollectionActionControls();
  };
  const collectionCoordinator = testerCollectionUiHelpers.create({
    request: (endpoint) => requestJson(endpoint),
    apply: renderTesterCollection,
    onBusyChange: setCollectionActionBusy,
  });
  const loadTesterCollection = async () => {
    try {
      return await collectionCoordinator.load();
    } catch (error) {
      if (testerCollectionStatus) testerCollectionStatus.textContent = `Накопление недоступно: ${error?.message || 'request failed'}.`;
      return false;
    }
  };
  const setTesterControls = (busy) => {
    if (testerStart) testerStart.disabled = busy || collectionActionBusy;
    if (testerStop) testerStop.disabled = busy ? false : true;
    if (testerRetry) testerRetry.disabled = busy || collectionActionBusy || !testerRetryable;
    testerControlsBusy = busy;
    refreshCollectionActionControls();
  };
  const renderTester = (job) => {
    const p = job.progress || {};
    const total = Number(p.total || job.strategy_count || 0);
    const singleMode = job.mode === 'SINGLE_MODE' || job.kind === 'strategies.tester.native.start' || job.kind === 'strategies.tester.start' || job.kind === 'strategies.tester.retry';
    const runs = false;
    const checked = Number(p.current || p.checked || 0);
    if (testerTrack) testerTrack.style.width = total ? `${Math.min(100, Math.round(checked * 100 / total))}%` : (job.state === 'COMMITTED' ? '100%' : '0%');
    const failed = Array.isArray(job.evidence?.failed_names) ? job.evidence.failed_names.length : 0;
    const progressTail = ['FAILED', 'CANCELLED'].includes(job.state) ? `failed ${failed}` : `remaining ${Math.max(0, total - checked)}`;
    const stage = p.stage || job.phase || job.state || 'RUNNING';
    const startup = stage === 'BOT_START' && Number.isFinite(Number(p.startup_elapsed_seconds))
      ? ` · startup ${Number(p.startup_elapsed_seconds).toFixed(1)}s`
      : '';
    const native = stage === 'BOT_RUN' && p.native_status ? ` · tester ${p.native_status}` : '';
    const detail = singleMode
      ? `${stage}${startup}${native} · batch ${p.batch_number || 0}/${p.batch_total || 0} · reports ${checked}/${total} · retries ${p.retries || 0} · ${progressTail}`
      : runs
        ? `${stage} · reports ${checked}/${total}`
        : `${stage} · reports ${checked}/${total}`;
    const committed = singleMode && job.state === 'COMMITTED';
    const verifiable = committed || (singleMode && job.state === 'RUNNING' && stage === 'COMMITTED'
      && total > 0 && checked === total && failed === 0);
    const ready = committed && job.inbox_ready === true;
    normalInboxReady = ready;
    if (testerIsTerminal(job) && !committed && job.job_id === testerJobId) {
      normalImportAuthorized = false;
      authorizedTesterJobId = '';
    }
    const importAllowed = normalImportAuthorized && authorizedTesterJobId === (collectionModeSelected() ? testerCollectionId : testerJobId)
      && (collectionImportReady() || ready);
    testerCommitted = committed;
    testerVerifiable = verifiable;
    if (testerText) testerText.textContent = detail;
    if (testerStatus) {
      const error = job.error?.code ? ` ${job.error.code}: ${job.error.message || ''}` : '';
      const gate = importAllowed ? 'CHECKED' : 'CHECK REQUIRED';
      testerStatus.textContent = `${singleMode ? 'SINGLE_MODE' : 'Tester'}: ${detail} · ${gate}.${error}${testerIsTerminal(job) ? '' : ' · подготовка нового пакета READY заблокирована: запись задачи tester в панели ещё активна.'}`;
    }
    if (testerStop) testerStop.disabled = !testerJobId || testerIsTerminal(job);
    testerRetryable = singleMode && ['FAILED', 'CANCELLED'].includes(job.state) && job.job_id === testerJobId;
    setTesterControls(!testerIsTerminal(job));
    if (inboxVerifyV2) {
      const collectionReady = collectionModeSelected() && collectionCommittedPacks > 0;
      if (collectionActionBusy) inboxVerifyV2.disabled = true;
      else if (!collectionModeSelected()) inboxVerifyV2.disabled = !verifiable || importAllowed;
      else inboxVerifyV2.disabled = importAllowed || normalVerifyInFlight || collectionActivePacks > 0 || !collectionReady;
    }
    if (importStartV2) importStartV2.disabled = collectionActionBusy || !importAllowed;
    if (importStatusV2 && !importJobV2) importStatusV2.textContent += ' · ' + (ready
      ? `Performance v2: ${importAllowed ? 'CHECKED · import enabled.' : 'CHECK REQUIRED · press Проверить to verify the committed inbox.'}`
      : verifiable
        ? 'Performance v2: reports complete · press Проверить to recover and verify the inbox.'
        : 'Performance v2: waiting for completed tester reports.');
    const badge = testerCard?.querySelector('summary .state-badge');
    if (badge) {
      badge.className = `state-badge ${importAllowed ? 'state-ready' : 'state-pending'}`;
      badge.textContent = importAllowed ? 'CHECKED' : (verifiable ? 'CHECK REQUIRED' : 'WAITING');
    }
  };
  const pollTester = async () => {
    if (!testerJobId) return;
    try {
      const job = await requestJson('/api/v2/strategies/tester/status?job_id=' + encodeURIComponent(testerJobId));
      renderTester(job);
      if (testerIsTerminal(job)) {
        window.clearInterval(testerPoller); testerPoller = 0;
        await loadTesterCollection();
      }
    } catch (error) {
      if (testerStatus) testerStatus.textContent = `Tester status unavailable: ${error?.message || 'request failed'}.`;
    }
  };
  const startTesterPolling = (interval) => {
    window.clearInterval(testerPoller);
    testerPoller = window.setInterval(pollTester, interval);
  };
  if (testerStart) testerStart.addEventListener('click', async () => {
    if (collectionActionBusy) return;
    if (!generatedBatchAnalysisId) {
      if (testerStatus) testerStatus.textContent = 'Сначала подготовьте пакет: выберите папку READY и нажмите «Подготовить».';
      return;
    }
    const startDate = testerStartDate?.value || '';
    const endDate = testerEndDate?.value || '';
    if (!validIsoDate(startDate) || !validIsoDate(endDate)) { if (testerStatus) testerStatus.textContent = 'Enter valid tester start and end dates.'; return; }
    const maxDate = testerMaxDate();
    if (testerEndDate) testerEndDate.max = maxDate;
    if (endDate > maxDate) { if (testerStatus) testerStatus.textContent = 'Tester end date must not be later than yesterday.'; return; }
    if (startDate > endDate) { if (testerStatus) testerStatus.textContent = 'Tester start date must not be after end date.'; return; }
    normalImportAuthorized = false;
    authorizedTesterJobId = '';
    normalVerifyEpoch += 1;
    testerCommitted = false;
    testerVerifiable = false;
    const collectReports = testerCollectReports?.checked === true;
    const collectionActionLease = collectReports ? collectionCoordinator.begin('start') : null;
    if (collectReports && !collectionActionLease) return;
    setTesterControls(true);
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.tester.start', request: { analysis_run_id: generatedBatchAnalysisId, start_date: startDate, end_date: endDate, collect_reports: collectReports } });
      testerJobId = result.job?.job_id || '';
      if (!testerJobId) throw new Error('missing job');
      renderTester(result.job); await loadTesterCollection(); await pollTester(); startTesterPolling(1000);
    } catch (error) {
      if (testerStatus) testerStatus.textContent = `SINGLE_MODE tester failed to start: ${error?.message || 'request failed'}.`;
      setTesterControls(false);
    } finally {
      if (collectionActionLease) {
        try { await collectionCoordinator.refresh(); } finally { collectionCoordinator.finish(collectionActionLease); }
      }
    }
  });
  testerRetry?.addEventListener('click', async () => {
    if (collectionActionBusy || !testerRetryable || !testerJobId) return;
    const sourceJobId = testerJobId;
    const collectionActionLease = collectionIsActive() ? collectionCoordinator.begin('retry') : null;
    if (collectionIsActive() && !collectionActionLease) return;
    testerRetryable = false;
    window.clearInterval(testerPoller); testerPoller = 0;
    setTesterControls(true);
    if (testerStop) testerStop.disabled = true;
    const startedAt = Date.now();
    const showRecovery = () => {
      if (testerStatus) testerStatus.textContent = `Восстановление отчётов · проверяю уже готовые HTML · ${Math.floor((Date.now() - startedAt) / 1000)} с`;
    };
    showRecovery();
    testerRetryTimer = window.setInterval(showRecovery, 1000);
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.tester.retry', request: { job_id: sourceJobId } });
      const retryJobId = result.job?.job_id || '';
      if (!retryJobId) throw new Error('missing job');
      testerJobId = retryJobId;
      renderTester(result.job); await loadTesterCollection(); await pollTester(); startTesterPolling(1000);
    } catch (error) {
      testerRetryable = testerJobId === sourceJobId;
      if (testerStatus) testerStatus.textContent = `Восстановление отчётов не выполнено: ${error?.message || 'request failed'}.`;
      setTesterControls(false);
    } finally {
      window.clearInterval(testerRetryTimer); testerRetryTimer = 0;
      if (collectionActionLease) {
        try { await collectionCoordinator.refresh(); } finally { collectionCoordinator.finish(collectionActionLease); }
      }
    }
  });
  if (testerStop) testerStop.addEventListener('click', async () => {
    if (!testerJobId) return;
    normalImportAuthorized = false;
    authorizedTesterJobId = '';
    normalVerifyEpoch += 1;
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.tester.cancel', request: { job_id: testerJobId } });
      renderTester(result.job || {});
      if (testerIsTerminal(result.job || {})) await loadTesterCollection();
    } catch (error) {
      if (testerStatus) testerStatus.textContent = `Tester stop failed: ${error?.message || 'request failed'}.`;
    }
  });
  const recoverJobs = async () => {
    try {
      const snapshot = await requestJson('/api/v2/jobs');
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const testerJobs = jobs
        .filter((job) => ['strategies.tester.start', 'strategies.tester.retry'].includes(job.kind) && job.retest !== true)
        .filter((job) => Number.isFinite(Date.parse(job.created_at_utc)) && (
          ['QUEUED', 'RUNNING', 'CANCELLING'].includes(job.state)
          || ['FAILED', 'CANCELLED'].includes(job.state)
          || job.state === 'COMMITTED'
        ))
        .sort((a, b) => Date.parse(b.created_at_utc) - Date.parse(a.created_at_utc)
          || String(b.job_id).localeCompare(String(a.job_id)));
      const tester = testerJobs[0];
      if (tester && typeof tester.job_id === 'string') {
        const job = tester;
        testerJobId = job.job_id;
        normalImportAuthorized = false;
        authorizedTesterJobId = '';
        renderTester(job);
        if (!testerIsTerminal(job)) { await pollTester(); startTesterPolling(1000); }
      }
    } catch (_) { /* requestJson already exposed a safe visible error. */ }
  };
  const importStartV2 = document.querySelector('#performance-import-start');
  const inboxVerifyV2 = document.querySelector('#performance-inbox-verify');
  const importStatusV2 = testerStatus;
  const importProgressV2 = testerText;
  const importFailureV2 = document.createElement('a');
  importFailureV2.hidden = true;
  importFailureV2.textContent = 'Open failure report';
  importStatusV2?.after(importFailureV2);
  let importJobV2 = '';
  let collectionImportActionLease = null;
  const finishCollectionImportAction = async () => {
    const lease = collectionImportActionLease;
    if (!lease) return;
    collectionImportActionLease = null;
    try { await collectionCoordinator.refresh(); } catch (_) { /* status remains visible */ }
    finally { collectionCoordinator.finish(lease); }
  };
  if (inboxVerifyV2) inboxVerifyV2.disabled = true;
  if (importStartV2) importStartV2.disabled = true;
  testerCollectionClear?.addEventListener('click', async () => {
    const collectionId = testerCollectionId;
    if (!collectionId || collectionActionBusy) return;
    const collectionActionLease = collectionCoordinator.begin('clear');
    if (!collectionActionLease) return;
    try {
      normalImportAuthorized = false;
      authorizedTesterJobId = '';
      normalVerifyEpoch += 1;
      await requestJson('/api/v2/strategies/tester/report-collection/clear', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ collection_id: collectionId }),
      });
      testerCollectionId = '';
      testerCollectionState = 'EMPTY';
      collectionCommittedPacks = 0;
      collectionActivePacks = 0;
      collectionFailedPacks = 0;
      collectionReportCount = 0;
      if (testerCollectReports) testerCollectReports.checked = false;
      if (testerStatus) testerStatus.textContent = 'Накопление очищено; файлы отчётов не удалялись.';
    } catch (error) {
      if (testerCollectionStatus) testerCollectionStatus.textContent = `Очистка накопления не выполнена: ${error?.message || 'request failed'}.`;
    } finally {
      try { await collectionCoordinator.refresh(); } catch (_) { /* status remains visible */ }
      finally { collectionCoordinator.finish(collectionActionLease); }
    }
  });
  const renderImportV2 = (job) => {
    const p = job.progress || {};
    const total = Number(p.total || 0);
    const current = Number(p.current || 0);
    const track = testerTrack;
    if (track) {
      const done = job.state === 'COMMITTED';
      track.classList.toggle('is-running', !total && !done && !['FAILED', 'CANCELLED'].includes(job.state));
      track.style.width = done ? '100%' : (total ? `${Math.max(0, Math.min(100, current * 100 / total))}%` : '0%');
    }
    const failed = Array.isArray(job.evidence?.failed_names) ? job.evidence.failed_names.length : Number(p.failed || 0);
    const detail = `${job.phase || job.state || 'IMPORTING'} · ${current} / ${total} reports · batch ${p.batch_number || 0}/${p.batch_total || 0} · retries ${p.retries || 0} · failed ${failed}`;
    if (importProgressV2) importProgressV2.textContent = detail;
    const result = job.result || {};
    if (result.failure_report_available === true) {
      importFailureV2.hidden = false;
      importFailureV2.href = `/api/artifact?name=${encodeURIComponent(`performance-v2-failure-report:${importJobV2}`)}`;
    }
    const warning = result.cleanup_warning || job.cleanup_warning;
    const warningText = warning && typeof warning === 'object'
      ? formatErrorReason({ code: warning.code, message: warning.message }, 'cleanup failed')
      : formatErrorReason(warning, '');
    const error = job.error ? ` ${formatErrorReason(job.error)}` : '';
    const terminal = ['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state);
    if (terminal && job.job_id === importJobV2) {
      const importedCollectionId = collectionImportJobId;
      normalImportInFlight = false;
      normalImportAuthorized = false;
      authorizedTesterJobId = '';
      if (inboxVerifyV2) inboxVerifyV2.disabled = collectionModeSelected()
        ? collectionActivePacks > 0 || collectionCommittedPacks <= 0
        : !testerCommitted;
      if (importStartV2) importStartV2.disabled = true;
      if (job.state === 'COMMITTED' && importedCollectionId) {
        if (importStatusV2) importStatusV2.textContent = 'Накопление импортировано; накопление закрыто.';
        void finishCollectionImportAction();
      } else if (importedCollectionId) void finishCollectionImportAction();
    }
    if (importStatusV2) importStatusV2.textContent = job.state === 'COMMITTED' && collectionImportJobId
      ? 'Накопление импортировано; накопление закрыто.'
      : collectionImportJobId && terminal
        ? `Импорт накопления не выполнен; доступен повтор проверки/повтор${error}.`
        : job.state === 'COMMITTED'
          ? `Performance v2: ${detail} · COMMITTED · imported ${result.imported_count || 0} · skipped ${result.skipped_count || 0} · rejected ${result.rejected_count || 0} · target ${result.database_path || '—'} · audit ${result.audit_path || '—'}.`
          : `Performance v2: ${detail}.${error}`;
    if (warningText && importStatusV2 && job.state === 'COMMITTED') importStatusV2.textContent += ` Cleanup warning: ${warningText}.`;
  };
  inboxVerifyV2?.addEventListener('click', async () => {
    if (collectionActionBusy) return;
    const collectionId = collectionModeSelected() ? testerCollectionId : '';
    const collectionReady = Boolean(collectionId) && collectionCommittedPacks > 0;
    if (!collectionId) {
      if (!testerJobId || !(testerCommitted || testerVerifiable) || normalVerifyInFlight) {
        if (importStatusV2) importStatusV2.textContent = 'CHECK REQUIRED: a committed tester job is required before Проверить.';
        return;
      }
    } else if (normalVerifyInFlight || collectionActivePacks > 0 || !collectionReady) {
      if (importStatusV2) importStatusV2.textContent = collectionId && collectionActivePacks > 0
        ? `Проверка накопления ожидает завершения активных пачек: ${collectionActivePacks}.`
        : collectionId && collectionFailedPacks > 0
          ? `Проверка накопления исключает неудачные пачки: ${collectionFailedPacks}.`
          : 'CHECK REQUIRED: a committed tester job is required before Проверить.';
      return;
    }
    const collectionActionLease = collectionId ? collectionCoordinator.begin('verify') : null;
    if (collectionId && !collectionActionLease) return;
    inboxVerifyV2.disabled = true;
    normalImportAuthorized = false;
    authorizedTesterJobId = '';
    // Ordinary fallback remains: const verifyJobId = testerJobId;
    const verifyJobId = collectionId || testerJobId;
    const verifyEpoch = ++normalVerifyEpoch;
    normalVerifyInFlight = true;
    if (importStatusV2) importStatusV2.textContent = 'Проверка verified inbox…';
    try {
      const verified = await requestJson('/api/v2/strategies/tester/verify-inbox', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_id: verifyJobId }) });
      if (collectionId) {
        if (verified?.state !== 'VERIFIED' || verified?.collection_id !== collectionId || verified?.importable_collection_job_id !== collectionId) throw new Error('verified collection is not ready');
      } else if (verified?.state !== 'COMMITTED' || verified?.inbox_ready !== true) throw new Error('verified inbox is not ready');
      // Ordinary stale-check remains: verifyEpoch !== normalVerifyEpoch || verifyJobId !== testerJobId || !testerCommitted
      if (verifyEpoch !== normalVerifyEpoch || verifyJobId !== (collectionModeSelected() ? testerCollectionId : testerJobId)
        || (collectionModeSelected() ? !collectionReady : !(testerCommitted || testerVerifiable))
        || (collectionActionLease && !collectionCoordinator.isCurrent(collectionActionLease))) return;
      const authorize = () => {
        normalImportAuthorized = true;
        authorizedTesterJobId = verifyJobId;
      if (collectionId) {
        renderTesterCollection(verified);
        if (importStatusV2) importStatusV2.textContent = 'CHECKED: накопление verified; import is enabled.';
      } else {
        renderTester(verified);
        if (importStatusV2) importStatusV2.textContent = 'CHECKED: committed inbox verified; import is enabled.';
      }
      };
      if (collectionActionLease) {
        if (!collectionCoordinator.commit(collectionActionLease, authorize)) return;
      } else authorize();
    } catch (error) {
      if (importStatusV2) importStatusV2.textContent = `Проверка не выполнена: ${error?.message || 'unknown error'}.`;
    }
    finally {
      normalVerifyInFlight = false;
      if (collectionActionLease) {
        try { await collectionCoordinator.refresh(); } catch (_) { /* status remains visible */ }
        finally { collectionCoordinator.finish(collectionActionLease); }
      } else refreshCollectionActionControls();
    }
  });
  importStartV2?.addEventListener('click', async () => {
    if (collectionActionBusy) return;
    const importTargetJobId = authorizedTesterJobId;
    const collectionImport = collectionIsActive() && importTargetJobId === testerCollectionId;
    const normalImportGateInvalid = !normalImportAuthorized || authorizedTesterJobId !== testerJobId;
    if (!importTargetJobId || (!collectionImport && (!testerJobId || !testerCommitted || !normalInboxReady))
      || (collectionImport && !collectionImportReady()) || normalImportInFlight
      || (!collectionImport && normalImportGateInvalid)) {
      if (importStatusV2) importStatusV2.textContent = 'CHECK REQUIRED: verify the committed tester inbox before import.';
      return;
    }
    const collectionActionLease = collectionImport ? collectionCoordinator.begin('import') : null;
    if (collectionImport && !collectionActionLease) return;
    if (collectionActionLease) collectionImportActionLease = collectionActionLease;
    importStartV2.disabled = true;
    importFailureV2.hidden = true;
    importFailureV2.removeAttribute('href');
    normalImportInFlight = true;
    collectionImportJobId = collectionImport ? importTargetJobId : '';
    window.clearInterval(testerPoller); testerPoller = 0;
    if (importStatusV2) importStatusV2.textContent = 'Импорт Performance v2…';
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.performance.v2.import', request: { tester_job_id: importTargetJobId } });
      importJobV2 = result.job?.job_id || '';
      if (!importJobV2) throw new Error('missing job');
      const poll = async () => {
        const job = await requestJson(`/api/v2/strategies/performance-v2/import/status?job_id=${encodeURIComponent(importJobV2)}`);
        renderImportV2(job);
        if (['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state)) {
          return true;
        }
        return false;
      };
      if (!(await poll())) { const timer = window.setInterval(async () => { if (await poll()) window.clearInterval(timer); }, 1000); }
    } catch (error) {
      normalImportInFlight = false;
      const reason = formatErrorReason({ code: error?.code, message: error?.message });
      if (importStatusV2) importStatusV2.textContent = collectionImportJobId
        ? `Импорт накопления не выполнен; доступен повтор проверки/повтор: ${reason}.`
        : `Импорт Performance v2 не прошёл проверку: ${reason}.`;
    }
    finally {
      if (collectionActionLease && !collectionImportActionLease) await finishCollectionImportAction();
      refreshCollectionActionControls();
    }
  });
  const recoverSplitJobs = async () => {
    try {
      const snapshot = await requestJson('/api/v2/jobs');
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const tester = jobs
        .filter((job) => ['strategies.tester.start', 'strategies.tester.retry'].includes(job.kind) && job.retest !== true)
        .filter((job) => Number.isFinite(Date.parse(job.created_at_utc)) && (
          ['QUEUED', 'RUNNING', 'CANCELLING'].includes(job.state)
          || (job.state === 'COMMITTED' && job.inbox_ready === true)
        ))
        .sort((a, b) => Date.parse(b.created_at_utc) - Date.parse(a.created_at_utc)
          || String(b.job_id).localeCompare(String(a.job_id)))[0];
      const testerCreatedAt = Date.parse(tester?.created_at_utc);
      const importJob = jobs
        .filter((job) => job.kind === 'strategies.performance.v2.import' && job.retest !== true)
        .filter((job) => Number.isFinite(Date.parse(job.created_at_utc)) && job.state !== 'CANCELLED')
        .filter((job) => !Number.isFinite(testerCreatedAt) || Date.parse(job.created_at_utc) >= testerCreatedAt)
        .sort((a, b) => Date.parse(b.created_at_utc) - Date.parse(a.created_at_utc)
          || String(b.job_id).localeCompare(String(a.job_id)))[0];
      if (importJob?.job_id) {
        importJobV2 = importJob.job_id;
        const poll = async () => {
          const job = await requestJson(`/api/v2/strategies/performance-v2/import/status?job_id=${encodeURIComponent(importJobV2)}`);
          renderImportV2(job);
          return ['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state);
        };
        if (!(await poll())) { const timer = window.setInterval(async () => { if (await poll()) window.clearInterval(timer); }, 1000); }
      }
    } catch (_) { /* stale jobs remain visible through the server registry */ }
  };

  const retestCard = document.querySelector('#performance-v2-retest-card');
  const retestCount = document.querySelector('#performance-v2-retest-count');
  const retestStartDate = document.querySelector('#performance-v2-retest-start');
  const retestEndDate = document.querySelector('#performance-v2-retest-end');
  const retestStart = document.querySelector('#performance-v2-retest-check');
  const retestImport = document.querySelector('#performance-v2-retest-import');
  const retestProgress = document.querySelector('#performance-v2-retest-progress');
  const retestProgressText = document.querySelector('#performance-v2-retest-progress-text');
  const retestStatus = document.querySelector('#performance-v2-retest-status');
  const retestInbox = document.querySelector('#performance-v2-retest-inbox');
  const retestFailure = document.querySelector('#performance-v2-retest-failure-report');
  const retestBadge = document.querySelector('#performance-v2-retest-badge');
  if (retestEndDate) retestEndDate.max = testerMaxDate();
  let retestTesterJobId = '';
  let retestImportJobId = '';
  let retestInboxReady = false;
  let retestTesterTimer = 0;
  let retestImportTimer = 0;
  const retestTerminal = (job) => ['COMMITTED', 'CANCELLED', 'FAILED'].includes(job?.state);
  const renderRetestTester = (job = {}) => {
    const p = job.progress || {};
    const current = Number(p.current || 0);
    const total = Number(p.total || 0);
    const failed = Array.isArray(job.evidence?.failed_names) ? job.evidence.failed_names.length : Number(p.failed || 0);
    retestInboxReady = job.state === 'COMMITTED' && job.inbox_ready === true;
    if (retestProgress) retestProgress.value = total ? Math.min(100, current * 100 / total) : (retestInboxReady ? 100 : 0);
    const startup = job.phase === 'BOT_START' && Number.isFinite(Number(p.startup_elapsed_seconds))
      ? ` · startup ${Number(p.startup_elapsed_seconds).toFixed(1)}s`
      : '';
    const native = job.phase === 'BOT_RUN' && p.native_status ? ` · tester ${p.native_status}` : '';
    if (retestProgressText) retestProgressText.textContent = `${job.phase || job.state || 'RUNNING'}${startup}${native} · ${current}/${total} · batch ${p.batch_number || 0}/${p.batch_total || 0} · retries ${p.retries || 0} · failed ${failed}`;
    if (retestInbox) retestInbox.textContent = job.inbox_path ? `committed inbox: ${job.inbox_path}` : '';
    if (retestStatus) retestStatus.textContent = `CHECK & RETEST: ${job.phase || job.state || 'RUNNING'}${startup}${native} · ${current}/${total} · batch ${p.batch_number || 0}/${p.batch_total || 0} · retries ${p.retries || 0} · failed ${failed}${job.error ? ` · ${formatErrorReason(job.error)}` : ''}`;
    if (retestStart) retestStart.disabled = !retestTerminal(job);
    if (retestImport) retestImport.disabled = !retestInboxReady;
    if (retestBadge) { retestBadge.className = `state-badge state-${retestInboxReady ? 'ready' : 'pending'}`; retestBadge.textContent = retestInboxReady ? 'INBOX_READY' : (job.phase || 'WAITING'); }
  };
  const pollRetestTester = async () => {
    if (!retestTesterJobId) return true;
    try {
      const job = await requestJson(`/api/v2/strategies/tester/status?job_id=${encodeURIComponent(retestTesterJobId)}`);
      renderRetestTester(job);
      return retestTerminal(job);
    } catch (error) {
      normalImportInFlight = false;
      if (retestStatus) retestStatus.textContent = `RETEST status unavailable: ${error?.message || 'request failed'}.`;
      return false;
    }
  };
  const startRetestPolling = () => {
    window.clearInterval(retestTesterTimer);
    retestTesterTimer = window.setInterval(async () => { if (await pollRetestTester()) window.clearInterval(retestTesterTimer); }, 1000);
  };
  const pollRetestImport = async () => {
    if (!retestImportJobId) return true;
    try {
      const job = await requestJson(`/api/v2/strategies/performance-v2/import/status?job_id=${encodeURIComponent(retestImportJobId)}`);
      const p = job.progress || {}; const current = Number(p.current || 0); const total = Number(p.total || 0); const failed = Array.isArray(job.evidence?.failed_names) ? job.evidence.failed_names.length : Number(p.failed || 0);
      if (retestProgress) retestProgress.value = total ? Math.min(100, current * 100 / total) : (job.state === 'COMMITTED' ? 100 : 0);
      if (retestProgressText) retestProgressText.textContent = `${job.phase || job.state || 'IMPORTING'} · ${current}/${total} · batch ${p.batch_number || 0}/${p.batch_total || 0} · retries ${p.retries || 0} · failed ${failed}`;
      if (retestStatus) retestStatus.textContent = `IMPORT & REPLACE: ${job.phase || job.state || 'IMPORTING'}${job.error ? ` · ${formatErrorReason(job.error)}` : ''}`;
      const reportAvailable = job.result?.failure_report_available === true;
      if (retestFailure && reportAvailable) { retestFailure.hidden = false; retestFailure.href = `/api/artifact?name=${encodeURIComponent(`performance-v2-failure-report:${retestImportJobId}`)}`; retestFailure.textContent = 'Open failure report'; }
      if (job.state === 'COMMITTED') { await loadRetestStatus(); return true; }
      if (retestTerminal(job) && retestImport) retestImport.disabled = !retestInboxReady;
      return retestTerminal(job);
    } catch (error) {
      if (retestStatus) retestStatus.textContent = `RETEST import status unavailable: ${error?.message || 'request failed'}.`;
      return false;
    }
  };
  const startRetestImportPolling = () => {
    window.clearInterval(retestImportTimer);
    retestImportTimer = window.setInterval(async () => { if (await pollRetestImport()) window.clearInterval(retestImportTimer); }, 1000);
  };
  const loadRetestStatus = async () => {
    try {
      const result = await requestJson('/api/v2/strategies/performance-v2/retest/status');
      const count = Number(result.count ?? result.retest_count ?? result.active_count ?? 0);
      if (retestCount) retestCount.textContent = String(count);
      if (retestStartDate && !retestStartDate.value && result.default_start) retestStartDate.value = result.default_start;
      if (retestEndDate && !retestEndDate.value && result.default_end) retestEndDate.value = result.default_end;
      if (retestBadge && !retestTesterJobId) { retestBadge.textContent = count ? 'READY' : 'EMPTY'; retestBadge.className = `state-badge state-${count ? 'ready' : 'pending'}`; }
    } catch (error) { if (retestStatus) retestStatus.textContent = `RETEST count unavailable: ${error?.message || 'request failed'}.`; }
  };
  retestStart?.addEventListener('click', async () => {
    const start = retestStartDate?.value || '';
    const end = retestEndDate?.value || '';
    retestStart.disabled = true;
    if (retestImport) retestImport.disabled = true;
    retestTesterJobId = '';
    retestInboxReady = false;
    retestImportJobId = '';
    if (retestFailure) { retestFailure.hidden = true; retestFailure.removeAttribute('href'); }
    if (retestStatus) retestStatus.textContent = 'CHECK & RETEST: checking configured tester inbox…';
    try {
      const snapshot = await requestJson('/api/v2/jobs');
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const candidate = selectCommittedRetestTester(jobs);
      if (candidate?.job_id) {
        try {
          const verified = await requestJson('/api/v2/strategies/tester/verify-inbox', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ job_id: candidate.job_id }),
          });
          retestTesterJobId = verified.job_id || candidate.job_id;
          renderRetestTester(verified);
          if (retestInboxReady) return;
          retestTesterJobId = '';
        } catch (error) {
          if (error?.code === 'RETEST_SOURCE_ARTIFACTS_UNAVAILABLE') {
            if (retestStatus) retestStatus.textContent = 'CHECK & RETEST: source files are absent; starting a new RETEST.';
          } else {
            if (retestStatus) retestStatus.textContent = `CHECK & RETEST: committed inbox is unavailable${error?.message ? ` · ${error.message}` : ''}.`;
            retestStart.disabled = false;
            return;
          }
        }
      }
      if (!validIsoDate(start) || !validIsoDate(end) || start >= end || end > testerMaxDate()) { if (retestStatus) retestStatus.textContent = 'Enter valid RETEST dates ending no later than yesterday.'; retestStart.disabled = false; return; }
      const result = await remoteRequest('/api/v2/strategies/performance-v2/retest/start', { test_start: start, test_end: end });
      retestTesterJobId = result.job?.job_id || '';
      if (!retestTesterJobId) throw new Error('missing RETEST job');
      renderRetestTester(result.job); await pollRetestTester();
      if (!retestTerminal(result.job)) startRetestPolling();
    } catch (error) { if (retestStatus) retestStatus.textContent = `CHECK & RETEST failed: ${error?.message || 'request failed'}.`; retestStart.disabled = false; }
  });
  retestImport?.addEventListener('click', async () => {
    if (!retestTesterJobId || !retestInboxReady) return;
    retestImport.disabled = true;
    if (retestFailure) { retestFailure.hidden = true; retestFailure.removeAttribute('href'); }
    try {
      const result = await remoteRequest('/api/v2/strategies/performance-v2/retest/import', { tester_job_id: retestTesterJobId });
      retestImportJobId = result.job?.job_id || '';
      if (!retestImportJobId) throw new Error('missing RETEST import job');
      if (!(await pollRetestImport())) startRetestImportPolling();
    } catch (error) { if (retestStatus) retestStatus.textContent = `IMPORT & REPLACE failed: ${error?.message || 'request failed'}.`; retestImport.disabled = !retestInboxReady; }
  });
  const recoverRetestJobs = async () => {
    try {
      const snapshot = await requestJson('/api/v2/jobs');
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const candidate = selectRetestTester(jobs);
      if (candidate?.state === 'COMMITTED' && candidate.inbox_ready === true && retestStatus) {
        retestStatus.textContent = 'RETEST inbox found; press CHECK & RETEST to verify it.';
      }
    } catch (_) { /* count/status remains independently visible */ }
  };
  retestCard?.addEventListener('toggle', () => { if (retestCard.open) loadRetestStatus(); });
  loadRetestStatus();
  recoverRetestJobs();

  // Global finalist retest uses a server-frozen cohort. The browser sends
  // only scope and dates; Strategy/Result IDs are returned by the server.
  const finalistRetestStart = document.querySelector('#performance-v2-finalist-retest-start-button');
  const finalistRetestCard = document.querySelector('#performance-v2-finalist-retest-card');
  const finalistRetestImport = document.querySelector('#performance-v2-finalist-retest-import-button');
  const finalistRetestEquityFilter = document.querySelector('#performance-v2-finalist-retest-equity-filter-button');
  const finalistRetestReserve = document.querySelector('#performance-v2-finalist-retest-reserve');
  const finalistRetestClearReports = document.querySelector('#performance-v2-finalist-retest-clear-reports');
  const finalistRetestStartDate = document.querySelector('#performance-v2-finalist-retest-start');
  const finalistRetestEndDate = document.querySelector('#performance-v2-finalist-retest-end');
  const finalistRetestStatus = document.querySelector('#performance-v2-finalist-retest-status');
  const finalistRetestCount = document.querySelector('#performance-v2-finalist-retest-count');
  const finalistRetestSuccesses = document.querySelector('#performance-v2-finalist-retest-successes');
  const finalistRetestFailures = document.querySelector('#performance-v2-finalist-retest-failures');
  const finalistRetestExport = document.querySelector('#performance-v2-finalist-retest-export');
  const finalistRetestControlFile = document.querySelector('#performance-v2-finalist-retest-control-file');
  const finalistRetestControlImport = document.querySelector('#performance-v2-finalist-retest-control-import');
  let finalistRetestJobId = '';
  let finalistRetestImportJobId = '';
  let finalistRetestHasSuccessfulExport = false;
  let finalistRetestTimer = 0;
  const updateFinalistRetestEquityFilter = (job) => {
    if (!finalistRetestEquityFilter) return;
    const child = job?.equity_filter || {};
    const active = child.state && !['IDLE', 'COMMITTED', 'FAILED', 'CANCELLED'].includes(child.state);
    const committed = child.state === 'COMMITTED';
    finalistRetestEquityFilter.disabled = child.eligible === false || active || committed || job?.state !== 'COMMITTED'
      || job?.outcomes_finalized !== true || Number(job?.success_count || 0) <= 0;
  };
  const updateFinalistRetestExport = () => {
    if (!finalistRetestExport) return;
    finalistRetestExport.hidden = false;
    finalistRetestExport.href = finalistRetestHasSuccessfulExport && finalistRetestJobId
      ? `/api/v2/strategies/performance-v2/finalist-retest/export?job_id=${encodeURIComponent(finalistRetestJobId)}`
      : `/api/v2/strategies/performance-v2/finalist-retest/export?include_reserve=${Boolean(finalistRetestReserve?.checked)}`;
  };
  const loadFinalistRetestPreview = async () => {
    try {
      const preview = await requestJson(`/api/v2/strategies/performance-v2/finalist-retest/preview?include_reserve=${Boolean(finalistRetestReserve?.checked)}`);
      if (finalistRetestStartDate && !finalistRetestStartDate.value) finalistRetestStartDate.value = preview.test_start || '';
      if (finalistRetestEndDate && !finalistRetestEndDate.value) finalistRetestEndDate.value = preview.test_end || '';
      if (finalistRetestCount) finalistRetestCount.textContent = String(preview.cohort_count ?? 0);
      if (finalistRetestFailures) finalistRetestFailures.textContent = String(preview.excluded_count ?? 0);
    } catch (error) {
      if (finalistRetestStatus) finalistRetestStatus.textContent = `Global finalist preview unavailable: ${error?.message || 'request failed'}.`;
    }
  };
  const pollFinalistRetest = async () => {
    if (!finalistRetestJobId) return;
    try {
      const job = await requestJson(`/api/v2/strategies/performance-v2/finalist-retest/status?job_id=${encodeURIComponent(finalistRetestJobId)}`);
      updateFinalistRetestEquityFilter(job);
      const importPending = job.import_pending === true;
      const equityState = job.equity_filter?.state;
      const equityActive = equityState && !['IDLE', 'COMMITTED', 'FAILED', 'CANCELLED'].includes(equityState);
      const terminal = !equityActive && !importPending && (
        ['FAILED', 'CANCELLED'].includes(job.state)
        || (job.state === 'COMMITTED' && (!job.import_job_id || job.outcomes_finalized === true))
      );
      if (finalistRetestCount) finalistRetestCount.textContent = String(job.cohort_count ?? 0);
      if (finalistRetestSuccesses) finalistRetestSuccesses.textContent = String(job.success_count ?? 0);
      if (finalistRetestFailures) finalistRetestFailures.textContent = String(job.failure_count ?? 0);
      finalistRetestImportJobId = typeof job.import_job_id === 'string' ? job.import_job_id : '';
      const progress = job.progress || {};
      const current = Number(progress.current || 0);
      const total = Number(progress.total || 0);
      const nativeStatus = progress.native_status ? ` · ${progress.native_status}` : '';
      const publicationWarning = progress.publication_error ? ` · Panel progress update: ${formatErrorReason(progress.publication_error)}` : '';
      const error = job.error ? ` · ${formatErrorReason(job.error)}` : '';
      const cleanupError = job.error?.cleanup_error ? ` · cleanup: ${formatErrorReason(job.error.cleanup_error)}` : '';
      const failureDetails = Array.isArray(job.failures)
        ? job.failures.slice(0, 8).map((failure) => `#${failure.strategy_id ?? '?'} ${failure.strategy_name || 'strategy'}: ${failure.reason || 'IMPORT_FAILED'}${failure.error ? ` (${failure.error})` : ''}`).join('; ')
        : '';
      const remainingFailures = Math.max(0, (Array.isArray(job.failures) ? job.failures.length : 0) - 8);
      const importTotals = `imported ${job.imported_count ?? job.success_count ?? 0}, skipped ${job.skipped_count ?? 0}, rejected ${job.rejected_count ?? job.failure_count ?? 0} of ${job.expected_count ?? job.cohort_count ?? 0}`;
      const memberSummary = failureDetails ? `; ${failureDetails}${remainingFailures ? `; and ${remainingFailures} more` : ''}` : '';
      const equity = job.equity_filter || {};
      const equityProgress = equity.progress || {};
      const equityError = equity.error ? ` · equity filter error: ${equity.error.code || 'RETEST_EQUITY_FILTER_FAILED'}: ${equity.error.message || 'request failed'}` : '';
      const equityResult = equity.result || {};
      const equityCounts = equity.state === 'COMMITTED'
        ? ` · pass ${equityResult.pass_count ?? 0}, drop ${equityResult.drop_count ?? 0}, reserved ${equityResult.reserved_count ?? 0}, not evaluated ${equityResult.not_evaluated_count ?? 0}`
        : '';
      const equityReason = !equity.eligible && equity.ineligible_reason ? ` · reason ${equity.ineligible_reason}` : '';
      const equityStatus = ` · EQUITY FILTER ${equity.phase || equity.state || 'IDLE'} ${equityProgress.current || 0}/${equityProgress.total || 0}${equityReason}${equityCounts}${equityError}`;
      if (importPending) {
        if (finalistRetestStatus) finalistRetestStatus.textContent = 'IMPORT & REPLACE handoff is pending; do not click again.';
      } else if (finalistRetestImportJobId) {
        const imported = await requestJson(`/api/v2/strategies/performance-v2/import/status?job_id=${encodeURIComponent(finalistRetestImportJobId)}`);
        const p = imported.progress || {};
        const current = Number(p.current || 0);
        const total = Number(p.total || 0);
        const failed = Array.isArray(imported.evidence?.failed_names) ? imported.evidence.failed_names.length : Number(p.failed || 0);
        if (finalistRetestStatus) finalistRetestStatus.textContent = `IMPORT & REPLACE: ${imported.phase || imported.state || 'IMPORTING'} · ${current}/${total} · batch ${p.batch_number || 0}/${p.batch_total || 0} · retries ${p.retries || 0} · failed ${failed}; ${importTotals}${memberSummary}${imported.error ? ` · ${formatErrorReason(imported.error)}` : ''}${equityStatus}`;
        if (finalistRetestImport) finalistRetestImport.disabled = !['FAILED', 'CANCELLED'].includes(imported.state);
      } else if (finalistRetestStatus) {
        finalistRetestStatus.textContent = `Global finalist retest: ${job.phase || job.state || 'RUNNING'} · ${current}/${total}${nativeStatus}${publicationWarning}${error}${cleanupError}${equityStatus}`;
      }
      if (finalistRetestImport && !finalistRetestImportJobId) finalistRetestImport.disabled = job.state !== 'COMMITTED' || importPending;
      if (job.success_count > 0) finalistRetestHasSuccessfulExport = true;
      updateFinalistRetestExport();
      if (terminal) { window.clearInterval(finalistRetestTimer); if (finalistRetestStart) finalistRetestStart.disabled = false; }
    } catch (error) { if (finalistRetestStatus) finalistRetestStatus.textContent = `Global finalist retest unavailable: ${error?.message || 'request failed'}.`; }
  };
  finalistRetestStart?.addEventListener('click', async () => {
    finalistRetestStart.disabled = true;
    try {
      const payload = { include_reserve: Boolean(finalistRetestReserve?.checked), clear_reports: Boolean(finalistRetestClearReports?.checked) };
      finalistRetestImportJobId = '';
      finalistRetestHasSuccessfulExport = false;
      updateFinalistRetestExport();
      if (finalistRetestStartDate?.value) payload.test_start = finalistRetestStartDate.value;
      if (finalistRetestEndDate?.value) payload.test_end = finalistRetestEndDate.value;
      const result = await remoteRequest('/api/v2/strategies/performance-v2/finalist-retest/start', payload);
      finalistRetestJobId = result.job_id || result.job?.job_id || '';
      if (!finalistRetestJobId) throw new Error('missing global finalist retest job');
      if (finalistRetestStatus) finalistRetestStatus.textContent = 'Global finalist retest started.';
      finalistRetestTimer = window.setInterval(pollFinalistRetest, 1000); pollFinalistRetest();
    } catch (error) { if (finalistRetestStatus) finalistRetestStatus.textContent = `Global finalist retest failed: ${error?.message || 'request failed'}.`; finalistRetestStart.disabled = false; }
  });
  finalistRetestCard?.addEventListener('toggle', () => { if (finalistRetestCard.open) loadFinalistRetestPreview(); });
  finalistRetestReserve?.addEventListener('change', () => { loadFinalistRetestPreview(); updateFinalistRetestExport(); });
  finalistRetestEquityFilter?.addEventListener('click', async () => {
    if (!finalistRetestJobId) return;
    finalistRetestEquityFilter.disabled = true;
    try {
      const result = await remoteRequest('/api/v2/strategies/performance-v2/finalist-retest/equity-filter', { job_id: finalistRetestJobId });
      if (finalistRetestStatus) finalistRetestStatus.textContent = `Equity filter queued: ${result.job_id || 'pending'}.`;
      window.clearInterval(finalistRetestTimer);
      finalistRetestTimer = window.setInterval(pollFinalistRetest, 1000);
      await pollFinalistRetest();
    } catch (error) {
      if (finalistRetestStatus) finalistRetestStatus.textContent = `Equity filter failed: ${error?.code ? `${error.code}: ` : ''}${error?.message || 'request failed'}.`;
      finalistRetestEquityFilter.disabled = false;
    }
  });
  updateFinalistRetestExport();
  const recoverFinalistRetestJob = async () => {
    try {
      const snapshot = await requestJson('/api/v2/jobs');
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const recovered = jobs
        .filter((job) => job?.kind === 'strategies.performance.v2.finalist-retest' && ['QUEUED', 'RUNNING', 'COMMITTED'].includes(job.state) && typeof job.job_id === 'string')
        .sort((left, right) => String(right.created_at_utc || '').localeCompare(String(left.created_at_utc || '')))[0];
      if (!recovered) return;
      finalistRetestJobId = recovered.job_id;
      if (finalistRetestStatus) finalistRetestStatus.textContent = 'Recovering latest global finalist retest...';
      await pollFinalistRetest();
    } catch (error) {
      if (finalistRetestStatus) finalistRetestStatus.textContent = `Global finalist retest recovery unavailable: ${error?.message || 'request failed'}.`;
    }
  };
  recoverFinalistRetestJob();
  finalistRetestExport?.addEventListener('click', async (event) => {
    event.preventDefault();
    if (finalistRetestStatus) finalistRetestStatus.textContent = 'Preparing control workbook...';
    try {
      const response = await fetch(finalistRetestExport.href);
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        const message = result.error?.code === 'SELECTION_CACHE_INCOMPLETE'
          ? 'Prepare or recalculate the selection cache first.'
          : result.error?.message || 'control workbook export failed';
        throw new Error(message);
      }
      const disposition = response.headers.get('Content-Disposition') || '';
      const filenameMatch = disposition.match(/filename=(?:"([^"]+)"|([^;]+))/i);
      const filename = filenameMatch?.[1] || filenameMatch?.[2]?.trim() || (
        finalistRetestJobId ? 'performance-v2-finalist-retest.xlsx' : 'performance-v2-current-finalists.xlsx'
      );
      const url = URL.createObjectURL(await response.blob());
      Object.assign(document.createElement('a'), {
        href: url,
        download: filename,
      }).click();
      URL.revokeObjectURL(url);
      if (finalistRetestStatus) finalistRetestStatus.textContent = 'Control workbook downloaded.';
    } catch (error) {
      if (finalistRetestStatus) finalistRetestStatus.textContent = `Control workbook export failed: ${error?.message || 'request failed'}.`;
    }
  });
  finalistRetestImport?.addEventListener('click', async () => {
    if (!finalistRetestJobId) {
      if (finalistRetestStatus) finalistRetestStatus.textContent = 'IMPORT & REPLACE unavailable: run the retest from this panel first.';
      return;
    }
    finalistRetestImport.disabled = true;
    try {
      const result = await remoteRequest('/api/v2/strategies/performance-v2/finalist-retest/import', { tester_job_id: finalistRetestJobId });
      finalistRetestImportJobId = result.job_id || result.job?.job_id || '';
      if (!finalistRetestImportJobId) throw new Error('missing global finalist import job');
      if (finalistRetestStatus) finalistRetestStatus.textContent = 'IMPORT & REPLACE: queued.';
      window.clearInterval(finalistRetestTimer);
      finalistRetestTimer = window.setInterval(pollFinalistRetest, 1000);
      pollFinalistRetest();
    }
    catch (error) {
      if (finalistRetestStatus) finalistRetestStatus.textContent = `Global finalist import failed: ${error?.message || 'request failed'}.`;
      window.clearInterval(finalistRetestTimer);
      finalistRetestTimer = window.setInterval(pollFinalistRetest, 1000);
      await pollFinalistRetest();
    }
  });
  finalistRetestControlImport?.addEventListener('click', () => finalistRetestControlFile?.click());
  finalistRetestControlFile?.addEventListener('change', async () => {
    const file = finalistRetestControlFile.files?.[0];
    if (!file) return;
    try {
      const response = await fetch('/api/v2/strategies/performance-v2/finalist-retest/control-import', {
        method: 'POST', headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }, body: file,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.code || result.error?.message || 'control import failed');
      if (finalistRetestStatus) finalistRetestStatus.textContent = `Control workbook imported: ${result.group_count || 0} groups.`;
    } catch (error) {
      if (finalistRetestStatus) finalistRetestStatus.textContent = `Control workbook import failed: ${error?.message || 'request failed'}.`;
    } finally { finalistRetestControlFile.value = ''; }
  });

  // Panel rework stage 4c: the A/B card, the Pareto/filters card and the export card are removed (their work is the
  // «Стратегии» view); the two retest cards stay.
  const performanceV2FinalistRetestTitle = document.querySelector('#performance-v2-finalist-retest-card summary b');
  const performanceV2RetestTitle = document.querySelector('#performance-v2-retest-card summary b');
  if (performanceV2FinalistRetestTitle) performanceV2FinalistRetestTitle.textContent = '4. Bulk RETEST current FINALIST';
  if (performanceV2RetestTitle) performanceV2RetestTitle.textContent = '5. CHECK & RETEST';
  const selectionReviewFile = document.querySelector('#performance-v2-selection-review-file');
  const selectionReviewImportButton = document.querySelector('#performance-v2-selection-review-import');
  const selectionReviewImportResults = document.querySelector('#performance-v2-selection-review-results');
  const selectionReviewImportStatus = document.querySelector('#performance-v2-retest-tag-import-status');
  selectionReviewImportButton?.addEventListener('click', () => selectionReviewFile?.click());
  selectionReviewFile?.addEventListener('change', async () => {
    const files = [...(selectionReviewFile.files || [])]
      .filter((file) => file.name.toLowerCase().endsWith('.xlsx'))
      .sort((a, b) => a.lastModified - b.lastModified || a.name.localeCompare(b.name));
    if (!files.length) return;
    if (selectionReviewImportButton) selectionReviewImportButton.disabled = true;
    const imported = [];
    let failed = 0;
    const importErrorText = {
      RETEST_TAG_IMPORT_INVALID_FILE: 'Некорректный XLSX-файл.',
      RETEST_TAG_IMPORT_DATABASE_MISMATCH: 'Файл относится к другой базе результатов.',
      RETEST_TAG_IMPORT_STRATEGY_MISMATCH: 'В XLSX есть неизвестная стратегия.',
      RETEST_TAG_IMPORT_INVALID_RETEST: 'В колонке RETEST допускается только RETEST или пустая ячейка.',
    };
    selectionReviewImportResults?.replaceChildren();
    try {
      for (const [index, file] of files.entries()) {
        const fileStatus = document.createElement('div');
        fileStatus.className = 'selection-review-import-result state-badge state-running';
        fileStatus.textContent = `… ${file.name} — импортируется (${index + 1}/${files.length})`;
        selectionReviewImportResults?.append(fileStatus);
        try {
          if (selectionReviewImportStatus) selectionReviewImportStatus.textContent = `Импорт XLSX ${index + 1}/${files.length}: ${file.name}`;
          const response = await fetch('/api/v2/strategies/performance-v2/retest-tags-import', {
            method: 'POST', headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }, body: file,
          });
          const result = await response.json().catch(() => ({}));
          const code = result.error?.code || '';
          if (response.ok) {
            imported.push(result);
            fileStatus.className = 'selection-review-import-result state-badge state-ready';
            fileStatus.textContent = `✓ ${file.name} — импортировано.`;
          } else {
            failed += 1;
            fileStatus.className = 'selection-review-import-result state-badge state-pending';
            fileStatus.textContent = `✗ ${file.name} — ${importErrorText[code] || result.error?.message || 'Ошибка импорта.'} (${code || 'UNKNOWN'})`;
          }
        } catch (error) {
          failed += 1;
          fileStatus.className = 'selection-review-import-result state-badge state-pending';
          fileStatus.textContent = `✗ ${file.name} — ошибка запроса: ${error.message || 'без описания'}.`;
        }
      }
      if (selectionReviewImportStatus) selectionReviewImportStatus.textContent = `Импортировано ${imported.length}/${files.length}. Ошибок: ${failed}.`;
    } finally {
      await loadRetestStatus();
      selectionReviewFile.value = '';
      if (selectionReviewImportButton) selectionReviewImportButton.disabled = false;
    }
  });

  const maintenanceEndpoint = '/api/v2/strategies/performance-v2/maintenance';
  const maintenanceCatalogButton = document.querySelector('#performance-v2-maintenance-catalog');
  const maintenanceCatalogContent = document.querySelector('#performance-v2-maintenance-catalog-content');
  const maintenancePairs = document.querySelector('#performance-v2-maintenance-pairs');
  const maintenanceStatus = document.querySelector('#performance-v2-maintenance-status');
  const maintenancePreview = document.querySelector('#performance-v2-maintenance-preview');
  const maintenancePreviewPairs = document.querySelector('#performance-v2-maintenance-preview-pairs');
  const maintenanceRejectedWarning = document.querySelector('#performance-v2-maintenance-rejected-warning');
  const maintenanceProgress = document.querySelector('#performance-v2-maintenance-progress');
  let maintenanceSymbols = [];
  let maintenancePreviewToken = '';
  let maintenancePreviewDocument = null;
  let maintenancePreviewGeneration = 0;
  let maintenancePollTimer = 0;
  let maintenancePollGeneration = 0;
  let maintenancePollJobId = '';
  let maintenanceJobActive = false;

  const maintenanceStopPolling = () => {
    if (maintenancePollTimer) window.clearTimeout(maintenancePollTimer);
    maintenancePollTimer = 0;
    maintenancePollJobId = '';
    maintenancePollGeneration += 1;
  };
  const maintenanceSchedulePoll = (jobId, delay) => {
    maintenanceStopPolling();
    maintenancePollJobId = jobId;
    const generation = maintenancePollGeneration;
    maintenancePollTimer = window.setTimeout(() => {
      maintenancePollTimer = 0;
      if (generation === maintenancePollGeneration && maintenancePollJobId === jobId) maintenancePoll(jobId, generation);
    }, delay);
  };

  const maintenanceSelectedSymbols = () => [...(maintenancePairs?.querySelectorAll('input[type="checkbox"]:checked') || [])].map((input) => input.value);
  const maintenanceSetStatus = (message, isError = false) => {
    if (!maintenanceStatus) return;
    maintenanceStatus.hidden = !message;
    maintenanceStatus.textContent = message;
    maintenanceStatus.dataset.state = isError ? 'error' : 'ready';
  };
  const maintenanceClearPreview = () => {
    maintenancePreviewGeneration += 1;
    maintenancePreviewToken = '';
    maintenancePreviewDocument = null;
    if (maintenancePreview) maintenancePreview.hidden = true;
  };
  const maintenanceSyncActions = () => {
    const hasSelection = maintenanceSelectedSymbols().length > 0;
    const busy = maintenanceJobActive;
    for (const id of ['performance-v2-maintenance-rejected', 'performance-v2-maintenance-full']) {
      const button = document.querySelector(`#${id}`);
      if (button) button.disabled = !hasSelection || busy;
    }
  };
  const maintenanceRenderSymbols = () => {
    if (!maintenancePairs) return;
    maintenancePairs.replaceChildren(...maintenanceSymbols.map((symbol) => {
      const label = document.createElement('label');
      label.className = 'check performance-v2-maintenance-pair';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = symbol;
      input.addEventListener('change', () => {
        maintenanceClearPreview();
        maintenanceSyncActions();
      });
      const text = document.createElement('span');
      text.textContent = symbol.endsWith('USDT') ? symbol.slice(0, -4) : symbol;
      label.append(input, text);
      return label;
    }));
    maintenanceSyncActions();
  };
  maintenanceCatalogButton?.addEventListener('click', async () => {
    maintenanceClearPreview();
    maintenanceCatalogButton.disabled = true;
    maintenanceSetStatus('Загрузка списка пар…');
    try {
      const result = await requestJson(`${maintenanceEndpoint}/catalog`);
      maintenanceSymbols = Array.isArray(result.symbols) ? result.symbols : [];
      maintenanceRenderSymbols();
      if (maintenanceCatalogContent) maintenanceCatalogContent.hidden = false;
      maintenanceCatalogButton.textContent = 'Обновить список пар';
      maintenanceSetStatus(`Загружено пар: ${maintenanceSymbols.length}.`);
    } catch (error) {
      maintenanceSetStatus(`Не удалось загрузить список пар: ${error?.message || 'ошибка запроса'}`, true);
    } finally {
      maintenanceCatalogButton.disabled = false;
    }
  });
  document.querySelector('#performance-v2-maintenance-select-all')?.addEventListener('click', () => {
    maintenancePairs?.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = true; });
    maintenanceClearPreview();
    maintenanceSyncActions();
  });
  document.querySelector('#performance-v2-maintenance-select-none')?.addEventListener('click', () => {
    maintenancePairs?.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.checked = false; });
    maintenanceClearPreview();
    maintenanceSyncActions();
  });

  const maintenanceCount = (value) => new Intl.NumberFormat('ru-RU').format(Number(value) || 0);
  const maintenanceRenderPreview = (result) => {
    if (!maintenancePreview || !maintenancePreviewPairs) return;
    maintenancePreviewToken = String(result.token || '');
    maintenancePreviewDocument = result;
    const strategyCountHeading = document.querySelector('#performance-v2-maintenance-strategy-count-heading');
    if (strategyCountHeading) strategyCountHeading.textContent = result.operation === 'full'
      ? 'Стратегий к удалению' : 'Стратегий к очистке';
    maintenancePreviewPairs.replaceChildren(...(result.pairs || []).map((pair) => {
      const row = document.createElement('tr');
      const symbol = document.createElement('th');
      symbol.scope = 'row';
      symbol.textContent = pair.symbol;
      const count = document.createElement('td');
      count.textContent = maintenanceCount(pair.strategy_count);
      row.append(symbol, count);
      return row;
    }));
    const scopeNote = document.querySelector('#performance-v2-maintenance-preview-scope-note');
    if (scopeNote) scopeNote.textContent = result.operation === 'full'
      ? 'Полное удаление также очищает записи выбора и больше не используемые analysis_plateaus; они не входят в число стратегий.'
      : 'Показаны стратегии для очистки. Подробные факты, кеши и per-strategy результаты удаляются, а настройки и компактный результат сохраняются только для дедупликации.';
    const warning = document.querySelector('#performance-v2-maintenance-global-warning');
    if (warning) warning.hidden = result.operation !== 'full';
    if (maintenanceRejectedWarning) maintenanceRejectedWarning.hidden = result.operation !== 'rejected';
    const confirm = document.querySelector('#performance-v2-maintenance-confirm');
    if (confirm) confirm.disabled = false;
    maintenancePreview.hidden = false;
  };
  const requestMaintenancePreview = async (operation) => {
    const symbols = maintenanceSelectedSymbols();
    if (!symbols.length) return;
    maintenanceClearPreview();
    const generation = ++maintenancePreviewGeneration;
    maintenanceSetStatus('Подсчёт строк для предварительного просмотра…');
    for (const id of ['performance-v2-maintenance-rejected', 'performance-v2-maintenance-full']) {
      const button = document.querySelector(`#${id}`);
      if (button) button.disabled = true;
    }
    try {
      const result = await requestJson(`${maintenanceEndpoint}/preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operation, symbols }),
      });
      if (generation !== maintenancePreviewGeneration) return;
      maintenanceRenderPreview(result);
      const strategyCount = (result.pairs || []).reduce((total, pair) => total + Number(pair.strategy_count || 0), 0);
      const action = operation === 'full' ? 'к удалению' : 'к очистке';
      maintenanceSetStatus(`Предварительный просмотр готов: ${maintenanceCount(strategyCount)} стратегий ${action}.`);
    } catch (error) {
      if (generation !== maintenancePreviewGeneration) return;
      maintenanceSetStatus(`Не удалось построить предварительный просмотр: ${error?.message || 'ошибка запроса'}`, true);
    } finally {
      if (generation === maintenancePreviewGeneration) maintenanceSyncActions();
    }
  };
  document.querySelector('#performance-v2-maintenance-rejected')?.addEventListener('click', () => requestMaintenancePreview('rejected'));
  document.querySelector('#performance-v2-maintenance-full')?.addEventListener('click', () => requestMaintenancePreview('full'));
  document.querySelector('#performance-v2-maintenance-cancel')?.addEventListener('click', () => {
    maintenanceClearPreview();
    maintenanceSetStatus('Предварительный просмотр отменён.');
  });

  const maintenanceRenderJob = (job) => {
    if (!maintenanceProgress) return;
    maintenanceProgress.hidden = false;
    const total = Number(job.pair_scoped_total) || 0;
    const deleted = Number(job.pair_scoped_deleted) || 0;
    const completed = job.status === 'COMMITTED' || job.status === 'FAILED';
    const percent = total > 0 ? Math.min(100, (deleted / total) * 100) : (job.status === 'COMMITTED' ? 100 : 0);
    const progressMax = total > 0 ? total : 1;
    const progressNow = total > 0 ? Math.min(deleted, total) : (job.status === 'COMMITTED' ? 1 : 0);
    const track = maintenanceProgress.querySelector('.progress-track');
    const fill = track?.querySelector('span');
    if (track) {
      track.setAttribute('aria-valuemax', String(progressMax));
      track.setAttribute('aria-valuenow', String(progressNow));
    }
    if (fill) fill.style.width = `${percent}%`;
    const phase = document.querySelector('#performance-v2-maintenance-phase');
    if (phase) phase.textContent = `Этап: ${job.phase || '—'}${job.current_table ? ` · таблица ${job.current_table}` : ''}`;
    const counts = document.querySelector('#performance-v2-maintenance-progress-counts');
    const strategyTotal = Number(job.strategy_total) || 0;
    const deletedStrategies = Number(job.table_counts?.strategies) || 0;
    const strategyProgress = job.operation === 'full'
      ? `Удалено стратегий ${maintenanceCount(deletedStrategies)} из ${maintenanceCount(strategyTotal)}`
      : job.status === 'COMMITTED'
        ? `Архивировано стратегий ${maintenanceCount(strategyTotal)} из ${maintenanceCount(strategyTotal)}`
        : `К очистке стратегий ${maintenanceCount(strategyTotal)}`;
    if (counts) counts.textContent = `${strategyProgress}; удалено ${maintenanceCount(deleted)} из ${maintenanceCount(total)} строк выбранных пар`;
    const global = document.querySelector('#performance-v2-maintenance-global-counts');
    if (global) global.hidden = job.operation !== 'full';
    if (global) {
      const values = job.global_journal_counts || {};
      global.textContent = `Глобальные записи: import_files — ${maintenanceCount(values.import_files)}, import_runs — ${maintenanceCount(values.import_runs)} из ${maintenanceCount(job.global_journal_total)}.`;
    }
    const elapsed = document.querySelector('#performance-v2-maintenance-elapsed');
    if (elapsed) elapsed.textContent = `Прошло: ${(Number(job.elapsed_seconds) || 0).toFixed(1)} с`;
    if (completed) {
      maintenanceStopPolling();
      if (job.status === 'COMMITTED' && total === 0 && !(Number(job.global_journal_total) > 0)) maintenanceSetStatus('Нечего удалять: в выбранных парах нет строк. Обслуживание завершено.', false);
      else if (job.status === 'COMMITTED') {
        const strategyOutcome = job.operation === 'full'
          ? `Удалено стратегий: ${maintenanceCount(strategyTotal)}.`
          : `Архивировано стратегий: ${maintenanceCount(strategyTotal)}.`;
        maintenanceSetStatus(`Обслуживание завершено. ${strategyOutcome} Показаны подтверждённые количества строк.`, false);
      }
      else maintenanceSetStatus(`Ошибка на этапе ${job.current_table || job.phase || 'неизвестно'}: ${job.error || 'операция завершилась с ошибкой'}. Показаны уже подтверждённые изменения.${job.recovery_warning ? ` ${job.recovery_warning}` : ''}`, true);
      maintenanceJobActive = false;
      if (maintenancePairs) maintenancePairs.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.disabled = false; });
      if (maintenanceCatalogButton) maintenanceCatalogButton.disabled = false;
      for (const id of ['performance-v2-maintenance-select-all', 'performance-v2-maintenance-select-none']) {
        const button = document.querySelector(`#${id}`);
        if (button) button.disabled = false;
      }
      maintenanceSyncActions();
      if (job.status === 'COMMITTED') {
        window.setTimeout(async () => {
          try {
            const result = await requestJson(`${maintenanceEndpoint}/catalog`);
            maintenanceSymbols = Array.isArray(result.symbols) ? result.symbols : [];
            maintenanceRenderSymbols();
            if (maintenanceCatalogContent) maintenanceCatalogContent.hidden = false;
            if (maintenanceCatalogButton) {
              maintenanceCatalogButton.textContent = 'Обновить список пар';
              maintenanceCatalogButton.disabled = false;
            }
          } catch (error) {
            maintenanceSetStatus(`Удаление завершено, но список пар не обновлён: ${error?.message || 'ошибка запроса'}`, true);
          }
        }, 100);
      } else if (String(job.error || '').includes('preview is stale')) {
        const operation = maintenancePreviewDocument?.operation;
        const generation = maintenancePreviewGeneration;
        maintenanceSetStatus('Предварительный просмотр устарел; рассчитывается новый перед повторным подтверждением.', true);
        if (operation) window.setTimeout(() => {
          if (generation === maintenancePreviewGeneration) requestMaintenancePreview(operation);
        }, 150);
      }
    }
  };
  const maintenancePoll = async (jobId, generation) => {
    try {
      const job = await requestJson(`${maintenanceEndpoint}/status?job_id=${encodeURIComponent(jobId)}`);
      if (generation !== maintenancePollGeneration || maintenancePollJobId !== jobId) return;
      maintenanceRenderJob(job);
      if (job.status === 'RUNNING') maintenanceSchedulePoll(jobId, 350);
    } catch (error) {
      if (generation !== maintenancePollGeneration || maintenancePollJobId !== jobId) return;
      maintenanceSetStatus(`Не удалось получить состояние операции: ${error?.message || 'ошибка запроса'}`, true);
      const missingJob = error?.code === 'MAINTENANCE_JOB_NOT_FOUND';
      if (missingJob || error?.code) {
        maintenanceStopPolling();
        maintenanceJobActive = false;
        if (maintenancePairs) maintenancePairs.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.disabled = false; });
        if (maintenanceCatalogButton) maintenanceCatalogButton.disabled = false;
        for (const id of ['performance-v2-maintenance-select-all', 'performance-v2-maintenance-select-none']) {
          const button = document.querySelector(`#${id}`);
          if (button) button.disabled = false;
        }
        maintenanceSyncActions();
        return;
      }
      maintenanceSchedulePoll(jobId, 1000);
    }
  };
  document.querySelector('#performance-v2-maintenance-confirm')?.addEventListener('click', async () => {
    if (!maintenancePreview || !maintenancePreviewToken || !maintenancePreviewDocument) return;
    const previewDocument = maintenancePreviewDocument;
    const previewToken = maintenancePreviewToken;
    maintenanceStopPolling();
    const confirm = document.querySelector('#performance-v2-maintenance-confirm');
    if (confirm) confirm.disabled = true;
    try {
      const result = await requestJson(`${maintenanceEndpoint}/apply`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: previewToken }),
      });
      maintenancePreviewToken = '';
      maintenancePreview.hidden = true;
      maintenanceJobActive = true;
      if (maintenanceCatalogButton) maintenanceCatalogButton.disabled = true;
      if (maintenancePairs) maintenancePairs.querySelectorAll('input[type="checkbox"]').forEach((input) => { input.disabled = true; });
      for (const id of ['performance-v2-maintenance-select-all', 'performance-v2-maintenance-select-none']) {
        const button = document.querySelector(`#${id}`);
        if (button) button.disabled = true;
      }
      maintenanceSyncActions();
      maintenanceSetStatus('Удаление запущено. Состояние обновляется по подтверждённым данным.');
      maintenanceRenderJob({
        status: 'RUNNING', operation: previewDocument.operation, phase: 'waiting_for_writer', pair_scoped_total: Number(previewDocument.pair_scoped_total) || 0,
        pair_scoped_deleted: 0, global_journal_total: Object.values(previewDocument.global_counts || {}).reduce((sum, count) => sum + (Number(count) || 0), 0),
        global_journal_counts: {}, elapsed_seconds: 0,
      });
      maintenanceSchedulePoll(result.job_id, 350);
    } catch (error) {
      maintenanceSetStatus(`Удаление не запущено: ${error?.message || 'ошибка запроса'}`, true);
      if (confirm) confirm.disabled = false;
      if (error?.code === 'PREVIEW_TOKEN_INVALID') {
        const operation = maintenancePreviewDocument?.operation;
        maintenanceClearPreview();
        const generation = maintenancePreviewGeneration;
        if (operation) window.setTimeout(() => {
          if (generation === maintenancePreviewGeneration) requestMaintenancePreview(operation);
        }, 150);
      }
    }
  });

  const settingsStatus = document.querySelector('#settings-status');
  // PerformanceDB selection (P7 spec §5): list, switch and create one database per exchange.
  const performanceDatabasesEndpoint = '/api/v2/settings/performance-databases';
  const performanceDatabasesRows = document.querySelector('#performance-databases-rows');
  const performanceDatabasesStatus = document.querySelector('#performance-databases-status');
  const setPerformanceDatabasesStatus = (message) => { if (performanceDatabasesStatus) performanceDatabasesStatus.textContent = message; };
  function renderPerformanceDatabases(data) {
    if (!performanceDatabasesRows) return;
    performanceDatabasesRows.replaceChildren();
    for (const row of data.items || []) {
      const line = document.createElement('tr');
      const markers = row.markers
        ? `${row.markers.evaluation_protocol} · ${row.markers.exchange || '—'} · схема ${row.markers.schema_version || '—'}`
        : (row.exists ? 'недоступны' : 'файла ещё нет');
      const cells = [row.name ?? 'по умолчанию', row.database_root, row.evaluation_protocol, row.exchange || '—', markers,
        row.size_bytes === null || row.size_bytes === undefined ? '—' : formatBytes(row.size_bytes)];
      for (const value of cells) { const cell = document.createElement('td'); cell.textContent = String(value); line.append(cell); }
      const action = document.createElement('td');
      if (row.active) action.textContent = 'активна';
      else {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'button button-secondary'; button.textContent = 'Сделать активной';
        button.disabled = !!data.busy;
        button.addEventListener('click', () => activatePerformanceDatabase(row.name ?? null));
        action.append(button);
      }
      line.append(action);
      performanceDatabasesRows.append(line);
    }
    const exchange = document.querySelector('#performance-database-exchange');
    if (exchange) {
      const current = exchange.value;
      exchange.replaceChildren(...(data.exchanges || []).map((name) => new Option(name, name)));
      if (current && (data.exchanges || []).includes(current)) exchange.value = current;
    }
    setPerformanceDatabasesStatus(data.busy
      ? 'Есть запущенные задания или открытый сбор отчётов: переключение недоступно.'
      : `Активна: ${data.active ?? 'база по умолчанию'}.`);
  }
  async function loadPerformanceDatabases() {
    if (!performanceDatabasesRows) return;
    try { renderPerformanceDatabases(await requestJson(performanceDatabasesEndpoint)); }
    catch (error) { setPerformanceDatabasesStatus(`Не удалось загрузить базы: ${error.message}`); }
  }
  async function activatePerformanceDatabase(name) {
    try {
      renderPerformanceDatabases(await remoteRequest(`${performanceDatabasesEndpoint}/activate`, { name }));
    } catch (error) {
      setPerformanceDatabasesStatus(error.code === 'RESOURCE_BUSY'
        ? 'Переключение недоступно: есть запущенные задания или открытый сбор отчётов.'
        : `Не удалось переключить: ${error.message}`);
    }
  }
  document.querySelector('#performance-databases-reload')?.addEventListener('click', loadPerformanceDatabases);
  document.querySelector('#performance-database-create')?.addEventListener('click', async () => {
    const value = (id) => document.querySelector(id)?.value.trim() || '';
    try {
      renderPerformanceDatabases(await remoteRequest(`${performanceDatabasesEndpoint}/create`, {
        name: value('#performance-database-name'), database_root: value('#performance-database-root'),
        exchange: value('#performance-database-exchange'),
      }));
      setPerformanceDatabasesStatus('База создана. Чтобы тестировать на ней, сделайте её активной.');
    } catch (error) { setPerformanceDatabasesStatus(`Не удалось создать базу: ${error.message}`); }
  });
  const settingsPayload = () => ({ panel: {
    default_root: 'static',
    path_defaults: {
      local_runner_root: document.querySelector('#settings-local-runner')?.value || '',
       local_source_db_root: document.querySelector('#settings-source-root')?.value || '',
      local_output_root: document.querySelector('#settings-output-root')?.value || '',
    },
   }, operational: {
     source_db_path: document.querySelector('#settings-source-root')?.value || '',
     output_root: document.querySelector('#settings-output-root')?.value || '',
     import_workers: Number(document.querySelector('#settings-import-workers')?.value || 0),
   } });
  const workerSettingsPayload = () => ({
    panel: {},
    operational: { import_workers: Number(document.querySelector('#settings-import-workers')?.value || 0) },
  });
  const settingsButtons = [...document.querySelectorAll('#settings .settings-generic-actions')];
  settingsButtons.forEach((row, index) => {
    const [validate, save] = row.querySelectorAll('button');
    validate?.addEventListener('click', async () => {
      try {
        await remoteRequest('/api/v2/settings/validate', settingsPayload());
        if (settingsStatus) settingsStatus.textContent = 'Settings are valid.';
      } catch (_) { if (settingsStatus) settingsStatus.textContent = 'Settings validation failed.'; }
    });
    save?.addEventListener('click', async () => {
      try {
        if (index === 0) {
          const result = await requestJson('/api/v2/settings/reload');
          if (!result.valid) throw new Error('reload failed');
          await loadSafeDefaults();
          if (settingsStatus) settingsStatus.textContent = 'Settings defaults reloaded.';
        } else {
          await remoteRequest('/api/v2/settings/save', settingsPayload());
          if (settingsStatus) settingsStatus.textContent = 'Settings saved.';
        }
      } catch (_) { if (settingsStatus) settingsStatus.textContent = 'Settings operation failed.'; }
    });
  });
  document.querySelector('#settings-workers-save')?.addEventListener('click', async () => {
    try {
      await remoteRequest('/api/v2/settings/save', workerSettingsPayload());
      await loadSafeDefaults();
      if (settingsStatus) settingsStatus.textContent = 'Общий лимит рабочих процессов сохранён.';
    } catch (_) { if (settingsStatus) settingsStatus.textContent = 'Общий лимит рабочих процессов не сохранён.'; }
  });
  function portfolioErrorMessage(error) {
    return error?.code ? `${error.code}: ${portfolioReasonHelpers.humanize(error.message || '')}`.trim() : portfolioReasonHelpers.humanize(error?.message || 'Не удалось выполнить запрос.');
  }

  function portfolioValues(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === 'object') return Object.values(value);
    return [];
  }

  function portfolioPairRows(readiness) {
    const source = readiness?.available_pairs || readiness?.pairs || readiness?.stage1?.available_pairs || readiness?.stage1?.pairs || [];
    const finalistCounts = readiness?.current_finalists || readiness?.stage1?.current_finalists || {};
    const grouped = new Map();
    const items = Array.isArray(source) ? source : Object.entries(source || {}).map(([pair, item]) => ({ ...(item && typeof item === 'object' ? item : {}), pair: item?.pair || pair }));
    for (const item of items) {
      if (typeof item !== 'string' || !item.includes('|')) continue;
      const [pair, side] = item.split('|'); const row = grouped.get(pair) || { pair, finalistLong: 0, finalistShort: 0, long: 0, short: 0, selected: false };
      row[`finalist${side[0]}${side.slice(1).toLowerCase()}`] = Number(finalistCounts[item] || 0); grouped.set(pair, row);
    }
    const normalItems = grouped.size ? [...grouped.values()] : items;
    return normalItems.map((item) => {
      const row = typeof item === 'string' ? { pair: item } : (item || {});
      const counts = row.finalist_counts || row.counts || {};
      const long = Number(row.current_finalist_long ?? row.finalist_long ?? row.finalistLong ?? counts.LONG ?? counts.long ?? 0);
      const short = Number(row.current_finalist_short ?? row.finalist_short ?? row.finalistShort ?? counts.SHORT ?? counts.short ?? 0);
      const finalistLong = Number.isFinite(long) ? Math.max(0, long) : 0;
      const finalistShort = Number.isFinite(short) ? Math.max(0, short) : 0;
      return { pair: String(row.pair || row.symbol || row.name || ''), finalistLong, finalistShort, long: finalistLong > 0 ? 1 : 0, short: finalistLong <= 0 && finalistShort > 0 ? 1 : 0, selected: row.selected === true };
    }).filter((row) => row.pair && (row.finalistLong > 0 || row.finalistShort > 0));
  }

  function loadPortfolioScreen() {
    if (!loadPortfolioScreen.state) {
      const state = { pairRows: [], pairSort: 'alpha', stage2Candidates: [], stage2Selected: new Set(), stage2Campaign: '', stage2Version: 0, readiness: null, configDigest: null, activeJobId: '', job: null, poller: 0, initialized: true, locked: false, renderedJobId: '', renderedPercent: 0, renderedSubstage: '', progressAnchor: 0, progressBaseElapsed: 0, heartbeatAnchor: 0, heartbeatBaseAge: 0, settingsChanged: false, refreshPromise: null };
      const portfolioJobEndpoint = '/api/v2/portfolio/jobs/';
      const query = (selector) => document.querySelector(selector);
      const prepareButton = query('#portfolio-prepare-finalists');
      const runButton = query('#portfolio-run');
      const newButton = query('#portfolio-new-calculation');
      const cancelButton = query('#portfolio-cancel');
      const stage2Submit = query('#portfolio-stage2-submit');
      const stage2State = query('#portfolio-stage2-state');
      const stage2Reason = query('#portfolio-stage2-reason');
      const stage2Progress = query('#portfolio-stage2-progress');
      const stage2Results = query('#portfolio-stage2-results');
      const formStatus = query('#portfolio-form-status');
      const portfolioStatusLabels = Object.freeze({
        WAITING: 'ОЖИДАНИЕ', READY: 'ГОТОВО', BLOCKED: 'ЗАБЛОКИРОВАНО', EDITABLE: 'РЕДАКТИРУЕМО', FROZEN: 'ЗАФИКСИРОВАНО',
        QUEUED: 'В ОЧЕРЕДИ', RUNNING: 'ВЫПОЛНЯЕТСЯ', SUCCEEDED: 'УСПЕШНО', FAILED: 'ОШИБКА', PARTIAL: 'ЧАСТИЧНО',
        CANCEL_REQUESTED: 'ОТМЕНА ЗАПРОШЕНА', CANCELLING: 'ОТМЕНЯЕТСЯ', CANCELLED: 'ОТМЕНЕНО', INTERRUPTED: 'ПРЕРВАНО',
        DISABLED: 'ОТКЛЮЧЕНО', 'NO JOB': 'НЕТ ЗАДАНИЯ',
      });
      const portfolioStatusLabel = (value) => portfolioStatusLabels[String(value ?? '').toUpperCase()] || String(value ?? '');
      const portfolioStageLabels = Object.freeze({
        VALIDATE_SNAPSHOT: 'Проверка снимка', LOAD_FINALISTS: 'Загрузка FINALIST', SELECT_CANDIDATES: 'Отбор кандидатов',
        GENERATE_VARIANTS: 'Генерация вариантов', VALIDATE_VARIANTS: 'Проверка вариантов', BUILD_WORKBOOK: 'Сборка книги',
        PUBLISH_RESULTS: 'Публикация результатов', PREPARE: 'Подготовка', FILL_PREBUILT: 'Заполнение тестера', START: 'Запуск', READBACK: 'Чтение результата',
      });
      const portfolioStageLabel = (value) => portfolioStageLabels[String(value ?? '').toUpperCase()] || (Number.isInteger(value) ? `Этап ${value}` : portfolioStatusLabel(value));
      const setBadge = (selector, value, kind = 'pending') => {
        const node = query(selector);
        if (node) { node.className = `state-badge state-${kind}`; node.textContent = portfolioStatusLabel(value); }
      };
      const text = (selector, value) => { const node = query(selector); if (node) node.textContent = value == null || value === '' ? '—' : String(value); };
      const terminal = (job) => ['SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'].includes(String(job?.status || job?.state || '').toUpperCase());
      const statusOf = (job) => String(job?.status || job?.state || 'WAITING').toUpperCase();
      const stage2Eligible = () => Boolean(
        state.job?.kind === 'STAGE1_CALCULATION'
        && statusOf(state.job) === 'SUCCEEDED'
        && typeof state.job.campaign_id === 'string'
        && state.job.campaign_id.length > 0
        && Number.isSafeInteger(state.job?.executables_count)
        && state.job.executables_count > 0
      );
      const portfolioDecimal = (value) => {
        const raw = String(value ?? '').trim(); const match = /^(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(raw);
        if (!match || (!match[1] && !match[2])) return false;
        const exponent = Number(match[3] || 0); if (!Number.isSafeInteger(exponent)) return false;
        let digits = `${match[1]}${match[2] || ''}`.replace(/^0+/, ''); let scale = (match[2] || '').length - exponent;
        while (digits.endsWith('0') && scale > 0) { digits = digits.slice(0, -1); scale -= 1; }
        return digits !== '' && Math.max(scale, 0) <= 12 && Math.max(digits.length - scale, 0) <= 26;
      };
      const portfolioSafeInteger = (value, minimum = 0) => { const raw = String(value ?? '').trim(); const number = Number(raw); return raw !== '' && Number.isSafeInteger(number) && number >= minimum; };
      const freezeStatusCode = 'SETTINGS_CHANGED_SINCE_FREEZE';
      const freezeStatus = (status, campaignId = '') => `${portfolioStatusLabel(status)}${campaignId ? ` · Кампания ${campaignId}` : ''}${state.settingsChanged ? ' · НАСТРОЙКИ ИЗМЕНЕНЫ ПОСЛЕ ФИКСАЦИИ' : ''}`;
      const updateFreezeStatus = (job) => {
        const changed = job?.settings_changed_since_freeze === true || (state.configDigest && job?.config_digest && state.configDigest !== job.config_digest);
        if (changed) state.settingsChanged = true;
      };
      const portfolioCombinationFactor = (value, available) => (Number.isSafeInteger(value) && value > 0 && Number.isSafeInteger(available) && available > 0 ? BigInt(Math.min(value, available)) : 1n);
      const portfolioCombinationCount = (pairs) => pairs.reduce((total, row) => total
        * portfolioCombinationFactor(row.long, row.finalistLong)
        * portfolioCombinationFactor(row.short, row.finalistShort), 1n);
      const portfolioLaunchForm = () => {
        const rows = state.pairRows.map((row) => ({ ...row }));
        const byPair = new Map(rows.map((row) => [row.pair, row]));
        const pairInputs = query('#portfolio-pairs')?.querySelectorAll('input[data-portfolio-side]') || [];
        let pairFieldsValid = rows.length > 0;
        for (const input of pairInputs) {
          const row = byPair.get(input.dataset.portfolioPair); const side = String(input.dataset.portfolioSide || '').toLowerCase();
          if (!row || !['long', 'short'].includes(side)) continue;
          const value = Number(input.value); row[side] = value;
          if (!portfolioSafeInteger(input.value, 0)) pairFieldsValid = false;
        }
        const selectedPairs = rows.filter((row) => row.selected);
        const activePair = selectedPairs.length > 0 && selectedPairs.every((row) => row.long > 0 || row.short > 0);
        const invalidPairLimits = selectedPairs.filter((row) => row.long > row.finalistLong || row.short > row.finalistShort).map((row) => row.pair);
        const selectedProfiles = ['aggressive', 'balanced', 'conservative'].filter((profile) => query(`#portfolio-profile-${profile}`)?.checked);
        const profiles = selectedProfiles.map((profile) => {
          const bankAvailable = query(`#portfolio-bank-available-${profile}`)?.value || '';
          const candidates = query(`#portfolio-candidates-${profile}`)?.value || '';
          return { profile, bankAvailable, candidates };
        });
        const profilesValid = profiles.length > 0 && profiles.every((profile) => (!String(profile.bankAvailable).trim() || portfolioDecimal(profile.bankAvailable)) && portfolioSafeInteger(profile.candidates, 1) && Number(profile.candidates) <= 50);
         return { rows, selectedPairs, activePair, invalidPairLimits, selectedProfiles, profiles, valid: state.readiness?.stage1?.enabled === true && pairFieldsValid && activePair && invalidPairLimits.length === 0 && profilesValid };
      };
      const blockerItems = (readiness) => portfolioValues(readiness?.stage1?.blockers || readiness?.blockers).map((value) => portfolioReasonHelpers.humanize(typeof value === 'string' ? value : (value?.code ? `${value.code}: ${value.message || ''}` : JSON.stringify(value)))).filter(Boolean);
      const renderReadiness = (readiness) => {
        state.readiness = readiness || {};
        state.configDigest = readiness?.config_digest ?? readiness?.settings?.digest ?? null;
        state.pairRows = portfolioPairRows(readiness);
        const stage1 = readiness?.stage1 || {};
        const stage1Ready = stage1.enabled === true;
        setBadge('#portfolio-readiness-state', stage1Ready ? 'READY' : 'BLOCKED', stage1Ready ? 'ready' : 'pending');
        setBadge('#portfolio-badge', stage1Ready ? 'READY' : 'BLOCKED', stage1Ready ? 'ready' : 'pending');
        text('#portfolio-stage1-state', stage1Ready ? 'Входные данные готовы.' : 'Расчёт заблокирован: проверьте входные данные.');
        const blockers = query('#portfolio-blockers');
        if (blockers) { const reasons = blockerItems(readiness); blockers.replaceChildren(); blockers.hidden = reasons.length === 0; for (const reason of reasons) { const item = document.createElement('li'); item.textContent = reason; blockers.append(item); } }
        renderPairs();
        if (state.job) { updateFreezeStatus(state.job); text('#portfolio-job-status', freezeStatus(statusOf(state.job), state.job.campaign_id)); }
        updateControls();
      };
      const renderPairs = () => {
        const container = query('#portfolio-pairs');
        if (!container) return;
        container.replaceChildren();
        if (!state.pairRows.length) { const tableRow = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = 4; cell.textContent = 'Нет пар с финалистами LONG или SHORT.'; tableRow.append(cell); container.append(tableRow); return; }
        for (const row of portfolioSelectionHelpers.sortRows(state.pairRows, state.readiness?.finalist_history || {}, state.pairSort || 'alpha')) {
          const tableRow = document.createElement('tr');
          const pairCell = document.createElement('th'); pairCell.scope = 'row';
          const label = document.createElement('label'); label.className = 'check';
          const selected = document.createElement('input'); selected.type = 'checkbox'; selected.checked = row.selected; selected.dataset.portfolioPair = row.pair; selected.setAttribute('aria-label', `Выбрать ${row.pair}`);
          selected.addEventListener('change', () => { row.selected = selected.checked; updateControls(); });
          const name = document.createElement('span'); name.textContent = row.pair;
          const count = document.createElement('small'); count.className = 'portfolio-pair-count'; count.textContent = `FINALIST: LONG ${row.finalistLong} · SHORT ${row.finalistShort}`;
          const historyCell = document.createElement('td'); historyCell.className = 'portfolio-pair-history'; historyCell.dataset.portfolioHistory = row.pair;
          const pairInfo = document.createElement('div'); pairInfo.className = 'portfolio-pair-info';
          label.append(selected, name); pairInfo.append(label, count); pairCell.append(pairInfo); tableRow.append(pairCell, historyCell);
          for (const [side, countValue] of [['LONG', row.long], ['SHORT', row.short]]) {
            const field = document.createElement('td');
            const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.max = String(side === 'LONG' ? row.finalistLong : row.finalistShort); input.step = '1'; input.value = String(countValue); input.dataset.portfolioPair = row.pair; input.dataset.portfolioSide = side; input.setAttribute('aria-label', `${row.pair}: максимум ${side}`);
            input.addEventListener('input', () => { row[side.toLowerCase()] = Number(input.value); updateControls(); }); field.append(input); tableRow.append(field);
          }
          container.append(tableRow);
        }
      };
      const setLocked = (locked) => {
        state.locked = locked;
        const form = query('#portfolio-launch-form');
        form?.querySelectorAll('input').forEach((input) => { input.disabled = locked; });
        const fieldsets = form?.querySelectorAll('fieldset'); fieldsets?.forEach((fieldset) => { fieldset.disabled = locked; });
        if (prepareButton) prepareButton.disabled = locked || state.readiness?.preparation?.state === 'READY';
        runButton.disabled = locked || !(state.readiness?.stage1?.enabled === true);
        setBadge('#portfolio-lock-badge', locked ? 'FROZEN' : 'EDITABLE', locked ? 'pending' : 'ready');
        updateControls();
      };
      const updateControls = () => {
        const launch = portfolioLaunchForm();
        const preparationState = state.readiness?.preparation?.state;
        if (formStatus && !state.locked) formStatus.textContent = launch.valid ? 'Кампания готова к фиксации.' : (launch.invalidPairLimits.length ? `Лимит превышает доступное число финалистов: ${launch.invalidPairLimits.join(', ')}.` : 'Заполните обязательные поля и исправьте недопустимые лимиты.');
        if (runButton) runButton.disabled = state.locked || !launch.valid;
        const history = state.readiness?.finalist_history || {};
        for (const row of launch.rows) {
          const note = query('#portfolio-pairs')?.querySelector(`[data-portfolio-history="${typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(row.pair) : row.pair}"]`);
          if (note) {
            const parts = portfolioSelectionHelpers.labelParts(portfolioSelectionHelpers.pairWindow(row, history));
            const days = document.createElement('strong'); days.textContent = parts ? parts.days : '—';
            note.replaceChildren(days, document.createTextNode(parts?.range ? ` · ${parts.range}` : ''));
          }
        }
        const summaryNode = query('#portfolio-selection-summary');
        if (summaryNode) {
          const selection = portfolioSelectionHelpers.summary(launch.selectedPairs, history, launch.profiles);
          summaryNode.textContent = selection.pairs
            ? `Выбрано пар: ${selection.pairs} · финалистов: ${selection.finalists} · совместная история: ${portfolioSelectionHelpers.label(selection.window)} · расчёт ${portfolioSelectionHelpers.duration(selection.etaSeconds)} (оценка)`
            : 'Выбрано пар: 0';
        }
        const combinationNode = query('#portfolio-combination-count');
        if (combinationNode) {
          const combinations = launch.selectedPairs.length ? portfolioCombinationCount(launch.selectedPairs) : 0n;
          combinationNode.textContent = combinations === 0n ? '' : `Комбинаций финалистов: ${combinations.toLocaleString('ru-RU')} · отбор: MILP-фронтир по уровням банка — до «Максимум кандидатов» разных портфелей на профиль, каждый с точным расчётом`;
        }
        const jobTerminal = terminal(state.job);
        if (prepareButton) { prepareButton.textContent = preparationState === 'READY' ? 'Готово' : (preparationState === 'ERROR' ? 'Повторить' : 'Подготовить данные финалистов'); prepareButton.disabled = state.locked || preparationState === 'READY' || preparationState === 'PREPARING'; }
        if (newButton) newButton.disabled = preparationState === 'PREPARING' || !jobTerminal;
        if (cancelButton) cancelButton.disabled = !state.activeJobId || jobTerminal || ['CANCEL_REQUESTED', 'CANCELLING'].includes(statusOf(state.job));
        renderStage2Candidates();
        const stage2Ready = stage2Eligible() && state.stage2Campaign === state.job?.campaign_id && state.stage2Selected.size > 0;
        if (stage2Submit) {
          stage2Submit.disabled = !stage2Ready;
          stage2Submit.setAttribute('aria-disabled', String(!stage2Ready));
          if (stage2Ready) stage2Submit.removeAttribute('tabindex'); else stage2Submit.setAttribute('tabindex', '-1');
        }
        if (state.job?.kind !== 'TESTER_SUBMISSION') {
          if (stage2State) setBadge('#portfolio-stage2-state', stage2Ready ? 'READY' : 'DISABLED', stage2Ready ? 'ready' : 'pending');
          if (stage2Reason) stage2Reason.textContent = stage2Eligible()
            ? `Выбрано портфелей для теста: ${state.stage2Selected.size} из ${state.stage2Candidates.length || state.job.executables_count}.`
            : 'Доступно после успешного расчёта и подтверждения состава портфелей.';
        }
      };
      const renderJournal = (job) => {
        const journal = query('#portfolio-journal'); if (!journal) return; journal.replaceChildren();
        const entries = portfolioValues(Array.isArray(job?.journal) && job.journal.length ? job.journal : job?.diagnostics);
        for (const entry of entries) { const line = document.createElement('p'); const severity = String(entry?.severity || entry?.level || 'INFO').toLowerCase(); const message = portfolioReasonHelpers.humanize(entry?.text || entry?.message || entry || ''); line.className = `is-${severity}`; line.textContent = `${entry?.timestamp_utc || entry?.created_at || ''} ${entry?.stage || ''} ${entry?.code || ''} ${message}`.trim(); journal.append(line); }
      };
       const renderResults = async (job) => {
        const summary = query('#portfolio-summary'); const exclusions = query('#portfolio-exclusions'); const portfolioXlsx = query('#portfolio-xlsx');
        if (portfolioXlsx) { portfolioXlsx.hidden = true; portfolioXlsx.removeAttribute('href'); }
        const succeeded = (job && job.status === 'SUCCEEDED') || statusOf(job) === 'SUCCEEDED';
        if (!job?.campaign_id || !succeeded) { if (summary) summary.textContent = 'Результаты появятся только после успешного завершения.'; if (exclusions) exclusions.replaceChildren(); return; }
        try {
          const result = await requestJson(`/api/v2/portfolio/campaigns/${encodeURIComponent(job.campaign_id)}/results`);
          if (state.stage2Campaign !== job.campaign_id) { state.stage2Campaign = job.campaign_id; state.stage2Selected = new Set(); }
          state.stage2Candidates = Array.isArray(result.candidates) ? result.candidates : [];
          state.stage2Version += 1;
          updateControls();
           const values = result.summary || result;
           const optimizerStatus = String(values?.optimizer_status || '').toUpperCase();
           if (optimizerStatus === 'PARTIAL') setBadge('#portfolio-result-state', 'PARTIAL', 'pending');
           else if (optimizerStatus === 'FAIL') setBadge('#portfolio-result-state', 'FAILED', 'pending');
          if (summary) {
            summary.replaceChildren();
            const membersKnown = Array.isArray(values['Weighted members']);
            const members = membersKnown ? values['Weighted members'] : [];
            const acceptedCount = Math.max(0, Number(values.candidates_selected) || 0);
            const zeroWeightCount = Math.max(0, acceptedCount - members.length);
            const accepted = document.createElement('p'); accepted.className = 'portfolio-result-accepted';
            accepted.textContent = `Взято финалистов в расчёт: ${portfolioResultHelpers.metric(acceptedCount, 0)}`;
            if (membersKnown) accepted.textContent += ` · В вариант №1 вошло: ${members.length}`;
            if (membersKnown && zeroWeightCount) accepted.textContent += ` · Без позиции: ${zeroWeightCount}`;
            summary.append(accepted);

            const profileCounts = values.variants_by_profile && typeof values.variants_by_profile === 'object' ? values.variants_by_profile : {};
            if (Object.keys(profileCounts).length) {
              const profiles = document.createElement('div'); profiles.className = 'portfolio-result-profiles';
              const heading = document.createElement('strong'); heading.textContent = 'Варианты по профилям'; profiles.append(heading);
              for (const [profileName, count] of Object.entries(profileCounts)) {
                const item = document.createElement('span'); item.textContent = `${portfolioResultHelpers.profile(profileName)}: ${portfolioResultHelpers.metric(count, 0)}`; profiles.append(item);
              }
              summary.append(profiles);
            }

            const candidateId = values['Weighted candidate ID'];
            const limiterDefinitions = [
              ['P30 limiter USDT/30d', 'PnL с ограничителем за 30 дней', (value) => portfolioResultHelpers.metric(value, 2, ' USDT')],
              ['Reserve before limiter', 'Запас до ограничителя', (value) => portfolioResultHelpers.metric(value, 2, '%')],
              ['Reserve after limiter', 'Запас после ограничителя', (value) => portfolioResultHelpers.metric(value, 2, '%')],
              ['Bottleneck', 'Ограничивающий фактор', String],
            ];
            const ddLimit = portfolioResultHelpers.metric(values['Profile DD limit %'], 2, '%') || '—';
            const targetBank = values['Target bank USDT'] ?? values['B available USDT'];
            const metricGroups = [
              {
                className: 'portfolio-result-metric-group portfolio-result-bank-group',
                title: 'Банки',
                items: [
                  ['B saturation USDT', 'Насыщение', (value) => portfolioResultHelpers.metric(value, 2, ' USDT')],
                  ['Target bank USDT', 'Целевой банк', (value) => portfolioResultHelpers.bank(value ?? targetBank)],
                  ['Historical bank USDT', `Минимальный банк для DD ≤ ${ddLimit} на истории`, (value) => portfolioResultHelpers.metric(value, 2, ' USDT')],
                  ['Stress bank P95 USDT', `Банк для DD ≤ ${ddLimit} в 95% стресс-сценариев`, (value) => portfolioResultHelpers.metric(value, 2, ' USDT')],
                  ['B margin USDT', 'Банк для профильных лимитов', (value) => portfolioResultHelpers.metric(value, 2, ' USDT')],
                ],
              },
              {
                className: 'portfolio-result-metric-group portfolio-result-drawdown-group',
                title: 'Просадки',
                items: [
                  ['MaxDD SUM USDT', 'MaxDD SUM', (value) => portfolioResultHelpers.metric(value, 2, ' USDT')],
                  ['MaxDD %', 'Исторический рассчитанный DD', (value) => portfolioResultHelpers.metric(value, 1, '%')],
                  ['CDaR peak80 USDT', 'Средняя глубина худших 20% просадок', (value) => portfolioResultHelpers.amountRatios(value, values['CDaR peak80 target %'], values['CDaR peak80 saturation %'])],
                  ['CDaR peak90 USDT', 'Средняя глубина худших 10% просадок', (value) => portfolioResultHelpers.amountRatios(value, values['CDaR peak90 target %'], values['CDaR peak90 saturation %'])],
                ],
              },
              {
                className: 'portfolio-result-metric-group portfolio-result-capital-group',
                title: 'Капитал и результат',
                items: [
                  ['P30 common USDT/30d', 'PnL', (value) => portfolioResultHelpers.metric(value, 2, ' USDT')],
                  ['IM all USDT', 'IM', (value) => portfolioResultHelpers.amountRatios(value, values['IM target %'], values['IM saturation %'])],
                  ['MM all USDT', 'MM', (value) => portfolioResultHelpers.amountRatios(value, values['MM target %'], values['MM saturation %'])],
                ],
              },
            ];
            if (Number(values['Limiter L']) > 0) metricGroups.push({
              className: 'portfolio-result-metric-group portfolio-result-limiter-group', title: 'Ограничитель', items: limiterDefinitions,
            });
            if (members.length || metricGroups.some(({ items }) => items.some(([key]) => portfolioResultHelpers.known(values[key])))) {
              const candidate = document.createElement('section'); candidate.className = 'portfolio-result-candidate';
              const heading = document.createElement('h3'); heading.textContent = 'Вариант №1'; candidate.append(heading);
              const ratioLegend = document.createElement('p'); ratioLegend.className = 'portfolio-result-note'; ratioLegend.textContent = 'В парах с процентами порядок всегда один: целевой банк / банк насыщения.'; candidate.append(ratioLegend);
              if (Number(values.variants_created) > 1) {
                const hint = document.createElement('p'); hint.className = 'helper'; hint.textContent = 'Остальные варианты находятся в XLSX.'; candidate.append(hint);
              }
              const metrics = document.createElement('dl'); metrics.className = 'portfolio-result-metrics';
              for (const groupDefinition of metricGroups) {
                const group = document.createElement('section'); group.className = groupDefinition.className;
                const groupTitle = document.createElement('h4'); groupTitle.textContent = groupDefinition.title; group.append(groupTitle);
                 const groupMetrics = document.createElement('dl'); groupMetrics.className = 'portfolio-result-metric-group-grid';
                 for (const [key, labelText, format] of groupDefinition.items) {
                   const rawValue = key === 'Target bank USDT' ? targetBank : values[key];
                   const item = document.createElement('div');
                   const label = document.createElement('dt'); label.textContent = labelText;
                   const value = document.createElement('dd'); value.textContent = format(rawValue) || '—';
                  item.append(label, value); groupMetrics.append(item);
                }
                if (groupMetrics.children.length) { group.append(groupMetrics); metrics.append(group); }
              }
              candidate.append(metrics);
              if (members.length) {
                const membersHeading = document.createElement('h4'); membersHeading.textContent = 'Состав варианта'; candidate.append(membersHeading);
                 const table = document.createElement('table'); table.className = 'portfolio-result-members';
                 const head = document.createElement('thead'); const headRow = document.createElement('tr');
                  for (const labelText of ['Пара / сторона', 'Стратегия / результат', 'Полный номинал позиции', 'Множитель пары', 'max_balance', 'Распределение входов', 'Плечо']) {
                    const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = labelText; headRow.append(cell);
                  }
                  head.append(headRow); table.append(head);
                const body = document.createElement('tbody');
                for (const member of members) {
                  const row = document.createElement('tr');
                  const cells = [
                    `${member.pair || '—'} / ${member.direction || '—'}`,
                    `${member.strategy_id ?? '—'} / ${member.result_id ?? '—'}`,
                    portfolioResultHelpers.metric(member.position_usdt, 2, ' USDT'),
                    portfolioResultHelpers.metric(member.bank_share_pct, 2, '%'),
                     portfolioResultHelpers.metric(member.leverage, 2),
                   ];
                   cells.splice(0, cells.length,
                     cells[0], cells[1], cells[2],
                     portfolioResultHelpers.metric(member.balance_percentage ?? member.pair_multiplier_pct ?? member.bank_share_pct, 2, '%'),
                     portfolioResultHelpers.metric(member.max_balance, 2, ' USDT'),
                     portfolioResultHelpers.orderPercentages(member.entry_order_percentages),
                     cells[4],
                   );
                  for (const value of cells) { const cell = document.createElement('td'); cell.textContent = value || '—'; row.append(cell); }
                  body.append(row);
                }
                table.append(body); candidate.append(table);
              }
              summary.append(candidate);
            }
            for (const warning of portfolioValues(values.warnings)) {
              const notice = document.createElement('p'); notice.className = 'portfolio-result-warning'; notice.textContent = portfolioResultHelpers.warning(warning); summary.append(notice);
            }
            if (!portfolioResultHelpers.known(values['Joint status'])) {
              const notice = document.createElement('p'); notice.className = 'portfolio-result-note'; notice.textContent = 'Совместный тест ещё не выполнялся.'; summary.append(notice);
            }

            const details = document.createElement('details'); details.className = 'portfolio-result-details';
            const toggle = document.createElement('summary'); toggle.textContent = 'Технические данные'; details.append(toggle);
            for (const [labelText, value] of [['Кампания', values.campaign_id || result.campaign_id], ['ID варианта', candidateId]]) {
              if (!portfolioResultHelpers.known(value)) continue;
              const row = document.createElement('div'); const label = document.createElement('strong'); const code = document.createElement('code');
              label.textContent = labelText; code.textContent = String(value); row.append(label, code); details.append(row);
            }
            summary.append(details);
          }
            if (exclusions) { exclusions.replaceChildren(); for (const item of portfolioValues(result.exclusions || result.blockers)) { const row = document.createElement('div'); row.className = 'portfolio-exclusion'; row.textContent = portfolioReasonHelpers.humanize(typeof item === 'string' ? item : `${item.code || item.stage || 'Исключено'}: ${item.message || item.reason || ''}`); exclusions.append(row); } }
          if (portfolioXlsx && result.workbook_available === true) { portfolioXlsx.href = `/api/v2/portfolio/campaigns/${encodeURIComponent(job.campaign_id)}/stage1.xlsx`; portfolioXlsx.hidden = false; }
        } catch (error) { if (summary) summary.textContent = `Результаты недоступны: ${portfolioErrorMessage(error)}`; }
      };
      const renderStage2Candidates = () => {
        const host = query('#portfolio-stage2-candidates');
        if (!host) return;
        // Rows belong to one Campaign; a different displayed Campaign shows nothing until its own rows load.
        const rows = stage2Eligible() && state.stage2Campaign === state.job?.campaign_id ? state.stage2Candidates : [];
        const token = rows.length ? `${state.stage2Campaign}:${state.stage2Version}` : '';
        if (host.dataset.token === token) {
          host.querySelectorAll('input[data-candidate-id]').forEach((box) => { box.checked = state.stage2Selected.has(box.dataset.candidateId); });
          const all = host.querySelector('input[data-candidate-all]'); if (all) all.checked = rows.length > 0 && state.stage2Selected.size === rows.length;
          return;
        }
        host.dataset.token = token;
        host.replaceChildren();
        if (!rows.length) return;
        const number = (value, digits = 0) => (value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('ru-RU', { maximumFractionDigits: digits, minimumFractionDigits: digits }));
        const table = document.createElement('table'); table.className = 'portfolio-stage2-table';
        const caption = document.createElement('caption'); caption.className = 'sr-only'; caption.textContent = 'Портфели этапа 1 для теста'; table.append(caption);
        const head = document.createElement('tr');
        const allCell = document.createElement('th'); allCell.scope = 'col';
        const all = document.createElement('input'); all.type = 'checkbox'; all.dataset.candidateAll = 'true'; all.setAttribute('aria-label', 'Выбрать все портфели');
        all.addEventListener('change', () => { state.stage2Selected = new Set(all.checked ? rows.map((row) => row.candidate_id) : []); updateControls(); });
        allCell.append(all); head.append(allCell);
        for (const title of ['№', 'Профиль', 'Позиций', 'Банк, USDT', 'P30, USDT/30д', 'Банк DD, USDT', 'Стресс P95, USDT', 'Маржа, USDT', 'CDaR 20%, USDT']) {
          const cell = document.createElement('th'); cell.scope = 'col'; cell.textContent = title; head.append(cell);
        }
        const thead = document.createElement('thead'); thead.append(head); table.append(thead);
        const body = document.createElement('tbody');
        rows.forEach((row, index) => {
          const tableRow = document.createElement('tr');
          const boxCell = document.createElement('td');
          const box = document.createElement('input'); box.type = 'checkbox'; box.dataset.candidateId = row.candidate_id; box.checked = state.stage2Selected.has(row.candidate_id);
          box.setAttribute('aria-label', `Выбрать портфель ${index + 1}`);
          box.addEventListener('change', () => { if (box.checked) state.stage2Selected.add(row.candidate_id); else state.stage2Selected.delete(row.candidate_id); updateControls(); });
          boxCell.append(box); tableRow.append(boxCell);
          for (const value of [String(index + 1), row.profile || '—', number(row.positions), number(row.required_bank_usdt), number(row.p30_usdt_30d), number(row.historical_bank_usdt), number(row.stress_bank_usdt), number(row.margin_bank_usdt), number(row.cdar80_usdt, 2)]) {
            const cell = document.createElement('td'); cell.textContent = value; tableRow.append(cell);
          }
          body.append(tableRow);
        });
        table.append(body);
        const wrap = document.createElement('div'); wrap.className = 'portfolio-stage2-wrap'; wrap.append(table);
        host.append(wrap);
        all.checked = rows.length > 0 && state.stage2Selected.size === rows.length;
      };
      const renderStage2 = (job) => {
        if (!stage2Progress || !stage2Results || !stage2State || !stage2Reason) return;
        if (job?.kind !== 'TESTER_SUBMISSION') {
          stage2Progress.textContent = '';
          stage2Results.replaceChildren();
          return;
        }
        const statusName = statusOf(job);
        const batch = job.batch || {};
        const total = Number.isSafeInteger(batch.total) ? batch.total : 0;
        const current = Number.isInteger(batch.current_index) ? batch.current_index + 1 : 0;
        const completed = Number.isSafeInteger(batch.completed) ? batch.completed : 0;
        const overall = Number.isFinite(Number(job.overall_percent)) ? Math.max(0, Math.min(100, Number(job.overall_percent))) : 0;
        const stageName = job.stage?.name || statusName;
        stage2Progress.textContent = `${current} / ${total} · ${completed} завершено · ${portfolioStageLabel(stageName)} · ${Math.round(overall)}% · ${portfolioStatusLabel(statusName)}`;
        setBadge('#portfolio-stage2-state', statusName, statusName === 'SUCCEEDED' ? 'ready' : (terminal(job) ? 'pending' : 'running'));
        const diagnostic = Array.isArray(job.diagnostics) ? job.diagnostics.find((item) => item && typeof item === 'object') : null;
        if (statusName === 'SUCCEEDED') stage2Reason.textContent = 'Тестирование портфелей завершено.';
        else if (statusName === 'FAILED') stage2Reason.textContent = `Тестирование остановлено: ${diagnostic?.code || 'PORTFOLIO_JOB_STAGE2_FAILED'}`;
        else if (statusName === 'CANCELLED') stage2Reason.textContent = 'Тестирование отменено; завершённые результаты сохранены.';
        else if (statusName === 'INTERRUPTED') stage2Reason.textContent = 'Задание прервано после перезапуска; продолжение не выполняется.';
        else stage2Reason.textContent = 'Портфели передаются тестеру поочерёдно.';
        stage2Results.replaceChildren();
        const results = Array.isArray(job.results) ? job.results : [];
        results.forEach((result, index) => {
          const row = document.createElement('li');
          const reportFolder = typeof result?.report_folder === 'string' ? result.report_folder : String(result?.candidate_id || '');
          const strategyCount = Array.isArray(result?.strategy_names) ? result.strategy_names.length : 0;
          const candidate = document.createElement('code'); candidate.textContent = reportFolder;
          row.append(document.createTextNode(`${index + 1}. Папка отчёта `), candidate, document.createTextNode(` · ${strategyCount} стратегий`));
          stage2Results.append(row);
        });
      };
      const renderJob = (job) => {
        state.job = job || null;
        const statusName = statusOf(job); const stage = job?.stage || {}; const overallPercent = Number(job?.overall_percent); const stagePercent = Number(stage.percent); const overallKnown = Number.isFinite(overallPercent); const stageKnown = Number.isFinite(stagePercent) && stage.total !== undefined && Number.isFinite(Number(stage.total)); const stageIndeterminate = !!job && !stageKnown;
        if (state.renderedJobId !== job?.job_id) { state.renderedJobId = job?.job_id || ''; state.renderedPercent = 0; state.settingsChanged = false; }
        updateFreezeStatus(job);
        text('#portfolio-job-status', job ? freezeStatus(statusName, job.campaign_id) : 'Нет активного расчёта.');
        text('#portfolio-progress-text', job ? `${portfolioStageLabel(stage.name || stage.index || statusName)}${stageIndeterminate ? ' · точный прогресс неизвестен, доступно время выполнения' : ` · ${Math.max(0, Math.min(100, Math.round(stagePercent)))}%`}` : 'Нет активного расчёта.');
        const bar = query('#portfolio-progress-bar'); if (bar) { bar.classList.toggle('is-running', stageIndeterminate && !terminal(job)); if (overallKnown) { state.renderedPercent = Math.max(state.renderedPercent, Math.max(0, Math.min(100, overallPercent))); bar.style.width = `${Math.round(state.renderedPercent)}%`; } else if (stageKnown) { state.renderedPercent = Math.max(state.renderedPercent, Math.max(0, Math.min(100, stagePercent))); bar.style.width = `${Math.round(state.renderedPercent)}%`; } else if (!job || terminal(job)) bar.style.width = statusName === 'SUCCEEDED' ? '100%' : '0%'; }
        const liveProgress = job?.progress || {};
        const liveSubstage = String(liveProgress.substage || stage.name || stage.index || statusName || '');
        const liveNow = performance.now();
        const liveElapsedBase = Number(liveProgress.elapsed_seconds);
        const liveHeartbeatBase = Number(liveProgress.heartbeat_age_seconds);
        if (state.renderedJobId !== job?.job_id) { state.renderedJobId = job?.job_id || ''; state.renderedPercent = 0; state.renderedSubstage = ''; state.settingsChanged = false; }
        if (state.renderedSubstage !== liveSubstage) { state.renderedSubstage = liveSubstage; state.renderedPercent = 0; state.progressAnchor = liveNow; state.progressBaseElapsed = Number.isFinite(liveElapsedBase) ? Math.max(0, liveElapsedBase) : 0; }
        if (Number.isFinite(liveHeartbeatBase)) { state.heartbeatAnchor = liveNow; state.heartbeatBaseAge = Math.max(0, liveHeartbeatBase); }
        const liveElapsed = state.progressBaseElapsed + Math.max(0, (liveNow - state.progressAnchor) / 1000);
        const liveHeartbeatAge = state.heartbeatBaseAge + Math.max(0, (liveNow - state.heartbeatAnchor) / 1000);
        const liveCompleted = Number(liveProgress.completed); const liveTotal = Number(liveProgress.total);
        const liveDeterminate = Number.isFinite(liveCompleted) && Number.isFinite(liveTotal) && liveTotal > 0 && liveCompleted >= 0 && liveCompleted <= liveTotal && liveProgress.indeterminate !== true;
        const livePercent = liveDeterminate ? Math.max(0, Math.min(100, liveCompleted * 100 / liveTotal)) : 0;
        const liveEta = Number(liveProgress.eta_seconds); const liveStale = liveHeartbeatAge > 30; const liveEtaUnavailable = liveStale || terminal(job);
        const liveDetail = liveProgress.detail ? ` · ${liveProgress.detail}` : '';
        const liveCount = liveDeterminate ? ` · ${Math.round(liveCompleted)} / ${Math.round(liveTotal)} (${Math.round(livePercent)}%)` : ' · progress indeterminate';
        const liveEtaText = !liveEtaUnavailable && Number.isFinite(liveEta) ? ` · ETA ${formatDuration(liveEta)}` : '';
        const liveEtaUnknownText = liveEtaUnavailable ? ' · stalled / ETA unknown' : '';
        const liveStaleText = liveStale ? ' · heartbeat stale (informational)' : '';
        text('#portfolio-progress-text', job ? `${portfolioStageLabel(liveSubstage)}${liveCount} · elapsed ${formatDuration(liveElapsed)}${liveEtaText}${liveEtaUnknownText} · heartbeat ${formatDuration(liveHeartbeatAge)}${liveDetail}${liveStaleText}` : 'Нет активного расчёта.');
        const liveBar = query('#portfolio-progress-bar'); if (liveBar) {
          liveBar.classList.toggle('is-running', !!job && !liveDeterminate && !terminal(job));
          liveBar.setAttribute('aria-busy', String(!!job && !liveDeterminate && !terminal(job)));
          if (liveDeterminate) { liveBar.setAttribute('role', 'progressbar'); liveBar.setAttribute('aria-valuemin', '0'); liveBar.setAttribute('aria-valuemax', String(Math.round(liveTotal))); liveBar.setAttribute('aria-valuenow', String(Math.round(liveCompleted))); state.renderedPercent = livePercent; liveBar.style.width = `${Math.round(livePercent)}%`; }
          else { liveBar.removeAttribute('role'); liveBar.removeAttribute('aria-valuemin'); liveBar.removeAttribute('aria-valuemax'); liveBar.removeAttribute('aria-valuenow'); liveBar.style.width = (!job || terminal(job)) && statusName === 'SUCCEEDED' ? '100%' : '0%'; }
        }
        setBadge('#portfolio-job-state', job ? statusName : 'NO JOB', statusName === 'SUCCEEDED' ? 'ready' : (job && !terminal(job) ? 'running' : 'pending'));
        setBadge('#portfolio-result-state', statusName === 'SUCCEEDED' ? 'SUCCEEDED' : (job ? statusName : 'WAITING'), statusName === 'SUCCEEDED' ? 'ready' : 'pending');
        renderJournal(job); updateControls(); renderStage2(job);
        if (statusName === 'SUCCEEDED') renderResults(job); else { const xlsx = query('#portfolio-xlsx'); if (xlsx) { xlsx.hidden = true; xlsx.removeAttribute('href'); } }
      };
      const pollPortfolioJob = async () => {
        if (!state.activeJobId) return;
        try {
          const payload = await requestJson(`${portfolioJobEndpoint}${encodeURIComponent(state.activeJobId)}`);
          const job = payload.job || payload; if (job?.job_id) state.activeJobId = job.job_id; renderJob(job);
          if (terminal(job)) {
            window.clearInterval(state.poller); state.poller = 0;
            if (job?.kind === 'FINALIST_PREPARATION') {
              state.activeJobId = ''; state.job = null; renderJob(null); setLocked(false);
              try { renderReadiness(await requestJson('/api/v2/portfolio/readiness')); } catch (_) { /* next refresh retains the server state. */ }
            }
            else setLocked(false);
            updateControls();
          }
        } catch (_) { /* requestJson exposes the safe error while polling remains server-only. */ }
      };
      const startPortfolioPolling = () => { window.clearInterval(state.poller); state.poller = window.setInterval(pollPortfolioJob, 1000); pollPortfolioJob(); };
      const recoverPortfolioJob = async (keepLocked = false) => {
        const active = await requestJson('/api/v2/portfolio/jobs/active'); const candidate = active.job;
        if (!candidate?.job_id) { state.activeJobId = ''; renderJob(null); if (!keepLocked) setLocked(false); window.clearInterval(state.poller); state.poller = 0; return; }
        state.activeJobId = candidate.job_id; setLocked(!terminal(candidate)); await pollPortfolioJob(); if (terminal(state.job)) setLocked(false); else startPortfolioPolling();
      };
      const refreshPortfolio = async () => {
        if (state.refreshPromise) return state.refreshPromise;
        state.refreshPromise = Promise.allSettled([requestJson('/api/v2/portfolio/readiness'), recoverPortfolioJob()]).then(([readiness]) => { if (readiness.status === 'fulfilled') renderReadiness(readiness.value); }).finally(() => { state.refreshPromise = null; });
        return state.refreshPromise;
      };
      prepareButton?.addEventListener('click', async () => {
        if (prepareButton.disabled) return;
        setLocked(true);
        if (formStatus) formStatus.textContent = 'Подготавливаются данные текущих FINALIST…';
        try {
          const result = await requestJson('/api/v2/portfolio/finalist-inputs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
          if (!result.job_id) {
            renderReadiness(await requestJson('/api/v2/portfolio/readiness'));
            setLocked(false);
            if (formStatus) formStatus.textContent = 'Данные текущих FINALIST уже подготовлены.';
            return;
          }
          state.activeJobId = result.job_id;
          renderJob({ job_id: state.activeJobId, kind: 'FINALIST_PREPARATION', status: result.status || 'QUEUED' });
          startPortfolioPolling();
        } catch (error) {
          setLocked(false);
          if (formStatus) formStatus.textContent = portfolioErrorMessage(error);
        }
      });
      runButton?.addEventListener('click', async () => {
        const launch = portfolioLaunchForm();
        if (!launch.valid) { if (formStatus) formStatus.textContent = 'Перед расчётом устраните блокировки входных данных и формы.'; updateControls(); return; }
        const selectedPairs = launch.selectedPairs.map((row) => ({ pair: row.pair, max_finalist_long: row.long, max_finalist_short: row.short }));
        const profiles = launch.profiles.map((profile) => {
          return {
            profile_id: profile.profile.toUpperCase(),
            bank_available_usdt: String(profile.bankAvailable).trim() || null,
            max_candidates: Number(profile.candidates),
          };
        });
        setLocked(true); if (formStatus) formStatus.textContent = 'Кампания зафиксирована, создаётся задание сервера…';
        try {
          const result = await requestJson('/api/v2/portfolio/campaigns', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pairs: selectedPairs, profiles, expected_config_digest: state.configDigest }) });
          state.activeJobId = result.job_id || result.job?.job_id || ''; renderJob(result.job || { job_id: state.activeJobId, campaign_id: result.campaign_id, status: result.status || 'QUEUED', config_digest: result.config_digest }); if (state.activeJobId) startPortfolioPolling();
          if (formStatus) formStatus.textContent = 'Кампания зафиксирована и поставлена в очередь на сервере.';
        } catch (error) {
          const activeConflict = ['PORTFOLIO_JOB_ACTIVE_DUPLICATE', 'PORTFOLIO_JOB_BUSY'].includes(error?.code);
          if (activeConflict) {
            setLocked(true);
            try { await recoverPortfolioJob(true); } catch (_) { /* retain the frozen form until the server job can be recovered. */ }
            if (formStatus) formStatus.textContent = `${error.code}: активный расчёт уже существует.`;
            return;
          }
          setLocked(false);
          if (formStatus) formStatus.textContent = portfolioErrorMessage(error);
        }
      });
      stage2Submit?.addEventListener('click', async () => {
        if (stage2Submit.disabled || !stage2Eligible() || state.stage2Campaign !== state.job?.campaign_id || !(state.stage2Selected.size > 0)) return;
        const campaignId = state.job.campaign_id;
        // Artifact order is kept; the checkboxes only choose which portfolios run.
        const selectedIds = state.stage2Candidates.map((row) => row.candidate_id).filter((id) => state.stage2Selected.has(id));
        const count = selectedIds.length;
        stage2Submit.disabled = true;
        stage2Submit.setAttribute('aria-disabled', 'true');
        const confirmed = window.confirm(`Отправить на тест ${count} портфелей кампании ${campaignId}?`);
        if (!confirmed) { updateControls(); return; }
        try {
          const result = await requestJson(`/api/v2/portfolio/campaigns/${encodeURIComponent(campaignId)}/tester-submissions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirmed: true, campaign_id: campaignId, candidate_ids: selectedIds }),
          });
          if (!result.job_id) throw new Error('Не удалось создать задание передачи тестеру.');
          state.activeJobId = result.job_id;
          renderJob({
            job_id: result.job_id,
            campaign_id: campaignId,
            kind: 'TESTER_SUBMISSION',
            status: result.status || 'QUEUED',
            overall_percent: 0,
            stage: { name: 'PREPARE' },
            results: [],
            batch: { current_index: null, total: count, completed: 0 },
          });
          startPortfolioPolling();
        } catch (error) {
          updateControls();
          if (stage2Reason) stage2Reason.textContent = portfolioErrorMessage(error);
        }
      });
      cancelButton?.addEventListener('click', async () => {
        if (!state.activeJobId || ['CANCEL_REQUESTED', 'CANCELLING'].includes(statusOf(state.job))) return;
        cancelButton.disabled = true;
        try { const result = await requestJson(`${portfolioJobEndpoint}${encodeURIComponent(state.activeJobId)}/cancel`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); renderJob(result.job || { ...state.job, status: result.status || 'CANCEL_REQUESTED' }); }
        catch (error) { if (formStatus) formStatus.textContent = portfolioErrorMessage(error); await pollPortfolioJob(); }
      });
      newButton?.addEventListener('click', async () => { if (!terminal(state.job)) return; state.activeJobId = ''; state.job = null; state.stage2Candidates = []; state.stage2Selected = new Set(); state.stage2Campaign = ''; state.stage2Version += 1; state.settingsChanged = false; renderJob(null); setLocked(false); try { renderReadiness(await requestJson('/api/v2/portfolio/readiness')); if (formStatus) formStatus.textContent = 'Новая кампания готова.'; } catch (error) { if (formStatus) formStatus.textContent = portfolioErrorMessage(error); } });
      ['aggressive', 'balanced', 'conservative'].forEach((profile) => query(`#portfolio-profile-${profile}`)?.addEventListener('change', updateControls));
      query('#portfolio-select-all')?.addEventListener('click', () => { state.pairRows.forEach((row) => { row.selected = true; }); renderPairs(); updateControls(); });
      query('#portfolio-select-n')?.addEventListener('click', () => {
        const input = query('#portfolio-select-n-value'); const count = Number(input?.value);
        if (!Number.isSafeInteger(count) || count < 1) { input?.setAttribute('aria-invalid', 'true'); input?.focus(); return; }
        input?.removeAttribute('aria-invalid');
        state.pairRows.forEach((row) => Object.assign(row, portfolioSelectionHelpers.limitRow(row, count)));
        renderPairs(); updateControls();
      });
      query('#portfolio-pair-sort')?.addEventListener('change', (event) => { state.pairSort = event.target.value; renderPairs(); updateControls(); });
      query('#portfolio-select-maximum')?.addEventListener('click', () => { state.pairRows.forEach((row) => { row.long = Math.max(0, Number(row.finalistLong) || 0); row.short = Math.max(0, Number(row.finalistShort) || 0); row.selected = row.long > 0 || row.short > 0; }); renderPairs(); updateControls(); });
      query('#portfolio-select-none')?.addEventListener('click', () => { state.pairRows.forEach((row) => { row.selected = false; }); renderPairs(); updateControls(); });
      query('#portfolio-profiles')?.querySelectorAll('input').forEach((input) => { input.addEventListener('input', updateControls); input.addEventListener('change', updateControls); });
      loadPortfolioScreen.state = state;
      state.refresh = refreshPortfolio;
    }
    loadPortfolioScreen.state.refresh();
  }

  function loadPortfolioSettings(force = false) {
    if (!loadPortfolioSettings.state) {
       const { clone, profiles, riskFields, riskDefaults, settingsPatch, validSettingsDocument, weightedSearchKeys, revealInvalidField, settingsValidationFeedback } = portfolioSettingsHelpers;
      const state = { digest: null, document: null, readOnly: true, dirty: false, loading: null, conflictMessage: '' };
      const readOnlyStates = ['MISSING', 'INVALID', 'UNSUPPORTED_SCHEMA'];
      const query = (selector) => document.querySelector(selector);
      const form = query('#portfolio-settings-form'); const save = query('#portfolio-settings-save'); const reload = query('#portfolio-settings-reload'); const meta = query('#portfolio-settings-meta');
      const settingsStatusLabels = Object.freeze({ READY: 'ГОТОВО', INVALID: 'ОШИБКА', MISSING: 'ОТСУТСТВУЕТ', UNSUPPORTED_SCHEMA: 'НЕПОДДЕРЖИВАЕМАЯ СХЕМА' });
      const settingsStatusLabel = (value) => settingsStatusLabels[String(value ?? '').toUpperCase()] || String(value ?? '');
      const controls = () => form ? [...form.querySelectorAll('input, textarea')] : [];
      const linkDescriptions = () => controls().forEach((control) => { const description = control.closest('.field-group')?.querySelector('small'); if (description) { description.id = `${control.id}-description`; control.setAttribute('aria-describedby', description.id); } });
      linkDescriptions();
      const setBadge = (value, kind) => { const node = query('#portfolio-settings-state'); if (node) { node.className = `state-badge state-${kind}`; node.textContent = value; } };
        const weightedInput = (name) => query(`#portfolio-settings-weighted-${name.replaceAll('_', '-')}`);
       const renderDocument = () => {
          const doc = state.document; if (!doc) { controls().forEach((control) => { if (control.type === 'checkbox') control.checked = false; else control.value = ''; }); return; }
          const exchange = query('#portfolio-settings-exchange'); if (exchange) exchange.value = doc.inputs?.exchange || 'Bybit';   // Panel stage 5e-2
          const seed = query('#portfolio-settings-seed'); if (seed) seed.value = String(doc.search.seed);
          const minimumCommonDays = query('#portfolio-settings-minimum-common-days'); if (minimumCommonDays) minimumCommonDays.value = String(doc.search.composition.parameters.minimum_common_days);
          const budget = query('#portfolio-settings-max-enumerated-combinations'); if (budget) budget.value = String(doc.search.max_enumerated_combinations);
          for (const name of weightedSearchKeys) { const control = weightedInput(name); if (control) control.value = String(doc.search.weighted_search[name]); }
          const baseCoefficient = query('#portfolio-settings-lot-model-base-coefficient'); if (baseCoefficient) baseCoefficient.value = String(doc.liquidity.parameters.lot_model_base_coefficient);
          const shiftBonus = query('#portfolio-settings-lot-model-max-shift-bonus'); if (shiftBonus) shiftBonus.value = String(doc.liquidity.parameters.lot_model_max_shift_bonus);
           const rounding = query('#portfolio-settings-round-down-usdt'); if (rounding) rounding.value = String(doc.liquidity.round_down_usdt);
          const openFee = query('#portfolio-settings-open-fee-rate'); if (openFee) openFee.value = String(doc.margin?.parameters?.open_fee_rate ?? '');
          const closeFee = query('#portfolio-settings-close-fee-rate'); if (closeFee) closeFee.value = String(doc.margin?.parameters?.close_fee_rate ?? '');
         const coverage = query('#portfolio-settings-minimum-coverage-pct'); if (coverage) coverage.value = String(doc.liquidity.minimum_coverage_pct);
         const age = query('#portfolio-settings-maximum-age-hours'); if (age) age.value = String(doc.liquidity.maximum_age_hours);
         const lag = query('#portfolio-settings-archive-publication-lag-hours'); if (lag) lag.value = String(doc.liquidity.archive_publication_lag_hours);
         const weekendStart = query('#portfolio-settings-weekend-start-utc'); if (weekendStart) weekendStart.value = doc.liquidity.weekend_start_utc;
         const weekendEnd = query('#portfolio-settings-weekend-end-utc'); if (weekendEnd) weekendEnd.value = doc.liquidity.weekend_end_utc;
          const backfill = query('#portfolio-settings-backfill-write-enabled'); if (backfill) backfill.checked = !!doc.liquidity.backfill_write_enabled;
          const spreadBypass = query('#portfolio-settings-spread-history-bypass-pretest'); if (spreadBypass) spreadBypass.checked = !!doc.liquidity.spread_history_bypass_pretest;
         for (const profile of profiles) for (const field of riskFields) { const control = query(`#portfolio-settings-risk-${profile.toLowerCase()}-${field.replaceAll('_', '-')}`); if (control) control.value = String(doc.profiles?.[profile]?.[field] ?? riskDefaults[profile][field]); }
       };
      const setDisabled = (disabled) => { controls().forEach((control) => { control.disabled = disabled; }); if (save) save.disabled = disabled; };
      const show = (result) => {
         const stateName = result?.state || 'INVALID'; state.digest = stateName === 'READY' ? (result?.digest ?? null) : null; state.document = null; if (stateName === 'READY' && result?.document) { const candidate = clone(result.document); if (validSettingsDocument(candidate)) state.document = candidate; } state.dirty = false; state.readOnly = readOnlyStates.includes(stateName) || stateName !== 'READY' || !state.document;
        renderDocument(); clearInvalid(); setDisabled(state.readOnly); setBadge(settingsStatusLabel(stateName), stateName === 'READY' && !state.readOnly ? 'ready' : 'pending');
        if (meta) meta.textContent = state.readOnly ? (stateName === 'READY' ? 'Серверные настройки содержат недопустимые значения. Сохранение отключено.' : `${settingsStatusLabel(stateName)}: настройки доступны только для чтения.`) : (state.conflictMessage || `ГОТОВО · digest ${state.digest || '—'}`);
      };
      const load = async (preserveConflict = false) => {
        if (state.loading) return state.loading;
        if (!preserveConflict) state.conflictMessage = '';
        state.loading = requestJson('/api/v2/portfolio/settings').then((result) => { show(result); return true; }).catch((error) => { state.document = null; state.digest = null; state.dirty = false; state.readOnly = true; state.conflictMessage = ''; renderDocument(); clearInvalid(); setDisabled(true); if (meta) meta.textContent = portfolioErrorMessage(error); return false; }).finally(() => { state.loading = null; });
        return state.loading;
      };
       const collect = () => {
          const weighted_search = Object.fromEntries(weightedSearchKeys.filter((name) => weightedInput(name)).map((name) => [name, weightedInput(name).type === 'checkbox' ? !!weightedInput(name).checked : weightedInput(name).value]));
          const risk_policy = Object.fromEntries(profiles.map((profile) => [profile, Object.fromEntries(riskFields.map((field) => [field, query(`#portfolio-settings-risk-${profile.toLowerCase()}-${field.replaceAll('_', '-')}`)?.value]))]));
           const values = { exchange: query('#portfolio-settings-exchange')?.value, seed: query('#portfolio-settings-seed')?.value, minimum_common_days: query('#portfolio-settings-minimum-common-days')?.value, max_enumerated_combinations: query('#portfolio-settings-max-enumerated-combinations')?.value, lot_model_base_coefficient: query('#portfolio-settings-lot-model-base-coefficient')?.value, lot_model_max_shift_bonus: query('#portfolio-settings-lot-model-max-shift-bonus')?.value, round_down_usdt: query('#portfolio-settings-round-down-usdt')?.value, open_fee_rate: query('#portfolio-settings-open-fee-rate')?.value, close_fee_rate: query('#portfolio-settings-close-fee-rate')?.value, minimum_coverage_pct: query('#portfolio-settings-minimum-coverage-pct')?.value, maximum_age_hours: query('#portfolio-settings-maximum-age-hours')?.value, archive_publication_lag_hours: query('#portfolio-settings-archive-publication-lag-hours')?.value, backfill_write_enabled: !!query('#portfolio-settings-backfill-write-enabled')?.checked, spread_history_bypass_pretest: !!query('#portfolio-settings-spread-history-bypass-pretest')?.checked, weighted_search, risk_policy };
        return settingsPatch(state.document, values);
      };
      const clearInvalid = () => controls().forEach((control) => { control.removeAttribute('aria-invalid'); control.setCustomValidity(''); });
      const markDirty = () => { clearInvalid(); state.dirty = true; if (meta) meta.textContent = state.conflictMessage || 'Есть несохранённые изменения.'; };
      controls().forEach((control) => { control.addEventListener('input', markDirty); control.addEventListener('change', markDirty); });
       reload?.addEventListener('click', async () => { if (state.dirty && !window.confirm('Отбросить несохранённые изменения настроек Оптимизатора портфеля?')) return; await load(); });
      save?.addEventListener('click', async () => {
        if (state.readOnly || !state.document) return;
        state.conflictMessage = '';
        clearInvalid();
        let payload; try { payload = collect(); } catch (error) {
           settingsValidationFeedback(error, query, meta, revealInvalidField);
          return;
        }
        save.disabled = true;
        try { const result = await requestJson('/api/v2/portfolio/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_digest: state.digest, document: payload }) }); show(result); }
        catch (error) {
          if (error?.code === 'CONFIG_CHANGED') {
            state.conflictMessage = 'Настройки изменены в другой сессии. Загружена серверная версия.';
            const refreshed = await load(true);
            if (meta) meta.textContent = refreshed ? state.conflictMessage : 'Не удалось загрузить актуальные настройки после конфликта. Сохранение отключено.';
          }
          else { if (meta) meta.textContent = portfolioErrorMessage(error); save.disabled = state.readOnly; }
        }
      });
       loadPortfolioSettings.state = state; state.load = load;
    }
    if (force || !loadPortfolioSettings.state.document) loadPortfolioSettings.state.load();
  }

  // «Пары и стратегии» main screen (docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 3 contract»): actions
  // with plan dialogs run as one task (progress, ETA, errors, «Стоп»); views and the pair card read the pair registry.
  const mainScreen = document.querySelector('#main-screen');
  if (mainScreen) {
    const mainEl = (id) => document.querySelector(`#main-${id}`);
    const mainState = { exchange: 'bybit', view: 'pairs', document: null, settings: {}, sort: null, desc: false, plan: null, timer: null, lastState: null, pair: null };
    const BINGX_READY_ACTIONS = new Set(['sync', 'history', 'ready', 'tester', 'tick']);   // Panel stages 5b, 5c, 5e-3a
    const node = (tag, text, className) => {
      const element = document.createElement(tag);
      if (text !== undefined && text !== null) element.textContent = String(text);
      if (className) element.className = className;
      return element;
    };
    const cellText = (value) => (value === null || value === undefined || value === '' ? '—'
      : typeof value === 'boolean' ? (value ? 'да' : 'нет') : String(value));
    const errorText = (error, fallback) => error?.message || fallback;
    const viewStatus = (text) => { const line = mainEl('view-status'); if (line) line.textContent = text; };
    const author = mainEl('author');

    // -- task area ------------------------------------------------------------------------------------------
    const renderTask = (status) => {
      const task = status?.task || {};
      const summary = mainScreenHelpers.taskSummary(task);
      const label = mainEl('task-label');
      if (label) label.textContent = task.label || 'Задача не запускалась';
      const badge = mainEl('task-badge');
      if (badge) { badge.textContent = summary.badge; badge.className = `state-badge ${summary.badgeClass}`; }
      const bar = mainEl('task-bar');
      if (bar) bar.style.width = `${summary.percent}%`;
      const text = mainEl('task-text');
      if (text) text.textContent = summary.text;
      const stop = mainEl('task-stop');
      if (stop) stop.disabled = summary.state !== 'running' || Boolean(task.stopping);
      const errors = mainEl('task-errors');
      if (errors) {
        const items = (task.errors || []).slice(-50).map((item) => node('li', [item.pair, item.step, item.message].filter(Boolean).join(' · ')));
        errors.replaceChildren(...items);
        errors.hidden = !items.length;
      }
      const log = mainEl('task-log');
      const logBox = mainEl('task-log-box');
      if (log && logBox) { log.textContent = (task.log || []).slice(-60).join('\n'); logBox.hidden = !(task.log || []).length; }
      document.querySelectorAll('[data-main-action]').forEach((button) => {
        button.dataset.busy = summary.state === 'running' ? 'true' : 'false';
      });
      syncActions();
      const finished = mainState.lastState === 'running' && summary.state !== 'running';
      mainState.lastState = summary.state;
      if (finished) loadView();
      return summary.state === 'running';
    };
    const pollTask = async () => {
      clearTimeout(mainState.timer);
      try {
        if (renderTask(await requestJson('/api/v2/main/task'))) mainState.timer = setTimeout(pollTask, 1500);
      } catch (_) { mainState.timer = setTimeout(pollTask, 5000); }
    };
    mainEl('task-stop')?.addEventListener('click', async () => {
      try { renderTask(await remoteRequest('/api/v2/main/stop', {})); pollTask(); }
      catch (error) { viewStatus(errorText(error, 'Остановка не удалась.')); }
    });

    // -- exchange switch and actions ------------------------------------------------------------------------
    function syncActions() {
      document.querySelectorAll('[data-main-action]').forEach((button) => {
        const pending = mainState.exchange === 'bingx' && !BINGX_READY_ACTIONS.has(button.dataset.mainAction);
        button.disabled = pending || button.dataset.busy === 'true';
        button.title = pending ? 'BingX — этап 5: ретест финалистов BingX ещё не подключён' : '';
      });
    }
    document.querySelectorAll('[data-exchange]').forEach((button) => {
      button.addEventListener('click', () => {
        mainState.exchange = button.dataset.exchange;
        document.querySelectorAll('[data-exchange]').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
        syncActions();
        loadView();
      });
    });
    const startRun = async (action, items, options) => {
      const result = await remoteRequest('/api/v2/main/run', { action, exchange: mainState.exchange, items, options });
      if (result.task) { renderTask(result); pollTask(); }
      if (result.tick) viewStatus(`Тик-тест запущен: ${result.tick.strategy_count} стратегий из ${result.tick.ready}; ход — в карточке «3. Test and Import».`);
      if (result.retest) viewStatus('Ретест финалистов запущен; ход — в карточке ретеста.');
      return result;
    };
    const planDialog = mainEl('plan');
    const planItems = () => Array.from(mainEl('plan-items')?.querySelectorAll('input[type="checkbox"]') || []);
    const planCount = () => {
      const count = mainEl('plan-count');
      const boxes = planItems();
      if (count) count.textContent = `отмечено ${boxes.filter((box) => box.checked).length} из ${boxes.length}`;
    };
    const isoDay = (day) => day.toISOString().slice(0, 10);
    const openPlan = (plan, title) => {
      mainState.plan = plan;
      mainEl('plan-title').textContent = title;
      mainEl('plan-note').textContent = plan.note || '';
      const list = mainEl('plan-items');
      list.replaceChildren(...(plan.items || []).map((item) => {
        const row = node('label', null, 'check main-plan-item');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.value = item.key;
        box.checked = Boolean(item.checked);
        box.addEventListener('change', planCount);
        const text = node('span');
        text.append(node('strong', item.label), node('small', item.detail));
        row.append(box, text);
        return row;
      }));
      const withItems = plan.action !== 'retest';
      mainEl('plan-select').hidden = !withItems;
      list.hidden = !withItems;
      mainEl('plan-window').hidden = plan.action !== 'tick';
      mainEl('plan-reserve-box').hidden = plan.action !== 'retest';
      if (plan.action === 'tick') {
        const end = new Date(Date.now() - 86400000);
        mainEl('plan-end').value = isoDay(end);
        mainEl('plan-start').value = isoDay(new Date(end.getTime() - 30 * 86400000));
      }
      mainEl('plan-reserve').checked = Boolean(plan.options?.include_reserve);
      const status = mainEl('plan-status');
      status.hidden = true;
      status.textContent = '';
      planCount();
      planDialog.showModal();
    };
    mainEl('plan-all')?.addEventListener('click', () => { planItems().forEach((box) => { box.checked = true; }); planCount(); });
    mainEl('plan-none')?.addEventListener('click', () => { planItems().forEach((box) => { box.checked = false; }); planCount(); });
    mainEl('plan-cancel')?.addEventListener('click', () => planDialog.close());
    mainEl('plan-run')?.addEventListener('click', async () => {
      const plan = mainState.plan;
      if (!plan) return;
      const items = planItems().filter((box) => box.checked).map((box) => box.value);
      const options = plan.action === 'tick' ? { start_date: mainEl('plan-start').value, end_date: mainEl('plan-end').value }
        : plan.action === 'retest' ? { include_reserve: mainEl('plan-reserve').checked } : {};
      const run = mainEl('plan-run');
      run.disabled = true;
      try {
        await startRun(plan.action, items, options);
        planDialog.close();
      } catch (error) {
        const status = mainEl('plan-status');
        status.hidden = false;
        status.textContent = errorText(error, 'Запуск не удался.');
      } finally { run.disabled = false; }
    });
    document.querySelectorAll('[data-main-action]').forEach((button) => {
      button.addEventListener('click', async () => {
        const action = button.dataset.mainAction;
        try {
          if (action === 'sync') { await startRun('sync', [], {}); return; }    // «Синхронизировать» has no plan dialog
          viewStatus(`Составляю план: ${button.textContent}…`);
          const plan = await remoteRequest('/api/v2/main/plan', { action, exchange: mainState.exchange });
          if (plan.disabled) { viewStatus(plan.disabled); return; }
          viewStatus('');
          openPlan(plan, button.textContent);
        } catch (error) { viewStatus(errorText(error, 'Действие не выполнено.')); }
      });
    });

    // -- views ----------------------------------------------------------------------------------------------
    const shownColumns = () => {
      const columns = (mainState.document?.columns || []).map((column) => column.key);
      const saved = mainState.settings[mainState.view]?.columns || [];
      const chosen = saved.filter((key) => columns.includes(key));
      const defaults = (mainState.document?.default_columns || []).filter((key) => columns.includes(key));
      return chosen.length ? chosen : defaults.length ? defaults : columns;
    };
    // «Стратегии» (Panel stage 4a): filter toggles, marks in the table, metrics, marks from Excel
    const strategyQuery = () => mainState.view !== 'strategies' ? ''
      : `&hard=${mainEl('strategy-hard')?.checked ? 1 : 0}&top5=${mainEl('strategy-top5')?.checked ? 1 : 0}&hide=${mainEl('strategy-hide')?.checked ? 1 : 0}`;
    const markCell = (row, key) => {
      const td = document.createElement('td');
      if (key === 'status') {
        const select = node('select', null, 'main-mark');
        select.setAttribute('aria-label', `Отметка ${row.strategy}`);
        ['', 'FINALIST', 'RESERVE', 'REJECTED'].forEach((value) => {
          const option = node('option', value || '—');
          option.value = value;
          select.append(option);
        });
        select.value = row.status || '';
        select.addEventListener('change', () => saveMark(row, { status: select.value, user_rank: row.user_rank }));
        td.append(select);
      } else {
        const input = document.createElement('input');
        input.type = 'number';
        input.min = '1';
        input.step = '1';
        input.className = 'main-mark main-mark-rank';
        input.setAttribute('aria-label', `Ранг ${row.strategy}`);
        input.value = row.user_rank ?? '';
        input.disabled = !row.status;
        input.addEventListener('change', () => saveMark(row, { status: row.status || '', user_rank: input.value === '' ? null : input.value }));
        td.append(input);
      }
      return td;
    };
    async function saveMark(row, mark) {
      try {
        const saved = await remoteRequest('/api/v2/main/finalist', {
          exchange: mainState.exchange, strategy: row.strategy, symbol: row.symbol, side: row.side || 'LONG', ...mark,
        });
        row.status = saved.status;
        row.user_rank = saved.user_rank;
        renderTable();
        viewStatus(`Отметка ${row.strategy}: ${saved.status || 'снята'}${saved.user_rank ? `, ранг ${saved.user_rank}` : ''}.`);
      } catch (error) {
        renderTable();                           // the select and the rank go back to the stored mark
        viewStatus(errorText(error, 'Отметка не сохранена.'));
      }
    }
    const visibleRows = () => {
      const shown = shownColumns();
      const filtered = mainScreenHelpers.filterRows(mainState.document?.rows || [], shown, mainEl('filter')?.value || '');
      return mainScreenHelpers.sortRows(filtered, shown.includes(mainState.sort) ? mainState.sort : null, mainState.desc);
    };
    const renderColumnsPanel = () => {
      const panel = mainEl('columns-panel');
      if (!panel || !mainState.document) return;
      const shown = shownColumns();
      const legend = node('legend', 'Показывать столбцы');
      panel.replaceChildren(legend, ...mainState.document.columns.map((column) => {
        const row = node('label', null, 'check');
        const box = document.createElement('input');
        box.type = 'checkbox';
        box.value = column.key;
        box.checked = shown.includes(column.key);
        box.addEventListener('change', async () => {
          const columns = Array.from(panel.querySelectorAll('input:checked')).map((input) => input.value);
          if (!columns.length) { box.checked = true; viewStatus('Нужен хотя бы один столбец.'); return; }
          try {
            mainState.settings = await remoteRequest('/api/v2/main/view-settings', { view: mainState.view, columns });
            renderTable();
          } catch (error) { box.checked = !box.checked; viewStatus(errorText(error, 'Выбор столбцов не сохранён.')); }
        });
        row.append(box, node('span', column.label));
        return row;
      }));
    };
    function renderTable() {
      const table = mainEl('table');
      if (!table || !mainState.document) return;
      const labels = Object.fromEntries(mainState.document.columns.map((column) => [column.key, column.label]));
      const shown = shownColumns();
      const head = document.createElement('tr');
      shown.forEach((key) => {
        const th = document.createElement('th');
        th.scope = 'col';
        th.setAttribute('aria-sort', mainState.sort === key ? (mainState.desc ? 'descending' : 'ascending') : 'none');
        const button = node('button', `${labels[key]}${mainState.sort === key ? (mainState.desc ? ' ↓' : ' ↑') : ''}`, 'main-sort');
        button.type = 'button';
        button.addEventListener('click', () => {
          if (mainState.sort === key) mainState.desc = !mainState.desc;
          else { mainState.sort = key; mainState.desc = false; }
          renderTable();
        });
        th.append(button);
        head.append(th);
      });
      table.tHead.replaceChildren(head);
      const rows = visibleRows();
      const withCard = mainState.view !== 'changes';
      table.tBodies[0].replaceChildren(...rows.map((row) => {
        const tr = document.createElement('tr');
        shown.forEach((key) => {
          const td = document.createElement('td');
          if (mainState.view === 'strategies' && (key === 'status' || key === 'user_rank')) {
            tr.append(markCell(row, key));
            return;
          }
          if (key === 'symbol' && withCard && row.symbol) {
            const link = node('button', row.symbol, 'main-pair-link');
            link.type = 'button';
            link.addEventListener('click', () => openPair(row.symbol));
            td.append(link);
          } else td.textContent = cellText(row[key]);
          tr.append(td);
        });
        return tr;
      }));
      const notes = (mainState.document.notes || []).length ? ` · ${mainState.document.notes.join('; ')}` : '';
      viewStatus(`Строк: ${rows.length} из ${(mainState.document.rows || []).length} · ${mainState.exchange === 'bingx' ? 'BingX' : 'Bybit'}${notes}`);
    }
    async function loadView() {
      const exchange = mainState.exchange;
      const view = mainState.view;
      try {
        const document_ = await requestJson(`/api/v2/main/view?name=${encodeURIComponent(view)}&exchange=${encodeURIComponent(exchange)}${strategyQuery()}`);
        if (exchange !== mainState.exchange || view !== mainState.view) return;      // a newer choice won
        mainState.document = document_;
        const top5Label = mainEl('strategy-top5-label');
        if (top5Label && typeof document_.top5_share_pct === 'number') top5Label.textContent = `Топ-5 > ${document_.top5_share_pct} %`;
        renderColumnsPanel();
        renderTable();
      } catch (error) {
        mainState.document = null;
        mainEl('table')?.tHead.replaceChildren();
        mainEl('table')?.tBodies[0].replaceChildren();
        viewStatus(errorText(error, 'Вид недоступен.'));
      }
    }
    document.querySelectorAll('[data-main-view]').forEach((button) => {
      button.addEventListener('click', () => {
        mainState.view = button.dataset.mainView;
        mainState.sort = null;
        mainState.desc = false;
        const tools = mainEl('strategy-tools');
        if (tools) tools.hidden = mainState.view !== 'strategies';
        document.querySelectorAll('[data-main-view]').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
        loadView();
      });
    });
    mainEl('filter')?.addEventListener('input', renderTable);
    ['hard', 'top5', 'hide'].forEach((key) => mainEl(`strategy-${key}`)?.addEventListener('change', loadView));
    mainEl('strategy-metrics')?.addEventListener('click', async () => {
      const button = mainEl('strategy-metrics');
      button.disabled = true;
      const poll = window.setInterval(async () => {
        try {
          const progress = await requestJson('/api/v2/strategies/performance-v2/recalculate-all/progress');
          viewStatus(`Метрики: ${progress.completed_pairs ?? 0} из ${progress.planned_pairs ?? progress.total_pairs ?? 0} пар${progress.current_pair ? ` · ${progress.current_pair}` : ''}`);
        } catch (_) { /* the progress line is optional */ }
      }, 2000);
      try {
        const result = await remoteRequest('/api/v2/strategies/performance-v2/recalculate-all', {});
        viewStatus(`Метрики посчитаны: ${result.recalculated_pairs ?? 0} пар.`);
        loadView();
      } catch (error) { viewStatus(errorText(error, 'Метрики не посчитаны.')); }
      finally { window.clearInterval(poll); button.disabled = false; }
    });
    mainEl('strategy-upload')?.addEventListener('click', () => mainEl('strategy-file')?.click());
    mainEl('strategy-file')?.addEventListener('change', async () => {
      const input = mainEl('strategy-file');
      const file = input.files && input.files[0];
      input.value = '';
      if (!file) return;
      try {
        const result = await requestJson(`/api/v2/main/finalists.xlsx?exchange=${encodeURIComponent(mainState.exchange)}`, {
          method: 'POST', headers: { 'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }, body: file,
        });
        if (!result.written) viewStatus(`Отметки не загружены: ${result.errors.map((item) => `строка ${item.row}: ${item.error}`).join('; ')}`);
        else {
          viewStatus(`Отметки загружены: изменено ${result.changed.length}, без изменений ${result.unchanged}, неизвестных ${result.unknown.length}.`);
          loadView();
        }
      } catch (error) { viewStatus(errorText(error, 'Отметки не загружены.')); }
    });
    mainEl('columns')?.addEventListener('click', () => {
      const panel = mainEl('columns-panel');
      panel.hidden = !panel.hidden;
      mainEl('columns').setAttribute('aria-expanded', String(!panel.hidden));
    });
    mainEl('excel')?.addEventListener('click', () => {
      const params = new URLSearchParams({ name: mainState.view, exchange: mainState.exchange, columns: shownColumns().join(','),
        text: mainEl('filter')?.value || '' });
      if (mainState.sort) { params.set('sort', mainState.sort); params.set('desc', mainState.desc ? '1' : '0'); }
      if (mainState.view === 'strategies') {
        ['hard', 'top5', 'hide'].forEach((key) => params.set(key, mainEl(`strategy-${key}`)?.checked ? '1' : '0'));
      }
      const link = document.createElement('a');
      link.href = `/api/v2/main/view.xlsx?${params.toString()}`;
      link.download = '';
      document.body.append(link);
      link.click();
      link.remove();
    });

    // -- pair card ------------------------------------------------------------------------------------------
    const pairTable = (headers, rows) => {
      const wrap = node('div', null, 'table-wrap');
      const table = document.createElement('table');
      const head = document.createElement('tr');
      headers.forEach(([, label]) => { const th = node('th', label); th.scope = 'col'; head.append(th); });
      table.createTHead().append(head);
      const body = table.createTBody();
      rows.forEach((row) => {
        const tr = document.createElement('tr');
        headers.forEach(([key]) => tr.append(node('td', cellText(row[key]))));
        body.append(tr);
      });
      wrap.append(table);
      return wrap;
    };
    const pairSection = (title, content) => {
      const section = node('section', null, 'main-pair-section');
      section.append(node('h3', title));
      if (typeof content === 'string') section.append(node('p', content, 'helper'));
      else section.append(content);
      return section;
    };
    const renderPair = (card) => {
      mainState.pair = card;
      mainEl('pair-title').textContent = `${card.symbol} · ${card.exchange === 'bingx' ? 'BingX' : 'Bybit'}`;
      const ref = card.reference || {};
      const facts = [['Статус', ref.status], ['Стадия', card.pipeline?.stage], ['История', card.history?.status],
        ['Тик', ref.tick_size], ['Шаг лота', ref.qty_step], ['Мин. ордер USDT', ref.min_notional],
        ['Макс. плечо', ref.max_leverage], ['Базовый лот', card.liquidity?.base_lot], ['Ликвидность на', card.liquidity?.as_of_day]];
      const list = node('dl', null, 'main-facts');
      facts.forEach(([label, value]) => { list.append(node('dt', label), node('dd', cellText(value))); });
      const body = mainEl('pair-body');
      body.replaceChildren(
        list,
        pairSection('Периоды в хранилище', card.periods.length ? pairTable([['start', 'С'], ['end', 'По'], ['source', 'Источник']], card.periods) : 'Периодов нет.'),
        pairSection('READY по ТФ', card.ready ? pairTable([['timeframe', 'ТФ'], ['score', 'Score'], ['dd', 'DD'], ['admitted', 'Допущена']], card.ready.timeframes) : 'READY не собирался.'),
        pairSection('Тесты', card.tests.length ? pairTable([['kind', 'Вид'], ['structure', 'Структура'], ['window_start', 'С'], ['window_end', 'По'], ['pnl_pct', 'PnL %'], ['maxdd_pct', 'DD %'], ['trades', 'Сделок']], card.tests.slice(0, 100)) : 'Тестов нет.'),
        pairSection('Решения', card.decisions.length ? pairTable([['when', 'Когда'], ['side', 'Сторона'], ['decision', 'Решение'], ['reason', 'Причина'], ['author', 'Автор']], card.decisions) : 'Решений нет.'),
        pairSection('Пороги фильтра READY', pairTable([['parameter', 'Параметр'], ['default', 'Общий'], ['value', 'Для пары'], ['overridden_text', 'Свой']],
          card.thresholds.map((item) => ({ ...item, overridden_text: item.overridden ? 'да' : '' })))),
        pairSection('История порогов', card.settings_history.length ? pairTable([['when', 'Когда'], ['side', 'Сторона'], ['parameter', 'Параметр'], ['value', 'Значение'], ['reason', 'Причина'], ['author', 'Автор']], card.settings_history) : 'Личных порогов не было.'),
      );
      const parameter = mainEl('setting-parameter');
      const previous = parameter.value;
      parameter.replaceChildren(...card.thresholds.map((item) => {
        const option = node('option', `${item.parameter} (общий ${item.default})`);
        option.value = item.parameter;
        return option;
      }));
      if (previous) parameter.value = previous;
    };
    async function openPair(symbol) {
      const status = mainEl('pair-status');
      status.textContent = '';
      try {
        renderPair(await requestJson(`/api/v2/main/pair?exchange=${encodeURIComponent(mainState.exchange)}&symbol=${encodeURIComponent(symbol)}`));
        const dialog = mainEl('pair');
        if (!dialog.open) dialog.showModal();
      } catch (error) { viewStatus(errorText(error, 'Карточка пары недоступна.')); }
    }
    mainEl('pair-close')?.addEventListener('click', () => mainEl('pair').close());
    const pairWrite = async (endpoint, payload, done) => {
      const status = mainEl('pair-status');
      const who = (author?.value || '').trim();
      if (!who) { status.textContent = 'Укажите автора записи.'; author?.focus(); return; }
      try {
        await remoteRequest(endpoint, { exchange: mainState.pair.exchange, symbol: mainState.pair.symbol, author: who, ...payload });
        status.textContent = done;
        await openPair(mainState.pair.symbol);
        mainEl('pair-status').textContent = done;
        loadView();
      } catch (error) { status.textContent = errorText(error, 'Запись не сохранена.'); }
    };
    mainEl('pair-decision')?.addEventListener('submit', (event) => {
      event.preventDefault();
      pairWrite('/api/v2/main/decision', { side: mainEl('decision-side').value, decision: mainEl('decision-value').value,
        reason: mainEl('decision-reason').value.trim() }, 'Решение записано.');
    });
    mainEl('pair-setting')?.addEventListener('submit', (event) => {
      event.preventDefault();
      const raw = mainEl('setting-value').value.trim();
      pairWrite('/api/v2/main/setting', { side: 'ALL', filter: 'ready_v4', parameter: mainEl('setting-parameter').value,
        value: raw === '' ? null : raw, reason: mainEl('setting-reason').value.trim() },
      raw === '' ? 'Порог сброшен к общему.' : 'Порог записан.');
    });

    requestJson('/api/v2/main/view-settings').then((settings) => { mainState.settings = settings; renderTable(); }).catch(() => {});
    syncActions();
    loadView();
    pollTask();
  }

  loadSafeDefaults();
  recoverJobs();
  loadTesterCollection();
  recoverSplitJobs();
})();
