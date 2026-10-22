"""
Robô de metas — vigia as cidades da região contra a meta de citologia do PPI.

Uma vez por dia, com a Central aberta (só no PC):
  1. baixa os meses recentes do SISCAN do estado inteiro (siscan/meses/AAAAMM.json);
  2. busca o PPI 0203 das cidades das listas que ainda não têm meta (fica no cache
     de PPI, então só a primeira rodada demora);
  3. compara a situação de cada cidade com a da rodada anterior e registra alertas:
     caiu para abaixo da meta, zerou a produção, se recuperou.

A conta da meta é a mesma da página Metas (core/metas.py).
"""
import json
import os
import re
import threading
import time
from datetime import datetime

import config
from core import consorcios_mg, contatos, metas, municipios, ppi_engine

PASTA = config.DATA_DIR / "robo"
ARQ_CONFIG = PASTA / "config.json"
ARQ_ESTADO = PASTA / "estado.json"
INTERVALO_HORAS = 20      # roda de novo quando a última rodada tem mais que isso
MAX_ALERTAS = 200


def _ler(caminho, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _gravar(caminho, dados):
    PASTA.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, caminho)


REGIONAIS_PADRAO = ["Varginha"]

# Chamado com o job no fim de cada rodada/estudo (a Central põe aqui quem salva a planilha).
AO_TERMINAR = None


def _ao_terminar(job):
    if AO_TERMINAR:
        try:
            AO_TERMINAR(job)
        except Exception as e:  # noqa: BLE001 — a cópia da planilha não derruba a rodada
            job.escrever(f"Aviso: não consegui salvar a planilha ({e}).")


def regionais_mg():
    """Unidades Regionais de Saúde (SES-MG) com o número de cidades, da mais perto de Varginha."""
    contagem, dist = {}, {}
    for m in municipios.todos():
        r = m.get("regional")
        if r:
            contagem[r] = contagem.get(r, 0) + 1
            d = municipios.distancia_varginha(m["codigo"])
            dist[r] = min(dist.get(r, 9999), d if d is not None else 9999)
    return [{"nome": r, "cidades": n} for r, n in sorted(contagem.items(), key=lambda x: dist[x[0]])]


def configuracao():
    """Listas da Central e consórcios de MG (COSECS) vigiados; regionais em estudo.

    Sem nada salvo: vigia todas as listas, nenhum consórcio de MG, e estuda só a
    regional de Varginha. "todas_mg" estende o estudo para MG inteira."""
    todas = [l["nome"] for l in ppi_engine.listar_listas()]
    salvo = _ler(ARQ_CONFIG, {})
    escolhidas = salvo.get("listas")
    siglas = {c["sigla"] for c in consorcios_mg.todos()}
    nomes_reg = {r["nome"] for r in regionais_mg()}
    return {"listas": [n for n in escolhidas if n in todas] if escolhidas is not None else todas, "todas": todas,
            "consorcios_mg": [s for s in salvo.get("consorcios_mg", []) if s in siglas],
            "regionais": [r for r in salvo.get("regionais", REGIONAIS_PADRAO) if r in nomes_reg],
            "todas_mg": bool(salvo.get("todas_mg"))}


def salvar_configuracao(listas, consorcios=(), regionais=None, todas_mg=None):
    atual = configuracao()
    todas = {l["nome"] for l in ppi_engine.listar_listas()}
    siglas = {c["sigla"] for c in consorcios_mg.todos()}
    nomes_reg = {r["nome"] for r in regionais_mg()}
    _gravar(ARQ_CONFIG, {"listas": [n for n in listas if n in todas],
                         "consorcios_mg": [s for s in consorcios if s in siglas],
                         "regionais": [r for r in regionais if r in nomes_reg] if regionais is not None
                         else atual["regionais"],
                         "todas_mg": bool(todas_mg) if todas_mg is not None else atual["todas_mg"]})


def em_estudo(codigo, cfg=None):
    """A cidade está numa regional em estudo (ou o estudo é de MG inteira)?"""
    cfg = cfg or configuracao()
    if cfg["todas_mg"]:
        return True
    return (municipios.por_codigo().get(codigo) or {}).get("regional") in set(cfg["regionais"])


def estado():
    return _ler(ARQ_ESTADO, {"ultima_rodada": None, "foto": {}, "alertas": []})


