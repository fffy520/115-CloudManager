/* =========================================================
 * 115 网盘管理器 前端逻辑（无框架 / 无构建）
 * 分区：
 *   1. 工具与全局状态        6. 目录浏览（状态保持/原地更新）
 *   2. toast 栈              7. 搜索
 *   3. 确认弹窗 / 进度弹窗    8. 扫描管理
 *   4. hash 路由             9. 自动转存
 *   5. 健康 / 备份          10. 结果与查重  11. 批量/移动  12. 键盘/启动
 * ========================================================= */
'use strict';

/* 全局错误钩子：本地排障用（控制台 + window.__lastErr + toast） */
window.addEventListener('error', e=>{
  window.__lastErr = (e.message||'unknown') + ' @ ' + (e.filename||'') + ':' + (e.lineno||0);
  try{ if(typeof toast==='function') toast('脚本错误: '+window.__lastErr, true); }catch(_){}
});
window.addEventListener('unhandledrejection', e=>{
  window.__lastRejection = String(e.reason && e.reason.stack || e.reason);
});

/* ============ 1. 工具与全局状态 ============ */
const $  = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const sleep = ms => new Promise(r => setTimeout(r, ms));
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmt(b){ if(!b) return '0 B';
  if(b>=1024**4) return (b/1024**4).toFixed(2)+' TB';
  if(b>=1024**3) return (b/1024**3).toFixed(2)+' GB';
  if(b>=1024**2) return (b/1024**2).toFixed(1)+' MB';
  return Math.round(b/1024)+' KB'; }
/* 把完整路径裁成「… / 末 keep 段」用于窄列显示。
   铁律：title 一律用完整路径，否则会出现「显示不全、悬停也看不全」。 */
function shortPath(p, keep=3){
  const segs=String(p||'').split(' / ').filter(Boolean);
  return segs.length>keep ? '… / '+segs.slice(-keep).join(' / ') : segs.join(' / ');
}
/* 拆出叶子名与父级路径：父级弱化显示且可被省略号截断，叶子加粗且不参与截断 */
function splitPath(p){
  const segs=String(p||'').split(' / ').filter(Boolean);
  const leaf=segs.length?segs[segs.length-1]:'';
  const parents=segs.slice(0,-1);
  return {leaf, parentsTxt:(parents.length>2?'… / ':'')+parents.slice(-2).join(' / ')};
}
async function api(path, opt={}){
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000); // 30秒超时
  try {
    const r = await fetch(path, {...opt, signal: controller.signal});
    const d = await r.json().catch(()=>({error:'响应解析失败'}));
    if(!r.ok) throw new Error(d.error||d.detail||('HTTP '+r.status));
    return d;
  } finally {
    clearTimeout(timeout);
  }
}
function statusBadge(s){
  const m={done:['已完成','b-done'],running:['运行中','b-run'],queued:['排队中','b-que'],
    error:['失败','b-err'],stopped:['已停止','b-stop'],success:['成功','b-done'],
    repeat:['已存在','b-warn'],expired:['已失效','b-err'],failed:['失败','b-err'],pending:['待处理','b-que'],
    retry_wait:['等待重试','b-warn'],paused:['已暂停','b-stop']};
  const [t,c]=m[s]||[s,'b-que']; return `<span class="badge ${c}">${esc(t)}</span>`; }

/* 全局状态：跨页签共享的可变数据全部收拢在此 */
const S = {
  roots: null,                 // /api/roots 缓存
  sel: new Map(),              // 批量勾选  cid -> {cid,pid,name,is_dir,size}
  expanded: new Set(),         // 目录树已展开节点 cid（重建后自动恢复）
  treeCur: null,               // 当前浏览的树 {cid,name,live}
  treeMode: 'local',           // 目录浏览模式: 'local' | 'live'
  detailNode: null,            // 详情面板当前节点
  liveCache: new Map(),        // 在线浏览缓存: cid -> {children, timestamp}
  parsed: [],                  // 转存解析结果
  dup: {                       // 查重页
    data:null,                 // 最近一次 fetch 的 {groups}
    choice:new Map(),          // 用户显式勾选 cid -> bool（渲染时显式选择优先）
    open:new Set(),            // 展开的重复组 gid
    rootsLoaded:false,
    autoTimer:null,
  },
  tags: {                      // 标签页
    list:null,                 // /api/tags 全部标签(含计数)
    cur:[],                    // 多选:已选标签 id 数组(原为单值, 改为支持组合筛选)
    op:'and',                  // 组合模式: 'and'(交集) 或 'or'(并集)
    per_tag:{},                // 当前结果下每个 tag 的命中数 {tag_id: count}, 用于 chip 数字
    editing:false,             // 编辑模式: true 时显示 ✎ ✕ 按钮
    nodes:null,                // 当前标签的节点清单
    total:0,
  },
};

/* ============ 2. toast 栈 ============ */
function toast(msg, bad){
  const c=$('#toasts');
  const t=document.createElement('div'); t.className='toast-item'+(bad?' bad':'');
  const sp=document.createElement('span'); sp.textContent=msg; t.appendChild(sp);
  if(bad){ const x=document.createElement('span'); x.className='tx'; x.textContent='✕';
    x.title='关闭'; x.onclick=()=>t.remove(); t.appendChild(x); }
  c.appendChild(t);
  while(c.children.length>5) c.firstElementChild.remove();
  setTimeout(()=>{ t.classList.add('out'); setTimeout(()=>t.remove(),300); }, bad?8000:3500);
}

/* ============ 3. 确认弹窗 / 进度弹窗 ============ */
/**
 * appConfirm({title, message, items, danger, okText, cancelText, searchable, okWarn})
 *  - items: [{name, sub?, sz?}] 完整清单回显（可滚动），searchable 开启过滤框
 *  - 返回 Promise<boolean>；Esc / 遮罩 / 取消 => false
 */
function appConfirm(opt){
  return new Promise(res=>{
    const o=opt||{};
    const bg=document.createElement('div');
    bg.className='modal-bg'; bg.dataset.app='confirm'; bg.style.zIndex=70;
    bg.innerHTML=`<div class="modal" style="max-width:640px">
      <h3><span></span><span class="close" data-c="n">×</span></h3>
      <div class="ac-msg" style="${o.message?'':'display:none'}"></div>
      <div class="ac-warn" style="${o.okWarn?'':'display:none'}"></div>
      ${o.items?'<input class="confirm-filter" placeholder="过滤清单…" style="display:none"><ul class="confirm-list"></ul><div class="sub ac-sum" style="margin-top:6px"></div>':''}
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
        <button data-c="n"></button><button data-c="y"></button>
      </div></div>`;
    bg.querySelector('h3 span').textContent=o.title||'请确认';
    if(o.message) bg.querySelector('.ac-msg').textContent=o.message;
    if(o.okWarn){ const w=bg.querySelector('.ac-warn'); w.textContent=o.okWarn; }
    const okBtn=bg.querySelector('[data-c="y"]'), noBtn=bg.querySelector('[data-c="n"]');
    okBtn.textContent=o.okText||'确定'; noBtn.textContent=o.cancelText||'取消';
    okBtn.className=o.danger?'danger':'pri';
    noBtn.className='cancel-btn';
    if(o.items){
      const ul=bg.querySelector('.confirm-list'), fi=bg.querySelector('.confirm-filter');
      bg.querySelector('.ac-sum').textContent=`共 ${o.items.length} 项`+
        (o.items.some(i=>i.sz)?` · 合计 ${fmt(o.items.reduce((s,i)=>s+(i.sz||0),0))}`:'');
      const renderList=q=>{
        ul.innerHTML='';
        const list=o.items.filter(i=>!q||i.name.toLowerCase().includes(q));
        list.slice(0,500).forEach(i=>{
          const li=document.createElement('li');
          const nm=document.createElement('span'); nm.className='cl-name'; nm.textContent=i.name; nm.title=i.name;
          li.appendChild(nm);
          if(i.sub){ const sp=document.createElement('span'); sp.className='sub'; sp.textContent=i.sub; li.appendChild(sp); }
          ul.appendChild(li);
        });
        if(list.length>500){
          const li=document.createElement('li');
          const sp=document.createElement('span'); sp.className='sub';
          sp.textContent=`…其余 ${list.length-500} 项未列出（不影响删除范围）`;
          li.appendChild(sp); ul.appendChild(li);
        }
      };
      renderList('');
      if(o.searchable!==false){ fi.style.display='block'; fi.oninput=()=>renderList(fi.value.trim().toLowerCase()); }
    }
    const done=v=>{ document.removeEventListener('keydown',onKey,true); bg.remove(); res(v); };
    const onKey=e=>{ if(e.key==='Escape'){ e.stopPropagation(); done(false); } };
    bg.addEventListener('click', e=>{
      const c=e.target.closest('[data-c]');
      if(c) done(c.dataset.c==='y');
      else if(e.target===bg) done(false);
    });
    document.addEventListener('keydown',onKey,true);
    document.body.appendChild(bg);
    okBtn.focus();
  });
}
/** 二段式危险确认：清单确认 + 简单二次确认 */
async function confirmDanger(o){
  if(!await appConfirm({...o, danger:true, okText:'删除'})) return false;
  return appConfirm({title:'二次确认', message:'真的要删除吗？\n删除将移入 115 回收站（30 天内可恢复）',
    danger:true, okText:'确认删除'});
}

/** 进度弹窗（长任务），返回控制器 {update(text,pct), close(msg), cancel()} */
function appProgress(title){
  const bg=document.createElement('div');
  bg.className='modal-bg'; bg.dataset.app='progress'; bg.style.zIndex=80;
  bg.innerHTML=`<div class="modal" style="max-width:460px"><h3><span></span><span class="close" data-c="cancel">×</span></h3>
    <div class="progress" style="height:10px;min-width:300px"><div style="width:0%"></div></div>
    <div class="pg-text">准备中…</div></div>`;
  bg.querySelector('h3 span:first-child').textContent=title;
  document.body.appendChild(bg);
  const bar=bg.querySelector('.progress>div'), txt=bg.querySelector('.pg-text');
  let closed=false;
  // 点击X或遮罩关闭
  bg.addEventListener('click',e=>{
    const c=e.target.closest('[data-c]');
    if(c && c.dataset.c==='cancel' && !closed){
      bg.remove();
    } else if(e.target===bg && !closed){
      bg.remove();
    }
  });
  return {
    update(text,pct){ txt.textContent=text; if(pct!=null) bar.style.width=Math.max(0,Math.min(100,pct))+'%'; },
    close(msg){ closed=true; txt.textContent=msg||'完成'; bar.style.width='100%';
      // 隐藏关闭按钮，显示完成关闭按钮
      const closeBtn=bg.querySelector('[data-c="cancel"]');
      if(closeBtn) closeBtn.style.display='none';
      const b=document.createElement('button'); b.className='pri'; b.textContent='关闭'; b.style.marginTop='12px';
      b.onclick=()=>bg.remove(); bg.querySelector('.modal').appendChild(b); }
  };
}

/* ============ 4. hash 路由 ============ */
const TABS = { dash:'p-dash', tree:'p-tree', search:'p-search', scan:'p-scan', transfer:'p-transfer', dup:'p-dup', ai:'p-ai', tags:'p-tags' };
let curTab = 'dash';

function parseHash(){
  const h=location.hash.replace(/^#\/?/,'');
  const [t,qs]=h.split('?');
  return { tab:t||'', params:new URLSearchParams(qs||'') };
}
function syncHash(params){
  let h='#'+curTab;
  if(params===undefined) params=hashForTab(curTab);
  if(params){ const s=params.toString(); if(s) h+='?'+s; }
  history.replaceState(null,'',h);
}
/* 页签对应的可复现参数：搜索=筛选条件，查重=范围 */
function hashForTab(key){
  if(key==='search'){
    const p=new URLSearchParams();
    const q=$('#searchQ').value.trim();
    if(q) p.set('q',q);
    if($('#searchType').value!=='all') p.set('type',$('#searchType').value);
    if($('#searchRoot').value) p.set('root',$('#searchRoot').value);
    if($('#searchMinMB').value) p.set('min',$('#searchMinMB').value);
    if($('#searchMaxMB').value) p.set('max',$('#searchMaxMB').value);
    return p;
  }
  if(key==='dup'){
    const sc=$('#dupScope').value;
    return sc?new URLSearchParams({scope:sc}):new URLSearchParams();
  }
  return new URLSearchParams();
}
function applyTab(key, params){
  if(!TABS[key]) return;
  params=params||null;
  curTab=key;
  $$('.tab').forEach(x=>x.classList.toggle('on', x.dataset.tab===key));
  $$('.panel').forEach(p=>p.classList.toggle('on', p.id===TABS[key]));
  localStorage.setItem('m115_tab', key);
  if(key==='scan') loadScanPage();
  else if(key==='search') loadSearchPage(params);
  else if(key==='dup') loadDupPage(params);
  else if(key==='transfer') loadTransferPage();
  else if(key==='dash') loadDash();
  else if(key==='ai') loadAiPage();
  else if(key==='tags') loadTagsPage();
}
window.addEventListener('hashchange', ()=>{
  const {tab,params}=parseHash();
  if(TABS[tab]) applyTab(tab, [...params].length?params:null);
});

/* ============ 5. 健康 / 备份 ============ */
async function loadHealth(){
  try{ const h=await api('/api/health');
    const el=$('#health');
    if(h.cookie_ok){
      let txt='🍪 Cookie 正常';
      if(h.ok_since){
        const days=Math.floor((Date.now()-new Date(h.ok_since.replace(' ','T')).getTime())/86400000);
        if(days>0) txt+=` · 已连续 ${days} 天`;
      }
      el.textContent=txt; el.className='ok';
      if(h.last_fail_at) el.title='上次失败: '+h.last_fail_at;
    }else{
      el.textContent='🍪 Cookie 失效: '+(h.cookie_error||''); el.className='bad';
    }
  }catch(e){ $('#health').textContent='🍪 服务异常'; $('#health').className='bad'; }
  try{ const s=await api('/api/stats');
    $('#dbStats').textContent=`本地库: ${s.dirs.toLocaleString()} 目录 / ${s.files.toLocaleString()} 文件 / ${fmt(s.total_size)}`; }catch(e){}
}

async function openBackupModal(){
  const old=document.getElementById('backupModal'); if(old) old.remove();
  const bg=document.createElement('div'); bg.id='backupModal'; bg.className='modal-bg';
  bg.onclick=e=>{ if(e.target===bg) bg.remove(); };
  bg.innerHTML=`<div class="modal">
      <h3>💾 数据库备份管理
        <span class="close" data-action="backup-close">×</span>
      </h3>
      <div class="bk-info">
        📂 备份目录: <code id="bkDir"></code><br>
        🕒 自动备份策略: <b>每天凌晨 03:00</b>（保留最近 7 个每日备份 + 4 个周备份）<br>
        🚀 服务启动时自动备份一次作为基线
      </div>
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
        <button data-action="backup-now" style="background:var(--accent);color:#fff;border-color:var(--accent)">📦 立即备份一次</button>
        <button data-action="backup-refresh">🔄 刷新列表</button>
        <span class="sub" style="margin-left:auto">恢复操作会先自动备份当前 DB</span>
      </div>
      <div id="bkList" style="border:1px solid var(--line);border-radius:8px;overflow:hidden"></div>
    </div>`;
  document.body.appendChild(bg);
  await loadBackupList();
}
async function loadBackupList(){
  const el=document.getElementById('bkList'); if(!el) return;
  el.innerHTML='<div class="empty sub" style="padding:18px">加载中…</div>';
  try{
    const d=await api('/api/backups');
    document.getElementById('bkDir').textContent=d.dir;
    if(!d.items.length){ el.innerHTML='<div class="empty sub" style="padding:18px">暂无备份</div>'; return; }
    el.innerHTML=d.items.map(b=>{
      const t=new Date(b.ts*1000);
      const ts=`${t.getFullYear()}-${String(t.getMonth()+1).padStart(2,'0')}-${String(t.getDate()).padStart(2,'0')} ${String(t.getHours()).padStart(2,'0')}:${String(t.getMinutes()).padStart(2,'0')}`;
      const size=b.size>1024*1024?(b.size/1024/1024).toFixed(1)+' MB':Math.round(b.size/1024)+' KB';
      const kind=b.kind==='weekly'?'<span class="b-warn" style="padding:1px 6px;border-radius:4px;font-size:10px;margin-left:4px">周备份</span>':'';
      return `<div class="bk-row">
        <div style="flex:1;min-width:0">
          <span class="name" title="${esc(b.name)}">${esc(b.name.replace(/^115_tree-/,'').replace(/\.db$/,''))}</span>${kind}
          <div class="meta">${ts} · ${size} · <b>${esc(b.reason)}</b></div>
        </div>
        <div class="act">
          <button data-action="bk-restore" data-name="${esc(b.name)}" style="background:var(--warn);color:#fff;border-color:var(--warn)">恢复</button>
          <button class="danger" data-action="bk-del" data-name="${esc(b.name)}">删除</button>
        </div>
      </div>`;
    }).join('');
  }catch(e){ el.innerHTML='<div class="empty" style="padding:18px">加载失败: '+esc(e.message)+'</div>'; }
}
async function doBackupNow(){
  try{ await api('/api/backup',{method:'POST'}); toast('✓ 备份完成'); loadBackupList(); }
  catch(e){ toast('备份失败: '+e.message,true); }
}
async function doRestore(name){
  const ok=await appConfirm({title:'恢复备份',
    message:`确定要从备份【${name}】恢复吗？\n\n恢复前会自动备份当前 DB。\n恢复后服务需要重启。`,
    okText:'恢复'});
  if(!ok) return;
  const ok2=await appConfirm({title:'再次确认', message:`真的要恢复【${name}】吗？`, danger:true, okText:'确认恢复'});
  if(!ok2) return;
  try{
    await api('/api/backups/restore/'+encodeURIComponent(name),
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})});
    toast('✓ 恢复成功（建议重启服务）');
  }catch(e){ toast('恢复失败: '+e.message,true); }
}
async function delBackup(name){
  if(!await appConfirm({title:'删除备份', message:`删除备份【${name}】？`, danger:true, okText:'删除'})) return;
  try{ await api('/api/backups/'+encodeURIComponent(name),{method:'DELETE'}); toast('已删除'); loadBackupList(); }
  catch(e){ toast('删除失败: '+e.message+'\n(可能沙箱保护导致,可手动删除文件)',true); }
}

/* ---------- Cookie 管理弹窗 ---------- */
async function openCookieModal(){
  const old=document.getElementById('cookieModal'); if(old) old.remove();
  const bg=document.createElement('div'); bg.id='cookieModal'; bg.className='modal-bg';
  bg.innerHTML=`<div class="modal">
    <h3><span>🍪 Cookie 管理</span><span class="close" data-action="cookie-close">×</span></h3>
    <div id="cookieInfo" style="margin-bottom:12px;font-size:13px"><div class="sub">加载中…</div></div>
    <div class="sub" style="margin-bottom:6px">Cookie 内容（从浏览器 F12 → Network → Request Headers → Cookie 复制整行粘贴到这里）：</div>
    <textarea id="cookieInput" rows="4" style="width:100%;font-family:Consolas,monospace;font-size:12px;resize:vertical" placeholder="USERSESSIONID=xxx; UID=xxx; CID=xxx; ..."></textarea>
    <div id="cookieTestResult" style="margin-top:8px;min-height:20px;font-size:13px"></div>
    <div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end">
      <button data-action="cookie-test">🔍 测试</button>
      <button class="pri" data-action="cookie-save">💾 保存</button>
      <button data-action="cookie-close">取消</button>
    </div></div>`;
  bg.onclick=e=>{ if(e.target===bg) bg.remove(); };
  document.body.appendChild(bg);
  try{
    const info=await api('/api/cookie');
    const el=document.getElementById('cookieInfo');
    const ok=info.cookie_ok;
    el.innerHTML=`
      <div style="display:flex;gap:12px;align-items:center;margin-bottom:6px">
        <span style="font-size:15px">${ok?'✅':'❌'}</span>
        <b style="color:${ok?'var(--ok)':'var(--danger)'}">Cookie ${ok?'正常':'失效'}</b>
        ${info.cookie_error?`<span class="sub">${esc(info.cookie_error)}</span>`:''}
      </div>
      <div style="display:flex;gap:16px;font-size:12px;color:var(--sub)">
        <span>长度: ${info.cookie_length} 字符</span>
        <span>UID: ${info.has_uid?'✅':'❌'}</span>
        <span>USERSESSIONID: ${info.has_session?'✅':'❌'}</span>
      </div>`;
    document.getElementById('cookieInput').value=info.cookie_content||'';
  }catch(e){
    document.getElementById('cookieInfo').innerHTML=`<div style="color:var(--danger)">加载失败: ${esc(e.message)}</div>`;
  }
}
async function testCookie(){
  const c=document.getElementById('cookieInput').value.trim();
  if(!c){ toast('请先粘贴 Cookie',true); return; }
  const el=document.getElementById('cookieTestResult');
  el.innerHTML='<span class="sub">测试中…</span>';
  try{
    const d=await api('/api/cookie/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie:c})});
    el.innerHTML=d.cookie_ok
      ?`<span style="color:var(--ok)">✅ ${esc(d.message)}</span>`
      :`<span style="color:var(--danger)">❌ ${esc(d.message)}</span>`;
  }catch(e){ el.innerHTML=`<span style="color:var(--danger)">测试失败: ${esc(e.message)}</span>`; }
}
async function saveCookie(){
  const c=document.getElementById('cookieInput').value.trim();
  if(!c){ toast('请先粘贴 Cookie',true); return; }
  try{
    const d=await api('/api/cookie',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie:c})});
    if(d.cookie_ok){
      toast('✅ Cookie 已保存并验证通过');
      document.getElementById('cookieModal').remove();
      loadHealth();
    }else{
      toast('Cookie 已保存但验证失败: '+(d.cookie_error||''),true);
      document.getElementById('cookieTestResult').innerHTML=`<span style="color:var(--danger)">⚠️ 保存成功但验证不通过: ${esc(d.cookie_error||'')}</span>`;
    }
  }catch(e){ toast('保存失败: '+e.message,true); }
}

