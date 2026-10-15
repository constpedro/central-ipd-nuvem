"""
Situação das metas — produção do SISCAN (citologia) contra a meta mensal do PPI.

Lê só os caches que os relatórios SISCAN + PPI já deixam em data/cache:
  siscan/resultados/<ano>/<cidade>.json  produção mês a mês (coluna Total)
  siscan/metas/<cidade>.json             meta mensal gravada pelo gerar_siscan
  ppi/<competencia>/<cidade>.json        alternativa para a meta (cache dos consórcios)
Não consulta a internet: uma cidade aparece aqui depois do primeiro relatório SISCAN.
"""
import json
import re
import unicodedata

import config

# Mesma regra do relatório: códigos 019 + 086 do subgrupo 0203, qtd anual ÷ 12.
CODIGOS_META = {"019", "086"}
LIMITE_ATENCAO = 80   # % — abaixo disso é "abaixo da meta"; entre isso e 100 é "atenção"
MESES = ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", "JULHO",
         "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO"]


def _sem_acento(txt):
    return "".join(c for c in unicodedata.normalize("NFD", txt) if unicodedata.category(c) != "Mn")


_MES_NUM = {_sem_acento(m): i + 1 for i, m in enumerate(MESES)}


def slug_cidade(nome):
    """Mesmo slug dos arquivos de cache do gerar_ppi ('São Lourenço' -> 'sao_lourenco')."""
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _ler(caminho):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _numero(texto):
    limpo = str(texto).strip().replace(".", "")
    return int(limpo) if limpo.isdigit() else 0


def _meses_siscan(dados):
    """{(ano, mes): quantidade} a partir das linhas 'AGOSTO/2026' do TabNet."""
    saida = {}
    for linha in dados.get("linhas", []):
        rotulo = _sem_acento(linha.get("label", "").strip().upper())
        if "/" not in rotulo or not linha.get("valores"):
            continue
        nome, ano = rotulo.split("/", 1)
        if nome in _MES_NUM and ano.isdigit():
            saida[(int(ano), _MES_NUM[nome])] = _numero(linha["valores"][-1])
    return saida


# Faixas de prazo do resultado (colunas "Interv Resultado" do TabNet). O TabNet
# omite a coluna de uma faixa quando ela está zerada no ano, por isso o casamento
# é pelo nome da coluna, não pela posição.
FAIXAS = [("0-10", "Até 10 dias"), ("11-20", "11 a 20 dias"), ("21-30", "21 a 30 dias"), (">30", "Mais de 30 dias")]


def _chaves_faixa(colunas):
    return [c.replace(" ", "").replace("dias", "") for c in colunas or []]


def _faixas_siscan(dados):
    """{(ano, mes): {"0-10": n, "11-20": n, ...}} a partir das colunas do TabNet."""
    chaves = _chaves_faixa(dados.get("colunas"))
    saida = {}
    for linha in dados.get("linhas", []):
        rotulo = _sem_acento(linha.get("label", "").strip().upper())
        if "/" not in rotulo:
            continue
        nome, ano = rotulo.split("/", 1)
        if nome in _MES_NUM and ano.isdigit():
            valores = dict(zip(chaves, linha.get("valores", [])))
            saida[(int(ano), _MES_NUM[nome])] = {k: _numero(valores.get(k, 0)) for k, _ in FAIXAS}
    return saida


def _meses_estado():
    """{(ano, mes): dados} das consultas do SISCAN com o estado inteiro, um mês por
    arquivo (siscan/meses/AAAAMM.json, gravado pelo gerar_siscan).

    Na consulta por município o TabNet simplesmente omite o mês sem nenhum exame.
    Sem esta referência, uma cidade que zerou a produção aparecia comparando um
    mês antigo (ex.: Março) e podia até ficar "ok". Aqui, mês publicado em que a
    cidade não aparece vale 0."""
    saida = {}
    pasta = config.CACHE_DIR / "siscan" / "meses"
    if pasta.is_dir():
        for arq in pasta.glob("*.json"):
            if not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", arq.stem):
                continue
            dados = _ler(arq)
            if dados and dados.get("municipios"):
                saida[(int(arq.stem[:4]), int(arq.stem[4:]))] = dados
    return saida


