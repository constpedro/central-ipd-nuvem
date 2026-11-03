#!/usr/bin/env python3
"""
Gera o PDF consolidado PPI + SISCAN a partir de um JSON de dados.

Uso:
    python3 gerar_relatorio.py dados.json saida.pdf

O JSON de entrada tem este formato (ver dados_exemplo.json neste mesmo diretório):

{
  "municipio_nome": "Bom Despacho",
  "municipio_codigo": "310740",
  "ano_competencia": "2026",
  "competencia_ppi": "2026/08",
  "atualizacao_dados": "20/08/2026",
  "siscan_linha": "Mes/Ano competencia",
  "siscan_coluna": "Interv Resultado",
  "siscan_colunas": ["0-10 dias", "11-20 dias", "21-30 dias", "> 30 dias", "Total"],
  "siscan_linhas": [
    {"label": "Total", "valores": ["270", "745", "501", "8", "1.524"], "total": true},
    {"label": "Janeiro/2026", "valores": ["15", "78", "112", "—", "205"]}
  ],
  "ppi_subgrupo": "0203 · Anatomia patológica e citopatologia",
  "ppi_procedimentos": [
    {
      "codigo": "019",
      "nome": "Cérvico-Vaginal / Microflora",
      "municipio_atendimento": "317070 · Varginha",
      "qtd_anual": 291,
      "valor_anual": 3992.52
    }
  ],
  "ppi_meta_mensal_qtd": 200.42,
  "ppi_meta_mensal_valor": 2966.90,
  "ppi_total_anual_qtd": 2405,
  "ppi_total_anual_valor": 35602.81,
  "ppi_codigos_ausentes": ["065"],
  "ppi_forma_organizacao": "020301 · Exames citopatológicos"
}

Regras já embutidas no script (não precisa recalcular fora):
- Se "ppi_meta_mensal_qtd" / "ppi_meta_mensal_valor" / "ppi_total_anual_*" não vierem
  no JSON, o script calcula a partir de "ppi_procedimentos" (qtd_anual/12, valor_anual/12,
  somando as médias de cada procedimento encontrado).
- "ppi_forma_organizacao" é OPCIONAL. Só inclua esse campo quando o usuário pediu
  explicitamente para filtrar por Forma de Organização (020301 ou 020302) no site do
  PPI. Nesse caso, o PDF mostra um item extra "Forma de Organização" nos parâmetros do
  relatório, para deixar claro que a lista de procedimentos foi filtrada (e portanto
  pode não conter todos os 7 códigos monitorados). Se o campo não vier no JSON, esse
  item simplesmente não aparece (comportamento padrão, sem filtro).
- O script calcula uma linha extra "Total em <cidade-polo> (÷ 12)" logo abaixo da
  linha Total. Ela usa somente os códigos 019 e 086, soma qtd_anual/valor_anual dos
  procedimentos atendidos no polo e divide por 12. O polo padrão é Varginha.
"""

import base64
import json
import sys
from pathlib import Path

TEMPLATE_PATH = Path(__file__).parent / "assets" / "template_siscan_ppi.html"
LOGO_PATH = Path(__file__).parent / "assets" / "logo_ipd.png"


def build_logo_data_uri():
    data = LOGO_PATH.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def fmt_int(n):
    return f"{n:,.0f}".replace(",", ".")


def fmt_money(n):
    s = f"{n:,.2f}"
    # troca separadores para padrão BR: 1,234.56 -> 1.234,56
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return f"R$ {s}"


def fmt_qtd_media(n):
    s = f"{n:,.2f}"
    s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    return s


def build_siscan_rows(data):
    rows_html = []
    for row in data["siscan_linhas"]:
        cls = ' class="total-row"' if row.get("total") else ""
        cells = "".join(f"<td>{v}</td>" for v in row["valores"])
        rows_html.append(f"<tr{cls}><td>{row['label']}</td>{cells}</tr>")
    return "\n        ".join(rows_html)


def build_siscan_header(data):
    cells = "".join(f"<th>{c}</th>" for c in data["siscan_colunas"])
    return cells


# O relatório mostra outros códigos de 0203 para conferência, mas o total geral e
# a linha do polo representam apenas os dois códigos citopatológicos monitorados.
CODIGOS_TOTAL_GERAL = {"019", "086"}
CODIGOS_DESTACADOS = CODIGOS_TOTAL_GERAL

# Municipio de referencia da linha extra "Total em <cidade> (/ 12)". A skill original
# fixava "Varginha"; aqui e ajustavel porque cada consorcio tem o seu polo.
MUNICIPIO_REFERENCIA = "Varginha"