/* ============ 5.5 概览仪表盘 ============ */
async function loadDash(){
  const meta=$('#dashMeta');
  try{ meta.textContent='加载中…'; }catch(e){}
  let d;
  try{ d=await api('/api/dashboard'); }
  catch(e){ meta.textContent='仪表盘加载失败: '+e.message; return; }
  meta.textContent=`数据生成于 ${d.generated_at}（服务端缓存 60 秒）`;
  const s=d.stats;
  $('#dashStats').innerHTML=[
    ['一级目录',s.roots.toLocaleString()],['目录总数',s.dirs.toLocaleString()],
    ['文件总数',s.files.toLocaleString()],['总大小',fmt(s.total_size)],
    ['本地数据目录',s.scanned.toLocaleString()],['重复组',d.dup.groups],
    ['精确重复组',d.dup.exact_groups],['可清理',fmt(d.dup.reclaim)],
  ].map(([k,v])=>`<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');
  renderTrend(d.trend);
  renderBuckets(d.buckets);
  renderDashRoots(d.top_roots);
  renderDashFiles(d.top_files);
  renderDashDup(d.dup);
  renderDashTransfer(d);
  renderDashJobs(d);
  renderDashActive(d);
}
function fmtElapsed(sec){
  sec = Math.max(0, parseInt(sec)||0);
  if(sec < 60) return sec + ' 秒';
  if(sec < 3600) return Math.floor(sec/60) + ' 分 ' + (sec%60) + ' 秒';
  const h=Math.floor(sec/3600), m=Math.floor((sec%3600)/60);
  return h + ' 时 ' + m + ' 分';
}
function renderDashActive(d){
  const el=$('#dashJobs');
  // 顶部：当前活跃任务进度（仅在有 running 任务时显示）
  let activeHtml='';
  const aj=d.active_job;
  if(aj){
    const pct=aj.total>0?Math.min(100,Math.round(aj.done_count/aj.total*100)):0;
    const rateTxt=aj.rate_per_sec>0?(aj.rate_per_sec>=1?(aj.rate_per_sec.toFixed(2)+' 个/秒'):(Math.round(1/aj.rate_per_sec)+' 秒/个')):'计算中…';
    const etaTxt=(aj.rate_per_sec>0 && aj.total>aj.done_count)?('预计还剩 '+fmtElapsed(Math.round((aj.total-aj.done_count)/aj.rate_per_sec))):'';
    activeHtml=`<div style="margin-bottom:10px;padding:10px 12px;background:var(--accent)0d;border:1px solid var(--accent)59;border-radius:6px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:13px">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);animation:dashPulse 1.5s infinite"></span>
        <b style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(aj.target_name)}">${esc(aj.target_name)}</b>
        <span class="sub">${aj.done_count.toLocaleString()} / ${aj.total.toLocaleString()} · ${pct}%</span>
      </div>
      <div style="background:#eef0f3;border-radius:4px;height:10px;overflow:hidden;margin-bottom:5px">
        <div style="width:${pct}%;height:100%;background:linear-gradient(90deg,var(--accent),var(--ok));transition:width 1s"></div>
      </div>
      <div style="font-size:11.5px" class="sub">已运行 ${fmtElapsed(aj.elapsed_sec)} · 速度 ${rateTxt}${etaTxt?' · '+etaTxt:''}</div>
      ${aj.current_path?`<div style="font-size:11px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" class="sub" title="${esc(aj.current_path)}">📂 ${esc(shortPath(aj.current_path,3))}</div>`:''}
    </div>`;
  }
  // 中部：🆕 新发现目录流（实时滚动感）
  const scans=d.recent_scans||[];
  let scansHtml='';
  if(scans.length){
    scansHtml=`<div style="margin-bottom:10px">
      <div class="sub" style="font-size:11.5px;margin-bottom:4px;display:flex;align-items:center">
        🆕 新发现目录
        <span style="flex:1"></span>
        <span>最近 ${scans.length} 条</span>
      </div>
      <div style="max-height:210px;overflow-y:auto;border:1px solid var(--line);border-radius:6px;background:var(--card)">
        ${scans.map((s,i)=>{
          const nm=s.name||'?';
          /* full = 后端按 pid 链还原的完整路径，直接进 title(悬停必须看得到全)；
             可见文本才裁段，并用 flex 保护叶子名不被省略号吃掉 */
          const full=s.path||[s.root_name,nm].filter(Boolean).join(' / ');
          const {leaf,parentsTxt}=splitPath(full);
          const nodes=s.node_count||0;
          const szTxt=nodes>0?(nodes+' 项'):'';
          const t=s.scanned_at?String(s.scanned_at).slice(11,19):'';
          const dn=s.scanned_at?String(s.scanned_at).slice(5,10):'';
          const accent=i<3?'var(--accent)':'var(--txt)';
          return `<div style="display:flex;align-items:center;gap:8px;padding:4px 10px;border-bottom:1px dashed var(--line);font-size:12.5px${i===0?';background:var(--ok)08':''}">
            <span class="sub" style="width:56px;flex:none;text-align:right;font-family:ui-monospace,monospace;font-size:11px">${esc(t)}</span>
            <span style="flex:1;min-width:0;display:flex;overflow:hidden;color:${accent}" title="${esc(full)}">
              ${parentsTxt?`<span class="sub" style="font-size:11px;flex:1 100 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(parentsTxt)} / </span>`:''}
              <b style="flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(leaf)}</b>
            </span>
            <span class="sub" style="flex:none;font-size:11px">${esc(szTxt)}</span>
            <span class="sub" style="flex:none;width:40px;text-align:right;font-size:11px">${esc(dn)}</span>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }
  // 底部：原「近 7 天 + 最近任务」保留
  const daily=d.scan_daily||[];
  const dailyHtml=daily.length
    ? '<div style="margin-bottom:8px;font-size:12px" class="sub">近 7 天扫描：'+
      daily.map(x=>`${x.d.slice(5)} ${x.n}个`).join(' · ')+'</div>'
    : '<div class="sub" style="font-size:12px;margin-bottom:8px">近 7 天无扫描记录</div>';
  const jobs=(d.recent_jobs||[]).map(j=>`
    <div style="display:flex;gap:8px;align-items:center;padding:3px 0;font-size:12.5px">
      ${statusBadge(j.status)}
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(j.target_name)}">${esc(j.target_name)}</span>
      <span class="sub" style="flex:none">${j.done_count}/${j.total||'?'}</span>
    </div>`).join('');
  el.innerHTML=activeHtml+scansHtml+dailyHtml+(jobs||'<div class="sub" style="font-size:12px">暂无任务</div>');
}
function renderTrend(trend){
  const el=$('#dashTrend');
  if(!trend || !trend.length){
    el.innerHTML='<div class="empty sub" style="padding:18px">暂无快照（每天 03:05 自动记录，明天起开始积累）</div>'; return;
  }
  const max=Math.max(...trend.map(t=>t.total_size||0),1);
  const last=trend[trend.length-1];
  el.innerHTML='<div style="display:flex;flex-direction:column;gap:3px">'+trend.map(t=>{
    const pct=Math.max(Math.round((t.total_size||0)/max*100),1);
    return `<div style="display:flex;align-items:center;gap:8px;font-size:12px">
      <span class="sub" style="width:76px;flex:none">${esc(t.date.slice(5))}</span>
      <div style="flex:1;background:#eef0f3;border-radius:4px;height:14px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:var(--accent);border-radius:4px" title="${(t.total_size/1024**4).toFixed(2)} TB · ${t.dirs} 目录 · ${t.files} 文件"></div>
      </div>
      <span class="sub" style="width:90px;flex:none;text-align:right">${(t.total_size/1024**4).toFixed(2)} TB</span>
    </div>`;
  }).join('')+'</div>'
  +`<div class="sub" style="margin-top:8px">共 ${trend.length} 天 · 目录 ${last.dirs.toLocaleString()} · 文件 ${last.files.toLocaleString()} · 已扫 ${last.scanned.toLocaleString()}</div>`;
}
function renderBuckets(b){
  const rows=[['< 1GB',b.lt1gb],['1~10GB',b.gb1_10],['10~100GB',b.gb10_100],['> 100GB',b.gb100p]];
  const total=rows.reduce((s,r)=>s+r[1],0)||1;
  $('#dashBuckets').innerHTML=rows.map(([k,v])=>`
    <div style="display:flex;align-items:center;gap:8px;font-size:12.5px;padding:3px 0">
      <span class="sub" style="width:66px;flex:none">${k}</span>
      <div style="flex:1;background:#eef0f3;border-radius:4px;height:14px;overflow:hidden">
        <div style="width:${Math.round(v/total*100)}%;height:100%;background:var(--ok)"></div>
      </div>
      <span style="width:118px;flex:none;text-align:right;font-size:12px">${v.toLocaleString()} 个 · ${Math.round(v/total*100)}%</span>
    </div>`).join('');
}
function renderDashRoots(roots){
  const el=$('#dashRoots');
  if(!roots||!roots.length){ el.innerHTML='<div class="empty sub" style="padding:12px">暂无数据</div>'; return; }
  el.innerHTML=roots.map((r,i)=>`
    <div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px dashed var(--line);font-size:13px">
      <span class="sub" style="width:22px;flex:none">${i+1}</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;color:var(--accent)" title="${esc(r.name)}" data-action="open-in-tree" data-cid="${esc(r.cid)}">${esc(r.name)}</span>
      <span class="sub" style="flex:none">${r.fc.toLocaleString()} 文件 · <b>${fmt(r.sz)}</b></span>
    </div>`).join('');
}
function renderDashFiles(files){
  const el=$('#dashFiles');
  if(!files||!files.length){ el.innerHTML='<div class="empty sub" style="padding:12px">暂无数据</div>'; return; }
  el.innerHTML=files.map((f,i)=>`
    <div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px dashed var(--line);font-size:13px">
      <span class="sub" style="width:22px;flex:none">${i+1}</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(f.path)}">${esc(f.name)}</span>
      <span class="sub" style="flex:none"><b>${fmt(f.size)}</b></span>
      <button data-action="open-in-tree" data-cid="${esc(f.pid)}" style="padding:2px 8px;font-size:11.5px">打开</button>
    </div>`).join('');
}
function renderDashDup(dup){
  $('#dashDup').innerHTML=`
    <div style="font-size:13px;line-height:2">
      疑似重复 <b>${dup.groups}</b> 组，其中精确重复 <b style="color:var(--danger)">${dup.exact_groups}</b> 组<br>
      多余副本 <b>${dup.extra_copies}</b> 个 · 可释放 <b style="color:var(--ok)">${fmt(dup.reclaim)}</b>
    </div>
    <div style="margin-top:10px"><button class="pri" data-action="tab" data-tab="dup">去查重清理 →</button></div>`;
}
function renderDashTransfer(d){
  const sc=d.transfer_script, db=d.transfer_db;
  $('#dashTransfer').innerHTML=`
    <div style="font-size:13px;line-height:2">
      <b>脚本全量转存</b>（transfer_state）<br>
      共 ${sc.total.toLocaleString()} 条：<span style="color:var(--ok)">成功 ${sc.success.toLocaleString()}</span> ·
      已存在 ${sc.repeat} · 失效 ${sc.expired} · 失败 ${sc.failed}<br>
      <b>管理器任务</b>：${db.tasks} 个任务 / 处理 ${db.processed.toLocaleString()} 条
    </div>`;
}
function renderDashJobs(d){
  const daily=d.scan_daily||[];
  const dailyHtml=daily.length
    ? '<div style="margin-bottom:8px;font-size:12px" class="sub">近 7 天扫描：'+
      daily.map(x=>`${x.d.slice(5)} ${x.n}个`).join(' · ')+'</div>'
    : '<div class="sub" style="font-size:12px;margin-bottom:8px">近 7 天无扫描记录</div>';
  const jobs=(d.recent_jobs||[]).map(j=>`
    <div style="display:flex;gap:8px;align-items:center;padding:3px 0;font-size:12.5px">
      ${statusBadge(j.status)}
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(j.target_name)}">${esc(j.target_name)}</span>
      <span class="sub" style="flex:none">${j.done_count}/${j.total||'?'}</span>
    </div>`).join('');
  $('#dashJobs').innerHTML=dailyHtml+(jobs||'<div class="sub" style="font-size:12px">暂无任务</div>');
}

/* ============ 6. 目录浏览 ============ */
/* ---------- 标签角标(树行/详情面板共用) ---------- */
/* 标签配色: 只认合法 hex(顺带堵掉 CSS 注入), 没配色的用分类主题色, 兜底中性灰 */
const TAG_CAT_COLORS={'类型':'#7a5af8','分辨率':'#0052d9','字幕':'#2ba471','国家':'#e37318',
  '画质':'#0aa5a5','音频':'#b0812b','状态':'#e34d59','来源':'#4a5568','属性':'#5b5b6e','自定义':'#8a919c'};
function tagColor(t){
  const c=String((t&&t.color)||'').trim();
  if(/^#[0-9a-fA-F]{6}$/.test(c)) return c;
  if(/^#[0-9a-fA-F]{3}$/.test(c)) return '#'+c[1]+c[1]+c[2]+c[2]+c[3]+c[3];
  return TAG_CAT_COLORS[(t&&t.category)||'']||'#8a919c';
}
function chipHtml(t){
  const c=tagColor(t);
  return `<span class="tchip" title="标签: ${esc(t.name)}${t.category?(' · '+esc(t.category)):''}" `+
    `style="background:${c}1f;color:${c};border:1px solid ${c}59">${esc(t.name)}</span>`;
}
function renderRowChips(el, tags){
  el.innerHTML=(tags||[]).map(chipHtml).join('');
}
/* 打标/摘标后原地刷新树行与详情面板的角标; map={cid:[tags]} */
function patchRowTags(map){
  for(const cid in map){
    const el=document.querySelector(`#treeBox .twrap[data-cid="${CSS.escape(cid)}"] .tchips`);
    if(el) renderRowChips(el, map[cid]||[]);
  }
  if(S.detailNode && Object.prototype.hasOwnProperty.call(map, String(S.detailNode.cid))){
    S.detailNode.tags=map[String(S.detailNode.cid)]||[];
    const dt=$('#dTags');
    if(dt) dt.innerHTML=S.detailNode.tags.length?S.detailNode.tags.map(chipHtml).join(' '):'<span class="sub">无</span>';
  }
}
async function loadRoots(){
  if(S.roots) return S.roots;
  const d=await api('/api/roots');
  S.roots=d.items;
  refreshDirPickerLabels();
  return S.roots;
}
/* 目录选择器已全部改为「按钮 + 公共目录选择器 pickDir」，不再填充原生下拉。
   此处在 roots 载入后，把各按钮上残留的 cid 文案补全成目录名。 */
function refreshDirPickerLabels(){
  if(!S.roots) return;
  const pairs=[
    ['#searchRoot','#searchRootBtn'], ['#scanRootSel','#scanRootBtn'], ['#schRootSel','#schRootBtn'],
    ['#aiScopeCid','#aiScopeDisplay'], ['#tfTargetCid','#tfTargetBtn'], ['#dupScope','#dupScopeBtn'],
  ];
  pairs.forEach(([i,b])=>{
    const iv=$(i), bv=$(b);
    if(!iv || !bv || !iv.value) return;
    const hit=S.roots.find(x=>x.cid===iv.value);
    if(hit && hit.name && !bv.textContent.includes(hit.name)){
      bv.textContent='📂 '+hit.name;
      bv.title=hit.name;
    }
  });
}

/**
 * 树节点行。展开状态记录在 S.expanded；子节点首次展开后构建，
 * 构建完成时若子目录在 expanded 中则自动级联展开（重建树后自动恢复现场）。
 */
function trow(node, depth, live){
  const wrap=document.createElement('div'); wrap.className='twrap';
  wrap.dataset.cid=node.cid;
  if(node.pid) wrap.dataset.pid=node.pid;
  wrap._live=!!live;

  const row=document.createElement('div'); row.className='trow';
  const ck=document.createElement('input'); ck.type='checkbox'; ck.className='ckrow';
  ck.checked=S.sel.has(node.cid);
  ck.title='勾选后可批量移动/删除';
  ck.onclick=e=>e.stopPropagation();
  ck.onchange=()=>toggleSel(node, ck.checked);
  row.appendChild(ck);

  const tw=document.createElement('span'); tw.className='tw';
  const nm=document.createElement('span'); nm.className='tname '+(node.is_dir?'dir':'file');
  nm.textContent=node.name; nm.title=node.name;
  row.appendChild(tw); row.appendChild(nm);
  const chips=document.createElement('span'); chips.className='tchips';
  renderRowChips(chips, node.tags||[]);
  row.appendChild(chips);

  if(node.is_dir && node.scan_state && node.scan_state!=='done'){
    const b=document.createElement('span'); b.className='badge b-warn'; b.textContent='未扫';
    row.appendChild(b);
    const sb=document.createElement('button');
    sb.className='badge b-pri'; sb.style.cursor='pointer'; sb.style.border='none';
    sb.textContent='扫'; sb.title='发起扫描：'+node.name;
    sb.onclick=e=>{ e.stopPropagation(); scanOne(node.cid, node.name); };
    row.appendChild(sb);
  }
  const meta=document.createElement('span'); meta.className='tmeta';
  meta.textContent=node.is_dir?(node.child_count!=null?node.child_count+' 项':''):fmt(node.size);
  row.appendChild(meta);
  wrap.appendChild(row);

  if(node.is_dir){
    tw.textContent='▶';
    const kids=document.createElement('div'); kids.className='tkids';
    wrap.appendChild(kids);
    async function toggle(){
      const open=kids.classList.toggle('open');
      tw.textContent=open?'▼':'▶';
      row.classList.toggle('sel', open);
      if(open){
        S.expanded.add(node.cid);
        // 保存展开状态到localStorage
        try{ localStorage.setItem('tree_expanded', JSON.stringify([...S.expanded])); }catch(_){}
        if(!wrap._built){
          wrap._built=true;
          kids.innerHTML='<div class="empty sub" style="padding:8px">加载中…</div>';
          try{
            const sortVal=$('#treeSort')?.value||'time_desc';
            let ch=[];
            // 在线模式使用缓存（5分钟有效期）
            if(live){
              const cached=S.liveCache.get(node.cid);
              const now=Date.now();
              if(cached && (now-cached.timestamp)<300000){
                ch=[...cached.children]; // 使用缓存的副本
              } else {
                const d=await api('/api/live/'+node.cid);
                ch=d.children||d.items||[];
                S.liveCache.set(node.cid,{children:ch, timestamp:now});
              }
            } else {
              const d=await api('/api/tree/'+node.cid+'?sort='+sortVal);
              ch=d.children||d.items||[];
            }
            // 在线模式需要前端排序（115 API排序参数不生效）
            if(live && ch.length){
              const [key, asc] = sortVal.split('_');
              ch.sort((a,b)=>{
                let va, vb;
                if(key==='time'){
                  // 按cid排序（cid越大越新）
                  va = parseInt(a.cid)||0; vb = parseInt(b.cid)||0;
                } else {
                  // 按名称排序
                  va = (a.name||'').toLowerCase(); vb = (b.name||'').toLowerCase();
                }
                if(va < vb) return asc==='asc' ? -1 : 1;
                if(va > vb) return asc==='asc' ? 1 : -1;
                return 0;
              });
            }
            kids.innerHTML='';
            if(!ch.length){ kids.innerHTML='<div class="empty sub" style="padding:6px">空目录</div>'; }
            else{
              ch.forEach(c=>kids.appendChild(trow(c, depth+1, live)));
              /* 级联恢复：已在展开记录里的子目录自动展开 */
              kids.querySelectorAll(':scope > .twrap').forEach(w=>{
                if(S.expanded.has(w.dataset.cid)){
                  const t2=w.querySelector(':scope > .trow > .tw');
                  if(t2 && t2.textContent==='▶') t2.click();
                }
              });
            }
          }catch(e){
            kids.innerHTML=`<div class="sub" style="padding:6px;color:var(--danger)">${esc(e.message)}</div>`;
            wrap._built=false;
          }
        }
      } else {
        S.expanded.delete(node.cid);
        try{ localStorage.setItem('tree_expanded', JSON.stringify([...S.expanded])); }catch(_){}
      }
    }
    tw.onclick=toggle;
    nm.onclick=async()=>{ await toggle(); showDetail(node, live); };
    nm.oncontextmenu=e=>{ e.preventDefault(); showDetail(node, live); };
  } else {
    tw.textContent='·'; tw.className='tw leaf';
    nm.onclick=()=>showDetail(node, live);
  }
  return wrap;
}

async function buildTree(cid, name, live){
  const box=$('#treeBox');
  const sameRoot=S.treeCur && S.treeCur.cid===cid && S.treeCur.live===!!live;
  const scrollY=window.scrollY;
  box.innerHTML='';
  S.treeCur={cid,name:name||'',live:!!live};
  // 同步模式状态
  if(live && S.treeMode!=='live') switchTreeMode('live');
  else if(!live && S.treeMode!=='local') switchTreeMode('local');
  const head=document.createElement('div');
  head.style.cssText='display:flex;gap:10px;align-items:center;margin-bottom:8px';
  head.innerHTML=`<span class="sub">${live?'🌐 在线':'💾 本地'} · ${esc(name||cid)}</span>`;
  box.appendChild(head);
  const anchor={cid,is_dir:1,name:name||'根目录',child_count:null};
  const wrap=trow(anchor,-1,live);
  box.appendChild(wrap);
  wrap.querySelector(':scope > .trow > .tw').click();
  if(sameRoot) requestAnimationFrame(()=>window.scrollTo(0,scrollY));
}

async function initTree(){
  // 恢复展开状态
  try{
    const saved=JSON.parse(localStorage.getItem('tree_expanded')||'[]');
    saved.forEach(cid=>S.expanded.add(cid));
  }catch(_){}
  const items=await loadRoots();
  const box=$('#treeBox'); box.innerHTML='';
  S.treeCur=null;
  const h=document.createElement('div');
  h.style.cssText='display:flex;gap:10px;align-items:center;margin-bottom:8px';
  h.innerHTML=`<span class="sub">💾 本地 · 网盘根目录</span><span class="sub" style="margin-left:8px">（${items.length} 个文件夹）</span>`;
  box.appendChild(h);
  items.forEach(it=>box.appendChild(trow({...it, is_dir:1}, 0, false)));
  // 自动展开之前展开过的文件夹
  requestAnimationFrame(()=>{
    box.querySelectorAll(':scope > .twrap').forEach(w=>{
      if(S.expanded.has(w.dataset.cid)){
        const tw=w.querySelector(':scope > .trow > .tw');
        if(tw && tw.textContent==='▶') tw.click();
      }
    });
  });
}
async function initLiveTree(){
  // 恢复展开状态
  try{
    const saved=JSON.parse(localStorage.getItem('tree_expanded')||'[]');
    saved.forEach(cid=>S.expanded.add(cid));
  }catch(_){}
  const box=$('#treeBox'); box.innerHTML='';
  S.treeCur=null;
  let items=[];
  try{ const d=await api('/api/live/0'); items=d.children||d.items||[]; }catch(e){}
  const dirs=items.filter(i=>i.is_dir);
  const h=document.createElement('div');
  h.style.cssText='display:flex;gap:10px;align-items:center;margin-bottom:8px';
  h.innerHTML=`<span class="sub">🌐 在线 · 网盘根目录</span><span class="sub" style="margin-left:8px">（${dirs.length} 个文件夹）</span>`;
  box.appendChild(h);
  dirs.forEach(it=>box.appendChild(trow({...it, is_dir:1}, 0, true)));
  // 自动展开之前展开过的文件夹
  requestAnimationFrame(()=>{
    box.querySelectorAll(':scope > .twrap').forEach(w=>{
      if(S.expanded.has(w.dataset.cid)){
        const tw=w.querySelector(':scope > .trow > .tw');
        if(tw && tw.textContent==='▶') tw.click();
      }
    });
  });
}
function backLocal(){ initTree().catch(()=>{}); }

/* ---------- 模式切换（本地数据 / 在线浏览） ---------- */
function switchTreeMode(mode){
  if(mode===S.treeMode) return;
  S.treeMode=mode;
  // 切换按钮高亮
  document.querySelectorAll('[data-action="tree-mode"]').forEach(b=>
    b.classList.toggle('on', b.dataset.treeMode===mode));
  // 加载对应模式的内容
  if(mode==='local') initTree().catch(()=>{});
  else initLiveTree().catch(()=>{});
}

// 双击在线浏览按钮：清除缓存并刷新
document.querySelectorAll('[data-action="tree-mode"]').forEach(b=>{
  if(b.dataset.treeMode==='live'){
    b.addEventListener('dblclick', ()=>{
      S.liveCache.clear();
      toast('在线缓存已清除，正在刷新…');
      initLiveTree().catch(()=>{});
    });
  }
});

// 排序选择器：切换时重新加载当前目录，并保存到localStorage
document.getElementById('treeSort')?.addEventListener('change', (e)=>{
  localStorage.setItem('tree_sort', e.target.value);
  if(S.treeCur){
    buildTree(S.treeCur.cid, S.treeCur.name, S.treeCur.live);
  }
});
// 初始化时恢复上次选择的排序方式
const savedSort = localStorage.getItem('tree_sort');
if(savedSort && document.getElementById('treeSort')){
  document.getElementById('treeSort').value = savedSort;
}

/* ---------- 详情面板 ---------- */
function showDetail(node, live){
  S.detailNode={...node, live:!!live};
  const d=$('#detail'); d.style.display='block';
  let html=`<h3 style="word-break:break-all">${esc(node.name)}</h3>
    <div class="kv"><b>类型</b><span>${node.is_dir?'目录':'文件'}</span></div>
    <div class="kv"><b>ID</b><span class="mono">${esc(node.cid)}</span></div>
    <div class="kv"><b>大小</b><span>${node.is_dir?'—':fmt(node.size)}</span></div>
    <div class="kv" style="flex-wrap:wrap"><b>标签</b><span id="dTags" style="text-align:right">${(node.tags&&node.tags.length)?node.tags.map(chipHtml).join(' '):'<span class="sub">无</span>'}</span></div>`;
  const tagBtn=live?'':'<button data-action="d-tag">🏷 标签</button>';
  if(node.is_dir){
    html+=`<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="pri" data-action="d-scan">扫描此目录</button>
      <button data-action="d-rescan" title="清除该目录已扫记录，重新完整扫描一遍">强制重扫</button>
      <button data-action="d-mkdir">新建子文件夹</button>
      ${live?'':'<button data-action="d-live">在线浏览</button>'}${tagBtn}
    </div>`;
  } else {
    html+=`<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
      ${tagBtn}
    </div>`;
  }
  html+=`<div style="margin-top:14px;padding-top:10px;border-top:1px solid var(--line)">
    <button class="danger" data-action="d-del">删除（进回收站）</button>
    <div class="sub" style="margin-top:4px">删除会移入 115 回收站，30 天内可在网盘恢复</div></div>`;
  d.innerHTML=html;
}

/* ---------- 发起扫描 / 下载 / 复制直链 ---------- */
async function copyText(text){
  if(navigator.clipboard && window.isSecureContext){
    try{ await navigator.clipboard.writeText(text); return true; }catch(e){ /* 走回退 */ }
  }
  const ta=document.createElement('textarea');
  ta.value=text; ta.style.cssText='position:fixed;opacity:0';
  document.body.appendChild(ta); ta.select();
  let ok=false;
  try{ ok=document.execCommand('copy'); }catch(_){}
  ta.remove();
  return ok;
}
async function copyDownloadLink(cid){
  try{
    const d=await api('/api/fs/download/'+cid);
    const ok=await copyText(d.url);
    toast(ok?'✓ 下载直链已复制到剪贴板':'复制失败（可点“获取下载直链”手动复制）', !ok);
  }catch(e){ toast('获取直链失败: '+e.message,true); }
}
async function scanOne(cid,name,rescan){
  const force=!!rescan;
  try{
    await api('/api/scan/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cid,name,rescan:force})});
    toast(force?'已提交强制重扫任务（清旧记录）':'已提交扫描任务（增量续扫）');
    applyTab('scan'); syncHash();
    S.roots=null;
  }catch(e){ toast(e.message,true); }
}
async function mkdirIn(pid,parentName){
  const name=prompt(`在「${parentName}」下新建文件夹：`);
  if(!name||!name.trim()) return;
  try{
    await api('/api/fs/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name.trim(),pid})});
    toast('✓ 已创建「'+name.trim()+'」');
    S.roots=null;
    if(S.treeCur) buildTree(S.treeCur.cid, S.treeCur.name, S.treeCur.live);
    else initTree().catch(()=>{});
  }catch(e){ toast('创建失败: '+e.message,true); }
}
async function dlOne(cid, outEl){
  if(!outEl) outEl=$('#dlOut');
  if(!outEl) return;
  outEl.innerHTML='<span class="sub">获取中…</span>';
  try{
    const d=await api('/api/fs/download/'+cid);
    outEl.innerHTML='';
    const a=document.createElement('a');
    a.href=d.url; a.target='_blank'; a.rel='noopener';
    a.style.wordBreak='break-all';
    a.textContent=`点击下载 (${fmt(d.size||0)})`;
    outEl.appendChild(a);
  }catch(e){
    outEl.innerHTML='';
    const sp=document.createElement('span'); sp.style.color='var(--danger)'; sp.textContent=e.message;
    outEl.appendChild(sp);
  }
}
/* 单删：成功后原地从树中移除，同时移除搜索结果行 */
async function delOne(cid,pid,name){
  const ok=await confirmDanger({title:'确认删除', message:`确认删除「${name}」？`});
  if(!ok) return;
  try{
    await api('/api/fs/delete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cid,pid,confirm:true})});
    toast('已删除并移入回收站');
    $('#detail').style.display='none';
    removeTreeNodes([cid]);
    // 从搜索结果表格中移除该行及其展开行
    const srRow=document.querySelector(`#searchResults tr.sr-row[data-cid="${CSS.escape(cid)}"]`);
    if(srRow){
      const detail=srRow.nextElementSibling;
      if(detail&&detail.classList.contains('sr-detail')) detail.remove();
      srRow.remove();
    }
    // 从展开的子项列表中移除
    const childEl=document.querySelector(`#searchResults .sr-child[data-cid="${CSS.escape(cid)}"]`);
    if(childEl) childEl.remove();
    S.roots=null; loadHealth();
  }catch(e){ toast('删除失败: '+e.message,true); }
}

/* ---------- 树原地更新 ---------- */
function decParentCount(pid, n){
  if(!pid) return;
  const prow=document.querySelector(`#treeBox .twrap[data-cid="${CSS.escape(pid)}"] > .trow`);
  if(!prow) return;
  const meta=prow.querySelector('.tmeta');
  if(meta){ const m=meta.textContent.match(/^(\d+)\s+项$/);
    if(m) meta.textContent=Math.max(0,+m[1]-n)+' 项'; }
}
/** 从当前树视图原地移除一组节点（含父计数回减与详情面板清理） */
function removeTreeNodes(cids){
  const set=new Set(cids.map(String));
  set.forEach(cid=>{
    const w=document.querySelector(`#treeBox .twrap[data-cid="${CSS.escape(cid)}"]`);
    if(!w) return;
    const pid=w.dataset.pid;
    w.remove();
    decParentCount(pid,1);
  });
  if(S.detailNode && set.has(String(S.detailNode.cid))) $('#detail').style.display='none';
  if(S.treeCur && set.has(String(S.treeCur.cid))) initTree().catch(()=>{});
}
/** 目标目录若正在树中展开，重拉其子节点 */
function refreshTreeNode(cid){
  const w=document.querySelector(`#treeBox .twrap[data-cid="${CSS.escape(cid)}"]`);
  if(!w) return;
  const kids=w.querySelector(':scope > .tkids');
  const tw=w.querySelector(':scope > .trow > .tw');
  if(kids && kids.classList.contains('open') && tw){
    kids.innerHTML=''; w._built=false; tw.click();
  }
}

/* ---------- 勾选 / 批量 ---------- */
function toggleSel(node, on){
  if(on) S.sel.set(node.cid,{cid:node.cid,pid:node.pid||'',name:node.name,is_dir:!!node.is_dir,size:node.size||0});
  else S.sel.delete(node.cid);
  updateBatchBar();
}
function updateBatchBar(){
  const bar=$('#batchBar');
  if(!S.sel.size){ bar.style.display='none'; $('#bbPop').style.display='none'; return; }
  bar.style.display='flex';
  const nDir=[...S.sel.values()].filter(i=>i.is_dir).length;
  $('#bbInfo').textContent=`已选 ${S.sel.size} 项（${nDir} 目录 / ${S.sel.size-nDir} 文件）`;
}
function renderBBPop(){
  const p=$('#bbPop');
  if(p.style.display!=='block') return;
  const items=[...S.sel.values()];
  if(!items.length){
    p.innerHTML='<div class="empty sub" style="padding:14px">未勾选任何项</div>'; return;
  }
  p.innerHTML=items.map(i=>`<div class="bp-row">
      <span>${i.is_dir?'📁':'📄'}</span>
      <span class="bp-name" title="${esc(i.name)}">${esc(i.name)}</span>
      <span class="sub">${i.is_dir?'目录':fmt(i.size)}</span>
      <span class="bp-x" data-action="bb-remove" data-cid="${esc(i.cid)}" title="移除">✕</span>
    </div>`).join('')
    +`<div class="bp-foot"><button data-action="bb-clear">清空全部</button></div>`;
}
function clearSel(){
  S.sel.clear();
  $$('.ckrow').forEach(c=>c.checked=false);
  updateBatchBar();
}
async function batchDel(){
  const items=[...S.sel.values()];
  if(!items.length) return;
  const ok=await confirmDanger({title:`删除选中的 ${items.length} 项`,
    message:'将移入 115 回收站（30天内可恢复）',
    items:items.map(i=>({name:i.name, sub:i.is_dir?'目录':fmt(i.size)})), searchable:true});
  if(!ok) return;
  toast('正在删除 '+items.length+' 项…');
  try{
    const r=await api('/api/fs/delete/batch',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items,confirm:true})});
    toast(r.fail?`删除完成: 成功 ${r.success} / 失败 ${r.fail}`:`已删除 ${r.success} 个并移入回收站`, !!r.fail);
    const okCids=(r.results||[]).filter(x=>x.ok).map(x=>x.cid);
    removeTreeNodes(okCids);
    clearSel(); S.roots=null; loadHealth(); refreshTagNodesIfOpen();
  }catch(e){ toast('批量删除失败: '+e.message,true); }
}

/* ---------- 搜索页快捷键 ---------- */
$('#searchQ').addEventListener('keydown', e=>{
  if(e.key==='Enter'){ e.preventDefault(); doSearch(); }
});

/* ============ 7. 搜索 ============ */
let searchHistCache=[];
/* 搜索范围：按钮 + 公共目录选择器（原来是一级目录原生下拉） */
function setSearchRoot(cid, name){
  cid = cid||'';
  const inp=$('#searchRoot'); if(inp) inp.value=cid;
  const btn=$('#searchRootBtn'); if(!btn) return;
  if(!cid){
    btn.textContent='📂 全部根目录'; btn.title='全部根目录';
    return;
  }
  const nm = name || (S.roots||[]).find(x=>x.cid===cid)?.name || cid;
  btn.textContent='📂 '+nm; btn.title=nm;
  /* 只有 cid 没名字时，异步补全真实目录名（例如从地址栏恢复） */
  if(!name && !(S.roots||[]).some(x=>x.cid===cid)){
    api('/api/node/'+cid).then(d=>{
      if(d && d.exists && d.name && d.name!==cid){ btn.textContent='📂 '+d.name; btn.title=d.name; }
    }).catch(()=>{});
  }
}
async function searchRootPick(){
  const cur=$('#searchRoot').value;
  const sel = await pickDir({
    title:'选择搜索范围',
    cid: cur,
    name: cur ? ($('#searchRootBtn').textContent||'').replace('📂 ','').trim() : '',
    persistKey:'search_root',
    allowAll:true, allLabel:'全部根目录',
  });
  if(!sel) return;
  setSearchRoot(sel.cid, sel.name);
}
async function loadSearchPage(params){
  try{ await loadRoots(); }catch(e){}
  if(params){
    if(params.has('q'))    $('#searchQ').value=params.get('q');
    if(params.has('type')) $('#searchType').value=params.get('type');
    if(params.has('root')) setSearchRoot(params.get('root'));
    if(params.has('min'))  $('#searchMinMB').value=params.get('min');
    if(params.has('max'))  $('#searchMaxMB').value=params.get('max');
    if(params.get('q')) doSearch();
  }
  loadHistory();
  $('#searchQ').focus();
}
function renderSearchHistory(){
  const el=$('#searchHistory');
  const bar=document.getElementById('histBar');
  if(!searchHistCache.length){
    if(bar) bar.style.display='none';
    el.innerHTML='';
    return;
  }
  if(bar) bar.style.display='flex';
  const now=Math.floor(Date.now()/1000);
  el.innerHTML=searchHistCache.slice(0,20).map((h,i)=>{
    let ago=''; const dt=now-h.ts;
    if(dt<60) ago='刚刚';
    else if(dt<3600) ago=Math.floor(dt/60)+' 分钟前';
    else if(dt<86400) ago=Math.floor(dt/3600)+' 小时前';
    else ago=Math.floor(dt/86400)+' 天前';
    const star=h.n>=3?' ⭐':'';
    const folder=h.scope?' 📂':'';
    const tip=`${h.scope?h.scope+' | ':''}${h.q} · ${ago} · ${h.n} 条结果 · 点击重新搜索`;
    return `<span class="hist-chip" data-action="hist-run" data-idx="${i}" title="${esc(tip)}"><span class="hq">${esc(h.q)}</span>${star?'<span>'+star+'</span>':''}${folder?'<span>'+folder+'</span>':''}<span class="hn">×${h.n}</span></span>`;
  }).join('');
}
async function loadHistory(){
  try{
    const d=await api('/api/search/history');
    searchHistCache=d.items||[];
  }catch(e){
    searchHistCache=JSON.parse(localStorage.getItem('m115_search_history')||'[]');
  }
  renderSearchHistory();
}
function runHistory(i){
  const h=searchHistCache[i]; if(!h) return;
  $('#searchQ').value=h.q;
  if(h.scope) setSearchRoot(h.scope);
  doSearch();
}
async function clearHistory(){
  if(!await appConfirm({title:'清空搜索历史', message:'清空所有设备上的搜索历史？', danger:true, okText:'清空'})) return;
  try{ await api('/api/search/history',{method:'DELETE'}); }
  catch(e){ toast('清空失败: '+e.message,true); return; }
  searchHistCache=[];
  localStorage.removeItem('m115_search_history');
  renderSearchHistory();
  toast('已清空');
}
function pushHistory(q, count, scope){
  const item={q, scope:scope||'', n:count||0, ts:Math.floor(Date.now()/1000)};
  searchHistCache=[item,...searchHistCache.filter(h=>!(h.q===q&&h.scope===item.scope))].slice(0,100);
  renderSearchHistory();
  api('/api/search/history',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({q,scope:scope||'',n:count||0})}).catch(()=>{});
}
async function doSearch(){
  const q=$('#searchQ').value.trim();
  if(!q){ toast('请输入关键字',true); return; }
  if(q.length<2){ toast('至少 2 个字',true); return; }
  const params=new URLSearchParams({q});
  const type=$('#searchType').value;
  if(type!=='all') params.set('type',type);
  const root=$('#searchRoot').value;
  if(root) params.set('root',root);
  const minMB=parseFloat($('#searchMinMB').value||0);
  if(minMB>0) params.set('min',minMB);
  const maxMB=parseFloat($('#searchMaxMB').value||0);
  if(maxMB>0) params.set('max',maxMB);
  $('#searchSummary').textContent='搜索中…';
  $('#searchResults').innerHTML='<div class="empty sub">加载中…</div>';
  try{
    const apiParams=new URLSearchParams({q});
    if(type!=='all') apiParams.set('type',type);
    if(root) apiParams.set('root',root);
    if(minMB>0) apiParams.set('min_size',Math.round(minMB*1024*1024));
    if(maxMB>0) apiParams.set('max_size',Math.round(maxMB*1024*1024));
    const d=await api('/api/search?'+apiParams.toString());
    // 去重：若文件位于某个结果目录的子树内（直接父级或更深层），只显示目录（展开可见）
    const dirCids=new Set(d.rows.filter(r=>r.is_dir).map(r=>r.cid));
    const dirPaths=d.rows.filter(r=>r.is_dir).map(r=>r.path).filter(Boolean);
    const filtered=d.rows.filter(r=>{
      if(r.is_dir) return true;
      if(dirCids.has(r.pid)) return false;                       // 直接父目录在结果中
      const p=r.path||'';
      for(const dp of dirPaths){                                 // 更深层：路径前缀匹配
        if(p.startsWith(dp+' / ')) return false;
      }
      return true;
    });
    $('#searchSummary').textContent=`找到 ${filtered.length} 条${d.total>=300?'（已截断到 300 条）':''}${filtered.length<d.rows.length?`（隐藏 ${d.rows.length-filtered.length} 条子项）`:''}`;
    pushHistory(q,d.rows.length,root);
    syncHash(params);
    renderSearchResults(filtered);
  }catch(e){
    $('#searchSummary').textContent='搜索失败: '+e.message;
    $('#searchResults').innerHTML='<div class="empty">无结果</div>';
  }
}
function renderSearchResults(rows){
  const el=$('#searchResults');
  if(!rows.length){ el.innerHTML='<div class="empty">无匹配结果</div>'; return; }
  el.innerHTML='<table><thead><tr><th style="width:50px">类型</th><th style="width:24%">名称</th><th>路径</th><th style="width:90px">大小</th><th style="width:180px">操作</th></tr></thead><tbody>'+
    rows.map(r=>{
      const ops=(r.is_dir
        ?`<button data-action="scan-one" data-cid="${esc(r.cid)}" data-name="${esc(r.name)}">扫描</button>`
        :'')
        +`<button data-action="open-in-tree" data-cid="${esc(r.is_dir?r.cid:(r.pid||r.cid))}">定位</button>`;
      const detail=`<tr class="sr-detail" data-pid="${esc(r.cid)}"><td colspan="5">
        <div class="sr-label">文件名</div><div class="sr-val">${esc(r.name)}</div>
        <div class="sr-label" style="margin-top:6px">完整路径</div><div class="sr-val">📂 ${esc(r.path)}</div>
        ${r.is_dir?'<div class="sr-children"><div class="sub" style="padding:4px 0">点击展开子项…</div></div>':''}
      </td></tr>`;
      return `<tr class="sr-row${r.is_dir?' sr-dir':''}" data-cid="${esc(r.cid)}" data-is-dir="${r.is_dir?1:0}" data-pid="${esc(r.pid||'')}" data-name="${esc(r.name)}">
        <td>${r.is_dir?'📁':'📄'}</td>
        <td><b>${esc(r.name)}</b></td>
        <td class="sub" title="${esc(r.path)}">${esc(r.path)}</td>
        <td class="sub">${r.is_dir?'—':fmt(r.size)}</td>
        <td>${ops}
          <button data-action="search-move-one" data-cid="${esc(r.cid)}" data-pid="${esc(r.pid)}" data-name="${esc(r.name)}">转移</button>
          <button class="danger" data-action="search-del-one" data-cid="${esc(r.cid)}" data-pid="${esc(r.pid)}" data-name="${esc(r.name)}">删</button></td>
      </tr>`+detail;
    }).join('')+'</tbody></table>';
}
function searchMoveOne(cid,pid,name){
  S.sel.clear();
  S.sel.set(cid,{cid,pid,name,is_dir:true,size:0});
  openMoveDlg();
}
/* 搜索结果行点击展开/收起 + 目录子项加载 */
$('#searchResults').addEventListener('click', async e=>{
  // 子项点击：递归展开子目录
  const childEl=e.target.closest('.sr-child');
  if(childEl&&!e.target.closest('button')&&!e.target.closest('a')){
    const depth=+(childEl.dataset.depth||0);
    if(depth>=5) return toast('最多展开5层',true);
    if(childEl.dataset.isDir!=='1') return;
    const cid=childEl.dataset.cid;
    const box=childEl.querySelector('.sr-child-sub');
    if(!box) return;
    // 已加载过：切换显隐
    if(box.dataset.loaded){
      const show=box.style.display==='none'||!box.style.display;
      box.style.display=show?'block':'none';
      childEl.classList.toggle('sr-open', show);
      return;
    }
    // 首次展开：加载子项
    await loadSearchChildren(box, cid, depth+1);
    box.style.display='block';
    childEl.classList.add('sr-open');
    return;
  }
  // 按钮/链接：不处理
  if(e.target.closest('button')||e.target.closest('a')) return;
  // 行点击：展开/收起 detail
  const row=e.target.closest('.sr-row');
  if(!row) return;
  const detail=row.nextElementSibling;
  if(!detail||!detail.classList.contains('sr-detail')) return;
  const isOpen=detail.style.display==='table-row';
  detail.style.display=isOpen?'none':'table-row';
  row.classList.toggle('sr-open', !isOpen);
  // 目录首次展开时加载子项
  if(!isOpen&&row.dataset.isDir==='1'){
    const childrenBox=detail.querySelector('.sr-children');
    if(childrenBox&&!childrenBox.dataset.loaded){
      await loadSearchChildren(childrenBox, row.dataset.cid, 0);
    }
  }
});
async function loadSearchChildren(container, cid, depth){
  container.innerHTML='<div class="sub" style="padding:4px 0">加载中…</div>';
  try{
    const d=await api('/api/tree/'+cid);
    const kids=d.children||[];
    if(!kids.length){ container.innerHTML='<div class="sub" style="padding:4px 0">空目录</div>'; return; }
    container.dataset.loaded='1';
    container.innerHTML=kids.map(k=>{
      const icon=k.is_dir?'📁':'📄';
      const meta=k.is_dir?`${k.child_count} 项`:fmt(k.size);
      const indent='margin-left:'+(depth*18)+'px';
      return `<div class="sr-child" data-cid="${esc(k.cid)}" data-pid="${esc(k.pid)}" data-name="${esc(k.name)}" data-is-dir="${k.is_dir}" data-depth="${depth}" style="${indent}">
        <span class="sr-icon">${icon}</span>
        <span class="sr-name">${esc(k.name)}</span>
        <span class="sr-meta sub">${meta}</span>
        ${k.is_dir?'<span class="sr-expand-arrow sub">▶</span>':''}
        <button data-action="search-move-one" data-cid="${esc(k.cid)}" data-pid="${esc(k.pid)}" data-name="${esc(k.name)}">转移</button>
        <button class="danger" data-action="search-del-one" data-cid="${esc(k.cid)}" data-pid="${esc(k.pid)}" data-name="${esc(k.name)}">删</button>
        ${k.is_dir?`<div class="sr-child-sub"></div>`:''}
      </div>`;
    }).join('');
  }catch(err){
    container.innerHTML='<div class="sub" style="padding:4px 0;color:var(--danger)">'+esc(err.message)+'</div>';
  }
}
function openInTree(cid){
  applyTab('tree'); syncHash();
  setTimeout(()=>buildTree(cid,'',false),50);
}

/* ============ 8. 扫描管理 ============ */
/* 扫描目标目录：按钮 + 公共目录选择器（与 转存/查重/AI 一致） */
function scanTargetEls(which){
  return which==='scan'
    ? {btn:'#scanRootBtn', inp:'#scanRootSel', key:'scan_target'}
    : {btn:'#schRootBtn',  inp:'#schRootSel',  key:'sch_target'};
}
function setScanTarget(which, cid, name){
  const {btn,inp,key}=scanTargetEls(which);
  cid = cid||''; name = name||cid;
  const be=$(btn); const ie=$(inp);
  if(ie) ie.value = cid;
  if(be){
    be.textContent = cid ? ('📂 '+name) : '📂 点击选择目录…';
    be.title = cid ? name : '点击选择目录…';
  }
}
/* 读取按钮上的真实目录名（去掉图标） */
function scanTargetName(which){
  const {btn}=scanTargetEls(which);
  const be=$(btn);
  return ((be && be.textContent)||'').replace('📂 ','').trim();
}
async function scanTargetPick(which){
  const {inp,key}=scanTargetEls(which);
  const cur=$(inp) ? $(inp).value : '';
  const sel = await pickDir({
    title: which==='scan' ? '选择要扫描的目录' : '选择定时扫描的目录',
    cid: cur,
    name: cur ? scanTargetName(which) : '',
    persistKey: key,
  });
  if(!sel) return;
  setScanTarget(which, sel.cid, sel.name);
  localStorage.setItem(key+'_cid', sel.cid);
  localStorage.setItem(key+'_name', sel.name);
}
async function scanRootPick(){ return scanTargetPick('scan'); }
async function schRootPick(){ return scanTargetPick('sch'); }
/* 恢复上次选择的扫描目标 */
function restoreScanTargets(){
  ['scan','sch'].forEach(which=>{
    const {inp,key}=scanTargetEls(which);
    if($(inp) && $(inp).value) return;       // 已有选择则不覆盖
    const cid=localStorage.getItem(key+'_cid');
    if(cid) setScanTarget(which, cid, localStorage.getItem(key+'_name')||cid);
  });
}
async function loadScanPage(){
  try{ await loadRoots(); }catch(e){}
  restoreScanTargets();
  refreshJobs(); refreshSched();
}
async function startScan(){
  const cid=$('#scanRootSel').value; if(!cid) return toast('先选择目录',true);
  const name=scanTargetName('scan')||cid;
  try{
    const d=await api('/api/scan/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cid,name,rescan:$('#scanRescan').checked})});
    toast('已提交, 任务 #'+d.job_id);
    refreshJobs(); S.roots=null;
  }catch(e){ toast(e.message,true); }
}
async function refreshJobs(){
  let d;
  try{ d=await api('/api/scan/jobs'); }catch(e){ return; }
  const el=$('#scanJobs');
  if(!d.jobs.length){
    el.innerHTML='<div class="empty">暂无任务</div>'; $('#scanHint').textContent=''; return;
  }
  /* 原位更新：按 job id diff，避免每 4 秒整表重渲染 */
  if(el.querySelector('.empty')) el.innerHTML='';
  let table=el.querySelector('table');
  if(!table){
    el.innerHTML='<table><thead><tr><th>#</th><th>目录</th><th>状态</th><th style="width:30%">进度</th><th>开始</th><th></th></tr></thead><tbody></tbody></table>';
    table=el.querySelector('table');
  }
  const tbody=table.tBodies[0];
  const existing={};
  tbody.querySelectorAll('tr[data-jid]').forEach(tr=>existing[tr.dataset.jid]=tr);
  let prev=null; const seen=new Set();
  for(const j of d.jobs){
    const key=String(j.id); seen.add(key);
    let tr=existing[key];
    if(!tr){
      tr=document.createElement('tr'); tr.dataset.jid=key;
      tr.innerHTML=`<td>${j.id}</td><td class="j-name"></td><td class="j-status"></td>
        <td><div class="row" style="flex-wrap:nowrap"><div class="progress"><div></div></div><span class="sub j-prog"></span></div>
        <div class="sub j-cur" style="font-size:11px;margin-top:2px;color:var(--accent)"></div></td>
        <td class="sub j-start"></td><td class="j-act"></td>`;
    }
    const pct=j.total?Math.round(j.done_count/j.total*100):0;
    tr.querySelector('.j-name').textContent=j.target_name.slice(0,40);
    tr.querySelector('.j-name').title=j.target_name;
    tr.querySelector('.j-status').innerHTML=statusBadge(j.status);
    tr.querySelector('.progress>div').style.width=pct+'%';
    tr.querySelector('.j-prog').textContent=j.status==='retry_wait'
      ?`${j.done_count}/${j.total||'?'} · 第 ${j.retry_count||0}/3 轮`
      :`${j.done_count}/${j.total||'?'}`;
    tr.querySelector('.j-start').textContent=j.started_at||'—';
    /* 运行中的任务显示「当前扫到哪个子目录」——扫大目录时唯一能看出进度位置的地方。
       可见文本裁剪，title 保留完整路径 */
    const cur=tr.querySelector('.j-cur');
    if(cur){
      const cp=(j.status==='running'||j.status==='retry_wait')?(j.current_path||''):'';
      cur.textContent=cp?('📂 '+shortPath(cp,3)):'';
      cur.title=cp;
    }
    let act='';
    if(j.status==='running'||j.status==='queued'||j.status==='retry_wait'){
      act=`<button data-action="job-stop" data-id="${j.id}">停止</button>
           <button data-action="job-pause" data-id="${j.id}" style="padding:2px 10px;font-size:12px">暂停</button>`;
    } else if(j.status==='error'||j.status==='stopped'||j.status==='paused'){
      act=`<button class="pri" data-action="job-resume" data-id="${j.id}" style="padding:2px 10px;font-size:12px">继续</button>
           <button class="danger" data-action="job-del" data-id="${j.id}" style="padding:2px 10px;font-size:12px">删除</button>`;
    } else {
      act=`<button class="danger" data-action="job-del" data-id="${j.id}" style="padding:2px 10px;font-size:12px">删除</button>`;
    }
    tr.querySelector('.j-act').innerHTML=act;
    if(!prev){ if(tbody.firstElementChild!==tr) tbody.prepend(tr); }
    else if(prev.nextElementSibling!==tr) prev.after(tr);
    prev=tr;
  }
  for(const k in existing) if(!seen.has(k)) existing[k].remove();
  const run=d.jobs.find(j=>j.status==='running');
  $('#scanHint').textContent=run
    ?`运行中: ${run.target_name.slice(0,30)} (${run.done_count}/${run.total})`
      +(run.current_path?` · 当前: ${shortPath(run.current_path,3)}`:'')
    :'当前无运行任务';
  $('#scanHint').title=(run&&run.current_path)?run.current_path:'';
}
async function stopJob(id){
  try{ await api('/api/scan/stop/'+id,{method:'POST'}); toast('已请求停止'); }catch(e){ toast(e.message,true); }
}
async function pauseJob(id){
  try{
    await api('/api/scan/pause/'+id,{method:'POST'});
    toast('任务 #'+id+' 已暂停，可随时继续');
    refreshJobs();
  }catch(e){ toast(e.message,true); }
}
async function resumeJob(id){
  try{
    await api('/api/scan/resume/'+id,{method:'POST'});
    toast('任务 #'+id+' 已重新入队，将从断点续扫');
    refreshJobs();
  }catch(e){ toast(e.message,true); }
}
async function deleteJob(id){
  if(!await appConfirm({title:'删除扫描任务', message:`删除任务 #${id} 的记录？\n只删除任务记录，已扫描的目录数据会保留。`, danger:true, okText:'删除'})) return;
  try{
    await api('/api/scan/jobs/'+id,{method:'DELETE'});
    toast('任务 #'+id+' 已删除');
    refreshJobs();
  }catch(e){ toast(e.message,true); }
}
async function refreshSched(){
  try{
    const d=await api('/api/schedules');
    const el=$('#schedList');
    if(!d.schedules.length){ el.innerHTML='<div class="empty sub" style="padding:10px">暂无定时任务</div>'; return; }
    el.innerHTML='<table><thead><tr><th>目录</th><th>每天时间</th><th>上次触发</th><th></th></tr></thead><tbody>'+
      d.schedules.map(s=>`<tr><td>${esc(s.target_name)}</td>
      <td class="mono">${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')}</td>
      <td class="sub">${esc(s.last_run||'—')}</td>
      <td><button class="danger" data-action="sched-del" data-id="${s.id}">删除</button></td></tr>`).join('')+'</tbody></table>';
  }catch(e){}
}
async function delSched(id){
  if(!await appConfirm({title:'删除定时扫描', message:'删除该定时扫描？', danger:true, okText:'删除'})) return;
  try{ await api('/api/schedules/'+id,{method:'DELETE'}); refreshSched(); }catch(e){ toast(e.message,true); }
}
async function addSched(){
  const cid=$('#schRootSel').value; if(!cid) return toast('先选择目录',true);
  const name=scanTargetName('sch')||cid;
  const [h,m]=($('#schTime').value||'03:00').split(':').map(Number);
  try{
    await api('/api/schedules',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cid,name,hour:h,minute:m})});
    toast('定时任务已添加'); refreshSched();
  }catch(e){ toast(e.message,true); }
}

