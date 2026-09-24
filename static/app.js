/* IPO Desk front end.
   Streams SSE from /api/chat, renders markdown, drives the rail. */

const $ = id => document.getElementById(id);
const thread = $('thread'), scroll = $('scroll'), input = $('input'), send = $('send');
let busy = false;
let ipoType = 'mainboard';

/* Server-side memory is keyed by thread_id, so clearing the screen alone would
   leave the model still remembering everything. Rotating the id is what
   actually starts a fresh conversation. */
const newThreadId = () =>
  'web-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
let threadId = newThreadId();

// Snapshot the empty state now, so it can be put back after a clear.
const openingHTML = $('opening') ? $('opening').outerHTML : '';

const TYPE_LABEL = {mainboard:'Mainboard · open', sme:'SME · open', all:'All · open'};

const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const atBottom = () => scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 90;
const toBottom = () => scroll.scrollTop = scroll.scrollHeight;

/* ---------- markdown ----------
   Deliberately small and escape-first: input is escaped before any tag is
   introduced, so model output can't inject markup. Handles what the agent
   actually emits — tables, lists, bold, code, links — and nothing more. */

function mdInline(s){
  return s
    .replace(/`([^`]+)`/g, (_,c) => `<code>${c}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, (_,c) => `<strong>${c}</strong>`)
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, (_,p,c) => `${p}<em>${c}</em>`)
    // &amp; is unwound inside hrefs only -- query strings would break otherwise
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_,t,u) => `<a href="${u.replace(/&amp;/g,'&')}" target="_blank" rel="noopener">${t}</a>`)
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g,
      (_,p,u) => `${p}<a href="${u.replace(/&amp;/g,'&')}" target="_blank" rel="noopener">${u}</a>`);
}

const isTableSep = l => /-{2,}/.test(l) && /^[\s|:-]+$/.test(l);
const cellsOf = r => r.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(c => c.trim());

function md(src){
  const lines = esc(src).replace(/\r/g,'').split('\n');
  let out = '', i = 0;

  const startsTable = n =>
    lines[n].includes('|') && n+1 < lines.length && isTableSep(lines[n+1]);

  while(i < lines.length){
    const line = lines[i];

    if(startsTable(i)){
      const head = cellsOf(line);
      i += 2;
      const rows = [];
      while(i < lines.length && lines[i].includes('|') && lines[i].trim()){
        rows.push(cellsOf(lines[i])); i++;
      }
      out += '<div class="tw"><table><thead><tr>'
        + head.map(h => `<th>${mdInline(h)}</th>`).join('')
        + '</tr></thead><tbody>'
        + rows.map(r => '<tr>' + r.map(c => `<td>${mdInline(c)}</td>`).join('') + '</tr>').join('')
        + '</tbody></table></div>';
      continue;
    }

    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if(h){
      const lvl = Math.min(h[1].length + 2, 5);
      out += `<h${lvl}>${mdInline(h[2])}</h${lvl}>`; i++; continue;
    }

    if(/^\s*[-*•]\s+/.test(line)){
      const items = [];
      while(i < lines.length && /^\s*[-*•]\s+/.test(lines[i])){
        items.push(lines[i].replace(/^\s*[-*•]\s+/,'')); i++;
      }
      out += '<ul>' + items.map(t => `<li>${mdInline(t)}</li>`).join('') + '</ul>';
      continue;
    }

    if(/^\s*\d+[.)]\s+/.test(line)){
      const items = [];
      while(i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])){
        items.push(lines[i].replace(/^\s*\d+[.)]\s+/,'')); i++;
      }
      out += '<ol>' + items.map(t => `<li>${mdInline(t)}</li>`).join('') + '</ol>';
      continue;
    }

    if(!line.trim()){ i++; continue; }

    const para = [];
    while(i < lines.length && lines[i].trim()
          && !/^\s*[-*•]\s+/.test(lines[i])
          && !/^\s*\d+[.)]\s+/.test(lines[i])
          && !/^#{1,4}\s/.test(lines[i])
          && !startsTable(i)){
      para.push(lines[i]); i++;
    }
    out += `<p>${mdInline(para.join(' '))}</p>`;
  }
  return out;
}

