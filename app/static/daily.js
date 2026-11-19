// 按站按日日结前端交互：导入、筛选、行跳转、复核、解锁
const feedback = () => document.querySelector('#feedback');
function show(msg, bad = false) {
  const el = feedback();
  if (el) { el.textContent = msg; el.style.color = bad ? 'var(--bad)' : 'var(--ok)'; }
}

// 行点击进入明细
document.querySelectorAll('.row-link').forEach((tr) => {
  tr.addEventListener('click', () => { location.assign(tr.dataset.href); });
});

// 筛选
document.querySelector('#filter-btn')?.addEventListener('click', () => {
  const station = document.querySelector('#filter-station').value.trim();
  const bizDate = document.querySelector('#filter-date').value.trim();
  const status = document.querySelector('#filter-status').value;
  const qs = new URLSearchParams();
  if (station) qs.set('station', station);
  if (bizDate) qs.set('biz_date', bizDate);
  if (status) qs.set('status', status);
  location.assign('/daily' + (qs.toString() ? '?' + qs.toString() : ''));
});

// 导入表单
const form = document.querySelector('#daily-form');
form?.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = form.querySelector('button');
  btn.disabled = true; btn.textContent = '生成中…';
  try {
    const mode = form.elements.mode.value;
    const url = mode === 'preview' ? '/api/daily/preview' : '/api/daily/runs';
    let init;
    const file = form.elements.file.files[0];
    if (file) {
      const body = new FormData();
      body.append('file', file);
      body.append('params', JSON.stringify({}));
      init = { method: 'POST', body };
    } else {
      init = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: form.elements.text.value }),
      };
    }
    const res = await fetch(url, init);
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '生成失败');
    if (mode === 'preview') {
      show(`预览成功：${data.rows.length} 个站日，隔离 ${data.quarantine.length} 行（未落库）`);
    } else {
      const s = data.save;
      show(`已保存：新建 ${s.created.length}，更新 ${s.updated.length}，无变化 ${s.unchanged.length}，锁定冲突 ${s.blocked.length}`);
      setTimeout(() => location.reload(), 700);
    }
  } catch (err) {
    show(err.message, true);
  } finally {
    btn.disabled = false; btn.textContent = '生成日结';
  }
});

// 复核
const reviewForm = document.querySelector('#review-form');
reviewForm?.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  try {
    const res = await fetch('/api/daily/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        station: reviewForm.dataset.station,
        biz_date: reviewForm.dataset.date,
        decision: reviewForm.elements.decision.value,
        note: reviewForm.elements.note.value,
        version: Number(reviewForm.dataset.version),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '复核失败');
    location.reload();
  } catch (err) { show(err.message, true); }
});

// 解锁
const reopenForm = document.querySelector('#reopen-form');
reopenForm?.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  try {
    const res = await fetch('/api/daily/reopen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ station: reopenForm.dataset.station, biz_date: reopenForm.dataset.date }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '解锁失败');
    location.reload();
  } catch (err) { show(err.message, true); }
});