/* ============ 9. 自动转存 ============ */
const goBtn=()=>document.querySelector('[data-action="transfer-go"]');
async function loadTransferPage(){
  try{ await loadRoots(); }catch(e){}
  refreshTasks();
  // 恢复上次选择的目标目录
  const savedCid = localStorage.getItem('tf_target_cid');
  const savedName = localStorage.getItem('tf_target_name');
  if(savedCid){
    document.getElementById('tfTargetCid').value = savedCid;
    document.getElementById('tfTargetBtn').textContent = '📂 ' + (savedName || savedCid);
  }
}

/* ============ 公共目录选择器 pickDir ============
 * 全站统一的树形目录选择弹窗：本地数据 / 在线浏览 + 排序 + 展开记忆 + 可选过滤/搜本地库。
 * 用法: const sel = await pickDir({title, cid, name, persistKey, allowAll, allLabel, withFilter});
 * 返回: {cid, name, mode, exists, scan_state}；用户取消返回 null。
 * 说明: persistKey 决定记忆键名，展开记忆存 <persistKey>_expanded、排序存 <persistKey>_sort、
 *       上次模式存 <persistKey>_mode —— 各处选择器互不干扰。
 */
function pickDir(opts){
  opts = opts || {};
  const pkey      = opts.persistKey || 'pickdir';
  const allowAll  = !!opts.allowAll;
  const withFilter= !!opts.withFilter;
  const allLabel  = opts.allLabel || '全部（默认）';
  /* allowAll 那一项代表的 cid：查重是 ''（不限定范围），AI 移动计划的父目录是 '0'（网盘根） */
  const allCid    = opts.allCid !== undefined ? opts.allCid : '';
  const dlgId     = opts.dlgId || 'pickDirDlg';

  return new Promise(resolve=>{
    const stale=document.getElementById(dlgId); if(stale) stale.remove();

    let picked = {cid: opts.cid||'', name: opts.name|| (allowAll && (opts.cid||'')===allCid ? allLabel : '')};
    let currentMode = 'local';
    try{ currentMode = localStorage.getItem(pkey+'_mode') || 'local'; }catch(_){}

    const expandedSet = new Set();
    try{ (JSON.parse(localStorage.getItem(pkey+'_expanded')||'[]')||[]).forEach(c=>expandedSet.add(c)); }catch(_){}
    const saveExpanded = ()=>{ try{ localStorage.setItem(pkey+'_expanded', JSON.stringify([...expandedSet])); }catch(_){} };

    const bg = document.createElement('div');
    bg.id = dlgId; bg.className = 'modal-bg'; bg.style.zIndex = 70;
    bg.innerHTML = `<div class="modal" style="max-width:520px">
      <h3><span>${esc(opts.title||'选择目录')}</span><span class="close" data-pd="close">×</span></h3>
      <div class="row" style="gap:6px;margin-bottom:6px;align-items:center">
        <button class="lg-chip" data-pd-mode="local">💾 本地数据</button>
        <button class="lg-chip" data-pd-mode="live">🌐 在线浏览</button>
        <span style="flex:1"></span>
        <select data-pd="sort" style="min-width:120px;font-size:12px">
          <option value="time_desc">⏰ 时间(最新)</option>
          <option value="time_asc">⏰ 时间(最早)</option>
          <option value="name_asc">🔤 名称 A→Z</option>
          <option value="name_desc">🔤 名称 Z→A</option>
        </select>
      </div>
      ${withFilter?`<div class="row" style="gap:6px;margin-bottom:6px">
        <input data-pd="filter" placeholder="过滤已加载目录名…" style="flex:1;min-width:170px">
        <button data-pd="search">搜本地库</button>
      </div>
      <div data-pd="searchout"></div>`:''}
      ${allowAll?`<div class="mt-row" data-pd="all"><span class="tw">🗂</span><span>${esc(allLabel)}</span></div>`:''}
      <div data-pd="tree" style="max-height:400px;overflow-y:auto;border:1px solid var(--line);border-radius:6px;padding:4px"></div>
      <div style="margin-top:10px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="sub">当前：</span><b data-pd="cur" style="color:var(--accent)">${esc(picked.name||'未选择')}</b>
        <span style="flex:1"></span>
        <button data-pd="ok" class="pri">确定</button>
        <button data-pd="cancel">取消</button>
      </div>
    </div>`;
    bg.onclick = e => { if(e.target===bg) done(null); };
    document.body.appendChild(bg);

    const q     = s => bg.querySelector('[data-pd="'+s+'"]');
    const tree  = q('tree');
    const curEl = q('cur');
    const sortEl= q('sort');

    try{ if(sortEl) sortEl.value = localStorage.getItem(pkey+'_sort') || 'time_desc'; }catch(_){}

    function done(val){
      document.removeEventListener('keydown', onEsc, true);
      bg.remove();
      resolve(val);
    }
    function onEsc(e){ if(e.key==='Escape') done(null); }
    document.addEventListener('keydown', onEsc, true);

    function markPicked(cid, name){
      picked = {cid:cid, name:name};
      if(curEl) curEl.textContent = name || '未选择';
      bg.querySelectorAll('.mt-row').forEach(r=>r.classList.remove('mt-sel'));
      if(cid){
        const row = tree.querySelector('.mt-row[data-cid="'+CSS.escape(cid)+'"]');
        if(row) row.classList.add('mt-sel');
      }
    }

    function sortItems(items){
      const v = (sortEl && sortEl.value) || 'time_desc';
      const [key, asc] = v.split('_');
      return items.sort((a,b)=>{
        let va, vb;
        if(key==='time'){ va = parseInt(a.cid)||0; vb = parseInt(b.cid)||0; }
        else { va = (a.name||'').toLowerCase(); vb = (b.name||'').toLowerCase(); }
        if(va<vb) return asc==='asc' ? -1 : 1;
        if(va>vb) return asc==='asc' ?  1 : -1;
        return 0;
      });
    }

    function pdRow(node, depth){
      const wrap = document.createElement('div');
      wrap.className = 'twrap'; wrap.dataset.cid = node.cid;
      const row = document.createElement('div');
      row.className = 'mt-row'; row.dataset.cid = node.cid;
      row.style.paddingLeft = (depth*16)+'px';
      if(picked.cid === node.cid) row.classList.add('mt-sel');
      const tw = document.createElement('span'); tw.className='tw'; tw.textContent = node.is_dir?'▶':'·';
      const nm = document.createElement('span'); nm.textContent = node.name; nm.title = node.name;
      if(node.is_dir) nm.className = 'tname dir';
      const meta = document.createElement('span'); meta.className='sub';
      meta.style.cssText = 'margin-left:6px;font-size:11px';
      meta.textContent = node.is_dir ? (node.child_count!=null ? node.child_count+' 项' : '') : '';
      row.appendChild(tw); row.appendChild(nm); row.appendChild(meta);
      const kids = document.createElement('div'); kids.style.display='none';
      if(node.is_dir){
        row.style.cursor = 'pointer';
        let built = false;
        row.onclick = async e=>{
          e.stopPropagation();
          const hidden = (kids.style.display==='none' || kids.style.display==='');
          if(hidden){
            kids.style.display='block'; tw.textContent='▼';
            expandedSet.add(node.cid); saveExpanded();
            if(!built){
              kids.innerHTML = '<div class="sub" style="padding:4px 8px">加载中…</div>';
              const sv = (sortEl && sortEl.value) || 'time_desc';
              let ch = [];
              if(currentMode==='live'){
                try{ const d = await api('/api/live/'+node.cid); ch = d.children||d.items||[]; }catch(_){}
                if(ch.length) sortItems(ch);
              } else {
                try{ const d = await api('/api/tree/'+node.cid+'?sort='+sv); ch = d.children||d.items||[]; }catch(_){}
              }
              kids.innerHTML='';
              const dirs = ch.filter(c=>c.is_dir);
              if(!dirs.length){
                kids.innerHTML='<div class="sub" style="padding:4px 8px">无子文件夹</div>';
              } else {
                built = true;
                dirs.forEach(c=>kids.appendChild(pdRow(c, depth+1)));
                kids.querySelectorAll(':scope > .twrap').forEach(w=>{
                  if(expandedSet.has(w.dataset.cid)){
                    const t2 = w.querySelector(':scope > .mt-row > .tw');
                    if(t2 && t2.textContent==='▶') t2.click();
                  }
                });
                applyFilter();
              }
            }
          } else {
            kids.style.display='none'; tw.textContent='▶';
            expandedSet.delete(node.cid); saveExpanded();
          }
          markPicked(node.cid, node.name);
        };
      }
      wrap.appendChild(row); wrap.appendChild(kids);
      return wrap;
    }

    function applyFilter(){
      if(!withFilter) return;
      const fi = q('filter'); if(!fi) return;
      const s = fi.value.trim().toLowerCase();
      const rows = tree.querySelectorAll('.mt-row');
      if(!s){ rows.forEach(r=>r.classList.remove('mv-hide')); return; }
      rows.forEach(r=>r.classList.add('mv-hide'));
      rows.forEach(r=>{
        if(r.textContent.toLowerCase().includes(s)){
          r.classList.remove('mv-hide');
          let el = r.parentElement;
          while(el && el!==tree){
            const prev = el.previousElementSibling;
            if(prev && prev.classList && prev.classList.contains('mt-row')) prev.classList.remove('mv-hide');
            el = el.parentElement;
          }
        }
      });
    }

    function restoreExpanded(){
      let opened = false;
      tree.querySelectorAll('.twrap').forEach(w=>{
        if(expandedSet.has(w.dataset.cid)){
          const tw = w.querySelector(':scope > .mt-row > .tw');
          if(tw && tw.textContent==='▶'){ tw.click(); opened = true; }
        }
      });
      if(opened) setTimeout(restoreExpanded, 300);
    }

    async function buildTree(mode){
      currentMode = mode;
      try{ localStorage.setItem(pkey+'_mode', mode); }catch(_){}
      bg.querySelectorAll('[data-pd-mode]').forEach(b=>b.classList.toggle('on', b.dataset.pdMode===mode));
      tree.innerHTML = '<div class="sub" style="padding:8px">加载中…</div>';
      try{
        if(mode==='live'){
          const d = await api('/api/roots');
          tree.innerHTML='';
          (d.items||[]).filter(r=>r.is_dir).forEach(r=>tree.appendChild(pdRow({...r, is_dir:1}, 0)));
        } else {
          try{ await loadRoots(); }catch(_){}
          tree.innerHTML='';
          const roots = S.roots || [];
          if(!roots.length){ tree.innerHTML='<div class="sub" style="padding:8px">暂无本地数据的根目录</div>'; return; }
          roots.forEach(r=>tree.appendChild(pdRow({...r, is_dir:1}, 0)));
        }
        applyFilter();
        restoreExpanded();
      }catch(e){
        tree.innerHTML = '<div class="sub" style="padding:8px;color:var(--danger)">加载失败: '+esc(e.message)+'</div>';
      }
    }

    bg.querySelectorAll('[data-pd-mode]').forEach(b=>{
      b.onclick = ()=>buildTree(b.dataset.pdMode);
    });
    if(sortEl) sortEl.addEventListener('change', ()=>{
      try{ localStorage.setItem(pkey+'_sort', sortEl.value); }catch(_){}
      buildTree(currentMode);
    });

    if(withFilter){
      const fi = q('filter'); if(fi) fi.oninput = applyFilter;
      const sb = q('search');
      if(sb) sb.onclick = async ()=>{
        const s = (fi.value||'').trim();
        if(s.length<2){ toast('至少 2 个字',true); return; }
        const out = q('searchout');
        out.innerHTML = '<div class="sub" style="padding:4px">搜索中…</div>';
        try{
          const d = await api('/api/search?type=dir&q='+encodeURIComponent(s));
          if(!d.rows.length){ out.innerHTML='<div class="sub" style="padding:4px">本地库无匹配目录</div>'; return; }
          out.innerHTML = d.rows.slice(0,20).map(r=>`<div class="ms-row" data-pd-pick="${esc(r.cid)}" data-name="${esc(r.name)}">
              <span>📁</span><span title="${esc(r.name)}">${esc(r.name)}</span>
              <span class="ms-path" title="${esc(r.path)}">${esc(r.path)}</span></div>`).join('')
            + (d.rows.length>20 ? `<div class="sub" style="padding:2px 6px">…共 ${d.rows.length} 条，仅显示前 20 条</div>` : '');
          out.querySelectorAll('[data-pd-pick]').forEach(el=>{
            el.onclick = ()=>{
              markPicked(el.dataset.pdPick, el.dataset.name);
              out.innerHTML = '';
              const row = tree.querySelector('.mt-row[data-cid="'+CSS.escape(el.dataset.pdPick)+'"]');
              if(row) row.scrollIntoView({block:'nearest'});
            };
          });
        }catch(e){ out.innerHTML='<div class="sub" style="padding:4px;color:var(--danger)">'+esc(e.message)+'</div>'; }
      };
    }

    if(allowAll){
      const allEl = q('all');
      if(allEl){
        if(picked.cid === allCid) allEl.classList.add('mt-sel');
        allEl.onclick = ()=>{
          markPicked(allCid, allLabel);
          allEl.classList.add('mt-sel');
        };
      }
    }

    q('ok').onclick = async ()=>{
      if(allowAll && picked.cid === allCid){
        done({cid:allCid, name:allLabel, mode:currentMode, exists:true, all:true});
        return;
      }
      if(!picked.cid){ toast('请先选择一个目录',true); return; }
      /* 统一校验是否已入库：未入库时明确提示（在线模式尤其容易选到未扫描目录） */
      let exists = null, scanState = null;
      try{
        const d = await api('/api/node/'+picked.cid);
        exists = !!d.exists; scanState = d.scan_state || null;
      }catch(_){ exists = null; }
      if(exists === false){
        const go = await appConfirm({
          title:'该目录尚未扫描入库',
          message:`「${picked.name}」还不在本地库里。\n依赖本地库的功能（例如查重、统计）可能得到空结果。\n\n仍要选择它吗？`,
          okText:'仍然选择',
        });
        if(!go) return;
      }
      done({cid:picked.cid, name:picked.name, mode:currentMode, exists:exists, scan_state:scanState});
    };
    q('cancel').onclick = ()=>done(null);
    q('close').onclick  = ()=>done(null);

    buildTree(currentMode);
  });
}