def _completar_com_estado(c, codigo, estado, faixas=None):
    """Sobrepõe os meses da cidade `c` com os do estado (mais recentes e com zeros)."""
    if not codigo or not estado:
        return
    for chave, dados in estado.items():
        valores = dados["municipios"].get(str(codigo))
        c["meses"][chave] = _numero(valores[-1]) if valores else 0
        if faixas is not None:
            por_coluna = dict(zip(_chaves_faixa(dados.get("colunas")), valores or []))
            faixas[chave] = {k: _numero(por_coluna.get(k, 0)) for k, _ in FAIXAS}
    if c["meses"] and max(c["meses"]) in estado:
        ultimo = estado[max(c["meses"])]
        c["atualizacao"] = ultimo.get("atualizacao") or c["atualizacao"]
        c["consultado_em"] = ultimo.get("consultado_em") or c["consultado_em"]


# Os 7 códigos de citologia do relatório SISCAN + PPI (subgrupo 0203).
CODIGOS_MONITORADOS = {"019", "022", "043", "065", "073", "081", "086"}


def _meta_pelo_cache_ppi(slug):
    """Meta a partir do cache de PPI da competência mais recente que tiver a cidade."""
    base = config.CACHE_DIR / "ppi"
    if not base.is_dir():
        return None
    for pasta in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        dados = _ler(pasta / (slug + ".json"))
        if not dados:
            continue
        procs = dados.get("subgrupos", {}).get("0203", {}).get("procedimentos", [])
        meta = sum(p.get("qtd_anual", 0) for p in procs if str(p.get("codigo", ""))[-3:] in CODIGOS_META) / 12
        if meta > 0:
            procs = [dict(p, codigo=str(p["codigo"])[-3:]) for p in procs
                     if str(p.get("codigo", ""))[-3:] in CODIGOS_MONITORADOS]
            return {"meta_mensal": meta, "competencia_ppi": pasta.name, "procedimentos": procs}
    return None


# Regra da meta por município: "019_086" (padrão, igual ao relatório) ou "todos"
# (soma dos 7 códigos monitorados). Guardada em data/metas_regras.json.
REGRAS = {"019_086": "Só 019 + 086", "todos": "Todos os 7 códigos"}
REGRA_PADRAO = "019_086"
ARQ_REGRAS = config.DATA_DIR / "metas_regras.json"


def regras():
    return _ler(ARQ_REGRAS) or {}


def salvar_regra(slug, regra):
    if regra not in REGRAS:
        raise ValueError("regra inválida")
    atuais = regras()
    if regra == REGRA_PADRAO:
        atuais.pop(slug, None)
    else:
        atuais[slug] = regra
    ARQ_REGRAS.write_text(json.dumps(atuais, ensure_ascii=False, indent=1), encoding="utf-8")


def _procedimentos(slug, gravada):
    """Os 7 códigos do PPI 0203 da cidade (codigo = final de 3 dígitos)."""
    procs = gravada.get("procedimentos") or (_meta_pelo_cache_ppi(slug) or {}).get("procedimentos") or []
    return [dict(p, codigo=str(p["codigo"])[-3:]) for p in procs]


def _meta_por_regra(procs, regra):
    codigos = CODIGOS_MONITORADOS if regra == "todos" else CODIGOS_META
    return sum(p.get("qtd_anual") or 0 for p in procs if p["codigo"] in codigos) / 12


# Pico atípico: mês muito acima do normal da própria cidade, típico de exames
# represados lançados de uma vez no SISCAN (ex.: Lagamar, jul/2026: 621 exames,
# 570 com mais de 30 dias, contra ~20/mês). Fica fora do acumulado e da média.
PICO_X_MEDIANA = 5
PICO_X_META = 5