def cidades_vigiadas():
    cidades = []
    for nome in configuracao()["listas"]:
        try:
            cidades += ppi_engine.ler_lista(nome)
        except (FileNotFoundError, ValueError):
            continue
    cidades += consorcios_mg.cidades_de(configuracao()["consorcios_mg"])
    return ppi_engine.limpar_cidades(cidades)


def deve_rodar():
    ultima = estado().get("ultima_rodada")
    if not ultima:
        return True
    try:
        return (datetime.now() - datetime.fromisoformat(ultima)).total_seconds() > INTERVALO_HORAS * 3600
    except ValueError:
        return True


# --- etapas -----------------------------------------------------------------------

def _atualizar_siscan(job):
    """Meses recentes do SISCAN de MG inteiro (o mesmo cache que os relatórios usam)."""
    os.environ["CENTRAL_IPD_SISCAN_CACHE_DIR"] = str(config.CACHE_DIR / "siscan")
    os.environ["CENTRAL_IPD_PPI_CACHE_DIR"] = str(config.CACHE_DIR / "ppi")
    ppi_engine._motor()  # garante a pasta do motor no sys.path
    import gerar_siscan as gs
    ano = datetime.now().year
    sessao = ppi_engine.nova_sessao()
    soup = gs._abrir_formulario_siscan(sessao, ano, recarregar=True)
    meses = gs.carregar_meses_estado(sessao, soup, ano, atualizar=True)
    if not meses:
        raise RuntimeError("o SISCAN não devolveu os meses do estado")
    job.escrever(f"SISCAN atualizado: {len(meses)} mês(es) de {ano}, último {meses[-1][0]}")


def _tem_meta(cidade):
    slug = metas.slug_cidade(cidade)
    gravada = metas._ler(config.CACHE_DIR / "siscan" / "metas" / (slug + ".json")) or {}
    return bool(gravada.get("meta_mensal")) or metas._meta_pelo_cache_ppi(slug) is not None


def _buscar_metas(job, cidades):
    faltam = [c for c in cidades if not _tem_meta(c)]
    if not faltam:
        job.escrever("Todas as cidades já têm meta do PPI.")
        return
    job.escrever(f"Buscando o PPI de {len(faltam)} cidade(s) sem meta (só nesta primeira vez)...")
    competencia = ppi_engine.competencia_padrao()
    sessao = ppi_engine.nova_sessao()
    job.progresso(0, len(faltam))
    for i, cidade in enumerate(faltam, 1):
        if job.cancelado:
            return
        try:
            ppi_engine.consultar_cidade(sessao, cidade, competencia, subgrupos=("0203",))
        except Exception as e:  # noqa: BLE001 — uma cidade com problema não para o robô
            job.escrever(f"{cidade}: não consegui o PPI ({e})")
            sessao = ppi_engine.nova_sessao()
        job.progresso(i, len(faltam))


def _foto(linhas):
    return {l["slug"]: {"cidade": l["cidade"], "classe": l["classe"], "pct": round(l["pct"]),
                        "mes": l["mes"], "qtd": l["qtd"], "zerado": l["mes_zerado"], "seguidos": l["seguidos"]}
            for l in linhas}


def _comparar(antes, agora):
    """Alertas do que mudou de uma rodada para a outra."""
    quando = datetime.now().strftime("%d/%m/%Y %H:%M")
    alertas = []

    def alerta(slug, tipo, texto):
        alertas.append({"quando": quando, "slug": slug, "cidade": agora[slug]["cidade"], "tipo": tipo, "texto": texto})

    for slug, a in agora.items():
        b = antes.get(slug)
        if b is None:
            if a["classe"] == "abaixo":
                alerta(slug, "abaixo", f"entrou no radar abaixo da meta ({a['pct']}% no ano)")
            continue
        if a["zerado"] and not (b["zerado"] and b["mes"] == a["mes"]):
            alerta(slug, "zerou", f"nenhum exame em {a['mes']}")
        if a["classe"] == "abaixo" and b["classe"] != "abaixo":
            alerta(slug, "abaixo", f"caiu para abaixo da meta ({b['pct']}% → {a['pct']}% no ano)")
        elif a["classe"] != "abaixo" and b["classe"] == "abaixo":
            alerta(slug, "recuperou", f"saiu de abaixo da meta ({b['pct']}% → {a['pct']}% no ano)")
        elif a["mes"] != b["mes"] and a["seguidos"] >= 3:
            alerta(slug, "seguidos", f"{a['seguidos']} meses seguidos abaixo da meta (último: {a['mes']})")
    return alertas


