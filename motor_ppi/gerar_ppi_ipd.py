#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera os relatórios PPI "puro" (sem SISCAN) dos municípios que você indicar, nos
subgrupos 0203 (citologia/anatomia patológica) e 0202 (diagnóstico em laboratório
clínico) — dois PDFs por cidade.

As cidades vêm do comando ou de um arquivo em listas/ (--lista). Não há lista fixa
embutida no script.

Uso:
    pip install -r requirements.txt
    python gerar_ppi_ipd.py "Patos de Minas" Uberlandia
    python gerar_ppi_ipd.py --lista cisalp

Saída: pasta ./saida/ com os 110 PDFs, nomeados
    ppi_<cidade_slug>_0203_<ano>.pdf
    ppi_<cidade_slug>_0202_<ano>.pdf

Se alguma cidade falhar (erro de rede, timeout, etc.), o script continua com as
demais e imprime um resumo no final com o que faltou. Toda rodada gera os PDFs
de novo, substituindo os que já estiverem na pasta.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

NEWLINE = chr(10)

# Em http. No https o endpoint de autocomplete responde 302 para a página inicial
# (testado em 15/09/2026), o que quebra a busca do município. Se o site mudar,
# aponte outro endereço na variável de ambiente PPI_BASE_URL.
BASE = os.environ.get("PPI_BASE_URL", "http://ppiassistencial.saude.mg.gov.br").rstrip("/")
URL_CONSULTA = f"{BASE}/consulta-ppi"
URL_AUTOCOMPLETE = f"{BASE}/consulta-ppi/autocomplete"

def _competencia_atual():
    """Mês anterior ao de hoje: o PPI publica a competência com um mês de atraso
    (em 06/10/2026 a mais recente no site era a 2026/09). Antes era um número
    fixo no código e ficava para trás a cada mês."""
    hoje = time.localtime()
    ano, mes = (hoje.tm_year, hoje.tm_mon - 1) if hoje.tm_mon > 1 else (hoje.tm_year - 1, 12)
    return f"{ano}{mes:02d}"


COMPETENCIA = _competencia_atual()
COMPETENCIA_FMT = COMPETENCIA[:4] + "/" + COMPETENCIA[4:]

SUBGRUPOS = {
    "0203": "0203 · Diagnóstico por anatomia patológica e citopatologia",
    "0202": "0202 · Diagnóstico em laboratório clínico",
}
# O subgrupo 0202 (laboratório clínico) tem centenas de procedimentos por município,
# o que deixa o relatório enorme e pouco útil. Para ele, agregamos as linhas por
# município de atendimento — exatamente como o próprio site faz na opção
# "Discriminar por: Município Atendimento". Continuamos consultando por
# procedimento (discriminar-por=2) porque só esse modo devolve as QUANTIDADES;
# o modo do site traz apenas os valores.
AGRUPAR_POR_MUNICIPIO = {"0202"}

SUBGRUPO_LABEL = {
    "0203": "Programação PPI — Citologia/Anatomia Patológica",
    "0202": "Programação PPI — Laboratório Clínico",
}

# Os 7 procedimentos do 0203 que vão no relatório SISCAN (sempre juntos).
# Com --sem-siscan o PDF do 0203 sai só com o resto (anatomia patológica).
CODIGOS_SISCAN = {"019", "022", "043", "065", "073", "081", "086"}
SEM_SISCAN = False

# Linha "Total em <polo> (÷ 12)", como no relatório SISCAN + PPI.
MUNICIPIO_REFERENCIA_NOME = "Varginha"
MUNICIPIO_REFERENCIA = "varginha"  # sem acento, para comparar

# Nao existe lista de cidades embutida: as cidades vem do comando ou de um arquivo
# em listas/ (uma cidade por linha; linhas com # sao comentario).

SCRIPT_DIR = Path(__file__).parent
LISTAS_DIR = SCRIPT_DIR / "listas"
TEMPLATE_PATH = SCRIPT_DIR / "assets" / "template_ppi_puro.html"
LOGO_PATH = SCRIPT_DIR / "assets" / "logo_ipd.png"
OUT_DIR = SCRIPT_DIR / "saida"
LOG_PATH = SCRIPT_DIR / "log_erros.txt"


def slugify(nome: str) -> str:
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def sem_acento(nome: str) -> str:
    """Forma segura para o autocomplete antigo do PPI."""
    return unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode("ascii")