/* 转存目标目录：复用公共目录选择器 */
async function tfTargetPick(){
  const sel = await pickDir({
    title:'选择转存目标',
    cid:  document.getElementById('tfTargetCid').value,
    name: document.getElementById('tfTargetBtn').textContent.replace('📂 ','').trim(),
    persistKey:'tf_target',
  });
  if(!sel) return;
  document.getElementById('tfTargetCid').value = sel.cid;
  document.getElementById('tfTargetBtn').textContent = '📂 ' + sel.name;
  localStorage.setItem('tf_target_cid', sel.cid);
  localStorage.setItem('tf_target_name', sel.name);
}
async function doParse(items, src){
  if(!items.length){ toast('没有解析到有效的 115 分享链接',true); return; }
  S.parsed=items;
  $('#parseCard').style.display='block';
  $('#parseCount').textContent=`共 ${items.length} 条（已按分享码去重）· 来源: ${src}`;
  renderParsed();
  goBtn().disabled=false;
  toast(`解析到 ${items.length} 条链接`);
}
function renderParsed(){
  const tb=$('#parseTable tbody');
  tb.innerHTML=S.parsed.map((it,i)=>`<tr><td>${esc(it.title||'—')}</td>
    <td class="mono">${esc(it.share_code)}</td>
    <td class="mono">${esc(it.receive_code||'无')}</td>
    <td><button class="danger" data-action="rm-item" data-idx="${i}">移除</button></td></tr>`).join('');
  if(!S.parsed.length){
    $('#parseCard').style.display='none';
    goBtn().disabled=true;
  }
}
async function parseText(){
  const text=$('#tfText').value.trim();
  if(!text) return toast('先粘贴链接或上传文件',true);
  try{
    const d=await api('/api/transfer/parse/text',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text})});
    doParse(d.items,'粘贴文本');
  }catch(e){ toast(e.message,true); }
}
async function parseFile(file){
  if(!file) return;
  const fd=new FormData(); fd.append('file',file);
  try{ const d=await api('/api/transfer/parse/file',{method:'POST',body:fd}); doParse(d.items,file.name); }
  catch(err){ toast('解析失败: '+err.message,true); }
}
async function startTransfer(){
  if(!S.parsed.length) return;
  let targetCid=$('#tfTargetCid').value;
  let targetName=$('#tfTargetBtn').textContent.replace('📂 ','').trim()||targetCid;
  const newDir=$('#tfNewDir').value.trim();
  if(newDir){
    // 在选定目录下创建子目录
    if(!targetCid) return toast('请先选择父目录，再输入新建目录名',true);
    try{
      const d=await api('/api/fs/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:newDir,pid:targetCid})});
      targetCid=d.cid; targetName=newDir;
      toast('已创建目录: '+newDir);
      // 更新按钮显示
      $('#tfTargetCid').value=targetCid;
      $('#tfTargetBtn').textContent='📂 '+targetName;
      localStorage.setItem('tf_target_cid', targetCid);
      localStorage.setItem('tf_target_name', targetName);
    }catch(e){ return toast('创建目录失败: '+e.message,true); }
  }
  if(!targetCid) return toast('先选择目标目录',true);
  if(!await appConfirm({title:'开始转存',
    message:`确认转存 ${S.parsed.length} 条到「${targetName}」？`, okText:'开始转存'})) return;
  try{
    const d=await api('/api/transfer/task',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:$('#tfName').value.trim(),items:S.parsed,target_cid:targetCid,target_name:targetName})});
    toast('转存任务 #'+d.task_id+' 已开始');
    S.parsed=[]; $('#parseCard').style.display='none';
    $('#tfText').value=''; $('#tfFile').value='';
    goBtn().disabled=true;
    refreshTasks();
  }catch(e){ toast(e.message,true); }
}
async function refreshTasks(){
  let d;
  try{ d=await api('/api/transfer/tasks'); }catch(e){ return; }
  const el=$('#tfTasks');
  if(!d.tasks.length){ el.innerHTML='<div class="empty">暂无转存任务</div>'; return; }
  if(el.querySelector('.empty')) el.innerHTML='';
  const existing={};
  el.querySelectorAll('.dcard[data-tid]').forEach(c=>existing[c.dataset.tid]=c);
  let prev=null; const seen=new Set();
  for(const t of d.tasks){
    const key=String(t.id); seen.add(key);
    let card=existing[key];
    if(!card){
      card=document.createElement('div'); card.className='dcard'; card.dataset.tid=key;
      card.innerHTML=`<div class="dhead" data-action="task-toggle" data-id="${t.id}" style="cursor:pointer">
          <span class="tf-badge"></span>
          <span class="t">#${t.id} ${esc(t.name)} → ${esc(t.target_name)}</span>
          <span class="sub tf-counts"></span>
        </div>
        <div class="dbody"><div class="sub" style="padding:6px">点击展开明细</div></div>`;
    }
    card.querySelector('.tf-badge').outerHTML=statusBadge(t.status).replace('class="badge','class="tf-badge badge');
    card.querySelector('.tf-counts').textContent=`${t.processed}/${t.total} · 成功${t.n_success} 已存${t.n_repeat} 失效${t.n_expired} 失败${t.n_failed}`;
    if(!prev){ if(el.firstElementChild!==card) el.prepend(card); }
    else if(prev.nextElementSibling!==card) prev.after(card);
    prev=card;
  }
  for(const k in existing) if(!seen.has(k)) existing[k].remove();
}
async function toggleTask(id){
  const body=document.querySelector(`.dcard[data-tid="${id}"] .dbody`);
  const card=body.parentElement;
  card.classList.toggle('open');
  if(!card.classList.contains('open')) return;
  body.innerHTML='<div class="sub">加载中…</div>';
  try{
    const d=await api('/api/transfer/tasks/'+id);
    body.innerHTML='<table class="tf-task-table"><thead><tr><th>标题</th><th>分享码</th><th>状态</th><th>信息</th><th>时间</th><th></th></tr></thead><tbody>'+
      d.items.map(it=>{
        const ok=it.status==='success'||it.status==='repeat';
        const shareBtn=ok?`<button class="share-btn" data-action="share-copy" data-code="${esc(it.share_code)}" data-pw="${esc(it.receive_code||'')}" data-title="${esc(it.title||'')}" title="复制分享格式">🔗 分享</button>`:'';
        return `<tr><td>${esc(it.title||'—')}</td><td class="mono">${esc(it.share_code)}</td>
      <td>${statusBadge(it.status)}</td><td class="sub">${esc(it.message||'')}</td>
      <td class="sub">${esc(it.processed_at||'')}</td><td>${shareBtn}</td></tr>`;
      }).join('')+'</tbody></table>';
  }catch(e){ body.innerHTML='<div class="sub">加载失败</div>'; }
}