# --- exploração: cidades de MG fora das listas -----------------------------------------
#
# Aos poucos (EXPLORAR_POR_RODADA por dia), o robô consulta o PPI 0203 das cidades de
# MG que não estão em nenhuma lista e guarda em robo/explorar.json para onde cada uma
# pactua a citologia. Fica fora do cache de PPI de propósito: assim a página Metas
# continua mostrando só as cidades que conhecemos.

ARQ_EXPLORAR = PASTA / "explorar.json"
EXPLORAR_POR_RODADA = 80
REVISAR_EXPLORADA_DIAS = 90
IBGE_VARGINHA = "317070"
_MINUSCULAS = {"de", "da", "do", "das", "dos", "e"}


def _nome_bonito(nome):
    partes = nome.lower().split()
    return " ".join(p if p in _MINUSCULAS and i else p.capitalize() for i, p in enumerate(partes))


def municipios_mg():
    """{código IBGE: nome} de todas as cidades de MG, tiradas do formulário do SISCAN."""
    arq = config.CACHE_DIR / "siscan" / "formulario_cito_colo.html"
    try:
        texto = arq.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    oficiais = {m["codigo"]: m["nome"] for m in municipios.todos()}
    return {cod: oficiais.get(cod) or _nome_bonito(nome)
            for cod, nome in re.findall(r"(31\d{4}) ([A-Z][A-Z' -]+?)\|", texto)}


def exploradas():
    feitas = _ler(ARQ_EXPLORAR, {})
    oficiais = {m["codigo"]: m["nome"] for m in municipios.todos()}
    for cod, e in feitas.items():  # as antigas foram gravadas com o nome do SISCAN, sem acento
        e["cidade"] = oficiais.get(cod, e["cidade"])
        e["varginha"] = _de_varginha(e.get("destinos", {}))  # as antigas usavam "qualquer exame em Varginha"
    return feitas


def _conhecidas():
    """Slugs das cidades de todas as listas e do cache de PPI."""
    nomes = []
    for l in ppi_engine.listar_listas():
        try:
            nomes += ppi_engine.ler_lista(l["nome"])
        except (FileNotFoundError, ValueError):
            pass
    return {metas.slug_cidade(n) for n in nomes} | set(metas._cidades_do_ppi())


def _consultar_ppi_0203(sessao, cidade, competencia, codigo=None):
    """Procedimentos do PPI 0203 da cidade, sem gravar no cache de PPI."""
    g = ppi_engine._motor()
    g.COMPETENCIA = competencia
    g.COMPETENCIA_FMT = ppi_engine.formatar_competencia(competencia)
    for i, nome in enumerate(municipios.nomes_para_busca(codigo, cidade)):
        try:
            origem_nome, origem_id = g.buscar_origem_id(sessao, nome)
            break
        except ValueError:  # "não encontrado": tenta a próxima grafia
            if i == len(municipios.nomes_para_busca(codigo, cidade)) - 1:
                raise
    if codigo and not origem_nome.startswith(codigo):
        raise ValueError(f"o PPI devolveu {origem_nome} em vez do código {codigo}")
    html = g.consultar_subgrupo(sessao, g.obter_csrf_token(sessao), origem_nome, origem_id, "0203")
    return g.parse_resultado(html)


def _resumir_ppi(procs):
    cito = [p for p in procs if str(p.get("codigo", ""))[-3:] in metas.CODIGOS_MONITORADOS]
    destinos = {}
    for p in cito:
        d = str(p.get("municipio_atendimento") or "?").split(" - ", 1)[-1]
        destinos[d] = destinos.get(d, 0) + (p.get("qtd_anual") or 0)
    meta = sum(destinos.values()) / 12
    return {"meta_mensal": round(meta, 1), "destinos": dict(sorted(destinos.items(), key=lambda d: -d[1])),
            "varginha": _de_varginha(destinos)}


# Basta pactuar qualquer quantidade de citologia com o executor: quem manda algum exame
# para Varginha já trabalha com Varginha (pedido do comercial, 22/10/2026).


def _de_varginha(destinos):
    """A cidade manda alguma citologia para Varginha?"""
    return destinos.get("Varginha", 0) > 0


def _pactua_em(destinos, executor):
    """A cidade manda alguma citologia para esse executor?"""
    return destinos.get(executor, 0) > 0


