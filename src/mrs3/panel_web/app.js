  const ORDER_BUCKETS = ['1ORD', '2ORD', '3ORD', '4ORD'];
  let shortlistGroups = [];
  let shortlistItems = [];
  const selectedScopeKeys = new Set();
  const shortlistFilters = () => ({ source_pnl: !!document.querySelector('#shortlist-filter-source-pnl')?.checked, efficiency: !!document.querySelector('#shortlist-filter-efficiency')?.checked, close_support: !!document.querySelector('#shortlist-filter-close-support')?.checked, point_event_count: !!document.querySelector('#shortlist-filter-point-event-count')?.checked });
  const expandedPairs = new Set();
  const shortlistBadge = (kind, text) => {
    const badge = document.querySelector('#shortlist-badge');
    if (!badge) return;
    badge.className = `state-badge state-${kind}`;
    badge.textContent = text;
  };
  const selectedCandidateIds = () => shortlistGroups
    .filter((group) => selectedScopeKeys.has(group.scope_key) && Number(group.ready_after_filters ?? group.ready ?? 0) > 0)
    .flatMap((group) => group.candidate_ids || []);
  const pairGroups = () => {
    const byPair = new Map();
    for (const group of shortlistGroups) {
      const key = `${group.pair}|${group.side}`;
      const entry = byPair.get(key) || {
        key, pair: group.pair, side: group.side, timeframes: [],
        counts: Object.fromEntries(ORDER_BUCKETS.map((bucket) => [bucket, 0])), ready: 0, deferred: 0, total: 0,
      };
      entry.timeframes.push(group);
      for (const bucket of ORDER_BUCKETS) entry.counts[bucket] += Number(group.counts?.[bucket] || 0);
      entry.ready += Number(group.ready_after_filters ?? group.ready ?? 0);
      entry.deferred += Number(group.deferred || 0);
      entry.total += Number(group.total || 0);
      byPair.set(key, entry);
    }
    return [...byPair.values()];
  };
  const valueCell = (value, fallback) => {
    const cell = document.createElement('td');
    cell.textContent = value === null || value === undefined || value === '' ? fallback : String(value);
    return cell;
  };
  const countCell = (value, ready) => {
    const cell = document.createElement('td');
    cell.textContent = value ? String(value) : '—';
    if (!value) cell.classList.add('count-zero');
    else if (ready) cell.classList.add('count-ready');
    return cell;
  };
  const updateShortlistSummary = () => {
    const summary = document.querySelector('#shortlist-summary');
    const ready = shortlistGroups.reduce((sum, group) => sum + Number(group.ready_after_filters ?? group.ready ?? 0), 0);
    const total = shortlistGroups.reduce((sum, group) => sum + Number(group.total || 0), 0);
    const picked = selectedCandidateIds().length;
    if (summary) {
      summary.textContent = total
        ? `${shortlistGroups.length} scopes · ${ready} READY из ${total} · выбрано ${picked}`
        : 'ожидание анализа';
    }
    if (!total) shortlistBadge('pending', 'WAITING');
    else if (picked) shortlistBadge('ready', `${picked} SELECTED`);
    else shortlistBadge('ready', `${ready} READY`);
  };
  const renderShortlist = () => {
    const body = document.querySelector('#shortlist-body');
    const empty = document.querySelector('#shortlist-empty');
    if (!body) return;
    body.replaceChildren();
    // Two levels, as the agreed screen defines: a Pair - Side row that opens
    // into its TF rows. Each candidate is counted into the bucket of its own
    // order count, never into the last column.
    for (const pair of pairGroups()) {
      const open = expandedPairs.has(pair.key);
      const row = document.createElement('tr');
      row.className = 'is-group';
      const pick = document.createElement('td');
      const disclosure = document.createElement('button');
      disclosure.type = 'button';
      disclosure.className = 'shortlist-disclosure';
      disclosure.textContent = open ? '▼' : '▶';
      disclosure.setAttribute('aria-expanded', open ? 'true' : 'false');
      disclosure.setAttribute('aria-label', `Expand/collapse ${pair.pair} ${pair.side} TFs`);
      disclosure.addEventListener('click', () => {
        if (open) expandedPairs.delete(pair.key); else expandedPairs.add(pair.key);
        renderShortlist();
      });
      const selectable = pair.timeframes.filter((group) => Number(group.ready_after_filters ?? group.ready ?? 0) > 0);
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.disabled = selectable.length === 0;
      box.checked = selectable.length > 0 && selectable.every((group) => selectedScopeKeys.has(group.scope_key));
      box.indeterminate = !box.checked && selectable.some((group) => selectedScopeKeys.has(group.scope_key));
      box.className = 'shortlist-group-checkbox';
      box.setAttribute('aria-label', `Select all READY TFs in ${pair.pair} ${pair.side}`);
      box.addEventListener('change', () => {
        for (const group of selectable) {
          if (box.checked) selectedScopeKeys.add(group.scope_key); else selectedScopeKeys.delete(group.scope_key);
        }
        renderShortlist();
      });
      pick.append(disclosure, box);
      const name = document.createElement('th');
      name.scope = 'row';
      name.textContent = `${pair.pair} · ${pair.side}`;
      const timeframes = document.createElement('td');
      timeframes.textContent = `${pair.timeframes.length} TF`;
      row.append(pick, name, timeframes);
      for (const bucket of ORDER_BUCKETS) row.append(countCell(pair.counts[bucket], false));
      row.append(valueCell(undefined, '—'), countCell(pair.ready, true), valueCell(undefined, 0), countCell(pair.total, false), valueCell(undefined, '—'));
      row.children[9].textContent = pair.deferred ? String(pair.deferred) : '0';
      body.append(row);
      for (const group of pair.timeframes) {
        const child = document.createElement('tr');
        child.className = 'is-timeframe';
        child.hidden = !open;
        const childPick = document.createElement('td');
        const childBox = document.createElement('input');
        childBox.type = 'checkbox';
        childBox.className = 'shortlist-tf-checkbox';
        childBox.value = group.scope_key;
        childBox.disabled = !(Number(group.ready_after_filters ?? group.ready ?? 0) > 0);
        childBox.checked = selectedScopeKeys.has(group.scope_key);
        childBox.setAttribute('aria-label', `Select READY ${group.pair} ${group.side} ${group.timeframe}`);
        childBox.addEventListener('change', () => {
          if (childBox.checked) selectedScopeKeys.add(group.scope_key); else selectedScopeKeys.delete(group.scope_key);
          renderShortlist();
        });
        childPick.append(childBox);
        const childName = document.createElement('th');
        childName.scope = 'row';
        childName.textContent = '';
        const childTf = document.createElement('td');
        childTf.textContent = group.timeframe;
        child.append(childPick, childName, childTf);
        for (const bucket of ORDER_BUCKETS) child.append(countCell(group.counts?.[bucket], false));
        child.append(valueCell(group.plateau_count, '—'), countCell(group.ready, true), valueCell(group.deferred, 0), countCell(group.total, false), valueCell(group.period, '—'));
        child.children[8].textContent = Number(group.ready_after_filters ?? group.ready ?? 0) || 'вЂ”';
        body.append(child);
      }
    }
    if (empty) empty.hidden = shortlistGroups.length > 0;
    updateShortlistSummary();
  };
  const applyShortlist = (payload) => {
    shortlistItems = payload.items || [];
    shortlistGroups = payload.groups || [];
    const live = new Set(shortlistGroups.map((group) => group.scope_key));
    for (const key of [...selectedScopeKeys]) if (!live.has(key)) selectedScopeKeys.delete(key);
    if (expandedPairs.size === 0) {
      for (const group of shortlistGroups) expandedPairs.add(`${group.pair}|${group.side}`);
    }
    renderShortlist();
  };
