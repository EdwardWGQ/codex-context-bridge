const $ = id => document.getElementById(id);
let token = location.hash.slice(1);
try {
  token = token || sessionStorage.getItem('bridge-token') || '';
  sessionStorage.setItem('bridge-token', token);
  history.replaceState(null, '', '/');
} catch { /* The URL fragment remains available if storage is disabled. */ }
let current, busy = false, editing = false, generation = 0;
function notify(text, error = false) {
  $('feedback').textContent = text;
  $('feedback').dataset.error = String(error);
}
async function api(path, data) {
  const response = await fetch('/api/' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'X-Bridge-Token': token, ...(data === undefined ? {} : {'Content-Type': 'application/json'})},
    ...(data === undefined ? {} : {body: JSON.stringify(data)}), cache: 'no-store'
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作失败。');
  return result;
}
function render(value, hydrate = false) {
  if (!current || hydrate) {
    $('upstream').value = value.config.upstream_url;
    $('port').value = value.config.port;
    $('timeout').value = value.config.timeout_seconds;
    $('history').checked = value.config.allow_response_link_removal;
    editing = false;
  }
  current = value;
  $('state').textContent = value.running ? '运行中' : '待启动';
  $('state').dataset.running = String(value.running);
  $('url').textContent = value.client_url;
  $('requests').textContent = value.counts.requests;
  $('reasoning').textContent = value.counts.reasoning_removed;
  $('ids').textContent = value.counts.ids_removed;
  $('active').textContent = value.counts.active ? `正在处理 ${value.counts.active} 个请求，请等待完成后修改设置。` : value.running ? '代理已就绪。上游是否可用，请通过实际聊天验证。' : '代理未启动。';
  $('version').textContent = 'v' + value.version;
  controls();
}
function controls() {
  for (const element of $('config-form').elements) element.disabled = busy;
  $('save').textContent = busy ? '正在处理…' : current?.running ? '保存并应用' : '保存设置';
  $('toggle').textContent = current?.running ? '停止代理' : '启动代理';
  $('toggle').disabled = busy || !current || (!current.running && (editing || !current.config.upstream_url));
}
async function operate(action) {
  if (busy) return;
  busy = true; generation++; controls(); notify('');
  try { await action(); } catch (error) { notify(error.message, true); }
  finally { busy = false; controls(); }
}
$('config-form').addEventListener('input', () => { editing = true; notify('有修改待保存。'); controls(); });
$('config-form').addEventListener('invalid', event => {
  if ($('advanced').contains(event.target)) $('advanced').open = true;
}, true);
$('config-form').addEventListener('submit', event => {
  event.preventDefault();
  const data = {upstream_url: $('upstream').value.trim(), port: Number($('port').value), timeout_seconds: Number($('timeout').value), allow_response_link_removal: $('history').checked};
  operate(async () => { render(await api('config', data), true); notify(current.running ? '配置已应用。' : '设置已保存，可以启动代理。'); });
});
$('toggle').addEventListener('click', () => operate(async () => {
  render(await api(current.running ? 'stop' : 'start', {}));
  notify(current.running ? '代理已启动，请在客户端新开会话。' : '代理已停止。');
}));
$('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('url').textContent); notify('客户端地址已复制。'); }
  catch { notify('请选中地址手动复制。', true); }
});
async function refresh() {
  if (busy) return;
  const revision = generation;
  try { const state = await api('state'); if (!busy && revision === generation) render(state); }
  catch (error) { if (!busy && revision === generation) { $('state').textContent = '连接断开'; notify('服务连接失败，请重新打开启动入口。', true); } }
}
controls(); refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
