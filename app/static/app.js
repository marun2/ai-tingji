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
  const plain = (t) => String(t || "").replace(/\*\*|__|`/g, "").replace(/^\s*(#{1,6}|>|[-*+]|\d+[.、)])\s+/gm, "").replace(/\s*\n\s*/g, " ").trim();
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
    go("ask");
  }
  function go(view) {
    S.view = view;
    $$(".nav button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
    if (view === "admin") renderAdmin(); else if (view === "ask") renderAsk(); else if (view === "reports") renderReports(); else if (view === "insight") renderInsight(); else renderKb();
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
    let h = `<div class="list-head"><div class="t">${esc(title)}${!isSearch && !S.sel.pending && total ? `<a class="btn sm export" href="${exportUrl({ folder: S.sel.folder, month: S.sel.month })}" title="每场会一个 Word，打包成 zip">导出 Word</a>` : ""}</div><div class="s">${esc(desc)}</div>`;
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
  function exportUrl(p) { const q = new URLSearchParams(); for (const [k, v] of Object.entries(p)) if (v) q.set(k, v); return "api/export?" + q; }
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
      <div class="doc-acts"><a class="btn sm" href="${exportUrl({ ids: m.id })}">导出 Word</a></div><h1 class="dt">${mark(esc(m.title))}</h1>
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


  // ------------------------------------------------------------ 提问
  const ASK = { thread: [], busy: false, folder: "" };
  const SUGGEST = ["最近一个月各区域 CNY 准备得怎么样？", "我们和李子园比，会上提到过哪些差距？", "盒马相关的会议里定了哪些事？", "上周开了哪些会？"];
  function renderAsk() {
    const folders = S.me.folders || [];
    app.innerHTML = `<div class="ask"><div class="ask-inner">
      <div class="ask-hero" ${ASK.thread.length ? "hidden" : ""}><div class="ask-logo">记</div><h2>问问会议记录</h2>
        <p>根据${S.me.see_all ? "全公司" : "「研发市场销售」"}的会议摘要和原文回答，每个要点都标出处，点一下就能打开那场会。</p>
        <div class="chips">${SUGGEST.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div></div>
      <div id="thread"></div></div>
      <form class="ask-bar" id="askForm"><select id="askScope" title="在哪些会议里找">${S.me.see_all ? `<option value="">全部会议</option>` : ""}${folders.map((f) => `<option value="${esc(f)}" ${ASK.folder === f ? "selected" : ""}>${esc(f)}</option>`).join("")}</select>
        <textarea id="askInput" rows="1" placeholder="输入问题，例如：浙江区域 CNY 的回款目标是多少？"></textarea>
        <button class="btn primary" id="askGo">提问</button>${ASK.thread.length ? `<button type="button" class="btn" id="askNew" title="清空对话，开始新话题">新话题</button>` : ""}</form></div>`;
    drawThread();
    const inp = $("#askInput");
    inp.oninput = () => { inp.style.height = "auto"; inp.style.height = Math.min(inp.scrollHeight, 160) + "px"; };
    inp.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#askForm").requestSubmit(); } };
    $("#askScope").onchange = (e) => { ASK.folder = e.target.value; };
    $("#askForm").onsubmit = (e) => { e.preventDefault(); sendAsk(inp.value); };
    $$(".chip").forEach((c) => c.onclick = () => sendAsk(c.dataset.q));
    const nw = $("#askNew"); if (nw) nw.onclick = () => { ASK.thread = []; renderAsk(); };
    inp.focus();
  }
  function answerHtml(t, sources) {
    const by = Object.fromEntries((sources || []).map((s) => [s.n, s]));
    return md(t).replace(/\[(\d{1,2})\]/g, (m0, n) => by[n] ? `<a class="cite" data-n="${n}" title="${esc(by[n].title)}">${n}</a>` : m0);
  }
  function drawThread() {
    const el = $("#thread"); if (!el) return;
    el.innerHTML = ASK.thread.map((t, i) => {
      let a;
      if (t.pending) a = `<div class="a pending"><span class="dots"><i></i><i></i><i></i></span>正在翻会议记录…</div>`;
      else if (t.error) a = `<div class="a"><div class="notice warn">${esc(t.error)}</div></div>`;
      else {
        let cited = t.sources.filter((s) => s.cited);
        if (!cited.length) cited = t.sources.slice(0, 8);
        const src = cited.length ? `<div class="srcs"><div class="srcs-h">出处 · ${cited.length} 处</div>${cited.map((s) => `<div class="src" data-i="${i}" data-n="${s.n}"><span class="n">${s.n}</span><div><div class="st">${esc(s.title)}</div><div class="sm">${esc(s.date || "日期不详")} · ${s.kind === "summary" ? "摘要" : "原文"}</div><div class="sx">${esc(plain(s.text).slice(0, 120))}…</div></div></div>`).join("")}</div>` : "";
        a = `<div class="a"><div class="md">${answerHtml(t.answer, t.sources)}</div>${src}
          <div class="a-note">回答里的数字和说法来自会议发言，未经核实；重要决定请点出处看原文。</div><div class="a-foot"><span class="muted">${t.seconds} 秒${t.vector ? "" : " · 只用了关键词检索"}${t.range && (t.range[0] || t.range[1]) ? ` · 时间 ${t.range[0] || "…"} ~ ${t.range[1] || "今天"}` : ""}</span>
          <button class="rate ${t.rating === 1 ? "on" : ""}" data-i="${i}" data-r="1" title="有用">👍</button><button class="rate ${t.rating === -1 ? "on" : ""}" data-i="${i}" data-r="-1" title="没用">👎</button></div></div>`;
      }
      return `<div class="turn"><div class="q">${esc(t.q)}</div>${a}</div>`;
    }).join("");
    $$(".cite", el).forEach((c) => c.onclick = () => { const turn = c.closest(".turn"); const i = [...el.children].indexOf(turn); openSource(i, +c.dataset.n); });
    $$(".src", el).forEach((c) => c.onclick = () => openSource(+c.dataset.i, +c.dataset.n));
    $$(".rate", el).forEach((b) => b.onclick = () => rateAnswer(+b.dataset.i, +b.dataset.r));
    const last = el.lastElementChild; if (last) last.scrollIntoView({ block: "end", behavior: "smooth" });
  }
  async function sendAsk(q) {
    q = (q || "").trim(); if (!q || ASK.busy) return;
    ASK.busy = true;
    const history = ASK.thread.filter((t) => t.answer).slice(-3).map((t) => ({ q: t.q, a: t.answer }));
    const turn = { q, pending: true }; ASK.thread.push(turn);
    if (ASK.thread.length === 1) renderAsk(); else { drawThread(); }
    $("#askInput").value = "";
    try { Object.assign(turn, await api("api/ask", { method: "POST", json: { question: q, folder: ASK.folder, history } }), { pending: false }); }
    catch (e) { Object.assign(turn, { pending: false, error: e.message }); }
    ASK.busy = false;
    if (ASK.thread.length === 1) renderAsk(); else drawThread();
  }
  async function rateAnswer(i, r) {
    const t = ASK.thread[i]; if (!t || !t.id) return;
    let comment = "";
    if (r === -1) comment = await askText("哪里不好？", "比如：答非所问、漏了某场会、数字不对。可以不填。");
    if (comment === null) return;
    try { await api(`api/ask/${t.id}/rate`, { method: "POST", json: { rating: r, comment } }); t.rating = r; drawThread(); toast(r > 0 ? "谢谢反馈" : "收到，我们会改进"); }
    catch (e) { toast(e.message, true); }
  }
  function askText(title, hint) {
    return new Promise((res) => {
      const m = $("#modal");
      m.innerHTML = `<div class="box"><h3>${esc(title)}</h3><textarea id="mt" rows="3" style="width:100%;border:1px solid var(--line-strong);border-radius:8px;padding:8px" placeholder="${esc(hint)}"></textarea>
        <div class="acts"><button class="btn" data-a="0">取消</button><button class="btn primary" data-a="1">提交</button></div></div>`;
      m.hidden = false; $("#mt").focus();
      m.onclick = (e) => { const a = e.target.dataset.a; if (a !== undefined || e.target === m) { m.hidden = true; res(a === "1" ? $("#mt").value : a === undefined ? null : null); } };
    });
  }
  async function openSource(i, n) {
    const t = ASK.thread[i]; const s = t && t.sources.find((x) => x.n === n); if (!s) return;
    openMeetingModal(s.meeting_id, s);
  }
  async function openMeetingModal(meetingId, s) {
    s = s || { text: "", kind: "summary" };
    let m;
    try { m = await api("api/meetings/" + meetingId); } catch (e) { toast(e.message, true); return; }
    const box = $("#modal");
    const hl = esc(s.text.slice(0, 60));
    box.innerHTML = `<div class="box doc-modal"><div class="dm-head"><div><div class="dm-t">${esc(m.title)}</div><div class="muted" style="font-size:12.5px">${esc(m.start_time || "日期不详")} · ${(m.folders || []).join("、")}</div></div>
      <div style="display:flex;gap:6px"><a class="btn sm" href="${exportUrl({ ids: m.id })}">导出 Word</a><button class="btn sm" data-a="0">关闭</button></div></div>
      ${s.text ? `<div class="notice info">引用的这段（${s.kind === "summary" ? "摘要" : "原文"}）：${esc(plain(s.text).slice(0, 300))}${s.text.length > 300 ? "…" : ""}</div>` : ""}
      <div class="tabs"><button data-t="summary" class="${s.kind === "summary" ? "on" : ""}">摘要</button><button data-t="transcript" class="${s.kind !== "summary" ? "on" : ""}">原文</button></div><div id="dmBody"></div></div>`;
    box.hidden = false;
    const show = (k) => {
      $$(".doc-modal .tabs button").forEach((b) => b.classList.toggle("on", b.dataset.t === k));
      $("#dmBody").innerHTML = k === "summary" ? (m.summary ? `<div class="md">${md(m.summary)}</div>` : `<div class="muted">没有摘要</div>`) : (m.transcript ? `<div class="transcript">${transcriptHtml(m.transcript)}</div>` : `<div class="muted">没有原文</div>`);
      // 滚到引用的位置
      const key = (s.text || "").split("\n").map((x) => plain(x.replace(/^\[[^\]]+\]\s*/, "")).split(/[：:]/).pop().trim()).find((x) => x.length > 8);
      if (key) { const hit = [...$("#dmBody").querySelectorAll("p,li,h2,h3,h4,blockquote")].find((p) => p.textContent.includes(key.slice(0, 12))); if (hit) { hit.classList.add("hit"); setTimeout(() => hit.scrollIntoView({ block: "center" }), 50); } }
    };
    $$(".doc-modal .tabs button").forEach((b) => b.onclick = () => show(b.dataset.t));
    show(s.kind === "summary" ? "summary" : "transcript");
    box.onclick = (e) => { if (e.target.dataset.a === "0" || e.target === box) box.hidden = true; };
  }



  // ------------------------------------------------------------ 洞察（可视化）
  const TCOLOR = { 部门: "#1d3f78", 客户: "#2f5bea", 产品: "#0e9f6e", 竞品: "#e0453a", 项目: "#c26b04", 区域: "#7c3aed", 话题: "#0891b2" };
  const INS = { tab: "cockpit", days: 90, types: ["客户", "产品", "竞品", "项目", "区域", "话题"], riverKind: "话题", raf: null };
  const svgEl = (tag, attrs = {}) => { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  function renderInsight() {
    cancelAnimationFrame(INS.raf);
    const tabs = [["cockpit", "经营驾驶舱"], ["graph", "知识图谱"], ["collab", "部门协作"], ["river", "话题河流"]];
    app.innerHTML = `<div class="ins"><div class="ins-bar"><div class="subnav">${tabs.map(([k, n]) => `<button data-k="${k}" class="${INS.tab === k ? "on" : ""}">${n}</button>`).join("")}</div><div id="insOpts" class="ins-opts"></div></div><div id="insBody" class="ins-body"></div></div>`;
    $$(".ins-bar .subnav button").forEach((b) => b.onclick = () => { INS.tab = b.dataset.k; renderInsight(); });
    ({ cockpit: insCockpit, graph: insGraph, collab: insCollab, river: insRiver })[INS.tab]();
  }
  const daysSel = () => `<select id="insDays">${[[30, "近 30 天"], [90, "近 90 天"], [180, "近半年"], [365, "近一年"]].map(([d, n]) => `<option value="${d}" ${INS.days === d ? "selected" : ""}>${n}</option>`).join("")}</select>`;
  function bindDays(fn) { const d = $("#insDays"); if (d) d.onchange = () => { INS.days = +d.value; fn(); }; }
  const emptyIns = (msg) => `<div class="empty-state"><div><div class="big">✨</div>${msg}</div></div>`;

  async function insCockpit() {
    $("#insOpts").innerHTML = "";
    const el = $("#insBody"); el.innerHTML = emptyIns("加载中…");
    let d; try { d = await api("api/insights/cockpit"); } catch (e) { el.innerHTML = emptyIns(esc(e.message)); return; }
    const mx = Math.max(1, ...d.trend);
    const W = 560, H = 150, bw = W / d.trend.length;
    const bars = d.trend.map((v, i) => `<rect x="${i * bw + 6}" y="${H - 20 - (v / mx) * (H - 40)}" width="${bw - 12}" height="${(v / mx) * (H - 40)}" rx="4" fill="${i === d.trend.length - 1 ? "#2f5bea" : "#b9c8f5"}"><title>${d.trend_labels[i]} 那周：${v} 场</title></rect><text x="${i * bw + bw / 2}" y="${H - 5}" text-anchor="middle" font-size="10" fill="#8b94a5">${d.trend_labels[i]}</text>${v ? `<text x="${i * bw + bw / 2}" y="${H - 24 - (v / mx) * (H - 40)}" text-anchor="middle" font-size="10" fill="#4f596b">${v}</text>` : ""}`).join("");
    const delta = d.this_week - d.last_week;
    const list = (items, fmt) => items.length ? items.map((x) => `<div class="li" data-m="${x.meeting_id}">${fmt(x)}<div class="lm">${esc((x.start_time || "").slice(5, 10))} · ${esc(x.title)}</div></div>`).join("") : `<div class="muted">近两周暂无</div>`;
    el.innerHTML = `<div class="cockpit">
      <div class="kpis"><div class="kpi"><div class="kl">本周会议</div><div class="kn">${d.this_week}</div><div class="kd ${delta >= 0 ? "up" : "down"}">${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta)} 较上周</div></div>
        <div class="kpi"><div class="kl">上周会议</div><div class="kn">${d.last_week}</div></div>
        <div class="kpi"><div class="kl">最近一场会</div><div class="kn sm">${esc((d.latest_meeting || "—").slice(0, 10))}</div></div>
        <div class="kpi"><div class="kl">最近导入</div><div class="kn sm">${esc((d.last_import || "还没导入").slice(0, 10))}</div>${d.pending ? `<div class="kd warn">待确认 ${d.pending} 场</div>` : ""}</div></div>
      <div class="cgrid">
        <div class="card"><h3>近 12 周会议数</h3><svg viewBox="0 0 ${W} ${H}" class="trend">${bars}</svg></div>
        <div class="card"><h3>升温最快</h3><p class="desc">最近两周比之前六周提得明显更多的客户、产品、话题</p>${d.rising.length ? d.rising.map((r) => `<div class="rise"><span class="dot" style="background:${TCOLOR[r.type] || "#888"}"></span><span class="rn">${esc(r.name)}</span><span class="muted">${r.type}</span><span class="rb"><i style="width:${Math.min(100, r.lift * 40)}%"></i></span><span class="rv">${r.recent} 场</span></div>`).join("") : `<div class="muted">数据还不够</div>`}</div>
        <div class="card"><h3>⚠️ 近两周的风险与问题</h3>${list(d.risks, (x) => esc(x.text))}</div>
        <div class="card"><h3>✅ 近两周的决议</h3>${list(d.decisions, (x) => esc(x.text))}</div>
        <div class="card wide"><h3>📈 近两周会上提到的数字</h3><p class="desc">来自会议发言，未经财务核实</p>${list(d.metrics, (x) => `<b>${esc(x.label)}</b> ${esc(x.value)}${esc(x.unit || "")} <span class="muted">${esc(x.text)}</span>`)}</div>
      </div></div>`;
    $$(".li[data-m]", el).forEach((x) => x.onclick = () => openMeetingModal(+x.dataset.m));
  }

  async function insGraph() {
    $("#insOpts").innerHTML = daysSel() + `<span class="legend">${Object.entries(TCOLOR).map(([t, c]) => `<label class="lg ${t === "部门" || INS.types.includes(t) ? "" : "off"}" data-t="${t}"><i style="background:${c}"></i>${t}</label>`).join("")}</span>`;
    bindDays(insGraph);
    $$(".lg").forEach((l) => l.onclick = () => { const t = l.dataset.t; if (t === "部门") return; INS.types = INS.types.includes(t) ? INS.types.filter((x) => x !== t) : [...INS.types, t]; insGraph(); });
    const el = $("#insBody"); el.innerHTML = emptyIns("加载中…");
    let d; try { d = await api("api/insights/graph?" + new URLSearchParams({ days: INS.days, types: INS.types.join(",") })); } catch (e) { el.innerHTML = emptyIns(esc(e.message)); return; }
    if (d.nodes.length < 2) { el.innerHTML = emptyIns("还没有足够的数据画图谱：需要先在「管理 → 模型与检索」里完成会议信息抽取"); return; }
    el.innerHTML = `<div class="graph-wrap"><svg id="gsvg"></svg><div class="gside" id="gside"><div class="muted">点任意一个点，看相关的会议。<br>滚轮缩放，拖动空白处平移。<br>近 ${d.days} 天 ${d.meetings} 场会。</div></div></div>`;
    forceGraph($("#gsvg"), d.nodes, d.edges, async (n) => {
      const side = $("#gside"); side.innerHTML = `<div class="gs-h"><span class="dot" style="background:${TCOLOR[n.type]}"></span>${esc(n.name)}<span class="muted">${n.type} · ${n.count} 场</span></div><div class="muted">加载中…</div>`;
      const r = await api("api/insights/node?" + new URLSearchParams({ id: n.id, days: INS.days }));
      side.innerHTML = `<div class="gs-h"><span class="dot" style="background:${TCOLOR[n.type]}"></span>${esc(n.name)}<span class="muted">${n.type} · ${n.count} 场</span></div>` +
        `<button class="btn sm" id="gAsk" style="margin:4px 0 10px">问问关于「${esc(n.name)}」</button>` +
        r.items.map((m) => `<div class="li" data-m="${m.id}"><div>${esc(m.title)}</div><div class="lm">${esc((m.start_time || "").slice(0, 10))}</div></div>`).join("");
      $$(".li", side).forEach((x) => x.onclick = () => openMeetingModal(+x.dataset.m));
      $("#gAsk").onclick = () => { go("ask"); setTimeout(() => sendAsk(`最近会议里关于「${n.name}」都讨论了什么？有哪些决定和问题？`), 50); };
    });
  }
  // 力导向布局（自己实现，不依赖外部库）：节点互相排斥、连线像弹簧、整体向中心收拢；一边算一边画，看起来是“动”的
  function forceGraph(svg, nodes, edges, onPick) {
    const W = svg.clientWidth || 900, H = svg.clientHeight || 600;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const maxC = Math.max(...nodes.map((n) => n.count));
    const N = nodes.map((n, i) => ({ ...n, r: n.type === "部门" ? 12 + 16 * Math.sqrt(n.count / maxC) : 5 + 14 * Math.sqrt(n.count / maxC),
      x: W / 2 + Math.cos(i * 2.4) * (60 + i * 3), y: H / 2 + Math.sin(i * 2.4) * (60 + i * 3), vx: 0, vy: 0 }));
    const idx = Object.fromEntries(N.map((n, i) => [n.id, i]));
    const maxW = Math.max(...edges.map((e) => e.w), 1);
    const E = edges.filter((e) => idx[e.s] !== undefined && idx[e.t] !== undefined).map((e) => ({ a: N[idx[e.s]], b: N[idx[e.t]], w: e.w }));
    const nb = N.map(() => new Set());
    E.forEach((e) => { nb[idx[e.a.id]].add(e.b.id); nb[idx[e.b.id]].add(e.a.id); });
    const view = { x: 0, y: 0, k: 1 };
    const g = svgEl("g"); svg.appendChild(g);
    const gl = svgEl("g"); const gn = svgEl("g"); g.appendChild(gl); g.appendChild(gn);
    const lines = E.map((e) => { const l = svgEl("line", { stroke: "#c3cbe0", "stroke-width": 0.6 + 3.4 * (e.w / maxW), "stroke-opacity": 0.55 }); gl.appendChild(l); return l; });
    const circles = N.map((n) => {
      const gg = svgEl("g", { class: "gnode" });
      gg.appendChild(svgEl("circle", { r: n.r, fill: TCOLOR[n.type] || "#888", "fill-opacity": n.type === "部门" ? 0.95 : 0.85, stroke: "#fff", "stroke-width": 1.5 }));
      const t = svgEl("text", { y: n.r + 12, "text-anchor": "middle", "font-size": n.type === "部门" ? 12.5 : 11, "font-weight": n.type === "部门" ? 600 : 400, fill: "#344054" });
      t.textContent = n.name; gg.appendChild(t);
      const title = svgEl("title"); title.textContent = `${n.name}（${n.type}）· ${n.count} 场会`; gg.appendChild(title);
      gg.onmouseenter = () => hl(n); gg.onmouseleave = () => hl(null);
      gg.onclick = (ev) => { ev.stopPropagation(); onPick(n); };
      gn.appendChild(gg); return gg;
    });
    function hl(n) {
      circles.forEach((c, i) => c.style.opacity = !n || N[i].id === n.id || nb[idx[n.id]].has(N[i].id) ? 1 : 0.15);
      lines.forEach((l, i) => l.style.strokeOpacity = !n ? 0.55 : (E[i].a.id === n.id || E[i].b.id === n.id ? 0.9 : 0.05));
    }
    let alpha = 1;
    const REP_K = Math.max(2500, (W * H) / Math.max(N.length, 1) * 0.9);   // 画布越大、点越少，排得越开
    function fit(animate) {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of N) { x0 = Math.min(x0, n.x - n.r - 40); y0 = Math.min(y0, n.y - n.r - 10); x1 = Math.max(x1, n.x + n.r + 40); y1 = Math.max(y1, n.y + n.r + 24); }
      const k = Math.min(2.2, W / (x1 - x0), H / (y1 - y0));
      view.k = k; view.x = (W - (x0 + x1) * k) / 2; view.y = (H - (y0 + y1) * k) / 2;
      if (animate) { g.style.transition = "transform .8s ease"; setTimeout(() => g.style.transition = "", 900); }
      apply();
    }
    function tick() {
      for (let i = 0; i < N.length; i++) for (let j = i + 1; j < N.length; j++) {
        const a = N[i], b = N[j]; let dx = b.x - a.x, dy = b.y - a.y; let d2 = dx * dx + dy * dy || 1;
        const minD = a.r + b.r + 26; const f = (REP_K / d2 + (d2 < minD * minD ? 1.2 : 0)) * alpha; const d = Math.sqrt(d2);
        dx /= d; dy /= d; a.vx -= dx * f; a.vy -= dy * f; b.vx += dx * f; b.vy += dy * f;
      }
      for (const e of E) {
        const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
        const target = 110 + e.a.r + e.b.r; const f = (d - target) * 0.012 * Math.min(1, 0.3 + e.w / maxW) * alpha;
        e.a.vx += dx / d * f; e.a.vy += dy / d * f; e.b.vx -= dx / d * f; e.b.vy -= dy / d * f;
      }
      for (const n of N) {
        n.vx += (W / 2 - n.x) * 0.004 * alpha; n.vy += (H / 2 - n.y) * 0.004 * alpha;
        n.vx *= 0.82; n.vy *= 0.82; n.x += n.vx; n.y += n.vy;
      }
      E.forEach((e, i) => { const l = lines[i]; l.setAttribute("x1", e.a.x); l.setAttribute("y1", e.a.y); l.setAttribute("x2", e.b.x); l.setAttribute("y2", e.b.y); });
      N.forEach((n, i) => circles[i].setAttribute("transform", `translate(${n.x},${n.y})`));
      alpha *= 0.985;
      if (alpha > 0.02 && document.body.contains(svg)) INS.raf = requestAnimationFrame(tick); else fit(true);
    }
    const apply = () => { g.style.transform = `translate(${view.x}px,${view.y}px) scale(${view.k})`; g.style.transformOrigin = "0 0"; };
    tick();
    svg.addEventListener("wheel", (e) => { e.preventDefault(); const k = Math.max(0.4, Math.min(3, view.k * (e.deltaY < 0 ? 1.1 : 0.9))); const r = svg.getBoundingClientRect(); const mx = (e.clientX - r.left) * W / r.width, my = (e.clientY - r.top) * H / r.height; view.x = mx - (mx - view.x) * k / view.k; view.y = my - (my - view.y) * k / view.k; view.k = k; apply(); }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", (e) => { if (e.target === svg) { drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }; svg.setPointerCapture(e.pointerId); } });
    svg.addEventListener("pointermove", (e) => { if (drag) { const r = svg.getBoundingClientRect(); view.x = drag.vx + (e.clientX - drag.x) * W / r.width; view.y = drag.vy + (e.clientY - drag.y) * H / r.height; apply(); } });
    svg.addEventListener("pointerup", () => { drag = null; });
  }

  async function insCollab() {
    $("#insOpts").innerHTML = daysSel(); bindDays(insCollab);
    const el = $("#insBody"); el.innerHTML = emptyIns("加载中…");
    let d; try { d = await api("api/insights/collab?" + new URLSearchParams({ days: INS.days })); } catch (e) { el.innerHTML = emptyIns(esc(e.message)); return; }
    if (d.folders.length < 2) { el.innerHTML = emptyIns("你能看到的只有一个文件夹，没有部门之间的关系可画"); return; }
    el.innerHTML = `<div class="graph-wrap"><svg id="csvg"></svg><div class="gside" id="cside"><div class="muted">圆越大，会越多；线越粗，两个部门一起开的会、共同关心的话题越多。<br>把鼠标放到线上看它们一起关心什么，点圆圈看这个部门的重点。</div></div></div>`;
    const svg = $("#csvg"); const W = svg.clientWidth || 900, H = svg.clientHeight || 600; svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const R = Math.min(W, H) / 2 - 70, cx = W / 2, cy = H / 2;
    const maxC = Math.max(...d.folders.map((f) => f.count)), maxW = Math.max(1, ...d.links.map((l) => l.w));
    const pos = {};
    d.folders.forEach((f, i) => { const a = -Math.PI / 2 + i * 2 * Math.PI / d.folders.length; pos[f.name] = { x: cx + R * Math.cos(a), y: cy + R * Math.sin(a), a, r: 10 + 26 * Math.sqrt(f.count / maxC), f }; });
    const gl = svgEl("g"); const gn = svgEl("g"); svg.appendChild(gl); svg.appendChild(gn);
    const col = (g) => g === "板块" ? "#2f5bea" : g === "上海食品" ? "#1d3f78" : "#0e9f6e";
    const side = $("#cside");
    const paths = d.links.map((l, i) => {
      const a = pos[l.s], b = pos[l.t]; if (!a || !b) return null;
      const p = svgEl("path", { d: `M${a.x},${a.y} Q${cx},${cy} ${b.x},${b.y}`, fill: "none", stroke: "url(#lg)", "stroke-width": 1 + 12 * (l.w / maxW), "stroke-opacity": 0.5, "stroke-linecap": "round", class: "clink" });
      p.style.strokeDasharray = "2000"; p.style.strokeDashoffset = "2000"; p.style.transition = `stroke-dashoffset 1.2s ease ${i * 0.04}s`;
      p.onmouseenter = () => { p.setAttribute("stroke-opacity", 0.9); side.innerHTML = `<div class="gs-h">${esc(l.s)} ⇄ ${esc(l.t)}</div><div>一起开过 <b>${l.meetings}</b> 场会，共同关心 <b>${l.common}</b> 个话题 / 客户 / 产品</div><div class="tags" style="margin-top:8px">${l.top.map((t) => `<span class="tag biz">${esc(t)}</span>`).join("")}</div>`; };
      p.onmouseleave = () => p.setAttribute("stroke-opacity", 0.5);
      gl.appendChild(p); return p;
    });
    const defs = svgEl("defs"); const lg = svgEl("linearGradient", { id: "lg" }); lg.appendChild(svgEl("stop", { offset: "0", "stop-color": "#2f5bea" })); lg.appendChild(svgEl("stop", { offset: "1", "stop-color": "#0891b2" })); defs.appendChild(lg); svg.insertBefore(defs, svg.firstChild);
    for (const p of Object.values(pos)) {
      const g = svgEl("g", { class: "gnode", transform: `translate(${p.x},${p.y})` });
      g.appendChild(svgEl("circle", { r: p.r, fill: col(p.f.group), "fill-opacity": 0.9, stroke: "#fff", "stroke-width": 2 }));
      const lx = Math.cos(p.a) * (p.r + 8), ly = Math.sin(p.a) * (p.r + 8);
      const t = svgEl("text", { x: lx, y: ly + 4, "text-anchor": Math.abs(Math.cos(p.a)) < 0.3 ? "middle" : Math.cos(p.a) > 0 ? "start" : "end", "font-size": 13, "font-weight": 600, fill: "#101828" }); t.textContent = `${p.f.name} ${p.f.count}`; g.appendChild(t);
      g.onclick = () => { side.innerHTML = `<div class="gs-h"><span class="dot" style="background:${col(p.f.group)}"></span>${esc(p.f.name)}<span class="muted">${p.f.count} 场</span></div><div class="muted" style="margin-bottom:6px">最常谈到</div><div class="tags">${p.f.top.map((t) => `<span class="tag biz">${esc(t)}</span>`).join("")}</div>`
        + `<div class="muted" style="margin:10px 0 6px">联系最紧的部门</div>` + d.links.filter((l) => l.s === p.f.name || l.t === p.f.name).slice(0, 5).map((l) => `<div class="li"><b>${esc(l.s === p.f.name ? l.t : l.s)}</b> · ${l.meetings} 场共同会议 · ${esc(l.top.slice(0, 3).join("、"))}</div>`).join(""); };
      gn.appendChild(g);
    }
    requestAnimationFrame(() => paths.forEach((p) => p && (p.style.strokeDashoffset = "0")));
  }

  async function insRiver() {
    $("#insOpts").innerHTML = `<select id="rk">${["话题", "客户", "产品", "竞品", "项目", "区域"].map((k) => `<option ${INS.riverKind === k ? "selected" : ""}>${k}</option>`).join("")}</select>`;
    $("#rk").onchange = (e) => { INS.riverKind = e.target.value; insRiver(); };
    const el = $("#insBody"); el.innerHTML = emptyIns("加载中…");
    let d; try { d = await api("api/insights/river?" + new URLSearchParams({ weeks: 16, kind: INS.riverKind })); } catch (e) { el.innerHTML = emptyIns(esc(e.message)); return; }
    if (!d.series.length) { el.innerHTML = emptyIns("还没有数据：需要先完成会议信息抽取"); return; }
    if (d.series.every((s) => !s.values[s.values.length - 1])) { d.weeks.pop(); d.series.forEach((s) => s.values.pop()); }  // 本周还没有会就不画
    el.innerHTML = `<div class="river-wrap"><svg id="rsvg"></svg><div class="rlegend">${d.series.map((s, i) => `<span class="lg" data-i="${i}"><i style="background:${riverColor(i)}"></i>${esc(s.name)} <span class="muted">${s.total}</span></span>`).join("")}</div><div id="rtip" class="rtip" hidden></div></div>`;
    const svg = $("#rsvg"); const W = svg.clientWidth || 1000, H = svg.clientHeight || 460; svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const n = d.weeks.length, pad = 40, x = (i) => pad + i * (W - 2 * pad) / (n - 1);
    const totals = d.weeks.map((_, i) => d.series.reduce((s, se) => s + se.values[i], 0));
    const maxT = Math.max(1, ...totals); const sc = (H - 70) / maxT;
    let base = d.weeks.map((_, i) => H / 2 - 15 - totals[i] * sc / 2);   // 居中堆叠（河流形状）
    const smooth = (pts) => pts.map((p, i) => i ? `C${(pts[i - 1][0] + p[0]) / 2},${pts[i - 1][1]} ${(pts[i - 1][0] + p[0]) / 2},${p[1]} ${p[0]},${p[1]}` : `M${p[0]},${p[1]}`).join(" ");
    const layers = d.series.map((s, k) => {
      const top = base.map((b, i) => [x(i), b]); const bot = base.map((b, i) => [x(i), b + s.values[i] * sc]);
      base = bot.map((p) => p[1]);
      const path = svgEl("path", { d: smooth(top) + " L" + smooth(bot.slice().reverse()).slice(1) + " Z", fill: riverColor(k), "fill-opacity": 0.85, class: "rl" });
      path.style.transformOrigin = `${W / 2}px ${H / 2}px`; path.style.animation = `grow .9s ease ${k * 0.07}s both`;
      path.onmousemove = (ev) => { const r = svg.getBoundingClientRect(); const i = Math.max(0, Math.min(n - 1, Math.round(((ev.clientX - r.left) * W / r.width - pad) / ((W - 2 * pad) / (n - 1))))); const tip = $("#rtip"); tip.hidden = false; tip.style.left = (ev.clientX - r.left + 12) + "px"; tip.style.top = (ev.clientY - r.top + 12) + "px"; tip.innerHTML = `<b>${esc(s.name)}</b><br>${d.weeks[i]} 那周：${s.values[i]} 场会提到`; layers.forEach((l) => l.setAttribute("fill-opacity", l === path ? 0.95 : 0.25)); };
      path.onmouseleave = () => { $("#rtip").hidden = true; layers.forEach((l) => l.setAttribute("fill-opacity", 0.85)); };
      svg.appendChild(path); return path;
    });
    d.weeks.forEach((w, i) => { if (i % 2 === 0) { const t = svgEl("text", { x: x(i), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: "#8b94a5" }); t.textContent = w; svg.appendChild(t); } });
  }
  function riverColor(i) { return ["#2f5bea", "#0891b2", "#0e9f6e", "#c26b04", "#e0453a", "#7c3aed", "#db2777", "#64748b"][i % 8]; }

  // ------------------------------------------------------------ 周报月报
  const REP = { cur: null };
  async function renderReports() {
    app.innerHTML = `<div class="kb rep"><aside class="col tree" id="repList"><div class="muted" style="padding:10px">加载中…</div></aside><section class="col preview" id="repView"></section></div>`;
    let r; try { r = await api("api/reports"); } catch (e) { $("#repList").textContent = e.message; return; }
    const el = $("#repList");
    let h = "";
    if (r.can_generate) h += `<div class="gen"><div class="muted" style="font-size:12px;margin:0 4px 6px">生成报告</div>
      <div class="gen-btns"><button class="btn sm" data-k="week" data-w="last">上周周报</button><button class="btn sm" data-k="week" data-w="current">本周至今</button>
      <button class="btn sm" data-k="month" data-w="last">上月月报</button><button class="btn sm" data-k="month" data-w="current">本月至今</button></div>
      <div class="muted" id="repJob" style="font-size:12px;margin:6px 4px 0">${r.job.running ? "⏳ " + esc(r.job.detail) : r.job.error ? "⚠️ " + esc(r.job.error) : "每周一、每月 1 号早上自动生成"}</div></div>`;
    const groups = { week: "周报", month: "月报" };
    for (const k of ["week", "month"]) {
      const items = r.items.filter((x) => x.kind === k);
      h += `<h4>${groups[k]}</h4>`;
      if (!items.length) h += `<div class="muted" style="padding:2px 10px 8px;font-size:12.5px">还没有</div>`;
      for (const x of items) h += `<div class="node ${REP.cur === x.id ? "on" : ""}" data-id="${x.id}"><span class="ico">${k === "week" ? "🗓" : "📅"}</span><span>${esc(x.period_start.slice(5))} ~ ${esc(x.period_end.slice(5))}</span><span class="n">${r.scopes[x.scope] === "全公司版" ? "全公司" : "研发市场销售"}</span></div>`;
    }
    el.innerHTML = h;
    $$(".node", el).forEach((n) => n.onclick = () => openReport(+n.dataset.id));
    $$(".gen-btns .btn", el).forEach((b) => b.onclick = async () => {
      try { const x = await api("api/reports/generate", { method: "POST", json: { kind: b.dataset.k, which: b.dataset.w } }); toast(`开始生成 ${x.start} ~ ${x.end}，大约一两分钟`); setTimeout(renderReports, 4000); }
      catch (e) { toast(e.message, true); }
    });
    if (r.job.running) setTimeout(() => { if (S.view === "reports") renderReports(); }, 5000);
    const first = REP.cur && r.items.find((x) => x.id === REP.cur) ? REP.cur : (r.items[0] && r.items[0].id);
    if (first) openReport(first); else $("#repView").innerHTML = `<div class="empty-state"><div><div class="big">📊</div>还没有报告${r.can_generate ? "，点左上角生成一份试试" : ""}</div></div>`;
  }
  async function openReport(id) {
    REP.cur = id;
    $$("#repList .node").forEach((n) => n.classList.toggle("on", +n.dataset.id === id));
    const el = $("#repView"); el.innerHTML = `<div class="empty-state muted">加载中…</div>`;
    let r; try { r = await api("api/reports/" + id); } catch (e) { el.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; return; }
    const st = r.stats || {};
    const fmax = Math.max(1, ...Object.values(st.folders || {}));
    const bars = Object.entries(st.folders || {}).map(([k, v]) => `<div class="bar"><span class="bl">${esc(k)}</span><span class="bt"><i style="width:${v / fmax * 100}%"></i></span><span class="bv">${v}</span></div>`).join("");
    const topics = (st.topics || []).map(([t, n]) => `<span class="tag biz">${esc(t)} ${n}</span>`).join("");
    const ent = (st.entities || {});
    const entHtml = ["客户", "竞品", "产品"].filter((k) => (ent[k] || []).length).map((k) => `<div class="ent"><b>${k}</b>${ent[k].map(([t, n]) => `<span class="tag">${esc(t)} ${n}</span>`).join("")}</div>`).join("");
    const body = md(r.content || "").replace(/\[M(\d+)\]/g, (m0, n) => r.refs[n] ? `<a class="cite" data-m="${n}" title="${esc(r.refs[n].title)} ${esc(r.refs[n].date)}">会</a>` : "");
    el.innerHTML = `<div class="doc report"><div class="rep-head"><div><h1 class="dt">${esc(r.title)}</h1><div class="muted" style="font-size:12.5px">生成于 ${esc(r.created_at)} · ${esc(r.created_by || "")}</div></div></div>
      <div class="rep-cards"><div class="kpi"><div class="kn">${st.meetings || 0}</div><div class="kl">场会议</div></div>
        <div class="kpi wide"><div class="kl">各文件夹</div>${bars || '<span class="muted">—</span>'}</div>
        <div class="kpi wide"><div class="kl">热门话题</div><div class="tags">${topics || '<span class="muted">—</span>'}</div>${entHtml}</div></div>
      <div class="md rep-body">${body}</div>
      <p class="muted" style="font-size:12px;margin-top:20px">经营数字来自会议发言，未经财务核实。点<span class="cite" style="cursor:default">会</span>可以打开对应的会议。</p></div>`;
    $$(".cite[data-m]", el).forEach((c) => c.onclick = () => openMeetingModal(+c.dataset.m));
  }

  // ------------------------------------------------------------ 管理
  let pollTimer = null;
  function renderAdmin() {
    clearTimeout(pollTimer);
    const tabs = [["import", "导入听记"], ["members", "人员权限"], ["model", "模型与检索"], ["synonyms", "同义词"], ["asks", "提问记录"], ["audit", "操作记录"]];
    api("api/admin/usage").then((u) => { const b = $("#useBanner"); if (b && (u.over || u.near)) { b.hidden = false; b.innerHTML = `大模型用量${u.over ? "已超出" : "快到"}本月额度（${fmtTok(u.total_in + u.total_out)} / ${fmtTok(u.budget_tokens)}），在「模型与检索」里查看。`; } }).catch(() => {});
    app.innerHTML = `<div class="admin"><div class="admin-inner"><div id="useBanner" class="notice warn" hidden></div><div class="subnav">${tabs.map(([k, n]) => `<button data-k="${k}" class="${S.adminTab === k ? "on" : ""}">${n}</button>`).join("")}</div><div id="adm"></div></div></div>`;
    $$(".subnav button").forEach((b) => b.onclick = () => { S.adminTab = b.dataset.k; renderAdmin(); });
    ({ import: admImport, members: admMembers, model: admModel, synonyms: admSyn, asks: admAsks, audit: admAudit })[S.adminTab]();
  }

  async function admImport() {
    const el = $("#adm");
    el.innerHTML = `<div class="card"><h3>上传听记 Excel</h3>
      <p class="desc">从钉钉 AI听记导出的 Excel（工作表「听记汇总」）。每次传新的就行，重复的会按听记链接自动跳过；只收 2026-08-14 以后、名单内主体的听记。<br>8月14日以前的历史：直接传以前手工整理的<b>文件夹压缩包（.zip）</b>，或者我生成的「历史听记包」（.json.gz），文件夹归属原样保留。大压缩包上传要几分钟，别关页面。</p>
      <label class="drop" id="drop"><input type="file" id="file" accept=".xlsx,.zip,.gz,.json" hidden><div style="font-size:28px">📤</div><div>点击选择文件，或把 Excel 拖到这里</div></label></div>
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
    const drop = $("#drop"); const mb = (f.size / 1048576).toFixed(1);
    // 用 XHR 才能看到上传进度（大压缩包要几分钟）
    await new Promise((done) => {
      const x = new XMLHttpRequest(); x.open("POST", "api/imports");
      x.upload.onprogress = (e) => { if (e.lengthComputable && drop) drop.innerHTML = `<div style="font-size:28px">⏫</div><div>正在上传 ${esc(f.name)}（${mb}MB）… ${Math.round(e.loaded / e.total * 100)}%</div><div class="progress" style="max-width:360px;margin:8px auto 0"><div style="width:${e.loaded / e.total * 100}%"></div></div>`; };
      x.onload = () => { let d = {}; try { d = JSON.parse(x.responseText); } catch { /* */ }
        if (x.status >= 200 && x.status < 300) toast("上传完成，开始导入"); else toast(d.error || `上传失败（${x.status}）`, true); done(); };
      x.onerror = () => { toast("上传中断了，请检查网络后重试", true); done(); };
      x.send(fd);
    });
    admImport();
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
      <div class="card"><h3>本地向量模型（检索用）</h3>
      <p class="desc">在服务器上把会议内容转成向量，提问时按意思找相关段落，内容不出服务器。第一次启动会自动下载模型（约 100MB），然后在后台把所有会议过一遍，大约要几十分钟；期间提问照常能用（先只用关键词）。</p>
      <div id="embState" class="muted">加载中…</div></div>
      <div class="card"><h3>大模型用量</h3><p class="desc">按月统计分类、抽取、提问、写报告消耗的字数（tokens）。设个每月额度，超过 80% 会在这里和管理页顶部提醒。价格填了就能估算花费（按你中转站的价格，每百万 tokens 多少元）。</p>
      <div id="useBox" class="muted">加载中…</div></div>
      <div class="card"><h3>会议信息抽取</h3>
      <p class="desc">用大模型从每场会的摘要里整理出<b>决议、风险问题、关键数字、客户 / 产品 / 竞品 / 项目 / 区域、话题</b>，周报月报和可视化都靠它。新导入的会议会自动抽；第一次全部抽一遍大约一两个小时，会消耗大模型用量。</p>
      <div id="exState" class="muted" style="margin-bottom:10px">加载中…</div>
      <button class="btn primary" id="exGo">补全抽取</button> <button class="btn" id="exRedo">全部重抽</button></div>
      <div class="card"><h3>子公司的会是否也放进「研发市场销售」</h3>
      <p class="desc">默认子公司的会都<b>不放</b>进「研发市场销售」（部门负责人看不到）。以后如果想让某个子公司的业务会也给负责人看，在这里勾上，再点“开始重新分类”。</p>
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
    pollEmb();
    pollEx();
    loadUsage();
    const exGo = async (redo) => {
      if (redo && !(await ask("全部重抽", "会把所有会议重新抽一遍，覆盖原来的结果，耗时较长、会消耗大模型用量。", "开始"))) return;
      try { const x = await api("api/admin/extract", { method: "POST", json: { redo } }); toast(`开始抽取 ${x.total} 场`); pollEx(); } catch (e) { toast(e.message, true); }
    };
    $("#exGo").onclick = () => exGo(false); $("#exRedo").onclick = () => exGo(true);
  }
  const fmtTok = (n) => n >= 1e8 ? (n / 1e8).toFixed(2) + " 亿" : n >= 1e4 ? (n / 1e4).toFixed(1) + " 万" : String(n);
  async function loadUsage() {
    const el = $("#useBox"); if (!el) return;
    let u; try { u = await api("api/admin/usage"); } catch (e) { el.textContent = e.message; return; }
    const tot = u.total_in + u.total_out, pct = u.budget_tokens ? Math.min(100, tot / u.budget_tokens * 100) : 0;
    el.innerHTML = `<div style="display:flex;gap:28px;flex-wrap:wrap;margin-bottom:10px;color:var(--text)"><div><div class="muted" style="font-size:12px">${u.month} 合计</div><div style="font-size:24px;font-weight:600">${fmtTok(tot)}</div><div class="muted" style="font-size:12px">输入 ${fmtTok(u.total_in)} · 输出 ${fmtTok(u.total_out)}</div></div>
      ${u.cost != null ? `<div><div class="muted" style="font-size:12px">估算花费</div><div style="font-size:24px;font-weight:600">¥${u.cost.toFixed(2)}</div></div>` : ""}
      ${u.budget_tokens ? `<div style="flex:1;min-width:200px"><div class="muted" style="font-size:12px">每月额度 ${fmtTok(u.budget_tokens)}${u.over ? ' · <span class="tag bad">已超出</span>' : u.near ? ' · <span class="tag warn">快用完了</span>' : ""}</div><div class="progress" style="height:8px"><div style="width:${pct}%;${u.over ? "background:var(--bad)" : ""}"></div></div></div>` : ""}</div>
      <table class="grid" style="margin-bottom:12px"><tr><th>用途</th><th>次数</th><th>输入</th><th>输出</th></tr>${u.by_task.map((t) => `<tr><td>${esc(t.task)}</td><td>${t.calls}</td><td>${fmtTok(t.in)}</td><td>${fmtTok(t.out)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">本月还没有用量</td></tr>'}</table>
      <div class="form-row"><label>每月额度（万 tokens）</label><input id="ubud" type="number" min="0" value="${u.budget_tokens ? u.budget_tokens / 1e4 : ""}" placeholder="不填 = 不提醒"></div>
      <div class="form-row"><label>输入价格（元/百万）</label><input id="upin" type="number" min="0" step="0.01" value="${u.price_in || ""}" placeholder="选填"></div>
      <div class="form-row"><label>输出价格（元/百万）</label><input id="upout" type="number" min="0" step="0.01" value="${u.price_out || ""}" placeholder="选填"></div>
      <div class="form-row"><label></label><div><button class="btn primary" id="usave">保存</button></div></div>`;
    $("#usave").onclick = async () => {
      try { await api("api/admin/usage", { method: "PUT", json: { budget_tokens: (+$("#ubud").value || 0) * 1e4, price_in: +$("#upin").value || 0, price_out: +$("#upout").value || 0 } }); toast("已保存"); loadUsage(); }
      catch (e) { toast(e.message, true); }
    };
  }
  async function admSyn() {
    const el = $("#adm");
    let r; try { r = await api("api/admin/synonyms"); } catch (e) { el.textContent = e.message; return; }
    const rows = r.items.map((x) => ({ ...x }));
    const draw = () => {
      el.innerHTML = `<div class="card"><h3>同义词表</h3><p class="desc">语音识别经常把专有名词听错，比如“均瑶健康”听成“君瑶健康”、“盒马”听成“河马”。在这里登记以后，<b>搜索和提问时任何一种写法都能找到</b>，图谱和报告里也会统一成标准写法。<br>初始表是从你现有的会议文字里统计出来的；黄色那行拿不准，请你确认。</p>
        <table class="grid syn"><tr><th style="width:150px">标准写法</th><th>其他写法（逗号分隔）</th><th style="width:240px">备注</th><th style="width:60px">启用</th><th style="width:40px"></th></tr>
        ${rows.map((x, i) => `<tr class="${/不确定/.test(x.note || "") ? "pend" : ""}"><td><input data-i="${i}" data-k="canonical" value="${esc(x.canonical)}"></td><td><input data-i="${i}" data-k="variants" value="${esc(x.variants || "")}"></td><td><input data-i="${i}" data-k="note" value="${esc(x.note || "")}"></td><td style="text-align:center"><input type="checkbox" data-i="${i}" data-k="enabled" ${x.enabled ? "checked" : ""}></td><td><button class="btn sm" data-del="${i}" title="删除">×</button></td></tr>`).join("")}</table>
        <div style="margin-top:12px;display:flex;gap:8px"><button class="btn" id="synAdd">+ 加一行</button><button class="btn primary" id="synSave">保存</button></div></div>`;
      $$(".syn input").forEach((inp) => inp.oninput = inp.onchange = () => { const x = rows[+inp.dataset.i]; x[inp.dataset.k] = inp.type === "checkbox" ? inp.checked : inp.value; });
      $$("[data-del]").forEach((b) => b.onclick = () => { rows.splice(+b.dataset.del, 1); draw(); });
      $("#synAdd").onclick = () => { rows.push({ canonical: "", variants: "", note: "", enabled: 1 }); draw(); };
      $("#synSave").onclick = async () => { try { await api("api/admin/synonyms", { method: "PUT", json: { items: rows } }); toast("已保存，搜索和提问马上生效"); admSyn(); } catch (e) { toast(e.message, true); } };
    };
    draw();
  }
  let exTimer = null;
  async function pollEx() {
    clearTimeout(exTimer);
    const el = $("#exState"); if (!el) return;
    let st; try { st = await api("api/admin/extract"); } catch (e) { el.textContent = e.message; return; }
    let h = `已抽取 <b>${st.extracted}</b> / ${st.all} 场`;
    if (st.running) h += `，正在进行 ${st.done}/${st.total}<div class="progress"><div style="width:${st.total ? st.done / st.total * 100 : 3}%"></div></div>`;
    else if (st.error) h += ` · <span class="tag bad">中断</span> ${esc(st.error)}`;
    else if (st.finished) h += ` · 上次完成：${esc(st.finished)}${st.fail ? `，失败 ${st.fail} 场（再点“补全抽取”会重试）` : ""}`;
    if ((st.errors || []).length) h += `<div style="margin-top:8px;font-size:12.5px">失败原因：${st.errors.map((e) => `<div title="${esc(e.example)}">· ${esc(e.reason)} <b>${e.count}</b> 场 <span class="muted">${esc(e.example.slice(0, 80))}</span></div>`).join("")}</div>`;
    el.innerHTML = h;
    if (st.running && S.adminTab === "model" && S.view === "admin") exTimer = setTimeout(pollEx, 3000);
  }
  let embTimer = null;
  async function pollEmb() {
    clearTimeout(embTimer);
    const el = $("#embState"); if (!el) return;
    let st; try { st = await api("api/admin/embed"); } catch (e) { el.textContent = e.message; return; }
    const label = { idle: "未启动", loading: "准备中", downloading: "下载模型中", ready: "已就绪", indexing: "正在生成向量", failed: "模型下载失败", error: "出错（会自动重试）" }[st.status] || st.status;
    const pct = st.total ? Math.min(100, st.indexed / st.total * 100) : 0;
    el.innerHTML = `<div><span class="tag ${st.status === "ready" ? "ok" : st.status === "failed" || st.status === "error" ? "bad" : "warn"}">${label}</span> 已生成 <b>${st.indexed}</b>${st.total ? ` / ${st.total}` : ""} 段 ${esc(st.detail || "")}</div>`
      + (st.status === "indexing" ? `<div class="progress"><div style="width:${pct}%"></div></div>` : "")
      + (st.status === "failed" ? `<button class="btn sm" id="embRetry" style="margin-top:8px">重试下载</button>` : "");
    const rb = $("#embRetry"); if (rb) rb.onclick = async () => { await api("api/admin/embed/retry", { method: "POST" }); pollEmb(); };
    if (["loading", "downloading", "indexing"].includes(st.status) && S.adminTab === "model" && S.view === "admin") embTimer = setTimeout(pollEmb, 3000);
  }
  async function admAsks(onlyBad) {
    const el = $("#adm");
    try {
      const r = await api("api/admin/asks" + (onlyBad ? "?bad=1" : ""));
      const st = r.stat || {};
      el.innerHTML = `<div class="card"><h3>提问记录</h3><p class="desc">一共 ${st.n || 0} 个问题，👍 ${st.up || 0}，👎 ${st.down || 0}。被点 👎 的问题可以发给 Claude，用来改进检索和回答。</p>
        <div style="margin-bottom:10px"><button class="btn sm ${onlyBad ? "" : "primary"}" id="aAll">全部</button> <button class="btn sm ${onlyBad ? "primary" : ""}" id="aBad">只看 👎</button></div>
        <table class="grid"><tr><th style="width:150px">时间</th><th style="width:90px">人</th><th>问题</th><th style="width:60px">评价</th></tr>${r.items.map((a) => `<tr class="arow" data-id="${a.id}" style="cursor:pointer"><td class="muted">${esc(a.created_at)}</td><td>${esc(a.user_name)}</td><td>${esc(a.question)}${a.comment ? `<div class="muted" style="font-size:12px">反馈：${esc(a.comment)}</div>` : ""}<div class="ans" hidden><div class="md" style="margin-top:8px;font-size:13px">${md(a.answer || "")}</div></div></td><td>${a.rating === 1 ? "👍" : a.rating === -1 ? "👎" : ""}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">暂无</td></tr>`}</table></div>`;
      $("#aAll").onclick = () => admAsks(false); $("#aBad").onclick = () => admAsks(true);
      $$(".arow").forEach((tr) => tr.onclick = () => { const a = $(".ans", tr); a.hidden = !a.hidden; });
    } catch (e) { el.textContent = e.message; }
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