def _localizar_cache_ppi():
    """Localiza o cache opcional da Central sem quebrar o uso standalone."""
    informado = os.environ.get("CENTRAL_IPD_PPI_CACHE_DIR")
    candidatos = [Path(informado)] if informado else []
    candidatos.append(Path.home() / "Downloads" / "central-ipd" / "data" / "cache" / "ppi")
    for candidato in candidatos:
        if candidato.is_dir():
            return candidato
    return None


PPI_CACHE_DIR = _localizar_cache_ppi()


def _ler_cache_ppi(competencia, cidade):
    if PPI_CACHE_DIR is None:
        return None
    caminho = PPI_CACHE_DIR / competencia / (slugify(cidade) + ".json")
    if not caminho.is_file():
        return None
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    subgrupo = dados.get("subgrupos", {}).get("0202")
    if not isinstance(subgrupo, dict) or "procedimentos" not in subgrupo:
        return None
    return dados


def carregar_lista(nome):
    """Le as cidades de um arquivo de listas/ (ou de um caminho completo).

    Aceita "cisalp", "cisalp.txt" ou um caminho qualquer. Linhas em branco e
    linhas comecadas por # sao ignoradas.
    """
    candidatos = [Path(nome), LISTAS_DIR / nome, LISTAS_DIR / (nome + ".txt")]
    for caminho in candidatos:
        if caminho.is_file():
            cidades = []
            for linha in caminho.read_text(encoding="utf-8").splitlines():
                linha = linha.strip()
                if linha and not linha.startswith("#"):
                    cidades.append(linha)
            if not cidades:
                raise SystemExit("a lista " + str(caminho) + " esta vazia")
            return cidades
    disponiveis = sorted(x.stem for x in LISTAS_DIR.glob("*.txt")) if LISTAS_DIR.is_dir() else []
    recado = "lista nao encontrada: " + nome
    if disponiveis:
        recado += "  (disponiveis: " + ", ".join(disponiveis) + ")"
    raise SystemExit(recado)


def build_logo_data_uri() -> str:
    import base64
    data = LOGO_PATH.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def fmt_int(n):
    return f"{n:,.0f}".replace(",", ".")


def fmt_qtd_media(n):
    s = f"{n:,.2f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def fmt_money(n):
    s = f"{n:,.2f}"
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return f"R$ {s}"


