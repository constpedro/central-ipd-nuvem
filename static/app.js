// Utilidades comuns da Central IPD.
function abrirPasta(qual) {
  // De outro PC o Explorer abriria na tela do servidor; então a pasta abre
  // no navegador (aberta já no clique, para o bloqueador de pop-up não barrar).
  if (!window.ACESSO_LOCAL) { window.open('/pasta/' + qual, '_blank'); return; }
  fetch('/abrir-pasta/' + qual).catch(() => {});
}

// (2) Números do topo contam de 0 até o valor ao carregar a página.
document.addEventListener('DOMContentLoaded', () => {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  document.querySelectorAll('.kpi strong').forEach(el => {
    const no = el.firstChild;
    if (!no || no.nodeType !== 3) return;
    const texto = no.nodeValue.trim();
    if (!/^\d{1,3}(\.\d{3})*$/.test(texto)) return;   // só números (ex.: 443, 2.001)
    const alvo = parseInt(texto.replace(/\./g, ''), 10);
    if (!alvo) return;
    const fmt = n => n.toLocaleString('pt-BR');
    const dur = 1600, t0 = performance.now();
    no.nodeValue = '0' + no.nodeValue.slice(no.nodeValue.indexOf(texto) + texto.length);
    const resto = no.nodeValue.slice(1);
    (function passo(t) {
      const p = Math.min((t - t0) / dur, 1), e = 1 - Math.pow(1 - p, 3);
      no.nodeValue = fmt(Math.round(alvo * e)) + resto;
      if (p < 1) requestAnimationFrame(passo);
    })(t0);
  });
});


// Botão mostra um girinho ao enviar o formulário (e evita clique duplo).
document.addEventListener('submit', e => {
  const form = e.target;
  if (e.defaultPrevented) return;                 // ex.: confirm() cancelado
  if (form.dataset.enviando) { e.preventDefault(); return; }
  form.dataset.enviando = '1';
  const btn = e.submitter || form.querySelector('button[type=submit], button:not([type])');
  if (btn && btn.classList.contains('btn')) btn.classList.add('carregando');
  // Se a resposta for um download (a página não muda), libera o botão depois de um tempo.
  setTimeout(() => { delete form.dataset.enviando; btn && btn.classList.remove('carregando'); }, 8000);
});
// Ao voltar pelo histórico, a página pode vir do cache com o botão girando.
window.addEventListener('pageshow', () => {
  document.querySelectorAll('.btn.carregando').forEach(b => b.classList.remove('carregando'));
  document.querySelectorAll('form[data-enviando]').forEach(f => delete f.dataset.enviando);
});

// Campo de arquivo: mostra o nome escolhido ao lado do botão (o texto padrão do navegador fica oculto no CSS)
document.addEventListener('change', e => {
  const inp = e.target;
  if (!(inp instanceof HTMLInputElement) || inp.type !== 'file') return;
  let rot = inp.nextElementSibling;
  if (!rot || !rot.classList.contains('arquivo-escolhido')) {
    rot = document.createElement('span');
    rot.className = 'arquivo-escolhido';
    inp.after(rot);
  }
  rot.textContent = inp.files.length ? [...inp.files].map(f => f.name).join(', ') : '';
});

// Menu lateral recolhível: guarda a escolha por PC e mostra o nome ao passar o mouse
(function () {
  const btn = document.getElementById('btn-recolher');
  if (!btn) return;
  document.querySelectorAll('.side a.n').forEach(a => {
    const r = a.querySelector('.rot');
    if (r && !a.title) a.title = r.textContent.trim();
  });
  const rot = btn.querySelector('.rot');
  const sync = () => {
    const mini = document.documentElement.classList.contains('menu-mini');
    btn.title = rot.textContent = mini ? 'Expandir menu' : 'Recolher menu';
  };
  sync();
  btn.addEventListener('click', () => {
    const mini = document.documentElement.classList.toggle('menu-mini');
    try { localStorage.setItem('menuMini', mini ? '1' : '0'); } catch (e) {}
    sync();
  });
})();

