/* InsightPilot 前端：原生 JS，无构建、无 CDN、断网可用。
 * 数据全部来自同源 /api/*；图表按服务端返回的 ChartSpec 用 SVG 手绘。 */
(function () {
  'use strict';

  var SPINE = ['intake', 'linker', 'sqlcraft', 'executor', 'analyst', 'critic', 'reporter'];
  var NODE_CN = {
    intake: '问题理解', linker: '语义检索', sqlcraft: 'SQL 生成', executor: '执行',
    analyst: '分析结论', critic: '自检', reporter: '作答',
    clarify: '澄清', failure: '失败终止', cache: '缓存命中'
  };
  var STATUS_CN = { ok: '通过', clarify: '需澄清', failed: '失败', rejected: '被拦截' };
  var META_KEYS = [
    ['kind', '意图'], ['rewritten', '改写'], ['tables', '表'], ['metrics', '指标'],
    ['token_est', 'token'], ['dropped', '裁掉'], ['source', '来源'], ['chars', 'SQL 字符'],
    ['rows', '行'], ['cols', '列'], ['truncated', '截断'], ['shape', '形状'],
    ['chart', '图表'], ['passed', '自检'], ['severity', '级别'], ['reasons', '原因'],
    ['headline', '结论'], ['had_feedback', '带反馈'], ['hit', '命中']
  ];
  var PIE_COLORS = ['#2563eb', '#4d7ff0', '#7b9df2', '#a4bcf5', '#c6d5f8', '#dde5fb', '#8aa4e8', '#6b8de0'];
  var TABLE_ROWS = 50;
  var CHART_W = 620;

  var state = {
    sessionId: 's_' + Math.random().toString(36).slice(2, 10),
    history: [],
    chain: [],
    busy: false
  };

  var el = {
    chat: document.getElementById('chat'),
    form: document.getElementById('composer'),
    input: document.getElementById('question'),
    send: document.getElementById('send'),
    chain: document.getElementById('chain'),
    traceMeta: document.getElementById('trace-meta'),
    traceNote: document.getElementById('trace-note'),
    mode: document.getElementById('chip-mode'),
    cache: document.getElementById('chip-cache'),
    turns: document.getElementById('chip-turns'),
    health: document.getElementById('chip-health'),
    samples: document.getElementById('samples')
  };

  // ------------------------------------------------------------------
  // 工具
  // ------------------------------------------------------------------
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var NUM = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 });

  function fmtNum(value) {
    var n = Number(value);
    return isFinite(n) ? NUM.format(n) : String(value);
  }

  function fmtValue(value) {
    if (value === null || value === undefined) return '—';
    if (typeof value === 'number') return fmtNum(value);
    return esc(value);
  }

  function clip(text, max) {
    var s = String(text === null || text === undefined ? '' : text);
    return s.length > max ? s.slice(0, max) + '…' : s;
  }

  function elNode(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function scrollDown() {
    el.chat.scrollTop = el.chat.scrollHeight;
  }

  // ------------------------------------------------------------------
  // 顶部徽标
  // ------------------------------------------------------------------
  function loadHealth() {
    return fetch('/api/health').then(function (r) { return r.json(); }).then(function (h) {
      if (h.mode === 'api') {
        el.mode.textContent = '模型模式 · ' + (h.model || 'api');
        el.mode.classList.add('is-api');
      } else {
        el.mode.textContent = '离线规则模式';
        el.mode.classList.remove('is-api');
      }
      el.mode.title = '模式 ' + h.mode + ' · 推理器 ' + h.inferencer + ' · 变体 ' + h.variant;
      el.health.textContent = '表 ' + h.tables + ' · 指标 ' + h.metrics;
      el.health.title = '仓库 ' + h.db_path + (h.db_exists ? '（存在）' : '（缺失）') +
        '\n语义层 ' + h.semantic_dir;
    }).catch(function () {
      el.mode.textContent = '引擎未连接';
    });
  }

  function loadStats() {
    return fetch('/api/stats').then(function (r) { return r.json(); }).then(function (s) {
      var rate = Math.round((s.cache && s.cache.hit_rate ? s.cache.hit_rate : 0) * 100);
      el.cache.textContent = '缓存命中 ' + rate + '%';
      el.cache.title = 'lookups 命中率 ' + rate + '% · 走缓存的轮次 ' +
        (s.cache ? s.cache.cached_turns : 0) + '/' + (s.cache ? s.cache.turns : 0);
      el.turns.textContent = '轮次 ' + s.turns + ' · 成功率 ' + Math.round((s.success_rate || 0) * 100) + '%';
      el.turns.title = '平均 ' + s.avg_elapsed_ms + ' ms · 平均 ' + s.avg_steps + ' 步 · ' +
        s.spans + ' 条 span';
    }).catch(function () { /* 顶栏是辅助信息，失败不打扰主流程 */ });
  }

  // ------------------------------------------------------------------
  // 链路面板
  // ------------------------------------------------------------------
  function statusClass(status) {
    if (status === 'error' || status === 'rejected') return 'is-bad';
    if (status === 'fail' || status === 'retry' || status === 'warn' || status === 'clarify') return 'is-warn';
    if (status === 'pending' || status === 'start' || status === 'run') return 'is-pending';
    return 'is-ok';
  }

  var BARE_META = { reasons: 1, headline: 1, rewritten: 1 };

  function metaText(meta) {
    if (!meta) return '';
    var parts = [];
    META_KEYS.forEach(function (pair) {
      var value = meta[pair[0]];
      if (value === undefined || value === null || value === '' || value === false) return;
      if (Array.isArray(value)) {
        if (!value.length) return;
        value = value.slice(0, 3).join('/') + (value.length > 3 ? '…' : '');
      } else if (typeof value === 'object') {
        return;
      } else if (typeof value === 'number') {
        value = fmtNum(value);
      } else if (pair[0] === 'passed') {
        value = value === true ? '通过' : '未通过';
      }
      parts.push(BARE_META[pair[0]] ? value : pair[1] + ' ' + value);
      if (parts.length >= 4) return;
    });
    return parts.join(' · ');
  }

  function renderChain(report) {
    var entries = state.chain.slice();
    if (!entries.length && report && report.path && report.path.length) {
      // 退化路径：没有 SSE 节点事件时，用 report.path 画出骨架（无耗时）
      entries = report.path.map(function (node, i) {
        var failed = report.status === 'error' && i === report.path.length - 1;
        return { node: node, status: failed ? 'error' : 'ok', ms: null };
      });
    }
    if (!entries.length) {
      el.chain.innerHTML = '<li class="chain-empty">提问后这里按真实执行顺序显示节点、状态与耗时；失败节点标红。</li>';
      return;
    }
    el.chain.innerHTML = entries.map(function (item) {
      var cls = statusClass(item.status);
      var cn = NODE_CN[item.node] || '';
      var ms = (item.ms === null || item.ms === undefined) ? '' : '<span class="node-ms">' + item.ms + ' ms</span>';
      var meta = metaText(item.meta);
      var err = item.error ? '<span class="node-error">' + esc(clip(item.error, 160)) + '</span>' : '';
      var sub = (meta || err) ? '<span class="node-sub">' + (meta ? esc(meta) : '') + err + '</span>' : '';
      return '<li class="node ' + cls + '">' +
        '<span class="node-dot"></span>' +
        '<span class="node-body">' +
        '<span class="node-line"><span class="node-name">' + esc(item.node) + '</span>' +
        '<span class="node-cn">' + esc(cn) + '</span>' + ms + '</span>' + sub +
        '</span></li>';
    }).join('');
    el.chain.scrollTop = el.chain.scrollHeight;
  }

  function resetChain() {
    state.chain = [];
    renderChain(null);
    el.traceMeta.textContent = '执行中';
    el.traceNote.textContent = '节点事件来自引擎 span 落库时同步推送';
  }

  function finishChain(report) {
    if (report) {
      el.traceMeta.textContent = report.steps + ' 步 · ' + report.elapsed_ms + ' ms · ' +
        (STATUS_CN[report.status] || report.status);
      var edges = report.edge_taken && report.edge_taken.length
        ? report.edge_taken.join(' → ')
        : '无分支';
      el.traceNote.textContent = '分支 ' + edges + (report.error ? ' · ' + report.error : '');
    }
    renderChain(report);
  }

  // ------------------------------------------------------------------
  // 图表：ChartSpec -> SVG 字符串
  // ------------------------------------------------------------------
  function svgBar(spec) {
    var points = spec.points.slice(0, 14);
    var labelW = 104, valueW = 66, rowH = 26, top = 14, bottom = 6;
    var h = top + points.length * rowH + bottom;
    var plotW = CHART_W - labelW - valueW - 14;
    var max = 1;
    points.forEach(function (p) { max = Math.max(max, Math.abs(Number(p.value) || 0)); });
    var rows = points.map(function (p, i) {
      var value = Number(p.value) || 0;
      var width = Math.max(2, Math.round(Math.abs(value) / max * plotW));
      var y = top + i * rowH;
      return '<g>' +
        '<text class="cx-label" x="' + (labelW - 10) + '" y="' + (y + 15) + '" text-anchor="end">' +
        esc(clip(String(p.label), 9)) + '</text>' +
        '<rect class="cx-bar" x="' + labelW + '" y="' + (y + 4) + '" width="' + width +
        '" height="' + (rowH - 12) + '" rx="2"></rect>' +
        '<text class="cx-value" x="' + (labelW + width + 8) + '" y="' + (y + 15) + '">' +
        esc(fmtNum(value)) + '</text></g>';
    }).join('');
    return '<svg class="svg-chart" viewBox="0 0 ' + CHART_W + ' ' + h +
      '" preserveAspectRatio="xMinYMin meet" role="img" aria-label="条形图">' +
      '<line class="cx-axis" x1="' + labelW + '" y1="' + (top - 4) + '" x2="' + labelW +
      '" y2="' + (h - bottom) + '"></line>' + rows + '</svg>';
  }

  function svgLine(spec) {
    var points = spec.points.slice(0, 80);
    var w = CHART_W, h = 232;
    var m = { l: 54, r: 18, t: 18, b: 36 };
    var plotW = w - m.l - m.r, plotH = h - m.t - m.b;
    var values = points.map(function (p) { return Number(p.value) || 0; });
    var max = Math.max(1, Math.max.apply(null, values));
    var n = points.length;
    function x(i) { return m.l + (n === 1 ? plotW / 2 : i * plotW / (n - 1)); }
    function y(v) { return m.t + plotH * (1 - v / max); }
    var poly = points.map(function (p, i) { return x(i).toFixed(1) + ',' + y(values[i]).toFixed(1); }).join(' ');
    var dots = points.map(function (p, i) {
      return '<circle class="cx-dot" cx="' + x(i).toFixed(1) + '" cy="' + y(values[i]).toFixed(1) + '" r="2.6"></circle>';
    }).join('');
    var grid = [0, 0.5, 1].map(function (t) {
      var v = max * t, yy = y(v);
      return '<line class="cx-grid" x1="' + m.l + '" y1="' + yy.toFixed(1) + '" x2="' + (w - m.r) +
        '" y2="' + yy.toFixed(1) + '"></line>' +
        '<text class="cx-tick" x="' + (m.l - 8) + '" y="' + (yy + 3.5).toFixed(1) +
        '" text-anchor="end">' + esc(fmtNum(v)) + '</text>';
    }).join('');
    // 按可用宽度决定标签密度，避免日期互相压字；首尾两个始终显示
    var step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 82))));
    var ticks = [];
    for (var i = 0; i < n; i += step) ticks.push(i);
    if (ticks[ticks.length - 1] !== n - 1) {
      if (ticks.length > 1 && n - 1 - ticks[ticks.length - 1] < step * 0.8) ticks.pop();
      ticks.push(n - 1);
    }
    var labels = ticks.map(function (i) {
      var anchor = i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle');
      return '<text class="cx-tick" x="' + x(i).toFixed(1) + '" y="' + (h - m.b + 18) +
        '" text-anchor="' + anchor + '">' + esc(clip(String(points[i].label), 10)) + '</text>';
    }).join('');
    return '<svg class="svg-chart" viewBox="0 0 ' + w + ' ' + h +
      '" preserveAspectRatio="xMinYMin meet" role="img" aria-label="折线图">' +
      grid + '<line class="cx-axis" x1="' + m.l + '" y1="' + m.t + '" x2="' + m.l +
      '" y2="' + (h - m.b) + '"></line>' +
      '<polyline class="cx-line" points="' + poly + '"></polyline>' + dots + labels + '</svg>';
  }

  function svgPie(spec) {
    var points = spec.points.slice(0, 8);
    var total = 0;
    points.forEach(function (p) { total += Math.abs(Number(p.value) || 0); });
    if (!total) total = 1;
    var cx = 110, cy = 112, r = 92;
    var angle = -Math.PI / 2;
    var arcs = points.map(function (p, i) {
      var value = Math.abs(Number(p.value) || 0);
      var span = value / total * Math.PI * 2;
      var d;
      if (span >= Math.PI * 2 - 0.0001) {
        d = 'M ' + cx + ' ' + cy + ' m -' + r + ' 0 a ' + r + ' ' + r + ' 0 1 0 ' + (2 * r) +
          ' 0 a ' + r + ' ' + r + ' 0 1 0 -' + (2 * r) + ' 0';
      } else {
        var x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
        var a2 = angle + span;
        var x2 = cx + r * Math.cos(a2), y2 = cy + r * Math.sin(a2);
        d = 'M ' + cx + ' ' + cy + ' L ' + x1.toFixed(2) + ' ' + y1.toFixed(2) +
          ' A ' + r + ' ' + r + ' 0 ' + (span > Math.PI ? 1 : 0) + ' 1 ' +
          x2.toFixed(2) + ' ' + y2.toFixed(2) + ' Z';
      }
      angle += span;
      return '<path d="' + d + '" fill="' + PIE_COLORS[i % PIE_COLORS.length] +
        '" stroke="#ffffff" stroke-width="1.5"></path>';
    }).join('');
    var legend = points.map(function (p, i) {
      var pct = Math.abs(Number(p.value) || 0) / total * 100;
      var ly = 32 + i * 22;
      return '<g><rect x="232" y="' + (ly - 9) + '" width="9" height="9" rx="2" fill="' +
        PIE_COLORS[i % PIE_COLORS.length] + '"></rect>' +
        '<text class="cx-label" x="250" y="' + ly + '">' + esc(clip(String(p.label), 14)) + '</text>' +
        '<text class="cx-value" x="512" y="' + ly +
        '" text-anchor="end">' + pct.toFixed(1) + '%</text></g>';
    }).join('');
    return '<svg class="svg-chart" viewBox="0 0 520 ' + Math.max(240, 40 + points.length * 22) +
      '" preserveAspectRatio="xMinYMin meet" role="img" aria-label="饼图">' + arcs + legend + '</svg>';
  }

  function chartBlock(spec) {
    if (!spec || spec.type === 'none' || !spec.points || !spec.points.length) return '';
    var body = '';
    if (spec.type === 'bar') body = svgBar(spec);
    else if (spec.type === 'line') body = svgLine(spec);
    else if (spec.type === 'pie') body = svgPie(spec);
    if (!body) return '';
    var caption = spec.title || '';
    if (spec.reason) caption += (caption ? ' · ' : '') + spec.reason;
    return '<figure class="chart">' + body +
      (caption ? '<figcaption class="chart-cap">' + esc(caption) + '</figcaption>' : '') + '</figure>';
  }

  // ------------------------------------------------------------------
  // 答案卡片
  // ------------------------------------------------------------------
  function tableBlock(answer) {
    var rows = answer.rows || [];
    if (!rows.length) {
      // 跑失败时没有表格可看，也不该说"没有数据"——那是另一回事
      if (answer.status === 'failed' || answer.status === 'rejected') return '';
      return '<p class="empty-note">该条件下没有数据</p>';
    }
    var columns = answer.columns || [];
    var shown = rows.slice(0, TABLE_ROWS);
    // 全是数字的列右对齐，读排名和金额时不用横向找小数点
    var numeric = columns.map(function (_, index) {
      var seen = false;
      return shown.every(function (row) {
        var cell = row[index];
        if (cell === null || cell === undefined) return true;
        if (typeof cell !== 'number') return false;
        seen = true;
        return true;
      }) && seen;
    });
    var head = columns.map(function (c, i) {
      return '<th' + (numeric[i] ? ' class="num"' : '') + '>' + esc(c) + '</th>';
    }).join('');
    var body = shown.map(function (row) {
      return '<tr>' + row.map(function (cell, i) {
        return '<td' + (numeric[i] ? ' class="num"' : '') + '>' + fmtValue(cell) + '</td>';
      }).join('') + '</tr>';
    }).join('');
    var more = rows.length > shown.length
      ? '<p class="table-note">仅显示前 ' + shown.length + ' 行，共 ' + rows.length + ' 行</p>'
      : '';
    return '<div class="table-wrap"><table><thead><tr>' + head + '</tr></thead><tbody>' +
      body + '</tbody></table></div>' + more;
  }

  function renderAnswer(answer, report) {
    var card = elNode('article', 'card');
    var insight = answer.insight || {};
    var status = answer.status || 'ok';
    var metaBits = [];
    metaBits.push((answer.elapsed_ms || 0) + ' ms');
    metaBits.push(answer.cached ? '缓存命中' : '实时计算');
    if (answer.mode) metaBits.push(answer.mode === 'api' ? '模型模式' : '离线规则模式');
    if (answer.trace_id) metaBits.push(answer.trace_id);

    var head = elNode('div', 'card-head');
    var badge = elNode('span', 'badge ' + (status === 'ok' ? 'is-ok' : 'is-warn'),
      STATUS_CN[status] || status);
    head.appendChild(badge);
    head.appendChild(elNode('span', 'card-meta mono', metaBits.join(' · ')));
    card.appendChild(head);

    card.insertAdjacentHTML('beforeend', '<h3 class="headline">' +
      esc(insight.headline || (status === 'ok' ? '没有生成结论' : '本次问数没有完成')) + '</h3>');

    if (status !== 'ok' && answer.message) {
      card.insertAdjacentHTML('beforeend', '<p class="notice">' + esc(answer.message) + '</p>');
    }
    if (answer.verdict && !answer.verdict.passed) {
      var reasons = (answer.verdict.reasons || []).join('；');
      var severity = answer.verdict.severity || 'info';
      if (reasons) {
        var sevCn = { info: '提示', warn: '警告', block: '拦截' }[severity] || '提示';
        card.insertAdjacentHTML('beforeend', '<p class="notice">自检未通过（' + esc(sevCn) +
          '）：' + esc(reasons) + '</p>');
      }
    }

    var bullets = insight.bullets || [];
    if (bullets.length) {
      card.insertAdjacentHTML('beforeend', '<ul class="bullets">' + bullets.map(function (b) {
        return '<li>' + esc(b) + '</li>';
      }).join('') + '</ul>');
    }

    card.insertAdjacentHTML('beforeend', chartBlock(insight.chart));

    var chartNote = '';
    if (insight.chart && insight.chart.type === 'none' && insight.chart.reason && (answer.rows || []).length) {
      chartNote = '<p class="chart-hint">图表：' + esc(insight.chart.reason) + '</p>';
    }
    if (chartNote) card.insertAdjacentHTML('beforeend', chartNote);

    card.insertAdjacentHTML('beforeend', tableBlock(answer));

    var notes = (insight.footnotes || []).slice();
    (answer.ambiguities || []).forEach(function (a) {
      notes.push('口径「' + a.term + '」按「' + a.chosen + '」理解' + (a.reason ? '：' + a.reason : ''));
    });
    if (notes.length) {
      card.insertAdjacentHTML('beforeend', '<ul class="notes">' + notes.map(function (n) {
        return '<li>' + esc(n) + '</li>';
      }).join('') + '</ul>');
    }

    if (answer.sql) {
      card.insertAdjacentHTML('beforeend',
        '<details class="sql"><summary>SQL' + (answer.rewritten && answer.rewritten !== answer.question
          ? '（改写：' + esc(clip(answer.rewritten, 40)) + '）' : '') +
        '</summary><pre class="mono">' + esc(answer.sql) + '</pre></details>');
    }

    if (report && report.path && report.path.length) {
      card.insertAdjacentHTML('beforeend', '<p class="card-foot mono">节点 ' +
        esc(report.path.join(' → ')) + '</p>');
    }
    return card;
  }

  function addUserBubble(text) {
    var wrap = elNode('div', 'msg msg-user');
    wrap.appendChild(elNode('p', 'bubble', text));
    el.chat.appendChild(wrap);
    scrollDown();
  }

  function addPending() {
    var wrap = elNode('div', 'msg msg-bot');
    var card = elNode('article', 'card card-pending');
    card.appendChild(elNode('p', 'pending-text', '正在执行：理解问题 → 检索语义层 → 生成 SQL → 执行 → 分析 → 自检'));
    wrap.appendChild(card);
    el.chat.appendChild(wrap);
    scrollDown();
    return wrap;
  }

  function addError(wrap, message) {
    wrap.innerHTML = '';
    var card = elNode('article', 'card');
    card.appendChild(elNode('span', 'badge is-warn', '请求失败'));
    card.appendChild(elNode('p', 'notice', message));
    wrap.appendChild(card);
    scrollDown();
  }

  // ------------------------------------------------------------------
  // SSE 客户端（fetch + 手写解析，EventSource 不支持 POST）
  // ------------------------------------------------------------------
  function parseEvent(raw) {
    var event = 'message';
    var dataLines = [];
    raw.split(/\r?\n/).forEach(function (line) {
      if (!line || line.charAt(0) === ':') return;
      var idx = line.indexOf(':');
      var field = idx === -1 ? line : line.slice(0, idx);
      var value = idx === -1 ? '' : line.slice(idx + 1).replace(/^ /, '');
      if (field === 'event') event = value;
      else if (field === 'data') dataLines.push(value);
    });
    if (!dataLines.length) return null;
    var payload;
    try {
      payload = JSON.parse(dataLines.join('\n'));
    } catch (err) {
      return null;
    }
    return { event: event, data: payload };
  }

  function askStream(question, handlers) {
    return fetch('/api/ask/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, session_id: state.sessionId, history: state.history.slice(-3) })
    }).then(function (res) {
      if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
      var reader = res.body.getReader();
      var decoder = new TextDecoder('utf-8');
      var buffer = '';
      var result = null;

      function drain(final) {
        var idx;
        while ((idx = buffer.search(/\r?\n\r?\n/)) !== -1) {
          var raw = buffer.slice(0, idx);
          buffer = buffer.slice(idx + (buffer.slice(idx, idx + 4) === '\r\n\r\n' ? 4 : 2));
          var ev = parseEvent(raw);
          if (!ev) continue;
          if (ev.event === 'node' && handlers.onNode) handlers.onNode(ev.data);
          else if (ev.event === 'done') result = ev.data;
          else if (ev.event === 'error') throw new Error(ev.data && ev.data.message ? ev.data.message : '流式执行失败');
        }
        if (final && buffer.trim()) {
          var tail = parseEvent(buffer);
          if (tail && tail.event === 'done') result = tail.data;
        }
      }

      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            drain(true);
            if (!result) throw new Error('流提前结束，没有收到 done 事件');
            return result;
          }
          buffer += decoder.decode(chunk.value, { stream: true });
          drain(false);
          return pump();
        });
      }
      return pump();
    });
  }

  function askPlain(question) {
    return fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, session_id: state.sessionId, history: state.history.slice(-3) })
    }).then(function (res) {
      return res.json().then(function (body) {
        if (!res.ok) {
          var detail = body && body.detail;
          if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg; }).join('；');
          throw new Error(detail || ('HTTP ' + res.status));
        }
        return body;
      });
    });
  }

  // ------------------------------------------------------------------
  // 主流程
  // ------------------------------------------------------------------
  function submit(question) {
    if (state.busy) return;
    state.busy = true;
    el.send.disabled = true;
    el.send.textContent = '执行中';
    var welcome = el.chat.querySelector('.welcome');
    if (welcome) welcome.remove();

    addUserBubble(question);
    var pending = addPending();
    resetChain();

    var streamed = true;
    askStream(question, {
      onNode: function (event) {
        state.chain.push({
          node: event.node,
          status: event.status,
          ms: event.ms,
          meta: event.meta,
          error: event.error
        });
        renderChain(null);
      }
    }).catch(function () {
      // 流式不可用（代理缓冲/浏览器限制）时回落到同步接口，链路改由 report.path 绘制
      streamed = false;
      state.chain = [];
      return askPlain(question).then(function (body) {
        return body;
      });
    }).then(function (body) {
      var answer = body.answer || {};
      var report = body.report || null;
      pending.innerHTML = '';
      pending.appendChild(renderAnswer(answer, report));
      finishChain(report);
      state.history.push({ question: answer.question || question, sql: answer.sql || '' });
      if (state.history.length > 8) state.history.shift();
      scrollDown();
      loadStats();
      if (!streamed) {
        el.traceNote.textContent = 'SSE 不可用，已回落到同步接口：链路取自 report.path（无耗时）';
      }
    }).catch(function (err) {
      addError(pending, err && err.message ? err.message : '请求失败');
      el.traceMeta.textContent = '失败';
      el.traceNote.textContent = err && err.message ? err.message : '';
    }).then(function () {
      state.busy = false;
      el.send.disabled = false;
      el.send.textContent = '提问';
      el.input.focus();
    });
  }

  el.form.addEventListener('submit', function (e) {
    e.preventDefault();
    var question = el.input.value.trim();
    if (!question) return;
    el.input.value = '';
    el.input.style.height = 'auto';
    submit(question);
  });

  el.input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      el.form.dispatchEvent(new Event('submit'));
    }
  });

  el.input.addEventListener('input', function () {
    el.input.style.height = 'auto';
    el.input.style.height = Math.min(120, el.input.scrollHeight) + 'px';
  });

  if (el.samples) {
    el.samples.addEventListener('click', function (e) {
      var btn = e.target.closest('.sample');
      if (!btn) return;
      el.input.value = btn.getAttribute('data-q') || '';
      el.form.dispatchEvent(new Event('submit'));
    });
  }

  loadHealth();
  loadStats();
  window.setInterval(loadStats, 20000);
  el.input.focus();
})();
