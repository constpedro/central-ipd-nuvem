# -*- coding: utf-8 -*-
"""
Padronização visual de planilhas com a identidade do IPD.

Entra um arquivo qualquer (.xlsx/.xlsm/.csv/.tsv/.txt) que a equipe recebeu ou
montou na mão; sai um .xlsx com a cara dos nossos materiais: logo no topo,
título, cabeçalho vermelho, zebra clara, bordas discretas, números alinhados e
formatados (moeda em real, datas em dd/mm/aaaa), filtro e painel congelado, e a
impressão já ajustada à largura da página.

O conteúdo não é alterado — só a apresentação. A única conversão é de texto para
número quando a coluna inteira é numérica, para que o Excel consiga somar.
"""
import csv
import io
import re
import unicodedata
from datetime import date, datetime

import config

# --- identidade visual ----------------------------------------------------------

VERMELHO = "C81E2B"        # var(--brand) da Central
VERMELHO_ESCURO = "A3121D"
VERMELHO_CLARO = "FAEBEC"  # var(--brand-bg), usado na zebra
TINTA = "14161B"
TINTA_3 = "7A8090"
LINHA = "E1E4E9"
FONTE = "Calibri"

# Papel timbrado do IPD (recortado do Comunicado 003/2026). O original é uma imagem
# A4 inteira com o rodapé deslocado para a esquerda; aqui cabeçalho e rodapé são
# peças separadas, e o rodapé é centralizado pelo próprio conteúdo, com a mesma
# sobra dos dois lados, em qualquer orientação de página.
CABECALHO = config.APP_DIR / "static" / "timbrado_cabecalho.png"   # 571 x 171 px
RODAPE = config.APP_DIR / "static" / "timbrado_rodape.png"         # 1505 x 163 px
MM_POR_PX = 210 / 1578          # escala do timbrado original (largura A4)
CAB_LARG_MM, CAB_ALT_MM = 571 * MM_POR_PX, 171 * MM_POR_PX
ROD_LARG_MM, ROD_ALT_MM = 1505 * MM_POR_PX, 163 * MM_POR_PX
TIMBRE_LADO_MM = (210 - ROD_LARG_MM) / 2   # sobra lateral do rodapé em retrato (~4,2 mm)
TIMBRE_TOPO_MM = 8.6            # do topo da página ao cabeçalho, como no comunicado
TIMBRE_BASE_MM = 8.5            # da base da página ao rodapé
MARGEM_TIMBRE_MM = 36           # área do texto começa/termina abaixo/acima das peças

LARGURA_MIN = 9
LARGURA_MAX = 58
MAX_LINHAS = 50_000   # trava de segurança: planilha maior que isso é recusada
MAX_COLUNAS = 120

RE_MOEDA = re.compile(r"VALOR|PRECO|VLR|R\$|CUSTO|TOTAL|SUBTOTAL|FATURA|TARIFA|UNIT")
# Colunas de código: numéricas, mas não se somam nem ganham separador de milhar.
RE_CODIGO = re.compile(r"\b(COD|CODIGO|IBGE|CNES|CNPJ|CPF|CEP|ANO|COMPETENCIA|COMPET|MATRICULA|ID|MNEMONICO)\b")
RE_PERCENT = re.compile(r"%|PERCENT|PERC")


def normalizar(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s).strip().upper()


def nome_arquivo_seguro(nome):
    """Limpa um componente de nome sem destruir letras acentuadas."""
    nome = unicodedata.normalize("NFC", str(nome or ""))
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", nome)
    nome = re.sub(r"\s+", " ", nome).strip(" .")
    return nome or "sem-nome"


# --- leitura --------------------------------------------------------------------

RE_MILHAR_PONTO = re.compile(r"[+-]?[1-9]\d{0,2}(\.\d{3})+")


def ponto_e_milhar(valores):
    """Numa coluna de texto, "1.500" é mil e quinhentos (Brasil) ou um e meio?
    É milhar quando TODOS os números só com ponto têm o formato de milhar
    (1.500, 12.300, 1.234.567). Dado decimal de verdade tem casas variadas
    ("12.5", "3.25", "0.125") e a coluna continua decimal."""
    so_ponto = []
    for v in valores:
        if isinstance(v, str):
            s = v.replace("R$", "").replace("%", "").replace(" ", "").replace("\xa0", "").strip("()")
            if "." in s and "," not in s and re.search(r"\d", s):
                so_ponto.append(s)
    return bool(so_ponto) and all(RE_MILHAR_PONTO.fullmatch(s) for s in so_ponto)