/* ---------- rail ---------- */
async function loadBoard(){
  try{
    const r = await fetch(`/api/board?type=${encodeURIComponent(ipoType)}`);
    const d = await r.json();
    const list = $('rail-list');
    const head = document.querySelector('.rail-head span');
    if(head) head.textContent = TYPE_LABEL[ipoType] || 'Open now';

    if(d.error){ list.innerHTML = `<div class="rail-empty">${esc(d.error)}</div>`; $('live-count').textContent='source down'; return; }

    $('rail-date').textContent = d.today ? d.today.slice(5) : '';
    $('live-count').textContent = `${d.open.length} open · ${d.upcoming.length} upcoming`;

    if(!d.open.length){
      list.innerHTML = `<div class="rail-empty">Nothing ${ipoType === 'all' ? '' : ipoType + ' '}open today. Ask what's coming up.</div>`;
      return;
    }

    list.innerHTML = d.open.map(o => {
      const n = o.days_until_close;
      const today = n === 0;
      const cls = today ? 'soon' : (n <= 2 ? 'soon' : 'safe');
      const label = today ? 'last day' : `${n}d left`;
      return `<button class="entry ${today?'today':''}" data-q="Tell me about the ${esc(o.name)} IPO and its deadline">
        <div class="entry-name">${esc(o.name)}</div>
        <div class="entry-meta"><span>closes ${esc(o.close_date.slice(5))}</span><span class="days ${cls}">${today?'':label}</span></div>
        ${today ? '<span class="stamp">Closes<br>today</span>' : ''}
      </button>`;
    }).join('');
  }catch(e){
    $('rail-list').innerHTML = `<div class="rail-empty">Couldn't load the register. Is the server running?</div>`;
  }
}

/* ---------- rendering ---------- */
function addTurn(who, text){
  const wrap = document.createElement('div');
  wrap.className = who === 'you' ? 'from-you' : 'from-desk';
  wrap.innerHTML = `<div class="turn-label">${who === 'you' ? 'You' : 'Desk'}</div><div class="text"></div>`;
  wrap.querySelector('.text').textContent = text || '';
  thread.appendChild(wrap);
  toBottom();
  return wrap.querySelector('.text');
}

function addTrace(html, parent){
  const el = document.createElement('div');
  el.className = 'trace';
  el.innerHTML = html;
  (parent || thread).appendChild(el);
  if(atBottom()) toBottom();
  return el;
}

let stepSeq = 0;

/* One folding container per turn. Open while the agent works so you can watch
   it, folded the moment the answer starts so attention moves to the answer.
   Re-openable, because the whole point of showing tool calls is being able to
   check them. */
function addSteps(){
  const id = `steps-${++stepSeq}`;
  const el = document.createElement('div');
  el.className = 'steps';
  el.dataset.open = '1';
  el.innerHTML = `
    <button class="steps-head working" aria-expanded="true" aria-controls="${id}">
      <span class="caret-tri">&#9654;</span>
      <span class="steps-label">Checking the register</span>
      <span class="steps-rule"></span>
    </button>
    <div class="steps-panel"><div class="steps-inner" id="${id}"></div></div>`;
  thread.appendChild(el);

  const head = el.querySelector('.steps-head');
  const label = el.querySelector('.steps-label');
  const inner = el.querySelector('.steps-inner');

  const setOpen = open => {
    el.dataset.open = open ? '1' : '0';
    head.setAttribute('aria-expanded', String(!!open));
  };
  head.addEventListener('click', () => setOpen(el.dataset.open === '0'));

  return {
    inner,
    fold: () => setOpen(false),
    finish(names){
      head.classList.remove('working');
      const n = names.length;
      label.textContent = n
        ? `${n} lookup${n > 1 ? 's' : ''} · ${[...new Set(names)].join(', ')}`
        : 'No lookups';
    },
    remove: () => el.remove(),
  };
}