def executores():
    """Executores que aparecem no PPI das cidades conhecidas, do que mais recebe para o que menos."""
    soma = {}
    for f in fichas().values():
        for d, q in (f.get("destinos") or {}).items():
            soma[d] = soma.get(d, 0) + q
    return sorted(soma, key=lambda d: -soma[d])


def por_executor(executor, so_abaixo=False):
    """Cidades que pactuam a citologia em `executor` (todas as situações ou só abaixo da meta)."""
    return _linhas(lambda l: _pactua_em(l["destinos"], executor)
                   and (not so_abaixo or l["classe"] == "abaixo"))


def _explorar(job, regional=None, maximo=EXPLORAR_POR_RODADA):
    """PPI das cidades em estudo fora das listas. Com `regional`, só as dela (até `maximo`)."""
    todas = municipios_mg()
    if not todas:
        job.escrever("Exploração: sem a lista de cidades de MG (falta o formulário do SISCAN).")
        return 0
    conhecidas = _conhecidas()
    feitas = exploradas()
    limite = datetime.now().timestamp() - REVISAR_EXPLORADA_DIAS * 86400
    cfg = configuracao()
    candidatas = [(cod, nome) for cod, nome in todas.items()
                  if metas.slug_cidade(nome) not in conhecidas
                  and (municipios.por_codigo().get(cod, {}).get("regional") == regional if regional
                       else em_estudo(cod, cfg))]
    # Das mais perto de Varginha para as mais longe.
    candidatas.sort(key=lambda c: municipios.distancia_varginha(c[0]) or 9999)
    novas = [c for c in candidatas if c[0] not in feitas]
    velhas = sorted((c for c in candidatas if c[0] in feitas and feitas[c[0]].get("ts", 0) < limite),
                    key=lambda c: feitas[c[0]].get("ts", 0))
    fila = (novas + velhas)[:maximo]
    if not fila:
        return 0
    onde = ("regional " + regional if regional else
            "MG inteira" if cfg["todas_mg"] else "regional " + ", ".join(cfg["regionais"]))
    job.escrever(f"Estudo ({onde}): PPI de {len(fila)} cidade(s) fora das listas "
                 f"({len(novas)} ainda nunca vistas)...")
    competencia = ppi_engine.competencia_padrao()
    sessao = ppi_engine.nova_sessao()
    job.progresso(0, len(fila))
    feitas_agora = 0
    for i, (cod, nome) in enumerate(fila, 1):
        if job.cancelado:
            break
        try:
            info = _resumir_ppi(_consultar_ppi_0203(sessao, nome, competencia, cod))
            feitas[cod] = dict(info, cidade=nome, competencia=competencia, ts=int(time.time()))
            feitas_agora += 1
        except Exception as e:  # noqa: BLE001 — uma cidade com problema não para o robô
            job.escrever(f"{nome}: não consegui o PPI ({e})")
            sessao = ppi_engine.nova_sessao()
        if i % 10 == 0:
            _gravar(ARQ_EXPLORAR, feitas)
        job.progresso(i, len(fila))
        time.sleep(0.6)  # gentileza com o servidor
    _gravar(ARQ_EXPLORAR, feitas)
    return feitas_agora


def fichas():
    """{código IBGE: ficha do PPI} das cidades exploradas e das conhecidas (cache de PPI das listas).

    Ficha: cidade, meta_mensal (todos os códigos de citologia), destinos {executor: qtd anual}, varginha, competencia."""
    return metas.memo("robo.fichas", _fichas)


def _fichas():
    saida = dict(exploradas())
    for slug, (nome, cod) in metas._cidades_do_ppi().items():
        cache = metas._meta_pelo_cache_ppi(slug)
        if cache:
            saida[cod] = dict(_resumir_ppi(cache["procedimentos"]), cidade=nome,
                              competencia=cache["competencia_ppi"], lista=True, slug=slug)
    return saida


