(function () {
  'use strict';

  var state = { current: null, refunds: [] };

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  function cents(yuanValue) { return Math.round((parseFloat(yuanValue) || 0) * 100); }
  function money(value) {
    var n = Number(value || 0);
    return '¥' + (n / 100).toFixed(2);
  }
  function moneyTitle(value) { return Number(value || 0) + ' 分'; }
  function setMessage(text, isError) {
    var node = $('notice');
    if (!node) return;
    node.textContent = text || '';
    node.style.color = isError ? 'var(--danger)' : '';
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || data.code || ('HTTP ' + response.status));
    return data;
  }

  var CHANNEL_OPTIONS = [
    ['gift_card', '礼品卡'], ['balance', '余额'], ['bank_card', '银行卡'],
    ['cash', '现金'], ['other', '其他渠道'], ['default', '默认/待补明细']
  ];

  function channelOptions(selected) {
    return CHANNEL_OPTIONS.map(function (item) {
      return '<option value="' + item[0] + '"' + (item[0] === selected ? ' selected' : '') + '>' + item[1] + '</option>';
    }).join('');
  }

  function paymentRow(values) {
    values = values || {};
    var amountYuan = values.amount_cents !== undefined ? (values.amount_cents / 100).toFixed(2) : (values.amount || '');
    var lineId = values.line_id || '';
    return '<div class="payment-row">' +
      '<select class="pay-channel" aria-label="渠道">' + channelOptions(values.channel || 'gift_card') + '</select>' +
      '<input class="pay-amount" type="number" min="0" step="0.01" placeholder="金额（元）" value="' + esc(amountYuan) + '" aria-label="付款金额">' +
      '<input class="pay-line" type="text" placeholder="仅支付行 line_id（可选）" value="' + esc(lineId) + '" aria-label="适用商品行">' +
      '<button type="button" class="remove-row secondary" title="移除">×</button>' +
      '</div>';
  }

  function addPaymentRow(values) {
    var wrap = $('payment-rows');
    var div = document.createElement('div');
    div.innerHTML = paymentRow(values);
    wrap.appendChild(div.firstElementChild);
  }

  function collectPayments() {
    return Array.prototype.map.call(document.querySelectorAll('.payment-row'), function (row) {
      var item = {
        channel: row.querySelector('.pay-channel').value,
        amount_cents: cents(row.querySelector('.pay-amount').value)
      };
      var lineId = row.querySelector('.pay-line').value.trim();
      if (lineId) item.line_id = lineId;
      return item;
    }).filter(function (item) { return item.amount_cents > 0; });
  }

  function orderId() { return $('refund-order').value.trim(); }

  async function savePayments(showSuccess) {
    var oid = orderId();
    if (!oid) throw new Error('请填写订单号');
    var payments = collectPayments();
    if (!payments.length) throw new Error('请至少填写一个有效付款渠道和金额');
    var result = await request('/api/orders/' + encodeURIComponent(oid) + '/payments', {
      method: 'POST', body: JSON.stringify({ payments: payments })
    });
    if (showSuccess) setMessage('付款构成已保存 rev.' + result.revision + (result.changed ? '，未签发拆分已标记失效' : '，内容无变化'));
    return result;
  }

  function statusLabel(status) {
    return { DRAFT: '草稿/待签发', STALE: '已失效待重算', ISSUED: '已签发' }[status] || status;
  }

  function channelChips(refund) {
    if (!refund || !refund.channels || !refund.channels.length) return '<span class="empty">无渠道明细</span>';
    return refund.channels.filter(function (c) { return c.requested_amount || c.amount || c.pending_amount; }).map(function (c) {
      var pending = c.pending_amount ? '<em class="pending-chip" title="超出该渠道可退额度，留待处理">待 ' + money(c.pending_amount) + '</em>' : '';
      return '<span class="channel-chip" data-channel="' + esc(c.channel) + '">' +
        '<b>' + esc(c.channel_label || c.channel) + '</b> ' +
        '<span title="' + moneyTitle(c.amount) + '">退 ' + money(c.amount) + '</span>' + pending +
        '</span>';
    }).join('');
  }

  function renderDetail(refund) {
    state.current = refund;
    var canIssue = refund && refund.status === 'DRAFT';
    $('issue-split').disabled = !canIssue;
    $('issue-split').textContent = !refund ? '签发当前拆分' :
      refund.status === 'ISSUED' ? '已签发，不可重复退款' :
      refund.status === 'STALE' ? '已失效，请先重算' : '签发当前拆分';
    if (!refund) {
      $('split-detail').innerHTML = '<p class="empty">选择或生成一笔退款后，这里显示每个渠道的到账/待处理金额。</p>';
      return;
    }
    if (refund.refund_key && !$('refund-key').value) $('refund-key').value = refund.refund_key;
    var conservation = money(refund.refund_amount) + ' = 已分 ' + money(refund.allocated_amount) +
      ' + 待处理 ' + money(refund.pending_amount);
    var rows = (refund.channels || []).map(function (c) {
      return '<tr data-channel="' + esc(c.channel) + '">' +
        '<td>' + esc(c.channel_label || c.channel) + '<small>' + esc(c.channel) + '</small></td>' +
        '<td class="num" title="' + moneyTitle(c.paid_amount) + '">' + money(c.paid_amount) + '</td>' +
        '<td class="num">' + money(c.prior_amount) + '</td>' +
        '<td class="num">' + money(c.available_amount) + '</td>' +
        '<td class="num requested">' + money(c.requested_amount) + '</td>' +
        '<td class="num actual"><b>' + money(c.amount) + '</b></td>' +
        '<td class="num pending">' + money(c.pending_amount) + '</td>' +
        '<td><span class="verdict" data-v="' + (c.pending_amount ? 'REVIEW' : 'PASS') + '">' +
          (c.pending_amount ? '等待' : '可退') + '</span></td>' +
      '</tr>';
    }).join('');
    var lineRows = (refund.line_splits || []).filter(function (l) { return l.refund_amount; }).map(function (l) {
      return '<li><code>' + esc(l.line_id) + '</code> 应退 ' + money(l.refund_amount) + '：' +
        esc(Object.keys(l.shares || {}).map(function (k) { return k + ' ' + money(l.shares[k]); }).join('，')) +
        ' <small>(' + esc(l.basis) + ')</small></li>';
    }).join('');
    $('split-detail').innerHTML =
      '<div class="split-head">' +
        '<div><strong>' + esc(refund.order_id) + '</strong> · <code>' + esc(refund.id) + '</code>' +
        (refund.parent_id ? ' · 差额单，原单 <code>' + esc(refund.parent_id) + '</code>' : '') + '</div>' +
        '<span class="verdict" data-v="' + (refund.status === 'ISSUED' ? 'PASS' : refund.status === 'STALE' ? 'REJECT' : 'REVIEW') + '">' +
          esc(statusLabel(refund.status)) + '</span>' +
      '</div>' +
      '<p class="conservation ' + (refund.balanced ? 'check-ok' : 'check-bad') + '">守恒：' + esc(conservation) + '</p>' +
      '<div class="table-wrap"><table><thead><tr>' +
        '<th>渠道</th><th>原实付</th><th>已累计退</th><th>当前可退额度</th><th>理想份额</th><th>本次实退</th><th>待处理</th><th>状态</th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
      (lineRows ? '<details class="line-splits"><summary>行级渠道归属</summary><ul>' + lineRows + '</ul></details>' : '');
  }

  function actionButtons(refund) {
    var id = esc(refund.id);
    if (refund.status === 'ISSUED') {
      return '<button type="button" class="mini" data-action="rerun" data-id="' + id + '">重跑（不重复退）</button>' +
        '<button type="button" class="mini secondary" data-action="supplement" data-id="' + id + '">补退差额单</button>';
    }
    if (refund.status === 'STALE') {
      return '<button type="button" class="mini" data-action="rerun" data-id="' + id + '">失效，点此重算</button>';
    }
    return '<button type="button" class="mini" data-action="issue" data-id="' + id + '">签发</button>' +
      '<button type="button" class="mini secondary" data-action="rerun" data-id="' + id + '">重算</button>';
  }

  function renderList(data) {
    state.refunds = data.refunds || [];
    var summary = data.summary || {};
    $('refund-summary').textContent = (summary.issued_count || 0) + ' 笔已签发 / ' +
      (summary.draft_count || 0) + ' 草稿 / ' + (summary.stale_count || 0) + ' 失效 · 待处理 ' +
      money(summary.issued_pending_amount || 0);
    $('refund-head').innerHTML = '<tr><th>订单/退款单</th><th>状态</th><th>应退</th><th>本次已分</th><th>待处理</th><th>渠道与流水</th><th>操作</th></tr>';
    $('refund-body').innerHTML = state.refunds.length ? state.refunds.map(function (refund) {
      return '<tr data-refund-id="' + esc(refund.id) + '">' +
        '<td><b>' + esc(refund.order_id) + '</b><br><code>' + esc(refund.id) + '</code>' +
          (refund.kind === 'SUPPLEMENT' ? '<br><small class="pending">差额单</small>' : '') + '</td>' +
        '<td><span class="verdict" data-v="' + (refund.status === 'ISSUED' ? 'PASS' : refund.status === 'STALE' ? 'REJECT' : 'REVIEW') + '">' +
          esc(statusLabel(refund.status)) + '</span>' + (refund.replayed ? '<br><small>重放原单</small>' : '') + '</td>' +
        '<td class="num">' + money(refund.refund_amount) + '</td>' +
        '<td class="num">' + money(refund.allocated_amount) + '</td>' +
        '<td class="num pending">' + money(refund.pending_amount) + '</td>' +
        '<td class="chips-cell">' + channelChips(refund) + '</td>' +
        '<td class="actions-cell">' + actionButtons(refund) + '</td>' +
      '</tr>';
    }).join('') : '<tr><td colspan="7" class="empty">还没有退款拆分；补录付款构成后生成一笔。</td></tr>';
  }

  async function loadRefunds(selectId) {
    try {
      var data = await request('/api/refunds');
      renderList(data);
      if (selectId) {
        var selected = data.refunds.find(function (r) { return r.id === selectId; });
        if (selected) renderDetail(selected);
      }
    } catch (error) { setMessage(error.message, true); }
  }

  async function loadPaymentComposition(oid) {
    try {
      var data = await request('/api/orders/' + encodeURIComponent(oid) + '/payments');
      $('payment-rows').innerHTML = '';
      (data.payments || []).forEach(addPaymentRow);
      if (!$('payment-rows').children.length) defaultRows();
    } catch (_error) {
      // 尚未补数据时保留可编辑样例行，不打断操作。
      if (!$('payment-rows').children.length) defaultRows();
    }
  }

  function defaultRows() {
    addPaymentRow({ channel: 'gift_card', amount_cents: 3000, line_id: 'O1L1' });
    addPaymentRow({ channel: 'balance', amount_cents: 5000 });
    addPaymentRow({ channel: 'bank_card', amount_cents: 12000 });
  }

  async function loadDemoPayments() {
    try {
      var sample = await request('/api/examples/payment-compositions');
      var orders = sample.orders || {};
      var ids = Object.keys(orders);
      for (var i = 0; i < ids.length; i += 1) {
        await request('/api/orders/' + encodeURIComponent(ids[i]) + '/payments', {
          method: 'POST',
          body: JSON.stringify({ payments: orders[ids[i]], unit: sample.unit || 'cents' })
        });
      }
      $('refund-order').value = 'O1';
      $('refund-amount').value = '45.00';
      $('refund-key').value = '';
      state.current = null;
      await loadPaymentComposition('O1');
      await loadRefunds();
      setMessage('已载入 clean 的礼品卡/余额/银行卡混付样例，可执行批次或生成 O1 拆分。');
    } catch (error) { setMessage(error.message, true); }
  }

  async function calculate(event) {
    if (event) event.preventDefault();
    try {
      setMessage('');
      var oid = orderId();
      var payments = collectPayments();
      var key = $('refund-key').value.trim();
      var payload = {
        order_id: oid,
        refund_amount: cents($('refund-amount').value),
        payments: payments,
        save_payments: true
      };
      if (key) payload.refund_key = key;
      var refund = await request('/api/refunds', { method: 'POST', body: JSON.stringify(payload) });
      renderDetail(refund);
      await loadRefunds(refund.id);
      setMessage('拆分已生成；各渠道份额可在下方核对。');
    } catch (error) { setMessage(error.message, true); }
  }

  async function issueCurrent() {
    if (!state.current) return;
    try {
      var refund = await request('/api/refunds/' + encodeURIComponent(state.current.id) + '/issue', {
        method: 'POST', body: JSON.stringify({})
      });
      renderDetail(refund);
      await loadRefunds(refund.id);
      setMessage(refund.replayed ? '该退款已签发，重跑未产生重复流水。' : '退款已签发，渠道流水已固定。');
    } catch (error) { setMessage(error.message, true); }
  }

  async function rowAction(action, id) {
    try {
      if (action === 'issue') {
        var issued = await request('/api/refunds/' + encodeURIComponent(id) + '/issue', { method: 'POST', body: '{}' });
        renderDetail(issued);
        setMessage('退款已签发。');
      } else if (action === 'rerun') {
        var rerun = await request('/api/refunds/' + encodeURIComponent(id) + '/rerun', { method: 'POST', body: '{}' });
        renderDetail(rerun);
        setMessage(rerun.status === 'ISSUED' ? '已签发退款重跑完成：金额不变，未重复退款。' : '未签发拆分已按最新补数据重算。');
      } else if (action === 'supplement') {
        var input = window.prompt('请输入后续补退差额（元）。系统会另开差额单，不改写原已签发退款。', '0.01');
        if (input === null) return;
        var difference = cents(input);
        if (difference <= 0) throw new Error('差额必须大于 0');
        var created = await request('/api/refunds/' + encodeURIComponent(id) + '/supplement', {
          method: 'POST', body: JSON.stringify({ difference_amount: difference, issue: false })
        });
        renderDetail(created);
        setMessage('已创建差额单 ' + created.id + '，核对后可签发。');
      }
      await loadRefunds(state.current && state.current.id);
    } catch (error) { setMessage(error.message, true); }
  }

  function boot() {
    if (!$('refund-form')) return;
    defaultRows();
    $('add-payment-row').addEventListener('click', function () { addPaymentRow(); });
    $('payment-rows').addEventListener('click', function (event) {
      if (event.target.classList.contains('remove-row')) event.target.closest('.payment-row').remove();
    });
    $('save-payments').addEventListener('click', function () {
      savePayments(true)
        .then(function () { return loadRefunds(state.current && state.current.id); })
        .catch(function (e) { setMessage(e.message, true); });
    });
    $('load-demo-payments').addEventListener('click', loadDemoPayments);
    $('refund-form').addEventListener('submit', calculate);
    $('issue-split').addEventListener('click', issueCurrent);
    $('refresh-refunds').addEventListener('click', function () { loadRefunds(); });
    $('refund-order').addEventListener('change', function () { loadPaymentComposition(orderId()); });
    $('refund-body').addEventListener('click', function (event) {
      var button = event.target.closest('[data-action]');
      if (!button) return;
      rowAction(button.getAttribute('data-action'), button.getAttribute('data-id'));
    });
    loadRefunds();
  }

  window.refreshRefunds = loadRefunds;
  document.addEventListener('DOMContentLoaded', boot);
})();
