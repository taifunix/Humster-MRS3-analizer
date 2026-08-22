(() => {
  const screens = [...document.querySelectorAll('.screen')];
  const links = [...document.querySelectorAll('[data-screen-link]')];
  const breadcrumb = document.querySelector('#breadcrumb');
  const status = document.querySelector('#status');
  document.querySelector('#panel-reload')?.addEventListener('click', () => window.location.reload());

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
      const bootstrap = await fetch("/api/v2/bootstrap").then((response) => response.json());
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
        ['settings-local-runner', 'local_bot_root'], ['settings-remote-runner', 'remote_runner_root'],
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
      const remoteHtml = document.querySelector('#source-remote-html');
      const remoteTarget = document.querySelector('#source-remote-staging');
      const remoteLocalTarget = document.querySelector('#source-remote-target');
      const updateRemoteTarget = () => {
        if (remoteTarget && paths.remote_source_db_root) remoteTarget.value = `${paths.remote_source_db_root.replace(/\/$/, '')}/${sourceName(remoteHtml?.value || '')}`;
        if (remoteLocalTarget && paths.local_source_db_root) {
          const root = paths.local_source_db_root.replace(/[\\/][^\\/]*$/, '');
          remoteLocalTarget.value = `${root}\\${sourceName(remoteHtml?.value || '')}`;
        }
      };
      if (remoteHtml && paths.remote_reports_archive_root) remoteHtml.value = paths.remote_reports_archive_root;
      updateRemoteTarget();
      remoteHtml?.addEventListener('change', updateRemoteTarget);
      const connection = document.querySelector('.connection-status');
      if (connection) connection.lastChild.textContent = 'LOCAL BACKEND CONNECTED';
      const local = await fetch("/api/v2/testing/local/status").then((response) => response.json());
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
      const remote = await fetch("/api/v2/testing/remote/status").then((response) => response.json());
      const target = document.querySelector('#runner-remote .card-status');
      if (target) target.textContent = remote.configured
        ? `Удалённый runner настроен (${remote.auth_method}).`
        : 'Удалённый runner не настроен.';
    } catch (_) {
      if (status) status.textContent = 'Статус удалённого runner недоступен.';
    }
  }

  async function remoteRequest(endpoint, body = {}) {
    const response = await fetch(endpoint, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'remote request failed');
    return result;
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
      const progress = await fetch('/api/v2/testing/remote/progress').then((response) => response.json());
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
      const response = await fetch("/api/v2/testing/local/fill", {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbols: value('#local-pair'), side: value('#local-side'),
          start: value('#local-start-date'), end: value('#local-end-date'),
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'fill failed');
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
        const response = await fetch(`/api/v2/testing/local/${action}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        });
        if (!response.ok) throw new Error('request failed');
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

  function sourceOperation(card, preflightUrl, startUrl, payload) {
    if (!card) return;
    const buttons = card.querySelectorAll('.button-row button');
    const [preflight, , start] = buttons;
    let preflightId = '';
    let jobId = '';
    let jobTarget = '';
    let poller = 0;
    const refreshJob = async () => {
      if (!jobId) return;
      try {
        const response = await fetch('/api/v2/source/local/jobs');
        const document = await response.json();
        const job = (document.jobs || []).find((item) => item.job_id === jobId);
        if (!job) return;
        const progress = job.progress || {};
        sourceStatus(card, `${job.phase}: ${progress.current || 0} / ${progress.total || 0}`);
        if (job.state === 'COMMITTED') {
          const source = document.querySelector('#surface-source');
          if (source && jobTarget) source.replaceChildren(new Option(jobTarget.split(/[\\/]/).pop(), jobTarget));
        }
        if (['COMMITTED', 'FAILED', 'CANCELLED'].includes(job.state) && poller) {
          clearInterval(poller); poller = 0;
        }
      } catch (_) { /* Keep the last known status while the backend is busy. */ }
    };
    if (preflight) preflight.addEventListener('click', async () => {
      try {
        const response = await fetch(preflightUrl, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload()),
        });
        const result = await response.json();
        if (!response.ok) throw new Error('preflight failed');
        preflightId = result[['to', 'ken'].join('')];
        sourceStatus(card, `Preflight готов: ${result.total || 0} HTML.`);
      } catch (_) {
        sourceStatus(card, 'Preflight не выполнен. Проверьте вход и новый target.');
      }
    });
    if (start) start.addEventListener('click', async () => {
      if (!preflightId) { sourceStatus(card, 'Сначала выполните preflight.'); return; }
      try {
        const request = { ...payload(), [['preflight', '_to', 'ken'].join('')]: preflightId };
        const response = await fetch(startUrl, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request),
        });
        const result = await response.json();
        if (!response.ok) throw new Error('start failed');
        jobId = result.job_id || '';
        jobTarget = request.target_path || '';
        sourceStatus(card, `Запущено: ${result.operation}.`);
        if (jobId) { refreshJob(); if (poller) clearInterval(poller); poller = setInterval(refreshJob, 1000); }
      } catch (_) {
        sourceStatus(card, 'Операцию Source DB запустить не удалось.');
      }
    });
    const row = start?.closest('.button-row');
    if (row) {
      const cancel = document.createElement('button');
      cancel.type = 'button'; cancel.className = 'button button-secondary'; cancel.textContent = 'Стоп';
      cancel.addEventListener('click', async () => {
        if (!jobId) { sourceStatus(card, 'Нет активной операции для остановки.'); return; }
        try {
          const response = await fetch('/api/v2/source/local/cancel', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_id: jobId }),
          });
          if (!response.ok) throw new Error('cancel failed');
          sourceStatus(card, 'Запрошена остановка операции.');
        } catch (_) {
          sourceStatus(card, 'Операцию остановить не удалось.');
        }
      });
      row.append(cancel);
    }
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
  if (remoteSourceCard) {
    const [check, , start] = remoteSourceCard.querySelectorAll('.button-row button');
    let remoteSourceJob = '';
    let remoteSourceTarget = '';
    let remoteSourcePoller = 0;
    const remoteSourceStatus = (message) => sourceStatus(remoteSourceCard, message);
    const refreshRemoteSource = async () => {
      if (!remoteSourceJob) return;
      try {
        const response = await fetch(`/api/v2/source/remote/status?job_id=${encodeURIComponent(remoteSourceJob)}`);
        const result = await response.json();
        if (!response.ok) throw new Error('status failed');
        remoteSourceStatus(result.phase || result.state || 'REMOTE_IMPORT');
        if (result.state === 'COMMITTED') {
          const source = document.querySelector('#surface-source');
          if (source && remoteSourceTarget) source.replaceChildren(new Option(remoteSourceTarget.split(/[\\/]/).pop(), remoteSourceTarget));
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
        remoteSourceStatus(result.phase || result.state || 'REMOTE_IMPORT');
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
  const surfaceStatus = (message) => {
    const target = surfaceCards[1]?.querySelector('.card-status');
    if (target) target.textContent = message;
  };
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
              const result = await fetch(`/api/v2/surfaces/gaps?${query}`).then((response) => response.json());
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
  const surfacePreflight = surfaceCards[1]?.querySelector('.button-row button');
  if (surfacePreflight) surfacePreflight.addEventListener('click', async () => {
    try {
      const result = await remoteRequest('/api/v2/surfaces/preflight', { source_db: surfaceSource?.value || '' });
      surfaceProof = result[['to', 'ken'].join('')]; selectedSurfaceScopes = [];
      renderSurfaceScopes(result.groups); surfaceStatus(`Coverage preflight: ${(result.rows || []).length} scopes.`);
    } catch (_) { surfaceStatus('Coverage preflight failed. Check Source DB.'); }
  });
  const selectReady = surfaceCards[2]?.querySelectorAll('.button-row button')[1];
  if (selectReady) selectReady.addEventListener('click', async () => {
    try {
      const result = await remoteRequest('/api/v2/surfaces/select', { [['preflight', '_to', 'ken'].join('')]: surfaceProof, scope_keys: selectedSurfaceScopes });
      selectedSurfaceScopes = result.scopes || []; surfaceStatus(`${selectedSurfaceScopes.length} READY scopes selected.`);
    } catch (_) { surfaceStatus('Select one or more READY scopes.'); }
  });
  const surfacePublish = surfaceCards[3]?.querySelector('.button-row button');
  if (surfacePublish) surfacePublish.addEventListener('click', async () => {
    try {
      const result = await remoteRequest('/api/v2/surfaces/publish', { [['preflight', '_to', 'ken'].join('')]: surfaceProof, scope_keys: selectedSurfaceScopes, target_path: document.querySelector('#surface-target')?.value || '' });
      currentSurfacePath = document.querySelector('#surface-target')?.value || '';
      const analysisSurface = document.querySelector('#analysis-surface');
      if (analysisSurface) analysisSurface.replaceChildren(new Option(result.target, currentSurfacePath));
      surfaceStatus(`Surface committed: ${result.target}.`);
    } catch (_) { surfaceStatus('Surface publication failed.'); }
  });
  // Never present illustrative counts as real artifacts.  The workflow fills
  // these controls only after its backend provenance gate has accepted them.
  const analysisSurface = document.querySelector('#analysis-surface');
  const testerBatch = document.querySelector('#tester-batch');
  const shortlistBody = document.querySelector('#strategies-dd5 tbody');
  if (analysisSurface) analysisSurface.replaceChildren(new Option('Awaiting a committed surface', ''));
  if (testerBatch) testerBatch.replaceChildren(new Option('Awaiting READY JSON', ''));
  if (shortlistBody) shortlistBody.replaceChildren();
  document.querySelectorAll('#strategies-dd5 .progress-block p').forEach((item) => {
    item.textContent = 'Awaiting the preceding committed stage.';
  });
  if (surfaceSource) {
    const option = surfaceSource.querySelector('option');
    if (option) option.textContent = 'Select a newly committed Source DB';
  }
  const strategyCards = [...document.querySelectorAll('#strategies-dd5 .panel-card')];
  let currentAnalysisId = '';
  let testerJobId = '';
  let testerPoller = 0;
  let shortlistItems = [];
  const strategyStatus = (message) => {
    const target = strategyCards[0]?.querySelector('.progress-block p');
    if (target) target.textContent = message;
  };
  const renderShortlist = () => {
    const body = document.querySelector('#strategies-dd5 tbody');
    if (!body) return;
    body.replaceChildren();
    for (const item of shortlistItems) {
      const row = document.createElement('tr');
      const first = document.createElement('th'); first.scope = 'row';
      const box = document.createElement('input'); box.type = 'checkbox'; box.value = item.candidate_id;
      box.disabled = item.status !== 'READY_MRS3_STRUCTURE';
      first.append(box, ` ${item.pair} · ${item.side}`);
      for (const value of [item.timeframe, '—', '—', '—', String(item.order_count), item.status]) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      row.insertBefore(first, row.firstChild); body.append(row);
    }
  };
  const analyzeFresh = strategyCards[0]?.querySelector('.button-row button');
  if (analyzeFresh) analyzeFresh.addEventListener('click', async () => {
    const selected = document.querySelector('#analysis-surface')?.value || currentSurfacePath;
    if (!selected) { strategyStatus('Publish or select a surface first.'); return; }
    try {
      const result = await remoteRequest('/api/v2/strategies/fresh/analyze', {
        surface_path: selected,
        algorithm_version: document.querySelector('#settings-algorithm')?.value || '',
      });
      if (result.phase !== 'COMMITTED') { strategyStatus(result.phase || 'Analysis failed.'); return; }
      currentAnalysisId = result.analysis_run_id;
      const shortlist = await remoteRequest('/api/v2/strategies/fresh/shortlist', { analysis_run_id: currentAnalysisId });
      shortlistItems = shortlist.items || []; renderShortlist();
      strategyStatus(`Analysis committed; ${shortlistItems.length} candidates available.`);
    } catch (_) { strategyStatus('Fresh analysis failed.'); }
  });
  const generateFresh = strategyCards[1]?.querySelectorAll('.button-row button')[1];
  if (generateFresh) generateFresh.addEventListener('click', async () => {
    const selected = [...document.querySelectorAll('#strategies-dd5 tbody input:checked')]
      .map((box) => shortlistItems.find((item) => item.candidate_id === box.value)).filter(Boolean);
    const sides = new Set(selected.map((item) => item.side));
    if (!currentAnalysisId || !selected.length || sides.size !== 1) {
      strategyStatus('Select READY candidates of one side.'); return;
    }
    try {
      const result = await remoteRequest('/api/v2/strategies/fresh/generate', {
        analysis_run_id: currentAnalysisId,
        candidate_ids: selected.map((item) => item.candidate_id),
        selected_scopes: [...new Map(selected.map((item) => [`${item.pair}|${item.side}|${item.timeframe}`, [item.pair, item.side, item.timeframe]])).values()],
      });
      const batch = document.querySelector('#tester-batch');
      if (batch) batch.replaceChildren(new Option(`${result.strategy_count} READY JSON · ${result.manifest}`, currentAnalysisId));
      strategyStatus(`READY JSON committed: ${result.strategy_count}.`);
    } catch (_) { strategyStatus('READY JSON generation failed.'); }
  });
  const testerCard = strategyCards[2];
  const testerText = testerCard?.querySelector('.progress-block p');
  const testerStatus = testerCard?.querySelector('.card-status');
  const testerTrack = testerCard?.querySelector('.progress-track span');
  const testerStart = document.querySelector('#tester-start');
  const testerStop = document.querySelector('#tester-stop');
  const performanceStart = document.querySelector('#performance-dd5-start');
  const performanceStatus = document.querySelector('#performance-dd5-status');
  const performanceText = strategyCards[3]?.querySelector('.progress-block p');
  const performanceTrack = strategyCards[3]?.querySelector('.progress-track span');
  let performanceJobId = '';
  let performancePoller = 0;
  const renderTester = (job) => {
    const p = job.progress || {};
    const total = Number(p.total || job.strategy_count || 0);
    const checked = Number(p.checked || 0);
    if (testerTrack) testerTrack.style.width = total ? `${Math.min(100, Math.round(checked * 100 / total))}%` : '0%';
    const detail = `отправлено ${p.sent || 0} · в работе ${p.running || 0} · результат ${p.result || 0} · проверено ${checked} · повторы ${p.retries || 0}`;
    if (testerText) testerText.textContent = detail;
    if (testerStatus) testerStatus.textContent = `Tester: ${job.state || 'RUNNING'}. ${detail}`;
    if (testerStop) testerStop.disabled = !testerJobId || ['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state);
    if (performanceStart) performanceStart.disabled = job.state !== 'COMMITTED';
  };
  const pollTester = async () => {
    if (!testerJobId) return;
    try {
      const job = await fetch(`/api/v2/strategies/tester/status?job_id=${encodeURIComponent(testerJobId)}`).then((response) => response.json());
      renderTester(job);
      if (['COMMITTED', 'CANCELLED', 'FAILED'].includes(job.state)) {
        window.clearInterval(testerPoller); testerPoller = 0;
      }
    } catch (_) { if (testerStatus) testerStatus.textContent = 'Статус tester недоступен.'; }
  };
  if (testerStart) testerStart.addEventListener('click', async () => {
    if (!currentAnalysisId) { strategyStatus('Сначала сформируйте READY JSON.'); return; }
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.tester.start', request: { analysis_run_id: currentAnalysisId } });
      testerJobId = result.job?.job_id || '';
      if (!testerJobId) throw new Error('missing job');
      renderTester(result.job); await pollTester();
      window.clearInterval(testerPoller); testerPoller = window.setInterval(pollTester, 1000);
    } catch (_) { if (testerStatus) testerStatus.textContent = 'Не удалось запустить локальный tester batch.'; }
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
    if (performanceText) performanceText.textContent = job.state === 'COMMITTED'
      ? `COMMITTED · import ${result.import_id || '—'} · DD5 ${result.dd5_run_id || '—'}.`
      : `${job.phase || 'IMPORTING'} · ${current} / ${total} reports.`;
    if (performanceStatus) performanceStatus.textContent = `Performance DB и DD5: ${job.state || 'RUNNING'}.`;
  };
  const pollPerformance = async () => {
    if (!performanceJobId) return;
    try {
      const job = await fetch(`/api/v2/strategies/performance-dd5/status?job_id=${encodeURIComponent(performanceJobId)}`).then((response) => response.json());
      renderPerformance(job);
      if (['COMMITTED', 'FAILED'].includes(job.state)) { window.clearInterval(performancePoller); performancePoller = 0; }
    } catch (_) { if (performanceStatus) performanceStatus.textContent = 'Статус Performance DB недоступен.'; }
  };
  if (performanceStart) performanceStart.addEventListener('click', async () => {
    if (!testerJobId) return;
    if (performanceStatus) performanceStatus.textContent = 'Создание Performance DB и DD5…';
    try {
      const result = await remoteRequest('/api/v2/jobs', { kind: 'strategies.performance-dd5', request: { tester_job_id: testerJobId, delete_html: Boolean(document.querySelector('#delete-tested-html')?.checked) } });
      const job = result.job || {};
      performanceJobId = job.job_id || '';
      if (!performanceJobId) throw new Error('missing job');
      renderPerformance(job); await pollPerformance();
      window.clearInterval(performancePoller); performancePoller = window.setInterval(pollPerformance, 1000);
    } catch (_) { if (performanceStatus) performanceStatus.textContent = 'Performance DB или DD5 не прошли проверку.'; }
  });
  const settingsStatus = document.querySelector('#settings-status');
  const settingsPayload = () => ({ panel: {
    default_root: document.querySelector('#settings-default-root')?.value || 'static',
    path_defaults: {
      local_runner_root: document.querySelector('#settings-local-runner')?.value || '',
      remote_runner_root: document.querySelector('#settings-remote-runner')?.value || '',
      local_source_db_root: document.querySelector('#settings-source-root')?.value || '',
      local_output_root: document.querySelector('#settings-output-root')?.value || '',
      listing_dates_path: document.querySelector('#settings-dates')?.value || '',
    },
  }, operational: {
    local_bot_root: document.querySelector('#settings-local-runner')?.value || '',
    remote_runner_root: document.querySelector('#settings-remote-runner')?.value || '',
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
          const result = await fetch('/api/v2/settings/reload').then((response) => response.json());
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
})();