def _linhas(filtro):
    """Uma linha por ficha que passa no filtro, com a produção do ano (SISCAN do estado).

    Para as cidades das listas, os números são os mesmos da página Metas (regra
    da meta e picos atípicos)."""
    estado_meses = metas._meses_estado()
    ano = max((k[0] for k in estado_meses), default=None)
    chaves = sorted(k for k in estado_meses if k[0] == ano)
    da_pagina = {l["slug"]: l for l in metas.situacao()["linhas"]}
    por_cidade = consorcios_mg.por_cidade()
    por_codigo = municipios.por_codigo()
    saida = []
    for cod, e in fichas().items():
        if e.get("meta_mensal", 0) <= 0:
            continue
        qtd = []
        for k in chaves:
            v = estado_meses[k]["municipios"].get(cod)
            qtd.append(metas._numero(v[-1]) if v else 0)
        meta, meta_acum, acumulado = e["meta_mensal"], e["meta_mensal"] * len(qtd), sum(qtd)
        pagina = da_pagina.get(e.get("slug"))
        if pagina:
            meta, meta_acum, acumulado = pagina["meta"], pagina["meta_acumulada"], pagina["acumulado"]
        pct = acumulado / meta_acum * 100 if meta_acum else 0
        linha = {"codigo": cod, "cidade": e["cidade"], "meta": meta, "destinos": e["destinos"],
                 "varginha": e.get("varginha"), "lista": bool(e.get("lista")), "acumulado": acumulado,
                 "meta_acumulada": round(meta_acum), "pct": int(pct), "classe": metas._classe(pct),
                 "ultimo": qtd[-1] if qtd else 0, "slug": e.get("slug") or metas.slug_cidade(e["cidade"]),
                 "consorcios": por_cidade.get(metas.slug_cidade(e["cidade"]), []),
                 "regional": (por_codigo.get(cod) or {}).get("regional", ""),
                 "distancia": municipios.distancia_varginha(cod)}
        if filtro(linha):
            linha["contatos"] = contatos.guardado(cod)
            saida.append(linha)
    saida.sort(key=lambda l: l["distancia"] if l["distancia"] is not None else 9999)
    return saida


def alvos():
    """Alvo principal: já pactuam citologia em Varginha e estão abaixo da meta (das listas ou não)."""
    return _linhas(lambda l: l["varginha"] and l["classe"] == "abaixo" and l["codigo"] != IBGE_VARGINHA)


def da_regional(nome):
    """Todas as cidades da regional com PPI conhecido — qualquer executor, qualquer situação."""
    return _linhas(lambda l: l["regional"] == nome)


def perto_da_meta():
    """Pactuam em Varginha e estão perto da meta (atenção: entre o limite e 99%), da maior % para a menor."""
    linhas = _linhas(lambda l: l["varginha"] and l["classe"] == "atencao" and l["codigo"] != IBGE_VARGINHA)
    return sorted(linhas, key=lambda l: -l["pct"])


def na_meta():
    """Pactuam em Varginha e cumprem a meta (100% ou mais) — os que vão bem."""
    linhas = _linhas(lambda l: l["varginha"] and l["classe"] == "ok" and l["codigo"] != IBGE_VARGINHA)
    return sorted(linhas, key=lambda l: -l["pct"])


def potenciais():
    """Estudo da regional: cidades das regionais em estudo, fora das listas, que pactuam em
    outro lugar e estão abaixo da meta."""
    cfg = configuracao()
    return _linhas(lambda l: not l["varginha"] and not l["lista"] and l["classe"] == "abaixo"
                   and em_estudo(l["codigo"], cfg))


CONTATOS_POR_RODADA = 40


def _sem_contato():
    """Cidades sem contato guardado: alvos primeiro, depois o estudo e as regionais em estudo."""
    cidades = alvos() + pactuam_varginha() + potenciais()
    for reg in configuracao()["regionais"]:
        cidades += da_regional(reg)
    vistos, saida = set(), []
    for p in cidades:
        if p["codigo"] in vistos:
            continue
        vistos.add(p["codigo"])
        g = contatos.guardado(p["codigo"])
        if not g or not g.get("ts"):
            saida.append(p)
    return saida