def build_ppi_rows(procedimentos):
    rows_html = []
    for p in procedimentos:
        qtd_media = p.get("qtd_media_mensal")
        valor_media = p.get("valor_media_mensal")
        if qtd_media is None:
            qtd_media = p["qtd_anual"] / 12
        if valor_media is None:
            valor_media = p["valor_anual"] / 12

        destaque = p["codigo"] in CODIGOS_DESTACADOS
        row_cls = ' class="row-destaque"' if destaque else ""
        qtd_media_cell = fmt_qtd_media(qtd_media)
        valor_media_cell = fmt_money(valor_media)
        if destaque:
            qtd_media_cell = f"<b>{qtd_media_cell}</b>"
            valor_media_cell = f"<b>{valor_media_cell}</b>"

        rows_html.append(
            f"<tr{row_cls}>"
            f"<td><b class=\"num\">{p['codigo']}</b> — {p['nome']}</td>"
            f"<td><span class=\"muni-tag\">{p['municipio_atendimento']}</span></td>"
            f"<td>{fmt_int(p['qtd_anual'])}</td>"
            f"<td>{fmt_money(p['valor_anual'])}</td>"
            f"<td>{qtd_media_cell}</td>"
            f"<td>{valor_media_cell}</td>"
            "</tr>"
        )
    return "\n        ".join(rows_html)


def compute_ppi_totals(data, somente_citopatologicos=True):
    """Totais anuais e médias mensais do PPI.

    Com somente_citopatologicos=True soma só 019 e 086 (cards do topo e a linha
    "Total 019 + 086"); com False soma todos os códigos exibidos (Total geral).
    """
    procedimentos = [
        p for p in data["ppi_procedimentos"]
        if not somente_citopatologicos or p.get("codigo", "")[-3:] in CODIGOS_TOTAL_GERAL
    ]
    # Não usar totais recebidos no JSON: podem ter sido calculados com outro recorte.
    total_qtd_anual = sum(p["qtd_anual"] for p in procedimentos)
    total_valor_anual = sum(p["valor_anual"] for p in procedimentos)
    meta_qtd = total_qtd_anual / 12
    meta_valor = total_valor_anual / 12

    return total_qtd_anual, total_valor_anual, meta_qtd, meta_valor


def build_footnote(data):
    ausentes = data.get("ppi_codigos_ausentes") or []
    base = (
        "<b>Fonte:</b> PPI / SES-MG (Subgrupo 0203). Média mensal = quantidade e valor "
        "anuais ÷ 12, por código, somados entre os códigos localizados. Município de "
        "atendimento exibido quando difere do município de origem. "
        "Total geral soma todos os códigos exibidos; a linha 019 + 086 e o total "
        "do polo consideram somente os códigos citopatológicos 019 e 086."
    )
    if ausentes:
        lista = ", ".join(ausentes)
        base += f" Códigos não localizados nesta consulta e desconsiderados: {lista}."
    return base


def build_varginha_row(procedimentos):
    varginha_procs = [
        p for p in procedimentos
        if p.get("codigo", "")[-3:] in CODIGOS_TOTAL_GERAL
        and MUNICIPIO_REFERENCIA.lower() in p["municipio_atendimento"].lower()
    ]
    if not varginha_procs:
        return ""

    total_qtd = sum(p["qtd_anual"] for p in varginha_procs)
    total_valor = sum(p["valor_anual"] for p in varginha_procs)
    media_qtd = total_qtd / 12
    media_valor = total_valor / 12

    return (
        '<tr class="varginha-row">'
        f"<td>Total em {MUNICIPIO_REFERENCIA} (÷ 12)</td>"
        f"<td>{MUNICIPIO_REFERENCIA}</td>"
        f"<td>{fmt_int(total_qtd)}</td>"
        f"<td>{fmt_money(total_valor)}</td>"
        f"<td>{fmt_qtd_media(media_qtd)}</td>"
        f"<td>{fmt_money(media_valor)}</td>"
        "</tr>"
    )


def build_cito_row(data):
    """Linha "Total 019 + 086 (÷ 12)": só os dois códigos citopatológicos."""
    if not any(p.get("codigo", "")[-3:] in CODIGOS_TOTAL_GERAL for p in data["ppi_procedimentos"]):
        return ""
    total_qtd, total_valor, media_qtd, media_valor = compute_ppi_totals(data)
    return (
        '<tr class="varginha-row">'
        "<td>Total 019 + 086 (÷ 12)</td>"
        "<td>—</td>"
        f"<td>{fmt_int(total_qtd)}</td>"
        f"<td>{fmt_money(total_valor)}</td>"
        f"<td>{fmt_qtd_media(media_qtd)}</td>"
        f"<td>{fmt_money(media_valor)}</td>"
        "</tr>"
    )


