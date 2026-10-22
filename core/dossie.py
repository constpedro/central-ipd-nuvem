"""
Dossiê de prospecção de um município com potencial (PDF).

Junta numa folha: contatos (prefeitura e Secretaria de Saúde), a produção de
citologia mês a mês contra a meta do PPI (gráfico), o prazo dos laudos e os
argumentos para a conversa. Os dados vêm do robô (robo/explorar.json), dos meses
do SISCAN do estado e de core/contatos.py.
"""
from datetime import datetime
from io import BytesIO

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing, Line, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from core import consorcios_mg, contatos, metas, robo

COR = colors.HexColor("#b4233c")
COR_OK = colors.HexColor("#2f8a57")
COR_ATENCAO = colors.HexColor("#c98a12")
CINZA = colors.HexColor("#6b6b6b")
ABREV = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def dados(codigo, fichas=None, estado=None, buscar_contatos=True):
    """Tudo o que o dossiê precisa de um município (das listas ou explorado pelo robô).

    buscar_contatos=False usa só os contatos já guardados (dossiê de várias cidades:
    a busca online, com o limite da Receita, fica com o robô)."""
    e = (fichas if fichas is not None else robo.fichas()).get(codigo)
    if not e:
        raise KeyError(codigo)
    estado = estado if estado is not None else metas._meses_estado()
    ano = max((k[0] for k in estado), default=datetime.now().year)
    meses = []
    for chave in sorted(k for k in estado if k[0] == ano):
        d = estado[chave]
        valores = d["municipios"].get(codigo)
        faixas = dict(zip(metas._chaves_faixa(d.get("colunas")), valores or []))
        meses.append({"mes": ABREV[chave[1] - 1], "qtd": metas._numero(valores[-1]) if valores else 0,
                      "mais30": metas._numero(faixas.get(">30", 0))})
    meta = e["meta_mensal"]
    acumulado = sum(m["qtd"] for m in meses)
    meta_acum = meta * len(meses)
    cont = contatos.guardado(codigo) or {}
    if buscar_contatos:
        try:
            cont = contatos.buscar(codigo)
        except Exception:  # noqa: BLE001 — sem internet o dossiê sai com o que estiver guardado
            pass
    return {"codigo": codigo, "cidade": e["cidade"], "meta": meta, "destinos": e["destinos"],
            "varginha": e.get("varginha"), "ano": ano, "meses": meses, "acumulado": acumulado,
            "meta_acumulada": meta_acum, "pct": acumulado / meta_acum * 100 if meta_acum else 0,
            "contatos": cont, "competencia_ppi": e.get("competencia", ""),
            "consorcios": [c for c in sorted(consorcios_mg.todos(), key=consorcios_mg.samu)
                           if metas.slug_cidade(e["cidade"]) in {metas.slug_cidade(x) for x in c["cidades"]}]}