def _buscar_contatos(job, maximo=CONTATOS_POR_RODADA):
    """Contatos (prefeitura e Secretaria de Saúde) das cidades que ainda não têm (`maximo` None = todas)."""
    faltam = _sem_contato()[:maximo]
    if not faltam:
        job.escrever("Todas as cidades já têm contatos.")
        return
    job.escrever(f"Buscando contatos de {len(faltam)} cidade(s) (alvos primeiro, depois o estudo)...")
    job.progresso(0, len(faltam))
    # Secretarias de Saúde (CNES) todas juntas; a prefeitura (Receita) segue uma a uma.
    sem_cnes = not contatos.cnes_no_ar()
    if sem_cnes:
        job.escrever("A API do CNES (Ministério da Saúde) não está respondendo agora — sem a Secretaria "
                     "de Saúde nesta rodada. Busco só o telefone da prefeitura pelo site; as cidades ficam "
                     "marcadas para tentar o CNES de novo depois.")
    sms = {} if sem_cnes else contatos.secretarias([p["codigo"] for p in faltam])
    for p in faltam:
        if job.cancelado:
            return
        try:
            contatos.buscar(p["codigo"], forcar=True, sms=sms.get(p["codigo"], False), sem_cnes=sem_cnes)
        except Exception as e:  # noqa: BLE001
            job.escrever(f"{p['cidade']}: não consegui os contatos ({e})")
        job.progresso(faltam.index(p) + 1, len(faltam))


def _resumo(s, novos, primeira, exploradas_agora):
    """Frases curtas do que o robô viu nesta rodada."""
    frases = [f"Vigiei {len(s['linhas'])} cidade(s) das listas: {s['n_abaixo']} abaixo da meta, "
              f"{s['n_atencao']} em atenção e {s['n_ok']} na meta."]
    zeradas = [l["cidade"] for l in s["linhas"] if l["mes_zerado"]]
    if zeradas:
        frases.append(f"{len(zeradas)} não mandaram nenhum exame no último mês: " + ", ".join(zeradas[:8])
                      + ("…" if len(zeradas) > 8 else "") + ".")
    piores = [l for l in s["linhas"] if l["classe"] == "abaixo" and not l["mes_zerado"]][:5]
    if piores:
        frases.append("Mais longe da meta: " + ", ".join(f"{l['cidade']} ({l['pct']:.0f}%)" for l in piores) + ".")
    if not primeira:
        if novos:
            tipos = {}
            for a in novos:
                tipos[a["tipo"]] = tipos.get(a["tipo"], 0) + 1
            nomes = {"abaixo": "caíram para abaixo", "zerou": "zeraram", "recuperou": "se recuperaram",
                     "seguidos": "seguem 3+ meses abaixo"}
            frases.append("Desde a última rodada: " + ", ".join(f"{n} {nomes.get(t, t)}" for t, n in tipos.items()) + ".")
        else:
            frases.append("Nada mudou desde a última rodada.")
    alv = alvos()
    if alv:
        frases.append(f"Alvos — {len(alv)} cidade(s) pactuam em Varginha e estão abaixo da meta: "
                      + ", ".join(f"{a['cidade']} ({a['pct']}%)" for a in alv[:8]) + ("…" if len(alv) > 8 else "") + ".")
    cfg = configuracao()
    if cfg["todas_mg"] or cfg["regionais"]:
        onde = "MG inteira" if cfg["todas_mg"] else "regional " + ", ".join(cfg["regionais"])
        pot = potenciais()
        frases.append(f"Estudo ({onde}): {exploradas_agora} cidade(s) nova(s) analisada(s) hoje; "
                      f"{len(pot)} pactuam em outro lugar e estão abaixo da meta"
                      + (": " + ", ".join(f"{p['cidade']} ({p['distancia']} km)" for p in pot[:5]) if pot else "") + ".")
    return {"quando": datetime.now().strftime("%d/%m/%Y %H:%M"), "frases": frases}