def para_numero(v, milhar_ponto=False):
    """'1.234,56' / 'R$ 1.234,56' / '1234.56' -> float. Texto comum -> None.
    milhar_ponto: a coluna usa ponto de milhar ("1.500" = 1500; ver ponto_e_milhar).
    Com dois ou mais pontos ("1.234.567") é sempre milhar."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").replace("R$", "").replace("%", "").strip()
    if not s or not re.search(r"\d", s):
        return None
    s = s.replace(" ", "").replace(" ", "")
    negativo = s.startswith("(") and s.endswith(")")
    if negativo:
        s = s[1:-1]
    if "," in s and "." in s:
        # o separador decimal é o que aparece por último
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif (milhar_ponto or s.count(".") > 1) and RE_MILHAR_PONTO.fullmatch(s):
        s = s.replace(".", "")
    if not re.fullmatch(r"[+-]?\d*\.?\d+", s):
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negativo else n


def _limpar(linhas):
    """Tira linhas e colunas totalmente vazias das bordas da planilha."""
    linhas = [list(l) for l in linhas]
    while linhas and not any(str(c).strip() for c in linhas[0] if c is not None):
        linhas.pop(0)
    while linhas and not any(str(c).strip() for c in linhas[-1] if c is not None):
        linhas.pop()
    if not linhas:
        return []
    largura = max(len(l) for l in linhas)
    linhas = [l + [None] * (largura - len(l)) for l in linhas]
    usadas = [i for i in range(largura)
              if any(str(l[i]).strip() for l in linhas if l[i] is not None)]
    if not usadas:
        return []
    return [[l[i] for i in usadas] for l in linhas]


def _separar_cabecalho(linhas):
    """Cabeçalho = 1ª linha com 2+ células preenchidas (ou a 1ª, se só há uma coluna)."""
    minimo = 2 if linhas and len(linhas[0]) > 1 else 1
    for i, l in enumerate(linhas):
        if sum(1 for c in l if str(c or "").strip()) >= minimo:
            corpo = [r for r in linhas[i + 1:] if any(str(c or "").strip() for c in r)]
            return [str(c or "").strip() for c in l], corpo
    return [], []


def _ler_csv(conteudo):
    texto = None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            texto = conteudo.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if texto is None:
        raise ValueError("não consegui decodificar o texto do arquivo")
    if texto.lower().startswith("sep="):
        partes = texto.split("\n", 1)
        texto = partes[1] if len(partes) > 1 else ""
    amostra = "\n".join(texto.splitlines()[:20])
    delim = max(";,\t|", key=amostra.count) if amostra else ";"
    if amostra.count(delim) == 0:
        delim = ";"
    return list(csv.reader(io.StringIO(texto), delimiter=delim))


def ler(nome, conteudo):
    """Devolve [{'nome', 'cabecalho', 'linhas'}] — uma entrada por aba com conteúdo."""
    nome_l = (nome or "").lower()
    brutas = []
    if nome_l.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                if ws.sheet_state != "visible":
                    continue
                linhas = _limpar(list(ws.iter_rows(values_only=True)))
                if linhas:
                    brutas.append((ws.title, linhas))
        finally:
            wb.close()
    else:
        linhas = _limpar(_ler_csv(conteudo))
        if linhas:
            brutas.append(("Dados", linhas))
    if not brutas:
        raise ValueError("o arquivo está vazio ou não tem nenhuma linha preenchida")

    abas = []
    for titulo, linhas in brutas:
        if len(linhas) > MAX_LINHAS:
            quantas = f"{len(linhas):,}".replace(",", ".")
            raise ValueError(f"a aba '{titulo}' tem {quantas} linhas — o limite é 50.000")
        if len(linhas[0]) > MAX_COLUNAS:
            raise ValueError(f"a aba '{titulo}' tem {len(linhas[0])} colunas — o limite é {MAX_COLUNAS}")
        cabecalho, corpo = _separar_cabecalho(linhas)
        if cabecalho:
            abas.append({"nome": titulo, "cabecalho": cabecalho, "linhas": corpo})
    if not abas:
        raise ValueError("não encontrei um cabeçalho reconhecível "
                         "(uma linha com 2 ou mais colunas preenchidas)")
    return abas


# --- tipo de cada coluna --------------------------------------------------------

def tipo_coluna(titulo, valores):
    """('texto'|'numero'|'moeda'|'inteiro'|'percent'|'data', formato do Excel)."""
    preenchidos = [v for v in valores if str(v or "").strip() != ""]
    if not preenchidos:
        return "texto", None
    n = normalizar(titulo)

    datas = sum(1 for v in preenchidos if isinstance(v, (datetime, date)))
    if datas >= len(preenchidos) * 0.8:
        return "data", "dd/mm/yyyy"

    milhar = ponto_e_milhar(preenchidos)
    numeros = [para_numero(v, milhar) for v in preenchidos]
    if sum(1 for x in numeros if x is not None) < len(preenchidos) * 0.8:
        return "texto", None
    # Códigos com zero à esquerda (CNES, IBGE, CPF) continuam texto: virar número
    # comeria o zero e mudaria o dado.
    for v in preenchidos:
        s = str(v).strip()
        if isinstance(v, str) and len(s) > 1 and s[0] == "0" and s[1] not in ",.":
            return "texto", None

    if RE_CODIGO.search(n) and all(x is not None and float(x).is_integer() for x in numeros):
        return "codigo", "0"
    if RE_MOEDA.search(n) or any("R$" in str(v) for v in preenchidos[:50]):
        return "moeda", "R$ #,##0.00"
    if RE_PERCENT.search(n):
        return "percent", '#,##0.0"%"'
    if all(x is not None and float(x).is_integer() for x in numeros):
        return "inteiro", "#,##0"
    return "numero", "#,##0.00"


# --- escrita --------------------------------------------------------------------

def _largura(titulo, valores, tipo):
    tamanhos = [len(str(titulo))]
    for v in valores[:400]:
        s = str(v or "")
        tamanhos.append(min(len(s), 14) if tipo != "texto" else len(s))
    tamanhos.sort()
    # percentil 95 em vez do máximo: uma observação gigante não estoura a coluna
    alvo = tamanhos[int(len(tamanhos) * 0.95)] if len(tamanhos) > 4 else tamanhos[-1]
    return max(LARGURA_MIN, min(LARGURA_MAX, alvo + 3))


def _montar_aba(ws, aba, titulo, subtitulo, com_logo, com_zebra, com_totais):
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    cabecalho, linhas = aba["cabecalho"], aba["linhas"]
    n_col = len(cabecalho)
    ultima_letra = get_column_letter(n_col)

    fina = Side(style="thin", color=LINHA)
    grade = Border(left=fina, right=fina, top=fina, bottom=fina)

    # ---- topo: logo, título, subtítulo, régua vermelha
    topo = 0
    if com_logo and CABECALHO.is_file():
        ws.row_dimensions[1].height = 40
        try:
            img = XLImage(str(CABECALHO))
            img.height = 50
            img.width = int(50 * 571 / 171)
            ws.add_image(img, "A1")
        except Exception:  # noqa: BLE001 — sem a logo é melhor que sem a planilha
            pass
        topo = 2  # a linha 2 fica de respiro sob a logo

    linha_titulo = topo + 1
    c = ws.cell(row=linha_titulo, column=1, value=titulo)
    c.font = Font(name=FONTE, size=15, bold=True, color=VERMELHO)
    c.alignment = Alignment(vertical="center")
    ws.row_dimensions[linha_titulo].height = 22

    c = ws.cell(row=linha_titulo + 1, column=1, value=subtitulo)
    c.font = Font(name=FONTE, size=9, color=TINTA_3)
    if n_col > 1:
        ws.merge_cells(start_row=linha_titulo, start_column=1,
                       end_row=linha_titulo, end_column=n_col)
        ws.merge_cells(start_row=linha_titulo + 1, start_column=1,
                       end_row=linha_titulo + 1, end_column=n_col)

    linha_regua = linha_titulo + 2
    ws.row_dimensions[linha_regua].height = 5
    for i in range(1, n_col + 1):
        ws.cell(row=linha_regua, column=i).fill = PatternFill("solid", fgColor=VERMELHO)

    # ---- cabeçalho
    linha_cab = linha_regua + 1
    fundo_cab = PatternFill("solid", fgColor=VERMELHO)
    fonte_cab = Font(name=FONTE, size=10.5, bold=True, color="FFFFFF")
    for i, nome in enumerate(cabecalho, start=1):
        c = ws.cell(row=linha_cab, column=i, value=str(nome).strip() or f"Coluna {i}")
        c.fill, c.font, c.border = fundo_cab, fonte_cab, grade
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[linha_cab].height = 26

    tipos, milhar = [], []
    for i in range(n_col):
        coluna = [l[i] if i < len(l) else None for l in linhas]
        tipos.append(tipo_coluna(cabecalho[i], coluna))
        milhar.append(ponto_e_milhar(coluna))

    # ---- corpo
    zebra = PatternFill("solid", fgColor=VERMELHO_CLARO)
    fonte_corpo = Font(name=FONTE, size=10.5, color=TINTA)
    esquerda = Alignment(horizontal="left", vertical="center")
    centro = Alignment(horizontal="center", vertical="center")
    direita = Alignment(horizontal="right", vertical="center")
    for j, linha in enumerate(linhas):
        r = linha_cab + 1 + j
        pintar = com_zebra and j % 2 == 1
        for i in range(n_col):
            bruto = linha[i] if i < len(linha) else None
            tipo, formato = tipos[i]
            if tipo in ("moeda", "numero", "inteiro", "percent", "codigo"):
                valor = para_numero(bruto, milhar[i])
                if valor is not None and tipo == "codigo":
                    valor = int(valor)
                if valor is None:  # célula fora do padrão da coluna: mantém como veio
                    valor = bruto if str(bruto or "").strip() else None
            elif isinstance(bruto, str):
                valor = bruto.strip()
            else:
                valor = bruto
            c = ws.cell(row=r, column=i + 1, value=valor)
            c.font, c.border = fonte_corpo, grade
            if pintar:
                c.fill = zebra
            if formato and isinstance(valor, (int, float, datetime, date)):
                c.number_format = formato
            c.alignment = esquerda if tipo == "texto" else (centro if tipo in ("data", "codigo") else direita)

    ultima_linha = linha_cab + len(linhas)

    # ---- linha de totais (SUBTOTAL respeita o filtro do cabeçalho)
    somadas = []
    if com_totais and linhas:
        r = ultima_linha + 1
        fundo_tot = PatternFill("solid", fgColor=VERMELHO_CLARO)
        fonte_tot = Font(name=FONTE, size=10.5, bold=True, color=VERMELHO_ESCURO)
        borda_tot = Border(left=fina, right=fina, bottom=fina,
                           top=Side(style="medium", color=VERMELHO))
        for i in range(n_col):
            tipo, formato = tipos[i]
            if tipo in ("moeda", "numero", "inteiro"):
                col = get_column_letter(i + 1)
                c = ws.cell(row=r, column=i + 1,
                            value=f"=SUBTOTAL(109,{col}{linha_cab + 1}:{col}{ultima_linha})")
                c.number_format = formato
                c.alignment = direita
                somadas.append(cabecalho[i])
            else:
                c = ws.cell(row=r, column=i + 1, value="TOTAL" if i == 0 else None)
                c.alignment = esquerda
            c.fill, c.font, c.border = fundo_tot, fonte_tot, borda_tot
        if not somadas:  # nenhuma coluna somável — a linha de total só atrapalharia
            for i in range(n_col):
                ws.cell(row=r, column=i + 1).value = None
                ws.cell(row=r, column=i + 1).fill = PatternFill()
                ws.cell(row=r, column=i + 1).border = Border()
        else:
            ultima_linha = r

    if com_logo and RODAPE.is_file() and linhas:
        try:
            img = XLImage(str(RODAPE))
            img.height = 56
            img.width = int(56 * 1505 / 163)
            ws.add_image(img, f"A{ultima_linha + 3}")
        except Exception:  # noqa: BLE001
            pass

    # ---- larguras, filtro, painel congelado
    for i in range(n_col):
        coluna = [l[i] if i < len(l) else None for l in linhas]
        ws.column_dimensions[get_column_letter(i + 1)].width = _largura(cabecalho[i], coluna, tipos[i][0])
    if linhas:
        ws.auto_filter.ref = f"A{linha_cab}:{ultima_letra}{linha_cab + len(linhas)}"
    ws.freeze_panes = f"A{linha_cab + 1}"

    # ---- impressão
    ws.page_setup.orientation = "landscape" if n_col > 6 else "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{linha_cab}:{linha_cab}"
    ws.print_options.horizontalCentered = True
    ws.oddFooter.right.text = "Página &P de &N"
    ws.oddFooter.right.size = 8
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = VERMELHO

    return {"aba": aba["nome"], "linhas": len(linhas), "colunas": n_col,
            "colunas_nomes": cabecalho, "tipos": [t for t, _ in tipos], "somadas": somadas}


def padronizar(abas, caminho, titulo, subtitulo=None, com_logo=True, com_zebra=True,
               com_totais=False):
    """Grava o .xlsx padronizado em `caminho` e devolve o resumo do que foi feito."""
    from openpyxl import Workbook

    if subtitulo is None:
        subtitulo = ("ipd Exames e Saúde · gerado pela Central IPD em "
                     + datetime.now().strftime("%d/%m/%Y às %H:%M"))

    wb = Workbook()
    wb.remove(wb.active)
    resumo, usados = [], set()
    for n, aba in enumerate(abas):
        nome = re.sub(r"[\\/*?:\[\]]", "-", str(aba["nome"]))[:31].strip() or f"Aba {n + 1}"
        base, i = nome, 2
        while nome.lower() in usados:
            nome = f"{base[:28]} {i}"
            i += 1
        usados.add(nome.lower())
        ws = wb.create_sheet(nome)
        titulo_aba = titulo if len(abas) == 1 else f"{titulo} — {aba['nome']}"
        resumo.append(_montar_aba(ws, aba, titulo_aba, subtitulo, com_logo, com_zebra, com_totais))

    wb.properties.creator = "Central IPD"
    wb.properties.title = titulo
    caminho.parent.mkdir(parents=True, exist_ok=True)
    wb.save(caminho)
    return {"arquivo": caminho.name, "titulo": titulo, "subtitulo": subtitulo, "abas": resumo}


# --- Word e PDF ------------------------------------------------------------------
# As duas saídas partem dos mesmos dados tipados da planilha; os números saem como
# texto no padrão brasileiro, porque Word e PDF não recalculam nada.

MAX_LINHAS_DOC = 5_000  # acima disso Word/PDF ficam enormes e lentos; use o .xlsx


def subtitulo_padrao():
    return "ipd Exames e Saúde · gerado pela Central IPD em " + datetime.now().strftime("%d/%m/%Y às %H:%M")


def _br(n, casas=2):
    return f"{n:,.{casas}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _texto(valor, tipo):
    if valor is None or valor == "":
        return ""
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        if tipo == "moeda":
            return ("-R$ " if valor < 0 else "R$ ") + _br(abs(valor))
        if tipo == "percent":
            return _br(valor, 1) + "%"
        if tipo == "inteiro":
            return _br(valor, 0)
        if tipo == "codigo":
            return str(int(valor))
        return _br(valor)
    if isinstance(valor, datetime):
        if tipo == "data" or (valor.hour, valor.minute) == (0, 0):
            return valor.strftime("%d/%m/%Y")
        return valor.strftime("%d/%m/%Y %H:%M")
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    return str(valor).strip()


def _tabela_texto(aba, com_totais):
    """(cabeçalho, linhas em texto, tipos, linha de total ou None, colunas somadas)."""
    cab, linhas = aba["cabecalho"], aba["linhas"]
    if len(linhas) > MAX_LINHAS_DOC:
        raise ValueError(f"a aba '{aba['nome']}' tem {_br(len(linhas), 0)} linhas; Word e PDF aceitam "
                         f"até {_br(MAX_LINHAS_DOC, 0)} (para planilhas maiores, gere só o .xlsx)")
    n = len(cab)
    colunas = [[l[i] if i < len(l) else None for l in linhas] for i in range(n)]
    tipos = [tipo_coluna(cab[i], colunas[i])[0] for i in range(n)]
    milhar = [ponto_e_milhar(c) for c in colunas]
    somas = [0.0 if t in ("moeda", "numero", "inteiro") else None for t in tipos]
    corpo = []
    for l in linhas:
        linha = []
        for i in range(n):
            v = l[i] if i < len(l) else None
            if tipos[i] in ("moeda", "numero", "inteiro", "percent", "codigo"):
                num = para_numero(v, milhar[i])
                if num is not None:
                    v = num
                    if somas[i] is not None:
                        somas[i] += num
            linha.append(_texto(v, tipos[i]))
        corpo.append(linha)
    somadas = [cab[i] for i in range(n) if somas[i] is not None]
    total = None
    if com_totais and somadas and corpo:
        total = [_texto(somas[i], tipos[i]) if somas[i] is not None else "" for i in range(n)]
        if not total[0]:
            total[0] = "TOTAL"
    cab = [str(c).strip() or f"Coluna {i + 1}" for i, c in enumerate(cab)]
    return cab, corpo, tipos, total, somadas


def _titulo_aba(titulo, aba, abas):
    return titulo if len(abas) == 1 else f"{titulo} — {aba['nome']}"


def _pesos(cab, corpo):
    """Peso de largura de cada coluna (percentil 95 do tamanho do texto)."""
    pesos = []
    for i in range(len(cab)):
        tam = sorted([len(cab[i])] + [len(l[i]) for l in corpo[:400]])
        alvo = tam[int(len(tam) * 0.95)] if len(tam) > 4 else tam[-1]
        pesos.append(max(4, len(cab[i]) + 2, min(45, alvo)))
    return pesos


def _timbrar_secao(secao, margem_lateral_mm):
    """Troca cabeçalho e rodapé da seção pelo papel timbrado do IPD."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    if not (CABECALHO.is_file() and RODAPE.is_file()):
        return
    secao.different_first_page_header_footer = False
    secao.header_distance = Cm(TIMBRE_TOPO_MM / 10)
    secao.footer_distance = Cm(TIMBRE_BASE_MM / 10)
    for parte in (secao.header, secao.footer):
        parte.is_linked_to_previous = False
        for filho in list(parte._element):   # descarta o cabeçalho/rodapé que o arquivo já tinha
            parte._element.remove(filho)
    cab_p = secao.header.add_paragraph()
    cab_p.paragraph_format.left_indent = Cm((TIMBRE_LADO_MM - margem_lateral_mm) / 10)
    cab_p.add_run().add_picture(str(CABECALHO), width=Cm(CAB_LARG_MM / 10))
    rod_p = secao.footer.add_paragraph()
    rod_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    # O parágrafo ocupa a página inteira (recuo negativo igual nos dois
    # lados até a borda) e a imagem fica centralizada nele: a sobra do
    # rodapé sai idêntica à esquerda e à direita. Um parágrafo da largura
    # exata da imagem faria o Word alinhá-la à esquerda por arredondamento.
    rod_p.paragraph_format.left_indent = rod_p.paragraph_format.right_indent = Cm(-margem_lateral_mm / 10)
    rod_p.add_run().add_picture(str(RODAPE), width=Cm(ROD_LARG_MM / 10))


