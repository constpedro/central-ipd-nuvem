#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extração dos dados do SISCAN (citologia do colo do útero) direto do TabNet/DATASUS,
sem navegador.

A interface do TabNet exige uma sequência de cliques e funções JavaScript internas
(`exibirselect`, `acha`, `achaopcao`) para escolher o município. Isso vale para quem
está usando a tela — por baixo, o TabNet é um formulário POST comum: basta enviar os
campos com os valores corretos para `webtabx.exe` e ler o resultado.

Detalhes que importam:
- A página usa ISO-8859-1, tanto na leitura quanto no corpo do POST. Enviar UTF-8 faz
  o filtro de município ser ignorado silenciosamente (o TabNet devolve o total do
  estado inteiro em vez de dar erro).
- Os nomes dos campos têm acento ("SMunic do un.saúde") — precisam ser codificados em
  ISO-8859-1 junto com os valores.
- Use "Munic do un.saúde" (S2) e NÃO "Munic do prest.serviço" (S1), que volta vazio.
- A tabela de resultado é desenhada por JavaScript (Google Charts) a partir de um array
  `data.addRows([...])`. Ela não existe como HTML de tabela — por isso lemos direto do
  array embutido no <script>.
"""

import os
import re
from datetime import date
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BASE = os.environ.get("SISCAN_BASE_URL", "https://tabnet.datasus.gov.br/cgi/").rstrip("/") + "/"
DEF_CITO_COLO = "SISCAN/cito_colo_atendmg.def"

# Campos que devem ir preenchidos; todos os outros filtros vão como "todas as categorias".
CAMPOS_PROPRIOS = {
    "Linha", "Coluna", "Incremento", "Arquivos",
    "SMunic do un.saúde", "SMes/Ano competencia",
}


def _valor_por_prefixo(soup, nome_select, prefixo):
    """Acha o value de uma <option> pelo começo do texto do value."""
    sel = soup.find("select", {"name": nome_select})
    if sel is None:
        raise RuntimeError(f"campo '{nome_select}' não existe na página do TabNet")
    for opt in sel.find_all("option"):
        if opt.get("value", "").startswith(prefixo):
            return opt["value"]
    raise RuntimeError(f"opção '{prefixo}' não encontrada em '{nome_select}'")


# O TabNet ignora boa parte das conexões novas (medido em 09/2026: ~60%), mas
# as que ele aceita abrem em menos de 0,1 s. Desistir cedo e tentar de novo é
# muito mais rápido do que esperar os ~21 s do Windows a cada conexão ignorada.
TIMEOUT_CONEXAO = 4


def abrir_formulario(session, arquivo_def=DEF_CITO_COLO):
    """Carrega a página de consulta e devolve o BeautifulSoup dela."""
    resp = session.get(BASE + "dhdat.exe?" + arquivo_def, timeout=(TIMEOUT_CONEXAO, 90))
    resp.raise_for_status()
    resp.encoding = "iso-8859-1"
    return BeautifulSoup(resp.text, "html.parser")


def _parse_colunas(html):
    """Lê os nomes das colunas do trecho data.addColumn(...)."""
    trecho = html[html.find("addColumn"):html.find("addRows")]
    nomes = re.findall(r"addColumn\('(?:string|number)','([^']*)'\)", trecho)
    # A primeira coluna é o rótulo da linha (Mes/Ano competencia); as demais são dados.
    return [n.strip() for n in nomes[1:]]


def _parse_linhas(html):
    """Lê as linhas do trecho data.addRows([...])."""
    inicio = html.find("addRows")
    fim = html.find("]);", inicio)
    if inicio < 0 or fim < 0:
        return []
    trecho = html[inicio:fim]
    linhas = []
    for bruto in re.findall(r"\[\s*[\"']([^\"']*)[\"']([^\[\]]*)\]", trecho):
        rotulo, resto = bruto[0].strip(), bruto[1]
        valores = re.findall(r"f:\s*'([^']*)'", resto)
        if not valores:
            continue
        linhas.append({"label": rotulo, "valores": valores})
    return linhas


def _parse_atualizacao(html):
    m = re.search(r"atualiza[^:]{0,20}dos dados:\s*(?:<[^>]+>\s*)*([0-9]{2}/[0-9]{2}/[0-9]{4})",
                  html, re.IGNORECASE)
    if m:
        return m.group(1)
    return date.today().strftime("%d/%m/%Y")


def consultar(session, codigo_ibge, ano, arquivo_def=DEF_CITO_COLO,
              coluna="Interv Resultado", soup=None):
    """Consulta a produção do SISCAN de um município num ano.

    Devolve um dicionário com:
        colunas       -> lista com os nomes das colunas de dados
        linhas        -> [{"label": "JANEIRO/2026", "valores": ["3", "-", ...]}, ...]
                         A primeira linha é o "Total" do período.
        atualizacao   -> data de atualização informada pelo TabNet (dd/mm/aaaa)

    O TabNet omite colunas inteiramente zeradas no período, então a lista de colunas
    varia de município para município — por isso ela vem junto no retorno.
    """
    if soup is None:
        soup = abrir_formulario(session, arquivo_def)

    campos = [
        ("Linha", _valor_por_prefixo(soup, "Linha", "Mes/Ano competencia")),
        ("Coluna", _valor_por_prefixo(soup, "Coluna", coluna)),
        ("Incremento", _valor_por_prefixo(soup, "Incremento", "Exames")),
        ("Arquivos", arquivo_def),
        ("SMunic do un.saúde",
         _valor_por_prefixo(soup, "SMunic do un.saúde", str(codigo_ibge))),
    ]

    # Período: todos os meses do ano pedido que existirem na lista.
    meses = [o["value"] for o in soup.find("select", {"name": "SMes/Ano competencia"}).find_all("option")
             if re.search(r"\|" + str(ano) + r"\d\d\|", o.get("value", ""))]
    if not meses:
        raise RuntimeError(f"o TabNet não oferece nenhum mês de {ano}")
    campos.extend(("SMes/Ano competencia", m) for m in meses)

    # Todos os demais filtros ficam abertos.
    for sel in soup.find_all("select"):
        nome = sel.get("name") or ""
        if nome in CAMPOS_PROPRIOS or nome.startswith("listahidden"):
            continue
        if nome.startswith(("S", "X")):
            campos.append((nome, "TODAS_AS_CATEGORIAS__"))
    campos += [("formato", "table"), ("mostre", "Mostra")]

    corpo = urlencode(campos, encoding="iso-8859-1", errors="replace")
    resp = session.post(
        BASE + "webtabx.exe?" + arquivo_def,
        data=corpo,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=(TIMEOUT_CONEXAO, 120),
    )
    resp.raise_for_status()
    resp.encoding = "iso-8859-1"
    html = resp.text

    if "addRows" not in html and "nenhum registro selecionado" in html.lower():
        raise RuntimeError(f"SISCAN sem nenhum exame registrado em {ano} para este município")
    if "addRows" not in html:
        raise RuntimeError("o TabNet não devolveu tabela de resultado")

    return {
        "colunas": _parse_colunas(html),
        "linhas": _parse_linhas(html),
        "atualizacao": _parse_atualizacao(html),
    }


def meses_do_ano(soup, ano):
    """[(value, rotulo, AAAAMM)] dos meses do ano oferecidos pelo formulário."""
    sel = soup.find("select", {"name": "SMes/Ano competencia"})
    saida = []
    for o in sel.find_all("option"):
        valor = o.get("value", "")
        m = re.match(r"([^|]+)\|(" + str(ano) + r"\d\d)\|", valor)
        if m:
            saida.append((valor, m.group(1).strip(), m.group(2)))
    return sorted(saida, key=lambda t: t[2])


def consultar_mes_estado(session, valor_mes, soup, arquivo_def=DEF_CITO_COLO,
                         coluna="Interv Resultado"):
    """Um mês, todos os municípios de MG numa consulta só.

    Linha = município da unidade de saúde, coluna = faixa de dias. Devolve
        {"colunas": [...], "municipios": {"310010": ["-", "3", ...]}, "atualizacao": ...}
    Município sem nenhum exame no mês simplesmente não aparece.
    """
    campos = [
        ("Linha", _valor_por_prefixo(soup, "Linha", "Munic do un.saúde")),
        ("Coluna", _valor_por_prefixo(soup, "Coluna", coluna)),
        ("Incremento", _valor_por_prefixo(soup, "Incremento", "Exames")),
        ("Arquivos", arquivo_def),
        ("SMes/Ano competencia", valor_mes),
        ("SMunic do un.saúde", "TODAS_AS_CATEGORIAS__"),
    ]
    for sel in soup.find_all("select"):
        nome = sel.get("name") or ""
        if nome in CAMPOS_PROPRIOS or nome.startswith("listahidden"):
            continue
        if nome.startswith(("S", "X")):
            campos.append((nome, "TODAS_AS_CATEGORIAS__"))
    campos += [("formato", "table"), ("mostre", "Mostra")]

    resp = session.post(
        BASE + "webtabx.exe?" + arquivo_def,
        data=urlencode(campos, encoding="iso-8859-1", errors="replace"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=(TIMEOUT_CONEXAO, 180),
    )
    resp.raise_for_status()
    resp.encoding = "iso-8859-1"
    html = resp.text
    if "addRows" not in html and "nenhum registro selecionado" in html.lower():
        # Mês ainda sem dados (o formulário lista o ano inteiro, inclusive o futuro).
        return {"colunas": [], "municipios": {}, "atualizacao": _parse_atualizacao(html)}
    if "addRows" not in html:
        raise RuntimeError("o TabNet não devolveu tabela de resultado")
    municipios = {}
    for linha in _parse_linhas(html):
        m = re.match(r"(\d{6})\b", linha["label"])
        if m:
            municipios[m.group(1)] = linha["valores"]
    if not municipios:
        raise RuntimeError("o TabNet devolveu a tabela sem municípios")
    return {
        "colunas": _parse_colunas(html),
        "municipios": municipios,
        "atualizacao": _parse_atualizacao(html),
    }


def _inteiro(texto):
    t = str(texto).replace(".", "").strip()
    return int(t) if t.isdigit() else 0


def _fmt(n):
    return f"{n:,}".replace(",", ".") if n else "-"


def montar_resultado_municipio(codigo_ibge, meses):
    """Remonta, a partir das consultas mensais do estado, o mesmo dicionário que
    consultar() devolve para um município (Total + uma linha por mês com exame).

    meses: [(rotulo, dados_do_mes)] em ordem cronológica. None se não houver exame.
    """
    codigo = str(codigo_ibge)
    # Ordem das colunas: a do TabNet (faixas crescentes, "Total" por último).
    ordem = []
    for _, dados in meses:
        for c in dados["colunas"]:
            if c not in ordem:
                ordem.append(c)
    if "Total" in ordem:
        ordem.remove("Total")
        ordem.append("Total")

    linhas = []
    for rotulo, dados in meses:
        valores = dados["municipios"].get(codigo)
        if valores is None:
            continue
        por_coluna = dict(zip(dados["colunas"], valores))
        linhas.append((rotulo, {c: _inteiro(por_coluna.get(c, "-")) for c in ordem}))
    if not linhas:
        return None
    # Como o TabNet faz na consulta do município: some a coluna toda zerada.
    colunas = [c for c in ordem if c == "Total" or any(v[c] for _, v in linhas)]
    total = {c: sum(v[c] for _, v in linhas) for c in colunas}
    return {
        "colunas": colunas,
        "linhas": [{"label": "Total", "valores": [_fmt(total[c]) for c in colunas]}]
                  + [{"label": r, "valores": [_fmt(v[c]) for c in colunas]} for r, v in linhas],
        "atualizacao": max((d["atualizacao"] for _, d in meses),
                           key=lambda s: s[6:] + s[3:5] + s[:2]),
    }


def formatar_para_relatorio(resultado):
    """Converte o retorno de consultar() no formato que o template do relatório usa.

    - Os meses aparecem como "Janeiro/2026" (e não "JANEIRO/2026").
    - A linha de total vem marcada com total=True.
    - Os traços do TabNet ("-") viram travessão ("—").
    """
    colunas = list(resultado["colunas"])
    linhas = []
    for linha in resultado["linhas"]:
        rotulo = linha["label"].strip()
        eh_total = rotulo.lower() == "total"
        if not eh_total and "/" in rotulo:
            mes, _, ano = rotulo.partition("/")
            rotulo = mes.capitalize() + "/" + ano
        valores = [("—" if v.strip() in ("-", "") else v) for v in linha["valores"]]
        item = {"label": "Total" if eh_total else rotulo, "valores": valores}
        if eh_total:
            item["total"] = True
        linhas.append(item)
    return colunas, linhas