/* ============ 10. 结果与查重 ============ */
/* 查重范围：按钮 + 公共目录选择器（原为原生下拉，只能选一级目录） */
function setDupScope(cid, name){
  cid = cid||'';
  $('#dupScope').value = cid;
  const btn = $('#dupScopeBtn');
  if(btn) btn.textContent = cid ? ('📂 ' + (name||cid)) : '📂 全部一级目录（默认）';
  if(btn) btn.title = cid ? (name||cid) : '全部一级目录（默认）';
}
async function dupScopePick(){
  const cur = $('#dupScope').value;
  const curName = ($('#dupScopeBtn').textContent||'').replace('📂 ','').trim();
  const sel = await pickDir({
    title:'选择查重范围',
    cid: cur, name: curName,
    persistKey:'dup_scope',
    allowAll:true, allLabel:'全部一级目录（默认）',
  });
  if(!sel) return;
  setDupScope(sel.cid, sel.name);
  loadDupPage();
}
function bindDupControls(){
  $('#dupRefresh').onclick=()=>loadDupPage();
  $('#dupSort').onchange=()=>renderDupList();
  $('#dupFilter').oninput=()=>renderDupList();
  $('#dupKeepN').onchange=()=>renderDupList();
  $('#dupAuto').onchange=e=>{
    if(S.dup.autoTimer){ clearInterval(S.dup.autoTimer); S.dup.autoTimer=null; }
    if(e.target.checked){
      S.dup.autoTimer=setInterval(()=>{
        if($('#p-dup').classList.contains('on')) loadDupPage();
      },30000);
    }
  };
}
async function loadDupPage(params){
  try{
    if(params && params.has('scope')){
      const cid = params.get('scope')||'';
      let nm = cid;
      if(cid){
        /* 优先取 /api/roots 的名字：个别自根节点在本地库里 name 被写成了 cid 数字串 */
        try{
          await loadRoots();
          const r=(S.roots||[]).find(x=>x.cid===cid);
          if(r && r.name) nm=r.name;
          else { const d=await api('/api/node/'+cid); if(d.exists && d.name && d.name!==cid) nm=d.name; }
        }catch(_){}
      }
      setDupScope(cid, nm);
    }
    const scope=$('#dupScope').value||'';
    const [s,d]=await Promise.all([api('/api/stats'), api('/api/dups'+(scope?'?scope='+encodeURIComponent(scope):''))]);
    S.dup.data=d;
    d.groups.forEach(g=>{ g.gid=(g.names[0]&&g.names[0].cid)||g.key; });
    /* 统计：精确重复里多余副本可释放字节数 */
    let reclaimBytes=0, exactDuplicates=0;
    d.groups.forEach(g=>{
      g.exact.forEach(cluster=>{
        if(cluster.length<2) return;
        cluster.sort((a,b)=>b.sz-a.sz);
        for(let i=1;i<cluster.length;i++){ reclaimBytes+=cluster[i].sz||0; exactDuplicates++; }
      });
    });
    $('#statGrid').innerHTML=[
      ['一级目录',s.roots.toLocaleString()],['目录总数',s.dirs.toLocaleString()],
      ['文件总数',s.files.toLocaleString()],['总大小',fmt(s.total_size)],
      ['本地数据目录',s.scanned.toLocaleString()],['重复组',d.groups.length],
      ['精确重复组',d.groups.filter(g=>g.exact.length).length],
      ['可清理',fmt(reclaimBytes)+'（'+exactDuplicates+' 个多余副本）'],
    ].map(([k,v])=>`<div class="stat"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');
    if(!S.dup.rootsLoaded && scope===''){
      try{ await loadRoots(); S.dup.rootsLoaded=true; }catch(e){}
    }
    renderDupList();
    syncHash(scope?new URLSearchParams({scope}):null);
  }catch(e){ $('#dupList').innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>'; }
}
function dupSugDel(g, keepN){
  const sug=new Set();
  g.exact.forEach(cluster=>{
    if(cluster.length<2) return;
    const sorted=cluster.slice().sort((a,b)=>b.sz-a.sz);
    for(let i=keepN;i<sorted.length;i++) sug.add(sorted[i].cid);
  });
  return sug;
}
function renderDupList(){
  const d=S.dup.data; if(!d) return;
  const q=($('#dupFilter').value||'').trim().toLowerCase();
  const sortMode=$('#dupSort').value;
  const keepN=parseInt($('#dupKeepN').value);
  let groups=d.groups.slice();
  if(q) groups=groups.filter(g=>g.key.toLowerCase().includes(q)||g.names.some(n=>n.name.toLowerCase().includes(q)));
  if(sortMode==='exact'){
    groups.sort((a,b)=>(b.exact.length?b.exact.reduce((s,c)=>s+c.length,0):0)-(a.exact.length?a.exact.reduce((s,c)=>s+c.length,0):0)||b.names.length-a.names.length||a.key.localeCompare(b.key));
  } else if(sortMode==='name'){
    groups.sort((a,b)=>a.key.localeCompare(b.key,'zh'));
  } else {
    groups.sort((a,b)=>b.names.length-a.names.length||a.key.localeCompare(b.key,'zh'));
  }
  const exactG=d.groups.filter(g=>g.exact.length).length;
  $('#dupSummary').textContent=q
    ?`匹配 ${groups.length} / ${d.groups.length} 组${exactG?`，其中 ${exactG} 组为精确重复`:''} · 可切换顶部范围/排序`
    :`共 ${d.groups.length} 组${exactG?`，其中 ${exactG} 组为精确重复（可一键勾选）`:''}`;
  const el=$('#dupList');
  if(!groups.length){ el.innerHTML='<div class="empty">没有匹配的重复组</div>'; return; }
  /* 勾选状态：显式选择(S.dup.choice) 优先，否则按“建议删除”默认 */
  el.innerHTML=groups.map(g=>{
    const ex=g.exact.length;
    const exactCids=new Set();
    g.exact.forEach(v=>v.forEach(m=>exactCids.add(m.cid)));
    const sugDel=dupSugDel(g,keepN);
    const names=g.names.map(n=>{
      const isExact=exactCids.has(n.cid);
      const suggested=sugDel.has(n.cid);
      const checked=S.dup.choice.has(n.cid)?S.dup.choice.get(n.cid):suggested;
      return `<li class="${isExact?'exact':''}">
        <input type="checkbox" data-dupck data-cid="${esc(n.cid)}" data-pid="${esc(n.pid)}" data-name="${esc(n.name)}" data-sz="${n.sz||0}" ${checked?'checked':''}>
        <span class="dpath" title="${esc(n.path)}">📁 ${esc(n.path)}</span>
        <span class="sub">${n.fc.toLocaleString()} 文件 · ${fmt(n.sz)}${suggested?' · 建议删除':''}</span>
        <button class="danger" style="padding:2px 10px;font-size:12px" data-action="dup-del-one" data-cid="${esc(n.cid)}" data-pid="${esc(n.pid)}" data-name="${esc(n.name)}">删</button>
      </li>`;
    }).join('');
    const selN=g.names.filter(n=>S.dup.choice.has(n.cid)?S.dup.choice.get(n.cid):sugDel.has(n.cid)).length;
    return `<div class="dcard ${S.dup.open.has(g.gid)?'open':''}" data-gid="${esc(g.gid)}">
      <div class="dhead" data-action="dup-toggle" data-gid="${esc(g.gid)}">
        <span class="badge ${ex?'':'b-warn'}" style="${ex?'background:var(--danger-bg);color:var(--danger)':''}">${ex?'精确重复':'×'+g.names.length+' 版本'}</span>
        <span class="t">${esc(g.key)}</span>
        <span class="sub">${g.sizes.map(fmt).join(' / ')}</span>
        <span class="sub dcount">已勾选 ${selN} 个待删</span>
        <button class="danger btn-del-sel" data-action="dup-del-sel" data-gid="${esc(g.gid)}">删除选中</button>
      </div>
      <div class="dbody"><ul>${names}</ul></div>
    </div>`;
  }).join('');
  $$('#dupList .dcard').forEach(c=>{
    const sel=c.querySelectorAll('input[type=checkbox]:checked').length;
    c.classList.toggle('has-sel', sel>0);
  });
}
function onDupCheck(cb){
  S.dup.choice.set(cb.dataset.cid, cb.checked);
  const card=cb.closest('.dcard');
  if(card){
    const sel=card.querySelectorAll('input[type=checkbox]:checked').length;
    const total=card.querySelectorAll('input[type=checkbox]').length;
    card.querySelector('.dcount').textContent=`已勾选 ${sel} 个待删`;
    card.classList.toggle('has-sel', sel>0);
  }
}
async function dupDelOne(cid,pid,name){
  const ok=await confirmDanger({title:'确认删除', message:`确认删除「${name}」？`});
  if(!ok) return;
  try{
    await api('/api/fs/delete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cid,pid,confirm:true})});
    toast('已删除并移入回收站');
    S.dup.choice.delete(cid);
    loadDupPage(); loadHealth();
    removeTreeNodes([cid]);
  }catch(e){ toast('删除失败: '+e.message,true); }
}
async function dupDelSel(gid){
  const card=document.querySelector(`.dcard[data-gid="${CSS.escape(gid)}"]`);
  if(!card) return;
  const checked=[...card.querySelectorAll('input[type=checkbox]:checked')];
  if(!checked.length){ toast('请先勾选要删除的版本',true); return; }
  const total=card.querySelectorAll('input[type=checkbox]').length;
  const items=checked.map(i=>({cid:i.dataset.cid,pid:i.dataset.pid||'',name:i.dataset.name,sz:+i.dataset.sz||0}));
  const warn=checked.length>=total?`⚠️ 你选中了该组全部 ${total} 个版本，删完这组就没有了！`:'';
  const ok=await confirmDanger({title:`删除选中的 ${checked.length} 个目录`,
    message:'将移入 115 回收站（30天内可恢复）', okWarn:warn,
    items:items.map(i=>({name:i.name, sub:fmt(i.sz)})), searchable:true});
  if(!ok) return;
  toast('正在删除 '+items.length+' 个目录…');
  try{
    const r=await api('/api/fs/delete/batch',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items:items.map(i=>({cid:i.cid,pid:i.pid})),confirm:true})});
    toast(r.fail?`删除完成: 成功 ${r.success} / 失败 ${r.fail}`:`已删除 ${r.success} 个并移入回收站`, !!r.fail);
    (r.results||[]).filter(x=>x.ok).forEach(x=>S.dup.choice.delete(x.cid));
    loadDupPage(); loadHealth();
  }catch(e){ toast('批量删除失败: '+e.message,true); }
}
/* 勾选行自身带 data-pid（渲染时写入） */
function latestPid(cb){ return cb.dataset.pid||''; }
async function dupSelAllExact(){
  if(!S.dup.data) return;
  const keepN=parseInt($('#dupKeepN').value);
  let total=0;
  S.dup.data.groups.forEach(g=>{
    dupSugDel(g,keepN).forEach(cid=>{ S.dup.choice.set(cid,true); total++; });
  });
  renderDupList();
  toast(`已勾选 ${total} 个精确重复多余副本（保留每组最大 ${keepN} 个）`);
}
async function dupDelAllExact(){
  if(!S.dup.data) return;
  const keepN=parseInt($('#dupKeepN').value);
  const items=[]; let totalBytes=0;
  S.dup.data.groups.forEach(g=>{
    g.exact.forEach(cluster=>{
      if(cluster.length<2) return;
      const sorted=cluster.slice().sort((a,b)=>b.sz-a.sz);
      for(let i=keepN;i<sorted.length;i++){ items.push({cid:sorted[i].cid,pid:sorted[i].pid,name:sorted[i].name,sz:sorted[i].sz||0}); totalBytes+=sorted[i].sz||0; }
    });
  });
  if(!items.length){ toast('没有需要清理的精确重复',true); return; }
  const ok=await confirmDanger({title:'一键清理精确重复',
    message:`即将删除 ${items.length} 个精确重复目录（多余副本），预计释放 ${fmt(totalBytes)}。\n\n可在下方清单中核对。`,
    items:items.map(i=>({name:i.name, sub:fmt(i.sz)})), searchable:true});
  if(!ok) return;
  await runBatchDelete(items,'一键清理精确重复');
}
async function runBatchDelete(items,label){
  const pg=appProgress(label);
  let success=0, fail=0;
  for(let i=0;i<items.length;i+=5){
    const chunk=items.slice(i,i+5);
    try{
      const r=await api('/api/fs/delete/batch',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({items:chunk.map(i=>({cid:i.cid,pid:i.pid})),confirm:true})});
      success+=r.success||0; fail+=r.fail||0;
    }catch(e){ fail+=chunk.length; }
    pg.update(`已完成 ${Math.min(i+5,items.length)}/${items.length} · 成功 ${success} · 失败 ${fail}`,
      Math.round(Math.min(i+5,items.length)/items.length*100));
    if(i+5<items.length) await sleep(3000);
  }
  pg.close(`完成：成功 ${success}，失败 ${fail}`);
  items.forEach(i=>S.dup.choice.delete(i.cid));
  loadDupPage(); loadHealth();
}

/* ============ 11. 批量移动 / 浮条 ============ */
let moveSel={cid:null,name:''};
/* 批量移动：复用公共目录选择器（保留「过滤已加载目录」+「搜本地库」） */
async function openMoveDlg(){
  if(!S.sel.size){ toast('请先勾选要移动的项',true); return; }
  const sel = await pickDir({
    title:'批量移动 — 选择目标目录',
    cid: moveSel.cid, name: moveSel.name,
    persistKey:'move_target',
    withFilter:true,
  });
  if(!sel) return;
  moveSel = {cid:sel.cid, name:sel.name};
  await doMove();
}
async function doMove(){
  if(!S.sel.size){ toast('请先勾选要移动的项',true); return; }
  if(!moveSel.cid){ toast('请先在目录树中选择目标目录',true); return; }
  const items=[...S.sel.values()];
  if(items.some(i=>i.cid===moveSel.cid)){ toast('不能移动到被移动的项自身',true); return; }
  if(!await appConfirm({title:'确认移动',
    message:`确认移动 ${items.length} 项到「${moveSel.name}」？\n（云端立即生效，本地库自动同步）`, okText:'移动'})) return;
  toast('正在移动 '+items.length+' 项…');
  try{
    const r=await api('/api/fs/move',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items:items.map(i=>({cid:i.cid,pid:i.pid})),to_cid:moveSel.cid,to_name:moveSel.name,confirm:true})});
    toast(r.fail?`移动完成: 成功 ${r.success} / 失败 ${r.fail}`:`已移动 ${r.success} 项到「${moveSel.name}」`, !!r.fail);
    const okCids=(r.results||[]).filter(x=>x.ok).map(x=>x.cid);
    removeTreeNodes(okCids);
    refreshTreeNode(moveSel.cid);
    clearSel(); S.roots=null; refreshTagNodesIfOpen();
  }catch(e){ toast('移动失败: '+e.message,true); }
}

/* ============ 11.5 AI 分类建议 ============ */
let aiData = null;          // 最近一次 /api/ai/suggestions 结果
let aiApproved = null;      // 最近一次已通过队列
const aiSel = new Map();    // 勾选的 cid -> {name, category}
const aiOpen = new Set();   // 展开的分类组

async function loadAiPage(){
  try{ await loadRoots(); }catch(e){}
  initAiScope();
  loadAiConfig();
  refreshAiBatches();
  loadSuggestions();
  loadApproved();
  loadMoveHistory();
}
async function loadMoveHistory(){
  try{
    const d=await api('/api/ai/move-history?limit=100');
    const rows=d.rows||[];
    const el=$('#aiMoveHistory');
    $('#aiMoveSum').textContent=rows.length?`共 ${rows.length} 条`:'';
    if(!rows.length){ el.innerHTML='<div class="empty">暂无记录</div>'; return; }
    // 按时间分组
    const groups={};
    rows.forEach(r=>{
      const day=(r.moved_at||'').slice(0,10)||'未知';
      (groups[day]=groups[day]||[]).push(r);
    });
    let html='';
    for(const [day,items] of Object.entries(groups)){
      html+=`<div style="margin-bottom:10px"><div class="sub" style="font-weight:600;margin-bottom:4px">📅 ${day} (${items.length} 条)</div>`;
      items.forEach(r=>{
        const icon=r.status==='ok'?'✅':'❌';
        html+=`<div style="padding:3px 0;font-size:12.5px;border-bottom:1px solid #f0f0f0">`
          +`${icon} <b>${esc(r.name||r.cid)}</b> → ${esc(r.to_path)}`
          +(r.err?` <span style="color:var(--danger)">(${esc(r.err)})</span>`:'')
          +`</div>`;
      });
      html+=`</div>`;
    }
    el.innerHTML=html;
  }catch(e){}
}
/* AI 分析范围显示：统一带 📂 图标（与 转存/查重 的显示保持一致） */
function setAiScope(cid, name){
  cid = cid||''; name = name||cid;
  $('#aiScopeCid').value = cid;
  const el=$('#aiScopeDisplay');
  if(el){
    el.textContent = cid ? ('📂 '+name) : '点击选择分析范围…';
    el.title = cid ? name : '点击选择分析范围…';
  }
}
/* 读取显示里的真实目录名（去掉图标，避免把图标写进库） */
function aiScopeRawName(){
  return ($('#aiScopeDisplay').textContent||'').replace('📂 ','').trim();
}
function initAiScope(){
  /* 恢复上次选择 或 默认选中第一个一级目录（如果有 HiveWeb转存 则优先） */
  if(!S.roots || !S.roots.length) return;
  if($('#aiScopeCid').value) return;  // 已有选择则不覆盖
  const saved = localStorage.getItem('ai_scope_cid');
  const savedName = localStorage.getItem('ai_scope_name');
  if(saved){
    // 直接恢复上次的选择（不限于一级目录）
    setAiScope(saved, savedName || saved);
    return;
  }
  const preferred = S.roots.find(r=>r.name==='HiveWeb转存') || S.roots[0];
  setAiScope(preferred.cid, preferred.name);
}
/* AI 分析范围：复用公共目录选择器 */
async function aiScopePick(){
  const sel = await pickDir({
    title:'选择分析范围',
    cid:  $('#aiScopeCid').value,
    name: aiScopeRawName(),
    persistKey:'ai_scope',
  });
  if(!sel) return;
  setAiScope(sel.cid, sel.name);
  localStorage.setItem('ai_scope_cid', sel.cid);
  localStorage.setItem('ai_scope_name', sel.name);
}
async function loadAiConfig(){
  try{
    const d = await api('/api/ai/config');
    $('#aiBase').value = d.base_url || '';
    $('#aiModel').value = d.model || '';
    $('#aiKey').placeholder = d.key_set ? `已保存 ${d.key_masked}（留空=不修改）` : 'API Key';
    $('#aiCfgState').textContent = d.ready ? '● 已就绪' : '● 未配置';
    $('#aiCfgState').style.color = d.ready ? 'var(--ok)' : 'var(--warn)';
    $('#aiPrompt').value = d.prompt || '';
    $('#aiBatchSize').value = d.batch_size || 10;
    $('#aiTmdbKey').placeholder = d.tmdb_key_set ? `已保存 ${d.tmdb_key_masked}（留空=不修改）` : 'TMDB API Key（可选）';
  }catch(e){}
}
async function aiSaveConfig(){
  try{
    const bs=parseInt($('#aiBatchSize').value)||10;
    await api('/api/ai/config',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({base_url:$('#aiBase').value, model:$('#aiModel').value, key:$('#aiKey').value, prompt:$('#aiPrompt').value, batch_size:bs, tmdb_api_key:$('#aiTmdbKey').value})});
    $('#aiKey').value='';
    $('#aiTmdbKey').value='';
    toast('✓ AI 配置已保存');
    loadAiConfig();
  }catch(e){ toast('保存失败: '+e.message,true); }
}
async function aiTestConfig(){
  const out=$('#aiTestOut');
  out.textContent='测试中…';
  try{
    const d=await api('/api/ai/config/test',{method:'POST'});
    out.textContent = d.ok ? `✓ 连接正常（${d.model}，${d.ms}ms，回复「${d.reply}」）` : '✗ '+(d.error||'失败');
    out.style.color = d.ok ? 'var(--ok)' : 'var(--danger)';
  }catch(e){ out.textContent='✗ '+e.message; out.style.color='var(--danger)'; }
}
async function aiAnalyze(){
  const cid=$('#aiScopeCid').value;
  if(!cid) return toast('先选择范围目录',true);
  const limit=parseInt($('#aiLimit').value||0);
  const name=aiScopeRawName()||cid;
  try{
    const d=await api('/api/ai/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({scope_cid:cid,scope_name:name,limit})});
    toast('分析批次 #'+d.batch_id+' 已开始'+(limit?`（试跑 ${limit} 条）`:''));
    refreshAiBatches();
  }catch(e){ toast(e.message,true); }
}
let _aiLastBatchStates = {};  // 记录上次批次状态，用于检测完成
async function refreshAiBatches(){
  let d; try{ d=await api('/api/ai/batches'); }catch(e){ return; }
  const el=$('#aiBatches');
  if(!d.batches.length){ el.innerHTML='<div class="empty sub" style="padding:8px">暂无分析任务</div>'; return; }
  if(el.querySelector('.empty')) el.innerHTML='';
  const existing={};
  el.querySelectorAll('.dcard[data-bid]').forEach(c=>existing[c.dataset.bid]=c);
  let prev=null; const seen=new Set();
  let needRefreshSuggestions = false;
  for(const b of d.batches){
    // 检测批次状态变化：从 running/queued 变成 done/error/stopped
    const prevState = _aiLastBatchStates[b.id];
    if(prevState && (prevState === 'running' || prevState === 'queued') &&
       (b.status === 'done' || b.status === 'error' || b.status === 'stopped')){
      needRefreshSuggestions = true;
    }
    _aiLastBatchStates[b.id] = b.status;
    const key=String(b.id); seen.add(key);
    let card=existing[key];
    if(!card){
      card=document.createElement('div'); card.className='dcard'; card.dataset.bid=key;
      card.innerHTML=`<div class="dhead" style="cursor:default">
          <span class="ai-badge"></span>
          <span class="t"></span>
          <span class="sub ai-prog"></span>
          <span class="ai-act"></span>
        </div>`;
    }
    card.querySelector('.ai-badge').outerHTML=statusBadge(b.status).replace('class="badge','class="ai-badge badge');
    card.querySelector('.t').textContent=`#${b.id} ${b.scope_name||b.scope_cid}`;
    card.querySelector('.ai-prog').textContent=
      `${b.processed}/${b.total} · 规则 ${b.n_rule} · 调用 ${b.n_calls} 次 · token ${b.tokens_in}+${b.tokens_out}`+
      (b.status==='error'&&b.err?` · ${b.err.slice(0,60)}`:'');
    let act='';
    if(b.status==='running'||b.status==='queued'){
      act=`<button data-action="ai-stop" data-id="${b.id}" style="padding:2px 10px;font-size:12px">停止</button>`;
    } else {
      act=`<button data-action="ai-view-batch" data-id="${b.id}" data-scope="${esc(b.scope_cid)}" style="padding:2px 10px;font-size:12px">查看</button>`
        +`<button data-action="ai-rerun" data-id="${b.id}" data-scope="${esc(b.scope_cid)}" data-name="${esc(b.scope_name||'')}" style="padding:2px 10px;font-size:12px">重跑</button>`
        +`<button data-action="ai-del-batch" data-id="${b.id}" style="padding:2px 10px;font-size:12px;color:var(--danger)">删除</button>`;
    }
    card.querySelector('.ai-act').innerHTML=act;
    if(!prev){ if(el.firstElementChild!==card) el.prepend(card); }
    else if(prev.nextElementSibling!==card) prev.after(card);
    prev=card;
  }
  for(const k in existing) if(!seen.has(k)) existing[k].remove();
  // 批次完成时自动刷新建议列表
  if(needRefreshSuggestions && typeof loadSuggestions === 'function'){
    loadSuggestions();
  }
}
async function loadSuggestions(){
  try{
    const status=$('#aiStatus').value, cat=$('#aiCat').value;
    const conf=parseFloat($('#aiConf').value||0);
    aiData=await api(`/api/ai/suggestions?status=${encodeURIComponent(status)}&category=${encodeURIComponent(cat)}&min_conf=${conf}&limit=500`);
  }catch(e){ $('#aiList').innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>'; return; }
  // 清空选择状态(避免跨筛选条件误操作)
  aiSel.clear();
  renderAiSum(); renderAiList(); fillAiCatOptions();
}
function showBatchPopup(bid, rows){
  const old=document.getElementById('batchPopup'); if(old) old.remove();
  const bg=document.createElement('div');
  bg.id='batchPopup'; bg.className='modal-bg'; bg.style.zIndex=70;
  const groups={};
  rows.forEach(r=>{ (groups[r.category]=groups[r.category]||[]).push(r); });
  const cats=Object.keys(groups).sort();
  const summary=cats.map(c=>`${c}(${groups[c].length})`).join('、');
  const listHtml=cats.length
    ? cats.map(cat=>{
        const items=groups[cat].map(r=>`
          <div style="display:flex;gap:6px;align-items:center;padding:4px 0;font-size:12.5px;border-bottom:1px dashed var(--line);flex-wrap:wrap">
            <span style="flex:1;min-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.name)}">${esc(r.name)}</span>
            <span style="flex:none;display:flex;gap:4px;flex-wrap:wrap">
              ${[r.resolution,r.subtitle,r.country,r.quality,r.audio].filter(Boolean).map(t=>`<span class="badge b-pri" style="padding:0 5px;font-size:10px">${esc(t)}</span>`).join('')}
              ${r.target_path?`<span class="badge" style="padding:0 5px;font-size:10px;background:#f0f4fa;color:var(--accent)">📂${esc(r.target_path)}</span>`:''}
            </span>
            <span class="sub" style="flex:none;width:40px;text-align:right">${(r.confidence||0).toFixed(2)}</span>
            <span class="badge ${r.status==='approved'?'b-done':r.status==='rejected'?'b-err':'b-que'}" style="flex:none;font-size:10px">${esc(r.status)}</span>
          </div>`).join('');
        return `<div style="margin-bottom:10px"><div style="font-weight:600;font-size:13px;padding:4px 0">${esc(cat)} <span class="sub">×${groups[cat].length}</span></div>${items}</div>`;
      }).join('')
    :'<div class="empty sub" style="padding:20px">该批次暂无建议</div>';
  bg.innerHTML=`<div class="modal" style="max-width:860px;max-height:80vh;display:flex;flex-direction:column">
    <h3><span>批次 #${bid} 建议（${rows.length} 条）</span><span class="close" data-action="batch-popup-close">×</span></h3>
    <div class="sub" style="margin-bottom:8px">${summary||'无数据'}</div>
    <div style="flex:1;overflow-y:auto">${listHtml}</div>
    <div style="margin-top:10px;text-align:right"><button data-action="batch-popup-close">关闭</button></div>
  </div>`;
  bg.onclick=e=>{ if(e.target===bg||e.target.closest('[data-action="batch-popup-close"]')) bg.remove(); };
  document.body.appendChild(bg);
}
function fillAiCatOptions(){
  if(!aiData) return;
  const cur=$('#aiCat').value;
  $('#aiCat').innerHTML='<option value="">全部分类</option>'+
    (aiData.by_category||[]).map(c=>`<option value="${esc(c.category)}">${esc(c.category)}（${c.n}）</option>`).join('');
  if(cur) $('#aiCat').value=cur;
}
function renderAiSum(){
  if(!aiData) return;
  const bs=aiData.by_status||{};
  $('#aiSum').textContent=`待审 ${bs.pending||0} · 已过 ${bs.approved||0} · 已驳 ${bs.rejected||0} · 已移 ${bs.moved||0}`;
}
function renderAiList(){
  const el=$('#aiList');
  if(!aiData || !aiData.rows.length){ el.innerHTML='<div class="empty">暂无建议（先发起分析）</div>'; return; }
  const groups={};
  aiData.rows.forEach(r=>{ (groups[r.category]=groups[r.category]||[]).push(r); });
  el.innerHTML=Object.keys(groups).map(cat=>{
    const list=groups[cat];
    const avg=list.reduce((s,r)=>s+(r.confidence||0),0)/list.length;
    const rows=list.map(r=>`
      <li data-cid="${esc(r.cid)}">
        <input type="checkbox" data-aick data-cid="${esc(r.cid)}">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.name)}">${esc(r.name)}</span>
        ${[r.resolution,r.subtitle,r.country,r.quality,r.audio].filter(Boolean).map(t=>`<span class="badge b-pri" style="flex:none;padding:0 6px;font-size:10px">${esc(t)}</span>`).join('')}
        ${r.target_path?`<span class="badge" style="flex:none;padding:0 6px;font-size:10px;background:#f0f4fa;color:var(--accent)" title="目标路径">📂 ${esc(r.target_path)}</span>`:''}
        <span style="flex:none;display:inline-block;width:44px;height:8px;background:#eef0f3;border-radius:4px;overflow:hidden;vertical-align:middle"><span style="display:block;height:100%;width:${Math.round((r.confidence||0)*100)}%;background:${r.confidence>=0.7?'var(--ok)':'var(--warn)'}"></span></span>
        <span class="sub" style="flex:none;width:34px;text-align:right">${(r.confidence||0).toFixed(2)}</span>
        <span class="sub" style="flex:none;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.reason||'')}">${esc(r.reason||'')}</span>
        <select data-airecat data-cid="${esc(r.cid)}" style="padding:2px 6px;font-size:12px;flex:none;max-width:118px" title="改判并自动通过">
          <option value="">改判…</option>
          ${aiData.taxonomy.map(t=>`<option value="${esc(t)}" ${t===r.category?'hidden':''}>${esc(t)}</option>`).join('')}
        </select>
        <button class="pri" data-action="ai-pass" data-cid="${esc(r.cid)}" style="padding:2px 10px;font-size:12px">通过</button>
        <button class="danger" data-action="ai-rej" data-cid="${esc(r.cid)}" style="padding:2px 10px;font-size:12px">拒</button>
      </li>`).join('');
    return `<div class="dcard ${aiOpen.has(cat)?'open':''}" data-cat="${esc(cat)}">
      <div class="dhead" data-action="ai-toggle" data-cat="${esc(cat)}">
        <span class="badge b-pri">${esc(cat)}</span>
        <span class="t">×${list.length}</span>
        <span class="sub">平均置信 ${avg.toFixed(2)}</span>
        <button class="pri btn-del-sel" data-action="ai-approve-cat" data-cat="${esc(cat)}">全部通过</button>
      </div>
      <div class="dbody"><ul>${rows}</ul></div>
    </div>`;
  }).join('');
  $$('#aiList .dcard').forEach(c=>{
    const sel=c.querySelectorAll('input:checked').length;
    c.classList.toggle('has-sel', sel>0);
  });
  updateAiSelInfo();
}
function updateAiSelInfo(){
  $('#aiSelInfo').textContent = aiSel.size ? `已勾选 ${aiSel.size} 项 ▾` : '未勾选';
  $('#aiSelInfo').style.cursor = aiSel.size ? 'pointer' : 'default';
}
/* 勾选清单展开/收起 */
function renderAiSelList(){
  const el=$('#aiSelList');
  if(!el || el.style.display!=='block') return;
  if(!aiSel.size){ el.innerHTML='<div class="empty sub" style="padding:8px">未勾选任何项</div>'; return; }
  el.innerHTML=[...aiSel.entries()].map(([cid,it])=>`<div style="display:flex;gap:8px;align-items:center;padding:4px 8px;border-bottom:1px dashed var(--line);font-size:12.5px">
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(it.name)}">${esc(it.name)}</span>
      <span class="sub" style="flex:none">${esc(it.category||'')}</span>
      <span class="bp-x" data-action="ai-sel-remove" data-cid="${esc(cid)}" title="取消勾选" style="cursor:pointer;color:var(--danger);flex:none;padding:0 4px">✕</span>
    </div>`).join('')
    +`<div style="text-align:right;padding:6px 4px 2px"><button data-action="ai-sel-clear" style="padding:3px 12px;font-size:12px">清空勾选</button></div>`;
}
async function loadApproved(){
  try{
    aiApproved = await api('/api/ai/suggestions?status=approved&limit=500');
  }catch(e){ $('#aiApprovedList').innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>'; return; }
  renderAiApproved();
}
function renderAiApproved(){
  const el=$('#aiApprovedList');
  if(!aiApproved){ el.innerHTML='<div class="empty sub" style="padding:10px">加载中…</div>'; return; }
  const rows=aiApproved.rows||[];
  $('#aiAppSum').textContent = rows.length ? `${rows.length} 项（生成移动计划将整批移走这些内容）` : '暂无——审核通过的建议会出现在这里';
  if(!rows.length){ el.innerHTML='<div class="empty sub" style="padding:12px">暂无已通过的建议</div>'; return; }
  const groups={};
  rows.forEach(r=>{ (groups[r.category]=groups[r.category]||[]).push(r); });
  el.innerHTML=Object.keys(groups).map(cat=>{
    const list=groups[cat];
    const li=list.map(r=>{
      // 收集标签
      const tags=[r.resolution,r.subtitle,r.country,r.quality,r.audio].filter(Boolean);
      const tagHtml=tags.map(t=>`<span class="badge b-pri" style="padding:0 5px;font-size:10px">${esc(t)}</span>`).join('');
      // 目标路径
      const pathHtml=r.target_path?`<span class="badge" style="padding:0 5px;font-size:10px;background:#f0f4fa;color:var(--accent)">📂${esc(r.target_path)}</span>`:'';
      return `
      <div style="display:flex;gap:6px;align-items:center;padding:5px 6px;border-bottom:1px dashed var(--line);font-size:12.5px;flex-wrap:wrap">
        <span style="flex:1;min-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.name)}">${esc(r.name)}</span>
        ${(r.attribute||r.format)==='原盘'?'<span class="badge b-warn" style="flex:none;padding:0 5px;font-size:10px">原盘</span>':''}
        ${tagHtml}
        ${pathHtml}
        <span class="sub" style="flex:none;width:45px;text-align:right">${(r.confidence||0).toFixed(2)}</span>
        <button data-action="ai-unapprove" data-cid="${esc(r.cid)}" style="padding:2px 8px;font-size:11px" title="移回待审核">移回</button>
        <button class="danger" data-action="ai-rej" data-cid="${esc(r.cid)}" style="padding:2px 8px;font-size:11px">驳回</button>
      </div>`;
    }).join('');
    return `<div style="margin-bottom:8px;border:1px solid var(--line);border-radius:8px;overflow:hidden">
      <div style="background:#fafbfc;padding:6px 10px;font-size:13px;font-weight:600">${esc(cat)} <span class="sub">×${list.length}</span></div>
      <div style="padding:2px 8px">${li}</div>
    </div>`;
  }).join('');
}
async function aiSetStatus(cids, status, category){
  if(!cids.length) return;
  try{
    const d=await api('/api/ai/suggestions/status',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cids,status,category:category||''})});
    toast(`已更新 ${d.updated} 条`);
    cids.forEach(c=>aiSel.delete(c));
    updateAiSelInfo(); renderAiSelList();
    loadSuggestions();
    loadApproved();
  }catch(e){ toast('操作失败: '+e.message,true); }
}
async function aiPlanFlow(){
  try{ await loadRoots(); }catch(e){}
  const approved=(aiData&&aiData.by_status.approved)||0;
  if(!approved){ toast('还没有「已通过」的建议：先审核通过再生成移动计划',true); return; }
  const bg=document.createElement('div');
  bg.className='modal-bg'; bg.dataset.app='confirm'; bg.style.zIndex=70;
  /* 默认父目录：优先「网盘管理系统」，否则第一个一级目录，再否则网盘根(cid=0) */
  const defRoot = (S.roots||[]).find(r=>r.name==='网盘管理系统') || (S.roots||[])[0];
  const defCid  = defRoot ? defRoot.cid : '0';
  const defName = defRoot ? defRoot.name : '网盘根目录';
  bg.innerHTML=`<div class="modal" style="max-width:520px">
    <h3><span>生成移动计划</span><span class="close" data-c="n">×</span></h3>
    <div class="ac-msg">将移动全部「已通过」的建议：<b>${approved}</b> 项。
每个分类会在所选父目录下自动建目录（已存在则复用），然后分批移动，本地树自动同步。</div>
    <div class="row">
      <span class="sub" style="flex:none">父目录</span>
      <button id="aiParentBtn" type="button" style="flex:1;min-width:220px;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(defName)}">📂 ${esc(defName)}</button>
      <input type="hidden" id="aiParentSel" value="${esc(defCid)}">
    </div>
    <div class="row" style="margin-top:8px">
      <span class="sub" style="flex:none">或新建</span>
      <input id="aiParentNew" placeholder="在网盘根下新建目录名（可选）" style="flex:1">
    </div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
      <button data-c="n">取消</button><button class="pri" data-c="y">生成计划</button>
    </div></div>`;
  const done=v=>bg.remove();
  bg.addEventListener('click',e=>{
    const c=e.target.closest('[data-c]');
    if(c) done(c.dataset.c==='y');
    else if(e.target===bg) done(false);
  });
  document.body.appendChild(bg);
  /* 父目录：按钮 + 公共目录选择器（「网盘根目录」= cid 0） */
  bg.querySelector('#aiParentBtn').onclick = async ()=>{
    const pInp=bg.querySelector('#aiParentSel'), pBtn=bg.querySelector('#aiParentBtn');
    const sel = await pickDir({
      title:'选择父目录',
      cid: pInp.value,
      name: (pBtn.textContent||'').replace('📂 ','').trim(),
      persistKey:'ai_parent',
      allowAll:true, allCid:'0', allLabel:'网盘根目录',
    });
    if(!sel) return;
    pInp.value = sel.cid;
    pBtn.textContent = '📂 ' + sel.name;
    pBtn.title = sel.name;
  };
  bg.querySelector('[data-c="y"]').onclick=async()=>{
    // 注意:必须在 done(true) 移除弹窗 DOM 之前读取全部表单值
    const parentName=$('#aiParentNew').value.trim();
    let parentCid=$('#aiParentSel').value;
    const parentLabel=($('#aiParentBtn').textContent||'').replace('📂 ','').trim();
    const discSep = false; // 不再单独归类原盘
    if(parentName){
      try{
        const d=await api('/api/fs/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({name:parentName,pid:'0'})});
        parentCid=d.cid; S.roots=null;
      }catch(e){ toast('创建目录失败: '+e.message,true); return; }
    }
    done(true);
    try{
      const d=await api('/api/ai/plan',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({cids:[],parent_cid:parentCid,parent_name:parentName,disc_separate:discSep})});
      const byTo={};
      d.plan.forEach(p=>{ byTo[p.to_name]=(byTo[p.to_name]||0)+1; });
      const perCat=Object.keys(byTo).sort().map(k=>`${k}：${byTo[k]} 项`).join('\n');
      const ok=await appConfirm({title:'确认执行移动',
        message:`计划：${d.plan.length} 项 → ${d.mkdirs.length} 个分类目录\n${perCat}\n\n父目录：${parentName||parentLabel||parentCid}\n移动可反悔（再移回即可），本地树自动同步。`,
        okText:'执行移动'});
      if(!ok) return;
      await runAiMoves(d.plan);
    }catch(e){ toast('生成计划失败: '+e.message,true); }
  };
}
async function runAiMoves(plan){
  const pg=appProgress('AI 分类移动');
  const byTo={};
  plan.forEach(p=>{ (byTo[p.to_cid]=byTo[p.to_cid]||[]).push(p); });
  let success=0, fail=0, idx=0;
  const movedCids=[];
  for(const toCid of Object.keys(byTo)){
    const items=byTo[toCid];
    for(let i=0;i<items.length;i+=5){
      const chunk=items.slice(i,i+5);
      try{
        const r=await api('/api/fs/move',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({items:chunk.map(p=>({cid:p.cid,pid:p.pid})),
            to_cid:toCid,to_name:chunk[0].to_name,confirm:true})});
        success+=r.success||0; fail+=r.fail||0;
        (r.results||[]).filter(x=>x.ok).forEach(x=>movedCids.push(x.cid));
        // 记录移动历史
        const histItems=(r.results||[]).map(x=>({
          cid:x.cid, name:chunk.find(p=>p.cid===x.cid)?.name||'',
          from_path:chunk.find(p=>p.cid===x.cid)?.from_name||'',
          to_path:chunk[0].to_name||'', status:x.ok?'ok':'failed', err:x.error||''
        }));
        if(histItems.length){
          api('/api/ai/move-history',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({items:histItems})}).catch(()=>{});
        }
      }catch(e){ fail+=chunk.length; }
      idx+=chunk.length;
      pg.update(`已处理 ${idx}/${plan.length} · 成功 ${success} · 失败 ${fail}`,
        Math.round(idx/plan.length*100));
      if(i+5<items.length) await sleep(3000);
    }
    await sleep(2000);  // 分类目录组间冷却
  }
  if(movedCids.length){
    try{
      await api('/api/ai/suggestions/status',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({cids:movedCids,status:'moved'})});
    }catch(e){
      toast('⚠ 移动完成但状态同步失败: '+e.message+'. 请手动刷新页面',true);
    }
  }
  pg.close(`完成：成功 ${success}，失败 ${fail}`);
  loadSuggestions(); loadApproved(); loadHealth(); loadMoveHistory(); S.roots=null;
}