# --- Geração de PDF via Chrome/Edge headless -------------------------------
# O WeasyPrint precisa das bibliotecas nativas do GTK (libgobject, pango, cairo),
# que não vêm instaladas no Windows. Como o Chrome (ou o Edge, presente em todo
# Windows 11) já renderiza o mesmo HTML/CSS e sabe imprimir em PDF, usamos ele:
# zero dependências extras para instalar.
CANDIDATOS_NAVEGADOR = [
    os.environ.get("PPI_CHROME"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def localizar_navegador() -> str:
    for cand in CANDIDATOS_NAVEGADOR:
        if cand and Path(cand).exists():
            return cand
    for nome in ("chrome", "msedge", "chromium"):
        achado = shutil.which(nome)
        if achado:
            return achado
    raise RuntimeError(
        "Não encontrei o Chrome nem o Edge para gerar os PDFs. "
        "Instale um deles ou aponte o caminho do .exe na variável de ambiente PPI_CHROME."
    )


class _NavegadorPersistente:
    """Um único Chrome/Edge headless aberto durante todo o lote.

    Abrir o navegador custa ~15 s nesta máquina; imprimir numa aba de um
    navegador já aberto custa menos de 1 s. Conversa com ele pelo protocolo
    DevTools (precisa do pacote websocket-client). Se algo falhar, quem chama
    cai no método antigo (um navegador por PDF).
    """

    def __init__(self, navegador):
        import threading
        import websocket  # noqa: F401 — falha aqui cai no modo lento
        self._lock = threading.Lock()
        self._perfil = tempfile.mkdtemp(prefix="ppi_chrome_")
        self.proc = subprocess.Popen(
            [navegador, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--remote-debugging-port=0",
             f"--user-data-dir={self._perfil}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        arq = Path(self._perfil) / "DevToolsActivePort"
        limite = time.time() + 90
        while time.time() < limite:
            if arq.exists():
                linhas = arq.read_text().split()
                if len(linhas) >= 2:
                    break
            time.sleep(0.2)
        else:
            self.fechar()
            raise RuntimeError("navegador não abriu a porta de depuração")
        self._url_ws = f"ws://127.0.0.1:{linhas[0]}{linhas[1]}"
        # Uma conexão por thread: cada cidade do lote imprime na sua própria aba,
        # em paralelo, em vez de esperar a fila de um canal único.
        self._local = threading.local()
        self._conexoes = []
        self._ws()

    def _ws(self):
        import websocket  # websocket-client
        ws = getattr(self._local, "ws", None)
        if ws is None:
            ws = websocket.create_connection(self._url_ws, timeout=120, suppress_origin=True)
            self._local.ws = ws
            self._local.id = 0
            with self._lock:
                self._conexoes.append(ws)
        return ws

    def _cmd(self, metodo, params=None, sessao=None, esperar_evento=None):
        ws = self._ws()
        self._local.id += 1
        id_msg = self._local.id
        msg = {"id": id_msg, "method": metodo, "params": params or {}}
        if sessao:
            msg["sessionId"] = sessao
        ws.send(json.dumps(msg))
        resultado = None
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == id_msg:
                if "error" in r:
                    raise RuntimeError(f"{metodo}: {r['error'].get('message')}")
                resultado = r.get("result", {})
                if not esperar_evento:
                    return resultado
            elif esperar_evento and r.get("method") == esperar_evento and r.get("sessionId") == sessao:
                esperar_evento = None
                if resultado is not None:
                    return resultado

    def imprimir(self, html_path: Path, pdf_path: Path):
        import base64
        alvo = self._cmd("Target.createTarget", {"url": "about:blank"})["targetId"]
        try:
            sessao = self._cmd("Target.attachToTarget", {"targetId": alvo, "flatten": True})["sessionId"]
            self._cmd("Page.enable", sessao=sessao)
            self._cmd("Page.navigate", {"url": html_path.resolve().as_uri()},
                      sessao=sessao, esperar_evento="Page.loadEventFired")
            pdf = self._cmd("Page.printToPDF", {"printBackground": True, "preferCSSPageSize": True},
                            sessao=sessao)
            pdf_path.write_bytes(base64.b64decode(pdf["data"]))
        finally:
            try:
                self._cmd("Target.closeTarget", {"targetId": alvo})
            except Exception:
                pass

    def fechar(self):
        for ws in getattr(self, "_conexoes", []):
            try:
                ws.close()
            except Exception:
                pass
        try:
            self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:
            pass
        shutil.rmtree(self._perfil, ignore_errors=True)


_navegador_aberto = None
_navegador_falhou = False


import threading as _threading
_navegador_lock = _threading.Lock()


def _obter_navegador_aberto(navegador):
    with _navegador_lock:
        return _obter_navegador_aberto_sem_lock(navegador)


def _obter_navegador_aberto_sem_lock(navegador):
    global _navegador_aberto, _navegador_falhou
    if _navegador_aberto is None and not _navegador_falhou:
        try:
            _navegador_aberto = _NavegadorPersistente(navegador)
        except Exception as e:
            _navegador_falhou = True
            print(f"(navegador único indisponível, usando o modo lento: {e})")
    return _navegador_aberto


def fechar_navegador_aberto():
    global _navegador_aberto
    if _navegador_aberto is not None:
        _navegador_aberto.fechar()
        _navegador_aberto = None


def aquecer_navegador(navegador, html_exemplo=None):
    """Abre (e aquece) o navegador numa thread, enquanto o lote consulta os sites.

    Abrir o Chrome e renderizar a primeira página (fontes embutidas) custa uns
    10–15 s nesta máquina; feito em paralelo com as consultas, isso some do tempo
    total — principalmente em lotes pequenos, de uma ou duas cidades.
    """
    def _aquecer():
        aberto = _obter_navegador_aberto(navegador)
        if aberto is None or html_exemplo is None:
            return
        destino = Path(tempfile.gettempdir()) / f"ppi_aquecimento_{os.getpid()}.pdf"
        try:
            aberto.imprimir(Path(html_exemplo), destino)
        except Exception:
            pass
        finally:
            destino.unlink(missing_ok=True)
    _threading.Thread(target=_aquecer, daemon=True).start()


import atexit as _atexit
_atexit.register(fechar_navegador_aberto)


def html_para_pdf(navegador: str, html_path: Path, pdf_path: Path):
    """Converte um arquivo HTML local em PDF, de preferência no navegador já aberto."""
    aberto = _obter_navegador_aberto(navegador)
    if aberto is not None:
        try:
            aberto.imprimir(html_path, pdf_path)
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                return
        except Exception as e:
            print(f"(falha no navegador único, tentando o modo lento: {e})")
    _html_para_pdf_avulso(navegador, html_path, pdf_path)


def _html_para_pdf_avulso(navegador: str, html_path: Path, pdf_path: Path):
    """Converte um arquivo HTML local em PDF usando o navegador em modo headless."""
    # Perfil descartável: evita conflito com uma janela do Chrome já aberta pelo usuário.
    with tempfile.TemporaryDirectory(prefix="ppi_chrome_") as perfil:
        cmd = [
            navegador,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            f"--user-data-dir={perfil}",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path.resolve()}",
            html_path.resolve().as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        detalhe = (proc.stderr or proc.stdout or "").strip().splitlines()
        detalhe = detalhe[-1] if detalhe else f"código de saída {proc.returncode}"
        raise RuntimeError(f"navegador não gerou o PDF: {detalhe}")


def garantir_sessao(session: requests.Session):
    """Abre a página de consulta uma vez para receber os cookies de sessão
    (laravel_session e ROUTEID, que prende a sessão a um dos servidores).

    Hoje o autocomplete responde mesmo sem sessão; a visita prévia é só uma
    garantia barata para o dia em que o site passar a exigir. Quando ele devolve
    HTML em vez de JSON, o sintoma é "Expecting value: line 1 column 1".
    """
    if "laravel_session" not in session.cookies:
        session.get(URL_CONSULTA, timeout=20).raise_for_status()


def buscar_origem_id(session: requests.Session, nome_cidade: str):
    """Consulta o endpoint de autocomplete e retorna (nome_formatado, code)."""
    garantir_sessao(session)
    # O autocomplete antigo falha silenciosamente com alguns caracteres UTF-8.
    # A busca sem acento encontra o mesmo município e o nome original continua
    # sendo usado nos títulos dos relatórios.
    payload = {"dataType": "json", "origem": sem_acento(nome_cidade), "tipo_origem": "1"}
    resp = session.post(URL_AUTOCOMPLETE, data=payload, timeout=20)
    resp.raise_for_status()
    if "json" not in resp.headers.get("content-type", ""):
        # sessão expirada ou trocada de servidor: renova e tenta mais uma vez
        session.cookies.clear()
        garantir_sessao(session)
        resp = session.post(URL_AUTOCOMPLETE, data=payload, timeout=20)
        resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(
            "o site do PPI não respondeu o autocomplete em JSON (pode estar fora do ar ou ter mudado)")
    if not data:
        raise ValueError(f"Município não encontrado no autocomplete: {nome_cidade}")
    # Prioriza correspondência exata do nome (ignorando acento/caixa) se houver mais de uma opção
    alvo = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", nome_cidade).encode("ascii", "ignore").decode("ascii").lower())
    melhor = data[0]
    for opt in data:
        nome_opt = opt["name"].split(" - ", 1)[-1]
        cand = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", nome_opt).encode("ascii", "ignore").decode("ascii").lower())
        if cand == alvo:
            melhor = opt
            break
    return melhor["name"], melhor["code"]


def obter_csrf_token(session: requests.Session) -> str:
    """Faz GET na página de consulta e extrai o _token CSRF (Laravel) do formulário."""
    resp = session.get(URL_CONSULTA, timeout=20)
    resp.raise_for_status()
    # A ordem dos atributos do <input> muda entre versões do site
    # (name/value, name/type/value...); a <meta csrf-token> é o plano B.
    m = (re.search(r'<input[^>]*name="_token"[^>]*value="([^"]+)"', resp.text)
         or re.search(r'<input[^>]*value="([^"]+)"[^>]*name="_token"', resp.text)
         or re.search(r'<meta[^>]*name="csrf-token"[^>]*content="([^"]+)"', resp.text)
         or re.search(r'<meta[^>]*content="([^"]+)"[^>]*name="csrf-token"', resp.text))
    if not m:
        raise RuntimeError("Não foi possível extrair o _token CSRF da página de consulta")
    return m.group(1)


def consultar_subgrupo(session: requests.Session, csrf_token: str, origem_nome: str, origem_id: int, subgrupo: str):
    """Submete o formulário de consulta do PPI e retorna o HTML de resultado."""
    payload = {
        "_token": csrf_token,
        "_method": "POST",
        "foco-pesquisa": "1",
        "tipo-origem": "1",
        "discriminar-por": "2",
        "grupo": "02",
        "subgrupo": subgrupo,
        "complexidade": "",
        "sistema-informacao": "",
        "forma-organizacao": "",
        "origem-recurso": "",
        "procedimento": "",
        "gestao-recurso": "",
        "competencia": COMPETENCIA,
        "origem": origem_nome,
        "origem-id": str(origem_id),
        "atendimento": "",
        "atendimento-id": "",
    }
    resp = session.post(URL_CONSULTA, data=payload, timeout=60)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def parse_resultado(html: str):
    """Extrai as linhas de procedimento da tabela de resultado."""
    soup = BeautifulSoup(html, "html.parser")
    linhas = []
    # A página de resultado tem VÁRIAS tabelas (cabeçalho do governo, resumo dos
    # filtros e, por último, a de dados). Por isso varremos todas as <tr> do
    # documento e ficamos só com as que começam com "<código> - <nome>".
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        primeiro = tds[0].get_text(strip=True)
        if primeiro.lower() == "total":
            continue
        m = re.match(r"^(\d+)\s*-\s*(.+)$", primeiro)
        if not m:
            continue
        codigo = m.group(1)
        nome = m.group(2).strip()
        municipio_atendimento = tds[1].get_text(strip=True)
        qtd_raw = tds[2].get_text(strip=True).replace(".", "").replace(",", ".")
        val_raw = tds[3].get_text(strip=True).replace("R$", "").strip().replace(".", "").replace(",", ".")
        try:
            qtd = float(qtd_raw) if qtd_raw else 0.0
        except ValueError:
            qtd = 0.0
        try:
            val = float(val_raw) if val_raw else 0.0
        except ValueError:
            val = 0.0
        linhas.append({
            "codigo": codigo,
            "nome": nome,
            "municipio_atendimento": municipio_atendimento,
            "qtd_anual": qtd,
            "valor_anual": val,
        })
    return linhas


def agrupar_por_municipio(procedimentos):
    """Soma as linhas por município de atendimento, agrupando pelo código IBGE.

    O agrupamento é pelo CÓDIGO e não pelo nome porque o mesmo município pode
    aparecer com rótulos diferentes (ex.: "317010 - Uberaba" e
    "317010 - Uberaba (FUNED)"), e o site soma os dois numa linha só.
    """
    agregado = {}
    ordem = []
    for p in procedimentos:
        rotulo = p["municipio_atendimento"]
        codigo = rotulo.split(" - ", 1)[0].strip()
        nome = rotulo.split(" - ", 1)[-1].strip()
        nome_base = nome.split(" (")[0].strip()  # remove sufixos como "(FUNED)"
        if codigo not in agregado:
            agregado[codigo] = {
                "municipio": codigo + " - " + nome_base,
                "qtd_anual": 0.0,
                "valor_anual": 0.0,
                "n_procedimentos": 0,
            }
            ordem.append(codigo)
        linha = agregado[codigo]
        linha["qtd_anual"] += p["qtd_anual"]
        linha["valor_anual"] += p["valor_anual"]
        linha["n_procedimentos"] += 1
    linhas = [agregado[c] for c in ordem]
    linhas.sort(key=lambda x: x["valor_anual"], reverse=True)
    return linhas


def build_ppi_rows(procedimentos):
    """Linhas detalhadas: uma por procedimento/municipio (subgrupo 0203)."""
    rows_html = []
    for p in procedimentos:
        rows_html.append(
            "<tr>"
            f"<td><b class=\"num\">{p['codigo']}</b> — {p['nome']}</td>"
            f"<td><span class=\"muni-tag\">{p['municipio_atendimento']}</span></td>"
            f"<td>{fmt_int(p['qtd_anual'])}</td>"
            f"<td>{fmt_money(p['valor_anual'])}</td>"
            f"<td>{fmt_qtd_media(p['qtd_anual'] / 12)}</td>"
            f"<td>{fmt_money(p['valor_anual'] / 12)}</td>"
            "</tr>"
        )
    return (NEWLINE + "        ").join(rows_html)


def build_ppi_rows_municipio(linhas):
    """Linhas agregadas: uma por municipio de atendimento (subgrupo 0202)."""
    rows_html = []
    for m in linhas:
        rows_html.append(
            "<tr>"
            f"<td><span class=\"muni-tag\">{m['municipio']}</span></td>"
            f"<td>{fmt_int(m['qtd_anual'])}</td>"
            f"<td>{fmt_money(m['valor_anual'])}</td>"
            f"<td>{fmt_qtd_media(m['qtd_anual'] / 12)}</td>"
            f"<td>{fmt_money(m['valor_anual'] / 12)}</td>"
            "</tr>"
        )
    return (NEWLINE + "        ").join(rows_html)


def gerar_pdf(municipio_nome, municipio_codigo, subgrupo, procedimentos, pdf_path: Path, logo_data_uri: str, template: str, navegador: str):
    total_qtd_anual = sum(p["qtd_anual"] for p in procedimentos)
    total_valor_anual = sum(p["valor_anual"] for p in procedimentos)
    meta_valor_mensal = total_valor_anual / 12
    n_procedimentos = len(procedimentos)

    em_ref = [p for p in procedimentos
              if MUNICIPIO_REFERENCIA.lower() in sem_acento(p.get("municipio_atendimento", "")).lower()]
    ref_qtd = sum(p["qtd_anual"] for p in em_ref)
    ref_valor = sum(p["valor_anual"] for p in em_ref)

    def _linha_total(classe, rotulo, qtd, valor, coluna_extra):
        return ("<tr class=\"" + classe + "\"><td>" + rotulo + "</td>"
                + ("<td>" + coluna_extra + "</td>" if coluna_extra is not None else "")
                + "<td>" + fmt_int(qtd) + "</td><td>" + fmt_money(valor) + "</td>"
                + "<td>" + fmt_qtd_media(qtd / 12) + "</td><td>" + fmt_money(valor / 12) + "</td></tr>")

    colunas_mensais = '<th>Qtd. mensal</th><th>Valor mensal</th></tr>'
    if subgrupo in AGRUPAR_POR_MUNICIPIO:
        linhas_municipio = agrupar_por_municipio(procedimentos)
        ppi_rows = build_ppi_rows_municipio(linhas_municipio)
        ppi_thead = (
            '<tr><th style="text-align:left">Município atendimento</th>'
            '<th>Qtd. anual</th><th>Valor anual</th>' + colunas_mensais
        )
        extra_ref, extra_geral = None, None
        stat_sub = str(len(linhas_municipio)) + " municípios · " + str(n_procedimentos) + " procedimentos"
        subtitulo = "Programação anual pactuada por município de atendimento — Grupo 02, Complexidade Média, Ambulatorial"
    else:
        ppi_rows = build_ppi_rows(procedimentos)
        ppi_thead = (
            '<tr><th style="text-align:left">Procedimento</th>'
            '<th style="text-align:left">Município atendimento</th>'
            '<th>Qtd. anual</th><th>Valor anual</th>' + colunas_mensais
        )
        extra_ref, extra_geral = MUNICIPIO_REFERENCIA_NOME, "—"
        stat_sub = str(n_procedimentos) + " procedimentos"
        subtitulo = "Programação anual pactuada por procedimento — Grupo 02, Complexidade Média, Ambulatorial"
    ppi_total_row = (
        _linha_total("varginha-row", "Total em " + MUNICIPIO_REFERENCIA_NOME + " (÷ 12)",
                     ref_qtd, ref_valor, extra_ref)
        + NEWLINE + "        "
        + _linha_total("total-row", "Total geral", total_qtd_anual, total_valor_anual, extra_geral)
    )

    replacements = {
        "{{MUNICIPIO_NOME}}": municipio_nome,
        "{{MUNICIPIO_CODIGO}}": municipio_codigo,
        "{{COMPETENCIA_PPI}}": COMPETENCIA_FMT,
        "{{PPI_SUBGRUPO}}": SUBGRUPOS[subgrupo],
        "{{PPI_SUBGRUPO_LABEL}}": ("Programação PPI — Anatomia Patológica (sem SISCAN)"
                                   if subgrupo == "0203" and SEM_SISCAN else SUBGRUPO_LABEL[subgrupo]),
        "{{PPI_ROWS}}": ppi_rows,
        "{{PPI_THEAD}}": ppi_thead,
        "{{PPI_TOTAL_ROW}}": ppi_total_row,
        "{{PPI_STAT_SUB}}": stat_sub,
        "{{PPI_SUBTITULO}}": subtitulo,
        "{{PPI_TOTAL_QTD}}": fmt_int(total_qtd_anual),
        "{{PPI_TOTAL_VALOR}}": fmt_money(total_valor_anual),
        "{{PPI_META_VALOR}}": fmt_money(meta_valor_mensal),
        "{{LOGO_DATA_URI}}": logo_data_uri,
    }

    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, str(value))

    tmp_html = pdf_path.with_suffix(".html")
    tmp_html.write_text(html, encoding="utf-8")
    try:
        html_para_pdf(navegador, tmp_html, pdf_path)
    finally:
        tmp_html.unlink(missing_ok=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Gera os relatorios PPI em PDF dos municipios indicados.",
        epilog=(
            "Exemplos:" + NEWLINE
            + "  python gerar_ppi_ipd.py Uberlandia Araguari" + NEWLINE
            + '  python gerar_ppi_ipd.py "Patos de Minas" --competencia 202609' + NEWLINE
            + "  python gerar_ppi_ipd.py Frutal --subgrupo 0203" + NEWLINE
            + "  python gerar_ppi_ipd.py --lista cisalp"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "cidades", nargs="*",
        help="Nomes dos municipios (entre aspas se tiver espaco).")
    parser.add_argument(
        "--lista", metavar="NOME",
        help="Le as cidades de um arquivo em listas/ (ex.: --lista cisalp).")
    parser.add_argument(
        "--competencia", default=COMPETENCIA, metavar="AAAAMM",
        help="Competencia no formato AAAAMM (padrao: " + COMPETENCIA + ").")
    parser.add_argument(
        "--subgrupo", action="append", choices=sorted(SUBGRUPOS), metavar="CODIGO",
        help="Subgrupo a gerar; pode repetir. Opcoes: " + ", ".join(sorted(SUBGRUPOS))
             + ". Padrao: todos.")
    parser.add_argument(
        "--sem-siscan", action="store_true",
        help="No 0203, deixa de fora os 7 procedimentos do SISCAN (só anatomia patológica).")
    parser.add_argument(
        "--saida", type=Path, default=OUT_DIR, metavar="PASTA",
        help="Pasta onde salvar os PDFs (padrao: " + OUT_DIR.name + ").")
    args = parser.parse_args(argv)

    if not args.cidades and not args.lista:
        parser.error("informe ao menos uma cidade, ou use --lista NOME")
    if not re.fullmatch(r"\d{6}", args.competencia) or not 1 <= int(args.competencia[4:]) <= 12:
        parser.error(
            "competencia invalida: " + args.competencia + " (use AAAAMM, ex.: 202608)")
    return args


def nome_pdf(slug, subgrupo, competencia):
    if subgrupo == "0203" and SEM_SISCAN:
        return f"ppi_{slug}_anatomia_{competencia}.pdf"
    return f"ppi_{slug}_{subgrupo}_{competencia}.pdf"


def _nova_sessao_ppi():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    return session


def _processar_cidade_ppi(indice, total_cidades, cidade, subgrupos, competencia,
                          out_dir, logo_data_uri, template, navegador):
    """Gera uma cidade isoladamente para permitir lote paralelo controlado."""
    prefixo = f"{indice}/{total_cidades} {cidade}"
    slug = slugify(cidade)
    mensagens = []
    erros = []
    pendentes = []
    # Sempre gera de novo: um PDF antigo na pasta é simplesmente substituído.
    for subgrupo in subgrupos:
        pendentes.append((subgrupo, out_dir / nome_pdf(slug, subgrupo, competencia)))

    session = _nova_sessao_ppi()
    cache_ppi = _ler_cache_ppi(competencia, cidade)
    if cache_ppi is not None:
        origem_nome = cache_ppi["origem_nome"]
        municipio_codigo = cache_ppi.get("codigo_ibge") or origem_nome.split(" - ", 1)[0]
    else:
        try:
            origem_nome, origem_id = buscar_origem_id(session, cidade)
            municipio_codigo = origem_nome.split(" - ", 1)[0]
        except Exception as e:
            for subgrupo, _ in pendentes:
                erros.append((cidade, subgrupo, f"autocomplete falhou: {e}"))
            mensagens.append(f"{prefixo} — ERRO ao localizar município: {e}")
            return mensagens, erros

    origem_id = (cache_ppi or {}).get("origem_id")
    for subgrupo, pdf_path in pendentes:
        try:
            dados_cache = (cache_ppi or {}).get("subgrupos", {}).get(subgrupo)
            if isinstance(dados_cache, dict) and "procedimentos" in dados_cache:
                procedimentos = dados_cache["procedimentos"]
                mensagens.append(f"{prefixo} / {subgrupo} — PPI em cache local")
            else:
                if origem_id is None:
                    origem_nome, origem_id = buscar_origem_id(session, cidade)
                    municipio_codigo = origem_nome.split(" - ", 1)[0]
                csrf_token = obter_csrf_token(session)
                html_resultado = consultar_subgrupo(
                    session, csrf_token, origem_nome, origem_id, subgrupo)
                procedimentos = parse_resultado(html_resultado)
            if subgrupo == "0203" and SEM_SISCAN:
                procedimentos = [p for p in procedimentos
                                 if str(p["codigo"])[-3:] not in CODIGOS_SISCAN]
            if not procedimentos:
                mensagem = "nenhum procedimento retornado"
                mensagens.append(f"{prefixo} / {subgrupo} — SEM procedimentos retornados")
                erros.append((cidade, subgrupo, mensagem))
                continue
            gerar_pdf(cidade, municipio_codigo, subgrupo, procedimentos, pdf_path,
                      logo_data_uri, template, navegador)
            mensagens.append(f"{prefixo} / {subgrupo} — OK ({len(procedimentos)} procedimentos)")
        except Exception as e:
            mensagens.append(f"{prefixo} / {subgrupo} — ERRO: {e}")
            erros.append((cidade, subgrupo, str(e)))
        if dados_cache is None:
            time.sleep(0.6)  # gentileza com o servidor
    return mensagens, erros


def main(argv=None):
    args = parse_args(argv)

    # A competencia e usada dentro de consultar_subgrupo() e gerar_pdf(), que a leem
    # do modulo -- por isso atualizamos as constantes globais aqui.
    global COMPETENCIA, COMPETENCIA_FMT, SEM_SISCAN
    SEM_SISCAN = args.sem_siscan
    COMPETENCIA = args.competencia
    COMPETENCIA_FMT = args.competencia[:4] + "/" + args.competencia[4:]

    cidades = list(args.cidades)
    if args.lista:
        cidades += carregar_lista(args.lista)
    subgrupos = args.subgrupo or list(SUBGRUPOS)
    out_dir = args.saida

    # Uma execução bem-sucedida não pode herdar erros da execução anterior.
    LOG_PATH.unlink(missing_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    logo_data_uri = build_logo_data_uri()
    navegador = localizar_navegador()
    aquecer_navegador(navegador, TEMPLATE_PATH)

    print("Competencia: " + COMPETENCIA_FMT
          + "  |  cidades: " + str(len(cidades))
          + "  |  subgrupos: " + ", ".join(subgrupos))
    print("Salvando em: " + str(out_dir.resolve()))
    print("Gerando PDFs com: " + navegador)
    print()

    erros = []
    total_alvo = len(cidades) * len(subgrupos)
    paralelo = max(1, int(os.environ.get("CENTRAL_IPD_PARALELO", "4") or 4))
    max_workers = min(paralelo, len(cidades)) or 1
    print("Processando até " + str(max_workers) + " cidades em paralelo")
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            tarefas = [executor.submit(
                _processar_cidade_ppi, i, len(cidades), cidade, subgrupos,
                COMPETENCIA, out_dir, logo_data_uri, template, navegador)
                for i, cidade in enumerate(cidades, 1)]
            concluidas = 0
            for tarefa in as_completed(tarefas):
                mensagens, falhas = tarefa.result()
                for mensagem in mensagens:
                    print(mensagem)
                erros.extend(falhas)
                concluidas += 1
                print("PROGRESSO " + str(concluidas * len(subgrupos))
                      + "/" + str(total_alvo))
    finally:
        fechar_navegador_aberto()

    print()
    print("==== RESUMO ====")
    print(f"Total esperado: {total_alvo} PDFs")
    ok = total_alvo - len(erros)
    print(f"Gerados com sucesso: {ok}")
    print(f"Com erro: {len(erros)}")

    if erros:
        with LOG_PATH.open("w", encoding="utf-8") as f:
            for cidade, subgrupo, msg in erros:
                linha = f"{cidade} | {subgrupo} | {msg}"
                print("  - " + linha)
                f.write(linha + NEWLINE)
        print()
        print("Detalhes salvos em " + str(LOG_PATH))
        print("Rode o script novamente para tentar de novo as cidades com erro.")
    return 1 if erros else 0


if __name__ == "__main__":
    sys.exit(main())