def gerar_docx(abas, caminho, titulo, subtitulo, com_logo=True, com_zebra=True, com_totais=False):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    def sombrear(celula, cor):
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), cor)
        celula._tc.get_or_add_tcPr().append(shd)

    def bordas(tabela):
        b = OxmlElement("w:tblBorders")
        for lado in ("top", "left", "bottom", "right", "insideH", "insideV"):
            e = OxmlElement(f"w:{lado}")
            for k, v in (("val", "single"), ("sz", "4"), ("color", LINHA)):
                e.set(qn(f"w:{k}"), v)
            b.append(e)
        tabela._tbl.tblPr.append(b)

    def escrever(celula, texto, tamanho, negrito=False, cor=TINTA, alinhamento=WD_ALIGN_PARAGRAPH.LEFT):
        p = celula.paragraphs[0]
        p.alignment = alinhamento
        p.paragraph_format.space_before = p.paragraph_format.space_after = Pt(1)
        run = p.add_run(texto)
        run.font.size, run.font.bold, run.font.name = Pt(tamanho), negrito, FONTE
        run.font.color.rgb = RGBColor.from_string(cor)

    def alinhar(t):
        if t == "texto":
            return WD_ALIGN_PARAGRAPH.LEFT
        return WD_ALIGN_PARAGRAPH.CENTER if t in ("data", "codigo") else WD_ALIGN_PARAGRAPH.RIGHT

    doc = Document()
    doc.styles["Normal"].font.name = FONTE
    doc.styles["Normal"].font.size = Pt(10)
    resumo = []
    for n, aba in enumerate(abas):
        cab, corpo, tipos, total, somadas = _tabela_texto(aba, com_totais)
        largo = len(cab) > 6
        secao = doc.sections[0] if n == 0 else doc.add_section()
        secao.orientation = WD_ORIENT.LANDSCAPE if largo else WD_ORIENT.PORTRAIT
        secao.page_width, secao.page_height = (Cm(29.7), Cm(21)) if largo else (Cm(21), Cm(29.7))
        secao.left_margin = secao.right_margin = Cm(1.5)
        secao.top_margin = secao.bottom_margin = Cm(MARGEM_TIMBRE_MM / 10) if com_logo else Cm(1.5)
        if com_logo:
            _timbrar_secao(secao, 15)

        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        r = p.add_run(_titulo_aba(titulo, aba, abas))
        r.font.size, r.font.bold = Pt(16), True
        r.font.color.rgb = RGBColor.from_string(VERMELHO)
        p = doc.add_paragraph()
        r = p.add_run(subtitulo)
        r.font.size = Pt(8.5)
        r.font.color.rgb = RGBColor.from_string(TINTA_3)
        bdr, fundo = OxmlElement("w:pBdr"), OxmlElement("w:bottom")  # régua vermelha
        for k, v in (("val", "single"), ("sz", "18"), ("space", "4"), ("color", VERMELHO)):
            fundo.set(qn(f"w:{k}"), v)
        bdr.append(fundo)
        p._p.get_or_add_pPr().append(bdr)

        tamanho = 9 if len(cab) <= 6 else (8 if len(cab) <= 10 else 7)
        tabela = doc.add_table(rows=1, cols=len(cab))
        tabela.alignment = WD_TABLE_ALIGNMENT.CENTER
        tabela.autofit = False
        bordas(tabela)
        util = (29.7 if largo else 21) - 3
        larguras = [Cm(util * p / sum(pesos)) for pesos in [_pesos(cab, corpo)] for p in pesos]
        for i, nome in enumerate(cab):
            c = tabela.rows[0].cells[i]
            sombrear(c, VERMELHO)
            escrever(c, nome, tamanho, True, "FFFFFF", WD_ALIGN_PARAGRAPH.CENTER)
        cabecalho_repete = OxmlElement("w:tblHeader")
        cabecalho_repete.set(qn("w:val"), "true")
        tabela.rows[0]._tr.get_or_add_trPr().append(cabecalho_repete)

        for j, linha in enumerate(corpo):
            celulas = tabela.add_row().cells
            for i, texto in enumerate(linha):
                if com_zebra and j % 2 == 1:
                    sombrear(celulas[i], VERMELHO_CLARO)
                escrever(celulas[i], texto, tamanho, alinhamento=alinhar(tipos[i]))
        if total:
            celulas = tabela.add_row().cells
            for i, texto in enumerate(total):
                sombrear(celulas[i], VERMELHO_CLARO)
                escrever(celulas[i], texto, tamanho, True, VERMELHO_ESCURO,
                         WD_ALIGN_PARAGRAPH.LEFT if i == 0 else alinhar(tipos[i]))
        # Word só respeita larguras com layout fixo + a grade de colunas preenchida
        layout = OxmlElement("w:tblLayout")
        layout.set(qn("w:type"), "fixed")
        tabela._tbl.tblPr.append(layout)
        for i, col in enumerate(tabela.columns):
            col.width = larguras[i]
        for linha in tabela.rows:
            for i, c in enumerate(linha.cells):
                c.width = larguras[i]
        resumo.append({"aba": aba["nome"], "linhas": len(corpo), "colunas": len(cab), "somadas": somadas})

    doc.core_properties.author = "Central IPD"
    doc.core_properties.title = titulo
    caminho.parent.mkdir(parents=True, exist_ok=True)
    doc.save(caminho)
    return resumo


