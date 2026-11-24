/* AI听记 前端（原生 JS，不用打包）。接口一律用相对路径 api/xxx。
   飞书内置浏览器会拦截 alert/confirm/prompt，一律用这里的 toast / ask。 */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const app = $("#app");

  // 本地试用：地址加 ?as=board / ?as=lead 可以模拟董事局 / 部门负责人（服务器上无效）
  const asParam = new URLSearchParams(location.search).get("as");
  if (asParam) document.cookie = "tj_as=" + asParam + "; path=/";

  const S = { me: null, tree: null, view: "kb", sel: { folder: "", month: "", pending: false }, page: 1, list: null,
              q: "", terms: [], cur: null, doc: null, tab: "summary", adminTab: "import", checked: new Set() };

  // ------------------------------------------------------------ 工具
  async function api(path, opts = {}) {
    const o = { headers: {}, ...opts };
    if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; delete o.json; }
    const r = await fetch(path, o);
    let data = null;
    try { data = await r.json(); } catch { /* 非 JSON */ }
    if (!r.ok) throw new Error((data && data.error) || `请求失败（${r.status}）`);
    return data;
  }
  function toast(msg, err) {
    const d = document.createElement("div");
    d.textContent = msg; if (err) d.className = "err";
    $("#toast").appendChild(d); setTimeout(() => d.remove(), err ? 5000 : 2600);
  }
  function ask(title, text, okText = "确定") {
    return new Promise((res) => {
      const m = $("#modal");
      m.innerHTML = `<div class="box"><h3>${esc(title)}</h3><div class="muted">${esc(text)}</div>
        <div class="acts"><button class="btn" data-a="0">取消</button><button class="btn primary" data-a="1">${esc(okText)}</button></div></div>`;
      m.hidden = false;
      m.onclick = (e) => { const a = e.target.dataset.a; if (a !== undefined || e.target === m) { m.hidden = true; res(a === "1"); } };
    });
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtMonth = (ym) => { if (!ym || ym === "unknown") return "日期不详"; const [y, m] = ym.split("-"); return `${y}年${+m}月`; };
  const fmtDur = (n) => !n ? "" : n >= 60 ? `${Math.floor(n / 60)}小时${n % 60 ? (n % 60) + "分" : ""}` : `${n}分钟`;
  const fmtTime = (t) => (t || "").replace(/^(\d{4})-/, (m, y) => (y === String(new Date().getFullYear()) ? "" : y + "-"));
  function mark(html) {
    if (!S.terms.length) return html;
    // 只替换标签外的文字
    const re = new RegExp("(" + S.terms.map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")", "g");
    return html.replace(/(^|>)([^<]+)/g, (m, a, b) => a + b.replace(re, "<mark>$1</mark>"));
  }

  // 简单 Markdown（钉钉摘要用到的：标题、列表、引用、表格、加粗、分隔线）
  function md(src) {
    const lines = String(src || "").replace(/\r/g, "").split("\n");
    const out = []; let list = null; let i = 0;
    const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<i>$2</i>").replace(/`([^`]+)`/g, "<code>$1</code>");
    const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
    while (i < lines.length) {
      const l = lines[i];
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        closeList();
        const row = (x) => x.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
        let h = `<table><tr>${row(l).map((c) => `<th>${c}</th>`).join("")}</tr>`; i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { h += `<tr>${row(lines[i]).map((c) => `<td>${c}</td>`).join("")}</tr>`; i++; }
        out.push(h + "</table>"); continue;
      }
      let m;
      if ((m = l.match(/^(#{1,6})\s+(.*)/))) { closeList(); const n = Math.min(4, m[1].length + 1); out.push(`<h${n}>${inline(m[2])}</h${n}>`); }
      else if ((m = l.match(/^\s*[-*+]\s+(.*)/))) { if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; } out.push(`<li>${inline(m[1])}</li>`); }
      else if ((m = l.match(/^\s*(\d+)[.、)]\s+(.*)/))) { if (list !== "ol") { closeList(); out.push(`<ol start="${m[1]}">`); list = "ol"; } out.push(`<li>${inline(m[2])}</li>`); }
      else if ((m = l.match(/^\s*>\s?(.*)/))) { closeList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
      else if (/^\s*(-{3,}|\*{3,})\s*$/.test(l)) { closeList(); out.push("<hr>"); }
      else if (!l.trim()) { closeList(); }
      else { closeList(); out.push(`<p>${inline(l)}</p>`); }
      i++;
    }
    closeList();
    return out.join("\n");
  }
  function transcriptHtml(t) {
    return String(t || "").split("\n").filter((x) => x.trim()).map((l) => {
      const m = l.match(/^\[([^\]]{1,20})\]\s*(.*)/);
      return m ? `<p><span class="sp">${esc(m[1])}</span>${esc(m[2])}</p>` : `<p>${esc(l)}</p>`;
    }).join("");
  }
  const folderTag = (f) => `<span class="tag ${f === "研发市场销售" ? "biz" : ""}">${esc(f)}</span>`;

  // ------------------------------------------------------------ 启动
  async function boot() {
    try { S.me = await api("api/me"); }
    catch (e) { app.innerHTML = `<div class="empty-state"><div><div class="big">🔒</div>${esc(e.message)}</div></div>`; return; }
    $("#me").innerHTML = `<div>${esc(S.me.name)}</div><div class="role">${esc(S.me.role_label)}</div>`;
    $("#navAdmin").hidden = !S.me.is_admin;
    if (S.me.role === "none") { app.innerHTML = `<div class="empty-state"><div><div class="big">🔒</div>你没有查看听记的权限，请联系管理员</div></div>`; return; }
    $$(".nav button").forEach((b) => b.onclick = () => go(b.dataset.view));
    $("#searchForm").onsubmit = (e) => { e.preventDefault(); doSearch($("#searchInput").value); };
    go("kb");
  }
  function go(view) {
    S.view = view;
    $$(".nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
    if (view === "admin") renderAdmin(); else renderKb();
  }

  // ------------------------------------------------------------ 知识库
  async function renderKb() {
    app.innerHTML = `<div class="kb" id="kb"><aside class="col tree" id="tree"></aside><section class="col list" id="list"></section><section class="col preview" id="preview"></section></div>`;
    await loadTree();
    if (S.q) await doSearch(S.q, true);
    else if (!S.sel.folder && !S.sel.pending && S.tree.folders.length) await select(S.tree.folders[0].name, "");
    else await loadList();
    renderPreview();
  }
  async function loadTree() {
    S.tree = await api("api/tree");
    const sub = S.tree.total !== undefined ? `会议知识库 · 共 ${S.tree.total} 场` : "会议知识库 · 研发市场销售";
    $("#brandSub").textContent = sub;
    drawTree();
  }
  function drawTree() {
    const el = $("#tree"); if (!el) return;
    const groups = [["板块", "📁"], ["上海食品", "🏢"], ["子公司", "🏬"]];
    let h = "";
    if (S.tree.pending !== undefined) {
      h += `<div class="node pending ${S.sel.pending ? "on" : ""}" data-pending="1"><span class="ico">⏳</span>待确认<span class="n">${S.tree.pending}</span></div>`;
    }
    for (const [g, ico] of groups) {
      const fs = S.tree.folders.filter((f) => f.group === g);
      if (!fs.length) continue;
      h += `<h4>${g === "板块" ? "业务板块" : g}</h4>`;
      for (const f of fs) {
        const open = S.sel.folder === f.name;
        h += `<div class="node ${open ? "open" : ""} ${open && !S.sel.month && !S.q ? "on" : ""}" data-folder="${esc(f.name)}"><span class="caret">▶</span>${esc(f.name)}<span class="n">${f.count}</span></div>`;
        if (open && f.months.length) {
          h += `<div class="months">` + f.months.map((m) => `<div class="node ${S.sel.month === m.ym && !S.q ? "on" : ""}" data-folder="${esc(f.name)}" data-month="${m.ym}">${fmtMonth(m.ym)}<span class="n">${m.count}</span></div>`).join("") + `</div>`;
        }
      }
    }
    if (!S.tree.folders.length) h += `<div class="muted" style="padding:10px">暂无可查看的文件夹</div>`;
    el.innerHTML = h;
    $$(".node", el).forEach((n) => n.onclick = () => {
      if (n.dataset.pending) return selectPending();
      const f = n.dataset.folder, m = n.dataset.month || "";
      if (!m && S.sel.folder === f && !S.sel.month && !S.q) { S.sel.folder = ""; drawTree(); return; } // 再点一次收起
      select(f, m);
    });
  }
  async function select(folder, month) {
    S.sel = { folder, month, pending: false }; S.page = 1; S.q = ""; S.terms = []; $("#searchInput").value = "";
    drawTree(); await loadList();
  }
  async function selectPending() {
    S.sel = { folder: "", month: "", pending: true }; S.page = 1; S.q = ""; S.terms = []; S.checked.clear(); $("#searchInput").value = "";
    drawTree(); await loadList();
  }
  async function loadList() {
    const el = $("#list"); if (!el) return;
    if (!S.sel.folder && !S.sel.pending) { el.innerHTML = `<div class="empty-state"><div>从左边选择一个文件夹</div></div>`; return; }
    el.innerHTML = `<div class="list-head"><div class="muted">加载中…</div></div>`;
    const p = new URLSearchParams({ page: S.page });
    if (S.sel.folder) p.set("folder", S.sel.folder);
    if (S.sel.month) p.set("month", S.sel.month);
    if (S.sel.pending) p.set("status", "pending");
    try { S.list = await api("api/meetings?" + p); } catch (e) { el.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; return; }
    const title = S.sel.pending ? "待确认" : S.sel.folder + (S.sel.month ? " · " + fmtMonth(S.sel.month) : "");
    const desc = S.sel.pending ? "系统拿不准的会，先只有董事局和管理员能看。点开核对归类，确认后才会出现在对应文件夹。" : `${S.list.total} 场会议`;
    drawList(title, desc, S.list.items, S.list.total);
  }
  function drawList(title, desc, items, total, isSearch) {
    const el = $("#list");
    const pending = S.sel.pending && !isSearch && S.me.is_admin;
    let h = `<div class="list-head"><div class="t">${esc(title)}</div><div class="s">${esc(desc)}</div>`;
    if (pending && items.length) h += `<div class="tools"><button class="btn sm" id="chkAll">全选本页</button><button class="btn sm primary" id="confirmSel" disabled>确认所选（按现在的归类）</button></div>`;
    h += `</div><div class="items">`;
    if (!items.length) h += `<div class="empty-state"><div><div class="big">🗂</div>${isSearch ? "没有找到相关会议" : S.sel.pending ? "没有待确认的会议" : "这里还没有会议"}</div></div>`;
    for (const m of items) {
      const tags = (m.folders || []).map(folderTag).join("") + (m.mtype && m.mtype !== "其他" ? `<span class="tag">${esc(m.mtype)}</span>` : "")
        + (m.sensitive ? `<span class="tag bad">敏感</span>` : "") + (m.status === "pending" && !pending ? `<span class="tag warn">待确认</span>` : "");
      h += `<div class="item ${S.cur === m.id ? "on" : ""}" data-id="${m.id}">
        ${pending ? `<input type="checkbox" class="chk" data-id="${m.id}" ${S.checked.has(m.id) ? "checked" : ""}>` : ""}
        <div class="title">${mark(esc(m.title))}</div>
        <div class="meta"><span>${esc(fmtTime(m.start_time))}</span><span>${esc(m.creator || "")}</span><span>${fmtDur(m.duration)}</span>${m.entity && m.entity !== "上海食品" ? `<span>${esc(m.entity)}</span>` : ""}</div>
        ${m.snippet ? `<div class="snip">${mark(esc(m.snippet))}</div>` : ""}
        <div class="tags">${tags}</div></div>`;
    }
    h += `</div>`;
    if (!isSearch && total > S.list.page_size) {
      const pages = Math.ceil(total / S.list.page_size);
      h += `<div class="pager"><button class="btn sm" id="prev" ${S.page <= 1 ? "disabled" : ""}>上一页</button>${S.page} / ${pages}<button class="btn sm" id="next" ${S.page >= pages ? "disabled" : ""}>下一页</button></div>`;
    }
    el.innerHTML = h; el.scrollTop = 0;
    $$(".item", el).forEach((it) => it.onclick = (e) => { if (e.target.classList.contains("chk")) return; openMeeting(+it.dataset.id); });
    $$(".chk", el).forEach((c) => c.onchange = () => { c.checked ? S.checked.add(+c.dataset.id) : S.checked.delete(+c.dataset.id); syncChk(); });
    const pv = $("#prev", el), nx = $("#next", el);
    if (pv) pv.onclick = () => { S.page--; loadList(); };
    if (nx) nx.onclick = () => { S.page++; loadList(); };
    const all = $("#chkAll", el);
    if (all) all.onclick = () => { items.forEach((m) => S.checked.add(m.id)); $$(".chk", el).forEach((c) => c.checked = true); syncChk(); };
    const cs = $("#confirmSel", el);
    if (cs) cs.onclick = async () => {
      if (!(await ask("确认归类", `把选中的 ${S.checked.size} 场会按现在的归类确认？确认后，放在「研发市场销售」的会部门负责人就能看到。`, "确认"))) return;
      try { const r = await api("api/meetings/confirm", { method: "POST", json: { ids: [...S.checked] } }); toast(`已确认 ${r.updated} 场`); S.checked.clear(); await loadTree(); await loadList(); }
      catch (e) { toast(e.message, true); }
    };
    syncChk();
  }
  function syncChk() { const b = $("#confirmSel"); if (b) { b.disabled = !S.checked.size; b.textContent = S.checked.size ? `确认所选 ${S.checked.size} 场` : "确认所选（按现在的归类）"; } }

  async function doSearch(q, keep) {
    q = (q || "").trim();
    if (!q) { S.q = ""; S.terms = []; if (!keep) renderKb(); return; }
    if (S.view !== "kb") { S.q = q; go("kb"); return; }
    S.q = q; drawTree();
    const el = $("#list"); el.innerHTML = `<div class="list-head"><div class="muted">搜索中…</div></div>`;
    try {
      const r = await api("api/search?" + new URLSearchParams({ q }));
      S.terms = r.terms || [];
      drawList(`搜索：${q}`, r.total > r.items.length ? `找到 ${r.total} 场，显示最相关的 ${r.items.length} 场` : `找到 ${r.total} 场`, r.items, r.total, true);
    } catch (e) { el.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; }
  }

  // ------------------------------------------------------------ 预览
  async function openMeeting(id) {
    S.cur = id; S.tab = "summary";
    $$(".item").forEach((it) => it.classList.toggle("on", +it.dataset.id === id));
    $("#kb").classList.add("show-doc");
    $("#preview").innerHTML = `<div class="empty-state muted">加载中…</div>`;
    try { S.doc = await api("api/meetings/" + id); } catch (e) { $("#preview").innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; return; }
    renderPreview();
  }
  function renderPreview() {
    const el = $("#preview"); if (!el) return;
    const m = S.doc;
    if (!m || m.id !== S.cur) { el.innerHTML = `<div class="empty-state"><div><div class="big">📄</div>选择一场会议查看摘要和原文</div></div>`; return; }
    const todos = (m.todos || "").split(/[;；\n]/).map((x) => x.trim()).filter(Boolean);
    let h = `<div class="doc"><button class="btn sm back" id="back">← 返回列表</button>
      <h1 class="dt">${mark(esc(m.title))}</h1>
      <div class="facts"><span><b>时间</b>${esc(m.start_time || "日期不详")}</span>${m.creator ? `<span><b>创建人</b>${esc(m.creator)}</span>` : ""}${m.org === "历史文件夹" ? `<span><b>来源</b>历史文件夹</span>` : ""}${m.duration ? `<span><b>时长</b>${fmtDur(m.duration)}</span>` : ""}<span><b>主体</b>${esc(m.entity || "—")}</span></div>
      <div class="tags" style="margin-top:8px">${(m.folders || []).map(folderTag).join("")}${m.mtype && m.mtype !== "其他" ? `<span class="tag">${esc(m.mtype)}</span>` : ""}${m.sensitive ? `<span class="tag bad">敏感</span>` : ""}${m.status === "pending" ? `<span class="tag warn">待确认</span>` : ""}${m.keywords ? m.keywords.split(/[,，、]/).slice(0, 8).map((k) => `<span class="tag ok">${esc(k.trim())}</span>`).join("") : ""}</div>`;
    if (S.me.is_admin) h += classifyBox(m);
    h += `<div class="tabs"><button data-t="summary" class="${S.tab === "summary" ? "on" : ""}">摘要</button><button data-t="transcript" class="${S.tab === "transcript" ? "on" : ""}">原文</button><button data-t="todos" class="${S.tab === "todos" ? "on" : ""}">待办${todos.length ? ` (${todos.length})` : ""}</button></div>`;
    if (S.tab === "summary") h += m.summary ? `<div class="md">${mark(md(m.summary))}</div>` : `<div class="muted">这场会没有 AI 摘要，可以看原文。</div>`;
    if (S.tab === "transcript") {
      if (m.truncated) h += `<div class="notice warn">${m.org === "历史文件夹" ? "这份原文在以前整理的 Word 里就只保存了一部分，后半段缺失。" : "原文太长，钉钉导出的 Excel 只保存了前 32,767 个字，后半段缺失。"}</div>`;
      h += m.transcript ? `<div class="transcript">${mark(transcriptHtml(m.transcript))}</div>` : `<div class="muted">没有转写原文。</div>`;
    }
    if (S.tab === "todos") h += todos.length ? `<ol class="todos md">${todos.map((t) => `<li>${mark(esc(t))}</li>`).join("")}</ol>` : `<div class="muted">没有待办事项。</div>`;
    h += `</div>`;
    el.innerHTML = h; el.scrollTop = 0;
    $$(".tabs button", el).forEach((b) => b.onclick = () => { S.tab = b.dataset.t; renderPreview(); });
    $("#back", el).onclick = () => $("#kb").classList.remove("show-doc");
    if (S.me.is_admin) bindClassify(m);
  }
  function classifyBox(m) {
    const all = ["研发市场销售", "人事", "财务", "IT", "董秘办", "每日博士", "大健康", "均瑶云商", "均瑶润盈", "衢州食品", "均瑶宜昌", "泛缘"];
    const by = { rule: "规则", llm: "大模型", manual: "人工" }[m.classified_by] || "";
    return `<div class="classify"><div class="h">归类${m.status === "pending" ? `<span class="tag warn">待确认</span>` : ""}<span class="muted" style="font-weight:400;font-size:12.5px">只有管理员能看到这块</span></div>
      <div class="reason">${by ? by + "判断" : ""}${m.confidence != null ? `（把握 ${Math.round(m.confidence * 100)}%）` : ""}：${esc(m.reason || "—")}</div>
      <div class="checks">${all.map((f) => `<label><input type="checkbox" name="cf" value="${f}" ${(m.folders || []).includes(f) ? "checked" : ""}>${f}</label>`).join("")}</div>
      <div class="checks"><label><input type="checkbox" id="cfSens" ${m.sensitive ? "checked" : ""}>敏感（面试、私人内容等，不会出现在研发市场销售）</label></div>
      <button class="btn sm primary" id="cfSave">${m.status === "pending" ? "保存并确认" : "保存归类"}</button>
      ${m.status === "ok" ? `<button class="btn sm" id="cfPend">改回待确认</button>` : ""}</div>`;
  }
  function bindClassify(m) {
    const save = async (status) => {
      const folders = $$('input[name="cf"]:checked').map((c) => c.value);
      const sensitive = $("#cfSens").checked;
      try {
        S.doc = await api("api/meetings/" + m.id, { method: "PUT", json: { folders, sensitive, status } });
        toast(status === "pending" ? "已改回待确认" : "已保存"); renderPreview(); await loadTree();
        if (S.q) doSearch(S.q, true); else loadList();
      } catch (e) { toast(e.message, true); }
    };
    $("#cfSave").onclick = () => save("ok");
    const p = $("#cfPend"); if (p) p.onclick = () => save("pending");
  }

  // ------------------------------------------------------------ 管理
  let pollTimer = null;
  function renderAdmin() {
    clearTimeout(pollTimer);
    const tabs = [["import", "导入听记"], ["members", "人员权限"], ["model", "模型与分类"], ["audit", "操作记录"]];
    app.innerHTML = `<div class="admin"><div class="admin-inner"><div class="subnav">${tabs.map(([k, n]) => `<button data-k="${k}" class="${S.adminTab === k ? "on" : ""}">${n}</button>`).join("")}</div><div id="adm"></div></div></div>`;
    $$(".subnav button").forEach((b) => b.onclick = () => { S.adminTab = b.dataset.k; renderAdmin(); });
    ({ import: admImport, members: admMembers, model: admModel, audit: admAudit })[S.adminTab]();
  }

  async function admImport() {
    const el = $("#adm");
    el.innerHTML = `<div class="card"><h3>上传听记 Excel</h3>
      <p class="desc">从钉钉 AI听记导出的 Excel（工作表「听记汇总」）。每次传新的就行，重复的会按听记链接自动跳过；只收 2026-08-14 以后、名单内主体的听记。<br>也可以传「历史听记包」（.json.gz，由以前手工整理的 Word 文件夹生成，文件夹归属原样保留）。</p>
      <label class="drop" id="drop"><input type="file" id="file" accept=".xlsx,.gz,.json" hidden><div style="font-size:28px">📤</div><div>点击选择文件，或把 Excel 拖到这里</div></label></div>
      <div class="card"><h3>导入记录</h3><div id="batches" class="muted">加载中…</div></div>`;
    const drop = $("#drop"), file = $("#file");
    file.onchange = () => file.files[0] && upload(file.files[0]);
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
    drop.ondragleave = () => drop.classList.remove("over");
    drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]); };
    await loadBatches();
  }
  async function upload(f) {
    const fd = new FormData(); fd.append("file", f);
    toast("上传中…");
    try { await api("api/imports", { method: "POST", body: fd }); toast("已开始导入"); loadBatches(); }
    catch (e) { toast(e.message, true); }
  }
  const STAT = [["new", "新增"], ["merged", "合并重复"], ["dup_existing", "以前导过"], ["dup_in_file", "表内重复"], ["empty", "空记录"], ["truncated", "原文被截断"],
    ["pending", "待确认"], ["no_date", "日期不详"], ["after_start", "8月14日以后（以Excel为准）"], ["before_start", "8月14日以前"], ["other_org", "名单外主体"], ["no_title", "无标题/时间的行"]];
  async function loadBatches() {
    clearTimeout(pollTimer);
    const el = $("#batches"); if (!el) return;
    let r; try { r = await api("api/imports"); } catch (e) { el.textContent = e.message; return; }
    if (!r.items.length) { el.innerHTML = "还没有导入过"; return; }
    el.innerHTML = `<table class="grid"><tr><th style="width:150px">时间</th><th>文件</th><th>结果</th></tr>${r.items.map((b) => {
      let res = "";
      if (b.status === "running") {
        const p = b.progress || {}; const pct = p.total ? Math.round(p.done / p.total * 100) : 5;
        res = `<div>${esc(p.step || "准备")} ${p.total ? `${p.done}/${p.total}` : ""}</div><div class="progress"><div style="width:${pct}%"></div></div>`;
      } else if (b.status === "failed") res = `<span class="tag bad">失败</span> ${esc(b.error || "")}`;
      else {
        const s = b.stats || {};
        res = `<div class="stats">${STAT.filter(([k]) => s[k]).map(([k, n]) => `<span>${n} <b>${s[k]}</b></span>`).join("")}</div>`
          + (s.skipped_orgs && Object.keys(s.skipped_orgs).length ? `<div class="muted" style="font-size:12px">名单外：${esc(Object.entries(s.skipped_orgs).map(([o, n]) => `${o} ${n}`).join("、"))}</div>` : "")
          + `<div class="muted" style="font-size:12px">${s.history ? "历史文件夹：按你手工分的文件夹归类" : s.llm ? "已用大模型分类" : "只用规则分类（还没配大模型）"}${s.empty ? ` · <a href="#" data-empty="${b.id}">看空记录</a>` : ""}</div>`;
      }
      return `<tr><td>${esc(b.created_at)}<div class="muted">${esc(b.uploaded_by || "")}</div></td><td>${esc(b.filename)}</td><td>${res}</td></tr>`;
    }).join("")}</table>`;
    $$("[data-empty]", el).forEach((a) => a.onclick = async (e) => {
      e.preventDefault();
      const x = await api(`api/imports/${a.dataset.empty}/empty`);
      const m = $("#modal");
      m.innerHTML = `<div class="box" style="width:min(640px,94vw)"><h3>空记录（${x.items.length} 场，摘要和原文都几乎为空，不进知识库）</h3><div style="max-height:55vh;overflow:auto;font-size:13px">${x.items.map((i) => `<div style="padding:4px 0;border-bottom:1px solid var(--line)">${esc(i.start_time)} · ${esc(i.creator || "")} · ${esc(i.title)}</div>`).join("")}</div><div class="acts"><button class="btn" data-a="0">关闭</button></div></div>`;
      m.hidden = false; m.onclick = (ev) => { if (ev.target.dataset.a !== undefined || ev.target === m) m.hidden = true; };
    });
    if (r.items.some((b) => b.status === "running")) pollTimer = setTimeout(loadBatches, 2000);
  }

  async function admMembers() {
    const el = $("#adm");
    el.innerHTML = `<div class="card"><h3>人员权限</h3>
      <p class="desc">谁能打开这个应用，在<b>企业中台</b>里设置（应用的可见范围：董事局 + 你指定的部门负责人）。打开以后能看什么按下面的规则：<br>
      · 中台「组织」里映射为<b>董事局</b>的人：全部可见<br>· 其他人：只看「研发市场销售」<br>· 需要例外时，在这里单独改。名单是打开过本应用的人。</p><div id="mem" class="muted">加载中…</div></div>`;
    const draw = (r) => {
      $("#mem").innerHTML = `<table class="grid"><tr><th>姓名</th><th>中台部门</th><th>现在能看</th><th>单独设置</th><th>最近打开</th></tr>${r.items.map((m) => `<tr>
        <td>${esc(m.name)}${m.user_id === S.me.id ? ` <span class="tag">我</span>` : ""}</td><td>${esc(m.dept === "board" ? "董事局" : m.dept || "—")}</td>
        <td>${esc(m.effective_label)}</td>
        <td><select class="role" data-uid="${esc(m.user_id)}"><option value="">按部门自动</option>${Object.entries(r.roles).map(([k, n]) => `<option value="${k}" ${m.role === k ? "selected" : ""}>${n}</option>`).join("")}</select></td>
        <td class="muted">${esc(m.last_seen || "")}</td></tr>`).join("")}</table>`;
      $$("select.role").forEach((s) => s.onchange = async () => {
        try { draw(await api("api/admin/members/" + encodeURIComponent(s.dataset.uid), { method: "PUT", json: { role: s.value } })); toast("已保存"); }
        catch (e) { toast(e.message, true); admMembers(); }
      });
    };
    try { draw(await api("api/admin/members")); } catch (e) { $("#mem").textContent = e.message; }
  }

  async function admModel() {
    const el = $("#adm");
    let r; try { r = await api("api/admin/settings"); } catch (e) { el.textContent = e.message; return; }
    const L = r.llm;
    el.innerHTML = `<div class="card"><h3>大模型</h3>
      <p class="desc">本应用单独接大模型（兼容 OpenAI 接口），用来给会议分类，后面的提问、周报月报也用它。密钥只存在本应用的数据库里，页面上不会再显示完整密钥。</p>
      <div class="form-row"><label>接口地址</label><input id="mUrl" value="${esc(L.llm_base_url)}" placeholder="例如 https://api.jiceapi.com/v1"></div>
      <div class="form-row"><label>模型</label><input id="mModel" value="${esc(L.llm_model)}" placeholder="例如 deepseek-v4-flash"></div>
      <div class="form-row"><label>密钥</label><input id="mKey" type="password" placeholder="${L.has_key ? "已填写（" + esc(L.key_hint) + "），留空表示不修改" : "sk-…"}"></div>
      <div class="form-row"><label>分类用的模型</label><input id="mCls" value="${esc(L.llm_model_classify)}" placeholder="留空 = 和上面一样"></div>
      <div class="form-row"><label></label><div style="display:flex;gap:8px"><button class="btn primary" id="mSave">保存</button><button class="btn" id="mTest">测试连接</button><span id="mRes" class="muted" style="align-self:center"></span></div></div></div>
      <div class="card"><h3>子公司的会是否也放进「研发市场销售」</h3>
      <p class="desc">勾选的子公司，业务会（不含面试、人事、财务类）会同时放进「研发市场销售」，部门负责人能看到。默认是大健康、均瑶云商（和你之前手工整理的一致）。</p>
      <div class="checks">${r.subs.map((s) => `<label><input type="checkbox" name="bs" value="${s}" ${r.biz_subs.includes(s) ? "checked" : ""}>${s}</label>`).join("")}</div>
      <button class="btn primary" id="bsSave">保存</button></div>
      <div class="card"><h3>重新分类</h3>
      <p class="desc">按现在的规则和大模型，把所有会议重新分一遍（手动调整过的不动）。配好大模型以后、或者改了上面的设置以后用。</p>
      <div id="rcState" class="muted" style="margin-bottom:10px"></div><button class="btn primary" id="rcGo">开始重新分类</button></div>`;
    $("#mSave").onclick = async () => {
      try { await api("api/admin/settings", { method: "PUT", json: { llm: { llm_base_url: $("#mUrl").value, llm_model: $("#mModel").value, llm_api_key: $("#mKey").value, llm_model_classify: $("#mCls").value } } }); toast("已保存"); admModel(); }
      catch (e) { toast(e.message, true); }
    };
    $("#mTest").onclick = async () => {
      $("#mRes").textContent = "测试中…";
      try { const t = await api("api/admin/llm-test", { method: "POST" }); $("#mRes").textContent = t.ok ? `连接成功（${t.seconds} 秒）：${t.reply}` : t.error; }
      catch (e) { $("#mRes").textContent = e.message; }
    };
    $("#bsSave").onclick = async () => {
      try { await api("api/admin/settings", { method: "PUT", json: { biz_subs: $$('input[name="bs"]:checked').map((c) => c.value) } }); toast("已保存，点“开始重新分类”后对已有会议生效"); }
      catch (e) { toast(e.message, true); }
    };
    $("#rcGo").onclick = async () => {
      if (!(await ask("重新分类", "会把所有没手动调整过的会议重新分一遍，大模型分类大约每场 1～3 秒。期间可以正常使用。", "开始"))) return;
      try { const x = await api("api/admin/reclassify", { method: "POST" }); toast(`开始重新分类 ${x.total} 场`); pollRc(); }
      catch (e) { toast(e.message, true); }
    };
    showRc(r.reclassify);
    if (r.reclassify && r.reclassify.running) pollRc();
  }
  function showRc(st) {
    const el = $("#rcState"); if (!el) return;
    if (!st || !Object.keys(st).length) { el.textContent = "还没有重新分类过"; return; }
    if (st.running) el.innerHTML = `正在分类 ${st.done}/${st.total}<div class="progress"><div style="width:${st.total ? st.done / st.total * 100 : 3}%"></div></div>`;
    else if (st.error) el.innerHTML = `<span class="tag bad">中断</span> ${esc(st.error)}`;
    else { const s = st.stats || {}; el.textContent = `上次：${st.finished}，${st.total} 场，${st.llm ? "用了大模型" : "只用规则"}；待确认 ${s.pending || 0} 场`; }
  }
  async function pollRc() {
    clearTimeout(pollTimer);
    try { const r = await api("api/admin/settings"); showRc(r.reclassify); if (r.reclassify && r.reclassify.running && S.adminTab === "model" && S.view === "admin") pollTimer = setTimeout(pollRc, 2000); }
    catch { /* 忽略 */ }
  }

  async function admAudit() {
    const el = $("#adm");
    try {
      const r = await api("api/admin/audit");
      el.innerHTML = `<div class="card"><h3>操作记录</h3><table class="grid"><tr><th style="width:160px">时间</th><th style="width:110px">人</th><th style="width:140px">操作</th><th>内容</th></tr>${r.items.map((a) => `<tr><td class="muted">${esc(a.created_at)}</td><td>${esc(a.user_name)}</td><td>${esc(a.action)}</td><td>${esc(a.detail)}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">暂无</td></tr>`}</table></div>`;
    } catch (e) { el.textContent = e.message; }
  }

  boot();
})();