def argumentos(d):
    """Frases para a conversa, a partir dos números da cidade."""
    args = []
    falta = round(d["meta_acumulada"] - d["acumulado"])
    pct = d["pct"]
    if falta > 0:
        args.append(f"<b>Déficit de {falta} exames em {d['ano']}.</b> A cidade fez {d['acumulado']} dos "
                    f"{d['meta_acumulada']:.0f} preventivos pactuados no PPI ({int(pct)}%). Cada exame que falta é "
                    f"uma mulher sem rastreamento do câncer do colo do útero — e recurso pactuado sem uso.")
    else:
        args.append(f"<b>Cumpre a meta ({int(pct)}%).</b> {d['acumulado']} exames de {d['meta_acumulada']:.0f} pactuados "
                    f"em {d['ano']}.")
    zerados = [m["mes"] for m in d["meses"] if m["qtd"] == 0]
    if zerados:
        args.append(f"<b>{len(zerados)} mês(es) sem nenhum exame no SISCAN</b> ({', '.join(zerados)}). Mês zerado "
                    f"costuma ser falha de coleta, de envio ou de digitação no SISCAN — gestão do município, não "
                    f"necessariamente do laboratório. Oferecer apoio na organização do fluxo é uma porta de entrada.")
    total = sum(m["qtd"] for m in d["meses"])
    mais30 = sum(m["mais30"] for m in d["meses"])
    if total:
        p30 = mais30 / total * 100
        if p30 >= 20:
            args.append(f"<b>{p30:.0f}% dos laudos saíram com mais de 30 dias</b> ({mais30} de {total}). Prazo longo "
                        f"atrasa o tratamento e desestimula a coleta; o prazo do laboratório é um argumento direto.")
        else:
            args.append(f"<b>Prazo dos laudos em dia</b> (só {p30:.0f}% com mais de 30 dias). O gargalo provável está "
                        f"na coleta e na busca ativa das mulheres — foco da conversa com a Secretaria de Saúde.")
    if len(d["meses"]) >= 6:
        rec = sum(m["qtd"] for m in d["meses"][-3:]) / 3
        ant = sum(m["qtd"] for m in d["meses"][-6:-3]) / 3
        if ant and rec < ant * 0.8:
            args.append(f"<b>Produção em queda:</b> média de {rec:.0f}/mês nos últimos 3 meses contra {ant:.0f}/mês "
                        f"nos 3 anteriores.")
        elif ant and rec > ant * 1.2:
            args.append(f"<b>Produção em alta:</b> {rec:.0f}/mês nos últimos 3 meses contra {ant:.0f}/mês antes — "
                        f"a cidade está se mexendo; bom momento para abordar.")
    destinos = ", ".join(d["destinos"])
    if d["varginha"]:
        args.append(f"<b>Já pactua citologia em Varginha</b> ({destinos}). O exame já tem destino certo: o que falta "
                    f"é a cidade coletar e enviar. Cada exame abaixo da meta é recurso pactuado parado — a conversa é "
                    f"sobre busca ativa, agenda de coleta e envio regular, e a Central acompanha mês a mês.")
    else:
        args.append(f"<b>Hoje pactua em {destinos}.</b> A troca de executor passa pela repactuação do PPI "
                    f"(gestão municipal + CIB); a meta de {d['meta']:.0f} exames/mês é o volume em jogo.")
    km = (d["contatos"] or {}).get("distancia_km")
    if km is not None:
        args.append(f"<b>Distância de Varginha:</b> cerca de {km} km em linha reta.")
    return args


def _grafico(d):
    larg, alt = 17 * cm, 6 * cm
    g = Drawing(larg, alt)
    ch = VerticalBarChart()
    ch.x, ch.y, ch.width, ch.height = 30, 25, larg - 40, alt - 45
    ch.data = [[m["qtd"] for m in d["meses"]] or [0]]
    ch.categoryAxis.categoryNames = [m["mes"] for m in d["meses"]] or ["—"]
    ch.categoryAxis.labels.fontSize = 8
    ch.valueAxis.labels.fontSize = 8
    ch.valueAxis.valueMin = 0
    topo = max(ch.data[0] + [d["meta"]]) * 1.15 or 10
    ch.valueAxis.valueMax = topo
    ch.bars.strokeColor = None
    for i, m in enumerate(d["meses"]):
        cor = COR_OK if m["qtd"] >= d["meta"] else COR_ATENCAO if m["qtd"] >= d["meta"] * metas.LIMITE_ATENCAO / 100 else COR
        ch.bars[(0, i)].fillColor = cor
    g.add(ch)
    y = ch.y + d["meta"] / topo * ch.height
    g.add(Line(ch.x, y, ch.x + ch.width, y, strokeColor=colors.black, strokeDashArray=[4, 3], strokeWidth=1))
    g.add(String(ch.x + ch.width - 2, y + 3, f"meta {d['meta']:.0f}/mês", fontSize=8, textAnchor="end"))
    g.add(String(ch.x, alt - 12, f"Preventivos por mês em {d['ano']} (SISCAN)", fontSize=9, fontName="Helvetica-Bold"))
    return g


def _estilos():
    base = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle("t", parent=base["Title"], fontSize=17, alignment=0, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=base["Normal"], fontSize=9, textColor=CINZA),
        "h": ParagraphStyle("h", parent=base["Heading3"], fontSize=11, spaceBefore=10, spaceAfter=4, textColor=COR),
        "p": ParagraphStyle("p", parent=base["Normal"], fontSize=9.5, leading=13),
        "li": ParagraphStyle("li", parent=base["Normal"], fontSize=9.5, leading=13, leftIndent=10, bulletIndent=0,
                             spaceAfter=3),
    }