def gerar_pdf(abas, caminho, titulo, subtitulo, com_logo=True, com_zebra=True, com_totais=False):
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (BaseDocTemplate, Frame, NextPageTemplate, PageBreak,
                                    PageTemplate, Paragraph, Spacer, Table, TableStyle)

    def cor(h):
        return colors.HexColor("#" + h)

    from reportlab.lib.units import mm

    margem = 15 * mm
    timbre = com_logo and CABECALHO.is_file() and RODAPE.is_file()
    vertical = MARGEM_TIMBRE_MM * mm if timbre else 15 * mm

    def moldura(canvas, doc):
        canvas.saveState()
        largura, altura = canvas._pagesize
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(cor(TINTA_3))
        if timbre:
            topo_cab = altura - TIMBRE_TOPO_MM * mm
            canvas.drawImage(str(CABECALHO), TIMBRE_LADO_MM * mm, topo_cab - CAB_ALT_MM * mm,
                             width=CAB_LARG_MM * mm, height=CAB_ALT_MM * mm, mask="auto")
            # rodapé centralizado pelo conteúdo: sobra igual à esquerda e à direita
            canvas.drawImage(str(RODAPE), (largura - ROD_LARG_MM * mm) / 2, TIMBRE_BASE_MM * mm,
                             width=ROD_LARG_MM * mm, height=ROD_ALT_MM * mm, mask="auto")
            canvas.drawRightString(largura - TIMBRE_LADO_MM * mm - 2 * mm,
                                   topo_cab - CAB_ALT_MM * mm / 2, f"Página {doc.page}")
        else:
            canvas.drawRightString(largura - margem, 8 * mm, f"Página {doc.page}")
        canvas.restoreState()

    def modelo(nome, tamanho):
        l, a = tamanho
        frame = Frame(margem, vertical, l - 2 * margem, a - 2 * vertical,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        return PageTemplate(id=nome, frames=[frame], onPage=moldura, pagesize=tamanho)

    st_titulo = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=16, leading=19, textColor=cor(VERMELHO))
    st_sub = ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, leading=11, textColor=cor(TINTA_3))
    alinhamentos = {"texto": TA_LEFT, "data": TA_CENTER, "codigo": TA_CENTER}

    historia, resumo, orientacoes = [], [], []
    for n, aba in enumerate(abas):
        cab, corpo, tipos, total, somadas = _tabela_texto(aba, com_totais)
        largo = len(cab) > 6
        orientacoes.append("paisagem" if largo else "retrato")
        if n > 0:
            historia += [NextPageTemplate(orientacoes[-1]), PageBreak()]
        tamanho = 8.5 if len(cab) <= 6 else (7.5 if len(cab) <= 10 else 6.5)
        estilos = {}

        def estilo(t, negrito=False, cor_txt=TINTA, tamanho=tamanho, estilos=estilos):
            chave = (t, negrito, cor_txt)
            if chave not in estilos:
                estilos[chave] = ParagraphStyle(
                    str(chave), fontName="Helvetica-Bold" if negrito else "Helvetica", fontSize=tamanho,
                    leading=tamanho * 1.25, textColor=cor(cor_txt),
                    alignment=alinhamentos.get(t, TA_RIGHT))
            return estilos[chave]

        dados = [[Paragraph(escape(c), estilo("data", True, "FFFFFF")) for c in cab]]
        dados += [[Paragraph(escape(t), estilo(tipos[i])) for i, t in enumerate(l)] for l in corpo]
        if total:
            dados.append([Paragraph(escape(t), estilo("texto" if i == 0 else tipos[i], True, VERMELHO_ESCURO))
                          for i, t in enumerate(total)])

        util = (landscape(A4)[0] if largo else A4[0]) - 2 * margem
        pesos = _pesos(cab, corpo)
        tabela = Table(dados, colWidths=[util * p / sum(pesos) for p in pesos], repeatRows=1)
        comandos = [
            ("BACKGROUND", (0, 0), (-1, 0), cor(VERMELHO)),
            ("GRID", (0, 0), (-1, -1), 0.4, cor(LINHA)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        if com_zebra:
            comandos += [("BACKGROUND", (0, j), (-1, j), cor(VERMELHO_CLARO))
                         for j in range(2, len(corpo) + 1, 2)]
        if total:
            comandos += [("BACKGROUND", (0, -1), (-1, -1), cor(VERMELHO_CLARO)),
                         ("LINEABOVE", (0, -1), (-1, -1), 1.2, cor(VERMELHO))]
        tabela.setStyle(TableStyle(comandos))
        historia += [Paragraph(escape(_titulo_aba(titulo, aba, abas)), st_titulo),
                     Paragraph(escape(subtitulo), st_sub), Spacer(1, 8), tabela]
        resumo.append({"aba": aba["nome"], "linhas": len(corpo), "colunas": len(cab), "somadas": somadas})

    modelos = {"retrato": modelo("retrato", A4), "paisagem": modelo("paisagem", landscape(A4))}
    ordem = [modelos[orientacoes[0]]] + [m for k, m in modelos.items() if k != orientacoes[0]]
    caminho.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(str(caminho), pagesize=ordem[0].pagesize, pageTemplates=ordem,
                          title=titulo, author="Central IPD")
    doc.build(historia)
    return resumo


# --- documentos Word e imagens ---------------------------------------------------
# Aqui o conteúdo já é um documento pronto: o .docx recebe o papel timbrado e a
# fonte da casa sem mexer no texto; as imagens viram um documento A4 com título,
# uma imagem por página. O PDF sai do próprio .docx (LibreOffice ou Word), então as
# duas versões ficam idênticas.

EXT_DOCUMENTO = (".docx",)
EXT_IMAGEM = (".png", ".jpg", ".jpeg")
MARGEM_DOC_MM = 20


def _fonte_unica(doc):
    """Tira as fontes explícitas do documento e põe a da casa como padrão."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    for raiz in (doc.element.body, doc.styles.element):
        for rfonts in list(raiz.iter(qn("w:rFonts"))):
            rfonts.getparent().remove(rfonts)
    padrao = doc.styles.element.find(qn("w:docDefaults"))
    if padrao is None:
        padrao = OxmlElement("w:docDefaults")
        doc.styles.element.insert(0, padrao)
    rpr_padrao = padrao.find(qn("w:rPrDefault"))
    if rpr_padrao is None:
        rpr_padrao = OxmlElement("w:rPrDefault")
        padrao.insert(0, rpr_padrao)
    rpr = rpr_padrao.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        rpr_padrao.append(rpr)
    rfonts = OxmlElement("w:rFonts")
    for k in ("ascii", "hAnsi", "cs", "eastAsia"):
        rfonts.set(qn(f"w:{k}"), FONTE)
    rpr.insert(0, rfonts)


def padronizar_docx(conteudo, caminho, titulo, com_logo=True):
    from docx import Document
    from docx.shared import Mm

    doc = Document(io.BytesIO(conteudo))
    doc.settings.odd_and_even_pages_header_footer = False
    secoes = list(doc.sections)
    for secao in secoes:
        deitada = secao.page_width > secao.page_height
        secao.page_width, secao.page_height = (Mm(297), Mm(210)) if deitada else (Mm(210), Mm(297))
        secao.left_margin = secao.right_margin = Mm(MARGEM_DOC_MM)
        secao.top_margin = secao.bottom_margin = Mm(MARGEM_TIMBRE_MM if com_logo else MARGEM_DOC_MM)
        if com_logo:
            _timbrar_secao(secao, MARGEM_DOC_MM)
    _fonte_unica(doc)
    doc.core_properties.author = "Central IPD"
    doc.core_properties.title = titulo
    caminho.parent.mkdir(parents=True, exist_ok=True)
    doc.save(caminho)
    return [{"item": "Documento", "detalhe": f"{len(secoes)} seção(ões), {len(doc.tables)} tabela(s)"}]


def imagens_docx(imagens, caminho, titulo, subtitulo, com_logo=True):
    """imagens: [(nome, bytes)] → .docx A4 com título e uma imagem por página."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.shared import Mm, Pt, RGBColor
    from PIL import Image, ImageOps

    doc = Document()
    doc.styles["Normal"].font.name = FONTE
    doc.styles["Normal"].font.size = Pt(10)
    secao = doc.sections[0]
    secao.page_width, secao.page_height = Mm(210), Mm(297)
    secao.left_margin = secao.right_margin = Mm(MARGEM_DOC_MM)
    vertical = MARGEM_TIMBRE_MM if com_logo else MARGEM_DOC_MM
    secao.top_margin = secao.bottom_margin = Mm(vertical)
    if com_logo:
        _timbrar_secao(secao, MARGEM_DOC_MM)

    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    r = p.add_run(titulo)
    r.font.size, r.font.bold = Pt(16), True
    r.font.color.rgb = RGBColor.from_string(VERMELHO)
    p = doc.add_paragraph()
    r = p.add_run(subtitulo)
    r.font.size = Pt(8.5)
    r.font.color.rgb = RGBColor.from_string(TINTA_3)

    larg_util = 210 - 2 * MARGEM_DOC_MM
    alt_util = 297 - 2 * vertical - 6
    resumo = []
    for n, (nome, dados) in enumerate(imagens):
        try:
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(dados)))
        except Exception:  # noqa: BLE001
            raise ValueError(f"'{nome}' não é uma imagem PNG/JPEG válida")
        com_alfa = img.mode in ("RGBA", "LA", "P")
        img = img.convert("RGBA" if com_alfa else "RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG" if com_alfa else "JPEG", quality=90)
        buf.seek(0)
        larg, alt = img.size
        # a primeira página divide espaço com o título
        alt_max = alt_util - (16 if n == 0 else 0)
        escala = min(larg_util / larg, alt_max / alt)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if n > 0:
            p.add_run().add_break(WD_BREAK.PAGE)
        p.add_run().add_picture(buf, width=Mm(larg * escala), height=Mm(alt * escala))
        resumo.append({"item": nome, "detalhe": f"{larg} × {alt} px — página {n + 1}"})

    doc.core_properties.author = "Central IPD"
    doc.core_properties.title = titulo
    caminho.parent.mkdir(parents=True, exist_ok=True)
    doc.save(caminho)
    return resumo