/* ============ 11.6 标签系统 ============ */
let tagCats=['属性','状态','来源','自定义'];

async function loadTagsPage(){
  await loadTagList();
  if(S.tags.cur.length) loadTagNodesBySet();
  else $('#tagNodesCard').style.display='none';
}
async function loadTagList(){
  try{
    const d=await api('/api/tags');
    S.tags.list=d.items||[];
    if(d.categories&&d.categories.length) tagCats=d.categories;
  }catch(e){
    $('#tagCloud').innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>';
    return;
  }
  fillTagCatSelects();
  renderTagCloud();
}
function fillTagCatSelects(){
  ['#tagNewCat','#tagDlgNewCat'].forEach(sel=>{
    const el=$(sel); if(!el) return;
    const cur=el.value;
    el.innerHTML=tagCats.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
    if(cur && tagCats.includes(cur)) el.value=cur;
  });
}
function renderTagCloud(){
  const list=S.tags.list||[];
  $('#tagSum').textContent=list.length?`共 ${list.length} 个标签`:'';
  if(!list.length){
    $('#tagCloud').innerHTML='<div class="empty sub" style="padding:12px">还没有标签，先在上面新建，或点「规则试跑」看看能自动识别多少</div>';
    return;
  }
  const groups={};
  list.forEach(t=>{ (groups[t.category||'自定义']=groups[t.category||'自定义']||[]).push(t); });
  const ordered=tagCats.filter(c=>groups[c]).concat(Object.keys(groups).filter(c=>!tagCats.includes(c)));
  // 多选 + 过滤栏(已选标签展示 + AND/OR 切换 + 清除 + 命中数)
  const filterBar=(S.tags.cur||[]).length?renderTagFilterBar():'';
  const tagsHtml=ordered.map(cat=>{
    const chips=groups[cat].map(t=>{
      const isOn=(S.tags.cur||[]).includes(t.id);
      const showN = isOn && S.tags.per_tag && (t.id in S.tags.per_tag)
        ? S.tags.per_tag[t.id]
        : (t.n||0);
      const dot=`<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${tagColor(t)};flex:none"></span>`;
      return `<span class="tagchip ${isOn?'on':''}" data-action="tag-toggle" data-id="${t.id}" data-cat="${esc(cat)}"
        title="点击 ${isOn?'移除':'加入'}筛选组合">
        ${dot}<span>${esc(t.name)}</span><b>${showN}</b>
        <span class="ticon" data-action="tag-rename" data-id="${t.id}" title="重命名">✎</span>
        <span class="ticon" data-action="tag-del" data-id="${t.id}" title="删除标签(关联一并删除)">✕</span>
      </span>`;
    }).join('');
    return `<div class="tagcat-row" data-cat="${esc(cat)}" style="margin-bottom:6px">
      <span class="sub" style="display:inline-block;width:52px;flex:none">${esc(cat)}</span>${chips}
    </div>`;
  }).join('');
  $('#tagCloud').innerHTML = filterBar + tagsHtml;
  // 编辑模式下绑定拖拽事件
  if(S.tags.editing) bindTagDragDrop();
}
function renderTagFilterBar(){
  const cur=S.tags.cur||[];
  const op=S.tags.op||'and';
  // 当前组合的总命中数(从 per_tag 任一标签拿都不准, 实际总数在 S.tags.total)
  const total=S.tags.total||0;
  // 顶部小说明 + AND/OR 切换 + 清除
  const opBtns=`<span class="sub" style="font-size:12px">组合:</span>
    <button data-action="tag-op" data-op="and" style="padding:3px 10px;font-size:12px;${op==='and'?'background:var(--accent);color:#fff;border-color:var(--accent)':''}">同时含 (AND)</button>
    <button data-action="tag-op" data-op="or"  style="padding:3px 10px;font-size:12px;${op==='or' ?'background:var(--accent);color:#fff;border-color:var(--accent)':''}">任一含 (OR)</button>`;
  // 已选标签条(可点 x 移除)
  const tagPills=cur.map(id=>{
    const t=(S.tags.list||[]).find(x=>x.id===id);
    if(!t) return '';
    const subCount = S.tags.per_tag && (id in S.tags.per_tag) ? S.tags.per_tag[id] : '?';
    const dot=`<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${tagColor(t)}"></span>`;
    return `<span class="tagchip on" style="padding:2px 6px;font-size:12px;cursor:default">
      ${dot}<span>${esc(t.name)}</span>
      <span class="sub" style="margin-left:2px">${subCount}</span>
      <span class="ticon" data-action="tag-toggle" data-id="${t.id}" title="移除" style="cursor:pointer">✕</span>
    </span>`;
  }).join('');
  return `<div style="margin-bottom:12px;padding:10px 12px;background:var(--accent-bg);border:1px solid var(--accent)59;border-radius:8px">
    <div class="row" style="gap:8px;margin-bottom:6px">
      ${opBtns}
      <span style="flex:1"></span>
      <b style="font-size:13px">${op==='and'?'交集':'并集'}</b>
      <span class="sub">共 <b style="color:var(--accent)">${total.toLocaleString()}</b> 个结果</span>
      <button data-action="tag-clear-filter" style="padding:2px 8px;font-size:11px">清除</button>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">${tagPills||'<span class="sub" style="font-size:12px">还没选标签</span>'}</div>
  </div>`;
}
async function loadTagNodesBySet(){
  const cur=S.tags.cur||[];
  // 已选 0 个: 保持旧行为(显示提示), 但保留空清单视图
  const card=$('#tagNodesCard'); card.style.display='none';
  if(!cur.length) return;
  card.style.display='block';
  $('#tagNodesTitle').textContent='标签组合清单';
  $('#tagNodes').innerHTML='<div class="empty sub">加载中…</div>';
  try{
    const op=S.tags.op||'and';
    const d=await api('/api/tags/nodes?ids='+cur.join(',')+'&op='+op+'&limit=500');
    S.tags.nodes=d.rows||[]; S.tags.total=d.total||0; S.tags.per_tag=d.per_tag||{};
    renderTagNodes();
  }catch(e){
    $('#tagNodes').innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>';
    S.tags.total=0; S.tags.nodes=[]; S.tags.per_tag={};
  }
  renderTagCloud();  // 刷新 chip 上的"组合下命中数"
}
function toggleTagSelection(id){
  id=parseInt(id);
  const cur=S.tags.cur||[];
  const idx=cur.indexOf(id);
  if(idx>=0) cur.splice(idx,1); else cur.push(id);
  S.tags.cur=cur;
  loadTagNodesBySet();
}
/* 拖拽排序: 在编辑模式下启用, 只支持同分类内拖拽 */
/* 拖拽状态放模块级 + 容器监听只绑一次:
   芯片每次渲染都重建, 状态放闭包里会"新芯片写新闭包、旧监听读旧闭包"对不上;
   容器监听器反复叠加会让一次拖拽触发 N 个保存请求(监听器泄漏) */