def build_forma_organizacao_item(data):
    forma = data.get("ppi_forma_organizacao")
    if not forma:
        return ""
    return (
        '<div class="item"><span class="lbl">Forma de Organização</span>'
        f"<b>{forma}</b></div>"
    )


def render(data, template, body_class):
    total_qtd_anual, total_valor_anual, meta_qtd, meta_valor = compute_ppi_totals(data)
    geral_qtd, geral_valor, geral_media_qtd, geral_media_valor = compute_ppi_totals(
        data, somente_citopatologicos=False)
    n_procedimentos = len(data["ppi_procedimentos"])

    replacements = {
        "{{MUNICIPIO_NOME}}": data["municipio_nome"],
        "{{MUNICIPIO_CODIGO}}": data["municipio_codigo"],
        "{{ANO_COMPETENCIA}}": data["ano_competencia"],
        "{{COMPETENCIA_PPI}}": data["competencia_ppi"],
        "{{ATUALIZACAO_DADOS}}": data["atualizacao_dados"],
        "{{SISCAN_HEADER_CELLS}}": build_siscan_header(data),
        "{{SISCAN_ROWS}}": build_siscan_rows(data),
        "{{PPI_SUBGRUPO}}": data.get("ppi_subgrupo", "0203 · Anatomia patológica e citopatologia"),
        "{{PPI_ROWS}}": build_ppi_rows(data["ppi_procedimentos"]),
        "{{PPI_META_QTD}}": fmt_qtd_media(meta_qtd),
        "{{PPI_META_VALOR}}": fmt_money(meta_valor),
        "{{PPI_TOTAL_QTD}}": fmt_int(total_qtd_anual),
        "{{PPI_TOTAL_VALOR}}": fmt_money(total_valor_anual),
        "{{PPI_N_PROCEDIMENTOS}}": str(n_procedimentos),
        "{{PPI_FOOTNOTE}}": build_footnote(data),
        "{{PPI_FORMA_ORGANIZACAO_ITEM}}": build_forma_organizacao_item(data),
        "{{PPI_GERAL_QTD}}": fmt_int(geral_qtd),
        "{{PPI_GERAL_VALOR}}": fmt_money(geral_valor),
        "{{PPI_GERAL_MEDIA_QTD}}": fmt_qtd_media(geral_media_qtd),
        "{{PPI_GERAL_MEDIA_VALOR}}": fmt_money(geral_media_valor),
        "{{PPI_CITO_ROW}}": build_cito_row(data),
        "{{PPI_VARGINHA_ROW}}": build_varginha_row(data["ppi_procedimentos"]),
        "{{LOGO_DATA_URI}}": build_logo_data_uri(),
        "{{BODY_CLASS}}": body_class,
    }

    html = template
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, str(value))
    return html


def gerar(data, pdf_path, navegador, template=None):
    """Renderiza o relatório e grava o PDF usando o navegador em modo headless.

    Mantém o comportamento da skill: tenta o layout padrão e, se o PDF sair com mais
    de uma página, reprocessa em modo compacto (mesma identidade visual, fontes e
    paddings menores). Devolve o número de páginas do PDF final.
    """
    from pypdf import PdfReader

    from gerar_ppi_ipd import html_para_pdf

    pdf_path = Path(pdf_path)
    if template is None:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
    tmp_html = pdf_path.with_suffix(".html")

    def render_e_conta(body_class):
        tmp_html.write_text(render(data, template, body_class), encoding="utf-8")
        html_para_pdf(navegador, tmp_html, pdf_path)
        return len(PdfReader(str(pdf_path)).pages)

    try:
        n_pages = render_e_conta("")
        if n_pages > 1:
            n_pages = render_e_conta("compact")
    finally:
        tmp_html.unlink(missing_ok=True)
    return n_pages


def main():
    if len(sys.argv) != 3:
        print("Uso: python relatorio_siscan_ppi.py dados.json saida.pdf")
        sys.exit(1)

    from gerar_ppi_ipd import localizar_navegador

    json_path = Path(sys.argv[1])
    pdf_path = Path(sys.argv[2])
    data = json.loads(json_path.read_text(encoding="utf-8"))

    n_pages = gerar(data, pdf_path, localizar_navegador())
    plural = "s" if n_pages != 1 else ""
    print("PDF gerado: " + str(pdf_path) + " (" + str(n_pages) + " pagina" + plural + ")")
    if n_pages > 1:
        print("Aviso: mesmo em modo compacto o relatorio ficou com mais de uma pagina.")


if __name__ == "__main__":
    main()