def executar(job):
    if consorcios_mg.vencido():
        try:
            consorcios_mg.atualizar(job.escrever)
        except Exception as e:  # noqa: BLE001 — segue com a lista guardada
            job.escrever(f"Aviso: não consegui atualizar os consórcios do COSECS-MG ({e}).")
    cidades = cidades_vigiadas()
    job.escrever(f"Vigiando {len(cidades)} cidade(s) de {len(configuracao()['listas'])} lista(s).")
    try:
        _atualizar_siscan(job)
    except Exception as e:  # noqa: BLE001 — segue com os meses que já estão no cache
        job.escrever(f"Aviso: não consegui atualizar o SISCAN ({e}); uso os meses guardados.")
    if job.cancelado:
        return {}
    _buscar_metas(job, cidades)
    if job.cancelado:
        return {}

    s = metas.situacao(cidades)
    anterior = estado()
    agora = _foto(s["linhas"])
    primeira = not anterior.get("foto")
    novos = [] if primeira else _comparar(anterior["foto"], agora)
    alertas = (novos + anterior.get("alertas", []))[:MAX_ALERTAS]
    try:
        _descobrir_varginha(job)
    except Exception as e:  # noqa: BLE001 — extra; o vigia não pode falhar por ela
        job.escrever(f"Aviso: não consegui a lista de quem pactua em Varginha ({e}).")
    if job.cancelado:
        return {}
    try:
        n_explorado = _explorar(job)
    except Exception as e:  # noqa: BLE001 — a exploração é extra; o vigia não pode falhar por ela
        job.escrever(f"Aviso: exploração interrompida ({e}).")
        n_explorado = 0
    if job.cancelado:
        return {}
    _buscar_contatos(job)
    if job.cancelado:
        return {}
    resumo = _resumo(s, novos, primeira, n_explorado)
    _gravar(ARQ_ESTADO, {"ultima_rodada": datetime.now().isoformat(timespec="seconds"),
                         "foto": agora, "alertas": alertas, "resumo": resumo})
    job.escrever("Resumo:")
    for f in resumo["frases"]:
        job.escrever("  " + f)
    job.escrever(f"{s['n_abaixo']} abaixo da meta, {s['n_atencao']} em atenção, {s['n_ok']} ok.")
    if primeira:
        job.escrever("Primeira rodada: situação guardada. Os alertas começam na próxima.")
    else:
        job.escrever(f"{len(novos)} alerta(s) novo(s).")
        for a in novos:
            job.escrever(f"  {a['cidade']}: {a['texto']}")
    _ao_terminar(job)
    if s["sem_siscan"]:
        job.escrever(f"Sem dados ({len(s['sem_siscan'])}): " + ", ".join(s["sem_siscan"]))
    return {"abaixo": s["n_abaixo"], "atencao": s["n_atencao"], "ok": s["n_ok"], "alertas": len(novos)}


# --- todas as cidades que pactuam em Varginha -------------------------------------
# O PPI consultado "ao contrário" (Atendimento = Varginha, por município de origem, como
# no Teto PPI) dá a lista de quem manda exame do 0203 para Varginha. O robô guarda essa
# lista e consulta o PPI completo das que ainda não conhece, para saber a parte de cada uma.

ARQ_VARGINHA = PASTA / "pactuam_varginha.json"
REVISAR_VARGINHA_DIAS = 7


def codigos_varginha():
    return set((_ler(ARQ_VARGINHA, {}) or {}).get("codigos", []))


def pactuam_varginha():
    """Todas as cidades que mandam citologia para Varginha, em qualquer situação."""
    cods = codigos_varginha()
    return _linhas(lambda l: l["codigo"] in cods and l["codigo"] != IBGE_VARGINHA
                   and l["destinos"].get("Varginha", 0) > 0)


def _descobrir_varginha(job, forcar=False):
    salvo = _ler(ARQ_VARGINHA, {}) or {}
    if not forcar and salvo.get("ts", 0) > time.time() - REVISAR_VARGINHA_DIAS * 86400:
        return 0
    from core import teto_ppi
    job.escrever("Buscando no PPI todas as cidades que pactuam em Varginha...")
    linhas, _ = teto_ppi.Consulta(ppi_engine.competencia_padrao()).consultar("0203")
    cods = [l[0][:6] for l in linhas]
    _gravar(ARQ_VARGINHA, {"ts": int(time.time()), "codigos": cods})
    por_codigo = municipios.por_codigo()
    n = _completar_fichas(job, [por_codigo[c] for c in cods if c in por_codigo])
    job.escrever(f"{len(cods)} cidade(s) mandam exames do 0203 para Varginha; {n} consultada(s) agora.")
    return n


def descobrir_varginha(job):
    """Botão: refaz a lista de quem pactua em Varginha, completa o PPI e os contatos."""
    _descobrir_varginha(job, forcar=True)
    if job.cancelado:
        return {}
    _buscar_contatos(job, maximo=None)
    lista = pactuam_varginha()
    abaixo = sum(1 for l in lista if l["classe"] == "abaixo")
    job.escrever(f"Pactuam citologia em Varginha: {len(lista)} cidade(s), {abaixo} abaixo da meta.")
    _ao_terminar(job)
    return {"cidades": len(lista), "abaixo": abaixo}


# --- estudo de uma regional, a partir de uma cidade ---------------------------------

def regional_da_cidade(nome):
    """(município, nome da regional) da cidade digitada, ou (None, None) se não achar."""
    m = municipios.resolver(nome or "")
    return (m, m.get("regional")) if m and m.get("regional") else (m, None)