// Tela inicial: escolher quais áreas aparecem (salvo por PC; o menu lateral continua com todas)
(function () {
  const btn = document.getElementById('btn-personalizar');
  const lista = document.querySelector('.areas');
  if (!btn || !lista) return;
  const cards = [...lista.querySelectorAll('.area[data-area]')];
  const rot = btn.querySelector('span');
  const salvar = () => {
    const ocultas = cards.filter(c => c.classList.contains('oculta')).map(c => c.dataset.area);
    try { localStorage.setItem('areasOcultas', JSON.stringify(ocultas)); } catch (e) {}
  };
  const atualizar = () => {
    const editando = document.body.classList.contains('personalizando');
    const visiveis = cards.filter(c => !c.classList.contains('oculta'));
    cards.forEach(c => c.classList.toggle('ult', c === visiveis[visiveis.length - 1]));
    lista.classList.toggle('vazia', !editando && visiveis.length === 0);
    rot.textContent = editando ? 'Concluir' : 'Personalizar';
  };
  btn.addEventListener('click', () => {
    document.body.classList.toggle('personalizando');
    salvar();
    atualizar();
  });
  lista.addEventListener('click', e => {
    if (!document.body.classList.contains('personalizando')) return;
    const card = e.target.closest('.area[data-area]');
    if (!card) return;
    e.preventDefault();
    card.classList.toggle('oculta');
    salvar();
    atualizar();
  });
  atualizar();
})();