def _contatos(d, st):
    c = d["contatos"] or {}
    sms, pref = c.get("secretaria") or {}, c.get("prefeitura") or {}
    linhas = [["", "Telefone", "E-mail", "Endereço"]]
    linhas.append([Paragraph("<b>Secretaria de Saúde</b>", st["p"]), sms.get("telefone") or "—",
                   Paragraph(sms.get("email") or "—", st["p"]), Paragraph(sms.get("endereco") or "—", st["p"])])
    linhas.append([Paragraph("<b>Prefeitura</b>", st["p"]), Paragraph("<br/>".join(pref.get("telefones") or ["—"]), st["p"]),
                   Paragraph(pref.get("email") or "—", st["p"]), Paragraph(pref.get("endereco") or "—", st["p"])])
    for cons in d["consorcios"]:
        linhas.append([Paragraph(f"<b>Consórcio {cons['sigla']}</b>", st["p"]), Paragraph(cons.get("telefone") or "—", st["p"]),
                       Paragraph(cons.get("site") or "—", st["p"]), Paragraph(f"Sede: {cons.get('sede') or '—'}", st["p"])])
    t = Table(linhas, colWidths=[3.4 * cm, 3.2 * cm, 4.6 * cm, 5.8 * cm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("TEXTCOLOR", (0, 0), (-1, 0), CINZA),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.lightgrey)]))
    return t


def _pagina(d, st):
    pct = d["pct"]
    situacao = "Na meta" if pct >= 100 else "Atenção" if pct >= metas.LIMITE_ATENCAO else "Abaixo da meta"
    kpis = Table([["Meta/mês (PPI)", f"Acumulado {d['ano']}", "% da meta", "Situação"],
                  [f"{d['meta']:.0f}", f"{d['acumulado']} de {d['meta_acumulada']:.0f}", f"{int(pct)}%", situacao]],
                 colWidths=[4.25 * cm] * 4)
    kpis.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, 0), 8), ("TEXTCOLOR", (0, 0), (-1, 0), CINZA),
                              ("FONTSIZE", (0, 1), (-1, 1), 13), ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                              ("BOX", (0, 0), (-1, -1), 0.4, colors.lightgrey), ("TOPPADDING", (0, 1), (-1, 1), 4)]))
    hoje = datetime.now().strftime("%d/%m/%Y")
    comp = d["competencia_ppi"]
    return [
        Paragraph(f"{d['cidade']} — dossiê de prospecção", st["titulo"]),
        Paragraph(f"Central IPD · gerado em {hoje} · IBGE {d['codigo']} · PPI {comp[4:]}/{comp[:4]} · "
                  f"pactua em {', '.join(d['destinos'])}"
                  + (f" · consórcio {', '.join(c['sigla'] for c in d['consorcios'])}" if d["consorcios"] else ""), st["sub"]),
        Spacer(1, 8), kpis,
        Paragraph("Contatos", st["h"]), _contatos(d, st),
        Paragraph("Produção x meta", st["h"]), _grafico(d),
        KeepTogether([Paragraph("Argumentos para a conversa", st["h"])]
                     + [Paragraph(a, st["li"], bulletText="•") for a in argumentos(d)]),
        KeepTogether([Paragraph("Roteiro de abordagem", st["h"])] + [Paragraph(a, st["li"], bulletText=f"{i}.") for i, a in enumerate([
            "Ligar primeiro para a Secretaria de Saúde (coordenação da Atenção Primária / saúde da mulher) e "
            "apresentar os números do SISCAN acima — mostram que acompanhamos a cidade.",
            "Perguntar como é hoje o fluxo da coleta até o laudo: onde trava (coleta, transporte, digitação, prazo).",
            ("Combinar metas mensais de coleta e envio para Varginha e oferecer o acompanhamento mensal da Central "
             "(meta e prazo do laudo); propor reunião com o secretário." if d["varginha"] else
             "Oferecer o apoio da Central: acompanhamento mensal da meta e prazo do laudo; propor reunião com o "
             "secretário e, se fizer sentido, levar ao consórcio/CIB a repactuação."),
        ], 1)]),
        Spacer(1, 6),
        Paragraph("Fontes: PPI/SES-MG, SISCAN/DATASUS (meses do estado), CNES e cadastro CNPJ da Receita. "
                  "Telefones vêm dos cadastros oficiais — confirmar na primeira ligação.", st["sub"]),
    ]


def pdf(codigos):
    """PDF (bytes) com uma página por município."""
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.6 * cm,
                            bottomMargin=1.6 * cm, title="Dossiê de prospecção — Central IPD")
    st = _estilos()
    historia = []
    fichas, estado = robo.fichas(), metas._meses_estado()
    for i, cod in enumerate(codigos):
        if i:
            historia.append(PageBreak())
        historia += _pagina(dados(cod, fichas, estado, buscar_contatos=len(codigos) == 1), st)
    doc.build(historia)
    return buf.getvalue()
