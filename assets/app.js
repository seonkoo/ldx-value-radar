/* 李大霄价值投资雷达 —— 前端逻辑 */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '--' : Number(v).toFixed(d);

let ALL = [];
let VIEW = [];
let sortKey = '总分';
let sortAsc = false;
let filter = 'all';
const openSet = new Set();

/* ---------------- 工具 ---------------- */
function pctColor(p) {
    if (p < 20) return 'var(--cold)';
    if (p < 35) return 'var(--cool)';
    if (p < 65) return 'var(--neutral)';
    if (p < 85) return 'var(--warm)';
    return 'var(--hot)';
}

function esc(s) {
    return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------------- 温度计 ---------------- */
function renderGauge(v) {
    if (!v || !v.available) {
        $('#gaugeBox').innerHTML = '<div class="err">大盘估值分位数据暂不可用</div>';
        return;
    }
    const p = v.percentile;
    const marks = [
        { q: v.quantiles['20'], label: '钻石底' },
        { q: v.quantiles['35'], label: '婴儿底' },
        { q: v.quantiles['65'], label: '正常' },
        { q: v.quantiles['85'], label: '偏热' },
    ];
    // 用分位数对应的 PE 在历史区间中的位置作为刻度（近似映射，直观易懂）
    const lo = v.history_min, hi = v.history_max;
    const pos = q => Math.max(0, Math.min(100, (q - lo) / (hi - lo) * 100));

    const markHtml = marks.map(m =>
        `<div class="gauge-mark" style="left:${pos(m.q).toFixed(1)}%" title="${m.label}"></div>`
    ).join('');

    $('#gaugeBox').innerHTML = `
        <div class="gauge">
            <div class="gauge-track">
                ${markHtml}
                <div class="gauge-cursor" style="left:${pos(v.pe).toFixed(1)}%">
                    <div class="pin"></div>
                    <div class="val">${fmt(p, 0)}%</div>
                </div>
            </div>
            <div class="gauge-label-row">
                <span>极度低估</span><span>低估</span><span>合理</span><span>偏高</span><span>泡沫</span>
            </div>
        </div>
        <div class="kv">
            <div><div class="k">当前 PE(TTM)</div><div class="v" style="color:${pctColor(p)}">${fmt(v.pe)}</div></div>
            <div><div class="k">历史分位</div><div class="v">${fmt(p, 1)}%</div></div>
            <div><div class="k">历史最低</div><div class="v" style="color:var(--cold)">${fmt(v.history_min)}</div></div>
            <div><div class="k">历史中位</div><div class="v">${fmt(v.history_median)}</div></div>
            <div><div class="k">历史最高</div><div class="v" style="color:var(--hot)">${fmt(v.history_max)}</div></div>
            <div><div class="k">样本区间</div><div class="v" style="font-size:12px">${v.start_date} 起 ${v.sample_days} 点</div></div>
        </div>`;
}

/* ---------------- 择时信号 ---------------- */
function renderTiming(t) {
    const s = t.signal;
    $('#signalBox').innerHTML = `
        <div class="signal ${s.tone}">
            <div class="sig-top">
                <span class="sig-label" style="color:${pctColor(t.valuation && t.valuation.available ? t.valuation.percentile : 50)}">${esc(s.label)}</span>
                <span class="sig-pos">建议仓位 <b>${esc(s.position)}</b></span>
            </div>
            <div class="sig-desc">${esc(s.desc)}</div>
            <div class="sig-act">${esc(s.action)}</div>
        </div>`;

    // 三要素
    const se = t.sentiment || {};
    const cap = t.capital || {};
    const v = t.valuation || {};

    const f1 = v.available ? `
        <div class="factor">
            <div class="ft">要素一 · 全市场估值水平</div>
            <div class="fv" style="color:${pctColor(v.percentile)}">${fmt(v.percentile, 0)}% 分位</div>
            <div class="fd">沪深300 PE(TTM) ${fmt(v.pe)}，2005 年以来${v.percentile < 35 ? '偏低' : v.percentile > 65 ? '偏高' : '中性'}</div>
            <div class="fsub">历史区间 ${fmt(v.history_min)} ~ ${fmt(v.history_max)}</div>
        </div>` : `<div class="factor"><div class="ft">要素一 · 全市场估值水平</div><div class="fv">--</div><div class="fd">数据暂不可用</div></div>`;

    const f2 = cap.available ? `
        <div class="factor">
            <div class="ft">要素二 · 产业资本动向</div>
            <div class="fv" style="color:${cap.净家数 > 0 ? 'var(--up)' : 'var(--down)'}">${cap.净家数 > 0 ? '+' : ''}${cap.净家数} 家</div>
            <div class="fd">蓝筹池内增持 ${cap.增持家数} 家 / 减持 ${cap.减持家数} 家</div>
            <div class="fsub">增持 ${cap.增持次数} 次 · 减持 ${cap.减持次数} 次（全量样本）</div>
        </div>` : `<div class="factor"><div class="ft">要素二 · 产业资本动向</div><div class="fv">--</div><div class="fd">数据暂不可用</div></div>`;

    const f3 = se.available ? `
        <div class="factor">
            <div class="ft">要素三 · 市场情绪（自建）</div>
            <div class="fv">${fmt(se.破净占比, 1)}%</div>
            <div class="fd">蓝筹池破净 ${se.破净家数} 家，中位 PB ${fmt(se.中位PB)}</div>
            <div class="fsub">高股息(≥5%) ${se.高股息家数} 家 · 中位股息率 ${fmt(se.中位股息率)}%</div>
        </div>` : `<div class="factor"><div class="ft">要素三 · 市场情绪</div><div class="fv">--</div><div class="fd">数据暂不可用</div></div>`;

    $('#factorsBox').innerHTML = `<div class="factors">${f1}${f2}${f3}</div>`;
}

/* ---------------- 表格 ---------------- */
function applyView() {
    let rows = ALL.slice();
    if (filter === 'pb') rows = rows.filter(r => r.破净);
    else if (filter === 'div') rows = rows.filter(r => r.股息率 >= 5);
    else if (filter === 'bank') rows = rows.filter(r => /银行|保险|证券/.test(r.名称));
    else if (filter === 'central') rows = rows.filter(r => /^中国|中油|中煤|中远|中海/.test(r.名称));

    rows.sort((a, b) => {
        const x = a[sortKey], y = b[sortKey];
        if (typeof x === 'string') return sortAsc ? x.localeCompare(y) : y.localeCompare(x);
        return sortAsc ? x - y : y - x;
    });
    VIEW = rows;
    renderTable();
}

function renderTable() {
    const cls = r => r.涨跌幅 > 0 ? 'up' : (r.涨跌幅 < 0 ? 'down' : '');
    const sign = r => r.涨跌幅 > 0 ? '+' : '';

    let html = `<div class="tbl-scroll"><table><thead><tr>
        ${[['rank', '#'], ['名称', '名称'], ['价格', '现价'], ['涨跌幅', '涨跌'], ['PE', 'PE'],
    ['PB', 'PB'], ['股息率', '股息率'], ['连续分红年', '连分年'],
    ['总市值亿', '总市值(亿)'], ['总分', '评分']]
        .map(([k, t]) => {
            const on = sortKey === k ? ` sorted${sortAsc ? ' asc' : ''}` : '';
            const al = ['rank', '名称'].includes(k) ? ' style="text-align:left"' : '';
            return `<th data-k="${k}" class="${on.trim()}"${al}>${t}</th>`;
        }).join('')}
        </tr></thead><tbody>`;

    VIEW.forEach(r => {
        const open = openSet.has(r.代码);
        html += `<tr data-c="${r.代码}" class="${open ? 'open' : ''}">
            <td class="rk">${r.rank}</td>
            <td><span class="nm">${esc(r.名称)}</span>
                ${r.破净 ? '<span class="tag pb">破净</span>' : ''}
                ${r.高股息 ? '<span class="tag dv">高息</span>' : ''}
                <div class="code">${r.代码}</div></td>
            <td>${fmt(r.价格)}</td>
            <td class="${cls(r)}">${sign(r)}${fmt(r.涨跌幅)}%</td>
            <td>${fmt(r.PE)}</td>
            <td>${fmt(r.PB)}</td>
            <td><b>${fmt(r.股息率)}%</b></td>
            <td>${r.连续分红年}</td>
            <td>${fmt(r.总市值亿, 0)}</td>
            <td><span class="score-cell">${fmt(r.总分, 1)}</span>
                <span class="score-bar"><i style="width:${r.总分}%"></i></span></td>
        </tr>`;
        if (open) html += detailRow(r);
    });

    html += '</tbody></table></div>';
    $('#tableBox').innerHTML = html;

    $$('#tableBox th').forEach(th => th.onclick = () => {
        const k = th.dataset.k;
        if (sortKey === k) sortAsc = !sortAsc;
        else { sortKey = k; sortAsc = ['PE', 'PB', 'rank'].includes(k); }
        applyView();
    });
    $$('#tableBox tr[data-c]').forEach(tr => tr.onclick = () => {
        const c = tr.dataset.c;
        openSet.has(c) ? openSet.delete(c) : openSet.add(c);
        applyView();
    });
}

function detailRow(r) {
    const bars = [
        ['股息率', r.S_股息, fmt(r.股息率) + '%'],
        ['低 PE', r.S_PE, 'PE ' + fmt(r.PE)],
        ['低 PB', r.S_PB, 'PB ' + fmt(r.PB)],
        ['分红稳定', r.S_稳定, fmt(r.分红稳定性, 0)],
    ];
    const barHtml = bars.map(([l, v, t]) => `
        <div class="bar-row">
            <div class="bl">${l}</div>
            <div class="bt"><i style="width:${v}%"></i></div>
            <div class="bv">${fmt(v, 0)}</div>
        </div>`).join('');

    return `<tr class="detail"><td colspan="10"><div class="detail-inner">
        <div class="dgrid">
            <div class="ditem"><div class="dk">近12月每股派息</div><div class="dv2">${fmt(r.每股派息, 4)} 元</div></div>
            <div class="ditem"><div class="dk">股息率口径</div><div class="dv2">${fmt(r.股息率)}%</div></div>
            <div class="ditem"><div class="dk">连续现金分红</div><div class="dv2">${r.连续分红年} 年</div></div>
            <div class="ditem"><div class="dk">分红稳定性</div><div class="dv2">${fmt(r.分红稳定性, 0)} / 100</div></div>
            <div class="ditem"><div class="dk">市盈率 PE(TTM)</div><div class="dv2">${fmt(r.PE)}</div></div>
            <div class="ditem"><div class="dk">市净率 PB</div><div class="dv2">${fmt(r.PB)}${r.破净 ? '（破净）' : ''}</div></div>
            <div class="ditem"><div class="dk">总市值</div><div class="dv2">${fmt(r.总市值亿, 0)} 亿</div></div>
            <div class="ditem"><div class="dk">综合评分</div><div class="dv2" style="color:var(--navy)">${fmt(r.总分, 1)}</div></div>
        </div>
        <div class="bars">
            <div style="font-size:12px;color:var(--ink-3);margin-bottom:8px">四维分位得分（0-100，越高越优）</div>
            ${barHtml}
        </div>
        <div class="dnote">
            股息率 = 近 12 个月内已完成除权的每股派息合计 ÷ 当前股价，含中期分红、剔除未实施预案。<br>
            分位得分为该股在通过筛选的 ${ALL.length} 只蓝筹中的相对排名，非绝对估值判断。
        </div>
    </div></td></tr>`;
}

/* ---------------- 元信息 ---------------- */
function renderMeta(m) {
    $('#metaRow').innerHTML = `
        <span class="chip">蓝筹池 <b>${m.pool} ${m.pool_size}</b> 只</span>
        <span class="chip">通过筛选 <b>${m.passed}</b> 只</span>
        <span class="chip">排除 <b>${m.filtered_out}</b> 只</span>
        <span class="chip">更新 <b>${esc(m.generated_at)}</b></span>`;

    $('#sourceNote').innerHTML = Object.entries(m.sources || {})
        .map(([k, v]) => `<b>${esc(k)}</b>：${esc(v)}`).join('<br>');
}

/* ---------------- 启动 ---------------- */
async function boot() {
    try {
        const res = await fetch('./data/data.json?t=' + Date.now());
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const d = await res.json();
        ALL = d.stocks || [];
        renderMeta(d.meta);
        renderGauge(d.timing.valuation);
        renderTiming(d.timing);
        applyView();

        $$('.fbtn').forEach(b => b.onclick = () => {
            $$('.fbtn').forEach(x => x.classList.remove('on'));
            b.classList.add('on');
            filter = b.dataset.f;
            openSet.clear();
            applyView();
        });
    } catch (e) {
        $('#tableBox').innerHTML =
            `<div class="err">数据加载失败：${esc(e.message)}<br><br>
             请通过 HTTP 服务访问（如 <code>python -m http.server</code>），
             直接以 file:// 打开会被浏览器的跨域策略拦截。</div>`;
    }
}

document.addEventListener('DOMContentLoaded', boot);
