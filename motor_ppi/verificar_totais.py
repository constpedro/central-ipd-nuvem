# -*- coding: utf-8 -*-
"""Confere, cidade a cidade, se os totais do relatório batem com o site.

Para cada cidade/subgrupo compara:
  1. a soma das linhas que o script extrai  X  a linha "Total" da página do PPI;
  2. (só 0202) a agregação por município     X  a consulta "Discriminar por:
     Município Atendimento" feita pelo próprio site.
Não gera PDF nenhum — é só auditoria.
"""
import io
import argparse
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import gerar_ppi_ipd as g


def num(t):
    t = t.replace("R$", "").strip().replace(".", "").replace(",", ".")
    try:
        return float(t) if t else 0.0
    except ValueError:
        return 0.0


def consulta_bruta(session, token, nome, oid, subgrupo, discriminar):
    payload = {
        "_token": token, "_method": "POST", "foco-pesquisa": "1", "tipo-origem": "1",
        "discriminar-por": discriminar, "grupo": "02", "subgrupo": subgrupo,
        "complexidade": "", "sistema-informacao": "", "forma-organizacao": "",
        "origem-recurso": "", "procedimento": "", "gestao-recurso": "",
        "competencia": g.COMPETENCIA, "origem": nome, "origem-id": str(oid),
        "atendimento": "", "atendimento-id": "",
    }
    r = session.post(g.URL_CONSULTA, data=payload, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def total_do_site(html):
    """Lê a linha 'Total' que o próprio site imprime no rodapé da tabela."""
    soup = BeautifulSoup(html, "html.parser")
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 4 and tds[0].get_text(strip=True).lower().startswith("total"):
            return num(tds[2].get_text(strip=True)), num(tds[3].get_text(strip=True))
    return None, None


def municipios_do_site(html):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        c0 = tds[0].get_text(strip=True)
        if not re.match(r"^\d+\s*-\s*.+$", c0):
            continue
        out[c0.split(" - ", 1)[0].strip()] = num(tds[1].get_text(strip=True))
    return out


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Confere os totais do PPI contra o site oficial.")
    parser.add_argument("cidades", nargs="*", help="Municípios a conferir.")
    parser.add_argument("--lista", metavar="NOME", help="Arquivo em listas/.")
    parser.add_argument("--competencia", default=g.COMPETENCIA, metavar="AAAAMM",
                        help="Competência do PPI (padrão: " + g.COMPETENCIA + ").")
    args = parser.parse_args(argv)
    if bool(args.cidades) == bool(args.lista):
        parser.error("informe cidades ou use --lista NOME")
    if not re.fullmatch(r"\d{6}", args.competencia) or not 1 <= int(args.competencia[4:]) <= 12:
        parser.error("competência inválida: " + args.competencia + " (use AAAAMM)")
    return args


def main(argv=None):
    args = parse_args(argv)
    cidades = g.carregar_lista(args.lista) if args.lista else list(args.cidades)
    g.COMPETENCIA = args.competencia
    g.COMPETENCIA_FMT = args.competencia[:4] + "/" + args.competencia[4:]

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    problemas = []
    checagens = 0

    for i, cidade in enumerate(cidades, 1):
        try:
            nome, oid = g.buscar_origem_id(session, cidade)
        except Exception as e:
            problemas.append(f"{cidade}: autocomplete falhou ({e})")
            print(f"[{i}/{len(cidades)}] {cidade}: ERRO no autocomplete")
            continue

        linha = f"[{i}/{len(cidades)}] {nome:38s}"
        for subgrupo in ("0203", "0202"):
            token = g.obter_csrf_token(session)
            html = consulta_bruta(session, token, nome, oid, subgrupo, "2")
            procs = g.parse_resultado(html)
            sq = sum(p["qtd_anual"] for p in procs)
            sv = sum(p["valor_anual"] for p in procs)
            tq, tv = total_do_site(html)
            checagens += 1
            if tq is None:
                problemas.append(f"{cidade}/{subgrupo}: site nao trouxe linha Total")
                linha += f"  {subgrupo}:SEM-TOTAL"
                continue
            if abs(sq - tq) > 0.01 or abs(sv - tv) > 0.01:
                problemas.append(
                    f"{cidade}/{subgrupo}: script qtd={sq:.0f} R${sv:.2f} | site qtd={tq:.0f} R${tv:.2f}")
                linha += f"  {subgrupo}:DIVERGE"
            else:
                linha += f"  {subgrupo}:ok({len(procs)})"

            if subgrupo in g.AGRUPAR_POR_MUNICIPIO:
                agreg = {m["municipio"].split(" - ", 1)[0]: m["valor_anual"]
                         for m in g.agrupar_por_municipio(procs)}
                token = g.obter_csrf_token(session)
                site = municipios_do_site(consulta_bruta(session, token, nome, oid, subgrupo, "1"))
                checagens += 1
                dif = [k for k in set(agreg) | set(site)
                       if abs(agreg.get(k, 0) - site.get(k, 0)) > 0.01]
                if dif:
                    problemas.append(f"{cidade}/0202 agregacao: municipios divergentes {dif}")
                    linha += " agreg:DIVERGE"
                else:
                    linha += f" agreg:ok({len(agreg)})"
            time.sleep(0.4)
        print(linha)

    print("\n==== RESULTADO ====")
    print(f"Checagens feitas: {checagens}")
    if problemas:
        print(f"PROBLEMAS: {len(problemas)}")
        for p in problemas:
            print("  - " + p)
    else:
        print("Todos os totais conferem exatamente com o site do PPI.")
    return 1 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())