def abas_de_imagens(imagens):
    """OCR: cada imagem vira uma aba de tabela, como se fosse uma planilha enviada."""
    from core import ocr

    abas = []
    for n, (nome, dados) in enumerate(imagens):
        linhas = _limpar(ocr.tabela_de_imagem(dados, nome))
        cabecalho, corpo = _separar_cabecalho(linhas)
        if not cabecalho:
            raise ValueError(f"não consegui montar uma tabela a partir de '{nome}'")
        titulo = nome.rsplit(".", 1)[0][:31] if len(imagens) > 1 else "Dados"
        abas.append({"nome": titulo, "cabecalho": cabecalho, "linhas": corpo})
    return abas


_LIBREOFFICE_TRAVA = __import__("threading").Lock()


def _soffice():
    import os
    import shutil
    from pathlib import Path

    candidatos = [shutil.which("soffice")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidatos.append(str(Path(base) / "LibreOffice" / "program" / "soffice.exe"))
    for c in candidatos:
        if c and Path(c).is_file():
            return c
    return None


def docx_para_pdf(docx, pdf):
    """PDF pelo LibreOffice; se ele não estiver instalado ou falhar, pelo
    Microsoft Word do próprio PC. Assim funciona com qualquer um dos dois."""
    from pathlib import Path

    pdf = Path(pdf)
    erro = None
    if _soffice():
        try:
            _pdf_pelo_libreoffice(docx, pdf)
            return pdf
        except ValueError as e:
            erro = e
    if _pdf_pelo_word(docx, pdf):
        return pdf
    if erro:
        raise erro
    raise ValueError("para gerar o PDF é preciso ter o LibreOffice ou o Microsoft Word instalado")


def _pdf_pelo_word(docx, pdf):
    """Abre o Word escondido (automação COM pelo PowerShell — basta o Office)."""
    import os
    import subprocess
    from pathlib import Path

    if os.name != "nt":
        return False
    pdf.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$ErrorActionPreference='Stop';"
        "$w=New-Object -ComObject Word.Application;$w.Visible=$false;$w.DisplayAlerts=0;"
        "try{$d=$w.Documents.Open($env:CIPD_DOCX,$false,$true);"
        "$d.SaveAs2($env:CIPD_PDF,17);$d.Close(0)}finally{$w.Quit()}"
    )
    env = dict(os.environ, CIPD_DOCX=str(Path(docx).resolve()), CIPD_PDF=str(pdf.resolve()))
    with _LIBREOFFICE_TRAVA:
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           env=env, capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return False
    return pdf.is_file()


def _pdf_pelo_libreoffice(docx, pdf):
    """Converte com o LibreOffice (sem janela), usando um perfil próprio da Central."""
    import subprocess
    import tempfile
    from pathlib import Path

    perfil = (config.CACHE_DIR / "libreoffice").resolve()
    perfil.mkdir(parents=True, exist_ok=True)
    with _LIBREOFFICE_TRAVA, tempfile.TemporaryDirectory() as tmp:
        cmd = [_soffice(), f"-env:UserInstallation={perfil.as_uri()}", "--headless", "--norestore",
               "--convert-to", "pdf", "--outdir", tmp, str(docx)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ValueError("o LibreOffice demorou demais para gerar o PDF; tente de novo")
        gerado = Path(tmp) / (Path(docx).stem + ".pdf")
        if not gerado.is_file():
            raise ValueError("o LibreOffice não conseguiu gerar o PDF")
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(gerado.read_bytes())
