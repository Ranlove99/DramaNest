(function () {
  var $ = function (id) { return document.getElementById(id); };

  // 应用根路径：端口模式为空（/api），统一网关模式为 /app/dramanest（/app/dramanest/api）
  var API_ROOT = (function () {
    var m = location.pathname.match(/^(\/app\/[^\/]+)/);
    return m ? m[1] : '';
  })();

  function api(path, opts) {
    return fetch(API_ROOT + '/api' + path, opts).then(function (r) {
      return r.json().catch(function () { return {}; });
    });
  }
  function post(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  var LABEL = {
    queued: '排队中', parsing: '解析中', downloading: '下载中', paused: '已暂停',
    partial: '部分完成', done: '完成', error: '失败', cancelled: '已取消'
  };
  var EPLABEL = {
    pending: '待下载', downloading: '下载中', paused: '已暂停',
    done: '已完成', error: '失败'
  };

  // ---------- 标签页 / 设置二级页面 ----------
  var PAGES = ['cms', 'new', 'scan', 'tasks'];

  function showPage(name) {
    PAGES.forEach(function (p) {
      var el = document.querySelector('.page[data-page="' + p + '"]');
      if (el) el.classList.toggle('hidden', p !== name);
    });
    Array.prototype.forEach.call(document.querySelectorAll('#mainTabs .tab'), function (b) {
      b.classList.toggle('active', b.getAttribute('data-page') === name);
    });
    $('settingsView').classList.add('hidden');
    $('mainTabs').classList.remove('hidden');
    document.querySelector('.layout').classList.remove('hidden');
    window.scrollTo && window.scrollTo(0, 0);
  }

  function showSettings() {
    PAGES.forEach(function (p) {
      var el = document.querySelector('.page[data-page="' + p + '"]');
      if (el) el.classList.add('hidden');
    });
    Array.prototype.forEach.call(document.querySelectorAll('#mainTabs .tab'), function (b) {
      b.classList.remove('active');
    });
    $('mainTabs').classList.add('hidden');
    document.querySelector('.layout').classList.add('hidden');
    $('settingsView').classList.remove('hidden');
    if ($('settingsView').scrollIntoView) $('settingsView').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  Array.prototype.forEach.call(document.querySelectorAll('#mainTabs .tab'), function (b) {
    b.onclick = function () { showPage(b.getAttribute('data-page')); };
  });
  $('settingsBtn').onclick = showSettings;
  $('settingsBack').onclick = function () { showPage('cms'); };

  var EPOPEN = {};   // 任务 → 是否展开分集进度

  function epSummary(t) {
    var st = t.ep_state || [];
    if (!st.length) return '';
    var c = { done: 0, downloading: 0, paused: 0, error: 0, pending: 0 };
    st.forEach(function (s) { c[s.st || 'pending'] = (c[s.st || 'pending'] || 0) + 1; });
    var parts = [];
    if (c.done) parts.push('<b class="ok">完成 ' + c.done + '</b>');
    if (c.downloading) parts.push('<b class="run">下载 ' + c.downloading + '</b>');
    if (c.paused) parts.push('<b class="pz">暂停 ' + c.paused + '</b>');
    if (c.error) parts.push('<b class="bad">失败 ' + c.error + '</b>');
    if (c.pending) parts.push('待下载 ' + c.pending);
    return parts.join(' · ');
  }

  function epGrid(t) {
    var st = t.ep_state || [];
    if (!st.length) return '<div class="hint">尚未解析分集</div>';
    var cells = st.map(function (s, i) {
      var stt = s.st || 'pending';
      var txt = String(i + 1);
      if (stt === 'downloading' && s.pct) txt = (i + 1) + '·' + s.pct + '%';
      else if (stt === 'done') txt = '✓' + (i + 1);
      else if (stt === 'error') txt = '✕' + (i + 1);
      else if (stt === 'paused') txt = '❙❙' + (i + 1);
      var tip = (s.t || ('第 ' + (i + 1) + ' 集')) + ' · ' + (EPLABEL[stt] || stt) +
        (s.size ? ' · ' + s.size : '') + (s.sp ? ' · ' + s.sp : '') + (s.err ? ' · ' + s.err : '') +
        (stt === 'done' ? '' : '　（点击' + (stt === 'paused' ? '开始' : '暂停') + '）');
      return '<div class="ep-cell ep-' + stt + '" data-tid="' + t.id + '" data-i="' + i +
        '" title="' + esc(tip) + '">' + esc(txt) + '</div>';
    }).join('');
    return '<div class="ep-grid">' + cells + '</div>';
  }

  function taskOps(t) {
    var b = [];
    var running = (t.status === 'downloading' || t.status === 'parsing' || t.status === 'queued');
    if (running) {
      b.push('<button class="ghost small" onclick="pauseTask(\'' + t.id + '\')">⏸ 暂停</button>');
      b.push('<button class="ghost small" onclick="cancelTask(\'' + t.id + '\')">取消</button>');
    }
    if (t.status === 'paused') {
      b.push('<button class="small" onclick="resumeTask(\'' + t.id + '\')">▶ 继续</button>');
    }
    if (t.status === 'paused' || t.status === 'error' || t.status === 'cancelled' || t.status === 'partial') {
      b.push('<button class="ghost small" onclick="retryTask(\'' + t.id + '\')">↻ 重试</button>');
    }
    if (t.ep_state && t.ep_state.length) {
      b.push('<button class="ghost small" onclick="toggleEps(\'' + t.id + '\')">' +
        (EPOPEN[t.id] ? '收起分集' : '分集进度') + '</button>');
    }
    b.push('<button class="ghost small" onclick="delTask(\'' + t.id + '\')">删除</button>');
    return b.join('');
  }

  function renderTasks(tasks) {
    var box = $('tasks');
    if (!tasks || !tasks.length) {
      box.innerHTML = '<div class="empty">还没有任务</div>';
      return;
    }
    box.innerHTML = tasks.map(function (t) {
      var pct = 0;
      if (t.status === 'done') pct = 100;
      else if (typeof t.pct === 'number') pct = t.pct;
      else if (t.total) pct = Math.round((t.current || 0) / t.total * 100);

      // 指标区：集数 / 百分比 / 已下大小 / 实时速度 / 剩余时间
      var chips = [];
      var doneCnt = (t.ep_state || []).filter(function (s) { return s.st === 'done'; }).length;
      if (t.total) {
        var shown = (t.ep_state && t.ep_state.length) ? doneCnt
          : (t.status === 'done' ? t.total : (t.current || 0));
        chips.push('<span class="chip chip-ep">📺 ' + shown + '/' + t.total + ' 集</span>');
      }
      if (t.status === 'downloading' || t.status === 'done' || t.status === 'partial') {
        chips.push('<span class="chip chip-pct">' + pct + '%</span>');
      }
      if (t.mode_text && (t.status === 'downloading' || t.status === 'partial')) {
        chips.push('<span class="chip chip-mode">🔧 ' + esc(t.mode_text) + '</span>');
      }
      if (t.size_text) chips.push('<span class="chip">💾 ' + esc(t.size_text) + '</span>');
      if (t.speed_text) chips.push('<span class="chip chip-speed">⚡ ' + esc(t.speed_text) + '</span>');
      else if (t.ff_speed) chips.push('<span class="chip">⚡ ' + esc(t.ff_speed) + ' 实时</span>');
      if (t.status === 'downloading' && t.eta && t.eta !== '—') {
        chips.push('<span class="chip">⏳ 剩余 ' + esc(t.eta) + '</span>');
      }
      if (t.elapsed && t.elapsed !== '00:00' && t.status === 'downloading') {
        chips.push('<span class="chip chip-muted">用时 ' + esc(t.elapsed) + '</span>');
      }

      // 本集内进度条（当能拿到总时长时显示）
      var epBar = '';
      if (t.status === 'downloading' && typeof t.ep_pct === 'number') {
        epBar = '<div class="bar bar-ep"><i style="width:' + t.ep_pct + '%"></i></div>';
      }
      var sum = epSummary(t);
      var open = !!EPOPEN[t.id];
      var warn = '';
      if (t.dir_warn) warn += '<div class="task-warn">⚠ ' + esc(t.dir_warn) + '</div>';
      if (t.last_error && (t.status === 'error' || t.status === 'partial')) {
        warn += '<div class="task-err">最近错误：' + esc(t.last_error) + '</div>';
      }
      return '<div class="task">' +
        '<div class="top"><div class="name">' + esc(t.title || t.url || t.series) + '</div>' +
        '<span class="status st-' + esc(t.status) + '">' + (LABEL[t.status] || t.status) + '</span></div>' +
        (chips.length ? '<div class="chips">' + chips.join('') + '</div>' : '') +
        '<div class="prog">' + esc(t.progress || '') + '</div>' +
        warn +
        '<div class="bar"><i style="width:' + pct + '%"></i></div>' + epBar +
        (sum ? '<div class="ep-sum">' + sum + '</div>' : '') +
        (open ? epGrid(t) : '') +
        '<div class="ops">' + taskOps(t) + '</div></div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.ep-cell'), function (c) {
      c.onclick = function () {
        var tid = c.getAttribute('data-tid'), i = +c.getAttribute('data-i');
        var task = (window.__TASKS || []).filter(function (x) { return x.id === tid; })[0];
        var cur = task && task.ep_state && task.ep_state[i] ? task.ep_state[i].st : 'pending';
        if (cur === 'done') { return; }
        var todo = (cur === 'paused') ? 'start' : 'pause';
        act('/ep/' + tid, { index: i, action: todo });
      };
    });
  }

  function refresh() {
    api('/tasks').then(function (d) {
      window.__TASKS = d.tasks || [];
      renderTasks(d.tasks);
      if ($('dlStat')) {
        $('dlStat').textContent = d.limit ? '并发 ' + (d.active || 0) + '/' + d.limit : '';
      }
    });
  }

  function act(path, body) {
    return post(path, body || {}).then(function () {
      refresh();
      // 后端要等 ffmpeg 退出后才写新状态，稍后再拉一次
      setTimeout(refresh, 900);
    });
  }

  window.cancelTask = function (id) { act('/cancel/' + id); };
  window.delTask = function (id) { act('/delete/' + id); };
  window.pauseTask = function (id) { act('/pause/' + id); };
  window.resumeTask = function (id) { act('/resume/' + id); };
  window.retryTask = function (id) { act('/retry/' + id); };
  window.toggleEps = function (id) { EPOPEN[id] = !EPOPEN[id]; refresh(); };

  $('parseBtn').onclick = function () {
    var url = $('url').value.trim();
    if (!url) return;
    var btn = this; btn.disabled = true; btn.textContent = '解析中';
    post('/parse', { url: url }).then(function (d) {
      btn.disabled = false; btn.textContent = '解析';
      if (d.error) { alert(d.error); return; }
      var p = $('preview'); p.classList.remove('hidden');
      p.innerHTML = '<b>' + esc(d.series) + '</b>　共 ' + d.episodes.length + ' 集<br>' +
        d.episodes.map(function (e) { return esc(e.title); }).join('　·　');
    }).catch(function (e) {
      btn.disabled = false; btn.textContent = '解析';
      alert('解析失败：' + e);
    });
  };

  $('dlBtn').onclick = function () {
    var url = $('url').value.trim();
    if (!url) { alert('请先填写地址'); return; }
    var urls = url.split(/\n+/).map(function (s) { return s.trim(); }).filter(Boolean);
    var btn = this; btn.disabled = true;
    var chain = Promise.resolve();
    urls.forEach(function (u) {
      chain = chain.then(function () { return post('/download', { url: u }); });
    });
    chain.then(function () {
      btn.disabled = false;
      $('url').value = '';
      $('preview').classList.add('hidden');
      refresh();
    }).catch(function (e) { btn.disabled = false; alert('创建失败：' + e); });
  };

  $('refreshBtn').onclick = refresh;
  $('clearBtn').onclick = function () { post('/clear-done', {}).then(refresh); };

  var SCAN = null, SHOWN = 200;

  function scanFiltered() {
    if (!SCAN) return [];
    var kw = ($('scanFilter').value || '').trim().toLowerCase();
    var sort = $('scanSort').value;
    var list = (SCAN.videos || []).filter(function (v) {
      if (!kw) return true;
      return (v.title || '').toLowerCase().indexOf(kw) >= 0 ||
             (v.tags || []).join(' ').toLowerCase().indexOf(kw) >= 0;
    });
    if (sort === 'hot') list.sort(function (a, b) { return (b.hot || 0) - (a.hot || 0); });
    else if (sort === 'score') list.sort(function (a, b) { return (b.score || 0) - (a.score || 0); });
    else if (sort === 'eps') list.sort(function (a, b) { return (b.episodes || 0) - (a.episodes || 0); });
    else list.sort(function (a, b) { return b.id - a.id; });
    return list;
  }

  function updateSelCount() {
    var n = document.querySelectorAll('#scanList input[type=checkbox]:checked').length;
    $('selCount').textContent = n ? '(' + n + ')' : '';
  }

  function renderScan() {
    var list = scanFiltered();
    var show = list.slice(0, SHOWN);
    $('scanList').innerHTML = show.map(function (v) {
      var tags = (v.tags || []).map(function (t) {
        return '<span class="scan-tag">' + esc(t) + '</span>';
      }).join('');
      return '<div class="scan-item">' +
        '<input type="checkbox" data-id="' + v.id + '">' +
        '<div class="scan-main"><div class="scan-title">' + esc(v.title) + '</div>' +
        '<div class="scan-meta">' + (v.episodes || 0) + ' 集' +
        (v.score ? ' · ★ ' + v.score : '') +
        (v.finished ? ' · 已完结' : ' · 连载中') +
        (v.duration ? ' · 单集约 ' + esc(v.duration) + ' 分钟' : '') +
        '<br>' + tags + '</div></div>' +
        '<a href="' + esc(v.url) + '" target="_blank" rel="noopener">打开</a></div>';
    }).join('') || '<div class="empty">没有匹配的结果</div>';
    $('scanMore').classList.toggle('hidden', list.length <= SHOWN);
    $('moreBtn').textContent = '显示更多（还有 ' + Math.max(0, list.length - SHOWN) + ' 部）';
    Array.prototype.forEach.call(document.querySelectorAll('#scanList input[type=checkbox]'), function (cb) {
      cb.onchange = updateSelCount;
    });
    updateSelCount();
  }

  $('scanBtn').onclick = function () {
    var site = $('siteUrl').value.trim();
    if (!site) { alert('请填写站点地址'); return; }
    var btn = this; btn.disabled = true; btn.textContent = '扫描中…';
    var all = [], page = 1, pages = 1, seen = {};

    function fail(msg) {
      btn.disabled = false; btn.textContent = '重新扫描';
      $('scanStatus').textContent = '扫描失败：' + msg;
    }

    function nextPage() {
      post('/scan', { url: site, page: page }).then(function (d) {
        if (d.error) { fail(d.error); return; }
        pages = d.pages || 1;
        (d.videos || []).forEach(function (v) {
          if (!seen[v.id]) { seen[v.id] = 1; all.push(v); }
        });
        $('scanStatus').textContent = '正在扫描 ' + page + '/' + pages +
          ' 页（已获取 ' + all.length + ' 部）…';
        if (!d.finished && page < pages) { page++; nextPage(); return; }
        btn.disabled = false; btn.textContent = '重新扫描';
        SCAN = { site: d.site, total: all.length, videos: all };
        SHOWN = 200;
        $('scanCount').textContent = '共 ' + all.length + ' 部';
        $('scanStatus').textContent = '扫描完成 · ' + d.site;
        $('scanTools').classList.remove('hidden');
        renderScan();
      }).catch(function (e) { fail(e); });
    }
    nextPage();
  };

  $('scanFilter').oninput = function () { SHOWN = 200; renderScan(); };
  $('scanSort').onchange = function () { SHOWN = 200; renderScan(); };
  $('moreBtn').onclick = function () { SHOWN += 200; renderScan(); };

  $('selectAll').onclick = function () {
    if (!SCAN) return;
    var ids = {};
    scanFiltered().slice(0, 300).forEach(function (v) { ids[String(v.id)] = 1; });
    Array.prototype.forEach.call(document.querySelectorAll('#scanList input[type=checkbox]'), function (cb) {
      cb.checked = !!ids[cb.getAttribute('data-id')];
    });
    updateSelCount();
  };
  $('selectNone').onclick = function () {
    Array.prototype.forEach.call(document.querySelectorAll('#scanList input[type=checkbox]'), function (cb) {
      cb.checked = false;
    });
    updateSelCount();
  };

  $('batchBtn').onclick = function () {
    var boxes = document.querySelectorAll('#scanList input[type=checkbox]:checked');
    if (!boxes.length) { alert('请先勾选视频'); return; }
    if (boxes.length > 300) { alert('单次最多 300 部，请减少勾选'); return; }
    var items = Array.prototype.map.call(boxes, function (cb) {
      var id = cb.getAttribute('data-id');
      var v = (SCAN.videos || []).filter(function (x) { return String(x.id) === id; })[0];
      return { url: v ? v.url : id, title: v ? v.title : '' };
    });
    if (!confirm('确认下载选中的 ' + items.length + ' 部？\n已完成的会自动跳过，任务按并发数排队执行。')) return;
    var btn = this; btn.disabled = true;
    post('/download-batch', { base: SCAN.site, urls: items, skip_existing: true }).then(function (d) {
      btn.disabled = false;
      if (d.error) { alert(d.error); return; }
      $('scanStatus').textContent = '已创建 ' + d.created + ' 个任务' +
        (d.skipped ? '，跳过已完成 ' + d.skipped + ' 部' : '');
      var box = document.getElementById('tasks');
      if (box && box.scrollIntoView) box.scrollIntoView({ behavior: 'smooth' });
      refresh();
    }).catch(function (e) { btn.disabled = false; alert('创建失败：' + e); });
  };

  // ---------- CMS 资源站 ----------
  var CMSSITES = [], CMSPAGE = 1, CMSSOURCE = [], ADULT_ON = false;

  function cmsApi() { return ($('cmsSite').value || '').trim(); }
  function opt(s) { return '<option value="' + esc(s.api) + '">' + esc(s.name) + '</option>'; }

  function folderOf(s) {
    return s.folder || (s.category === 'adult' ? '成人内容' : '常用资源');
  }

  function renderSiteOptions() {
    // 按 folder 分组，保持站点原始顺序；成人组受开关控制；禁用的源单独一组放最后
    var groups = [], index = {}, off = [];
    CMSSITES.forEach(function (s) {
      if (s.disabled) { off.push(s); return; }
      var adult = s.category === 'adult';
      if (adult && !ADULT_ON) return;
      var f = folderOf(s);
      var key = (adult ? 'A:' : 'N:') + f;
      if (!(key in index)) { index[key] = groups.length; groups.push({ label: f, adult: adult, items: [] }); }
      groups[index[key]].items.push(s);
    });
    var html = groups.map(function (g) {
      var label = g.adult ? g.label + '（成人内容）' : g.label;
      return '<optgroup label="' + esc(label) + '">' + g.items.map(opt).join('') + '</optgroup>';
    }).join('');
    if (off.length) {
      html += '<optgroup label="已禁用（不可用）">' + off.map(function (s) {
        return '<option value="' + esc(s.api) + '" disabled>' + esc(s.name) + '</option>';
      }).join('') + '</optgroup>';
    }
    $('cmsSite').innerHTML = html || '<option value="">（无可用源）</option>';
    // 当前选中的源若被禁用，自动切到第一个可用源
    var sel = $('cmsSite');
    if (sel.selectedOptions && sel.selectedOptions[0] && sel.selectedOptions[0].disabled) {
      var first = sel.querySelector('option:not([disabled])');
      if (first) sel.value = first.value;
    }
  }

  function loadCmsSites(after) {
    api('/cms/sites').then(function (d) {
      CMSSITES = d.sites || [];
      ADULT_ON = !!d.adult_enabled;
      renderSiteOptions();
      $('cmsSitesJson').value = JSON.stringify(CMSSITES, null, 2);
      renderSiteRows();
      if (CMSSITES.length) cmsQuery(1);
      // 恢复上一次的测活分数（进程内缓存）
      api('/cms/sites/test').then(function (r) {
        if (r && r.results && Object.keys(r.results).length) {
          SITETEST = r.results;
          renderSiteRows();
          $('testMsg').textContent = testSummary(r);
          if (r.running) pollTest(true);
        }
        if (typeof after === 'function') after();
      });
    });
  }

  // ---------- 站点排序 / 文件夹分类 / 测活评分 ----------
  var SITETEST = {};

  function scoreCls(sc) {
    if (!sc) return 's-0';
    if (sc >= 85) return 's-hi';
    if (sc >= 65) return 's-mid';
    return 's-low';
  }

  function siteScoreOf(s) {
    var r = SITETEST[s.api];
    return (r && r.ok) ? r.score : -1;
  }

  function sortMode() {
    var el = $('siteSort');
    return (el && el.value) || 'manual';
  }

  function sortSites() {
    var arr = CMSSITES.slice();
    if (sortMode() === 'score') {
      // 文件夹按首次出现顺序分组，组内按分数从高到低（失效/未测沉底）
      var order = {}, seq = 0;
      arr.forEach(function (s) {
        var f = folderOf(s);
        if (!(f in order)) order[f] = seq++;
      });
      arr.sort(function (a, b) {
        var fa = order[folderOf(a)], fb = order[folderOf(b)];
        if (fa !== fb) return fa - fb;
        return siteScoreOf(b) - siteScoreOf(a);
      });
    }
    // 禁用的始终排最后
    CMSSITES = arr.filter(function (s) { return !s.disabled; })
      .concat(arr.filter(function (s) { return s.disabled; }));
  }

  function siteRowHtml(s, i, locked) {
    var adult = s.category === 'adult';
    var r = SITETEST[s.api];
    var badge = '<span class="score s-0" title="未测活">—</span>';
    if (r) {
      badge = '<span class="score ' + scoreCls(r.score) + '" title="' +
        esc(r.ok ? ('响应 ' + r.ms + 'ms · ' + r.count + ' 条 · ' + r.classes + ' 个分类')
                 : ('测活失败：' + (r.err || ''))) + '">' +
        (r.ok ? r.score + '分' : '失效') + '</span>';
    }
    var mvTip = locked ? '按分数排序时不可手动调整，先切回「手动排序」' : '';
    return '<div class="site-row' + (adult ? ' is-adult' : '') + (s.disabled ? ' is-off' : '') +
      '" draggable="' + (locked ? 'false' : 'true') + '" data-i="' + i + '">' +
      '<span class="drag"' + (locked ? ' title="' + esc(mvTip) + '"' : '') + '>' + (locked ? '·' : '⠿') + '</span>' +
      badge +
      '<span class="s-name">' + esc(s.name) + (adult ? ' <b class="tag-adult">成人</b>' : '') +
      (s.disabled ? ' <b class="tag-off">已禁用</b>' : '') + '</span>' +
      '<input class="s-folder" data-i="' + i + '" value="' + esc(folderOf(s)) + '" placeholder="文件夹">' +
      '<button class="ghost small mv" data-i="' + i + '" data-dir="-1" title="' + esc(mvTip || '上移') + '"' +
      (locked ? ' disabled' : '') + '>↑</button>' +
      '<button class="ghost small mv" data-i="' + i + '" data-dir="1" title="' + esc(mvTip || '下移') + '"' +
      (locked ? ' disabled' : '') + '>↓</button>' +
      '<button class="ghost small tog" data-i="' + i + '" title="' +
      (s.disabled ? '启用' : '禁用（不删除）') + '">' + (s.disabled ? '启用' : '禁用') + '</button>' +
      '<button class="ghost small del" data-i="' + i + '" title="删除">×</button>' +
      '</div>';
  }

  function renderSiteRows() {
    var box = $('siteRows');
    if (!box) return;
    if (!CMSSITES.length) { box.innerHTML = '<div class="empty">还没有站点</div>'; return; }
    sortSites();
    var mode = sortMode();
    var locked = mode === 'score';
    var lastKey = null;
    var html = [];
    CMSSITES.forEach(function (s, i) {
      var key = s.disabled ? '__off__' : folderOf(s);
      if (mode === 'score' && key !== lastKey) {
        lastKey = key;
        var cnt = CMSSITES.filter(function (x) {
          return (x.disabled ? '__off__' : folderOf(x)) === key;
        }).length;
        html.push('<div class="site-group">' + (s.disabled ? '🚫 已禁用' : '📁 ' + esc(key)) +
          ' <span class="gcount">' + cnt + '</span></div>');
      }
      html.push(siteRowHtml(s, i, locked));
    });
    box.innerHTML = html.join('');
    bindSiteRowEvents(locked);
  }

  function bindSiteRowEvents(locked) {
    Array.prototype.forEach.call(document.querySelectorAll('#siteRows .mv'), function (b) {
      b.onclick = function () {
        var i = +b.getAttribute('data-i'), dir = +b.getAttribute('data-dir'), j = i + dir;
        if (j < 0 || j >= CMSSITES.length) return;
        var t = CMSSITES[i]; CMSSITES[i] = CMSSITES[j]; CMSSITES[j] = t;
        renderSiteRows();
      };
    });
    Array.prototype.forEach.call(document.querySelectorAll('#siteRows .tog'), function (b) {
      b.onclick = function () {
        var i = +b.getAttribute('data-i');
        CMSSITES[i].disabled = !CMSSITES[i].disabled;
        renderSiteRows();
        $('orderMsg').textContent = '已' + (CMSSITES[i].disabled ? '禁用' : '启用') +
          '「' + CMSSITES[i].name + '」，记得点「保存排序与分类」';
      };
    });
    Array.prototype.forEach.call(document.querySelectorAll('#siteRows .del'), function (b) {
      b.onclick = function () {
        var i = +b.getAttribute('data-i');
        var name = CMSSITES[i].name;
        if (!confirm('确认彻底删除「' + name + '」？\n如果只是暂时不用，建议点「禁用」而不是删除。')) return;
        CMSSITES.splice(i, 1);
        renderSiteRows();
        $('orderMsg').textContent = '已删除，记得点「保存排序与分类」';
      };
    });
    Array.prototype.forEach.call(document.querySelectorAll('#siteRows .s-folder'), function (inp) {
      inp.onchange = function () {
        var i = +inp.getAttribute('data-i');
        CMSSITES[i].folder = inp.value.trim() || folderOf(CMSSITES[i]);
      };
    });
    // 拖动排序（按分数排序时锁定）
    var dragFrom = null;
    if (locked) return;
    Array.prototype.forEach.call(document.querySelectorAll('#siteRows .site-row'), function (row) {
      row.ondragstart = function () { dragFrom = +row.getAttribute('data-i'); row.classList.add('dragging'); };
      row.ondragend = function () { row.classList.remove('dragging'); };
      row.ondragover = function (e) { e.preventDefault(); row.classList.add('dragover'); };
      row.ondragleave = function () { row.classList.remove('dragover'); };
      row.ondrop = function (e) {
        e.preventDefault(); row.classList.remove('dragover');
        var to = +row.getAttribute('data-i');
        if (dragFrom === null || dragFrom === to) return;
        var moved = CMSSITES.splice(dragFrom, 1)[0];
        CMSSITES.splice(to, 0, moved);
        dragFrom = null;
        renderSiteRows();
      };
    });
  }

  // ---------- 一键测活 ----------
  function testSummary(d) {
    var rs = d.results || {};
    var apis = Object.keys(rs);
    var alive = apis.filter(function (k) { return rs[k].ok; }).length;
    if (d.running) {
      return '测活中 ' + d.done + '/' + d.total + '（可用 ' + alive + '）';
    }
    return '测活完成：' + alive + ' 可用 / ' + (apis.length - alive) + ' 失效';
  }

  var AUTOSORT = false;

  function pollTest(first) {
    api('/cms/sites/test').then(function (d) {
      SITETEST = d.results || {};
      if (!d.running && AUTOSORT) {
        AUTOSORT = false;
        $('siteSort').value = 'score';
      }
      renderSiteRows();
      $('testMsg').textContent = testSummary(d);
      if (d.running) {
        $('testSitesBtn').disabled = true;
        setTimeout(function () { pollTest(false); }, 900);
      } else {
        $('testSitesBtn').disabled = false;
        $('testSitesBtn').textContent = '一键测活评分';
      }
    });
  }

  $('siteSort').onchange = function () { renderSiteRows(); };

  $('testSitesBtn').onclick = function () {
    var btn = this;
    btn.disabled = true;
    btn.textContent = '测活中…';
    AUTOSORT = true;
    $('testMsg').textContent = '正在并发测活，请稍候…';
    post('/cms/sites/test', { threads: 8 }).then(function () { pollTest(true); })
      .catch(function (e) { btn.disabled = false; btn.textContent = '一键测活评分'; $('testMsg').textContent = '测活失败：' + e; });
  };

  $('disableBadBtn').onclick = function () {
    var bad = Object.keys(SITETEST).filter(function (k) { return !SITETEST[k].ok; });
    if (!bad.length) {
      $('testMsg').textContent = '没有检测到失效源，请先点「一键测活评分」';
      return;
    }
    if (!confirm('确认禁用 ' + bad.length + ' 个失效源？\n禁用不会删除，可在列表里点「启用」恢复。')) return;
    post('/cms/sites/disable', { apis: bad, disabled: true }).then(function (d) {
      if (d.error) { $('testMsg').textContent = d.error; return; }
      loadCmsSites(function () {
        $('testMsg').textContent = '已禁用 ' + bad.length + ' 个失效源（可在列表里重新启用）';
      });
    });
  };

  $('enableAllBtn').onclick = function () {
    var off = CMSSITES.filter(function (s) { return s.disabled; }).map(function (s) { return s.api; });
    if (!off.length) { $('testMsg').textContent = '当前没有被禁用的源'; return; }
    post('/cms/sites/disable', { apis: off, disabled: false }).then(function (d) {
      if (d.error) { $('testMsg').textContent = d.error; return; }
      loadCmsSites(function () {
        $('testMsg').textContent = '已启用 ' + off.length + ' 个源';
      });
    });
  };

  $('saveOrderBtn').onclick = function () {
    post('/cms/sites', { sites: CMSSITES }).then(function (d) {
      if (d.error) { $('orderMsg').textContent = d.error; return; }
      $('orderMsg').textContent = '已保存 ' + d.sites.length + ' 个站点';
      loadCmsSites();
      setTimeout(function () { $('orderMsg').textContent = ''; }, 2500);
    });
  };

  $('addSiteBtn').onclick = function () {
    var name = ($('newSiteName') && $('newSiteName').value || '').trim();
    var apiUrl = ($('newSiteApi') && $('newSiteApi').value || '').trim();
    if (!name || !apiUrl) { $('orderMsg').textContent = '请填写站点名称和接口地址'; return; }
    if (!/^https?:\/\//i.test(apiUrl)) { $('orderMsg').textContent = '接口地址需以 http(s):// 开头'; return; }
    var folder = ($('newFolder') && $('newFolder').value || '').trim() || '常用资源';
    CMSSITES.push({ name: name, api: apiUrl, category: 'normal', folder: folder });
    $('newSiteName').value = ''; $('newSiteApi').value = '';
    renderSiteRows();
    $('orderMsg').textContent = '已加入列表，点「保存排序与分类」生效';
  };

  $('cmsSaveSites').onclick = function () {
    var sites;
    try { sites = JSON.parse($('cmsSitesJson').value); }
    catch (e) { $('cmsSaveMsg').textContent = 'JSON 格式错误'; return; }
    if (!Array.isArray(sites)) { $('cmsSaveMsg').textContent = '必须是数组'; return; }
    post('/cms/sites', { sites: sites }).then(function (d) {
      $('cmsSaveMsg').textContent = d.ok ? '已保存 ' + d.sites.length + ' 个站点' : (d.error || '保存失败');
      if (d.ok) { loadCmsSites(); }
    });
  };

  function cmsQuery(page) {
    var a = cmsApi();
    if (!a) return;
    ALLMODE = false;
    $('allBar').classList.add('hidden');
    $('cmsPager').classList.remove('hidden');
    CMSPAGE = page || 1;
    $('cmsStatus').textContent = '正在读取...';
    post('/cms/list', {
      api: a, page: CMSPAGE,
      keyword: $('cmsKw').value.trim(), type_id: $('cmsType').value
    }).then(function (d) {
      if (d.error) { $('cmsStatus').textContent = '失败：' + d.error; return; }
      if (CMSPAGE === 1) {
        $('cmsType').innerHTML = '<option value="">全部分类</option>' + (d.class || []).map(function (c) {
          return '<option value="' + esc(c.id) + '">' + esc(c.name) + '</option>';
        }).join('');
      }
      $('cmsStatus').textContent = '共 ' + d.total + ' 部 · ' + d.pages + ' 页';
      $('cmsPage').textContent = CMSPAGE + ' / ' + d.pages;
      $('cmsList').innerHTML = (d.videos || []).map(function (v) {
        return '<div class="scan-item">' +
          '<div class="scan-main"><div class="scan-title">' + esc(v.title) + '</div>' +
          '<div class="scan-meta">' + esc(v.type) + (v.remarks ? ' · ' + esc(v.remarks) : '') +
          (v.time ? ' · ' + esc(v.time) : '') + '</div></div>' +
          '<button class="ghost small cms-open" data-id="' + esc(v.id) + '" data-title="' + esc(v.title) + '">选集</button>' +
          '</div>';
      }).join('') || '<div class="empty">没有找到视频</div>';
      Array.prototype.forEach.call(document.querySelectorAll('.cms-open'), function (b) {
        b.onclick = function () {
          var btn = this;
          btn.disabled = true; btn.textContent = '读取中';
          cmsDetail(a, b.getAttribute('data-id'), b.getAttribute('data-title'), btn);
        };
      });
    }).catch(function (e) { $('cmsStatus').textContent = '失败：' + e; });
  }

  // ---------- 视频预览播放器 ----------
  // 思路同 LibreTV：m3u8 交给 hls.js，所有上游请求走本地 /px 代理
  // （补 UA/Referer 绕防盗链、m3u8 内的分片和密钥地址一并改写）。
  var PL = { api: '', id: '', from: '', title: '', list: [], i: -1, hls: null };

  // 代理地址要走 base href 的前缀（飞牛上应用挂在 /app/dramanest/ 下），
  // 直接写 /px 会被网关路由到别处。
  var PXBASE = (function () {
    var b = document.baseURI || (location.origin + '/');
    return b.replace(/[^/]*$/, '');
  })();

  function pxOf(u, ref) {
    var q = 'u=' + encodeURIComponent(u || '');
    if (ref) q += '&r=' + encodeURIComponent(ref);
    return PXBASE + 'px?' + q;
  }

  function copyText(t, cb) {
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = t;
      ta.style.position = 'fixed';
      ta.style.top = '-1000px';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(ta);
      if (cb) cb(ok);
    }
    // http 局域网访问下 navigator.clipboard 不可用（非安全上下文），要留兜底
    if (navigator.clipboard && navigator.clipboard.writeText && window.isSecureContext) {
      navigator.clipboard.writeText(t).then(function () { if (cb) cb(true); }, fallback);
      return;
    }
    fallback();
  }

  function flashDlMsg(text, kind) {
    var el = $('cmsDlMsg');
    if (!el) return;
    el.textContent = text;
    el.className = 'dl-flash ' + (kind === 'warn' ? 'is-warn' : 'is-ok');
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.classList.add('hidden'); }, 6000);
  }

  function plStop() {
    if (PL.hls) { try { PL.hls.destroy(); } catch (e) {} PL.hls = null; }
    var v = $('plVideo');
    try { v.pause(); } catch (e) {}
    v.removeAttribute('src');
    try { v.load(); } catch (e) {}
  }

  function plShow(show) {
    $('playerMask').classList.toggle('hidden', !show);
    if (!show) plStop();
  }

  function plSetMsg(t) { $('plMsg').textContent = t || ''; }

  function plPlayUrl(url, referer) {
    var v = $('plVideo');
    plStop();
    $('plUrl').value = url || '';
    $('plOpen').href = pxOf(url, referer);
    if (!url) { plSetMsg('没有可播放地址'); return; }
    var isHls = /\.m3u8(\?|$)/i.test(url);
    var src = pxOf(url, referer);
    if (isHls && window.Hls && window.Hls.isSupported()) {
      PL.hls = new window.Hls({ maxBufferLength: 30, manifestLoadingTimeOut: 15000 });
      PL.hls.on(window.Hls.Events.ERROR, function (ev, data) {
        if (!data || !data.fatal) return;
        // 资源站的签名地址通常几分钟就过期，缓存命中后播不出来时重解析一次
        PL.tries = (PL.tries || 0) + 1;
        if (PL.tries <= 1) {
          plSetMsg('播放中断，正在重新解析地址…');
          plGo(PL.i, true);
          return;
        }
        plSetMsg('播放失败：' + (data.details || data.type) + '，可点「新窗口」交给系统播放器');
      });
      PL.hls.loadSource(src);
      PL.hls.attachMedia(v);
    } else {
      // mp4 直链，或 Safari 原生支持 HLS
      v.src = src;
    }
    var p = v.play();
    if (p && p.catch) p.catch(function () {});
  }

  function plNav() {
    var i = PL.i, cur = PL.list[i];
    $('plPrev').disabled = i <= 0;
    $('plNext').disabled = i < 0 || i >= PL.list.length - 1;
    $('plEps').textContent = PL.list.length
      ? ((i + 1) + ' / ' + PL.list.length + '　' + (cur ? cur.title : '')) : '';
    $('plTitle').textContent = PL.title || '视频预览';
  }

  function plGo(i, force) {
    if (i < 0 || i >= PL.list.length) return;
    PL.i = i;
    if (!force) PL.tries = 0;
    plNav();
    var ep = PL.list[i];
    var seq = (PL.seq = (PL.seq || 0) + 1);   // 快速连点上一集/下一集时，只认最后一次
    $('plUrl').value = ep.url || '';
    $('plLoading').classList.remove('hidden');
    plSetMsg('');
    post('/cms/resolve', { api: PL.api, id: PL.id, from: PL.from, index: ep.index, force: !!force })
      .then(function (d) {
        if (seq !== PL.seq) return;
        $('plLoading').classList.add('hidden');
        if (d.error) { plSetMsg('解析失败：' + d.error); return; }
        plPlayUrl(d.url, d.referer);
      })
      .catch(function (e) {
        if (seq !== PL.seq) return;
        $('plLoading').classList.add('hidden');
        plSetMsg('解析失败：' + e);
      });
  }

  function openPlayer(ctx, index) {
    plShow(true);
    plSetMsg('');
    $('plLoading').classList.remove('hidden');
    $('plTitle').textContent = ctx.title || '视频预览';
    $('plEps').textContent = '';
    $('plUrl').value = '';
    PL.api = '';   // 每次打开都重新拉分集列表，避免源换了集数对不上
    PL.list = [];
    post('/cms/links', { api: ctx.api, id: ctx.id, from: ctx.from }).then(function (d) {
      if (d.error) throw new Error(d.error);
      PL.api = ctx.api; PL.id = ctx.id; PL.from = d.from || ctx.from;
      PL.title = ctx.title || d.title || '视频预览';
      PL.list = d.episodes || [];
      var k = -1;
      for (var j = 0; j < PL.list.length; j++) {
        if (PL.list[j].index === index) { k = j; break; }
      }
      if (k < 0) { $('plLoading').classList.add('hidden'); plSetMsg('没有找到该分集'); return; }
      plGo(k);
    }).catch(function (e) {
      $('plLoading').classList.add('hidden');
      plSetMsg('读取分集失败：' + e);
    });
  }

  $('plClose').onclick = function () { plShow(false); };
  $('playerMask').onclick = function (e) { if (e.target === this) plShow(false); };
  $('plPrev').onclick = function () { plGo(PL.i - 1); };
  $('plNext').onclick = function () { plGo(PL.i + 1); };
  $('plCopy').onclick = function () {
    var b = this;
    copyText($('plUrl').value, function (ok) {
      b.textContent = ok ? '已复制' : '复制失败';
      setTimeout(function () { b.textContent = '复制链接'; }, 1400);
    });
  };
  document.addEventListener('keydown', function (e) {
    if ($('playerMask').classList.contains('hidden')) return;
    if (e.key === 'Escape') plShow(false);
    else if (e.key === 'ArrowLeft') plGo(PL.i - 1);
    else if (e.key === 'ArrowRight') plGo(PL.i + 1);
  });

  function cmsDetail(apiUrl, id, title, btn, restoreText) {
    var box = $('cmsDetail');
    box.classList.remove('hidden');
    box.innerHTML = '<div class="empty">正在读取分集...</div>';
    if (box.scrollIntoView) box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    function restore() { if (btn) { btn.disabled = false; btn.textContent = restoreText || '选集'; } }
    post('/cms/detail', { api: apiUrl, id: id }).then(function (d) {
      restore();
      if (d.error || !d.sources || !d.sources.length) {
        box.innerHTML = '<div class="empty">读取失败：' + esc(d.error || '没有可用播放源') + '</div>';
        return;
      }
      CMSSOURCE = d.sources;
      function fromOf() {
        return CMSSOURCE[parseInt($('cmsSrc').value || '0', 10)].from;
      }
      function epRowHtml(e) {
        return '<div class="ep-row">' +
          '<label><input type="checkbox" value="' + e.index + '"><span>' + esc(e.title) + '</span></label>' +
          '<button class="ep-play" data-i="' + e.index + '" title="预览播放">▶</button></div>';
      }
      $('cmsDetail').innerHTML = '<h3>' + esc(title || d.title) + '</h3><div class="srcbar">' +
        '<label style="margin:0">播放源</label><select id="cmsSrc">' +
        d.sources.map(function (s, i) {
          return '<option value="' + i + '">' + esc(s.from) + '（' + s.episodes.length + '集）</option>';
        }).join('') + '</select>' +
        '<button id="cmsAll" class="ghost small">全选</button>' +
        '<button id="cmsNone" class="ghost small">取消全选</button>' +
        '<button id="cmsLinksBtn" class="ghost small">显示链接</button>' +
        '<button id="cmsDl" class="small">下载选中</button>' +
        '<span id="cmsDlMsg" class="dl-flash hidden"></span></div>' +
        '<div class="cms-ep" id="cmsEps">' + d.sources[0].episodes.map(epRowHtml).join('') + '</div>' +
        '<div id="cmsLinks" class="link-panel hidden"></div>';
      function bindEps() {
        Array.prototype.forEach.call($('cmsEps').querySelectorAll('.ep-play'), function (b) {
          b.onclick = function () {
            openPlayer({ api: apiUrl, id: id, title: title || d.title, from: fromOf() },
                       parseInt(b.getAttribute('data-i'), 10));
          };
        });
      }
      bindEps();
      function renderEps() {
        var i = parseInt($('cmsSrc').value || '0', 10);
        $('cmsEps').innerHTML = CMSSOURCE[i].episodes.map(epRowHtml).join('');
        $('cmsLinks').classList.add('hidden');
        $('cmsLinks').innerHTML = '';
        $('cmsLinksBtn').textContent = '显示链接';
        bindEps();
      }
      $('cmsSrc').onchange = renderEps;
      $('cmsAll').onclick = function () {
        Array.prototype.forEach.call($('cmsEps').querySelectorAll('input'), function (c) { c.checked = true; });
      };
      $('cmsNone').onclick = function () {
        Array.prototype.forEach.call($('cmsEps').querySelectorAll('input'), function (c) { c.checked = false; });
      };
      // 显示视频链接：把当前播放源每集的地址列出来，可复制、可播放
      $('cmsLinksBtn').onclick = function () {
        var panel = $('cmsLinks'), self = this;
        if (!panel.classList.contains('hidden')) {
          panel.classList.add('hidden'); self.textContent = '显示链接'; return;
        }
        panel.classList.remove('hidden');
        self.textContent = '隐藏链接';
        panel.innerHTML = '<div class="empty">正在读取链接…</div>';
        post('/cms/links', { api: apiUrl, id: id, from: fromOf() }).then(function (r) {
          if (r.error) { panel.innerHTML = '<div class="empty">' + esc(r.error) + '</div>'; return; }
          panel.innerHTML = (r.episodes || []).map(function (e) {
            return '<div class="link-row"><span class="lk-name">' + esc(e.title) + '</span>' +
              '<input type="text" readonly spellcheck="false" value="' + esc(e.url) + '">' +
              '<button class="ghost small lk-copy" data-u="' + esc(e.url) + '">复制</button>' +
              '<button class="ghost small lk-play" data-i="' + e.index + '">播放</button></div>';
          }).join('') || '<div class="empty">没有分集</div>';
          Array.prototype.forEach.call(panel.querySelectorAll('.lk-copy'), function (b) {
            b.onclick = function () {
              copyText(b.getAttribute('data-u'), function (ok) {
                b.textContent = ok ? '已复制' : '复制失败';
                setTimeout(function () { b.textContent = '复制'; }, 1400);
              });
            };
          });
          Array.prototype.forEach.call(panel.querySelectorAll('.lk-play'), function (b) {
            b.onclick = function () {
              openPlayer({ api: apiUrl, id: id, title: title || d.title, from: fromOf() },
                         parseInt(b.getAttribute('data-i'), 10));
            };
          });
        }).catch(function (e) {
          panel.innerHTML = '<div class="empty">读取失败：' + esc(e) + '</div>';
        });
      };
      $('cmsDl').onclick = function () {
        var idx = [];
        Array.prototype.forEach.call($('cmsEps').querySelectorAll('input:checked'), function (c) {
          idx.push(parseInt(c.value, 10));
        });
        if (!idx.length) { alert('请先勾选分集'); return; }
        var i = parseInt($('cmsSrc').value || '0', 10);
        var btn = this; btn.disabled = true;
        post('/cms/download', {
          api: apiUrl, id: id, title: title || d.title,
          play_from: CMSSOURCE[i].from, episodes: idx,
          skip_existing: idx.length === CMSSOURCE[i].episodes.length
        }).then(function (r) {
          btn.disabled = false;
          if (r.error) { alert(r.error); return; }
          // 就在按钮旁边给出明确反馈，不用去任务列表里找
          if (r.created) {
            flashDlMsg('✓ 已加入下载列表 · ' + idx.length + ' 集', 'ok');
            btn.classList.add('is-ok');
            setTimeout(function () { btn.classList.remove('is-ok'); }, 1200);
          } else {
            flashDlMsg('已在下载列表里，未重复创建', 'warn');
          }
          $('cmsStatus').textContent = r.created ? '已创建下载任务' : '该视频已下载过';
          refresh();
        }).catch(function (e) { btn.disabled = false; alert('创建失败：' + e); });
      };
    }).catch(function (e) {
      restore();
      box.innerHTML = '<div class="empty">读取失败：' + esc(e) + '</div>';
    });
  }

  $('cmsSearchBtn').onclick = function () { cmsQuery(1); };
  $('cmsKw').onkeydown = function (e) { if (e.key === 'Enter') cmsQuery(1); };
  $('cmsType').onchange = function () { cmsQuery(1); };
  $('cmsSite').onchange = function () { $('cmsDetail').classList.add('hidden'); cmsQuery(1); };
  $('cmsPrev').onclick = function () { if (CMSPAGE > 1) cmsQuery(CMSPAGE - 1); };
  $('cmsNext').onclick = function () { cmsQuery(CMSPAGE + 1); };

  // ---------- 搜全部源：跨源搜索并汇总 ----------
  var ALLMODE = false, ALLDATA = null;

  function allSourcesOf(d) {
    var set = {};
    (d.items || []).forEach(function (it) {
      (it.sources || []).forEach(function (s) { set[s.site] = 1; });
    });
    return Object.keys(set);
  }

  function renderAll(d) {
    var filt = $('allFilter').value;
    var items = (d.items || []).filter(function (it) {
      if (!filt) return true;
      return (it.sources || []).some(function (s) { return s.site === filt; });
    });
    $('cmsList').innerHTML = items.map(function (it) {
      var chips = (it.sources || []).map(function (s) {
        return '<button class="src-chip' + (s.adult ? ' adult' : '') + '"' +
          ' data-api="' + esc(s.api) + '" data-id="' + esc(s.id) + '" data-title="' + esc(it.title) + '">' +
          esc(s.site) + (s.remarks ? ' · ' + esc(s.remarks) : '') + '</button>';
      }).join('');
      return '<div class="scan-item all-item">' +
        '<div class="scan-main"><div class="scan-title">' + esc(it.title) +
        ' <span class="src-count">' + (it.sources || []).length + ' 个源</span></div>' +
        '<div class="scan-meta">' + (it.type ? esc(it.type) + ' · ' : '') +
        (it.remarks ? esc(it.remarks) + ' · ' : '') + (it.time ? esc(it.time) : '') + '</div>' +
        '<div class="src-chips">' + chips + '</div></div></div>';
    }).join('') || '<div class="empty">' + (d.running ? '正在搜索…' : '没有找到匹配的片名') + '</div>';
    Array.prototype.forEach.call(document.querySelectorAll('#cmsList .src-chip'), function (b) {
      b.onclick = function () {
        var old = b.textContent;
        b.disabled = true;
        cmsDetail(b.getAttribute('data-api'), b.getAttribute('data-id'),
                  b.getAttribute('data-title'), b, old);
      };
    });
  }

  function updateAllFilter(d) {
    var names = allSourcesOf(d);
    var cur = $('allFilter').value;
    if ($('allFilter').getAttribute('data-n') === String(names.length) &&
        $('allFilter').getAttribute('data-k') === names.join(',')) return;
    $('allFilter').setAttribute('data-n', names.length);
    $('allFilter').setAttribute('data-k', names.join(','));
    $('allFilter').innerHTML = '<option value="">全部来源（' + names.length + '）</option>' +
      names.map(function (n) { return '<option value="' + esc(n) + '">' + esc(n) + '</option>'; }).join('');
    $('allFilter').value = names.indexOf(cur) >= 0 ? cur : '';
  }

  function pollAll() {
    api('/cms/search-all').then(function (d) {
      ALLDATA = d;
      updateAllFilter(d);
      renderAll(d);
      if (d.running) {
        $('allStat').textContent = '搜索中 ' + d.done + '/' + d.total + ' 个源，已找到 ' +
          (d.items || []).length + ' 部…';
        setTimeout(pollAll, 900);
      } else {
        var bad = Object.keys(d.errors || {}).length;
        var secs = d.started ? Math.max(1, Math.round((d.finished_at || d.started) - d.started)) : 0;
        $('allStat').textContent = '「' + (d.keyword || '') + '」共 ' + (d.items || []).length +
          ' 部 · 来自 ' + allSourcesOf(d).length + ' 个源 · 用时 ' + secs + 's' +
          (bad ? ' · ' + bad + ' 个源查询失败' : '');
        $('cmsAllBtn').disabled = false;
        $('cmsAllBtn').textContent = '搜全部源';
      }
    }).catch(function () { setTimeout(pollAll, 1200); });
  }

  $('cmsAllBtn').onclick = function () {
    var kw = $('cmsKw').value.trim();
    if (!kw) { $('cmsStatus').textContent = '请先输入片名'; return; }
    var btn = this;
    btn.disabled = true; btn.textContent = '搜索中…';
    ALLMODE = true;
    $('cmsDetail').classList.add('hidden');
    $('cmsPager').classList.add('hidden');
    $('allBar').classList.remove('hidden');
    $('allStat').textContent = '正在向所有可用源发起搜索…';
    $('cmsList').innerHTML = '<div class="empty">正在搜索…</div>';
    $('cmsStatus').textContent = '';
    post('/cms/search-all', { keyword: kw, per_site: 20 }).then(function (d) {
      if (d.error && !d.running) {
        btn.disabled = false; btn.textContent = '搜全部源';
        $('allStat').textContent = d.error;
        return;
      }
      pollAll();
    }).catch(function (e) {
      btn.disabled = false; btn.textContent = '搜全部源';
      $('allStat').textContent = '搜索失败：' + e;
    });
  };

  $('allFilter').onchange = function () { if (ALLDATA) renderAll(ALLDATA); };
  $('allExit').onclick = function () {
    ALLMODE = false;
    $('allBar').classList.add('hidden');
    $('cmsPager').classList.remove('hidden');
    cmsQuery(1);
  };
  loadCmsSites();

  // ---------- 设置：成人开关 / 并发线程 ----------
  api('/config').then(function (c) {
    if (c.download_root) $('downloadRoot').value = c.download_root;
    $('adultEnabled').checked = !!c.adult_enabled;
    if (c.workers) $('workers').value = c.workers;
    if (c.seg_workers) $('segWorkers').value = c.seg_workers;
  });

  // ---------- 下载目录：浏览 / 授权 ----------
  var PATHS = [];
  var EXTRA_PATHS = [];   // 本次会话里手动检测/新建过的目录

  function pathBadge(p) {
    if (!p.exists) return '<span class="pstat p-off">不存在</span>';
    if (p.writable) return '<span class="pstat p-ok">可写</span>';
    if (p.readable) return '<span class="pstat p-ro">只读</span>';
    return '<span class="pstat p-off">无权限</span>';
  }

  function renderPaths(d) {
    // 用户刚新建/检测过的目录不在后端候选列表里（后端只列存储卷和回退目录），
    // 把它并到最前面，否则点完「创建并授权」列表里看不到这个新目录。
    var list = d.paths || [];
    EXTRA_PATHS.forEach(function (x) {
      if (!list.some(function (p) { return p.path === x.path; })) list.unshift(x);
    });
    PATHS = list;
    var cur = d.current || '';
    $('pathList').innerHTML = PATHS.map(function (p) {
      return '<div class="path-row' + (p.path === cur ? ' is-cur' : '') + '">' +
        '<span class="p-path" title="' + esc(p.error || '') + '">' + esc(p.path) + '</span>' +
        pathBadge(p) +
        (p.mode ? '<span class="p-mode">' + esc(p.mode) + '</span>' : '') +
        '<button class="ghost small pick" data-p="' + esc(p.path) + '"' +
        (p.writable ? '' : ' title="' + esc(p.error || '') + '"') + '>' +
        (p.writable ? '选用' : '查看') + '</button></div>';
    }).join('') || '<div class="empty">没有可列出的目录</div>';
    Array.prototype.forEach.call(document.querySelectorAll('#pathList .pick'), function (b) {
      b.onclick = function () {
        var p = b.getAttribute('data-p');
        $('downloadRoot').value = p;
        $('cfgMsg').textContent = '已选择 ' + p + '，点「保存设置」生效';
      };
    });
  }

  function loadPaths() {
    $('pathList').innerHTML = '<div class="empty">正在检测目录权限…</div>';
    api('/paths').then(function (d) {
      if (d.error) { $('pathList').innerHTML = '<div class="empty">' + esc(d.error) + '</div>'; return; }
      renderPaths(d);
    }).catch(function (e) {
      $('pathList').innerHTML = '<div class="empty">检测失败：' + esc(e) + '</div>';
    });
  }

  $('pickPathBtn').onclick = function () {
    var panel = $('pathPanel');
    panel.classList.toggle('hidden');
    if (!panel.classList.contains('hidden')) loadPaths();
  };

  $('mkPathBtn').onclick = function () {
    var p = $('newPath').value.trim();
    if (!p) { $('cfgMsg').textContent = '请先填写要新建的目录'; return; }
    var btn = this; btn.disabled = true;
    post('/paths/probe', { path: p, create: true }).then(function (d) {
      btn.disabled = false;
      if (d.error && !d.ok) { $('cfgMsg').textContent = d.error; return; }
      $('cfgMsg').textContent = d.ok
        ? ('已创建并确认可写：' + d.path)
        : ('目录已创建，但应用仍无权写入，请到飞牛应用设置里授权该文件夹');
      $('downloadRoot').value = d.path;
      $('pathPanel').classList.remove('hidden');
      EXTRA_PATHS = EXTRA_PATHS.filter(function (x) { return x.path !== d.path; });
      EXTRA_PATHS.unshift({ path: d.path, exists: !!d.exists, readable: !!d.readable,
                            writable: !!d.writable, error: d.error || '',
                            mode: d.mode || '', parent_exists: !!d.parent_exists });
      loadPaths();
    }).catch(function (e) { btn.disabled = false; $('cfgMsg').textContent = '创建失败：' + e; });
  };

  $('saveCfg').onclick = function () {
    var w = parseInt($('workers').value || '2', 10);
    if (isNaN(w) || w < 1) w = 1;
    if (w > 8) w = 8;
    $('workers').value = w;
    var sw = parseInt($('segWorkers').value || '6', 10);
    if (isNaN(sw) || sw < 1) sw = 1;
    if (sw > 16) sw = 16;
    $('segWorkers').value = sw;
    post('/config', {
      download_root: $('downloadRoot').value.trim(),
      adult_enabled: $('adultEnabled').checked,
      workers: w,
      seg_workers: sw
    }).then(function (d) {
      $('cfgMsg').textContent = d.ok
        ? ('已保存（' + w + ' 个任务并发 · 每个任务 ' + sw + ' 路分片）') : '保存失败';
      ADULT_ON = $('adultEnabled').checked;
      var current = cmsApi();
      renderSiteOptions();
      // 若当前选中的是成人源而开关被关闭，切回第一个普通源
      var stillThere = !!document.querySelector('#cmsSite option[value="' + current.replace(/"/g, '\\"') + '"]');
      if (!stillThere) cmsQuery(1);
      setTimeout(function () { $('cfgMsg').textContent = ''; }, 2500);
    });
  };

  // ---------- 源导入导出 ----------
  $('exportSites').onclick = function () {
    var blob = new Blob([JSON.stringify(CMSSITES, null, 2)], { type: 'application/json;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'dramanest_sites.json';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    $('importMsg').textContent = '已导出 ' + CMSSITES.length + ' 个源';
  };

  function doImport(payload) {
    if (!Array.isArray(payload)) { $('importMsg').textContent = '格式错误：必须是数组'; return; }
    var btn = $('importTextBtn'); btn.disabled = true;
    post('/cms/sites/import', { sites: payload, mode: $('importMode').value }).then(function (d) {
      btn.disabled = false;
      if (d.error) { $('importMsg').textContent = d.error; return; }
      $('importMsg').textContent = '导入成功，当前共 ' + d.count + ' 个源';
      loadCmsSites();
    }).catch(function (e) { btn.disabled = false; $('importMsg').textContent = '导入失败：' + e; });
  }

  $('importTextBtn').onclick = function () {
    var raw = $('importJson').value.trim();
    if (!raw) { $('importMsg').textContent = '请先粘贴 JSON'; return; }
    try { doImport(JSON.parse(raw)); }
    catch (e) { $('importMsg').textContent = 'JSON 格式错误：' + e.message; }
  };

  $('importFileBtn').onclick = function () { $('importFile').click(); };
  $('importFile').onchange = function () {
    var f = this.files && this.files[0];
    if (!f) return;
    var reader = new FileReader();
    reader.onload = function () {
      try { $('importJson').value = String(reader.result); doImport(JSON.parse(String(reader.result))); }
      catch (e) { $('importMsg').textContent = '文件解析失败：' + e.message; }
    };
    reader.readAsText(f, 'utf-8');
    this.value = '';
  };

  api('/version').then(function (v) {
    $('ver').textContent = 'v' + v.version;
    if (v.project_url && $('disclaimerRepo')) {
      $('disclaimerRepo').href = v.project_url;
      $('disclaimerRepo').textContent = v.project_url;
    }
  });

  // ---------- 首次打开免责声明 ----------
  (function () {
    var KEY = 'dramanest_disclaimer_agreed_v1';
    var mask = $('disclaimerMask'), chk = $('agreeCheck'), btn = $('agreeBtn');
    if (!mask || !chk || !btn) return;
    var agreed = false;
    try { agreed = localStorage.getItem(KEY) === '1'; } catch (e) {}
    if (agreed) { mask.classList.add('hidden'); return; }
    mask.classList.remove('hidden');

    // 按钮始终可点，仅用样式提示“未勾选”；点击时再校验，避免 disabled 状态不同步导致卡死
    function syncBtn() { btn.classList.toggle('btn-pending', !chk.checked); }
    chk.addEventListener('change', syncBtn);
    chk.addEventListener('input', syncBtn);
    chk.addEventListener('click', function () { setTimeout(syncBtn, 0); });
    syncBtn();

    btn.onclick = function () {
      if (!chk.checked) {
        btn.classList.add('shake');
        setTimeout(function () { btn.classList.remove('shake'); }, 420);
        return;
      }
      try { localStorage.setItem(KEY, '1'); } catch (e) {}
      mask.classList.add('hidden');
    };
  })();

  refresh();
  setInterval(refresh, 2500);
})();