let dragEl=null, dragCat=null;
function bindTagDragDrop(){
  const cloud=$('#tagCloud');
  if(!cloud) return;
  cloud.querySelectorAll('.tagchip').forEach(el=>{
    el.draggable=true;
    el.addEventListener('dragstart', e=>{
      if(!S.tags.editing) return;
      dragEl=el; dragCat=el.dataset.cat||'';
      el.classList.add('dragging');
      e.dataTransfer.effectAllowed='move';
      e.dataTransfer.setData('text/plain', el.dataset.id||'');
    });
    el.addEventListener('dragend', ()=>{
      el.classList.remove('dragging');
      dragEl=null; dragCat=null;
      cloud.querySelectorAll('.tagchip.dragging').forEach(x=>x.classList.remove('dragging'));
    });
  });
  // 容器级监听只绑一次(重复绑定 = 一次拖拽发 N 个保存请求)
  if(cloud.dataset.dndBound==='1') return;
  cloud.dataset.dndBound='1';
  // 在每个 chip 上监听 dragover/drop, 判断插入位置
  cloud.addEventListener('dragover', e=>{
    if(!dragEl) return;
    e.preventDefault();
    e.dataTransfer.dropEffect='move';
    const target=e.target.closest('.tagchip');
    if(!target || target===dragEl || (target.dataset.cat||'')!==dragCat) return;
    // 计算插入位置(左/右)
    const rect=target.getBoundingClientRect();
    const mid=rect.left+rect.width/2;
    const before=e.clientX<mid;
    // 找目标的父容器, 在正确位置插入拖拽元素
    const container=target.parentNode;
    if(before) container.insertBefore(dragEl, target);
    else container.insertBefore(dragEl, target.nextSibling);
  });
  cloud.addEventListener('drop', e=>{
    e.preventDefault();
    if(!dragEl) return;
    // 同分类内保存新顺序: 收集该分类下所有 tag id
    const catRow=cloud.querySelector(`.tagcat-row[data-cat="${CSS.escape(dragCat)}"]`);
    if(!catRow) return;
    const newOrder=[...catRow.querySelectorAll('.tagchip[data-cat]')].map(x=>parseInt(x.dataset.id));
    if(!newOrder.length) return;
    // 保存到后端
    api('/api/tags/order',{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ids:newOrder})}).catch(e=>toast('排序保存失败: '+e.message,true));
  });
  cloud.addEventListener('dragenter', e=>e.preventDefault());
}
function renderTagNodes(){
  const rows=S.tags.nodes||[];
  $('#tagNodesSum').textContent=`共 ${S.tags.total.toLocaleString()} 个`+
    (S.tags.total>rows.length?`（本页显示 ${rows.length} 个，按大小排序）`:'');
  if(!rows.length){
    $('#tagNodes').innerHTML='<div class="empty sub" style="padding:12px">这个标签下还没有内容（或节点已不在库中）</div>';
    return;
  }
  $('#tagNodes').innerHTML='<ul class="tag-node-list">'+rows.map(r=>{
    const checked=S.sel.has(r.cid)?'checked':'';
    return `<li>
      <input type="checkbox" class="ckrow" data-tagck data-cid="${esc(r.cid)}" ${checked}>
      <span style="flex:none">${r.is_dir?'📁':'📄'}</span>
      <span class="tname ${r.is_dir?'dir':'file'}" style="cursor:pointer" data-action="tagnode-open"
        data-cid="${esc(r.cid)}" data-pid="${esc(r.pid||'')}" data-isdir="${r.is_dir?1:0}"
        title="${esc(r.name)}">${esc(r.name)}</span>
      <span class="sub" style="flex:none">${r.is_dir?'—':fmt(r.size)}</span>
    </li>`;
  }).join('')+'</ul>';
}
function tagSelAll(){
  const rows=S.tags.nodes||[]; if(!rows.length) return;
  rows.forEach(r=>toggleSel({cid:r.cid,pid:r.pid||'',name:r.name,is_dir:!!r.is_dir,size:r.size||0}, true));
  $$('#tagNodes input[data-tagck]').forEach(c=>c.checked=true);
  toast('已全选本页 '+rows.length+' 项，可点「移动勾选到…」');
}
function tagSelNone(){
  const rows=S.tags.nodes||[];
  rows.forEach(r=>S.sel.delete(r.cid));
  $$('#tagNodes input[data-tagck]').forEach(c=>c.checked=false);
  updateBatchBar();
}
function refreshTagNodesIfOpen(){
  if($('#p-tags').classList.contains('on') && S.tags.cur.length) loadTagNodesBySet();
}

/* ---------- 打标签弹窗(目录树勾选 / 标签清单 / 详情面板共用) ---------- */
let tagPick=null;   // {cids:[], common:Set<tagId>}
async function fetchTagsMap(cids){
  const map={};
  for(let i=0;i<cids.length;i+=200){
    const part=cids.slice(i,i+200);
    try{
      const d=await api('/api/nodes/tags?cids='+encodeURIComponent(part.join(',')));
      Object.assign(map, d.map||{});
    }catch(e){ toast('读取现有标签失败: '+e.message,true); break; }
  }
  return map;
}
async function openTagDlg(cids, title){
  cids=(cids||[]).map(String);
  if(!cids.length) return;
  if(!S.tags.list) await loadTagList();
  $('#tagDlgTitle').textContent=title||('打标签（已选 '+cids.length+' 项）');
  $('#tagDlgNew').value='';
  $('#tagDlg').style.display='flex';
  $('#tagDlgList').innerHTML='<div class="empty sub" style="padding:10px">读取中…</div>';
  const map=await fetchTagsMap(cids);
  const list=S.tags.list||[];
  // common = 全部所选节点共有的标签 => 初始勾选; 应用时未勾选的 common 会被摘掉
  const common=new Set(list.filter(t=>cids.every(c=>(map[c]||[]).some(x=>x.id===t.id))).map(t=>t.id));
  tagPick={cids, common};
  if(!list.length){
    $('#tagDlgList').innerHTML='<div class="empty sub" style="padding:10px">还没有标签，在下方新建一个吧</div>';
    return;
  }
  const groups={};
  list.forEach(t=>{ (groups[t.category||'自定义']=groups[t.category||'自定义']||[]).push(t); });
  const ordered=tagCats.filter(c=>groups[c]).concat(Object.keys(groups).filter(c=>!tagCats.includes(c)));
  $('#tagDlgList').innerHTML=ordered.map(cat=>{
    const rows=groups[cat].map(t=>{
      const dot=`<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${tagColor(t)};flex:none"></span>`;
      return `<label style="display:flex;gap:7px;align-items:center;padding:3px 2px;cursor:pointer;font-size:13px">
        <input type="checkbox" data-tagpick data-id="${t.id}" ${common.has(t.id)?'checked':''}>
        ${dot}<span>${esc(t.name)}</span><span class="sub">(${t.n||0})</span>
      </label>`;
    }).join('');
    return `<div style="padding:3px 0"><div class="sub" style="font-size:11.5px;margin:2px 0">${esc(cat)}</div>${rows}</div>`;
  }).join('');
}
function closeTagDlg(){ $('#tagDlg').style.display='none'; tagPick=null; }
async function applyTagPick(){
  if(!tagPick) return;
  const checked=[...document.querySelectorAll('#tagDlgList input[data-tagpick]:checked')].map(i=>+i.dataset.id);
  const add=checked.filter(id=>!tagPick.common.has(id));
  const remove=[...tagPick.common].filter(id=>!checked.includes(id));
  if(!add.length && !remove.length){ closeTagDlg(); return; }
  try{
    const d=await api('/api/tags/apply',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cids:tagPick.cids, add, remove})});
    toast(`标签已更新（打上 ${d.added} / 摘掉 ${d.removed}）`);
    patchRowTags(d.tags||{});
    closeTagDlg();
    loadTagList();
    refreshTagNodesIfOpen();
  }catch(e){ toast('打标失败: '+e.message,true); }
}
async function tagDlgNew(){
  if(!tagPick) return;
  const name=$('#tagDlgNew').value.trim();
  if(!name) return toast('先输入标签名',true);
  try{
    const d=await api('/api/tags',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name, category:$('#tagDlgNewCat').value, color:''})});
    const r=await api('/api/tags/apply',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cids:tagPick.cids, add:[d.id], remove:[]})});
    toast(`已创建并应用「${name}」（${r.added} 项）`);
    patchRowTags(r.tags||{});
    await loadTagList();
    openTagDlg(tagPick.cids, $('#tagDlgTitle').textContent);
  }catch(e){ toast('失败: '+e.message,true); }
}

/* ---------- 标签库管理 ---------- */
async function tagCreate(){
  const name=$('#tagNewName').value.trim();
  if(!name) return toast('先输入标签名',true);
  try{
    await api('/api/tags',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name, category:$('#tagNewCat').value, color:$('#tagNewColor').value})});
    $('#tagNewName').value='';
    toast('已创建标签「'+name+'」');
    loadTagList();
  }catch(e){ toast(e.message,true); }
}
async function tagDelete(id){
  const t=(S.tags.list||[]).find(x=>x.id===id); if(!t) return;
  if(!await appConfirm({title:'删除标签',
    message:`删除标签「${t.name}」？\n它的 ${t.n||0} 个关联会一并删除（资源本身不受任何影响）。`,
    danger:true, okText:'删除'})) return;
  try{
    await api('/api/tags/'+id,{method:'DELETE'});
    toast('已删除标签「'+t.name+'」');
    if(S.tags.cur.includes(id)) S.tags.cur = S.tags.cur.filter(x=>x!==id);
    S.tags.nodes=null; S.tags.total=0; S.tags.per_tag={};
    $('#tagNodesCard').style.display = S.tags.cur.length ? 'block' : 'none';
    loadTagList();
  }catch(e){ toast(e.message,true); }
}
async function tagRename(id){
  const t=(S.tags.list||[]).find(x=>x.id===id); if(!t) return;
  const name=prompt('修改标签名（当前: '+t.name+'）', t.name);
  if(name==null) return;
  const nm=name.trim();
  if(!nm || nm===t.name) return;
  try{
    await api('/api/tags/'+id,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:nm, category:t.category, color:t.color})});
    toast('已重命名为「'+nm+'」');
    loadTagList();
    if(S.tags.cur.includes(id)) loadTagNodesBySet();
  }catch(e){ toast(e.message,true); }
}
async function tagCleanup(){
  try{
    const d=await api('/api/tags/cleanup',{method:'POST'});
    toast('已清理失效关联 '+d.removed+' 条');
    loadTagList();
  }catch(e){ toast(e.message,true); }
}
async function tagClearAll(){
  const n=(S.tags.list||[]).reduce((a,t)=>a+(t.n||0),0);
  if(!await appConfirm({title:'一键清零全部标签',
    message:`即将清空全部标签关联（${n.toLocaleString()} 条），标签定义保留，之后可重新打标。\n此操作不可撤销。`,
    danger:true, okText:'确认清零'})) return;
  try{
    const d=await api('/api/tags/clear-all',{method:'POST'});
    toast('已清零 '+d.removed.toLocaleString()+' 条标签关联');
    loadTagList();
    if(S.tags.cur.length) loadTagNodesBySet();
  }catch(e){ toast(e.message,true); }
}

/* ---------- 规则引擎 ---------- */
async function runTagRules(dry){
  const clean=!dry && $('#tagRulesClean').checked;
  if(!dry){
    const ok=await appConfirm({title:'规则引擎打标',
      message:'按规则表给全库已入库目录和单体文件打标签（4K/原盘/国语/中字等，规则可在「规则管理」里编辑）。\n'
        +(clean?'将先清掉上次的规则结果再重新打标。':'已有规则结果会保留（重复的不叠加）。')+'\n手动打的标不受影响。',
      okText:'开始打标'});
    if(!ok) return;
  }
  const out=$('#tagRulesOut');
  out.style.display='block';
  out.textContent=dry?'试跑中…':'打标中…';
  try{
    const d=await api('/api/tags/rules/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({dry_run:dry, clean})});
    const lines=[`已入库目录 ${d.dirs.toLocaleString()} + 单体文件 ${(d.files||0).toLocaleString()}`+
      (d.files_skipped?`（已跳过逐集文件 ${d.files_skipped.toLocaleString()}）`:'')+
      (d.files_deny_skipped?`（已跳过图片/文档等非媒体文件 ${d.files_deny_skipped.toLocaleString()}）`:''),'————————————'];
    Object.keys(d.matched||{}).forEach(k=>{
      const nf=(d.matched_files||{})[k]||0;
      let line=`${k}：${d.matched[k]} 个`+(nf?`（目录 ${d.matched[k]-nf} / 文件 ${nf}）`:'');
      const smp=(d.sample&&d.sample[k]||[]).slice(0,2).map(s=>s.name);
      if(smp.length) line+=`　例: ${smp.join(' / ')}`;
      lines.push(line);
    });
    if(d.rule_errors&&d.rule_errors.length) lines.push('⚠ 有正则错误被跳过的规则: '+d.rule_errors.join('; '));
    lines.push('————————————');
    if(dry) lines.push('试跑结束，未写库。确认无误点「规则打标…」实际写入'+(clean?'（勾选了先清理）':''));
    else{
      lines.push(`已写入关联 ${d.assigned} 条`+(d.cleaned?`（先清理旧规则关联 ${d.cleaned} 条）`:''));
      loadTagList(); refreshTagNodesIfOpen();
    }
    out.textContent=lines.join('\n');
  }catch(e){
    out.textContent='失败: '+e.message;
  }
}

/* ---------- 规则管理(查看/编辑/停用/新增/删除, 规则存库) ---------- */
let tagRulesData=null, tagRuleEditing=null;   // tagRuleEditing: null | 'new' | 规则id
async function toggleTagRulesMgr(){
  const box=$('#tagRulesMgr');
  const show=box.style.display==='none';
  box.style.display=show?'block':'none';
  if(show) await loadTagRules();
}
async function loadTagRules(){
  $('#tagRulesList').innerHTML='<div class="empty sub" style="padding:10px">加载中…</div>';
  try{ tagRulesData=await api('/api/tags/rules'); }
  catch(e){ $('#tagRulesList').innerHTML='<div class="empty">加载失败: '+esc(e.message)+'</div>'; return; }
  renderTagRules();
}
function ruleRowView(r){
  return `<div style="display:flex;gap:8px;align-items:center;padding:5px 4px;border-bottom:1px dashed var(--line);min-width:0">
    <span class="sub" style="width:24px;flex:none">${r.id}</span>
    <span style="width:84px;flex:none;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.tag_name)}">${esc(r.tag_name)}</span>
    <code style="flex:2;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px" title="${esc(r.dir_pattern)}">${esc(r.dir_pattern)}</code>
    <code style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:#8a919c" title="${esc(r.file_pattern||'（未配，文件级用目录正则）')}">${r.file_pattern?esc(r.file_pattern):'（用目录正则）'}</code>
    <span class="sub" style="flex:none">${r.on_files?'含文件':'仅目录'}</span>
    <span class="sub" style="flex:none;width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.note||'')}">${esc(r.note||'')}</span>
    <span style="flex:none;display:flex;gap:4px;align-items:center">
      ${r.enabled?'':'<span class="sub" style="color:#e34d59">已停用</span>'}
      <button style="padding:2px 8px;font-size:12px" data-action="tag-rule-edit" data-id="${r.id}">编辑</button>
      <button style="padding:2px 8px;font-size:12px" data-action="tag-rule-enable" data-id="${r.id}">${r.enabled?'停用':'启用'}</button>
      <button style="padding:2px 8px;font-size:12px" data-action="tag-rule-del" data-id="${r.id}">删除</button>
    </span>
  </div>`;
}
function ruleRowEdit(r){
  const isNew=!r;
  const en=isNew?1:(r.enabled?1:0);
  return `<div data-rule-edit data-en="${en}" style="border:1px solid var(--line);border-radius:8px;background:#fff;padding:8px;margin:6px 0">
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
      <input data-rf="tag_name" placeholder="标签名(如 4K)" value="${isNew?'':esc(r.tag_name)}" style="width:100px">
      <input data-rf="dir_pattern" placeholder="目录/通用正则，如 2160p|4k" value="${isNew?'':esc(r.dir_pattern)}" style="flex:1;min-width:200px;font-family:Consolas,monospace;font-size:12px">
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:5px">
      <input data-rf="file_pattern" placeholder="文件级专用正则（空=用目录正则；原盘类建议只认 .iso/.nrg）" value="${isNew?'':esc(r.file_pattern||'')}" style="flex:1;min-width:200px;font-family:Consolas,monospace;font-size:12px">
      <label class="sub" style="display:flex;align-items:center;gap:3px;margin:0"><input type="checkbox" data-rf="on_files" ${isNew||r.on_files?'checked':''}> 也处理单体文件</label>
      <input data-rf="note" placeholder="备注" value="${isNew?'':esc(r.note||'')}" style="width:170px">
      <span style="flex:1"></span>
      <button class="pri" data-action="tag-rule-save" data-id="${isNew?'':r.id}">保存</button>
      <button data-action="tag-rule-cancel">取消</button>
    </div>
    ${isNew?'<div class="sub" style="margin-top:4px">正则为 Python 语法；中英混排名可参考现有规则（CJK 与字母间无 \\b 词边界，用 (?&lt;![a-z])xx(?![a-z]) 自定义边界）。</div>':''}
  </div>`;
}
function renderTagRules(){
  if(!tagRulesData) return;
  const deny=$('#tagRulesDeny');
  if(deny && document.activeElement!==deny) deny.value=tagRulesData.deny_ext||'';
  const rules=tagRulesData.rules||[];
  const parts=[];
  if(tagRuleEditing==='new') parts.push(ruleRowEdit(null));
  rules.forEach(r=>parts.push(tagRuleEditing===r.id?ruleRowEdit(r):ruleRowView(r)));
  $('#tagRulesList').innerHTML=parts.length?parts.join(''):'<div class="empty sub" style="padding:10px">还没有规则，点下方「新增规则」创建</div>';
}
async function tagRuleSave(id){   // id 为空 = 新增
  const box=$('#tagRulesList [data-rule-edit]'); if(!box) return;
  const get=k=>{ const el=box.querySelector(`[data-rf="${k}"]`); if(!el) return ''; return el.type==='checkbox'?el.checked:el.value.trim(); };
  const body={tag_name:get('tag_name'), dir_pattern:get('dir_pattern'),
    file_pattern:get('file_pattern'), on_files:!!get('on_files'),
    enabled:box.dataset.en==='1', note:get('note')};
  try{
    if(id) await api('/api/tags/rules/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    else await api('/api/tags/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    tagRuleEditing=null;
    toast('规则已保存。建议先「规则试跑」预览，再勾「先清理」重新打标');
    loadTagRules();
  }catch(e){ toast('保存失败: '+e.message,true); }
}
async function tagRuleToggle(id){
  const r=(tagRulesData&&tagRulesData.rules||[]).find(x=>x.id===id); if(!r) return;
  try{
    await api('/api/tags/rules/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tag_name:r.tag_name,dir_pattern:r.dir_pattern,file_pattern:r.file_pattern||'',
        on_files:!!r.on_files,enabled:!r.enabled,note:r.note||''})});
    toast(r.enabled?`已停用「${r.tag_name}」（已打关联保留，重打前不影响）`:`已启用「${r.tag_name}」`);
    loadTagRules();
  }catch(e){ toast(e.message,true); }
}
async function tagRuleDelete(id){
  const r=(tagRulesData&&tagRulesData.rules||[]).find(x=>x.id===id); if(!r) return;
  if(!await appConfirm({title:'删除规则',
    message:`删除「${r.tag_name}」这条规则？\n已经按它打上的关联不会被立即移除——之后勾选「先清理」重新打标即可清掉。`,
    danger:true, okText:'删除'})) return;
  try{
    await api('/api/tags/rules/'+id,{method:'DELETE'});
    toast('已删除规则「'+r.tag_name+'」');
    loadTagRules();
  }catch(e){ toast(e.message,true); }
}
async function tagDenySave(){
  try{
    await api('/api/tags/rules/deny-ext',{method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({deny_ext:$('#tagRulesDeny').value.trim()})});
    toast('已保存排除扩展名');
    loadTagRules();
  }catch(e){ toast(e.message,true); }
}

