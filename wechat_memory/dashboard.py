"""Self-contained static report; all imported content is escaped before rendering."""
from __future__ import annotations

from html import escape
from pathlib import Path


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def render(path: Path, analysis: dict, messages: list[dict]) -> None:
    timeline = "".join(
        f'<tr><td>{esc(q["quarter"])}</td><td>{q["message_count"]}</td><td>{q["chat_count"]}</td>'
        f'<td>{esc(" / ".join(q["keywords"]))}</td><td>{"少样本" if q["small_sample"] else ""}</td></tr>'
        for q in analysis["timeline"]
    )
    cards = "".join(
        f'<article><h3>{esc(c["concept"])}</h3><p>{c["message_count"]} 条匹配消息 · '
        f'{esc(" → ".join(q["quarter"] for q in c["quarters"]))}</p></article>'
        for c in analysis["concept_cards"][:30]
    )
    ai_sections = "".join(
        f'<article><h3>{esc(a["chunk_id"])}</h3><p>{esc(a["summary"])}</p>'
        f'<p class="muted">置信度 {a["confidence"]} · {esc(a["uncertainty"])}</p>'
        + "".join(f'<blockquote>{esc(e["quote"])}<br><small>{esc(e["message_id"])}</small></blockquote>' for e in a["evidence"])
        + '</article>' for a in analysis["annotations"]
    )
    # Paginate in the browser without injecting source data into script or attributes.
    rows = "".join(
        f'<tr><td>{esc(m["datetime"])}</td><td>{esc(m["chat_id"])}</td><td>{esc(m["sender_name"])}</td>'
        f'<td class="text">{esc(m["text"] or "[无导出文本]")}</td><td><code>{esc(m["id"])}</code></td></tr>'
        for m in messages
    )
    limitations = "".join(f'<li>{esc(note)}</li>' for note in analysis["limitations"])
    path.write_text(f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>WeChat Memory · 会话记忆</title><style>
:root{{color-scheme:light;--ink:#19332f;--muted:#59716c;--line:#d3e0db;--accent:#247b61}}
*{{box-sizing:border-box}}body{{margin:0;background:#f5f8f6;color:var(--ink);font:16px/1.6 system-ui,sans-serif}}
main{{max-width:1280px;margin:auto;padding:36px 24px}}h1{{font-size:38px;margin:0}}h2{{margin-top:36px}}
.muted,small{{color:var(--muted)}}.stats,.cards{{display:flex;gap:16px;flex-wrap:wrap}}.stat,article{{background:white;border:1px solid var(--line);padding:18px 22px;border-radius:12px}}.stat strong{{font-size:32px;display:block}}article{{flex:1 1 250px}}.scroll{{overflow:auto;background:white;border:1px solid var(--line);border-radius:12px}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{background:#eaf2ed;white-space:nowrap}}.text{{white-space:pre-wrap;min-width:300px;overflow-wrap:anywhere}}code{{font-size:11px}}input,button{{font:inherit;padding:9px 14px;border:1px solid var(--line);border-radius:8px}}input{{width:min(100%,600px)}}button{{cursor:pointer;background:white;color:var(--ink)}}button:disabled{{opacity:.4}}blockquote{{border-left:3px solid var(--accent);margin:12px 0;padding:6px 12px;white-space:pre-wrap}}[hidden]{{display:none!important}}
</style></head><body><main>
<p class="muted">LOCAL FIRST / WECHAT MEMORY 1.0</p><h1>会话记忆</h1>
<p>从聊天导出中整理时间、主题与原文证据。页面在本地运行，搜索不发起网络请求。</p>
<div class="stats"><div class="stat"><strong>{analysis["message_count"]}</strong>消息</div><div class="stat"><strong>{analysis["chat_count"]}</strong>会话</div><div class="stat"><strong>{analysis["chunk_count"]}</strong>上下文片段</div><div class="stat"><strong>{len(analysis["timeline"])}</strong>季度</div></div>
<h2>季度概览</h2><p class="muted">关键词来自本地词项提取，按整个数据集展示。少样本阈值为 20 条含文本消息。</p>
<div class="scroll"><table><thead><tr><th>季度</th><th>消息</th><th>会话</th><th>关键词线索</th><th>提示</th></tr></thead><tbody>{timeline}</tbody></table></div>
<h2>概念出现轨迹</h2><p class="muted">仅表示字面出现，不据此判断一个人的立场变化。</p><div class="cards">{cards or '<p>暂无可提取的概念。</p>'}</div>
<h2>AI 会话概括</h2>{ai_sections or '<p class="muted">未启用 AI。当前分析完全在本地完成。</p>'}
<h2>原文与证据检索</h2><label for="filter">搜索时间、会话、发送者、原文或消息 ID</label><p><input id="filter" placeholder="输入关键词筛选" autocomplete="off"></p>
<p><button id="prev">上一页</button> <span id="page" aria-live="polite"></span> <button id="next">下一页</button></p>
<div class="scroll"><table><thead><tr><th>UTC 时间</th><th>会话</th><th>发送者</th><th>导出内容</th><th>证据 ID</th></tr></thead><tbody id="messages">{rows}</tbody></table></div>
<h2>使用边界</h2><ul>{limitations}</ul><p class="muted">本页面包含导入的原文及身份信息，请作为私人文件保存。</p>
</main><script>
const rows=Array.from(document.querySelectorAll('#messages tr'));
const input=document.querySelector('#filter'),prev=document.querySelector('#prev'),next=document.querySelector('#next');
let page=0,matching=rows;
function show(){{rows.forEach(r=>r.hidden=true);matching.slice(page*50,(page+1)*50).forEach(r=>r.hidden=false);document.querySelector('#page').textContent=`${{matching.length}} 条 · ${{page+1}} / ${{Math.max(1,Math.ceil(matching.length/50))}} 页`;prev.disabled=page===0;next.disabled=(page+1)*50>=matching.length;}}
input.addEventListener('input',()=>{{const q=input.value.toLocaleLowerCase();matching=rows.filter(r=>r.textContent.toLocaleLowerCase().includes(q));page=0;show();}});
prev.addEventListener('click',()=>{{if(page>0)page--;show();}});next.addEventListener('click',()=>{{if((page+1)*50<matching.length)page++;show();}});show();
</script></body></html>''', encoding="utf-8")