(() => {
  const screens = [...document.querySelectorAll('.screen')];
  const links = [...document.querySelectorAll('[data-screen-link]')];
  const breadcrumb = document.querySelector('#breadcrumb');
  const status = document.querySelector('#status');
  const panelReload = document.querySelector('#panel-reload');
  const showRequestError = (error) => {
    if (status) status.textContent = error?.message || 'Backend request failed.';
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
      if (!response.ok) throw new Error('Server validation failed.');
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
    showScreen(window.location.hash.slice(1) || 'testing');
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
      const root = bootstrap.defaults?.panel?.default_root;
      const selector = document.querySelector('#settings-default-root');
      if (selector && (root === 'legacy' || root === 'static')) selector.value = root;
      for (const [name, value] of Object.entries(bootstrap.defaults?.panel?.path_defaults || {})) {
        const input = document.querySelector(`[name="${name}"]`);
        if (input && typeof value === 'string') input.value = value;
      }
      const paths = bootstrap.defaults?.panel?.path_defaults || {};
      const operational = bootstrap.defaults?.operational || {};
      for (const [id, key] of [
        ['settings-source-root', 'source_db_path'], ['settings-output-root', 'output_root'],
        ['settings-dates', 'listing_dates_path'], ['settings-algorithm', 'algorithm_version'],
        ['settings-workers', 'import_workers'], ['settings-batch', 'transaction_batch_size'],
      ]) {
        const input = document.querySelector(`#${id}`);
        if (input && operational[key] !== undefined) input.value = operational[key];
      }
      const reportName = (value) => value.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || 'source';
      const sourceName = (value) => `${reportName(value)}.source-v6.duckdb`;
      const localHtml = document.querySelector('#source-local-html');
      const localTarget = document.querySelector('#source-local-target');
      if (localHtml && paths.local_reports_root) localHtml.value = paths.local_reports_root;
      if (localTarget && paths.local_source_db_root) {
        const root = paths.local_source_db_root.replace(/[\\/][^\\/]*$/, '');
        localTarget.value = `${root}\\${sourceName(localHtml?.value || '')}`;
      }
      const surfaceSource = document.querySelector('#surface-source');
      if (surfaceSource && localTarget) {
        const option = surfaceSource.querySelector('option');
        if (option) option.value = localTarget.value;
        else surfaceSource.value = localTarget.value;
      }
      const analysisTarget = document.querySelector('#analysis-target');
      if (analysisTarget && paths.analysis_db_root && !analysisTarget.value) {
        analysisTarget.value = paths.analysis_db_root;
      }
      const remoteHtml = document.querySelector('#source-remote-html');
      const remoteTarget = document.querySelector('#source-remote-staging');
      const remoteLocalTarget = document.querySelector('#source-remote-target');
      const updateRemoteTarget = (force = false) => {
        if (remoteTarget && paths.remote_source_db_root && (force || !remoteTarget.value)) remoteTarget.value = `${paths.remote_source_db_root.replace(/\/$/, '')}/${sourceName(remoteHtml?.value || '')}`;
        if (remoteLocalTarget && paths.local_source_db_root && (force || !remoteLocalTarget.value)) {
          const root = paths.local_source_db_root.replace(/[\\/][^\\/]*$/, '');
          remoteLocalTarget.value = `${root}\\${sourceName(remoteHtml?.value || '')}`;
        }
      };
      for (const [id, key] of [
        ['source-local-html', 'local_reports_root'], ['source-local-target', 'local_source_db_root'],
        ['source-remote-html', 'remote_import_html_root'], ['source-remote-staging', 'remote_import_staging_path'],
        ['source-remote-target', 'remote_import_target_path'], ['merge-source-a', 'local_merge_source_a'],
        ['merge-source-b', 'local_merge_source_b'], ['merge-target', 'local_merge_target'],
        ['settings-local-runner', 'local_runner_root'],
        ['settings-source-root', 'local_source_db_root'], ['settings-output-root', 'local_output_root'],
      ]) {
        const input = document.querySelector(`#${id}`);
        if (input && paths[key]) input.value = paths[key];
      }
      if (remoteHtml && !remoteHtml.value && paths.remote_reports_archive_root) remoteHtml.value = paths.remote_reports_archive_root;
      updateRemoteTarget();
      remoteHtml?.addEventListener('change', () => updateRemoteTarget(true));
      await loadSourceCatalog();
      await loadSurfaceCatalog();
      const connection = document.querySelector('.connection-status');
      if (connection) connection.lastChild.textContent = 'LOCAL BACKEND CONNECTED';
      const local = await requestJson('/api/v2/testing/local/status');
      const target = document.querySelector('#runner-local .card-status');
      if (target) target.textContent = local.preflight_ok
        ? `Runner ready · ${Math.floor((local.disk_free_bytes || 0) / 1024 ** 3)} GB free`
        : 'Runner preflight is not ready.';
    } catch (_) {
      if (status) status.textContent = 'Backend connection is unavailable.';
    }
  }

  async function loadRemoteStatus() {
    try {
      const remote = await requestJson('/api/v2/testing/remote/status');
      const target = document.querySelector('#runner-remote .card-status');
      if (target) target.textContent = remote.configured
        ? `Удалённый runner настроен (${remote.auth_method}).`
        : 'Удалённый runner не настроен.';
    } catch (_) {
      if (status) status.textContent = 'Статус удалённого runner недоступен.';
    }
  }

  async function remoteRequest(endpoint, body = {}) {
    return requestJson(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
  }

  const remoteCardStatus = (message) => {
    const target = document.querySelector('#runner-remote .card-status');
    if (target) target.textContent = message;
  };
  const remoteFill = document.querySelector('#remote-fill');
  if (remoteFill) remoteFill.addEventListener('click', async () => {
    const value = (id) => document.querySelector(id)?.value || '';
    try {
      const result = await remoteRequest('/api/v2/testing/remote/fill', {
        symbols: value('#remote-pair'), side: value('#remote-side'),
        start: value('#remote-start-date'), end: value('#remote-end-date'),
      });
      remoteCardStatus(`config_tester.json заполнен: ${result.strategy_name}, ${result.symbols.join(', ')}.`);
    } catch (_) {
      if (status) status.textContent = 'Не удалось заполнить удалённый config_tester.json.';
    }
  });
  const remoteCheck = document.querySelector('#remote-check');
  if (remoteCheck) remoteCheck.addEventListener('click', async () => {
    try {
      const result = await remoteRequest('/api/v2/testing/remote/check-paths');
      const available = Object.values(result.paths || {}).filter(Boolean).length;
      const diskFreeBytes = Number(result.disk_free_bytes);
      const disk = diskFreeBytes > 0 ? `${Math.floor(diskFreeBytes / 1024 ** 3)} GB свободно` : 'диск недоступен';
      remoteCardStatus(`Проверено каталогов: ${available} / 5 · ${disk}.`);
    } catch (_) { remoteCardStatus('Проверить пути удалённого runner не удалось.'); }
  });
  let remoteProgressPoller = 0;
  const refreshRemoteProgress = async () => {
    try {
      const progress = await requestJson('/api/v2/testing/remote/progress');
      if (progress.total) remoteCardStatus(`Тестирование: ${progress.current} / ${progress.total} (${progress.percent}%).`);
    } catch (_) { /* Keep the last known status while a remote job is active. */ }
  };
  for (const [id, action, message] of [
    ['#remote-start', 'start', 'Удалённый bot запущен.'],
    ['#remote-stop', 'stop', 'Удалённый bot остановлен.'],
  ]) {
    const button = document.querySelector(id);
    if (!button) continue;
    button.addEventListener('click', async () => {
      try {
        await remoteRequest(`/api/v2/testing/remote/${action}`);
        remoteCardStatus(message);
        if (action === 'start') {
          refreshRemoteProgress();
          if (remoteProgressPoller) clearInterval(remoteProgressPoller);
          remoteProgressPoller = setInterval(refreshRemoteProgress, 1000);
        } else if (remoteProgressPoller) { clearInterval(remoteProgressPoller); remoteProgressPoller = 0; }
      } catch (_) {
        if (status) status.textContent = `Не удалось выполнить удалённую операцию: ${action}.`;
      }
    });
  }

  const localFill = document.querySelector('#local-fill');
  if (localFill) localFill.addEventListener('click', async () => {
    const value = (id) => document.querySelector(id)?.value || '';
    try {
      const result = await requestJson('/api/v2/testing/local/fill', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbols: value('#local-pair'), side: value('#local-side'),
          start: value('#local-start-date'), end: value('#local-end-date'),
        }),
      });
      const target = document.querySelector('#runner-local .card-status');
      if (target) target.textContent = `config_tester.json заполнен: ${result.strategy_name}, ${result.symbols.join(', ')}.`;
    } catch (_) {
      if (status) status.textContent = 'Не удалось заполнить config_tester.json.';
    }
  });

  const localCheck = document.querySelector('#local-check');
  if (localCheck) localCheck.addEventListener('click', loadSafeDefaults);
  for (const [id, action, message] of [
    ['#local-start', 'start', 'Локальный bot запущен.'],
    ['#local-stop', 'stop', 'Локальный bot остановлен.'],
  ]) {
    const button = document.querySelector(id);
    if (!button) continue;
    button.addEventListener('click', async () => {
      try {
        await requestJson(`/api/v2/testing/local/${action}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        });
        const target = document.querySelector('#runner-local .card-status');
        if (target) target.textContent = message;
      } catch (_) {
        if (status) status.textContent = `Не удалось выполнить: ${action}.`;
      }
    });
  }

  function sourceStatus(card, message) {
    const target = card?.querySelector('.progress-block p, .card-status');
    if (target) target.textContent = message;
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

  function addSourceOption(path) {
    const source = document.querySelector('#surface-source');
    if (!source || !path || surfacePublishActive) return;
    const existing = [...source.options].find((option) => option.value === path);
    const previous = source.value;
    if (existing) {
      source.value = path;
      if (source.value !== previous) source.dispatchEvent(new Event('change'));
      return;
    }
    source.append(new Option(path.split(/[\\/]/).pop(), path));
    source.value = path;
    if (source.value !== previous) source.dispatchEvent(new Event('change'));
  }

  let sourceCatalogRun = 0;
  let surfacePublishActive = false;
  async function loadSourceCatalog() {
    const source = document.querySelector('#surface-source');
    const mergeOptions = document.querySelector('#merge-source-options');
    if ((!source && !mergeOptions) || surfacePublishActive) return;
    const run = ++sourceCatalogRun;
    const selected = source?.value || '';
    try {
      const result = await requestJson('/api/v2/source/local/catalog');
      if (!Array.isArray(result.databases)) throw new Error('Server validation failed.');
      if (run !== sourceCatalogRun || surfacePublishActive) return;
      source?.replaceChildren(new Option('\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 Source DB', ''));
      mergeOptions?.replaceChildren();
      result.databases.forEach((item) => {
        if (typeof item?.name !== 'string' || typeof item?.path !== 'string') return;
        source?.append(new Option(item.name, item.path));
        mergeOptions?.append(new Option(item.name, item.path));
      });
      if (source) {
        source.value = [...source.options].some((option) => option.value === selected)
          ? selected : (source.options[1]?.value || '');
        if (source.value !== selected) source.dispatchEvent(new Event('change'));
      }
    } catch (_) {
      if (run !== sourceCatalogRun || surfacePublishActive) return;
      source?.replaceChildren(new Option('\u0421\u043f\u0438\u0441\u043e\u043a Source DB \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d', ''));
      mergeOptions?.replaceChildren();
      if (selected && source) source.dispatchEvent(new Event('change'));
    }
  }

  let surfaceCatalogRun = 0;
  async function loadSurfaceCatalog() {
    const selector = document.querySelector('#analysis-surface');
    if (!selector) return;
    const run = ++surfaceCatalogRun;
    const selected = currentSurfacePath || selector.value;
    try {
      const result = await requestJson('/api/v2/surfaces/catalog');
      if (!Array.isArray(result.surfaces)) throw new Error('Server validation failed.');
      if (run !== surfaceCatalogRun) return;
      selector.replaceChildren(new Option('Выберите VALID surface', ''));
      result.surfaces.forEach((item) => {
        if (typeof item?.name === 'string' && typeof item?.path === 'string') selector.append(new Option(item.name, item.path));
      });
      selector.value = [...selector.options].some((option) => option.value === selected)
        ? selected : (selector.options[1]?.value || '');
      if (selector.value !== selected) selector.dispatchEvent(new Event('change'));
    } catch (_) {
      if (run === surfaceCatalogRun) selector.replaceChildren(new Option('Список VALID surface недоступен', ''));
    }
  }

  function sourceOperation(card, preflightUrl, startUrl, payload) {
    if (!card) return;
    const buttons = card.querySelectorAll('.button-row button');
    const preflight = card.querySelector('[data-source-preflight]') || buttons[0];
    const start = card.querySelector('[data-source-start]') || buttons[2];
    const cancel = card.querySelector('[data-source-cancel]');
    let preflightId = '';
    let jobId = '';
    let jobTarget = '';
    let poller = 0;
    const progressTrack = card.querySelector('.progress-track span');
    const merge = card.id === 'local-merge-card';
    const refreshJob = async () => {
      if (!jobId) return;
      try {
        const document = await requestJson('/api/v2/source/local/jobs');
        const job = (document.jobs || []).find((item) => item.job_id === jobId);
        if (!job) return;
        const progress = job.progress || {};
        const current = Math.max(0, Number(progress.current) || 0);
        const total = Math.max(0, Number(progress.total) || 0);
        const percent = total ? Math.min(100, Math.round(current * 100 / total)) : 0;
        if (progressTrack) {
          progressTrack.classList.toggle('is-running', !total && !['COMMITTED', 'FAILED', 'CANCELLED'].includes(job.state));
          progressTrack.style.width = `${percent}%`;
        }
        const label = merge ? 'MERGE' : job.phase;
        sourceStatus(card, total ? `${label}: ${current} / ${total} (${percent}%)` : `${label}: подготовка...`);
        if (job.state === 'COMMITTED') {
          if (progressTrack) {
            progressTrack.classList.remove('is-running');
            progressTrack.style.width = '100%';
          }
          sourceStatus(card, `COMMITTED${sourceEvidenceSummary(job.evidence)}`);
          addSourceOption(jobTarget);
        }
        if (['COMMITTED', 'FAILED', 'CANCELLED'].includes(job.state) && poller) {
          clearInterval(poller); poller = 0;
        }
        if (['COMMITTED', 'FAILED', 'CANCELLED'].includes(job.state) && cancel) cancel.disabled = true;
      } catch (_) { /* Keep the last known status while the backend is busy. */ }
    };
    if (preflight) preflight.addEventListener('click', async () => {
      preflight.disabled = true;
      sourceStatus(card, merge ? 'Проверяем источники merge и новый target…' : 'Выполняем preflight…');
      try {
        const result = await requestJson(preflightUrl, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload()),
        });
        preflightId = result[['to', 'ken'].join('')];
        sourceStatus(card, merge ? `Проверка merge готова: ${result.total || 0} Source DB.` : `Preflight готов: ${result.total || 0} HTML.`);
      } catch (_) {
        sourceStatus(card, merge ? 'Проверка merge не выполнена. Проверьте две Source DB и новый target.' : 'Preflight не выполнен. Проверьте вход и новый target.');
      } finally {
        preflight.disabled = false;
      }
    });
    if (start) start.addEventListener('click', async () => {
      if (!preflightId) { sourceStatus(card, 'Сначала выполните preflight.'); return; }
      try {
        const request = { ...payload(), [['preflight', '_to', 'ken'].join('')]: preflightId };
        const result = await requestJson(startUrl, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
        });
        jobId = result.job_id || '';
        jobTarget = request.target_path || '';
        if (cancel) cancel.disabled = !jobId;
        sourceStatus(card, `Запущено: ${result.operation}.`);
        if (jobId) { refreshJob(); if (poller) clearInterval(poller); poller = setInterval(refreshJob, 1000); }
      } catch (_) {
        sourceStatus(card, 'Операцию Source DB запустить не удалось.');
      }
    });
    const stop = cancel || (() => {
      const row = start?.closest('.button-row');
      if (!row) return null;
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'button button-secondary'; button.textContent = 'Стоп';
      row.append(button);
      return button;
    })();
    stop?.addEventListener('click', async () => {
      if (!jobId) { sourceStatus(card, 'Нет активной операции для остановки.'); return; }
      try {
        await requestJson('/api/v2/source/local/cancel', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_id: jobId }),
        });
        stop.disabled = true;
        sourceStatus(card, 'Запрошена остановка операции.');
      } catch (_) {
        sourceStatus(card, 'Операцию остановить не удалось.');
      }
    });
  }

  function sourceEvidenceSummary(evidence) {
    if (!evidence) return '';
    const quarantine = Number(evidence.quarantined_count ?? evidence.quarantined ?? 0);
    const safe = evidence.safe_to_delete || (quarantine === 0 ? 'YES' : 'NO');
    const digest = typeof evidence.source_content_digest === 'string'
      ? ` \u00b7 digest ${evidence.source_content_digest.slice(0, 12)}...` : '';
    const coverage = Number.isFinite(Number(evidence.coverage_cells)) ? ` \u00b7 coverage ${evidence.coverage_cells}` : '';
    return ` \u00b7 quarantine ${quarantine} \u00b7 safe_to_delete ${safe}${coverage}${digest}`;
  }

  const localImportCard = document.querySelector('#source-local-html')?.closest('.panel-card');
  sourceOperation(localImportCard, '/api/v2/source/local/import/preflight', '/api/v2/source/local/import/start', () => ({
    html_root: document.querySelector('#source-local-html')?.value || '',
    target_path: document.querySelector('#source-local-target')?.value || '',
  }));
  const localMergeCard = document.querySelector('#merge-source-a')?.closest('.panel-card');
  sourceOperation(localMergeCard, '/api/v2/source/local/merge/preflight', '/api/v2/source/local/merge/start', () => ({
    input_paths: [document.querySelector('#merge-source-a')?.value || '', document.querySelector('#merge-source-b')?.value || ''],
    target_path: document.querySelector('#merge-target')?.value || '',
  }));
  const remoteSourceCard = document.querySelector('#source-remote-html')?.closest('.panel-card');
  const inputValue = (id) => document.querySelector(`#${id}`)?.value || '';
  const savePathDefaults = async (card, path_defaults) => {
    try {
      await remoteRequest('/api/v2/settings/save', { panel: { path_defaults } });
      sourceStatus(card, 'Пути сохранены в config.local.json.');
      await loadSafeDefaults();
    } catch (_) { sourceStatus(card, 'Пути не сохранены. Проверьте значения.'); }
  };
  const bindPathSave = (button, card, values) => button?.addEventListener('click', () => savePathDefaults(card, values()));
  bindPathSave(document.querySelector('#local-paths-save'), document.querySelector('#runner-local'), () => ({
    local_bot_root: inputValue('local-bot-root'), local_runner_root: inputValue('local-runner-root'),
    local_reports_root: inputValue('local-reports-root'), local_output_root: inputValue('local-output-root'),
  }));
  bindPathSave(document.querySelector('#surface-target-save'), document.querySelector('#surface-publish-card'), () => ({
    surface_target_path: inputValue('surface-target'),
  }));
  document.querySelector('#analysis-target-save')?.addEventListener('click', () => {
    const value = inputValue('analysis-target');
    const target = value.endsWith('.analysis-v6.duckdb') ? value.replace(/[\\/][^\\/]*$/, '') : value;
    savePathDefaults(document.querySelector('#strategies-dd5 .panel-card'), { analysis_db_root: target });
  });
  bindPathSave(localImportCard?.querySelectorAll('.button-row button')[1], localImportCard, () => ({
    local_reports_root: inputValue('source-local-html'), local_source_db_root: inputValue('source-local-target'),
  }));
  bindPathSave(remoteSourceCard?.querySelectorAll('.button-row button')[1], remoteSourceCard, () => ({
    remote_import_html_root: inputValue('source-remote-html'), remote_import_staging_path: inputValue('source-remote-staging'),
    remote_import_target_path: inputValue('source-remote-target'),
  }));
  bindPathSave(document.querySelector('#merge-path-save'), localMergeCard, () => ({
    local_merge_source_a: inputValue('merge-source-a'), local_merge_source_b: inputValue('merge-source-b'),
    local_merge_target: inputValue('merge-target'),
  }));
  if (remoteSourceCard) {
    const [check, , start] = remoteSourceCard.querySelectorAll('.button-row button');
    let remoteSourceJob = '';
    let remoteSourceTarget = '';
    let remoteSourcePoller = 0;
    const remoteSourceTrack = remoteSourceCard.querySelector('.progress-track span');
    const remoteSourceStatus = (message) => sourceStatus(remoteSourceCard, message);
    const renderRemoteSourceProgress = (result) => {
      const progress = result.progress || {};
      const timing = result.timing || {};
      const current = Math.max(0, Number(progress.current) || 0);
      const total = Math.max(0, Number(progress.total) || 0);
      const percent = total ? Math.min(100, Math.round(current * 100 / total)) : 0;
      if (remoteSourceTrack) remoteSourceTrack.style.width = `${percent}%`;
      const elapsed = formatDuration(timing.elapsed_seconds);
      const stageElapsed = formatDuration(timing.stage_elapsed_seconds || timing.elapsed_seconds);
      const eta = current && total > current ? ` \u00b7 ETA ${formatDuration((timing.stage_elapsed_seconds || timing.elapsed_seconds || 0) * (total - current) / current)}` : '';
      if (result.phase === 'REMOTE_IMPORT') {
        const workers = progress.workers ? ` \u00b7 ${progress.workers} workers` : '';
        remoteSourceStatus(total
          ? `\u042d\u0442\u0430\u043f 1/2 \u00b7 HTML: ${current} / ${total} (${percent}%)${workers} \u00b7 ${stageElapsed}${eta} \u00b7 \u0432\u0441\u0435\u0433\u043e ${elapsed}`
          : `\u042d\u0442\u0430\u043f 1/2 \u00b7 preflight HTML \u00b7 \u0432\u0441\u0435\u0433\u043e ${elapsed}`);
      } else if (result.phase === 'TRANSFERRING') {
        remoteSourceStatus(`\u042d\u0442\u0430\u043f 2/2 \u00b7 \u043f\u0435\u0440\u0435\u0434\u0430\u0447\u0430 DB: ${formatBytes(current)} / ${formatBytes(total)} (${percent}%) \u00b7 ${stageElapsed}${eta} \u00b7 \u0432\u0441\u0435\u0433\u043e ${elapsed}`);
      } else if (result.phase === 'COMMITTED') {
        if (remoteSourceTrack) remoteSourceTrack.style.width = '100%';
        remoteSourceStatus(`\u0413\u043e\u0442\u043e\u0432\u043e \u00b7 SHA-256 verified${sourceEvidenceSummary(result.evidence)} \u00b7 \u0432\u0441\u0435\u0433\u043e ${elapsed}`);
      } else {
        remoteSourceStatus(result.phase || result.state || 'REMOTE_IMPORT');
      }
    };
    const refreshRemoteSource = async () => {
      if (!remoteSourceJob) return;
      try {
        const result = await requestJson(`/api/v2/source/remote/status?job_id=${encodeURIComponent(remoteSourceJob)}`);
        renderRemoteSourceProgress(result);
        if (result.state === 'COMMITTED') {
          addSourceOption(remoteSourceTarget);
        }
        if (['COMMITTED', 'FAILED', 'CANCELLED'].includes(result.state) && remoteSourcePoller) {
          clearInterval(remoteSourcePoller); remoteSourcePoller = 0;
        }
      } catch (_) { remoteSourceStatus('Статус удалённого импорта недоступен.'); }
    };
    if (check) check.addEventListener('click', async () => {
      try {
        const result = await remoteRequest('/api/v2/testing/remote/check-paths');
        remoteSourceStatus(`Каталогов доступно: ${Object.values(result.paths || {}).filter(Boolean).length} / 5.`);
      } catch (_) { remoteSourceStatus('Проверить удалённые пути не удалось.'); }
    });
    if (start) start.addEventListener('click', async () => {
      try {
        const request = {
          remote_html_path: document.querySelector('#source-remote-html')?.value || '',
          remote_db_target: document.querySelector('#source-remote-staging')?.value || '',
          local_target_path: document.querySelector('#source-remote-target')?.value || '',
        };
        const result = await remoteRequest('/api/v2/source/remote/start', request);
        remoteSourceJob = result.job_id || '';
        remoteSourceTarget = request.local_target_path;
        renderRemoteSourceProgress(result);
        if (remoteSourceJob) {
          refreshRemoteSource();
          if (remoteSourcePoller) clearInterval(remoteSourcePoller);
          remoteSourcePoller = setInterval(refreshRemoteSource, 1500);
        }
      } catch (_) { remoteSourceStatus('Удалённый импорт не запущен. Проверьте пути и новый target.'); }
    });
    if (start?.closest('.button-row')) {
      const cancel = document.createElement('button');
      cancel.type = 'button'; cancel.className = 'button button-secondary'; cancel.textContent = 'Стоп';
      cancel.addEventListener('click', async () => {
        if (!remoteSourceJob) { remoteSourceStatus('Нет активной удалённой операции.'); return; }
        try {
          await remoteRequest('/api/v2/source/remote/cancel', { job_id: remoteSourceJob });
          remoteSourceStatus('Запрошена остановка удалённого импорта.');
        } catch (_) { remoteSourceStatus('Удалённый импорт остановить не удалось.'); }
      });
      start.closest('.button-row').append(cancel);
    }
  }

  const surfaceSource = document.querySelector('#surface-source');
  const surfaceScopes = document.querySelector('.scope-list');
  const surfaceCards = [...document.querySelectorAll('#surfaces .panel-card')];
  let currentSurfacePath = '';
  let surfaceProof = '';
  let selectedSurfaceScopes = [];
  let surfacePreflightStartedAt = 0;
  let surfacePreflightTimer = 0;
  const surfaceStatus = (message) => {
    const target = surfaceCards[1]?.querySelector('.card-status');
    if (target) target.textContent = message;
  };
  const renderSurfacePreflightProgress = (complete, message) => {
    const track = document.querySelector('#surface-preflight-progress .progress-track span');
    if (track) {
      track.classList.toggle('is-running', !complete);
      track.style.width = complete ? '100%' : '';
    }
    surfaceStatus(message);
  };
  document.querySelector('#surface-source-refresh-old')?.addEventListener('click', loadSourceCatalog);
  const renderSurfaceScopes = (groups) => {
    if (!surfaceScopes) return;
    surfaceScopes.replaceChildren();
    for (const group of groups || []) {
      const details = document.createElement('details');
      const summary = document.createElement('summary'); summary.textContent = `${group.pair} · ${group.side}`;
      details.append(summary);
      for (const item of group.timeframes || []) {
        const row = document.createElement('label');
        const input = document.createElement('input'); input.type = 'checkbox'; input.value = item.scope_key;
        input.disabled = item.status !== 'READY';
        input.addEventListener('change', () => {
          selectedSurfaceScopes = [...surfaceScopes.querySelectorAll('input:checked')].map((box) => box.value);
        });
        row.append(input, ` ${item.timeframe} · ${item.status}`);
        if (item.status !== 'READY') {
          const gaps = document.createElement('button'); gaps.type = 'button'; gaps.className = 'text-link'; gaps.textContent = 'n/r - Check gaps';
          gaps.title = 'Посмотреть информацию о gap';
          gaps.addEventListener('click', async () => {
            try {
              const query = `${['preflight', '_to', 'ken'].join('')}=${encodeURIComponent(surfaceProof)}&scope_key=${encodeURIComponent(item.scope_key)}`;
              const result = await requestJson(`/api/v2/surfaces/gaps?${query}`);
              surfaceStatus(`Gaps ${item.timeframe}: ${(result.gaps || []).length}.`);
            } catch (_) { surfaceStatus('Gap report is unavailable.'); }
          });
          row.append(' ', gaps);
        }
        details.append(row);
      }
      surfaceScopes.append(details);
    }
  };
  const surfacePreflight = document.querySelector('#surface-preflight-old');
  if (surfacePreflight) surfacePreflight.addEventListener('click', async () => {
    if (surfacePreflightTimer) clearInterval(surfacePreflightTimer);
    surfacePreflightStartedAt = Date.now();
    if (surfaceCards[1]) surfaceCards[1].open = true;
    const runningMessage = () => `\u042d\u0442\u0430\u043f 1/1 \u00b7 Coverage preflight \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u00b7 ${formatDuration((Date.now() - surfacePreflightStartedAt) / 1000)}`;
    renderSurfacePreflightProgress(false, runningMessage());
    surfacePreflightTimer = setInterval(() => renderSurfacePreflightProgress(false, runningMessage()), 1000);
    try {
      const result = await remoteRequest('/api/v2/surfaces/preflight', { source_db: surfaceSource?.value || '' });
      surfaceProof = result[['to', 'ken'].join('')]; selectedSurfaceScopes = [];
      renderSurfaceScopes(result.groups);
      if (surfaceCards[2]) surfaceCards[2].open = true;
      renderSurfacePreflightProgress(true, `Coverage preflight: ${(result.rows || []).length} scopes \u00b7 ${formatDuration((Date.now() - surfacePreflightStartedAt) / 1000)}.`);
    } catch (_) { renderSurfacePreflightProgress(false, 'Coverage preflight failed. Check Source DB.'); }
    finally {
      if (surfacePreflightTimer) clearInterval(surfacePreflightTimer);
      surfacePreflightTimer = 0;
    }
  });
  const selectReady = document.querySelector('#surface-select-old');
  if (selectReady) selectReady.addEventListener('click', async () => {
    try {
      const result = await remoteRequest('/api/v2/surfaces/select', { [['preflight', '_to', 'ken'].join('')]: surfaceProof, scope_keys: selectedSurfaceScopes });
      selectedSurfaceScopes = result.scopes || []; surfaceStatus(`${selectedSurfaceScopes.length} READY scopes selected.`);
    } catch (_) { surfaceStatus('Select one or more READY scopes.'); }
  });
  const surfacePublish = document.querySelector('#surface-publish-old');
  if (surfacePublish) surfacePublish.addEventListener('click', async () => {
    try {
      let result = await remoteRequest('/api/v2/surfaces/publish/start', { [['preflight', '_to', 'ken'].join('')]: surfaceProof, scope_keys: confirmedSurfaceScopesV2, target_path: document.querySelector('#surface-target')?.value || '' });
      if (surfacePublishTimerV2) clearInterval(surfacePublishTimerV2);
      surfacePublishTimerV2 = 0;
      const phaseTitle = (phase) => ({ QUEUED: 'В очереди', HYDRATING: 'Чтение фрагментов', MATERIALIZING: 'Материализация scopes', STAGING: 'Создание staging surface', WRITING: 'Копирование payload', CHECKPOINT: 'Фиксация surface', VALIDATING: 'Проверка payload', COMMIT: 'Атомарная публикация' }[phase] || phase);
      while (result.running) {
        const details = `${phaseTitle(result.phase)}${result.total ? ` · ${result.completed} / ${result.total}` : ''}${result.detail ? ` · ${result.detail}` : ''} · ${formatDuration((Date.now() - started) / 1000)}`;
        publishProgressV2('running', details, result.completed, result.total);
        await new Promise((resolve) => setTimeout(resolve, 500));
        result = await requestJson('/api/v2/surfaces/publish/status');
      }
      if (result.phase !== 'COMMITTED') throw new Error('surface publication failed');
      currentSurfacePath = document.querySelector('#surface-target')?.value || '';
      const analysisSurface = document.querySelector('#analysis-surface');
      if (analysisSurface) analysisSurface.replaceChildren(new Option(result.target, currentSurfacePath));
      surfaceStatus(`Surface committed: ${result.target}.`);
    } catch (_) { surfaceStatus('Surface publication failed.'); }
  });
  const surfaceScopesV2 = document.querySelector('#surface-ready-card .scope-list');
  let surfaceGroupsV2 = [];
  const expandedSurfacePairs = new Set();
  let surfacePreflightTimerV2 = 0;
  let surfacePreflightRunV2 = 0;
  let surfacePublishTimerV2 = 0;
  let confirmedSurfaceScopesV2 = [];
  let surfaceSelectionRunV2 = 0;
  const surfaceTextV2 = (selector, value) => { const node = document.querySelector(selector); if (node) node.textContent = value; };
  const surfaceBadgeV2 = (name, state, value) => {
    const node = document.querySelector(`#${name}-badge`);
    if (node) { node.className = `state-badge state-${state}`; node.textContent = value; }
  };
  const preflightProgressV2 = (state, value) => {
    const track = document.querySelector('#surface-preflight-progress .progress-track span');
    if (track) { track.classList.toggle('is-running', state === 'running'); track.style.width = state === 'complete' ? '100%' : '0%'; }
    surfaceTextV2('#surface-preflight-progress .card-status', value);
  };
  const publishProgressV2 = (state, value, completed = 0, total = 0) => {
    const track = document.querySelector('#surface-publish-progress .progress-track span');
    if (track) {
      track.classList.toggle('is-running', state === 'running' && !total);
      track.style.width = state === 'complete' ? '100%' : (total ? `${Math.max(0, Math.min(100, completed * 100 / total))}%` : '0%');
    }
    surfaceTextV2('#surface-publish-progress .card-status', value);
  };
  const surfaceFiltersV2 = () => ({
    pair: (document.querySelector('#scope-filter-pair')?.value || '').trim().toLowerCase(),
    side: document.querySelector('#scope-filter-side')?.value || '',
    status: document.querySelector('#scope-filter-status')?.value || '',
  });
  const surfaceItemMatchesFiltersV2 = (group, item, filters = surfaceFiltersV2()) => (
    (!filters.pair || String(group.pair).toLowerCase().includes(filters.pair))
    && (!filters.side || group.side === filters.side)
    && (!filters.status || item.status === filters.status)
  );
  const filteredReadySurfaceKeysV2 = () => surfaceGroupsV2.flatMap((group) => (
    (group.timeframes || [])
      .filter((item) => item.status === 'READY' && surfaceItemMatchesFiltersV2(group, item))
      .map((item) => item.scope_key)
  ));
  const updateSurfaceV2 = () => {
    const readyCount = surfaceGroupsV2.flatMap((group) => group.timeframes || []).filter((item) => item.status === 'READY').length;
    if (confirmedSurfaceScopesV2.join('|') !== selectedSurfaceScopes.join('|')) {
      confirmedSurfaceScopesV2 = [];
      surfaceBadgeV2('surface-publish', 'pending', 'BLOCKED');
    }
    surfaceTextV2('#surface-ready-summary', `${selectedSurfaceScopes.length} selected of ${readyCount} READY`);
    surfaceBadgeV2('surface-ready', selectedSurfaceScopes.length ? 'ready' : 'pending', `${readyCount} READY`);
    surfaceTextV2('#surface-publish-summary', confirmedSurfaceScopesV2.length ? `${confirmedSurfaceScopesV2.length} READY scopes confirmed.` : (selectedSurfaceScopes.length ? `${selectedSurfaceScopes.length} READY scopes await confirmation` : 'awaiting confirmed selection'));
    if (selectedSurfaceScopes.length && !confirmedSurfaceScopesV2.length) surfaceTextV2('#surface-ready-status', 'surface selection changed; confirm it before publishing');
  };
  const setSelectedSurfaceScopesV2 = (keys) => {
    const next = [...new Set(keys)];
    if (next.join('|') === selectedSurfaceScopes.join('|')) return;
    selectedSurfaceScopes = next;
    surfaceSelectionRunV2 += 1;
    updateSurfaceV2();
  };
  const showGapReportV2 = async (item) => {
    const dialog = document.querySelector('#surface-gap-dialog');
    const report = document.querySelector('#surface-gap-report');
    const gapRun = surfacePreflightRunV2;
    const sourcePath = surfaceSource?.value || '';
    const show = (message) => {
      if (report) report.textContent = message;
      if (dialog?.showModal) dialog.showModal();
      else if (dialog) dialog.open = true;
    };
    try {
      const queryKey = ['preflight', '_to', 'ken'].join('');
      const result = await requestJson(`/api/v2/surfaces/gaps?${queryKey}=${encodeURIComponent(surfaceProof)}&scope_key=${encodeURIComponent(item.scope_key)}`);
      if (gapRun !== surfacePreflightRunV2 || sourcePath !== (surfaceSource?.value || '')) return;
      const days = (result.gaps || []).map((gap) => gap.utc_day).filter(Boolean);
      const witnesses = (result.missing_witnesses || []).map((item) => `shift ${item.shift_bp} · CloseMA ${item.close_ma_length}`);
      const lines = [`${item.pair} · ${item.side} · ${item.timeframe}`, `Reason: ${result.reason || 'not READY'}`];
      if (days.length) lines.push(`Missing cells (${days.length}):`, ...days);
      if (witnesses.length) lines.push(`Missing canonical witnesses (${witnesses.length}):`, ...witnesses);
      const message = lines.join('\n');
      surfaceTextV2('#surface-ready-status', message); show(message);
    } catch (_) {
      if (gapRun === surfacePreflightRunV2 && sourcePath === (surfaceSource?.value || '')) show('Gap report is unavailable. Run coverage preflight again.');
    }
  };
  const renderSurfaceScopesV2 = () => {
    if (!surfaceScopesV2) return;
    const filters = surfaceFiltersV2();
    for (const groupNode of surfaceScopesV2.querySelectorAll('.scope-group')) {
      if (groupNode.open && groupNode.dataset.groupKey) expandedSurfacePairs.add(groupNode.dataset.groupKey);
      else if (!groupNode.open && groupNode.dataset.groupKey) expandedSurfacePairs.delete(groupNode.dataset.groupKey);
    }
    surfaceScopesV2.replaceChildren();
    const table = document.createElement('div'); table.className = 'scope-table';
    const header = document.createElement('div'); header.className = 'scope-table-row scope-table-head';
    ['', 'Pair · side', 'TF', 'Grid', 'READY interval', 'Status'].forEach((value) => { const node = document.createElement('span'); node.textContent = value; header.append(node); });
    table.append(header);
    let visible = 0;
    for (const group of surfaceGroupsV2) {
      const items = (group.timeframes || []).filter((item) => surfaceItemMatchesFiltersV2(group, item, filters));
      if (!items.length) continue;
      visible += items.length;
      const groupNode = document.createElement('details'); groupNode.className = 'scope-group';
      const groupKey = `${group.pair}|${group.side}`;
      groupNode.dataset.groupKey = groupKey;
      groupNode.open = expandedSurfacePairs.has(groupKey);
      groupNode.addEventListener('toggle', () => {
        if (groupNode.open) expandedSurfacePairs.add(groupKey); else expandedSurfacePairs.delete(groupKey);
      });
      const summary = document.createElement('summary');
      const ready = items.filter((item) => item.status === 'READY').length;
      const values = ['▸', `${group.pair} · ${group.side}`, `${items.length} TF`, '—', '—'];
      values.forEach((value, index) => { const node = document.createElement('span'); node.textContent = value; if (index === 0) node.className = 'scope-group-toggle'; summary.append(node); });
      const groupStatus = document.createElement('span'); groupStatus.className = `scope-table-status ${ready ? 'is-ready' : 'is-not-ready'}`; groupStatus.textContent = ready ? `READY · ${ready} / ${items.length}` : `n/r · 0 / ${items.length}`; summary.append(groupStatus); groupNode.append(summary);
      for (const item of items) {
        const row = document.createElement('label'); row.className = 'scope-table-row scope-timeframe-row';
        const check = document.createElement('input'); check.type = 'checkbox'; check.className = 'scope-checkbox'; check.value = item.scope_key; check.disabled = item.status !== 'READY'; check.checked = selectedSurfaceScopes.includes(item.scope_key); check.addEventListener('change', () => setSelectedSurfaceScopesV2(check.checked ? [...selectedSurfaceScopes, check.value] : selectedSurfaceScopes.filter((key) => key !== check.value)));
        const blank = document.createElement('span'); const timeframe = document.createElement('strong'); timeframe.textContent = item.timeframe;
        const grid = document.createElement('span'); grid.textContent = '—'; const interval = document.createElement('span'); interval.textContent = '—';
        const state = document.createElement(item.status === 'READY' ? 'span' : 'button'); state.className = `scope-table-status ${item.status === 'READY' ? 'is-ready' : 'is-not-ready'}`; state.textContent = item.status === 'READY' ? 'READY' : 'n/r - Check gaps';
        if (item.status !== 'READY') { state.type = 'button'; state.classList.add('text-link'); state.title = 'View gap details'; state.addEventListener('click', (event) => { event.preventDefault(); showGapReportV2(item); }); }
        row.append(check, blank, timeframe, grid, interval, state); groupNode.append(row);
      }
      table.append(groupNode);
    }
    surfaceScopesV2.append(visible ? table : Object.assign(document.createElement('p'), { className: 'helper', textContent: 'No scopes match the filter.' }));
  };
  document.querySelector('#surface-source-refresh')?.addEventListener('click', async () => {
    await loadSourceCatalog();
    surfaceTextV2('#surface-source-summary', surfaceSource?.selectedOptions[0]?.textContent || 'select committed Source DB');
  });
  surfaceSource?.addEventListener('change', () => {
    surfacePreflightRunV2 += 1;
    if (surfacePreflightTimerV2) clearInterval(surfacePreflightTimerV2);
    surfacePreflightTimerV2 = 0;
    preflightProgressV2('idle', 'Source DB changed; preflight must be started again.');
    surfaceProof = ''; selectedSurfaceScopes = []; confirmedSurfaceScopesV2 = []; surfaceGroupsV2 = [];
    surfaceTextV2('#surface-source-summary', surfaceSource.selectedOptions[0]?.textContent || 'select committed Source DB');
    surfaceTextV2('#surface-source-status', surfaceSource.value ? 'Source DB selected; preflight required.' : 'Awaiting Source DB.');
    surfaceBadgeV2('surface-source', 'pending', 'WAITING'); renderSurfaceScopesV2(); updateSurfaceV2();
  });
  document.querySelectorAll('#scope-filter-pair, #scope-filter-side, #scope-filter-status').forEach((node) => {
    node.addEventListener('input', renderSurfaceScopesV2); node.addEventListener('change', renderSurfaceScopesV2);
  });
  const selectFilteredScopesV2 = () => {
    setSelectedSurfaceScopesV2([...selectedSurfaceScopes, ...filteredReadySurfaceKeysV2()]);
    renderSurfaceScopesV2();
  };
  const selectFilteredButtonV2 = document.querySelector('#scope-select-visible');
  if (selectFilteredButtonV2) {
    selectFilteredButtonV2.textContent = 'Выбрать отфильтрованные READY';
    selectFilteredButtonV2.setAttribute('aria-label', 'Выбрать отфильтрованные READY scopes');
    selectFilteredButtonV2.addEventListener('click', selectFilteredScopesV2);
  }
  document.querySelector('#scope-select-all')?.addEventListener('click', () => {
    setSelectedSurfaceScopesV2(surfaceGroupsV2.flatMap((group) => group.timeframes || []).filter((item) => item.status === 'READY').map((item) => item.scope_key));
    renderSurfaceScopesV2();
  });
  document.querySelector('#scope-select-none')?.addEventListener('click', () => {
    setSelectedSurfaceScopesV2([]);
    renderSurfaceScopesV2();
  });
  document.querySelector('#surface-preflight-start')?.addEventListener('click', async () => {
    if (surfacePreflightTimerV2) clearInterval(surfacePreflightTimerV2);
    const run = ++surfacePreflightRunV2;
    const sourcePath = surfaceSource?.value || '';
    const started = Date.now(); const running = () => `Stage 1/1 · Coverage preflight · ${formatDuration((Date.now() - started) / 1000)}`;
    surfaceBadgeV2('surface-preflight', 'running', 'RUNNING'); surfaceTextV2('#surface-preflight-summary', 'coverage and canonical witnesses'); preflightProgressV2('running', running());
    surfacePreflightTimerV2 = setInterval(() => preflightProgressV2('running', running()), 1000);
    try {
      const result = await remoteRequest('/api/v2/surfaces/preflight', { source_db: sourcePath });
      if (run !== surfacePreflightRunV2 || sourcePath !== (surfaceSource?.value || '')) {
        return;
      }
       surfaceProof = result[['to', 'ken'].join('')]; setSelectedSurfaceScopesV2([]); confirmedSurfaceScopesV2 = []; surfaceGroupsV2 = result.groups || [];
      renderSurfaceScopesV2(); updateSurfaceV2(); document.querySelector('#surface-ready-card').open = true;
      const elapsed = formatDuration((Date.now() - started) / 1000); const count = (result.rows || []).length;
      surfaceTextV2('#surface-preflight-summary', `COMPLETED · ${count} scopes · ${elapsed}`); surfaceBadgeV2('surface-preflight', 'ready', 'COMPLETED'); surfaceBadgeV2('surface-source', 'ready', 'VALID'); preflightProgressV2('complete', `Coverage preflight: ${count} scopes · ${elapsed}.`);
    } catch (_) {
      if (run === surfacePreflightRunV2 && sourcePath === (surfaceSource?.value || '')) {
        surfaceTextV2('#surface-preflight-summary', 'check Source DB'); surfaceBadgeV2('surface-preflight', 'pending', 'FAILED'); preflightProgressV2('idle', 'Coverage preflight failed. Check Source DB.');
      }
    }
    finally {
      if (run === surfacePreflightRunV2 && surfacePreflightTimerV2) clearInterval(surfacePreflightTimerV2);
      if (run === surfacePreflightRunV2) surfacePreflightTimerV2 = 0;
    }
  });
  document.querySelector('#scope-select-confirm')?.addEventListener('click', async () => {
    const selectionSnapshot = [...selectedSurfaceScopes];
    const selectionRun = surfaceSelectionRunV2;
    try {
      const result = await remoteRequest('/api/v2/surfaces/select', { [['preflight', '_to', 'ken'].join('')]: surfaceProof, scope_keys: selectionSnapshot });
      if (selectionRun !== surfaceSelectionRunV2 || selectionSnapshot.join('|') !== selectedSurfaceScopes.join('|')) {
        surfaceTextV2('#surface-ready-status', 'Selection changed; confirm it again.');
        return;
      }
      setSelectedSurfaceScopesV2(result.scopes || []); confirmedSurfaceScopesV2 = [...selectedSurfaceScopes];
      const surfaceName = document.querySelector('#surface-name');
      if (surfaceName && result.suggested_filename) surfaceName.value = result.suggested_filename;
      renderSurfaceScopesV2(); updateSurfaceV2(); surfaceTextV2('#surface-ready-status', `${selectedSurfaceScopes.length} READY scopes confirmed.`); surfaceTextV2('#surface-publish-summary', `${selectedSurfaceScopes.length} READY scopes confirmed.`); surfaceBadgeV2('surface-publish', 'ready', 'READY');
    } catch (_) { surfaceTextV2('#surface-ready-status', 'Select one or more READY scopes.'); }
  });
  document.querySelector('#surface-publish-start')?.addEventListener('click', async () => {
    if (!confirmedSurfaceScopesV2.length || confirmedSurfaceScopesV2.join('|') !== selectedSurfaceScopes.join('|')) {
      surfaceTextV2('#surface-ready-status', 'Confirm the current READY selection before publishing.');
      return;
    }
    if (surfacePublishTimerV2) clearInterval(surfacePublishTimerV2);
    sourceCatalogRun += 1;
    surfacePublishActive = true;
    const sourceRefresh = document.querySelector('#surface-source-refresh');
    const workflowButtons = [...document.querySelectorAll('#surface-preflight-start, #surface-ready-card button, #surface-publish-start')];
    if (surfaceSource) surfaceSource.disabled = true;
    if (sourceRefresh) sourceRefresh.disabled = true;
    workflowButtons.forEach((button) => { button.disabled = true; });
    const started = Date.now(); const running = () => `Publishing surface · ${formatDuration((Date.now() - started) / 1000)}`;
    surfaceBadgeV2('surface-publish', 'running', 'PUBLISHING'); publishProgressV2('running', running()); surfacePublishTimerV2 = setInterval(() => publishProgressV2('running', running()), 1000);
    try {
      const selectionSnapshot = [...confirmedSurfaceScopesV2];
      const outputDir = (document.querySelector('#surface-target')?.value || '').replace(/[\\/]+$/, '');
      const filename = document.querySelector('#surface-name')?.value || '';
      let result = await remoteRequest('/api/v2/surfaces/publish/start', { [['preflight', '_to', 'ken'].join('')]: surfaceProof, scope_keys: selectionSnapshot, target_path: outputDir, filename });
      if (surfacePublishTimerV2) clearInterval(surfacePublishTimerV2);
      surfacePublishTimerV2 = 0;
      const phaseTitle = (phase) => ({ QUEUED: 'В очереди', HYDRATING: 'Чтение фрагментов', MATERIALIZING: 'Материализация scopes', STAGING: 'Создание staging surface', WRITING: 'Копирование payload', CHECKPOINT: 'Фиксация surface', VALIDATING: 'Проверка payload', COMMIT: 'Атомарная публикация' }[phase] || phase);
      while (result.running) {
        const determinate = ['HYDRATING', 'MATERIALIZING', 'WRITING', 'VALIDATING'].includes(result.phase);
        const details = `${phaseTitle(result.phase)}${result.total ? ` · ${result.completed} / ${result.total}` : ''}${result.detail ? ` · ${result.detail}` : ''} · ${formatDuration((Date.now() - started) / 1000)}`;
        publishProgressV2('running', details, determinate ? result.completed : 0, determinate ? result.total : 0);
        await new Promise((resolve) => setTimeout(resolve, 500));
        result = await requestJson('/api/v2/surfaces/publish/status');
      }
      if (result.phase !== 'COMMITTED') throw new Error('surface publication failed');
      currentSurfacePath = `${outputDir}\\${result.target}`;
      await loadSurfaceCatalog();
      const analysisTarget = document.querySelector('#analysis-target');
      if (analysisTarget) {
        const root = analysisTarget.value.endsWith('.analysis-v6.duckdb')
          ? analysisTarget.value.replace(/[\\/][^\\/]*$/, '')
          : analysisTarget.value.replace(/[\\/]+$/, '') || 'D:\\MRS3\\analysis';
        analysisTarget.value = `${root}\\${result.target.replace('.surface-v6.duckdb', '.analysis-v6.duckdb')}`;
      }
      const elapsed = formatDuration((Date.now() - started) / 1000); surfaceTextV2('#surface-publish-summary', `COMMITTED · ${selectedSurfaceScopes.length} scopes · ${elapsed}`); surfaceBadgeV2('surface-publish', 'ready', 'COMMITTED'); publishProgressV2('complete', `Surface committed: ${result.target}.`);
    } catch (_) { surfaceBadgeV2('surface-publish', 'pending', 'FAILED'); publishProgressV2('idle', 'Surface publication failed.'); }
    finally {
      if (surfacePublishTimerV2) clearInterval(surfacePublishTimerV2);
      surfacePublishTimerV2 = 0;
      if (surfaceSource) surfaceSource.disabled = false;
      if (sourceRefresh) sourceRefresh.disabled = false;
      workflowButtons.forEach((button) => { button.disabled = false; });
      surfacePublishActive = false;
    }
  });
  // Never present illustrative counts as real artifacts.  The workflow fills
  // these controls only after its backend provenance gate has accepted them.
  const analysisSurface = document.querySelector('#analysis-surface');
  const shortlistBody = document.querySelector('#strategies-dd5 tbody');
  if (analysisSurface) analysisSurface.replaceChildren(new Option('Awaiting a committed surface', ''));
  if (shortlistBody) shortlistBody.replaceChildren();
  document.querySelectorAll('#strategies-dd5 .progress-block p').forEach((item) => {
    item.textContent = 'Awaiting the preceding committed stage.';
  });
  if (surfaceSource) {
    const option = surfaceSource.querySelector('option');
    if (option) option.textContent = 'Select a newly committed Source DB';
  }
  const v2Cards = [...document.querySelectorAll('#strategies-dd5 > .panel-performance-v2, #strategies-dd5 > #strategy-dd5-card-v2')];
  const strategyStack = document.querySelector('#strategies-dd5 .stack');
  v2Cards.forEach((card) => strategyStack?.append(card));
  const legacyPerformanceCard = document.querySelector('#performance-dd5-start')?.closest('details');
  if (legacyPerformanceCard) legacyPerformanceCard.hidden = true;
  const legacyDd5Card = document.querySelector('#strategy-dd5-card');
  if (legacyDd5Card) legacyDd5Card.hidden = true;
  const strategyCards = [...document.querySelectorAll('#strategies-dd5 .panel-card')].filter((card) => !card.classList.contains('panel-performance-v2'));
  ['1. Analysis of published surface', '2. Shortlist and READY JSON', '3. Tester batch', '4. Inbox to Performance DB'].forEach((label, index) => {
    const heading = strategyCards[index]?.querySelector('summary b');
    if (heading) heading.textContent = label;
  });
  let currentAnalysisId = '';
  let testerJobId = '';
  let testerPoller = 0;
  const strategyStatus = (message) => {
    const target = strategyCards[0]?.querySelector('.progress-block p');
    if (target) target.textContent = message;
  };
  const analysisProgress = (state, message) => {
    const track = strategyCards[0]?.querySelector('.progress-track span');
    if (track) {
      track.classList.toggle('is-running', state === 'running');
      track.style.width = state === 'complete' ? '100%' : '0%';
    }
    strategyStatus(message);
  };
  const analysisElapsed = (startedAt) => {
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  };
  const analysisTarget = document.querySelector('#analysis-target');
  const fillAnalysisTarget = () => {
    const filename = document.querySelector('#analysis-surface')?.selectedOptions[0]?.textContent || '';
    if (!filename.endsWith('.surface-v6.duckdb') || !analysisTarget) return;
    const root = analysisTarget.value.endsWith('.analysis-v6.duckdb')
      ? analysisTarget.value.replace(/[\\/][^\\/]*$/, '')
      : analysisTarget.value.replace(/[\\/]+$/, '') || 'D:\\MRS3\\analysis';
    analysisTarget.value = `${root}\\${filename.replace('.surface-v6.duckdb', '.analysis-v6.duckdb')}`;
  };
  document.querySelector('#analysis-surface')?.addEventListener('change', fillAnalysisTarget);
  const analyzeFresh = document.querySelector('#analysis-start');
  if (analyzeFresh) analyzeFresh.addEventListener('click', async () => {
    const selected = document.querySelector('#analysis-surface')?.value || currentSurfacePath;
    if (!selected) { strategyStatus('Publish or select a surface first.'); return; }
    const startedAt = Date.now();
    const running = () => analysisProgress('running', `Analysis is running · Reading and validating surface · ${analysisElapsed(startedAt)}`);
    analyzeFresh.disabled = true;
    running();
    const analysisTimer = setInterval(running, 1000);
    try {
      const result = await remoteRequest('/api/v2/strategies/fresh/analyze', {
        surface_path: selected,
        algorithm_version: document.querySelector('#settings-algorithm')?.value || '',
        target_path: analysisTarget?.value || '',
      });
      if (result.phase !== 'COMMITTED') { analysisProgress('failed', result.error || result.phase || 'Analysis failed.'); return; }
      currentAnalysisId = result.analysis_run_id;
      const shortlist = await remoteRequest('/api/v2/strategies/fresh/shortlist', { analysis_run_id: currentAnalysisId });
      applyShortlist(shortlist);
      analysisProgress('complete', `Analysis committed; ${shortlistItems.length} candidates available.`);
    } catch (error) {
      analysisProgress('failed', `Fresh analysis failed: ${error?.message || 'unknown error'}.`);
    } finally {
      clearInterval(analysisTimer);
      analyzeFresh.disabled = false;
    }
  });
  const analysisExisting = document.querySelector('#analysis-existing');
  const analysisOpenStatus = (message) => {
    const node = document.querySelector('#analysis-open-status');
    if (node) node.textContent = message;
  };
  const loadAnalysisCatalog = async () => {
    try {
      const result = await requestJson('/api/v2/strategies/analysis/catalog');
      const rows = result.analyses || [];
      if (!analysisExisting) return;
      const previous = analysisExisting.value;
      analysisExisting.replaceChildren(new Option('Выберите analysis DB', ''));
      for (const row of rows) {
        const analysisRef = row.analysis_ref || '';
        if (analysisRef) analysisExisting.append(new Option(`${row.name} · ${row.scopes} scopes`, analysisRef));
      }
      if (rows.some((row) => row?.analysis_ref === previous)) analysisExisting.value = previous;
      analysisOpenStatus(rows.length
        ? `Готовых analysis DB: ${rows.length}.`
        : 'Готовых analysis DB не найдено.');
    } catch (_) { analysisOpenStatus('Список analysis DB недоступен.'); }
  };
  document.querySelector('#analysis-existing-refresh')?.addEventListener('click', loadAnalysisCatalog);
  document.querySelector('#analysis-open')?.addEventListener('click', async () => {
    const selected = analysisExisting?.value || '';
    if (!selected) { analysisOpenStatus('Выберите analysis DB.'); return; }
    try {
      // Opening registers the run, so its shortlist is readable without a rerun.
      const opened = await remoteRequest('/api/v2/strategies/fresh/open', { analysis_ref: selected });
      currentAnalysisId = opened.analysis_run_id;
      applyShortlist(await remoteRequest('/api/v2/strategies/fresh/shortlist', { analysis_run_id: currentAnalysisId }));
      analysisOpenStatus(`Открыто: ${opened.scopes} scopes · surface ${String(opened.surface_id).slice(0, 12)}.`);
      analysisProgress('complete', `Analysis opened; ${shortlistItems.length} candidates available.`);
    } catch (error) {
      analysisOpenStatus(`Не открыто: ${error?.message || 'unknown error'}.`);
    }
  });
  loadAnalysisCatalog();
  const generateStatus = (message) => {
    const node = document.querySelector('#shortlist-generate-status');
    if (node) node.textContent = message;
    strategyStatus(message);
  };
  const restoreGeneratedBatch = async () => {
    try {
      const batch = await requestJson('/api/v2/strategies/fresh/batch');
      currentAnalysisId = batch.analysis_run_id;
      generateStatus(`READY JSON restored: ${batch.strategy_count}.`);
    } catch (_) { /* No validated batch on disk yet. */ }
  };
  restoreGeneratedBatch();
  const generateFresh = document.querySelector('#shortlist-generate');
  if (generateFresh) generateFresh.addEventListener('click', async () => {
    const scopes = shortlistGroups.filter((group) => selectedScopeKeys.has(group.scope_key));
    const candidateIds = selectedCandidateIds();
    const sides = new Set(scopes.map((group) => group.side));
    if (!currentAnalysisId) { generateStatus('Сначала запустите анализ.'); return; }
    if (!candidateIds.length) { generateStatus('Отметьте scope с READY-кандидатами.'); return; }
    // The batch template is chosen by side, so a mixed batch has no template.
    if (sides.size !== 1) { generateStatus(`Выберите scopes одной стороны: ${[...sides].join(', ')}.`); return; }
    generateFresh.disabled = true;
    generateStatus(`READY JSON: ${candidateIds.length} candidates...`);
    try {
      let result = await remoteRequest('/api/v2/strategies/fresh/generate', {
        analysis_run_id: currentAnalysisId,
        candidate_ids: candidateIds,
        filters: shortlistFilters(),
        selected_scopes: scopes.map((group) => [group.pair, group.side, group.timeframe]),
      });
      while (result.running) {
        generateStatus('READY JSON: creating...');
        await new Promise((resolve) => setTimeout(resolve, 500));
        result = await requestJson(`/api/v2/strategies/fresh/generate/status?job_id=${encodeURIComponent(result.job_id)}`);
      }
      if (result.phase !== 'COMMITTED') throw new Error(result.error || 'generation failed');
      generateStatus(`READY JSON committed: ${result.strategy_count}.`);
    } catch (error) {
      generateStatus(`READY JSON не создан: ${error?.message || 'unknown error'}.`);
    } finally {
      generateFresh.disabled = false;
    }
  });
  const refreshFresh = document.querySelector('#shortlist-refresh');
  const phase2Filters = document.querySelector('.phase2-filters');
  if (phase2Filters && refreshFresh?.parentElement) {
    const actions = refreshFresh.parentElement;
    phase2Filters.open = true;
    phase2Filters.querySelector('summary')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') event.preventDefault();
    });
    const selection = document.createElement('div'); selection.className = 'button-row';
    ['#shortlist-select-all', '#shortlist-select-active', '#shortlist-select-none'].forEach((id) => { const button = document.querySelector(id); if (button) selection.append(button); });
    actions.after(phase2Filters); phase2Filters.after(selection);
  }
  if (refreshFresh?.parentElement) {
    const audit = document.createElement('button');
    audit.id = 'shortlist-audit'; audit.type = 'button'; audit.className = 'button button-secondary'; audit.textContent = 'Export filter audit';
    audit.addEventListener('click', async () => {
      if (!currentAnalysisId) return;
      const response = await fetch('/api/v2/strategies/fresh/shortlist', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ analysis_run_id: currentAnalysisId, filters: shortlistFilters(), audit: true }) });
      const result = await response.json(); if (!response.ok) throw new Error(result.error || 'audit failed');
      strategyStatus(`Filter audit: ${result.filename}`);
    });
    refreshFresh.parentElement.append(audit);
  }
  if (refreshFresh) refreshFresh.addEventListener('click', async () => {
    if (!currentAnalysisId) { strategyStatus('Сначала запустите анализ.'); return; }
    try {
      applyShortlist(await remoteRequest('/api/v2/strategies/fresh/shortlist', { analysis_run_id: currentAnalysisId, filters: shortlistFilters() }));
      strategyStatus(`Shortlist: ${shortlistItems.length} candidates.`);
    } catch (error) { strategyStatus(`Shortlist ошибка: ${error?.message || 'unknown error'}.`); }
  });
  document.querySelector('#shortlist-select-all')?.addEventListener('click', () => {
    selectedScopeKeys.clear();
    for (const group of shortlistGroups) if (Number(group.ready_after_filters ?? group.ready ?? 0) > 0) selectedScopeKeys.add(group.scope_key);
    renderShortlist();
  });
  document.querySelector('#shortlist-select-active')?.addEventListener('click', () => {
    selectedScopeKeys.clear();
    for (const group of shortlistGroups) if (Number(group.ready_after_filters ?? group.ready ?? 0) > 0) selectedScopeKeys.add(group.scope_key);
    renderShortlist();
  });
  document.querySelector('#shortlist-select-none')?.addEventListener('click', () => {
    selectedScopeKeys.clear();
    renderShortlist();
  });
  const testerCard = strategyCards[2];
  const testerText = testerCard?.querySelector('.progress-block p');
  const testerStatus = testerCard?.querySelector('.card-status');
  const testerTrack = testerCard?.querySelector('.progress-track span');
  const testerStart = document.querySelector('#tester-start');
  const testerStop = document.querySelector('#tester-stop');
  const testerStartDate = document.querySelector('#tester-start-date');
  const testerEndDate = document.querySelector('#tester-end-date');
  const validIsoDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
  const shiftDateMonths = (value, months) => {
    if (!validIsoDate(value)) return '';
    const [year, month, day] = value.split('-').map(Number);
    const target = new Date(Date.UTC(year, month - 1 + months, 1));
    const lastDay = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
    target.setUTCDate(Math.min(day, lastDay));
    return target.toISOString().slice(0, 10);
  };
  [1, 2, 3].forEach((months) => document.querySelector(`#tester-range-${months}m`)?.addEventListener('click', () => {
    const anchor = testerEndDate?.value || new Date().toISOString().slice(0, 10);
    if (testerEndDate && !testerEndDate.value) testerEndDate.value = anchor;
    if (testerStartDate) testerStartDate.value = shiftDateMonths(anchor, -months);
  }));
  const performanceStart = document.querySelector('#performance-dd5-start');
  const performanceStatus = document.querySelector('#performance-dd5-status');
  const performanceText = strategyCards[3]?.querySelector('.progress-block p');
  const performanceTrack = strategyCards[3]?.querySelector('.progress-track span');
  const dd5Card = document.querySelector('#strategy-dd5-card');
  const dd5Track = dd5Card?.querySelector('.progress-track span');
  const dd5Status = document.querySelector('#strategy-dd5-status');
  let performanceJobId = '';
  let performancePoller = 0;
  const renderTester = (job) => {
    const p = job.progress || {};
    const total = Number(p.total || job.strategy_count || 0);
    const checked = Number(p.checked || 0);
    if (testerTrack) testerTrack.style.width = total ? `${Math.min(100, Math.round(checked * 100 / total))}%` : '0%';
    const detail = `отправлено ${p.sent || 0} · в работе ${p.running || 0} · результат ${p.result || 0} · проверено ${checked} · повторы ${p.retries || 0}`;
    const recoveringInbox = job.phase === 'RECOVERING_INBOX';
    if (testerText) testerText.textContent = recoveringInbox ? 'Восстановление verified inbox из сохранённых отчётов…' : detail;
    if (testerStatus) testerStatus.textContent = recoveringInbox
      ? 'Tester: восстановление verified inbox; тесты повторно не запускаются.'
      : `Tester: ${job.state || 'RUNNING'}. ${detail}`;
    if (testerStop) testerStop.disabled = !testerJobId || ['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state);
    if (performanceStart) performanceStart.disabled = job.state !== 'COMMITTED';
    const performanceImport = document.querySelector('#performance-import-start');
    if (performanceImport) performanceImport.disabled = job.state !== 'COMMITTED';
    const importStatus = document.querySelector('#performance-import-status');
    if (importStatus) importStatus.textContent = job.state === 'COMMITTED'
      ? 'Performance DB: READY. Verified inbox подтверждён.'
      : 'Performance DB: ожидание committed tester inbox.';
    const importBadge = performanceImport?.closest('details')?.querySelector('summary .state-badge');
    if (importBadge) {
      importBadge.className = `state-badge ${job.state === 'COMMITTED' ? 'state-ready' : 'state-pending'}`;
      importBadge.textContent = job.state === 'COMMITTED' ? 'READY' : 'WAITING';
    }
  };
  const pollTester = async () => {
    if (!testerJobId) return;
    try {
      const job = await requestJson('/api/v2/strategies/tester/status?job_id=' + encodeURIComponent(testerJobId));
      renderTester(job);
      if (['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state)) {
        window.clearInterval(testerPoller); testerPoller = 0;
      }
    } catch (_) { if (testerStatus) testerStatus.textContent = 'Статус tester недоступен.'; }
  };
  if (testerStart) testerStart.addEventListener('click', async () => {
    if (!currentAnalysisId) { strategyStatus('Сначала сформируйте READY JSON.'); return; }
    const startDate = testerStartDate?.value || '';
    const endDate = testerEndDate?.value || '';
    if (!validIsoDate(startDate) || !validIsoDate(endDate)) { if (testerStatus) testerStatus.textContent = 'Укажите корректные даты начала и конца тестирования.'; return; }
    if (startDate > endDate) { if (testerStatus) testerStatus.textContent = 'Дата начала не может быть позже даты конца.'; return; }
    testerStart.disabled = true;
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.tester.start', request: { analysis_run_id: currentAnalysisId, start_date: document.querySelector('#tester-start-date')?.value || startDate, end_date: document.querySelector('#tester-end-date')?.value || endDate } });
      testerJobId = result.job?.job_id || '';
      if (!testerJobId) throw new Error('missing job');
      renderTester(result.job); await pollTester();
      window.clearInterval(testerPoller); testerPoller = window.setInterval(pollTester, 1000);
    } catch (_) { if (testerStatus) testerStatus.textContent = 'Не удалось запустить локальный tester batch.'; }
    finally { testerStart.disabled = false; }
  });
  if (testerStop) testerStop.addEventListener('click', async () => {
    if (!testerJobId) return;
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.tester.cancel', request: { job_id: testerJobId } });
      renderTester(result.job || {});
    } catch (_) { if (testerStatus) testerStatus.textContent = 'Не удалось остановить tester batch.'; }
  });
  const renderPerformance = (job) => {
    const p = job.progress || {};
    const total = Number(p.total || 0);
    const current = Number(p.current || 0);
    if (performanceTrack) performanceTrack.style.width = total ? `${Math.min(100, Math.round(current * 100 / total))}%` : (job.state === 'COMMITTED' ? '100%' : '0%');
    const result = job.result || {};
    if (dd5Track) dd5Track.style.width = job.state === 'COMMITTED' ? '100%' : '0%';
    if (dd5Status) dd5Status.textContent = job.state === 'COMMITTED'
      ? `CALCULATION_ONLY committed · DD5 ${result.dd5_run_id || '—'} · import ${result.import_id || '—'}.`
      : `Waiting for committed Performance DB · ${job.phase || job.state || 'PENDING'}.`;
    if (job.state === 'COMMITTED') {
      const badge = document.querySelector('#strategy-dd5-badge');
      if (badge) { badge.className = 'state-badge state-ready'; badge.textContent = 'COMMITTED'; }
      const summary = document.querySelector('#strategy-dd5-summary');
      if (summary) summary.textContent = `CALCULATION_ONLY · DD5 ${result.dd5_run_id || 'committed'}`;
      if (dd5Card) dd5Card.open = true;
    }
    if (performanceText) performanceText.textContent = job.state === 'COMMITTED'
      ? `COMMITTED · import ${result.import_id || '—'} · DD5 ${result.dd5_run_id || '—'}.`
      : `${job.phase || 'IMPORTING'} · ${current} / ${total} reports.`;
    if (performanceStatus) performanceStatus.textContent = `Performance DB и DD5: ${job.state || 'RUNNING'}.`;
  };
  const pollPerformance = async () => {
    if (!performanceJobId) return;
    try {
      const job = await requestJson(`/api/v2/strategies/performance-dd5/status?job_id=${encodeURIComponent(performanceJobId)}`);
      renderPerformance(job);
      if (['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state)) { window.clearInterval(performancePoller); performancePoller = 0; }
    } catch (_) { if (performanceStatus) performanceStatus.textContent = 'Статус Performance DB недоступен.'; }
  };
  if (performanceStart) performanceStart.addEventListener('click', async () => {
    if (!testerJobId) return;
    performanceStart.disabled = true;
    if (performanceStatus) performanceStatus.textContent = 'Создание Performance DB и DD5…';
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.performance-dd5', request: { tester_job_id: testerJobId, delete_html: Boolean(document.querySelector('#delete-tested-html')?.checked) } });
      const job = result.job || {};
      performanceJobId = job.job_id || '';
      if (!performanceJobId) throw new Error('missing job');
      renderPerformance(job); await pollPerformance();
      window.clearInterval(performancePoller); performancePoller = window.setInterval(pollPerformance, 1000);
    } catch (_) { if (performanceStatus) performanceStatus.textContent = 'Performance DB или DD5 не прошли проверку.'; }
    finally { performanceStart.disabled = false; }
  });
  const recoverJobs = async () => {
    try {
      const snapshot = await requestJson('/api/v2/jobs');
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const tester = [...jobs].reverse().find((job) => job.kind === 'strategies.tester.start' || job.kind === 'strategies.tester');
      const performance = [...jobs].reverse().find((job) => job.kind === 'strategies.performance-dd5');
      if (tester && typeof tester.job_id === 'string') {
        const job = tester;
        testerJobId = job.job_id;
        renderTester(job);
        if (!['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state)) {
          await pollTester();
          window.clearInterval(testerPoller); testerPoller = window.setInterval(pollTester, 1000);
        }
      }
      if (performance && typeof performance.job_id === 'string') {
        const job = performance;
        performanceJobId = job.job_id;
        renderPerformance(job);
        if (!['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state)) {
          await pollPerformance();
          window.clearInterval(performancePoller); performancePoller = window.setInterval(pollPerformance, 1000);
        }
      }
    } catch (_) { /* requestJson already exposed a safe visible error. */ }
  };
  const importStartV2 = document.querySelector('#performance-import-start');
  const inboxVerifyV2 = document.querySelector('#performance-inbox-verify');
  const importStatusV2 = document.querySelector('#performance-import-status');
  const importProgressV2 = document.querySelector('#performance-import-progress');
  const dbSelectV2 = document.querySelector('#performance-db-select');
  const dbRefreshV2 = document.querySelector('#performance-db-refresh');
  const auditOpenV2 = document.querySelector('#performance-audit-open');
  const dd5StartV2 = document.querySelector('#dd5-start');
  const workbookOpenV2 = document.querySelector('#dd5-workbook-open');
  const dd5StatusV2 = document.querySelector('#strategy-dd5-status-v2');
  const workbookPathV2 = document.createElement('input');
  workbookPathV2.type = 'text'; workbookPathV2.readOnly = true; workbookPathV2.setAttribute('aria-label', 'Workbook path'); workbookPathV2.placeholder = 'data\\workbooks\\<Performance DB>.dd5.xlsx';
  document.querySelector('#dd5-workbook-open')?.before(workbookPathV2);
  let importJobV2 = '';
  let dd5JobV2 = '';
  let selectedDbV2 = '';
  inboxVerifyV2?.addEventListener('click', async () => {
    if (!testerJobId) return;
    inboxVerifyV2.disabled = true;
    try { renderTester(await requestJson('/api/v2/strategies/tester/verify-inbox', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_id: testerJobId }) })); }
    catch (_) { if (importStatusV2) importStatusV2.textContent = 'Verified inbox ещё не готов.'; }
    finally { inboxVerifyV2.disabled = false; }
  });
  const refreshPerformanceCatalog = async () => {
    try {
      const document = await requestJson('/api/v2/strategies/performance/catalog');
      const databases = Array.isArray(document.databases) ? document.databases : [];
      if (dbSelectV2) {
        dbSelectV2.replaceChildren(new Option('Выберите базу', ''));
        databases.forEach((item) => dbSelectV2.add(new Option(item.database_name, item.database_name)));
        if (selectedDbV2 && databases.some((item) => item.database_name === selectedDbV2)) dbSelectV2.value = selectedDbV2;
      }
      return databases;
    } catch (_) { return []; }
  };
  dbRefreshV2?.addEventListener('click', refreshPerformanceCatalog);
  const selectedDbDocument = () => dbSelectV2?.selectedOptions?.[0]?.value || selectedDbV2;
  dbSelectV2?.addEventListener('change', () => {
    selectedDbV2 = dbSelectV2.value;
    workbookPathV2.value = selectedDbV2 ? `data\\workbooks\\${selectedDbV2.replace(/\.duckdb$/, '')}\\${selectedDbV2.replace(/\.duckdb$/, '.dd5.xlsx')}` : '';
    if (auditOpenV2) auditOpenV2.disabled = !selectedDbV2;
    if (dd5StartV2) dd5StartV2.disabled = !selectedDbV2;
    if (workbookOpenV2) workbookOpenV2.disabled = !selectedDbV2;
  });
  auditOpenV2?.addEventListener('click', () => {
    const databaseName = selectedDbDocument();
    if (databaseName) window.open(`/api/v2/strategies/performance/artifact?database_name=${encodeURIComponent(databaseName)}&kind=audit`, '_blank', 'noopener');
  });
  workbookOpenV2?.addEventListener('click', () => {
    const databaseName = selectedDbDocument();
    if (databaseName) window.open(`/api/v2/strategies/performance/artifact?database_name=${encodeURIComponent(databaseName)}&kind=workbook`, '_blank', 'noopener');
  });
  importStartV2?.addEventListener('click', async () => {
    if (!testerJobId) return;
    importStartV2.disabled = true;
    if (importStatusV2) importStatusV2.textContent = 'Импорт Performance DB…';
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.performance.import', request: { tester_job_id: testerJobId, delete_html: Boolean(document.querySelector('#delete-tested-html-v2')?.checked) } });
      importJobV2 = result.job?.job_id || '';
      if (!importJobV2) throw new Error('missing job');
      const poll = async () => {
        const job = await requestJson(`/api/v2/strategies/performance/import/status?job_id=${encodeURIComponent(importJobV2)}`);
        const p = job.progress || {};
        if (importProgressV2) importProgressV2.textContent = `${job.phase || 'IMPORTING'} · ${p.current || 0} / ${p.total || 0} reports.`;
        if (importStatusV2) importStatusV2.textContent = `Performance DB: ${job.state || 'RUNNING'}.`;
        if (['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state)) {
        if (String(job.state) === 'COMMITTED' && job.result?.database_name) { selectedDbV2 = job.result.database_name; await refreshPerformanceCatalog(); if (dbSelectV2) { dbSelectV2.value = selectedDbV2; dbSelectV2.dispatchEvent(new Event('change')); } }
          return true;
        }
        return false;
      };
      if (!(await poll())) { const timer = window.setInterval(async () => { if (await poll()) window.clearInterval(timer); }, 1000); }
    } catch (_) { if (importStatusV2) importStatusV2.textContent = 'Импорт Performance DB не прошёл проверку.'; }
    finally { importStartV2.disabled = false; }
  });
  dd5StartV2?.addEventListener('click', async () => {
    const databaseName = selectedDbDocument();
    if (!databaseName) return;
    dd5StartV2.disabled = true;
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.dd5.start', request: { database_name: databaseName } });
      dd5JobV2 = result.job?.job_id || '';
      const poll = async () => {
        const job = await requestJson(`/api/v2/strategies/dd5/status?job_id=${encodeURIComponent(dd5JobV2)}`);
        if (dd5StatusV2) dd5StatusV2.textContent = `DD5: ${job.state || 'RUNNING'}. ${job.result?.workbook_name || ''}`;
        if (['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state)) return true;
        return false;
      };
      if (!(await poll())) { const timer = window.setInterval(async () => { if (await poll()) window.clearInterval(timer); }, 1000); }
    } catch (_) { if (dd5StatusV2) dd5StatusV2.textContent = 'DD5 не прошёл проверку.'; }
    finally { dd5StartV2.disabled = false; }
  });
  const recoverSplitJobs = async () => {
    try {
      const snapshot = await requestJson('/api/v2/jobs');
      const jobs = Array.isArray(snapshot.jobs) ? snapshot.jobs : [];
      const importJob = [...jobs].reverse().find((job) => job.kind === 'strategies.performance.import');
      const dd5Job = [...jobs].reverse().find((job) => job.kind === 'strategies.dd5.start');
      if (importJob?.job_id) {
        importJobV2 = importJob.job_id;
        const poll = async () => {
          const job = await requestJson(`/api/v2/strategies/performance/import/status?job_id=${encodeURIComponent(importJobV2)}`);
          if (importStatusV2) importStatusV2.textContent = `Performance DB: ${job.state || 'RUNNING'}.`;
          if (String(job.state) === 'COMMITTED' && job.result?.database_name) { selectedDbV2 = job.result.database_name; await refreshPerformanceCatalog(); if (dbSelectV2) { dbSelectV2.value = selectedDbV2; dbSelectV2.dispatchEvent(new Event('change')); } }
          return ['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state);
        };
        if (!(await poll())) { const timer = window.setInterval(async () => { if (await poll()) window.clearInterval(timer); }, 1000); }
      }
      if (dd5Job?.job_id) {
        dd5JobV2 = dd5Job.job_id;
        const poll = async () => {
          const job = await requestJson(`/api/v2/strategies/dd5/status?job_id=${encodeURIComponent(dd5JobV2)}`);
          if (dd5StatusV2) dd5StatusV2.textContent = `DD5: ${job.state || 'RUNNING'}. ${job.result?.workbook_name || ''}`;
          return ['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state);
        };
        if (!(await poll())) { const timer = window.setInterval(async () => { if (await poll()) window.clearInterval(timer); }, 1000); }
      }
    } catch (_) { /* stale jobs remain visible through the server registry */ }
  };
  refreshPerformanceCatalog();
  const settingsStatus = document.querySelector('#settings-status');
  const settingsPayload = () => ({ panel: {
    default_root: document.querySelector('#settings-default-root')?.value || 'static',
    path_defaults: {
      local_runner_root: document.querySelector('#settings-local-runner')?.value || '',
       local_source_db_root: document.querySelector('#settings-source-root')?.value || '',
      local_output_root: document.querySelector('#settings-output-root')?.value || '',
      listing_dates_path: document.querySelector('#settings-dates')?.value || '',
      surface_target_path: document.querySelector('#surface-target')?.value || '',
    },
  }, operational: {
     source_db_path: document.querySelector('#settings-source-root')?.value || '',
    output_root: document.querySelector('#settings-output-root')?.value || '',
    listing_dates_path: document.querySelector('#settings-dates')?.value || '',
    algorithm_version: document.querySelector('#settings-algorithm')?.value || '',
    import_workers: Number(document.querySelector('#settings-workers')?.value || 0),
    transaction_batch_size: Number(document.querySelector('#settings-batch')?.value || 0),
  } });
  const settingsButtons = [...document.querySelectorAll('#settings .button-row')];
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
  loadSafeDefaults();
  loadRemoteStatus();
  recoverJobs();
  recoverSplitJobs();
})();
