# -*- coding: utf-8 -*-
"""
Acesso ao PPI Assistencial (SES-MG) reaproveitando o gerar_ppi.

Não reimplementa nada: importa as funções do gerar_ppi_ipd.py (autocomplete,
token CSRF, consulta e parse) e acrescenta o que a Central precisa por cima:

  - cache em disco por competência/cidade (data/cache/ppi/<AAAAMM>/<slug>.json),
    para o relatório por consórcio não refazer 110 consultas a cada clique;
  - a linha "Total" oficial da página, para conferir a soma das linhas extraídas
    (é a mesma checagem do verificar_totais.py);
  - leitura/gravação das listas de cidades em gerar_ppi/listas/.
"""
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import config

if str(config.GERAR_PPI_DIR) not in sys.path:
    sys.path.insert(0, str(config.GERAR_PPI_DIR))

# O motor é opcional no arranque: se a pasta gerar_ppi não existir (ou mudar de
# lugar), a Central sobe mesmo assim e a tela inicial mostra o aviso para
# ajustar GERAR_PPI_DIR. As funções que precisam do motor chamam _motor().
try:
    import gerar_ppi_ipd as g  # noqa: E402
except ImportError:
    g = None

# Espelho do gerar_ppi_ipd.SUBGRUPOS, para os formulários funcionarem sem o motor.
_SUBGRUPOS_PADRAO = {
    "0203": "0203 · Diagnóstico por anatomia patológica e citopatologia",
    "0202": "0202 · Diagnóstico em laboratório clínico",
}
SUBGRUPOS = dict(g.SUBGRUPOS) if g is not None else dict(_SUBGRUPOS_PADRAO)
CACHE_PPI = config.CACHE_DIR / "ppi"


def motor_disponivel():
    return g is not None


def _motor():
    if g is None:
        raise FileNotFoundError(
            f"não encontrei gerar_ppi_ipd.py em {config.GERAR_PPI_DIR} — ajuste GERAR_PPI_DIR em config.py")
    return g


def competencia_padrao():
    if config.COMPETENCIA_PADRAO:
        return config.COMPETENCIA_PADRAO
    if g is not None:
        return g.COMPETENCIA
    # Sem o motor: o mês anterior ao atual (o PPI publica com um mês de atraso).
    hoje = datetime.now()
    ano, mes = (hoje.year, hoje.month - 1) if hoje.month > 1 else (hoje.year - 1, 12)
    return f"{ano}{mes:02d}"


def slugify(nome):
    """Mesmo slug que o gerar_ppi usa nos nomes dos PDFs e do cache."""
    if g is not None:
        return g.slugify(nome)
    s = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def formatar_competencia(comp):
    return comp[:4] + "/" + comp[4:]


def validar_competencia(comp):
    comp = (comp or "").strip()
    if not re.fullmatch(r"\d{6}", comp) or not 1 <= int(comp[4:]) <= 12:
        raise ValueError("competência inválida: use AAAAMM, ex.: 202608")
    return comp


def nova_sessao():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    return s


