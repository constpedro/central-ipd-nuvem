#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gera em lote o relatório consolidado SISCAN + PPI de citologia do colo do útero.

É o mesmo relatório que a skill `ppi-siscan-relatorio` produz — duas seções numa página
A4, produção real do SISCAN mês a mês em cima e a meta mensal do PPI 0203 embaixo —,
só que puxando os dois sistemas por requisição direta, sem navegador. Uma cidade leva
alguns segundos em vez de vários minutos.

Uso:
    python gerar_siscan.py Uberlândia Araguari
    python gerar_siscan.py --lista cisalp
    python gerar_siscan.py "Patos de Minas" --competencia 202609
    python gerar_siscan.py Frutal --referencia Uberaba

Saída: pasta ./saida/, arquivos siscan_<cidade>_<competencia>_com_ppi.pdf

O resultado já consultado do SISCAN é mantido em cache local por cidade e ano.
Use --atualizar-siscan quando precisar ignorar esse cache e buscar os dados atuais.
"""

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import siscan
import relatorio_siscan_ppi
import gerar_ppi_ipd as ppi
from gerar_ppi_ipd import (
    COMPETENCIA,
    LOG_PATH,
    OUT_DIR,
    buscar_origem_id,
    carregar_lista,
    consultar_subgrupo,
    localizar_navegador,
    obter_csrf_token,
    parse_resultado,
    slugify,
)

# Os 7 procedimentos de citologia monitorados no relatório, todos do subgrupo 0203.
# A chave é o final do código (0203010019 -> "019"); o nome é o rótulo curto usado na
# tabela do PDF, para não estourar a largura da coluna.
CODIGOS_MONITORADOS = {
    "019": "Exame Citopatológico Cérvico-Vaginal/Microflora",
    "022": "Exame Anatomo-Patológico do Colo Uterino - Peça Cirúrgica",
    "043": "Exame Citopatológico de Mama",
    "065": "Exame Anatomopatológico de Mama - Biópsia",
    "073": "Exame Anatomopatológico de Mama - Peça Cirúrgica",
    "081": "Exame Anatomo-Patológico do Colo Uterino - Biópsia",
    "086": "Exame Citopatológico Cérvico Vaginal/Microflora-Rastreamento",
}

# A Central IPD mantém um cache do PPI por competência e município. O gerador
# continua funcionando sozinho quando essa variável não existe, mas, quando é
# chamado pela Central, pode reutilizar o 0203 que já foi consultado para os
# relatórios por consórcio.
def _localizar_cache_central():
    informado = os.environ.get("CENTRAL_IPD_PPI_CACHE_DIR")
    candidatos = [Path(informado)] if informado else []
    # Fallback para a instalação padrão deste computador. Assim, uma Central
    # já aberta também passa a aproveitar o cache sem exigir reinicialização.
    candidatos.append(Path.home() / "Downloads" / "central-ipd" / "data" / "cache" / "ppi")
    for candidato in candidatos:
        if candidato.is_dir():
            return candidato
    return None


PPI_CACHE_DIR = _localizar_cache_central()


def _localizar_cache_siscan():
    informado = os.environ.get("CENTRAL_IPD_SISCAN_CACHE_DIR")
    candidatos = [Path(informado)] if informado else []
    candidatos.append(Path.home() / "Downloads" / "central-ipd" / "data" / "cache" / "siscan")
    for candidato in candidatos:
        # A pasta final pode ser criada na primeira execução; basta a pasta de
        # cache da Central já existir para autorizar esse caminho.
        if candidato.is_dir() or candidato.parent.is_dir():
            return candidato
    return None


SISCAN_CACHE_DIR = _localizar_cache_siscan()


def _com_retentativa(descricao, funcao, tentativas=4, espera=3):
    """Repete uma chamada de rede algumas vezes antes de desistir.

    Os servidores do TabNet/DATASUS costumam recusar ou demorar na primeira
    tentativa e responder normalmente na seguinte. Sem isto, uma única falha de
    rede — em especial no carregamento do formulário, que acontecia uma vez só,
    no início e SEM proteção — derrubava a rodada inteira do SISCAN, e só o PPI
    0202 acabava saindo.
    """
    # Silencioso de propósito: retentar é rotina com os servidores do governo, não
    # erro. Ficar imprimindo "falhou..." a cada tentativa deixava o log parecendo um
    # desastre mesmo quando tudo se recuperava. Se esgotar as tentativas, o erro sobe
    # e quem chamou registra a linha de erro daquela cidade.
    # Conexão recusada/ignorada é o caso comum no TabNet e custa só os poucos
    # segundos do timeout de conexão: tenta de novo na hora, várias vezes, sem
    # gastar as tentativas "lentas" reservadas para os demais erros.
    ultimo = None
    falhas_lentas = 0
    falhas_conexao = 0
    while True:
        try:
            return funcao()
        except requests.ConnectionError:
            falhas_conexao += 1
            if falhas_conexao >= 15:
                raise
            time.sleep(0.3)
        except requests.RequestException as e:
            ultimo = e
            falhas_lentas += 1
            if falhas_lentas >= tentativas:
                raise ultimo
            time.sleep(espera)


def _ler_cache_ppi(competencia, cidade):
    """Lê o cache opcional da Central, sem tornar o script dependente dele."""
    if PPI_CACHE_DIR is None:
        return None
    caminho = PPI_CACHE_DIR / competencia / (slugify(cidade) + ".json")
    if not caminho.is_file():
        return None
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    subgrupo = dados.get("subgrupos", {}).get("0203")
    if not isinstance(subgrupo, dict) or "procedimentos" not in subgrupo:
        return None
    return dados


def _abrir_formulario_siscan(session, ano, recarregar=False):
    """Reutiliza o formulário pesado do TabNet quando ele ainda contém o ano.

    recarregar=True baixa de novo, para enxergar um mês recém-publicado. Sem
    isso, o cache também é renovado quando passa de REVISAR_APOS_DIAS dias.
    """
    cache_path = (SISCAN_CACHE_DIR / "formulario_cito_colo.html"
                  if SISCAN_CACHE_DIR is not None else None)
    guardado = None
    if cache_path is not None and cache_path.is_file():
        try:
            texto = cache_path.read_text(encoding="utf-8")
            soup = BeautifulSoup(texto, "html.parser")
            meses = soup.find("select", {"name": "SMes/Ano competencia"})
            if meses is not None and any(
                    re.search(r"\|" + str(ano) + r"\d\d\|", o.get("value", ""))
                    for o in meses.find_all("option")):
                guardado = soup
                velho = time.time() - cache_path.stat().st_mtime > REVISAR_APOS_DIAS * 86400
                if not recarregar and not velho:
                    print("Formulário SISCAN em cache local, sem novo download")
                    return soup
        except (OSError, ValueError):
            pass

    try:
        soup = _com_retentativa(
            "carregar o formulário do TabNet",
            lambda: siscan.abrir_formulario(session))
    except requests.RequestException:
        if guardado is None:
            raise
        # O TabNet não respondeu: segue com o formulário guardado em vez de parar.
        print("Aviso: não consegui renovar o formulário do TabNet; usando o guardado.")
        return guardado
    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(str(soup), encoding="utf-8")
        except OSError:
            pass
    return soup


# Quantas cidades processar ao mesmo tempo (PPI + PDF).
PARALELO = max(1, int(os.environ.get("CENTRAL_IPD_PARALELO", "4") or 4))
# Com --atualizar-siscan, só os meses mais recentes são consultados de novo: os
# antigos quase não mudam e continuam vindo do cache mensal.
MESES_REVISADOS = 3
# Mesmo sem --atualizar-siscan, esses meses recentes são consultados de novo
# quando o cache deles tem mais de tantos dias (o DATASUS segue lançando exames
# atrasados por algumas semanas depois de publicar o mês).
REVISAR_APOS_DIAS = 7


def _caminho_cache_mes(aaaamm):
    if SISCAN_CACHE_DIR is None:
        return None
    return SISCAN_CACHE_DIR / "meses" / (aaaamm + ".json")


def carregar_meses_estado(session, soup, ano, atualizar=False):
    """Produção do SISCAN de todos os municípios de MG, mês a mês, no ano.

    Cada mês é UMA consulta ao TabNet com o estado inteiro, guardada em
    cache/meses/AAAAMM.json — serve a todas as cidades do lote e às próximas
    rodadas. Devolve [(rotulo, dados_do_mes)] em ordem, ou None se falhar.
    """
    hoje = time.strftime("%Y%m")
    meses = [m for m in siscan.meses_do_ano(soup, ano) if m[2] <= hoje]
    if not meses:
        return None
    recentes = {m[2] for m in meses[-MESES_REVISADOS:]}
    revisar = set(recentes) if atualizar else set()
    for aaaamm in recentes:
        caminho = _caminho_cache_mes(aaaamm)
        try:
            if caminho is not None and time.time() - caminho.stat().st_mtime > REVISAR_APOS_DIAS * 86400:
                revisar.add(aaaamm)
        except OSError:
            pass
    resultado = {}
    faltam = []
    for valor, rotulo, aaaamm in meses:
        caminho = _caminho_cache_mes(aaaamm)
        if aaaamm not in revisar and caminho is not None and caminho.is_file():
            try:
                resultado[aaaamm] = (rotulo, json.loads(caminho.read_text(encoding="utf-8")))
                continue
            except (OSError, json.JSONDecodeError):
                pass
        faltam.append((valor, rotulo, aaaamm))

    if faltam:
        print("SISCAN: consultando " + str(len(faltam)) + " mês(es) do estado inteiro ("
              + ", ".join(r for _, r, _ in faltam) + ")")

    def _baixar(item):
        valor, rotulo, aaaamm = item
        sessao = _nova_sessao()
        dados = _com_retentativa(
            "consultar o SISCAN de " + rotulo,
            lambda: siscan.consultar_mes_estado(sessao, valor, soup))
        dados["consultado_em"] = time.strftime("%d/%m/%Y %H:%M")
        caminho = _caminho_cache_mes(aaaamm)
        # Mês vazio (ainda não publicado) não vai para o cache: na próxima rodada
        # ele é consultado de novo.
        if caminho is not None and dados["municipios"]:
            try:
                caminho.parent.mkdir(parents=True, exist_ok=True)
                caminho.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return aaaamm, rotulo, dados

    try:
        with ThreadPoolExecutor(max_workers=min(4, len(faltam)) or 1) as ex:
            for aaaamm, rotulo, dados in ex.map(_baixar, faltam):
                resultado[aaaamm] = (rotulo, dados)
                print("SISCAN " + rotulo + ": " + str(len(dados["municipios"])) + " municípios")
    except Exception as e:
        print("Aviso: a consulta mensal do SISCAN falhou (" + type(e).__name__
              + "). Vou consultar cidade a cidade.")
        return None
    return [resultado[k] for k in sorted(resultado)]


def _caminho_cache_resultado_siscan(cidade, ano):
    if SISCAN_CACHE_DIR is None:
        return None
    return SISCAN_CACHE_DIR / "resultados" / str(ano) / (slugify(cidade) + ".json")


def _ler_cache_resultado_siscan(cidade, ano, codigo_ibge):
    """Lê o resultado parseado do SISCAN sem repetir a consulta ao TabNet."""
    caminho = _caminho_cache_resultado_siscan(cidade, ano)
    if caminho is None or not caminho.is_file():
        return None
    try:
        pacote = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if str(pacote.get("codigo_ibge", "")) != str(codigo_ibge):
        return None
    dados = pacote.get("dados")
    if not isinstance(dados, dict):
        return None
    if not all(chave in dados for chave in ("colunas", "linhas", "atualizacao")):
        return None
    return dados


def _salvar_cache_resultado_siscan(cidade, ano, codigo_ibge, dados):
    caminho = _caminho_cache_resultado_siscan(cidade, ano)
    if caminho is None:
        return
    pacote = {
        "cidade": cidade,
        "codigo_ibge": str(codigo_ibge),
        "ano": str(ano),
        "consultado_em": time.strftime("%d/%m/%Y %H:%M"),
        "dados": dados,
    }
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(json.dumps(pacote, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        # O cache é apenas uma otimização; falta de permissão não pode impedir o PDF.
        pass


def _salvar_meta_mensal(cidade, codigo_ibge, competencia, meta_mensal, procedimentos):
    """Guarda a meta mensal do PPI para o painel de metas da Central."""
    if SISCAN_CACHE_DIR is None:
        return
    caminho = SISCAN_CACHE_DIR / "metas" / (slugify(cidade) + ".json")
    pacote = {
        "cidade": cidade,
        "codigo_ibge": str(codigo_ibge),
        "competencia_ppi": competencia,
        "meta_mensal": meta_mensal,
        "procedimentos": procedimentos,
        "salvo_em": time.strftime("%d/%m/%Y %H:%M"),
    }
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_text(json.dumps(pacote, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _filtrar_ppi(procedimentos):
    """Filtra os sete códigos monitorados, tanto da rede quanto do cache."""
    selecionados = []
    encontrados = set()
    for p in procedimentos:
        final = p["codigo"][-3:]
        if final not in CODIGOS_MONITORADOS:
            continue
        encontrados.add(final)
        selecionados.append({
            "codigo": final,
            "nome": CODIGOS_MONITORADOS[final],
            "municipio_atendimento": p["municipio_atendimento"],
            "qtd_anual": p["qtd_anual"],
            "valor_anual": p["valor_anual"],
        })
    selecionados.sort(key=lambda x: x["codigo"])
    ausentes = sorted(set(CODIGOS_MONITORADOS) - encontrados)
    return selecionados, ausentes


def coletar_ppi(session, origem_nome, origem_id, subgrupo="0203"):
    """Consulta o PPI e filtra só os códigos monitorados de citologia."""
    token = obter_csrf_token(session)
    procedimentos = parse_resultado(
        consultar_subgrupo(session, token, origem_nome, origem_id, subgrupo))
    return _filtrar_ppi(procedimentos)


def _numero_tabnet(texto):
    """'1.524' -> 1524; '-' ou vazio -> 0."""
    limpo = texto.strip().replace(".", "")
    return int(limpo) if limpo.isdigit() else 0


def avaliar_meta(procedimentos, dados_siscan, limite_pct):
    """Compara a produção média mensal do SISCAN com a meta mensal do PPI.

    A meta é a mesma dos cards do relatório: códigos 019 + 086, qtd anual ÷ 12.
    A produção de cada mês é a última coluna (Total) da linha do mês.
    """
    meta = sum(p["qtd_anual"] for p in procedimentos
               if p["codigo"] in relatorio_siscan_ppi.CODIGOS_TOTAL_GERAL) / 12
    meses = [(l["label"].strip().capitalize(), _numero_tabnet(l["valores"][-1]))
             for l in dados_siscan["linhas"]
             if l["label"].strip().lower() != "total" and l["valores"]]
    if meta <= 0 or not meses:
        return None
    media = sum(q for _, q in meses) / len(meses)
    pct = media / meta * 100
    return {
        "meta_mensal": meta,
        "media_mensal": media,
        "pct": pct,
        "meses": len(meses),
        "meses_abaixo": [m for m, q in meses if q < meta * limite_pct / 100],
        "abaixo": pct < limite_pct,
    }


def _nova_sessao():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    return session


def _processar_cidade_siscan(indice, total, cidade, competencia, ano, referencia,
                             out_dir, navegador, template, formulario_html,
                             atualizar_siscan=False, limite_pct=100, meses_estado=None):
    """Processa uma cidade sem compartilhar sessão HTTP com outra thread."""
    prefixo = f"{indice}/{total} {cidade}"
    # Sempre gera de novo: um PDF antigo na pasta é simplesmente substituído.
    pdf_path = out_dir / ("siscan_" + slugify(cidade) + "_" + competencia + "_com_ppi.pdf")

    session = _nova_sessao()
    try:
        cache_ppi = _ler_cache_ppi(competencia, cidade)
        if cache_ppi is not None:
            origem_nome = cache_ppi["origem_nome"]
            origem_id = cache_ppi["origem_id"]
            codigo_ibge = cache_ppi.get("codigo_ibge") or origem_nome.split(" - ", 1)[0]
            procedimentos, ausentes = _filtrar_ppi(
                cache_ppi["subgrupos"]["0203"]["procedimentos"])
            mensagens = [prefixo + " — PPI 0203 em cache (sem nova consulta ao PPI)"]
        else:
            origem_nome, origem_id = _com_retentativa(
                "buscar o município no PPI",
                lambda: buscar_origem_id(session, cidade))
            codigo_ibge = origem_nome.split(" - ", 1)[0]
            procedimentos, ausentes = _com_retentativa(
                "consultar o PPI",
                lambda: coletar_ppi(session, origem_nome, origem_id))
            mensagens = []

        if not procedimentos:
            mensagem = "PPI sem nenhum dos 7 códigos monitorados"
            return {"mensagens": mensagens + [prefixo + " — " + mensagem],
                    "erro": (cidade, mensagem)}

        # Os dados mensais do estado vêm primeiro: eles enxergam os meses
        # publicados depois. O cache por cidade nunca expirava, e o PDF de uma
        # cidade consultada em setembro continuava sem os meses seguintes.
        dados_siscan = None
        if meses_estado is not None:
            dados_siscan = siscan.montar_resultado_municipio(codigo_ibge, meses_estado)
            if dados_siscan is None:
                mensagem = "SISCAN sem registros em " + ano
                return {"mensagens": mensagens + [prefixo + " — " + mensagem],
                        "erro": (cidade, mensagem)}
            _salvar_cache_resultado_siscan(cidade, ano, codigo_ibge, dados_siscan)
        elif not atualizar_siscan:
            dados_siscan = _ler_cache_resultado_siscan(cidade, ano, codigo_ibge)
            if dados_siscan is not None:
                mensagens.append(prefixo + " — SISCAN em cache (sem nova consulta)")
        if dados_siscan is None:
            soup = (BeautifulSoup(formulario_html, "html.parser")
                    if formulario_html else None)
            dados_siscan = _com_retentativa(
                "consultar o SISCAN",
                lambda: siscan.consultar(session, codigo_ibge, ano, soup=soup))
            _salvar_cache_resultado_siscan(cidade, ano, codigo_ibge, dados_siscan)
        meses = [l for l in dados_siscan["linhas"]
                 if l["label"].strip().lower() != "total"]
        if not meses:
            mensagem = "SISCAN sem registros em " + ano
            return {"mensagens": mensagens + [prefixo + " — " + mensagem],
                    "erro": (cidade, mensagem)}

        dados = montar_dados(cidade, codigo_ibge, competencia, procedimentos,
                             ausentes, dados_siscan)
        n_pages = relatorio_siscan_ppi.gerar(dados, pdf_path, navegador, template)
        aviso = ""
        if ausentes:
            aviso += "  (codigos ausentes: " + ", ".join(ausentes) + ")"
        if n_pages > 1:
            aviso += "  [" + str(n_pages) + " paginas]"
        mensagens.append(prefixo + " — OK: " + str(len(procedimentos))
                         + " proc. PPI, " + str(len(meses)) + " meses SISCAN" + aviso)
        meta = avaliar_meta(procedimentos, dados_siscan, limite_pct)
        if meta:
            _salvar_meta_mensal(cidade, codigo_ibge, competencia, meta["meta_mensal"], procedimentos)
        if meta and meta["abaixo"]:
            mensagens.append(prefixo + " — ⚠ ABAIXO DA META: média "
                             + f"{meta['media_mensal']:.0f}/mês x meta {meta['meta_mensal']:.0f}"
                             + f" ({meta['pct']:.0f}%)")
        return {"mensagens": mensagens, "erro": None, "cidade": cidade, "meta": meta}
    except Exception as e:
        return {"mensagens": [prefixo + " — ERRO: " + str(e)],
                "erro": (cidade, str(e))}


def montar_dados(cidade, codigo_ibge, competencia, procedimentos, ausentes, siscan_dados):
    colunas, linhas = siscan.formatar_para_relatorio(siscan_dados)
    return {
        "municipio_nome": cidade,
        "municipio_codigo": codigo_ibge,
        "ano_competencia": competencia[:4],
        "competencia_ppi": competencia[:4] + "/" + competencia[4:],
        "atualizacao_dados": siscan_dados["atualizacao"],
        "siscan_linha": "Mes/Ano competencia",
        "siscan_coluna": "Interv Resultado",
        "siscan_colunas": colunas,
        "siscan_linhas": linhas,
        "ppi_subgrupo": "0203 · Anatomia patológica e citopatologia",
        "ppi_procedimentos": procedimentos,
        "ppi_codigos_ausentes": ausentes,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Gera o relatorio consolidado SISCAN + PPI dos municipios indicados.",
        epilog=("Exemplos:\n"
                "  python gerar_siscan.py\n"
                "  python gerar_siscan.py Uberlandia Araguari\n"
                '  python gerar_siscan.py "Patos de Minas" --competencia 202609\n'
                "  python gerar_siscan.py Frutal --referencia Uberaba"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("cidades", nargs="*",
                        help="Nomes dos municipios (entre aspas se tiver espaco).")
    parser.add_argument("--lista", metavar="NOME",
                        help="Le as cidades de um arquivo em listas/ (ex.: --lista cisalp).")
    parser.add_argument("--competencia", default=COMPETENCIA, metavar="AAAAMM",
                        help="Competencia do PPI; o ano dela e o periodo do SISCAN "
                             "(padrao: " + COMPETENCIA + ").")
    parser.add_argument("--referencia", default="Varginha", metavar="CIDADE",
                        help="Cidade-polo para a linha extra 'Total em <cidade> (/12)'. "
                             "Padrao: Varginha.")
    parser.add_argument("--atualizar-siscan", action="store_true",
                        help="Ignora o cache local do SISCAN e consulta o TabNet novamente.")
    parser.add_argument("--limite-meta", type=float, default=100, metavar="PCT",
                        help="Avisa quando a producao media mensal fica abaixo deste "
                             "percentual da meta do PPI (padrao: 100).")
    parser.add_argument("--saida", type=Path, default=OUT_DIR, metavar="PASTA",
                        help="Pasta onde salvar os PDFs (padrao: " + OUT_DIR.name + ").")
    args = parser.parse_args(argv)
    if not args.cidades and not args.lista:
        parser.error("informe ao menos uma cidade, ou use --lista NOME")
    if len(args.competencia) != 6 or not args.competencia.isdigit() \
            or not 1 <= int(args.competencia[4:]) <= 12:
        parser.error("competencia invalida: " + args.competencia + " (use AAAAMM)")
    return args


def main(argv=None):
    args = parse_args(argv)
    cidades = list(args.cidades)
    if args.lista:
        cidades += carregar_lista(args.lista)
    ano = args.competencia[:4]
    out_dir = args.saida
    # consultar_subgrupo() lê a competência no módulo gerar_ppi_ipd.
    ppi.COMPETENCIA = args.competencia
    ppi.COMPETENCIA_FMT = args.competencia[:4] + "/" + args.competencia[4:]
    # Não deixe erros de uma execução anterior aparecerem como se fossem atuais.
    LOG_PATH.unlink(missing_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Varginha é o polo padrão. O campo continua permitindo outro polo quando
    # necessário, mas não deixa mais a linha desaparecer por estar em branco.
    relatorio_siscan_ppi.MUNICIPIO_REFERENCIA = args.referencia.strip() or "Varginha"

    navegador = localizar_navegador()
    template = relatorio_siscan_ppi.TEMPLATE_PATH.read_text(encoding="utf-8")

    print("Competencia PPI: " + args.competencia[:4] + "/" + args.competencia[4:]
          + "  |  SISCAN: ano " + ano + "  |  cidades: " + str(len(cidades)))
    print("Linha de referencia: Total em " + relatorio_siscan_ppi.MUNICIPIO_REFERENCIA)
    print("Salvando em: " + str(out_dir.resolve()))
    print()

    ppi.aquecer_navegador(navegador, relatorio_siscan_ppi.TEMPLATE_PATH)

    session = _nova_sessao()
    # O formulário do TabNet é pesado (~1 MB) e igual para todos: carrega uma vez só.
    # O TabNet costuma falhar/demorar na 1ª tentativa; por isso a retentativa. E se
    # mesmo assim não vier, NÃO derrubamos a rodada: cada cidade recarrega o próprio
    # formulário (mais lento, mas o SISCAN continua saindo em vez de zerar tudo).
    try:
        form_siscan = _abrir_formulario_siscan(session, ano, recarregar=args.atualizar_siscan)
    except requests.RequestException as e:
        form_siscan = None
        print("Aviso: não consegui pré-carregar o formulário do TabNet ("
              + type(e).__name__ + "). Vou carregá-lo cidade a cidade.")

    # Produção do estado inteiro mês a mês (uma consulta por mês, em vez de uma
    # por cidade). Os meses já guardados vêm do disco; só os que faltam — ou os
    # recentes com cache antigo — vão ao TabNet.
    meses_estado = None
    if form_siscan is not None:
        meses_estado = carregar_meses_estado(session, form_siscan, ano, args.atualizar_siscan)

    erros = []
    avaliacoes = []
    formulario_html = str(form_siscan) if form_siscan is not None else None
    max_workers = min(PARALELO, len(cidades)) or 1
    print("Processando até " + str(max_workers) + " cidades em paralelo")
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            tarefas = [executor.submit(
                _processar_cidade_siscan, i, len(cidades), cidade,
                args.competencia, ano, args.referencia, out_dir, navegador,
                template, formulario_html, args.atualizar_siscan, args.limite_meta,
                meses_estado)
                for i, cidade in enumerate(cidades, 1)]
            concluidas = 0
            for tarefa in as_completed(tarefas):
                resultado = tarefa.result()
                for mensagem in resultado["mensagens"]:
                    print(mensagem)
                if resultado["erro"]:
                    erros.append(resultado["erro"])
                if resultado.get("meta"):
                    avaliacoes.append((resultado["cidade"], resultado["meta"]))
                concluidas += 1
                print("PROGRESSO " + str(concluidas) + "/" + str(len(cidades)))
    finally:
        ppi.fechar_navegador_aberto()

    print()
    print("==== RESUMO ====")
    print("Cidades: " + str(len(cidades)) + "  |  com erro: " + str(len(erros)))
    if avaliacoes:
        avaliacoes.sort(key=lambda a: a[1]["pct"])
        abaixo = [a for a in avaliacoes if a[1]["abaixo"]]
        print()
        print("==== ABAIXO DA META (" + f"{args.limite_meta:g}" + "% da média mensal do PPI 019+086) ====")
        if not abaixo:
            print("  Nenhum município abaixo da meta.")
        for cidade, m in abaixo:
            print(f"  - {cidade}: {m['media_mensal']:.0f}/mês x meta {m['meta_mensal']:.0f}"
                  f" ({m['pct']:.0f}%) — {len(m['meses_abaixo'])}/{m['meses']} meses abaixo")
        csv_path = out_dir / ("alertas_meta_" + args.competencia + ".csv")
        # ; e utf-8-sig para o Excel abrir direto com acentos e colunas certas.
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            escritor = csv.writer(f, delimiter=";")
            escritor.writerow(["Município", "Situação", "Meta mensal", "Média mensal",
                               "% da meta", "Meses", "Meses abaixo"])
            for cidade, m in avaliacoes:
                escritor.writerow([
                    cidade, "ABAIXO" if m["abaixo"] else "OK",
                    f"{m['meta_mensal']:.2f}".replace(".", ","),
                    f"{m['media_mensal']:.2f}".replace(".", ","),
                    f"{m['pct']:.1f}".replace(".", ","),
                    m["meses"], ", ".join(m["meses_abaixo"])])
        print("Planilha de metas: " + str(csv_path))
    if erros:
        with LOG_PATH.open("w", encoding="utf-8") as f:
            for cidade, msg in erros:
                print("  - " + cidade + " | " + msg)
                f.write(cidade + " | " + msg + "\n")
        print()
        print("Detalhes em " + str(LOG_PATH))
    return 1 if erros else 0


if __name__ == "__main__":
    sys.exit(main())