def _completar_fichas(job, cidades):
    """Cidades da regional que estão nas listas mas sem ficha do PPI (a meta veio só do
    relatório SISCAN, sem os procedimentos): consulta o PPI para saber onde pactuam."""
    tem = fichas()
    faltam = [m for m in cidades if m["codigo"] not in tem]
    if not faltam:
        return 0
    job.escrever(f"PPI de {len(faltam)} cidade(s) das listas ainda sem ficha: "
                 + ", ".join(m["nome"] for m in faltam))
    feitas = exploradas()
    competencia = ppi_engine.competencia_padrao()
    sessao = ppi_engine.nova_sessao()
    conhecidas = _conhecidas()
    n = 0
    job.progresso(0, len(faltam))
    for i, m in enumerate(faltam, 1):
        if job.cancelado:
            break
        job.progresso(i, len(faltam))
        try:
            info = _resumir_ppi(_consultar_ppi_0203(sessao, m["nome"], competencia, m["codigo"]))
            slug = metas.slug_cidade(m["nome"])
            feitas[m["codigo"]] = dict(info, cidade=m["nome"], competencia=competencia, ts=int(time.time()),
                                       slug=slug, lista=slug in conhecidas)
            n += 1
        except Exception as e:  # noqa: BLE001
            job.escrever(f"{m['nome']}: não consegui o PPI ({e})")
            sessao = ppi_engine.nova_sessao()
        time.sleep(0.6)
    _gravar(ARQ_EXPLORAR, feitas)
    return n


def estudar_regional(job, regional):
    """Põe a regional em estudo e consulta, de uma vez, o PPI de todas as cidades dela."""
    cfg = configuracao()
    if regional not in cfg["regionais"]:
        salvar_configuracao(cfg["listas"], cfg["consorcios_mg"], cfg["regionais"] + [regional])
        job.escrever(f"Regional {regional} entrou no estudo do robô.")
    cidades = [m for m in municipios.todos() if m.get("regional") == regional]
    job.escrever(f"Regional {regional}: {len(cidades)} cidade(s): " + ", ".join(m["nome"] for m in cidades))
    try:
        _atualizar_siscan(job)
    except Exception as e:  # noqa: BLE001 — segue com os meses que já estão no cache
        job.escrever(f"Aviso: não consegui atualizar o SISCAN ({e}); uso os meses guardados.")
    if job.cancelado:
        return {}
    n = _explorar(job, regional=regional, maximo=len(cidades))
    if job.cancelado:
        return {}
    n += _completar_fichas(job, cidades)
    if job.cancelado:
        return {}
    _buscar_contatos(job, maximo=None)
    linhas = da_regional(regional)
    abaixo = [l for l in linhas if l["classe"] == "abaixo"]
    job.escrever(f"Estudo pronto: {n} cidade(s) consultada(s) agora; {len(linhas)} com PPI e SISCAN, "
                 f"{len(abaixo)} abaixo da meta.")
    for l in abaixo:
        job.escrever(f"  {l['cidade']}: {l['pct']:.0f}% da meta")
    _ao_terminar(job)
    return {"regional": regional, "cidades": len(linhas), "abaixo": len(abaixo)}


def completar_contatos(job):
    """Busca, sem o limite por rodada, os contatos de todas as cidades que ainda não têm."""
    _buscar_contatos(job, maximo=None)
    faltam = _sem_contato()
    job.escrever(f"Ainda sem contato: {len(faltam)}" + (" — " + ", ".join(p["cidade"] for p in faltam)
                                                         if faltam else "."))
    _ao_terminar(job)
    return {"sem_contato": len(faltam)}


# --- agendamento --------------------------------------------------------------------

TITULO = "Robô de metas — vigiar cidades da região"


def iniciar_agendador(gerenciador):
    """Confere a cada 30 min se já é hora de rodar (e se não há uma rodada na fila)."""
    def laco():
        time.sleep(60)  # deixa a Central terminar de subir
        while True:
            try:
                if deve_rodar() and not any(j.tipo == "robo" for j in gerenciador.ativos()):
                    gerenciador.enfileirar("robo", TITULO, executar)
            except Exception:  # noqa: BLE001 — o agendador nunca pode morrer
                pass
            time.sleep(30 * 60)

    threading.Thread(target=laco, daemon=True, name="robo-metas").start()