/* ============ 12. 全局事件委托 / 键盘 / 启动 ============ */
const ACTIONS={
  'tab': el=>{ applyTab(el.dataset.tab); syncHash(); },
  'open-backup': ()=>openBackupModal(),
  'backup-close': ()=>{ const m=document.getElementById('backupModal'); if(m) m.remove(); },
  'backup-now': ()=>doBackupNow(),
  'backup-refresh': ()=>loadBackupList(),
  'bk-restore': el=>doRestore(el.dataset.name),
  'bk-del': el=>delBackup(el.dataset.name),
  'tree-mode': el=>switchTreeMode(el.dataset.treeMode),
  'search-go': ()=>doSearch(),
  'search-root-pick': ()=>searchRootPick(),
  'hist-run': el=>runHistory(+el.dataset.idx),
  'hist-clear': ()=>clearHistory(),
  'scan-start': ()=>startScan(),
  'scan-root-pick': ()=>scanRootPick(),
  'job-stop': el=>stopJob(+el.dataset.id),
  'job-pause': el=>pauseJob(+el.dataset.id),
  'job-resume': el=>resumeJob(+el.dataset.id),
  'job-del': el=>deleteJob(+el.dataset.id),
  'sched-add': ()=>addSched(),
  'sch-root-pick': ()=>schRootPick(),
  'sched-del': el=>delSched(+el.dataset.id),
  'parse-text': ()=>parseText(),
  'rm-item': el=>{ S.parsed.splice(+el.dataset.idx,1); renderParsed(); },
  'transfer-go': ()=>startTransfer(),
  'tf-target-pick': ()=>tfTargetPick(),
  'task-toggle': el=>toggleTask(+el.dataset.id),
  'dup-refresh': ()=>loadDupPage(),
  'dup-scope-pick': ()=>dupScopePick(),
  'dup-toggle': el=>{
    const card=el.closest('.dcard'); const gid=el.dataset.gid;
    card.classList.toggle('open');
    if(card.classList.contains('open')) S.dup.open.add(gid); else S.dup.open.delete(gid);
  },
  'dup-sel-exact': ()=>dupSelAllExact(),
  'dup-del-exact': ()=>dupDelAllExact(),
  'dup-del-one': el=>dupDelOne(el.dataset.cid, el.dataset.pid, el.dataset.name),
  'dup-del-sel': el=>dupDelSel(el.dataset.gid),
  'd-scan': ()=>{ if(S.detailNode) scanOne(S.detailNode.cid, S.detailNode.name); },
  'd-rescan': ()=>{ if(S.detailNode) scanOne(S.detailNode.cid, S.detailNode.name, true); },
  'd-mkdir': ()=>{ if(S.detailNode) mkdirIn(S.detailNode.cid, S.detailNode.name); },
  'd-live': ()=>{ if(S.detailNode){ $('#detail').style.display='none'; buildTree(S.detailNode.cid,'',true); } },
  'd-dl': el=>dlOne(el.dataset.cid, el.nextElementSibling&&el.nextElementSibling.classList.contains('dlout')?el.nextElementSibling:$('#dlOut')),
  'd-copy': el=>copyDownloadLink(el.dataset.cid),
  'd-del': ()=>{ if(S.detailNode) delOne(S.detailNode.cid, S.detailNode.pid||'', S.detailNode.name); },
  'open-in-tree': el=>openInTree(el.dataset.cid),
  'scan-one': el=>scanOne(el.dataset.cid, el.dataset.name||''),
  'del-one': el=>delOne(el.dataset.cid, el.dataset.pid, el.dataset.name),
  'search-del-one': el=>delOne(el.dataset.cid, el.dataset.pid, el.dataset.name),
  'search-move-one': el=>searchMoveOne(el.dataset.cid, el.dataset.pid, el.dataset.name),
  'dl-one': el=>dlOne(el.dataset.cid, el.nextElementSibling),
  'move-open': ()=>openMoveDlg(),
  'bb-toggle': ()=>{
    const p=$('#bbPop');
    p.style.display=p.style.display==='block'?'none':'block';
    renderBBPop();
  },
  'bb-del': ()=>batchDel(),
  'bb-clear': ()=>{ clearSel(); renderBBPop(); },
  'bb-remove': el=>{
    S.sel.delete(el.dataset.cid);
    const ck=document.querySelector(`#treeBox .twrap[data-cid="${CSS.escape(el.dataset.cid)}"] .ckrow`);
    if(ck) ck.checked=false;
    updateBatchBar(); renderBBPop();
  },
  'ai-scope-pick': ()=>aiScopePick(),
  'ai-save-config': ()=>aiSaveConfig(),
  'ai-test': ()=>aiTestConfig(),
  'ai-prompt-toggle': ()=>{
    const w=$('#aiPromptWrap');
    w.style.display=w.style.display==='none'?'block':'none';
  },
  'ai-adv-toggle': ()=>{
    const w=$('#aiAdvWrap');
    w.style.display=w.style.display==='none'?'block':'none';
  },
  'ai-analyze': ()=>aiAnalyze(),
  'ai-refresh-batches': ()=>refreshAiBatches(),
  'ai-stop': async el=>{
    try{ await api('/api/ai/stop/'+el.dataset.id,{method:'POST'}); toast('已请求停止'); refreshAiBatches(); }
    catch(e){ toast(e.message,true); }
  },
  'ai-del-batch': async el=>{
    const bid=el.dataset.id;
    if(!await appConfirm({title:'删除分析批次', message:`删除批次 #${bid} 及其所有建议？\n已通过的建议不会被删除。`, danger:true, okText:'删除'})) return;
    try{ await api('/api/ai/batches/'+bid,{method:'DELETE'}); toast('已删除'); refreshAiBatches(); loadSuggestions(); }
    catch(e){ toast(e.message,true); }
  },
  'ai-rerun': el=>{
    const scope=el.dataset.scope, name=el.dataset.name||scope;
    setAiScope(scope, name);
    toast('已设置范围为「'+name+'」，点击「开始分析」重跑');
  },
  'ai-view-batch': async el=>{
    const bid=el.dataset.id;
    try{
      const d=await api('/api/ai/suggestions?batch_id='+bid+'&limit=500');
      showBatchPopup(bid, d.rows||[]);
    }catch(e){ toast('加载失败: '+e.message,true); }
  },
  'ai-load-sug': ()=>loadSuggestions(),
  'ai-approve-sel': ()=>aiSetStatus([...aiSel],'approved'),
  'ai-reject-sel': ()=>aiSetStatus([...aiSel],'rejected'),
  'ai-pass': el=>aiSetStatus([el.dataset.cid],'approved'),
  'ai-rej': el=>aiSetStatus([el.dataset.cid],'rejected'),
  'ai-approve-cat': el=>{
    const cat=el.dataset.cat;
    const cids=(aiData&&aiData.rows||[]).filter(r=>r.category===cat&&r.status==='pending').map(r=>r.cid);
    if(!cids.length){ toast('该组没有待审核的条目',true); return; }
    aiSetStatus(cids,'approved');
  },
  'ai-toggle': el=>{
    const card=el.closest('.dcard'); const cat=el.dataset.cat;
    card.classList.toggle('open');
    if(card.classList.contains('open')) aiOpen.add(cat); else aiOpen.delete(cat);
  },
  'ai-plan': ()=>aiPlanFlow(),
  'ai-clear-rejected': async()=>{
    if(!confirm('确认清除所有「已驳回」的建议？清除后这些条目可以重新分析。')) return;
    try{ const d=await api('/api/ai/suggestions/delete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({status:'rejected'})}); toast(`已清除 ${d.deleted} 条驳回记录`); loadSuggestions(); loadApproved(); }
    catch(e){ toast('清除失败: '+e.message,true); }
  },
  'ai-clear-moved': async()=>{
    if(!confirm('确认清除所有「已移动」的建议？移动历史记录会保留。')) return;
    try{ const d=await api('/api/ai/suggestions/delete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({status:'moved'})}); toast(`已清除 ${d.deleted} 条移动记录`); loadSuggestions(); loadApproved(); }
    catch(e){ toast('清除失败: '+e.message,true); }
  },
  'ai-clear-all': async()=>{
    if(!confirm('⚠ 确认清除全部 AI 建议？此操作不可恢复！')) return;
    if(!confirm('再次确认：清除全部建议数据？')) return;
    try{ const d=await api('/api/ai/suggestions/delete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({status:''})}); toast(`已清除 ${d.deleted} 条全部建议`); loadSuggestions(); loadApproved(); }
    catch(e){ toast('清除失败: '+e.message,true); }
  },
  'ai-clear-move-history': async()=>{
    if(!confirm('⚠ 确认清除所有移动历史记录？此操作不可恢复！')) return;
    try{ const d=await api('/api/ai/move-history',{method:'DELETE'}); toast(`已清除 ${d.deleted} 条移动历史`); loadMoveHistory(); }
    catch(e){ toast('清除失败: '+e.message,true); }
  },
  'ai-sel-toggle': ()=>{
    const el=$('#aiSelList');
    el.style.display = el.style.display==='block' ? 'none' : 'block';
    renderAiSelList();
  },
  'ai-sel-remove': el=>{
    aiSel.delete(el.dataset.cid);
    const ck=document.querySelector(`#aiList [data-aick][data-cid="${CSS.escape(el.dataset.cid)}"]`);
    if(ck) ck.checked=false;
    updateAiSelInfo(); renderAiSelList();
  },
  'ai-sel-clear': ()=>{
    aiSel.clear();
    $$('#aiList [data-aick]').forEach(c=>c.checked=false);
    $$('#aiList .dcard').forEach(c=>c.classList.remove('has-sel'));
    updateAiSelInfo(); renderAiSelList();
  },
  'ai-sel-all-page': ()=>{
    $$('#aiList [data-aick]').forEach(c=>{
      c.checked=true;
      const row=(aiData&&aiData.rows||[]).find(r=>r.cid===c.dataset.cid);
      if(row) aiSel.set(c.dataset.cid,{name:row.name,category:row.category});
    });
    $$('#aiList .dcard').forEach(c=>c.classList.add('has-sel'));
    updateAiSelInfo(); renderAiSelList();
    toast(`已全选 ${aiSel.size} 项`);
  },
  'ai-sel-none': ()=>{
    aiSel.clear();
    $$('#aiList [data-aick]').forEach(c=>c.checked=false);
    $$('#aiList .dcard').forEach(c=>c.classList.remove('has-sel'));
    updateAiSelInfo(); renderAiSelList();
  },
  'ai-unapprove': el=>aiSetStatus([el.dataset.cid],'pending'),
  'bb-tag': ()=>{ if(!S.sel.size) return toast('先勾选要打标的项',true); openTagDlg([...S.sel.keys()]); },
  'tagdlg-close': ()=>closeTagDlg(),
  'tagdlg-apply': ()=>applyTagPick(),
  'tagdlg-new': ()=>tagDlgNew(),
  'tag-create': ()=>tagCreate(),
  'tag-cleanup': ()=>tagCleanup(),
  'tag-clear-all': ()=>tagClearAll(),
  'tag-rules-dry': ()=>runTagRules(true),
  'tag-rules-run': ()=>runTagRules(false),
  'tag-rules-mgr': ()=>toggleTagRulesMgr(),
  'tag-rule-add': ()=>{ tagRuleEditing='new'; renderTagRules(); },
  'tag-rule-edit': el=>{ tagRuleEditing=+el.dataset.id; renderTagRules(); },
  'tag-rule-cancel': ()=>{ tagRuleEditing=null; renderTagRules(); },
  'tag-rule-save': el=>tagRuleSave(el.dataset.id||''),
  'tag-rule-del': el=>tagRuleDelete(+el.dataset.id),
  'tag-rule-enable': el=>tagRuleToggle(+el.dataset.id),
  'tag-deny-save': ()=>tagDenySave(),
  'tag-toggle': el=>toggleTagSelection(+el.dataset.id),
  'tag-op': el=>{
    const op=el.dataset.op;
    if(S.tags.op===op) return;
    S.tags.op=op;
    loadTagNodesBySet();
  },
  'tag-clear-filter': ()=>{
    S.tags.cur=[]; S.tags.per_tag={}; S.tags.nodes=null; S.tags.total=0;
    $('#tagNodesCard').style.display='none';
    renderTagCloud();
  },
  'tag-edit-toggle': el=>{
    S.tags.editing = !S.tags.editing;
    el.textContent = S.tags.editing ? '✅ 完成' : '✏️ 编辑';
    el.style.background = S.tags.editing ? 'var(--accent-bg)' : '';
    el.style.borderColor = S.tags.editing ? 'var(--accent)' : '';
    const cloud=$('#tagCloud');
    cloud.classList.toggle('tags-editing', S.tags.editing);
    if(S.tags.editing){
      bindTagDragDrop();
    }else{
      // 退出编辑时清除所有 draggable
      cloud.querySelectorAll('.tagchip[draggable]').forEach(x=>{ x.draggable=false; x.classList.remove('dragging'); });
      // 保存最终排序(整个 tagCloud 里的顺序)
      const allIds=[...cloud.querySelectorAll('.tagchip[data-cat]')].map(x=>parseInt(x.dataset.id)).filter(Boolean);
      if(allIds.length){
        api('/api/tags/order',{method:'PUT',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ids:allIds})}).catch(e=>toast('排序保存失败: '+e.message,true));
      }
    }
  },
  'tag-del': el=>tagDelete(+el.dataset.id),
  'tag-rename': el=>tagRename(+el.dataset.id),
  'tagnode-open': el=>openInTree(el.dataset.isdir==='1'?el.dataset.cid:(el.dataset.pid||el.dataset.cid)),
  'tag-sel-all': ()=>tagSelAll(),
  'tag-sel-none': ()=>tagSelNone(),
  'd-tag': ()=>{ if(S.detailNode) openTagDlg([String(S.detailNode.cid)], '打标签 — '+(S.detailNode.name||'').slice(0,24)); },
  'log-open': ()=>toggleLogPanel(),
  'log-close': ()=>closeLogPanel(),
  'log-clear': ()=>{ $('#logBody').innerHTML='<div class="lg lg-hint"><span class="lm">已清屏(服务端缓冲不受影响)</span></div>'; },
  'log-filter': el=>logSetFilter(el.dataset.src||'', el),
  'cookie-open': ()=>openCookieModal(),
  'cookie-close': ()=>{ const m=document.getElementById('cookieModal'); if(m) m.remove(); },
  'cookie-test': ()=>testCookie(),
  'cookie-save': ()=>saveCookie(),
  'share-copy': el=>{
    const code=el.dataset.code, pw=el.dataset.pw, title=el.dataset.title;
    const text=`https://115cdn.com/s/${code}?password=${pw}#\n${title}\n访问码：${pw}`;
    copyText(text).then(ok=>{
      toast(ok?'✓ 分享格式已复制':'复制失败', !ok);
    });
  },
};
document.addEventListener('click', e=>{
  const el=e.target.closest('[data-action]');
  if(el){
    const fn=ACTIONS[el.dataset.action];
    if(fn) fn(el,e);
  }
  /* 点击浮层/开关之外时收起已选清单 */
  const bp=$('#bbPop');
  if(bp.style.display==='block' && !e.target.closest('#bbPop') && !e.target.closest('[data-action="bb-toggle"]')){
    bp.style.display='none';
  }
});
document.addEventListener('change', e=>{
  const t=e.target;
  if(t && 'dupck' in (t.dataset||{})) onDupCheck(t);
  if(t && 'tagck' in (t.dataset||{})){
    const row=(S.tags.nodes||[]).find(x=>x.cid===t.dataset.cid);
    if(row) toggleSel({cid:row.cid,pid:row.pid||'',name:row.name,is_dir:!!row.is_dir,size:row.size||0}, t.checked);
  }
  if(t && 'aick' in (t.dataset||{})){
    const row=(aiData&&aiData.rows||[]).find(x=>x.cid===t.dataset.cid);
    if(t.checked) aiSel.set(t.dataset.cid,{name:row?row.name:t.dataset.cid, category:row?row.category:''});
    else aiSel.delete(t.dataset.cid);
    const card=t.closest('.dcard');
    if(card){
      const sel=card.querySelectorAll('input:checked').length;
      card.classList.toggle('has-sel', sel>0);
    }
    updateAiSelInfo(); renderAiSelList();
  }
  if(t && 'airecat' in (t.dataset||{}) && t.value){
    aiSetStatus([t.dataset.cid],'approved',t.value);
  }
});

function isTyping(el){
  const t=el&&el.tagName;
  return t==='INPUT'||t==='TEXTAREA'||t==='SELECT'||(el&&el.isContentEditable);
}
document.addEventListener('keydown', e=>{
  if(e.key==='Escape'){
    /* appConfirm/进度弹窗自带 Esc 处理；这里管页面级浮层，逐层关闭 */
    const lp=$('#logPanel');
    if(lp.style.display==='flex'){ closeLogPanel(); return; }
    const tg=$('#tagDlg');
    if(tg.style.display==='flex'){ tg.style.display='none'; return; }
    const cm=document.getElementById('cookieModal');
    if(cm && cm.style.display!=='none'){ cm.remove(); return; }
    const bp=$('#bbPop');
    if(bp.style.display==='block'){ bp.style.display='none'; return; }
    const dt=$('#detail');
    if(dt.style.display==='block'){ dt.style.display='none'; return; }
    return;
  }
  if(e.key==='/' && !isTyping(e.target) && !e.ctrlKey && !e.metaKey && !e.altKey){
    e.preventDefault();
    applyTab('search'); syncHash();
    setTimeout(()=>$('#searchQ').focus(),60);
  }
});

/* ============ 12.5 实时日志窗口 ============ */
/* 服务端内存日志总线(GET /api/logs 增量拉取, seq 游标); 面板打开时 2s 轮询,
   关闭时每 20s 静默查一次是否有新 error -> 按钮亮红点 */
S.log = { open:false, seq:0, follow:true, src:'', bgSeq:0, fetching:false };

function logAppend(entries){
  const body=$('#logBody');
  body.querySelectorAll('.lg-hint').forEach(h=>h.remove());
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 30;
  for(const e of entries){
    const div=document.createElement('div');
    div.className='lg lv-'+(e.lv||'info');
    const t=document.createElement('span'); t.className='lt'; t.textContent=e.t;
    const s=document.createElement('span'); s.className='ls'; s.textContent=e.src;
    const m=document.createElement('span'); m.className='lm'; m.textContent=e.msg;
    div.append(t,s,m);
    body.appendChild(div);
    if(e.seq>S.log.seq) S.log.seq=e.seq;
  }
  while(body.children.length>1500) body.firstElementChild.remove();
  if(S.log.follow && atBottom) body.scrollTop=body.scrollHeight;
}

async function pollLogs(){
  if(!S.log.open || S.log.fetching) return;
  try{
    const d=await api('/api/logs?after='+S.log.seq+(S.log.src?('&src='+encodeURIComponent(S.log.src)):''));
    if(d.latest < S.log.seq){ S.log.seq=0; S.log.bgSeq=0; logFetchLatest(); return; }  // 服务端重启过, 游标失效重建
    if(d.entries&&d.entries.length) logAppend(d.entries);
  }catch(_){}
}

function logFetchLatest(){
  /* 按当前过滤拉最近 300 条(首次打开/切过滤/游标失效时); fetching 防止与 2s 轮询并发双拉 */
  if(S.log.fetching) return;
  S.log.fetching=true;
  const src=S.log.src;
  api('/api/logs?after=0&limit=300'+(src?('&src='+encodeURIComponent(src)):'')).then(d=>{
    if(src!==S.log.src) return;   // 期间又切了过滤, 丢弃旧结果
    $('#logBody').innerHTML='';
    S.log.seq=0;
    if(d.entries&&d.entries.length) logAppend(d.entries);
    $('#logBody').scrollTop=$('#logBody').scrollHeight;
  }).catch(()=>{}).finally(()=>{ S.log.fetching=false; });
}

function openLogPanel(){
  S.log.open=true;
  const lp=$('#logPanel'), modal=lp.querySelector('.log-modal');
  lp.style.display='flex';
  /* 恢复上次保存的尺寸 */
  const saved=localStorage.getItem('m115_log_size');
  if(saved){ try{ const s=JSON.parse(saved); modal.style.width=s.w; modal.style.height=s.h; }catch(_){} }
  const btn=document.querySelector('header [data-action="log-open"]');
  if(btn) btn.classList.add('on');
  $('#logDot').style.display='none';
  if(!S.log.seq){ logFetchLatest(); }
  else { $('#logBody').scrollTop=$('#logBody').scrollHeight; pollLogs(); }
}

function closeLogPanel(){
  S.log.open=false;
  $('#logPanel').style.display='none';
  const btn=document.querySelector('header [data-action="log-open"]');
  if(btn) btn.classList.remove('on');
}

/* 顶栏按钮 = 开关: 弹窗开着时再点即关闭 */
function toggleLogPanel(){ S.log.open ? closeLogPanel() : openLogPanel(); }

/* 点遮罩空白处关闭; resize/dragging 期间不关闭 */
let logDragging=false;
$('#logPanel').addEventListener('pointerdown', e=>{
  if(e.target.id==='logPanel') return;
  const modal=e.target.closest('.log-modal');
  if(!modal) return;
  const rect=modal.getBoundingClientRect();
  const inResizeZone=e.clientX>=rect.right-12&&e.clientY>=rect.bottom-12;
  const inDragZone=e.target.closest('h3');
  if(inResizeZone||inDragZone) logDragging=true;
});
document.addEventListener('pointerup', ()=>{
  /* 保存弹窗尺寸 */
  if(S.log.open){
    const modal=$('#logPanel').querySelector('.log-modal');
    if(modal&&modal.offsetWidth) localStorage.setItem('m115_log_size',
      JSON.stringify({w:modal.offsetWidth+'px',h:modal.offsetHeight+'px'}));
  }
});
$('#logPanel').addEventListener('click', e=>{
  if(logDragging){ logDragging=false; return; }   /* resize/drag 后的 click 不关闭 */
  if(e.target.id==='logPanel') closeLogPanel();
});

/* 标题栏拖动弹窗(transform 平移, 关闭重开保留位置; 右下角 grip 拉伸由 CSS resize:both 提供) */
(()=>{
  const dlg=$('#logPanel'), modal=dlg.querySelector('.log-modal');
  let dragging=false, sx=0, sy=0, ox=0, oy=0;
  modal.querySelector('h3').addEventListener('mousedown', e=>{
    if(e.target.closest('[data-action]')) return;   // 不拦关闭按钮
    dragging=true; sx=e.clientX; sy=e.clientY; e.preventDefault();
  });
  document.addEventListener('mousemove', e=>{
    if(!dragging) return;
    ox += e.clientX-sx; oy += e.clientY-sy; sx=e.clientX; sy=e.clientY;
    modal.style.transform=`translate(${ox}px, ${oy}px)`;
  });
  document.addEventListener('mouseup', ()=>{ dragging=false; });
})();

function logSetFilter(src, el){
  S.log.src=src;
  $$('.log-toolbar .lg-chip').forEach(c=>c.classList.toggle('on', c===el));
  logFetchLatest();
}

/* 用户上翻 => 暂停跟随; 滚回底部 => 恢复 */
$('#logBody').addEventListener('scroll', ()=>{
  const b=$('#logBody');
  const atBottom = b.scrollHeight - b.scrollTop - b.clientHeight < 30;
  S.log.follow=atBottom;
  const st=$('#logFollow');
  st.textContent=atBottom?'跟随中':'已暂停(滚回底部恢复)';
  st.classList.toggle('on',atBottom);
});

/* 关闭时的静默监视: 有新 error 亮红点 */
setInterval(async ()=>{
  if(S.log.open){ S.log.bgSeq=S.log.seq; return; }
  try{
    const d=await api('/api/logs?after='+S.log.bgSeq+'&limit=100');
    if(d.latest < S.log.bgSeq){ S.log.bgSeq=0; return; }   // 服务端重启过, 游标归零重来
    if(d.entries&&d.entries.some(e=>e.lv==='error')) $('#logDot').style.display='inline-block';
    if(d.latest>S.log.bgSeq) S.log.bgSeq=d.latest;
  }catch(_){}
}, 20000);

/* ---------- 启动 ---------- */
loadHealth(); setInterval(loadHealth, 60000);
setInterval(()=>{ if($('#p-scan').classList.contains('on')) refreshJobs(); }, 4000);
setInterval(()=>{ if($('#p-transfer').classList.contains('on')) refreshTasks(); }, 4000);
setInterval(()=>{ if($('#p-ai').classList.contains('on')) refreshAiBatches(); }, 4000);
setInterval(pollLogs, 2000);
bindDupControls();
/* 搜索页：大小上下限输入后自动防抖搜索 */
let sizeTimer=null;
['searchMinMB','searchMaxMB'].forEach(id=>{
  $('#'+id).addEventListener('input', ()=>{ clearTimeout(sizeTimer); sizeTimer=setTimeout(doSearch,600); });
});
$('#tfFile').addEventListener('change', e=>parseFile(e.target.files[0]));

initTree().catch(e=>{ $('#treeBox').innerHTML='<div class="empty">根目录加载失败: '+esc(e.message)+'</div>'; });

/* 路由初始化：优先 hash，其次记住的上次页签，最后默认仪表盘 */
(()=>{
  const {tab,params}=parseHash();
  if(TABS[tab]) applyTab(tab,[...params].length?params:null);
  else{
    const last=localStorage.getItem('m115_tab');
    applyTab(TABS[last]?last:'dash');
  }
  syncHash();
})();