// ---- Tabelas: ordenar clicando no título, filtro rápido e contador ----
(function () {
  const chave = txt => {
    const t = (txt || '').trim();
    if (!t || t === '—' || t === '-') return null;
    let m = t.match(/^(\d{2})\/(\d{2})\/(\d{4})(?:\D+(\d{2}):(\d{2}))?/);
    if (m) return +(m[3] + m[2] + m[1] + (m[4] || '00') + (m[5] || '00'));
    m = t.match(/^(\d+)\/(\d+)$/);                       // progresso 3/10
    if (m) return +m[2] ? m[1] / m[2] : 0;
    const n = t.replace(/R\$|%|\s/g, '').replace(/\./g, '').replace(',', '.');
    if (/^[-+]?\d+(\.\d+)?$/.test(n)) return parseFloat(n);
    return t.toLocaleLowerCase('pt-BR');
  };
  const comparar = (a, b) => {
    if (a === null) return b === null ? 0 : 1;           // vazios sempre no fim
    if (b === null) return -1;
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    return String(a).localeCompare(String(b), 'pt-BR', { numeric: true });
  };
  const cabecalho = t => (t.tHead && t.tHead.rows[0]) || [...t.rows].find(r => r.querySelector('th'));
  const linhasDados = (t, cab) => [...t.rows].filter(r => r !== cab && !r.classList.contains('total') && r.querySelector('td') && !(t.tHead && t.tHead.contains(r)));

  function contar(t) {
    const info = t._contador; if (!info) return;
    const todas = linhasDados(t, cabecalho(t));
    const vis = todas.filter(r => r.style.display !== 'none').length;
    info.textContent = vis === todas.length ? todas.length + (todas.length === 1 ? ' linha' : ' linhas') : vis + ' de ' + todas.length;
  }

  document.querySelectorAll('table.dados:not(.itens):not([data-sem-ordem])').forEach(t => {
    const cab = cabecalho(t); if (!cab) return;
    [...cab.cells].forEach((th, i) => {
      if (!th.textContent.trim()) return;
      th.classList.add('ordenavel'); th.title = 'Clique para ordenar';
      th.addEventListener('click', () => {
        const asc = th.dataset.ordem !== 'asc';
        [...cab.cells].forEach(c => delete c.dataset.ordem);
        th.dataset.ordem = asc ? 'asc' : 'desc';
        const linhas = linhasDados(t, cab);
        if (!linhas.length) return;
        const pai = linhas[0].parentNode, totais = [...pai.querySelectorAll(':scope > tr.total')];
        linhas.map(r => [r, chave(r.cells[i] && r.cells[i].textContent)])
          .sort((x, y) => (x[1] === null || y[1] === null) ? comparar(x[1], y[1]) : (asc ? 1 : -1) * comparar(x[1], y[1]))
          .forEach(([r]) => pai.appendChild(r));
        totais.forEach(r => pai.appendChild(r));
      });
    });

    if (!t.hasAttribute('data-filtro')) return;
    const barra = document.createElement('div');
    barra.className = 'filtro-tabela';
    barra.innerHTML = '<label class="filtro-campo"><svg class="ic ic-s" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.5-3.5"></path></svg><input type="search" placeholder="Filtrar nesta tabela…" autocomplete="off"></label><span class="filtro-cont"></span>';
    const box = t.closest('.rolagem') || t;
    box.parentNode.insertBefore(barra, box);
    const inp = barra.querySelector('input');
    t._contador = barra.querySelector('span');
    inp.addEventListener('input', () => {
      const q = inp.value.trim().toLocaleLowerCase('pt-BR');
      linhasDados(t, cab).forEach(r => { r.style.display = !q || r.textContent.toLocaleLowerCase('pt-BR').includes(q) ? '' : 'none'; });
      contar(t);
    });
    contar(t);
  });

  // ---- Grupos de caixas: marcar todos / limpar + contador ----
  document.querySelectorAll('[data-selecao]').forEach(g => {
    const caixas = [...g.querySelectorAll('input[type=checkbox]')];
    if (caixas.length < 2) return;
    const barra = document.createElement('div');
    barra.className = 'selecao-barra';
    barra.innerHTML = '<button type="button" class="link">Marcar todos</button> · <button type="button" class="link">Limpar seleção</button> <span class="dim"></span>';
    const [bTodos, bLimpar] = barra.querySelectorAll('button'), info = barra.querySelector('span');
    const atualizar = () => { const n = caixas.filter(c => c.checked).length; info.textContent = '(' + n + ' de ' + caixas.length + ')'; };
    const definir = v => { caixas.forEach(c => { if (c.checked !== v) { c.checked = v; c.dispatchEvent(new Event('change', { bubbles: true })); } }); atualizar(); };
    bTodos.onclick = () => definir(true); bLimpar.onclick = () => definir(false);
    caixas.forEach(c => c.addEventListener('change', atualizar));
    g.appendChild(barra); atualizar();
  });

  // ---- Atalhos: "/" vai para a busca, Esc limpa ----
  const buscas = () => [...document.querySelectorAll('.busca-acoes input[type=text], .busca-caixa input, #busca, .filtro-tabela input')].filter(e => e.offsetParent);
  document.addEventListener('keydown', e => {
    const alvo = e.target, digitando = /^(INPUT|TEXTAREA|SELECT)$/.test(alvo.tagName) || alvo.isContentEditable;
    if (e.key === '/' && !digitando && !e.ctrlKey && !e.altKey) {
      const b = buscas()[0]; if (b) { e.preventDefault(); b.focus(); b.select(); }
    } else if (e.key === 'Escape' && buscas().includes(alvo) && alvo.value) {
      alvo.value = ''; alvo.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
})();

// ---- Campos que lembram o último valor usado (ex.: competência) ----
// A memória vale só enquanto o valor-padrão da página for o mesmo de quando foi
// gravada: a competência padrão anda sozinha todo mês e, sem isso, um "202609"
// lembrado em outubro continuaria aparecendo (e sendo regravado) para sempre.
(function () {
  document.querySelectorAll('input[data-lembrar]').forEach(inp => {
    const chave = 'lembrar:' + inp.dataset.lembrar;
    const padrao = inp.defaultValue;
    try {
      let salvo = null;
      try { salvo = JSON.parse(localStorage.getItem(chave)); } catch (e) {}  // formato antigo (texto) é descartado
      if (salvo && salvo.padrao === padrao && salvo.valor && salvo.valor !== inp.value) {
        inp.value = salvo.valor;
        const dica = document.createElement('span');
        dica.className = 'dica'; dica.textContent = 'Última usada — altere se precisar.';
        inp.insertAdjacentElement('afterend', dica);
      } else if (!salvo || salvo.padrao !== padrao) {
        localStorage.removeItem(chave);
      }
    } catch (e) {}
    const salvar = () => {
      try {
        const v = inp.value.trim();
        if (v === padrao) localStorage.removeItem(chave);  // usar o padrão não precisa ser lembrado
        else if (v && inp.checkValidity()) localStorage.setItem(chave, JSON.stringify({valor: v, padrao: padrao}));
      } catch (e) {}
    };
    if (inp.form) inp.form.addEventListener('submit', salvar);
    inp.addEventListener('change', salvar);
  });
})();

// Celular: ao encostar o dedo num link, a página já começa a ser buscada — quando o
// toque termina, ela abre quase na hora.
(function () {
  const feitos = new Set();
  document.addEventListener('touchstart', e => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.target === '_blank' || a.hasAttribute('download') || a.origin !== location.origin) return;
    if (/\/(sair|abrir-pasta)(\/|$)/.test(a.pathname)) return;  // links que fazem algo: nunca buscar antes
    const url = a.href.split('#')[0];
    if (feitos.has(url) || url === location.href.split('#')[0]) return;
    feitos.add(url);
    const l = document.createElement('link');
    l.rel = 'prefetch'; l.href = url;
    document.head.appendChild(l);
  }, {passive: true});
})();