def _picos(meses, meta):
    """Chaves (ano, mes) dos meses atípicos: > 5× a mediana dos outros meses e > 5× a meta."""
    picos = set()
    for chave, qtd in meses.items():
        outros = sorted(v for k, v in meses.items() if k != chave)
        if len(outros) < 3:
            continue
        meio = len(outros) // 2
        mediana = outros[meio] if len(outros) % 2 else (outros[meio - 1] + outros[meio]) / 2
        if qtd > PICO_X_MEDIANA * max(mediana, 1) and qtd > PICO_X_META * meta:
            picos.add(chave)
    return picos


def _classe(pct):
    if pct >= 100:
        return "ok"
    return "atencao" if pct >= LIMITE_ATENCAO else "abaixo"


def situacao(cidades=None):
    """Uma linha por município com SISCAN em cache, do pior para o melhor.

    Com `cidades` (ex.: os municípios de um consórcio), mostra só esses e lista
    em `sem_siscan` os que ainda não têm SISCAN consultado.
    """
    filtro = {slug_cidade(c): c for c in cidades} if cidades is not None else None
    base = config.CACHE_DIR / "siscan"
    resultados = base / "resultados"
    cidades = {}  # slug -> {"cidade", "meses", "atualizacao"}
    if resultados.is_dir():
        for arq in sorted(resultados.glob("*/*.json")):
            pacote = _ler(arq)
            if not pacote or not isinstance(pacote.get("dados"), dict):
                continue
            c = cidades.setdefault(arq.stem, {"cidade": pacote.get("cidade") or arq.stem, "meses": {},
                                              "atualizacao": "", "consultado_em": "", "codigo": ""})
            c["meses"].update(_meses_siscan(pacote["dados"]))
            c["atualizacao"] = pacote["dados"].get("atualizacao") or c["atualizacao"]
            c["consultado_em"] = pacote.get("consultado_em") or c["consultado_em"]
            c["codigo"] = str(pacote.get("codigo_ibge") or c["codigo"])

    if filtro is not None:
        cidades = {k: v for k, v in cidades.items() if k in filtro}
    estado = _meses_estado()
    for c in cidades.values():
        _completar_com_estado(c, c["codigo"], estado)
    linhas, sem_meta = [], []
    regras_salvas = regras()
    for slug, c in cidades.items():
        if not c["meses"]:
            continue
        gravada = _ler(base / "metas" / (slug + ".json")) or {}
        fonte = gravada if gravada.get("meta_mensal") else _meta_pelo_cache_ppi(slug)
        if not fonte:
            sem_meta.append(c["cidade"])
            continue
        regra = regras_salvas.get(slug, REGRA_PADRAO)
        procs = _procedimentos(slug, gravada)
        # Sem os procedimentos guardados só dá para usar a meta 019 + 086 gravada.
        meta = _meta_por_regra(procs, regra) if procs else 0
        if meta <= 0:
            meta, regra = float(fonte["meta_mensal"]), REGRA_PADRAO
        ordem = sorted(c["meses"])
        ultimo = ordem[-1]
        qtd = c["meses"][ultimo]
        pct_mes = qtd / meta * 100
        # A situação é pelo acumulado do ano mais recente: meses acima da meta
        # compensam os de baixo (e um mês ainda não digitado não derruba a cidade).
        picos = _picos(c["meses"], meta)
        do_ano = [c["meses"][k] for k in ordem if k[0] == ultimo[0] and k not in picos]
        acumulado, meta_acumulada = sum(do_ano), meta * len(do_ano)
        pct = acumulado / meta_acumulada * 100 if meta_acumulada else 0
        # Meses seguidos abaixo de 100%, contando do mais recente para trás.
        seguidos = 0
        for chave in reversed(ordem):
            if c["meses"][chave] >= meta:
                break
            seguidos += 1
        historico = [{"mes": f"{MESES[m - 1][:3].capitalize()}/{str(a)[2:]}", "qtd": c["meses"][(a, m)],
                      "pct": round(c["meses"][(a, m)] / meta * 100), "classe": _classe(c["meses"][(a, m)] / meta * 100),
                      "pico": (a, m) in picos}
                     for a, m in ordem[-6:]]
        linhas.append({
            "slug": slug,
            "cidade": c["cidade"],
            "regra": regra,
            "regra_nome": REGRAS[regra],
            "regra_escolhida": regras_salvas.get(slug, REGRA_PADRAO),
            "tem_procedimentos": bool(procs),
            "mes": f"{MESES[ultimo[1] - 1].capitalize()}/{ultimo[0]}",
            "qtd": qtd,
            "meta": meta,
            "pct": pct,
            "classe": _classe(pct),
            "pct_mes": pct_mes,
            "classe_mes": _classe(pct_mes),
            "mes_zerado": qtd == 0,
            "ano": ultimo[0],
            "meses_ano": len(do_ano),
            "acumulado": acumulado,
            "meta_acumulada": meta_acumulada,
            "seguidos": seguidos,
            "picos": [f"{MESES[m - 1][:3].capitalize()}/{str(a)[2:]} ({c['meses'][(a, m)]})" for a, m in sorted(picos)],
            "picos_qtd": sum(c["meses"][k] for k in picos),
            "historico": historico,
            "competencia_ppi": fonte.get("competencia_ppi", ""),
            "atualizacao": c["atualizacao"],
            "consultado_em": c["consultado_em"],
        })
    linhas.sort(key=lambda l: (l["pct"], l["cidade"]))
    return {
        "linhas": linhas,
        "sem_meta": sorted(sem_meta),
        "sem_siscan": [nome for k, nome in (filtro or {}).items() if k not in cidades],
        "n_abaixo": sum(l["classe"] == "abaixo" for l in linhas),
        "n_atencao": sum(l["classe"] == "atencao" for l in linhas),
        "n_ok": sum(l["classe"] == "ok" for l in linhas),
        "limite_atencao": LIMITE_ATENCAO,
    }