def _validar_nome_lista(nome):
    """Aceita apenas o nome lógico de uma lista, nunca um caminho."""
    nome = str(nome or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", nome):
        raise ValueError("nome de lista inválido")
    return nome


# --- listas de cidades ---------------------------------------------------------

def listar_listas():
    """[{nome, cidades:[...], caminho}] — cada .txt em gerar_ppi/listas é uma lista."""
    saida = []
    if not config.LISTAS_DIR.is_dir():
        return saida
    for p in sorted(config.LISTAS_DIR.glob("*.txt")):
        saida.append({"nome": p.stem, "titulo": titulo_lista(p.stem), "cidades": ler_lista(p.stem), "caminho": str(p)})
    return saida


def titulo_lista(nome):
    """Título de exibição: o comentário da 1ª linha ('# CIS São Lourenço — 25 municípios'), se houver."""
    nome = _validar_nome_lista(nome)
    p = config.LISTAS_DIR / (nome + ".txt")
    if p.is_file():
        # utf-8-sig: lista salva no Bloco de Notas pode vir com BOM antes do "#"
        primeira = p.read_text(encoding="utf-8-sig").splitlines()[:1]
        if primeira and primeira[0].startswith("#"):
            return primeira[0].lstrip("# ").split(" — ")[0].strip() or nome.upper()
    return nome.replace("_", " ").upper()


def ler_lista(nome):
    nome = _validar_nome_lista(nome)
    p = config.LISTAS_DIR / (nome + ".txt")
    if not p.is_file():
        raise FileNotFoundError("lista não encontrada: " + nome)
    cidades = []
    for linha in p.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#"):
            cidades.append(linha)
    return cidades


def salvar_lista(nome, cidades, titulo=""):
    nome = re.sub(r"[^a-z0-9_\-]+", "_", str(nome or "").strip().lower()).strip("_")
    if not nome:
        raise ValueError("dê um nome para a lista")
    cidades = limpar_cidades(cidades)
    if not cidades:
        raise ValueError("a lista está vazia")
    titulo = (titulo or "").strip() or nome.replace("_", " ").upper()
    config.LISTAS_DIR.mkdir(parents=True, exist_ok=True)
    (config.LISTAS_DIR / (nome + ".txt")).write_text(
        f"# {titulo} — {len(cidades)} municípios\n" + "\n".join(cidades) + "\n", encoding="utf-8")
    return nome


def apagar_lista(nome):
    nome = _validar_nome_lista(nome)
    p = config.LISTAS_DIR / (nome + ".txt")
    if p.is_file():
        p.unlink()


def limpar_cidades(texto_ou_lista):
    """Aceita texto com uma cidade por linha (ou separadas por ;) ou uma lista."""
    if isinstance(texto_ou_lista, str):
        partes = re.split(r"[\n;]+", texto_ou_lista)
    else:
        partes = list(texto_ou_lista)
    vistos, saida = set(), []
    for c in partes:
        c = c.strip().strip('"').strip()
        if c and not c.startswith("#") and c.lower() not in vistos:
            vistos.add(c.lower())
            saida.append(c)
    return saida


# --- consulta com cache --------------------------------------------------------

def _caminho_cache(competencia, cidade):
    return CACHE_PPI / competencia / (slugify(cidade) + ".json")


def ler_cache(competencia, cidade):
    p = _caminho_cache(competencia, cidade)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def extrair_total_oficial(html):
    """Linha 'Total' impressa pelo site: (qtd, valor). None se não achar."""
    soup = BeautifulSoup(html, "html.parser")
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 4 and tds[0].get_text(strip=True).lower() == "total":
            qtd = tds[2].get_text(strip=True).replace(".", "").replace(",", ".")
            val = tds[3].get_text(strip=True).replace("R$", "").strip().replace(".", "").replace(",", ".")
            try:
                return float(qtd or 0), float(val or 0)
            except ValueError:
                return None
    return None


def consultar_cidade(session, cidade, competencia, subgrupos=("0203", "0202"), usar_cache=True, log=None):
    """Consulta (ou lê do cache) os subgrupos de uma cidade numa competência.

    Retorna:
      {cidade, origem_nome, codigo_ibge, competencia, consultado_em,
       subgrupos: {"0203": {"procedimentos": [...], "total_oficial": [qtd, valor]}, ...}}
    """
    dados = ler_cache(competencia, cidade) if usar_cache else None
    faltam = [s for s in subgrupos if not dados or s not in dados.get("subgrupos", {})]
    if dados and not faltam:
        if log:
            log(f"{cidade}: cache ({', '.join(subgrupos)})")
        return dados

    g = _motor()
    # A competência é lida das constantes do módulo pelo consultar_subgrupo.
    g.COMPETENCIA = competencia
    g.COMPETENCIA_FMT = formatar_competencia(competencia)

    if not dados:
        # Busca pelo nome oficial (SES-MG) e confere o código IBGE: nomes com
        # parêntese, hífen, apóstrofo ou grafia diferente não acham a cidade errada.
        from core import municipios
        oficial = municipios.resolver(cidade)
        nomes = municipios.nomes_para_busca(oficial["codigo"], oficial["nome"]) if oficial else [cidade]
        for i, nome in enumerate(nomes):
            try:
                origem_nome, origem_id = g.buscar_origem_id(session, nome)
                break
            except ValueError:  # "não encontrado": tenta a próxima grafia conhecida
                if i == len(nomes) - 1:
                    raise
        if oficial and not origem_nome.startswith(oficial["codigo"]):
            raise ValueError(f"o PPI devolveu {origem_nome}, mas {cidade} é {oficial['codigo']} - {oficial['nome']}")
        dados = {
            "cidade": cidade,
            "origem_nome": origem_nome,
            "origem_id": origem_id,
            "codigo_ibge": origem_nome.split(" - ", 1)[0],
            "competencia": competencia,
            "subgrupos": {},
        }
    for sub in faltam:
        token = g.obter_csrf_token(session)
        html = g.consultar_subgrupo(session, token, dados["origem_nome"], dados["origem_id"], sub)
        procedimentos = g.parse_resultado(html)
        total = extrair_total_oficial(html)
        dados["subgrupos"][sub] = {
            "procedimentos": procedimentos,
            "total_oficial": list(total) if total else None,
        }
        if log:
            log(f"{cidade} / {sub}: {len(procedimentos)} procedimentos")
        time.sleep(0.6)  # gentileza com o servidor
    dados["consultado_em"] = datetime.now().strftime("%d/%m/%Y %H:%M")

    p = _caminho_cache(competencia, cidade)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    return dados


def cidades_em_cache(competencia):
    pasta = CACHE_PPI / competencia
    return sorted(p.stem for p in pasta.glob("*.json")) if pasta.is_dir() else []