/* ---------- turn ---------- */
async function askQuestion(q){
  if(busy || !q.trim()) return;
  busy = true; send.disabled = true;
  clearBtn.disabled = true;
  disarm();                       // a pending confirm is stale once a new turn starts
  $('opening')?.remove();

  addTurn('you', q);
  input.value = ''; input.style.height = 'auto';

  let body = null, buffer = '', pending = false, stick = true;
  const steps = addSteps();
  const called = [];

  try{
    const res = await fetch('/api/chat', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ message:q, thread_id: threadId, ipo_type: ipoType })
    });
    if(!res.ok) throw new Error(`server returned ${res.status}`);

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let carry = '';

    while(true){
      const {value, done} = await reader.read();
      if(done) break;
      carry += dec.decode(value, {stream:true});

      const frames = carry.split('\n\n');
      carry = frames.pop();

      for(const frame of frames){
        const line = frame.split('\n').find(l => l.startsWith('data: '));
        if(!line) continue;
        const ev = JSON.parse(line.slice(6));

        if(ev.event === 'tool_call'){
          called.push(ev.name);
          const args = Object.keys(ev.args||{}).length ? JSON.stringify(ev.args) : '';
          addTrace(`<b>&rarr; ${esc(ev.name)}</b>${args ? ' ' + esc(args) : ''}`, steps.inner);
        }
        else if(ev.event === 'tool_result'){
          const size = ev.count != null
            ? `${ev.count} record${ev.count === 1 ? '' : 's'}`
            : `${ev.length} chars`;
          addTrace(`<span class="ret">&larr; ${esc(ev.name)} · ${esc(size)}</span>`, steps.inner);
        }
        else if(ev.event === 'token'){
          if(!body){
            steps.fold();          // answer is arriving; get the scaffolding out of the way
            body = addTurn('desk','');
          }
          buffer += ev.text;
          // Re-render the whole buffer each frame rather than per token. A
          // half-arrived table has no separator row yet, so it briefly reads
          // as a paragraph and then snaps into a table -- re-rendering is what
          // makes that correct rather than permanently mangled.
          if(!pending){
            pending = true;
            requestAnimationFrame(() => {
              pending = false;
              body.innerHTML = md(buffer);
              if(stick) toBottom();
            });
          }
          stick = atBottom();
        }
        else if(ev.event === 'error'){
          const f = document.createElement('div');
          f.className = 'fault';
          f.textContent = `That turn failed — ${ev.message}. Check the server log and try again.`;
          thread.appendChild(f); toBottom();
        }
      }
    }
    if(!body && !buffer){
      addTrace('<span class="ret">No answer came back. Try rephrasing.</span>', steps.inner);
    }
    // The rAF throttle can swallow the last chunk; render once more to be sure.
    if(body){ body.innerHTML = md(buffer); }
  }catch(e){
    const f = document.createElement('div');
    f.className = 'fault';
    f.textContent = `Lost the connection — ${e.message}. Is the server still running?`;
    thread.appendChild(f); toBottom();
  }finally{
    // A turn with no tool calls leaves an empty container; drop it.
    if(called.length) steps.finish(called); else steps.remove();
    busy = false; send.disabled = false; clearBtn.disabled = false; input.focus();
    loadBoard();
  }
}

/* ---------- clear ---------- */
const clearBtn = $('clear');
let armed = false, armTimer = null;

function disarm(){
  armed = false;
  clearTimeout(armTimer);
  clearBtn.classList.remove('armed');
  clearBtn.textContent = 'Clear';
}

function clearConversation(){
  thread.innerHTML = openingHTML;   // put the prompts and heading back
  threadId = newThreadId();         // server forgets this conversation
  disarm();
  toBottom();
  input.focus();
}

clearBtn.addEventListener('click', () => {
  if(busy) return;
  if(!armed){
    armed = true;
    clearBtn.classList.add('armed');
    clearBtn.textContent = 'Click again';
    armTimer = setTimeout(disarm, 3500);   // don't leave it armed indefinitely
    return;
  }
  clearConversation();
});

/* ---------- wiring ---------- */
send.addEventListener('click', () => askQuestion(input.value));

input.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); askQuestion(input.value); }
});
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 120) + 'px';
});

document.querySelectorAll('.seg').forEach(btn => {
  btn.addEventListener('click', () => {
    if(btn.dataset.type === ipoType) return;
    ipoType = btn.dataset.type;
    document.querySelectorAll('.seg').forEach(b =>
      b.setAttribute('aria-pressed', String(b.dataset.type === ipoType)));
    loadBoard();
  });
});

document.addEventListener('click', e => {
  const t = e.target.closest('[data-q]');
  if(t) askQuestion(t.dataset.q);
});

loadBoard();
setInterval(loadBoard, 300000); // refresh the rail every 5 min
input.focus();