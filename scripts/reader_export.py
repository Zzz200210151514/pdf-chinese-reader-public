"""Export reviewed reading blocks to portable Markdown or a standalone offline HTML reader.

The default path uses only the Python standard library. ``--embed-font`` optionally
subsets and inlines OFL fonts with fontTools so the page renders the same on any machine.
No translation, network access, or raw HTML/Markdown input is accepted.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import re
from pathlib import Path
from urllib.parse import quote

# [[ref:12]] [[ref:9-11,15]] [[fig:1|图 1b]] [[eq:2]] [[table:1]]  ^{上标}  _{下标}
INLINE = re.compile(
    r'\[\[(fig|ref|eq|table):([^\]|\n]+)(?:\|([^\]\n]+))?\]\]'
    r'|\^\{([^{}\n]+)\}|_\{([^{}\n]+)\}')
LABELS = {'fig': '图', 'ref': '文献', 'eq': '式', 'table': '表'}
MEDIA = {'figure': 'fig', 'equation': 'eq', 'table': 'table'}
TEXT_TYPES = ('paragraph', 'heading', 'abstract', 'note')
MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
THEMES = ('serif', 'kai', 'sans')
FONT_ROLES = {'serif': 'RL Embedded Serif', 'sans': 'RL Embedded Sans', 'kai': 'RL Embedded Kai'}
ID_RE = re.compile(r'[A-Za-z][A-Za-z0-9_-]*')

# Latin glyphs come from a real Latin serif; curly quotes, em dash, ellipsis and the
# middle dot are left to the CJK font so Chinese punctuation keeps full width.
LATIN_RANGE = ('U+0000-00B6,U+00B8-02FF,U+0370-03FF,U+1E00-1EFF,U+2000-2013,U+2015-2017,'
               'U+201A-201B,U+201E-2025,U+2027-206F,U+2070-209F,U+20A0-20CF,U+2100-214F,'
               'U+2190-21FF,U+2200-22FF')
LATIN_FACES = (
    ('normal', 400, ('Source Serif 4', 'SourceSerif4-Regular', 'Source Serif Pro', 'SourceSerifPro-Regular',
                     'Charter', 'Charter Roman', 'Cambria', 'Georgia')),
    ('normal', 700, ('Source Serif 4 Bold', 'SourceSerif4-Bold', 'Source Serif Pro Bold', 'SourceSerifPro-Bold',
                     'Charter Bold', 'Cambria Bold', 'Georgia Bold')),
    ('italic', 400, ('Source Serif 4 Italic', 'SourceSerif4-Italic', 'Source Serif Pro Italic',
                     'SourceSerifPro-It', 'Charter Italic', 'Cambria Italic', 'Georgia Italic')),
)
CJK_STACKS = {
    'serif': '"Source Han Serif SC","Noto Serif SC","Noto Serif CJK SC","Source Han Serif CN","思源宋体","Songti SC","STSong","SimSun"',
    'sans': '"Source Han Sans SC","Noto Sans SC","Noto Sans CJK SC","PingFang SC","HarmonyOS Sans SC","Microsoft YaHei","微软雅黑"',
    'kai': '"LXGW WenKai","LXGW WenKai Screen","霞鹜文楷","Kaiti SC","STKaiti","KaiTi","楷体"',
}

CSS = r'''
:root{--fs:18px;--measure:36rem;--lh:1.82;--body-weight:400;
--bg:#fbfaf7;--soft:#f3f1ec;--text:#1f2328;--muted:#5d6872;--faint:#8b949b;--line:#e4e1da;
--accent:#1f5f8b;--accent-soft:#e3eef6;--mark:#fff1b8;--img:#fff;--shadow:0 10px 30px rgba(20,24,28,.14);
--ui:"Segoe UI",system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
html[data-theme=sepia]{--bg:#f6efe0;--soft:#ede4d0;--text:#3a2f22;--muted:#6d5d48;--faint:#9a8a72;--line:#e0d4bc;--accent:#8a4a1b;--accent-soft:#eadcc3;--mark:#f3dc9a}
html[data-theme=dark]{--bg:#18191c;--soft:#222429;--text:#d9d6cf;--muted:#a19d95;--faint:#7c7a75;--line:#33363c;--accent:#86b7dc;--accent-soft:#25384a;--mark:#4d4420;--img:#f4f4f2;--shadow:0 10px 30px rgba(0,0,0,.5)}
html[data-width=narrow]{--measure:31rem}html[data-width=wide]{--measure:42rem}
html[data-font=serif]{--body:var(--stack-serif);--body-weight:var(--serif-weight)}
html[data-font=kai]{--body:var(--stack-kai)}
html[data-font=sans]{--body:var(--stack-sans);--lh:1.85}
html[data-theme=dark][data-font=serif]{--body-weight:400}
*{box-sizing:border-box}
html{font-size:var(--fs);-webkit-text-size-adjust:100%;scroll-padding-top:76px;background:var(--bg)}
body{margin:0;color:var(--text);background:var(--bg);font-family:var(--body);font-weight:var(--body-weight);line-height:var(--lh);font-kerning:normal;text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased;transition:background .2s,color .2s}
a{color:var(--accent);text-decoration:none}
button{font:inherit;color:inherit;cursor:pointer}
[tabindex="-1"]:focus{outline:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
sup,sub{font-size:.72em;line-height:0;position:relative;vertical-align:baseline}
sup{top:-.48em}sub{bottom:-.22em}
#ui-progress{position:fixed;top:0;left:0;height:2px;width:0;background:var(--accent);z-index:40}
.bar{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:6px;height:52px;padding:0 14px;background:var(--bg);background:color-mix(in srgb,var(--bg) 86%,transparent);backdrop-filter:blur(10px) saturate(1.3);-webkit-backdrop-filter:blur(10px);border-bottom:1px solid var(--line);font:14px/1 var(--ui)}
.bar-title{flex:1;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;color:var(--muted);font-size:13px;opacity:0;transition:opacity .2s}
.scrolled .bar-title{opacity:1}
.bar button{border:1px solid transparent;background:transparent;border-radius:8px;padding:7px 10px;color:var(--muted);white-space:nowrap}
.bar button:hover{background:var(--soft);color:var(--text)}
.bar button[aria-pressed=true]{color:var(--accent);border-color:var(--line);background:var(--soft)}
#ui-toc-toggle{display:none;font-size:17px;padding:5px 10px}
.panel{position:fixed;top:58px;right:14px;z-index:35;width:290px;padding:14px 16px;background:var(--bg);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);font:13px/1.4 var(--ui);color:var(--muted)}
.panel .row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:8px 0}
.seg{display:flex;align-items:center;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{border:0;background:transparent;padding:6px 10px;color:var(--muted)}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed=true]{background:var(--accent);color:var(--bg)}
.seg output{min-width:2.6em;text-align:center;color:var(--text);border-left:1px solid var(--line);border-right:1px solid var(--line);padding:6px 0}
.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,var(--measure)) minmax(0,1fr);column-gap:56px;padding:0 24px}
#ui-toc{grid-column:1;justify-self:end;width:236px;position:sticky;top:52px;align-self:start;max-height:calc(100vh - 52px);overflow:auto;padding:44px 4px 40px 0;font:13px/1.5 var(--ui);scrollbar-width:thin}
.toc-title{font-weight:600;letter-spacing:.12em;color:var(--faint);font-size:11px;margin:0 0 10px 12px}
#ui-toc ol{list-style:none;margin:0;padding:0}
#ui-toc a{display:block;padding:5px 10px 5px 12px;color:var(--muted);border-left:2px solid transparent;border-radius:0 6px 6px 0}
#ui-toc li.l3 a{padding-left:24px;font-size:12.5px;color:var(--faint)}
#ui-toc a:hover{color:var(--text);background:var(--soft)}
#ui-toc a.active{color:var(--accent);border-left-color:var(--accent);background:var(--soft);font-weight:600}
main{grid-column:2;min-width:0;padding-bottom:30vh}
.article-head{padding:52px 0 4px}
.kicker{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline;border-top:3px solid var(--accent);padding-top:12px;margin-bottom:22px;font:600 12.5px/1.4 var(--ui);letter-spacing:.05em;color:var(--accent)}
.kicker .k2{color:var(--muted);font-weight:500;letter-spacing:.02em}
h1{font-family:var(--stack-sans);font-weight:700;font-size:2rem;line-height:1.34;letter-spacing:.01em;margin:0 0 .45em;text-wrap:balance}
.title-en{font-family:"RL Latin Serif",Georgia,serif;font-style:italic;color:var(--muted);font-size:1rem;line-height:1.5;margin:0 0 1.3em}
.authors{font-family:var(--stack-sans);font-size:.9rem;line-height:1.75;margin:0 0 .3em}
.authors sup{color:var(--muted);font-size:.68em;margin-left:.08em}
.authors .mail{color:var(--accent);font-size:.8em;margin-left:.1em}
details.affil{font:13px/1.75 var(--ui);color:var(--muted);margin:.2em 0 1.1em}
details.affil summary{cursor:pointer;color:var(--faint);width:max-content}
details.affil ol{margin:.4em 0;padding-left:1.4em}
.meta{display:flex;flex-wrap:wrap;gap:4px 20px;margin:0 0 2rem;padding:10px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);font:12.5px/1.6 var(--ui);color:var(--muted)}
.meta div{display:flex;gap:6px}.meta dt{color:var(--faint)}.meta dd{margin:0;color:var(--text)}
.abstract{margin:0 0 2.4rem;padding:1.25rem 1.45rem 1.05rem;background:var(--soft);border-radius:12px}
.abstract .abs-label{margin:0 0 .55em;font:700 12px/1.4 var(--ui);letter-spacing:.24em;color:var(--accent)}
.abstract p{text-indent:0;font-size:1.03rem;line-height:1.85;font-weight:calc(var(--body-weight) + 100);margin:0 0 .5em}
.scope{font:12.5px/1.7 var(--ui);color:var(--faint);margin:-1.2rem 0 2.2rem}
article p{margin:0 0 .6em;text-align:justify;text-indent:2em;line-break:strict;overflow-wrap:break-word;hanging-punctuation:allow-end}
article h2,article h3,article h4{font-family:var(--stack-sans);line-height:1.45;text-wrap:balance}
article h2{position:relative;font-size:1.36rem;font-weight:700;margin:2.4em 0 .85em;padding-top:.85em;border-top:1px solid var(--line)}
article h2::before{content:"";position:absolute;top:-2px;left:0;width:2.4em;height:3px;background:var(--accent)}
article h3{font-size:1.1rem;font-weight:650;margin:1.9em 0 .6em}
article h4{font-size:1rem;font-weight:600;margin:1.5em 0 .5em;color:var(--muted)}
.cite{font-family:var(--ui);font-size:.66em;font-weight:500;top:-.62em;margin:0 .06em;letter-spacing:.01em;white-space:nowrap}
.cite a{padding:0 .1em;border-radius:3px}
.cite a:hover,a.xref:hover{background:var(--accent-soft)}
a.xref{border-bottom:1px solid color-mix(in srgb,var(--accent) 35%,transparent)}
.blk{position:relative;border-radius:6px}
.tools{position:absolute;top:.3em;left:calc(100% + 18px);display:flex;flex-direction:column;align-items:flex-start;gap:4px;opacity:0;transition:opacity .15s;font:11px/1.3 var(--ui);color:var(--faint);white-space:nowrap}
.blk:hover>.tools,.blk:focus-within>.tools{opacity:1}
.tools button{border:1px solid var(--line);background:var(--bg);border-radius:6px;padding:2px 7px;color:var(--muted)}
.tools a{color:var(--faint)}.tools a:hover{color:var(--accent)}
.src{display:none;margin:.1em 0 1.1em;padding:.6em .95em;border-left:3px solid var(--line);background:var(--soft);border-radius:0 8px 8px 0;font-family:"RL Latin Serif",Georgia,serif;font-size:.86rem;line-height:1.68;color:var(--muted);text-align:left;text-indent:0;hyphens:auto}
.src-h{display:block;font:600 11px/1.6 var(--ui);letter-spacing:.06em;color:var(--faint);margin-bottom:.15em}
.show-src .src,.blk.open>.src{display:block}
.note p{font-size:.88rem;line-height:1.75;text-indent:0;color:var(--muted)}
figure{margin:0}
.media{margin:2.2em 0}
.figure .zoom,.table .zoom{display:block;width:100%;padding:0;border:0;background:var(--img);border-radius:10px;cursor:zoom-in;overflow:hidden;box-shadow:0 0 0 1px var(--line)}
.zoom img{display:block;width:100%;height:auto}
html[data-theme=dark] .zoom img{filter:brightness(.93)}
figcaption{font-family:var(--stack-sans);font-size:.8rem;line-height:1.78;color:var(--muted);margin:.9em .15em 0;text-align:justify}
.table figcaption{margin:0 .15em .7em}
.cap-label{font-weight:700;color:var(--text);margin-right:.55em}
.cap-title{font-weight:600;color:var(--text)}
.equation{margin:1.3em 0}
.eq-row{position:relative;display:flex;justify-content:center;align-items:center;padding:0 3em}
.equation .zoom{display:block;max-width:100%;padding:0;border:0;background:transparent;cursor:zoom-in}
.equation img{display:block;max-width:100%;height:auto;mix-blend-mode:multiply}
html[data-theme=dark] .equation img{filter:invert(1);mix-blend-mode:screen}
.eq-no{position:absolute;right:0;font-family:"RL Latin Serif",Georgia,serif;color:var(--muted)}
.equation figcaption{text-align:center;font-size:.72rem;color:var(--faint);margin-top:.2em}
.references{margin-top:3em}
.reflist{list-style:none;margin:0;padding:0;font:.8rem/1.62 var(--ui)}
.reflist li{display:grid;grid-template-columns:2.6em minmax(0,1fr);padding:.32em .5em .32em 0;border-radius:6px;overflow-wrap:anywhere}
.ref-no{text-align:right;padding-right:.75em;color:var(--faint);font-variant-numeric:tabular-nums}
.colophon{margin-top:4rem;padding-top:1rem;border-top:1px solid var(--line);font:12px/1.7 var(--ui);color:var(--faint)}
.flash{animation:flash 2s ease-out}
@keyframes flash{0%,35%{background:var(--mark)}100%{background:transparent}}
#ui-back{position:fixed;right:22px;bottom:22px;z-index:30;padding:10px 16px;border-radius:999px;border:1px solid var(--line);background:var(--bg);color:var(--accent);box-shadow:var(--shadow);font:14px/1 var(--ui)}
#ui-resume{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:30;display:flex;align-items:center;gap:10px;max-width:92vw;padding:9px 10px 9px 16px;border-radius:999px;background:var(--text);color:var(--bg);box-shadow:var(--shadow);font:13px/1.3 var(--ui)}
#ui-resume span{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
#ui-resume button{border:0;border-radius:999px;padding:6px 12px;background:var(--bg);color:var(--text)}
#ui-resume button+button{background:transparent;color:var(--bg);padding:6px 8px}
#ui-peek{position:absolute;z-index:45;width:min(440px,92vw);padding:12px 14px;background:var(--bg);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);font:13px/1.65 var(--ui);color:var(--text)}
#ui-peek .pk-h{display:block;font-weight:600;color:var(--accent);margin-bottom:.2em}
#ui-peek img{display:block;max-width:100%;max-height:240px;margin:0 auto .5em;background:var(--img);border-radius:6px}
#ui-peek .pk-c{color:var(--muted);display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
dialog#ui-viewer{width:min(96vw,1400px);max-width:96vw;max-height:94vh;padding:0;border:0;border-radius:12px;background:var(--bg);color:var(--text)}
dialog#ui-viewer::backdrop{background:rgba(10,12,14,.78)}
.viewer-bar{position:sticky;top:0;display:flex;gap:8px;justify-content:flex-end;padding:10px 12px;background:var(--bg);border-bottom:1px solid var(--line);font:13px/1 var(--ui)}
.viewer-bar button{border:1px solid var(--line);background:var(--bg);border-radius:8px;padding:7px 12px}
.viewer-body{overflow:auto;max-height:calc(94vh - 52px);padding:14px}
.viewer-body img{display:block;max-width:100%;max-height:72vh;margin:auto;background:var(--img);cursor:zoom-in}
dialog.actual .viewer-body img{max-width:none;max-height:none;cursor:zoom-out}
.viewer-body figcaption{max-width:60rem;margin:1em auto 0}
@media (min-width:1320px){.figure,.table{margin-left:-4.5rem;margin-right:-4.5rem}}
@media (max-width:1180px){
 .layout{grid-template-columns:minmax(0,var(--measure));justify-content:center;padding:0 22px}
 main{grid-column:1}
 #ui-toc-toggle{display:inline-block}
 #ui-toc{position:fixed;top:52px;left:0;bottom:0;z-index:25;width:min(300px,86vw);max-height:none;padding:22px 10px 40px 4px;background:var(--bg);border-right:1px solid var(--line);box-shadow:var(--shadow);transform:translateX(-104%);transition:transform .22s}
 .toc-open #ui-toc{transform:none}
 .tools{display:none}
}
@media (max-width:640px){
 html{--fs:17px}
 .layout{padding:0 18px}
 h1{font-size:1.6rem}
 .article-head{padding-top:30px}
 .abstract{padding:1rem 1.05rem .8rem;margin-left:-.3rem;margin-right:-.3rem}
 .bar #ui-bilingual{padding:7px 6px}
 .eq-row{padding:0 2.2em}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
@media print{
 html,body{background:#fff;color:#000;font-size:10.5pt}
 .bar,#ui-toc,#ui-progress,.tools,#ui-back,#ui-resume,#ui-peek,dialog,.colophon{display:none!important}
 .layout{display:block;padding:0}
 .media,.reflist li{break-inside:avoid}
 .figure,.table{margin-left:0;margin-right:0}
 a{color:inherit}
}
'''

HEAD_SCRIPT = r'''try{var d=document.documentElement,p=JSON.parse(localStorage.getItem('pdf-chinese-reader:prefs')||'{}');
if(/^(serif|kai|sans)$/.test(p.font))d.dataset.font=p.font;
if(/^(light|sepia|dark)$/.test(p.theme))d.dataset.theme=p.theme;
if(/^(narrow|normal|wide)$/.test(p.width))d.dataset.width=p.width;
if(p.fs>=14&&p.fs<=26)d.style.setProperty('--fs',p.fs+'px');}catch(e){}'''

SCRIPT = r'''(()=>{
const $=s=>document.querySelector(s),$$=s=>Array.from(document.querySelectorAll(s));
const root=document.documentElement,body=document.body,narrow=()=>matchMedia('(max-width:1180px)').matches;
const PREF='pdf-chinese-reader:prefs',POS='pdf-chinese-reader:pos:'+body.dataset.key;
const store={get(k){try{return JSON.parse(localStorage.getItem(k)||'null')}catch(e){return null}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const prefs=Object.assign({font:root.dataset.font,theme:root.dataset.theme||'light',width:root.dataset.width||'normal',fs:parseInt(getComputedStyle(root).fontSize)||18,src:false},store.get(PREF)||{});
const blocks=$$('.blk[id]');
function anchor(){for(const b of blocks){const r=b.getBoundingClientRect();if(r.bottom>70)return{el:b,off:r.top};}return null}
function keep(fn){const a=anchor();fn();if(a)scrollTo(0,a.el.getBoundingClientRect().top+scrollY-a.off)}
function apply(){root.dataset.font=prefs.font;root.dataset.theme=prefs.theme;root.dataset.width=prefs.width;root.style.setProperty('--fs',prefs.fs+'px');
 body.classList.toggle('show-src',!!prefs.src);const bi=$('#ui-bilingual');if(bi)bi.setAttribute('aria-pressed',String(!!prefs.src));
 $('#ui-fs').textContent=prefs.fs;$$('.seg[data-pref] button').forEach(b=>b.setAttribute('aria-pressed',String(prefs[b.parentElement.dataset.pref]===b.dataset.v)));}
function save(){store.set(PREF,{font:prefs.font,theme:prefs.theme,width:prefs.width,fs:prefs.fs,src:prefs.src})}
apply();
$$('.seg[data-pref] button').forEach(b=>b.onclick=()=>{keep(()=>{prefs[b.parentElement.dataset.pref]=b.dataset.v;apply()});save()});
$('#ui-fs-down').onclick=()=>{keep(()=>{prefs.fs=Math.max(14,prefs.fs-1);apply()});save()};
$('#ui-fs-up').onclick=()=>{keep(()=>{prefs.fs=Math.min(26,prefs.fs+1);apply()});save()};
const bi=$('#ui-bilingual');if(bi)bi.onclick=()=>{keep(()=>{prefs.src=!prefs.src;apply()});save()};
const panel=$('#ui-settings'),sbtn=$('#ui-settings-btn');
sbtn.onclick=e=>{e.stopPropagation();panel.hidden=!panel.hidden;sbtn.setAttribute('aria-expanded',String(!panel.hidden))};
$('#ui-toc-toggle').onclick=e=>{e.stopPropagation();body.classList.toggle('toc-open')};
document.addEventListener('click',e=>{if(!panel.hidden&&!e.target.closest('#ui-settings'))panel.hidden=true;if(body.classList.contains('toc-open')&&!e.target.closest('#ui-toc'))body.classList.remove('toc-open')});
$$('.tool-src').forEach(b=>b.onclick=()=>b.closest('.blk').classList.toggle('open'));
// progress + title
const prog=$('#ui-progress');let ticking=false;
function onScroll(){const h=document.documentElement.scrollHeight-innerHeight;prog.style.width=(h>0?Math.min(100,scrollY/h*100):0)+'%';body.classList.toggle('scrolled',scrollY>260);ticking=false}
addEventListener('scroll',()=>{if(!ticking){ticking=true;requestAnimationFrame(onScroll)}},{passive:true});onScroll();
// toc highlight
const tocLinks=new Map($$('#ui-toc a').map(a=>[decodeURIComponent(a.hash.slice(1)),a]));
let current=null;
function setActive(id){const a=tocLinks.get(id);if(!a||a===current)return;if(current)current.classList.remove('active');current=a;a.classList.add('active');if(!narrow())a.scrollIntoView({block:'nearest'})}
if('IntersectionObserver' in window){const io=new IntersectionObserver(es=>{es.forEach(e=>{if(e.isIntersecting)setActive(e.target.id)})},{rootMargin:'-60px 0px -72% 0px'});tocLinks.forEach((a,id)=>{const t=document.getElementById(id);if(t)io.observe(t)})}
// jumps with dynamic return
const stack=[],back=$('#ui-back'),viewer=$('#ui-viewer');
function updateBack(){back.hidden=!stack.length;back.textContent='↩ 返回刚才位置'+(stack.length>1?' ('+stack.length+')':'')}
function flash(t){t.classList.remove('flash');void t.offsetWidth;t.classList.add('flash')}
document.addEventListener('click',e=>{const a=e.target.closest('a[href^="#"]');if(!a)return;const id=decodeURIComponent(a.hash.slice(1));const t=document.getElementById(id);if(!t)return;
 e.preventDefault();hidePeek();if(viewer.open)viewer.close();
 if(a.closest('#ui-toc')){body.classList.remove('toc-open')}else{stack.push(scrollY);updateBack()}
 history.replaceState(null,'','#'+id);const center=t.matches('li,figure');t.scrollIntoView({block:center?'center':'start'});t.focus({preventScroll:true});flash(center?t:(t.closest('.blk')||t))});
back.onclick=()=>{const y=stack.pop();if(y!=null)scrollTo(0,y);updateBack()};
// hover preview
const peek=$('#ui-peek');let pt;
function hidePeek(){clearTimeout(pt);peek.hidden=true}
function showPeek(a){const t=document.getElementById(decodeURIComponent(a.hash.slice(1)));if(!t)return;peek.replaceChildren();
 const h=document.createElement('span');h.className='pk-h';
 if(t.matches('li')){h.textContent='['+t.querySelector('.ref-no').textContent+']';peek.append(h,t.querySelector('.ref-text').cloneNode(true))}
 else{const img=t.querySelector('img'),cap=t.querySelector('figcaption');if(img){const c=img.cloneNode();c.removeAttribute('loading');peek.append(c)}
  if(cap){const c=document.createElement('div');c.className='pk-c';c.textContent=cap.textContent;peek.append(c)}}
 peek.hidden=false;const r=a.getBoundingClientRect(),w=peek.offsetWidth,ph=peek.offsetHeight;
 let top=scrollY+r.bottom+8;if(r.bottom+ph+16>innerHeight&&r.top>ph+16)top=scrollY+r.top-ph-8;
 peek.style.top=top+'px';peek.style.left=Math.max(8,Math.min(scrollX+r.left-24,scrollX+innerWidth-w-8))+'px'}
if(matchMedia('(hover:hover) and (pointer:fine)').matches){
 document.addEventListener('mouseover',e=>{const a=e.target.closest('.cite a,a.xref');if(a){clearTimeout(pt);pt=setTimeout(()=>showPeek(a),160)}else if(e.target.closest('#ui-peek'))clearTimeout(pt)});
 document.addEventListener('mouseout',e=>{if(e.target.closest('.cite a,a.xref,#ui-peek')){clearTimeout(pt);pt=setTimeout(hidePeek,220)}})}
// image viewer
const vimg=$('#ui-viewer-img'),vcap=$('#ui-viewer-cap');
document.addEventListener('click',e=>{const b=e.target.closest('button.zoom');if(!b)return;const img=b.querySelector('img'),cap=b.closest('figure').querySelector('figcaption');
 vimg.src=img.src;vimg.alt=img.alt;vcap.replaceChildren();if(cap)vcap.append(cap.cloneNode(true));viewer.classList.remove('actual');viewer.showModal();viewer.querySelector('.viewer-body').scrollTo(0,0)});
vimg.onclick=()=>viewer.classList.toggle('actual');$('#ui-viewer-size').onclick=()=>viewer.classList.toggle('actual');
$('#ui-viewer-close').onclick=()=>viewer.close();viewer.addEventListener('click',e=>{if(e.target===viewer)viewer.close()});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){panel.hidden=true;body.classList.remove('toc-open');hidePeek()}});
// resume reading
let st;addEventListener('scroll',()=>{clearTimeout(st);st=setTimeout(()=>{const a=anchor();if(a)store.set(POS,{id:a.el.id,off:a.off,y:scrollY,label:current?current.textContent:''})},400)},{passive:true});
const saved=store.get(POS),toast=$('#ui-resume');
if(saved&&saved.y>900&&!location.hash){$('#ui-resume-label').textContent='上次读到'+(saved.label?'：'+saved.label:'这里');toast.hidden=false;
 const hide=()=>{toast.hidden=true};setTimeout(hide,12000);$('#ui-resume-close').onclick=hide;
 $('#ui-resume-go').onclick=()=>{const el=document.getElementById(saved.id);scrollTo(0,el?el.getBoundingClientRect().top+scrollY-(saved.off||0):saved.y);hide()}}
})();'''


# --------------------------------------------------------------------------- text

def md_text(value):
    """Literal Markdown text: escape only syntax that would change meaning."""
    s = re.sub(r'([\\`*_\[\]<>~])', r'\\\1', str(value))
    return re.sub(r'&(?=#?\w+;)', '&amp;', s)


def md_line_starts(s):
    return re.sub(r'^([ \t]*)(#|>|[-+=](?=\s|$)|\d+(?=[.)]))',
                  lambda m: m[1] + ('\\' + m[2] if not m[2][0].isdigit() else m[2] + '\\'), s, flags=re.M)


def parse_ref_spec(spec):
    items = []
    for part in spec.split(','):
        m = re.fullmatch(r'\s*([1-9]\d*)\s*(?:[-–]\s*([1-9]\d*)\s*)?', part)
        if not m:
            raise ValueError(f'Malformed reference list: {spec}')
        a, b = int(m[1]), (int(m[2]) if m[2] else None)
        if b is not None and b <= a:
            raise ValueError(f'Reference range must increase: {spec}')
        items.append((a, b))
    return items


def pages_of(block):
    pages = block.get('pages')
    if pages is None and block.get('source_page') is not None:
        pages = [block['source_page']]
    return [p for p in (pages or []) if type(p) is int and p > 0]


def page_label(pages):
    if not pages:
        return ''
    lo, hi = min(pages), max(pages)
    return f'第 {lo} 页' if lo == hi else f'第 {lo}–{hi} 页'


class Inline:
    def __init__(self, fmt, targets):
        self.fmt, self.targets, self.links, self.citations = fmt, targets, [], 0

    def esc(self, s):
        if self.fmt == 'html':
            escaped = html.escape(s).replace('\n', '<br>')
            return re.sub(r'&lt;(/?(?:sub|sup|strong|b|i|em|code))&gt;', r'<\1>', escaped)
        return md_text(s).replace('\n', '  \n')

    def link(self, target, label, cls=''):
        if target not in self.targets:
            raise ValueError(f'Missing link target: {target}')
        self.links.append(target)
        if self.fmt == 'html':
            c = f' class="{cls}"' if cls else ''
            return f'<a{c} href="#{target}">{label}</a>'
        return f'[{label}](#{target})'

    def __call__(self, text):
        if not isinstance(text, str):
            raise ValueError('Text fields must be strings')
        # Pre-normalize figure markers like [Fig:1a] or [Fig:1]
        text = re.sub(r'\[(?:Fig|fig|Figure|figure):([1-9]\d*)([a-zA-Z]*)\]', r'[[fig:\1|图 \1\2]]', text)
        # Scientific superscripts remain literal; citations require explicit markers.

        out, start = [], 0
        for m in INLINE.finditer(text):
            out.append(self.esc(text[start:m.start()]))
            start = m.end()
            kind, spec, label, sup, sub = m.groups()
            if sup is not None or sub is not None:
                tag = 'sup' if sup is not None else 'sub'
                out.append(f'<{tag}>{self.esc(sup if sup is not None else sub)}</{tag}>')
                continue
            if kind == 'ref' and not label:
                parts = []
                for a, b in parse_ref_spec(spec):
                    if b is None:
                        parts.append(self.link(f'ref-{a}', str(a)))
                    else:
                        for n in range(a, b + 1):
                            if f'ref-{n}' not in self.targets:
                                raise ValueError(f'Missing link target: ref-{n}')
                        parts.append(self.link(f'ref-{a}', str(a)) + '–' + self.link(f'ref-{b}', str(b)))
                self.citations += 1
                joined = ','.join(parts)
                out.append(f'<sup class="cite">{joined}</sup>' if self.fmt == 'html' else f'<sup>{joined}</sup>')
                continue
            if not re.fullmatch(r'[1-9]\d*', spec.strip()):
                raise ValueError(f'Malformed {kind} marker: {spec}')
            n = spec.strip()
            default = f'{LABELS[kind]} ({n})' if kind == 'eq' else f'{LABELS[kind]} {n}'
            out.append(self.link(f'{kind}-{n}', self.esc(label or default), 'xref'))
        out.append(self.esc(text[start:]))
        if re.search(r'\[\[(?:fig|ref|eq|table):', INLINE.sub('', text)):
            raise ValueError('Malformed citation marker')
        return ''.join(out)


# --------------------------------------------------------------------------- fonts

def latin_faces():
    rules = []
    for style, weight, names in LATIN_FACES:
        src = ','.join(f'local("{n}")' for n in names)
        rules.append(f'@font-face{{font-family:"RL Latin Serif";src:{src};font-style:{style};'
                     f'font-weight:{weight};unicode-range:{LATIN_RANGE}}}')
    return ''.join(rules)


def embed_fonts(specs, chars):
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        raise ValueError('--embed-font needs fontTools: python -m pip install fonttools brotli') from exc
    try:
        import brotli  # noqa: F401
        flavor, mime = 'woff2', 'font/woff2'
    except ImportError:
        flavor, mime = 'woff', 'font/woff'
    faces, roles, info = [], set(), []
    text = ''.join(sorted(chars))
    for spec in specs:
        role, _, path = spec.partition('=')
        if role not in FONT_ROLES or not path:
            raise ValueError('--embed-font expects serif=PATH, sans=PATH or kai=PATH')
        number = 0
        m = re.fullmatch(r'(.+\.(?:ttc|otc))#(\d+)', path, re.I)
        if m:
            path, number = m[1], int(m[2])
        font = TTFont(path, fontNumber=number) if path.lower().endswith(('.ttc', '.otc')) else TTFont(path)
        fs_type = font['OS/2'].fsType if 'OS/2' in font else 0
        if fs_type & 0x0002:
            raise ValueError(f'{path} does not allow embedding (OS/2 fsType restricted)')
        axis = next((a for a in font['fvar'].axes if a.axisTag == 'wght'), None) if 'fvar' in font else None
        opts = subset.Options()
        opts.flavor, opts.layout_features, opts.hinting, opts.notdef_outline = flavor, ['*'], False, True
        sub = subset.Subsetter(opts)
        sub.populate(text=text)
        sub.subset(font)
        buf = io.BytesIO()
        font.flavor = flavor
        font.save(buf)
        data = buf.getvalue()
        weight = f'{int(axis.minValue)} {int(axis.maxValue)}' if axis else str(font['OS/2'].usWeightClass)
        style = 'italic' if font['OS/2'].fsSelection & 1 else 'normal'
        faces.append(f'@font-face{{font-family:"{FONT_ROLES[role]}";src:url(data:{mime};base64,'
                     f'{base64.b64encode(data).decode("ascii")}) format("{flavor}");font-weight:{weight};'
                     f'font-style:{style};font-display:swap}}')
        roles.add(role)
        info.append({'role': role, 'file': Path(path).name, 'bytes': len(data), 'variable': bool(axis),
                     'fs_type': fs_type, 'glyph_chars': len(chars)})
    return ''.join(faces), roles, info


def font_vars(embedded, serif_weight):
    def stack(role, latin_first):
        lead = f'"{FONT_ROLES[role]}",' if role in embedded else ('"RL Latin Serif",' if latin_first else '')
        tail = 'sans-serif' if role == 'sans' else 'serif'
        return f'{lead}{CJK_STACKS[role]},{tail}'
    sans = stack('sans', False).replace(',sans-serif', ',system-ui,sans-serif')
    return (f':root{{--stack-serif:{stack("serif", True)};--stack-kai:{stack("kai", True)};'
            f'--stack-sans:{sans};--serif-weight:{serif_weight}}}')


# --------------------------------------------------------------------------- build

def validate(m, manifest_dir):
    if not isinstance(m.get('title'), str) or not m['title'].strip():
        raise ValueError('Manifest needs a non-empty title')
    blocks, refs = m['blocks'], m.get('references', [])
    ids, targets, assets = set(), set(), {}
    for b in blocks:
        ident = b.get('id')
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident) or ident in ids or ident.startswith('ui-'):
            raise ValueError(f'Invalid or duplicate block ID: {ident}')
        ids.add(ident)
        kind = b.get('type', 'paragraph')
        if kind not in TEXT_TYPES and kind not in MEDIA:
            raise ValueError(f'Unsupported block type: {kind}')
        if kind == 'heading' and b.get('level', 2) not in (2, 3, 4):
            raise ValueError('Heading level must be 2, 3 or 4')
        if kind in MEDIA:
            n = b.get('number')
            if n is None and kind == 'figure':
                raise ValueError('Figures need a positive integer number')
            if n is not None:
                if type(n) is not int or n < 1:
                    raise ValueError('Media number must be a positive integer')
                target = f'{MEDIA[kind]}-{n}'
                if target in targets:
                    raise ValueError(f'Duplicate media target: {target}')
                targets.add(target)
            asset = (manifest_dir / b['asset']).resolve()
            if not asset.is_relative_to(manifest_dir) or asset.suffix.lower() not in MIME:
                raise ValueError('Assets must be PNG/JPEG/WebP inside the manifest folder')
            assets[ident] = (asset, asset.suffix.lower(), asset.read_bytes())
            if 'width_pt' in b and not (isinstance(b['width_pt'], (int, float)) and 0 < b['width_pt'] < 2000):
                raise ValueError('width_pt must be a positive number of PDF points')
        text = b.get('translation', b.get('text_zh', ''))
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f'Every block needs reviewed Chinese text or a caption: {ident}')
    numbers = [r.get('number') for r in refs]
    if any(type(n) is not int or n < 1 for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError('Invalid or duplicate reference numbers')
    for r in refs:
        if not isinstance(r.get('text'), str) or not r['text'].strip():
            raise ValueError('Empty reference')
    targets.update(f'ref-{n}' for n in numbers)
    if ids & targets:
        raise ValueError('Block IDs collide with link targets')
    if 'expected_block_ids' in m and set(m['expected_block_ids']) != ids:
        raise ValueError('Source block coverage mismatch')
    return targets, assets


def build(manifest, output, fmt, theme='serif', embed=(), serif_weight=450):
    manifest, output = Path(manifest).resolve(), Path(output).resolve()
    if fmt not in ('md', 'html'):
        raise ValueError('format must be md or html')
    if theme not in THEMES:
        raise ValueError('theme must be serif, kai or sans')
    m = json.loads(manifest.read_text(encoding='utf-8-sig'))
    targets, assets = validate(m, manifest.parent)
    blocks, refs, meta = m['blocks'], sorted(m.get('references', []), key=lambda r: r['number']), m.get('meta') or {}
    inline = Inline(fmt, targets)
    esc = inline.esc
    report_path = output.with_suffix(output.suffix + '.validation.json')
    inputs = {manifest, *(a[0] for a in assets.values())}
    if output in inputs or report_path in inputs:
        raise ValueError('Output must not overwrite input')
    source_pdf = meta.get('source_pdf')
    if source_pdf is not None and not (isinstance(source_pdf, str) and re.fullmatch(r'[^:?#<>"]+\.pdf', source_pdf, re.I)
                                       and not source_pdf.startswith(('/', '\\'))):
        raise ValueError('meta.source_pdf must be a relative .pdf path')
    doi = meta.get('doi')
    if doi is not None and not (isinstance(doi, str) and re.fullmatch(r'10\.\d{4,9}/\S+', doi)):
        raise ValueError('meta.doi must look like 10.xxxx/yyyy')

    if fmt == 'md':
        content, pending = render_md(m, meta, blocks, refs, assets, inline)
    else:
        content, toc_items = render_html_body(m, meta, blocks, refs, assets, inline, source_pdf)
    stats = {'internal_links': len(inline.links), 'citations': inline.citations}

    fonts_info = []
    if fmt == 'html':
        chars = set(html.unescape(re.sub(r'<[^>]+>', '', content))) | set(
            '目录阅读设置字体宋楷黑号背景白米黄夜间行宽窄中返回刚才位置上次读到继续这里关闭原始尺寸放大图片中英对照原文第页'
            '0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.,:;!?()[]{}-–—"\'/+=%&*#@~·…“”‘’，。、；：？！（）【】《》')
        face_css, embedded, fonts_info = embed_fonts(embed, chars) if embed else ('', set(), [])
        title = esc(m['title'])
        key = hashlib.sha256(manifest.read_bytes()).hexdigest()[:24]
        has_src = any(isinstance(b.get('source_text', b.get('text_en')), str) for b in blocks)
        content = page_shell(title, key, theme, content, toc_items, has_src,
                             latin_faces() + face_css + font_vars(embedded, serif_weight))
    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == 'md':
        for name, data in pending.items():
            asset_out = output.parent / 'assets' / name
            asset_out.parent.mkdir(exist_ok=True)
            asset_out.write_bytes(data)
    output.write_text(content, encoding='utf-8')
    report = {'format': fmt, 'theme': theme if fmt == 'html' else None, 'blocks': len(blocks),
              'references': len(refs), 'images': len(assets), **stats, 'fonts_embedded': fonts_info,
              'bytes': output.stat().st_size, 'targets_valid': True,
              'semantic_accuracy': 'REQUIRES REVIEW', 'reader_click_test': 'NOT TESTED'}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def block_text(b):
    return b.get('translation', b.get('text_zh', ''))


def source_of(b):
    s = b.get('source_text', b.get('text_en'))
    return s if isinstance(s, str) and s.strip() else None


def render_authors(meta, inline):
    authors = meta.get('authors') or []
    out = []
    for a in authors:
        if isinstance(a, str):
            out.append(inline(a))
            continue
        name = inline(a['name'])
        aff = a.get('affiliations') or []
        if aff and not all(type(x) is int and x > 0 for x in aff):
            raise ValueError('Author affiliations must be positive integers')
        sup = f'<sup>{",".join(map(str, aff))}</sup>' if aff else ''
        mail = '<span class="mail" title="通讯作者">✉</span>' if a.get('corresponding') else ''
        out.append(f'<span class="au">{name}{sup}{mail}</span>')
    return ', '.join(out)


def render_html_body(m, meta, blocks, refs, assets, inline, source_pdf):
    esc = inline.esc
    head = ['<header class="article-head" id="ui-top">']
    kick = [esc(x) for x in (meta.get('journal'), meta.get('article_type')) if isinstance(x, str) and x.strip()]
    if kick:
        head.append('<div class="kicker">' + ''.join(
            f'<span class="k{i + 1}">{k}</span>' for i, k in enumerate(kick)) + '</div>')
    head.append(f'<h1>{esc(m["title"])}</h1>')
    if isinstance(meta.get('title_en'), str):
        head.append(f'<p class="title-en" lang="en">{esc(meta["title_en"])}</p>')
    if meta.get('authors'):
        head.append(f'<p class="authors">{render_authors(meta, inline)}</p>')
    affs, corr = meta.get('affiliations') or [], meta.get('correspondence')
    if affs or corr:
        items = ''.join(f'<li>{inline(a)}</li>' for a in affs)
        corr_html = f'<p>通讯：{inline(corr)}</p>' if isinstance(corr, str) else ''
        head.append(f'<details class="affil"><summary>作者单位与通讯方式</summary><ol>{items}</ol>{corr_html}</details>')
    meta_items = []
    dates = meta.get('dates') or {}
    for label, value in (dates.items() if isinstance(dates, dict) else dates):
        meta_items.append((esc(label), esc(value)))
    if isinstance(meta.get('citation'), str):
        meta_items.append(('引用', esc(meta['citation'])))
    if meta.get('doi'):
        d = meta['doi']
        meta_items.append(('DOI', f'<a href="https://doi.org/{html.escape(d)}" target="_blank" rel="noopener">{esc(d)}</a>'))
    if source_pdf:
        meta_items.append(('原文', f'<a href="{html.escape(quote(source_pdf))}" target="_blank" rel="noopener">PDF</a>'))
    if meta_items:
        head.append('<dl class="meta">' + ''.join(f'<div><dt>{k}</dt><dd>{v}</dd></div>' for k, v in meta_items) + '</dl>')

    def tools(b):
        pages, src = pages_of(b), source_of(b)
        bits = []
        if src:
            bits.append('<button class="tool-src" type="button" title="显示/隐藏本段原文">原文</button>')
        if pages:
            label = esc(page_label(pages))
            bits.append(f'<a href="{html.escape(quote(source_pdf))}#page={min(pages)}" target="_blank" rel="noopener">{label}</a>'
                        if source_pdf else f'<span>{label}</span>')
        return f'<span class="tools">{"".join(bits)}</span>' if bits else ''

    def src_html(b):
        src = source_of(b)
        if not src:
            return ''
        pg = page_label(pages_of(b))
        return f'<div class="src" lang="en"><span class="src-h">原文{" · " + esc(pg) if pg else ""}</span>{inline(src)}</div>'

    abstract = [b for b in blocks if b.get('type') == 'abstract']
    if abstract:
        label = esc(meta.get('abstract_label', '摘要'))
        parts = ''.join(f'<div class="blk" id="{b["id"]}" tabindex="-1"><p>{inline(block_text(b))}</p>{src_html(b)}{tools(b)}</div>'
                        for b in abstract)
        head.append(f'<section class="abstract" id="ui-abstract"><h2 class="abs-label">{label}</h2>{parts}</section>')
    if isinstance(m.get('scope'), str) and m['scope'].strip():
        head.append(f'<p class="scope">{esc(m["scope"])}</p>')
    head.append('</header>')

    toc = [('ui-abstract', esc(meta.get('abstract_label', '摘要')), 2)] if abstract else []
    body = []
    font_pt = float(m.get('source_font_pt', 9.5))
    for b in blocks:
        ident, kind = b['id'], b.get('type', 'paragraph')
        if kind == 'abstract':
            continue
        text = inline(block_text(b))
        if kind == 'heading':
            lvl = b.get('level', 2)
            if lvl in (2, 3):
                toc.append((ident, text, lvl))
            body.append(f'<h{lvl} id="{ident}" tabindex="-1">{text}</h{lvl}>')
        elif kind in MEDIA:
            _, ext, data = assets[ident]
            n = b.get('number')
            mk = MEDIA[kind]
            fid = f' id="{mk}-{n}"' if n else ''
            label = (f'{LABELS[mk]} ({n})' if kind == 'equation' else f'{LABELS[mk]} {n}') if n else ''
            alt = esc(b.get('alt') or label or LABELS[mk])
            src = f'data:{MIME[ext]};base64,' + base64.b64encode(data).decode('ascii')
            width = f' style="width:{b["width_pt"] / font_pt:.2f}rem"' if kind == 'equation' and b.get('width_pt') else ''
            img = f'<button class="zoom" type="button" aria-label="放大{alt}"><img src="{src}" alt="{alt}"{width} loading="lazy" decoding="async"></button>'
            if kind == 'equation':
                no = f'<span class="eq-no">({n})</span>' if n else ''
                fig = f'<figure class="equation"{fid} tabindex="-1"><div class="eq-row">{img}{no}</div><figcaption>{text}</figcaption></figure>'
            else:
                title = f'<span class="cap-title">{inline(b["title"])}</span> ' if isinstance(b.get('title'), str) else ''
                cap = f'<figcaption><span class="cap-label">{esc(label)}</span>{title}{text}</figcaption>'
                fig = (f'<figure class="{kind}"{fid} tabindex="-1">' + (cap + img if kind == 'table' else img + cap) + '</figure>')
            body.append(f'<div class="blk media" id="{ident}" tabindex="-1">{fig}{src_html(b)}{tools(b)}</div>')
        else:
            cls = 'blk note' if kind == 'note' else 'blk'
            body.append(f'<div class="{cls}" id="{ident}" tabindex="-1"><p>{text}</p>{src_html(b)}{tools(b)}</div>')
    if refs:
        toc.append(('ui-references', '参考文献', 2))
        items = ''.join(f'<li id="ref-{r["number"]}" tabindex="-1"><span class="ref-no">{r["number"]}</span>'
                        f'<span class="ref-text">{inline(r["text"])}</span></li>' for r in refs)
        body.append(f'<section class="references"><h2 id="ui-references" tabindex="-1">参考文献</h2><ol class="reflist">{items}</ol></section>')
    note = ('中文阅读版由 pdf-chinese-reader 生成。译文仅供阅读参考，术语、数值与结论请以原文为准；'
            '参考文献保留原文书目信息。')
    body.append(f'<footer class="colophon">{esc(note)}</footer>')
    return '\n'.join(head + body), toc


def page_shell(title, key, theme, content, toc, has_src, font_css):
    toc_html = ''.join(f'<li class="l{lvl}"><a href="#{ident}">{label}</a></li>' for ident, label, lvl in toc)
    bilingual = '<button id="ui-bilingual" type="button" aria-pressed="false" title="逐段显示英文原文">中英对照</button>' if has_src else ''
    seg = lambda pref, opts: f'<div class="seg" data-pref="{pref}">' + ''.join(
        f'<button type="button" data-v="{v}">{t}</button>' for v, t in opts) + '</div>'
    settings = ('<div id="ui-settings" class="panel" hidden role="dialog" aria-label="阅读设置">'
                f'<div class="row"><span>字体</span>{seg("font", (("serif", "宋体"), ("kai", "楷体"), ("sans", "黑体")))}</div>'
                '<div class="row"><span>字号</span><div class="seg"><button id="ui-fs-down" type="button" aria-label="减小字号">A−</button>'
                '<output id="ui-fs">18</output><button id="ui-fs-up" type="button" aria-label="增大字号">A+</button></div></div>'
                f'<div class="row"><span>背景</span>{seg("theme", (("light", "白"), ("sepia", "米黄"), ("dark", "夜间")))}</div>'
                f'<div class="row"><span>行宽</span>{seg("width", (("narrow", "窄"), ("normal", "中"), ("wide", "宽")))}</div></div>')
    return ('<!doctype html><html lang="zh-CN" data-font="' + theme + '" data-theme="light" data-width="normal"><head>'
            '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="color-scheme" content="light dark"><title>{title}</title>'
            f'<script>{HEAD_SCRIPT}</script><style>{font_css}{CSS}</style></head>'
            f'<body data-key="{key}"><div id="ui-progress"></div>'
            '<div class="bar"><button id="ui-toc-toggle" type="button" aria-label="目录">☰</button>'
            f'<span class="bar-title">{title}</span>{bilingual}'
            '<button id="ui-settings-btn" type="button" aria-label="阅读设置" aria-expanded="false">Aa 设置</button></div>'
            f'{settings}<div class="layout"><nav id="ui-toc" aria-label="目录"><div class="toc-title">目录</div><ol>{toc_html}</ol></nav>'
            f'<main><article>{content}</article></main></div>'
            '<button id="ui-back" type="button" hidden>↩ 返回刚才位置</button>'
            '<div id="ui-resume" hidden role="status"><span id="ui-resume-label"></span>'
            '<button id="ui-resume-go" type="button">继续阅读</button><button id="ui-resume-close" type="button" aria-label="关闭">✕</button></div>'
            '<div id="ui-peek" hidden role="tooltip"></div>'
            '<dialog id="ui-viewer" aria-label="图片放大"><div class="viewer-bar"><button id="ui-viewer-size" type="button">原始尺寸</button>'
            '<button id="ui-viewer-close" type="button">关闭</button></div><div class="viewer-body"><img id="ui-viewer-img" alt="">'
            f'<div id="ui-viewer-cap"></div></div></dialog><script>{SCRIPT}</script></body></html>')


def render_md(m, meta, blocks, refs, assets, inline):
    esc = inline.esc
    parts = [f'# {esc(m["title"])}']
    if isinstance(meta.get('title_en'), str):
        parts.append(f'*{esc(meta["title_en"])}*')
    if meta.get('authors'):
        names = []
        for a in meta['authors']:
            if isinstance(a, str):
                names.append(inline(a))
            else:
                aff = ','.join(map(str, a.get('affiliations') or []))
                names.append(inline(a['name']) + (f'<sup>{aff}</sup>' if aff else '') + ('✉' if a.get('corresponding') else ''))
        parts.append(', '.join(names))
    info = []
    for k in ('journal', 'article_type', 'citation'):
        if isinstance(meta.get(k), str):
            info.append(esc(meta[k]))
    dates = meta.get('dates') or {}
    info += [f'{esc(k)} {esc(v)}' for k, v in (dates.items() if isinstance(dates, dict) else dates)]
    if meta.get('doi'):
        info.append(f'DOI [{esc(meta["doi"])}](https://doi.org/{meta["doi"]})')
    if info:
        parts.append(' · '.join(info))
    abstract = [b for b in blocks if b.get('type') == 'abstract']
    if abstract:
        lines = [f'> **{esc(meta.get("abstract_label", "摘要"))}**', '>']
        for b in abstract:
            lines.append(f'> <a id="{b["id"]}"></a>' + inline(block_text(b)).replace('\n', '\n> '))
        parts.append('\n'.join(lines))
    if isinstance(m.get('scope'), str) and m['scope'].strip():
        parts.append(f'*{esc(m["scope"])}*')
    affs = meta.get('affiliations') or []
    if affs:
        parts.append('<details><summary>作者单位</summary>\n\n' + '\n'.join(
            f'{i}\\. {inline(a)}  ' for i, a in enumerate(affs, 1)) + '\n\n</details>')
    pending = {}
    for b in blocks:
        ident, kind = b['id'], b.get('type', 'paragraph')
        if kind == 'abstract':
            continue
        text = md_line_starts(inline(block_text(b)))
        anchor = f'<a id="{ident}"></a>'
        if kind in MEDIA:
            _, ext, data = assets[ident]
            n, mk = b.get('number'), MEDIA[kind]
            label = (f'{LABELS[mk]} ({n})' if kind == 'equation' else f'{LABELS[mk]} {n}') if n else ''
            alt = md_text(b.get('alt') or label or LABELS[mk])
            name = hashlib.sha256(data).hexdigest()[:16] + ext
            pending[name] = data
            target = f'<a id="{mk}-{n}"></a>' if n else ''
            title = f' **{inline(b["title"])}**' if isinstance(b.get('title'), str) else ''
            cap = (f'**{esc(label)}**' if label and kind != 'equation' else '') + title + ' ' + text
            parts.append(f'{anchor}{target}\n\n![{alt}](assets/{name})\n\n{cap.strip()}')
        elif kind == 'heading':
            parts.append('#' * b.get('level', 2) + f' {anchor}{text}')
        else:
            parts.append(anchor + text)
    if refs:
        parts.append('## 参考文献')
        for r in refs:
            parts.append(f'<a id="ref-{r["number"]}"></a>**{r["number"]}.** {inline(r["text"])}')
    return '\n\n'.join(parts) + '\n', pending


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--manifest', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--format', choices=['md', 'html'], required=True)
    p.add_argument('--theme', choices=THEMES, default='serif',
                   help='HTML default body font: serif 宋体 (default), kai 楷体/文楷, sans 黑体; readers can switch in 设置')
    p.add_argument('--embed-font', action='append', default=[], metavar='ROLE=PATH',
                   help='subset and inline an OFL font, e.g. serif=C:/Windows/Fonts/NotoSerifSC-VF.ttf (repeatable)')
    p.add_argument('--serif-weight', type=int, default=450,
                   help='body weight for the serif theme; 450 suits variable Source Han/Noto Serif on Windows')
    args = p.parse_args()
    print(json.dumps(build(args.manifest, args.output, args.format, args.theme, args.embed_font, args.serif_weight),
                     ensure_ascii=False))