def detalhe(slug):
    """Dados da página do município: todos os meses, meta, PPI e PDFs já gerados."""
    linha = next((l for l in situacao()["linhas"] if l["slug"] == slug), None)
    if linha is None:
        return None
    base = config.CACHE_DIR / "siscan"
    meses, faixas, codigo = {}, {}, ""
    for arq in sorted((base / "resultados").glob("*/" + slug + ".json")):
        pacote = _ler(arq) or {}
        meses.update(_meses_siscan(pacote.get("dados") or {}))
        faixas.update(_faixas_siscan(pacote.get("dados") or {}))
        codigo = str(pacote.get("codigo_ibge") or codigo)
    _completar_com_estado({"meses": meses, "atualizacao": "", "consultado_em": ""}, codigo,
                          _meses_estado(), faixas)
    meta = linha["meta"]
    serie = [{"mes": f"{MESES[m - 1][:3].capitalize()}/{str(a)[2:]}", "qtd": meses[(a, m)],
              "pct": round(meses[(a, m)] / meta * 100), "classe": _classe(meses[(a, m)] / meta * 100),
              "faixas": faixas.get((a, m), {}), "pico": (a, m) in _picos(meses, meta)}
             for a, m in sorted(meses)]

    gravada = _ler(base / "metas" / (slug + ".json")) or {}
    procs = sorted(_procedimentos(slug, gravada), key=lambda p: p["codigo"])
    for p in procs:
        p["na_meta"] = linha["regra"] == "todos" or p["codigo"] in CODIGOS_META

    normais = [s["qtd"] for s in serie if not s["pico"]] or [0]
    media = sum(normais) / len(normais)
    pdfs = []
    if config.SAIDA_DIR.is_dir():
        pdfs = sorted((a.name for a in config.SAIDA_DIR.glob("*.pdf")
                       if a.name.startswith(("siscan_" + slug + "_", "ppi_" + slug + "_"))), reverse=True)
    totais_faixa = {k: sum(s["faixas"].get(k, 0) for s in serie) for k, _ in FAIXAS}
    return dict(linha, regras=REGRAS, serie=serie, faixas=[{"chave": k, "nome": n, "total": totais_faixa[k]} for k, n in FAIXAS], procedimentos=procs, pdfs=pdfs, media=media,
                pct_media=media / meta * 100,
                total=sum(s["qtd"] for s in serie), meta_periodo=meta * len(serie))
