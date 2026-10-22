# -*- coding: utf-8 -*-
"""
Propostas comerciais: catálogo das existentes e geração de novas a partir dos
templates da IPD.

CATÁLOGO — varre as pastas de config.PROPOSTAS_PASTAS atrás de .docx/.pdf com
"proposta" ou "cota" no nome, extrai número, ano e cliente do nome do arquivo
(padrões: "Proposta Comercial - 0049 - 28042026 - Fhomuv", "COTAÇÃO 0075 -
PREF MUN POUSO ALEGRE", "Cotação 079-2026 - CISVER") e sugere o próximo número.

GERAÇÃO — abre o .docx do template (Saúde Pública = "Cotação de preços" com os
dados do fornecedor; Saúde Privada = "Cotação exame" em carta), troca número,
cliente, contato, objeto, data e reconstrói a tabela de itens copiando a
formatação da primeira linha. O logo e o restante do layout ficam intactos.
Se houver LibreOffice, converte também para PDF.
"""
import copy
import json
import math
import os
import re
import shutil
import unicodedata
from datetime import date, datetime
from pathlib import Path

from docx import Document

import config

MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]
RE_NUMERO = re.compile(r"(?<![A-Za-z0-9])(\d{3,4})\s*[-_/ ]\s*(20\d{2})(?!\d)")       # 079-2026, 0079_2026, 079/2026
RE_NUMERO_DATA = re.compile(r"(?<![A-Za-z0-9])(\d{3,4})\s*-\s*(\d{2})(\d{2})(20\d{2})")  # 0049 - 28042026
RE_NUMERO_DATA6 = re.compile(r"(?<![A-Za-z0-9])(\d{3,4})\s*-\s*(\d{2})(\d{2})(\d{2})(?!\d)")  # 0170 -251222 (AAMMDD)
RE_NUMERO_SOLTO = re.compile(r"(?<![A-Za-z0-9])0(\d{2,3})(?!\d)")                      # 0075
RE_RESERVA = re.compile(r"^\.proposta-(\d+)-(\d{4})\.lock$")
PASTA_ANTERIORES = "_versoes_anteriores"   # versões substituídas ao editar uma proposta
PASTA_EXCLUIDAS = "_excluidas"             # propostas excluídas (o número volta a ficar livre)
ARQ_DADOS = "proposta.json"                 # dados usados na geração (para editar depois)


def fmt_moeda(v):
    s = f"{v:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return s


def data_extenso(d=None):
    d = d or date.today()
    return f"{config.CIDADE_EMISSAO}, {d.day:02d} de {MESES[d.month - 1]} de {d.year}."


# --- catálogo -------------------------------------------------------------------

def _interpretar_nome(nome):
    base = Path(nome).stem
    numero = ano = None
    m = RE_NUMERO_DATA.search(base)
    if m:
        numero, ano = int(m.group(1)), int(m.group(4))
    elif RE_NUMERO.search(base):
        m = RE_NUMERO.search(base)
        numero, ano = int(m.group(1)), int(m.group(2))
    elif RE_NUMERO_DATA6.search(base):
        m = RE_NUMERO_DATA6.search(base)
        numero, ano = int(m.group(1)), 2000 + int(m.group(2))
        if not 2015 <= ano <= date.today().year + 1:  # "301010" e afins: data ilegível
            ano = None
    elif RE_NUMERO_SOLTO.search(base):
        numero = int(RE_NUMERO_SOLTO.search(base).group(1))
    if numero is None or numero > 999:
        return None
    # cliente = o que sobra depois de tirar prefixo, número e datas
    cliente = base
    cliente = re.sub(r"(?i)^(proposta|cota[cç][aã]o)( comercial| de pre[cç]os)?\s*", "", cliente)
    cliente = re.sub(r"(?i)\b(n[º°o]\.?)\s*", "", cliente)
    cliente = re.sub(r"\b\d{3,4}\s*[-_/ ]\s*20\d{2}\b", "", cliente)
    cliente = re.sub(r"\b\d{8}\b", "", cliente)
    cliente = re.sub(r"\b0\d{2,3}\b", "", cliente)
    cliente = re.sub(r"\(\d+\)", "", cliente)
    cliente = re.sub(r"[-_–—]+", " ", cliente)
    cliente = re.sub(r"\s+", " ", cliente).strip(" -_–—")
    return {"numero": numero, "ano": ano, "cliente": cliente}


CAMPOS_CLASSIF = ("tipo_orcamento", "cliente_categoria", "cliente_situacao", "produtos", "etapa")


def _classificacao(pasta):
    """Classificação gravada no proposta.json (vazio nas propostas antigas)."""
    try:
        dados = json.loads((pasta / ARQ_DADOS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: dados.get(k) for k in CAMPOS_CLASSIF if dados.get(k)}


def rotulos_classificacao(c):
    """Rótulos legíveis de uma classificação, na ordem de exibição."""
    if not c:
        return []
    r = []
    if c.get("tipo_orcamento") in config.CLASSIF_TIPO_ORCAMENTO:
        r.append(config.CLASSIF_TIPO_ORCAMENTO[c["tipo_orcamento"]])
    if c.get("cliente_categoria") in config.CLASSIF_CLIENTE:
        txt = config.CLASSIF_CLIENTE[c["cliente_categoria"]]
        if c.get("cliente_situacao") in config.CLASSIF_SITUACAO_CLIENTE:
            txt += " · " + config.CLASSIF_SITUACAO_CLIENTE[c["cliente_situacao"]].lower()
        r.append(txt)
    r += [config.CLASSIF_PRODUTOS[p] for p in (c.get("produtos") or []) if p in config.CLASSIF_PRODUTOS]
    if c.get("etapa") in config.CLASSIF_ETAPA:
        r.append(config.CLASSIF_ETAPA[c["etapa"]])
    return r


def catalogo():
    vistos = set()
    itens = []

    def considerar(p, origem):
        if p.suffix.lower() not in (".docx", ".pdf") or p.name.startswith("~$"):
            return
        if PASTA_ANTERIORES in p.parts or PASTA_EXCLUIDAS in p.parts:
            return
        n = p.name.lower()
        if not ("proposta" in n or "cota" in n):
            return
        chave = str(p.resolve()).lower()
        if chave in vistos:
            return
        vistos.add(chave)
        info = _interpretar_nome(p.name)
        if not info:
            return
        st = p.stat()
        itens.append({
            "numero": info["numero"], "ano": info["ano"], "cliente": info["cliente"] or p.stem,
            "arquivo": p.name, "caminho": str(p), "tipo": p.suffix.lower()[1:],
            "modificado": datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y"),
            "mtime": st.st_mtime, "origem": origem, "editar": _pasta_editavel(p),
            "classif": _classificacao(p.parent),
        })

    for pasta in config.PROPOSTAS_PASTAS:
        if pasta.is_dir():
            for p in pasta.rglob("*"):
                if p.is_file():
                    considerar(p, pasta.name)
    for pasta in config.PROPOSTAS_PASTAS_RASAS:
        if pasta.is_dir():
            for p in pasta.iterdir():
                if p.is_file():
                    considerar(p, pasta.name)

    itens.sort(key=lambda x: (x["ano"] or 0, x["numero"], x["mtime"]), reverse=True)
    return itens


def proximo_numero(itens=None, ano=None):
    ano = ano or date.today().year
    itens = itens if itens is not None else catalogo()
    maior = max((i["numero"] for i in itens if i["ano"] == ano), default=0)
    # Também considera números reservados por uma geração em andamento.
    if config.PROPOSTAS_SAIDA.is_dir():
        for reserva in config.PROPOSTAS_SAIDA.glob(".proposta-*-*.lock"):
            m = RE_RESERVA.match(reserva.name)
            if m and int(m.group(2)) == ano and _reserva_ativa(reserva):
                maior = max(maior, int(m.group(1)))
    return max(maior + 1, config.PROPOSTA_NUMERO_INICIAL if ano == date.today().year else 1)


def _processo_vivo(pid):
    """No Windows os.kill(pid, 0) não testa nada (nunca falha, mesmo com o
    processo morto) — lá a consulta é feita pela API do sistema."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # acesso negado = existe, de outro usuário
        try:
            codigo = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo)):
                return True
            return codigo.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _reserva_ativa(caminho):
    """Verifica uma reserva e remove marcadores deixados por processo morto."""
    try:
        pid = int(caminho.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return True  # reserva ilegível é mantida por segurança
    if pid == os.getpid() or _processo_vivo(pid):
        return True
    try:
        caminho.unlink()
    except OSError:
        pass
    return False


def _reservar_numero(numero, ano):
    config.PROPOSTAS_SAIDA.mkdir(parents=True, exist_ok=True)
    caminho = config.PROPOSTAS_SAIDA / f".proposta-{numero:03d}-{ano:04d}.lock"
    if caminho.exists() and not _reserva_ativa(caminho):
        caminho.unlink(missing_ok=True)
    try:
        with caminho.open("x", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except FileExistsError as e:
        raise ValueError(f"a proposta {numero:03d}/{ano} já está sendo gerada") from e
    return caminho


def fmt_quantidade(v):
    qtd = float(v or 1)
    if qtd.is_integer():
        return str(int(qtd))
    return f"{qtd:.2f}".rstrip("0").rstrip(".").replace(".", ",")


# --- geração --------------------------------------------------------------------

def _definir_texto(paragrafo, texto):
    """Troca o texto do parágrafo mantendo a formatação do primeiro run."""
    runs = paragrafo.runs
    if not runs:
        paragrafo.add_run(texto)
        return
    runs[0].text = texto
    for r in runs[1:]:
        r.text = ""


def _substituir_prefixo(paragrafo, prefixo, valor):
    """Para parágrafos 'Rótulo: valor' — mantém o rótulo, troca o valor."""
    _definir_texto(paragrafo, f"{prefixo}{valor}")


def _preencher_tabela(tabela, linhas):
    """Mantém o cabeçalho, apaga as linhas de dados e recria copiando a 1ª."""
    if len(tabela.rows) < 2:
        raise ValueError("a tabela de itens do template precisa de cabeçalho + 1 linha modelo")
    modelo = copy.deepcopy(tabela.rows[1]._tr)
    for row in list(tabela.rows)[1:]:
        tabela._tbl.remove(row._tr)
    for valores in linhas:
        tr = copy.deepcopy(modelo)
        tabela._tbl.append(tr)
        row = tabela.rows[-1]
        for celula, valor in zip(row.cells, valores):
            paragrafos = celula.paragraphs
            _definir_texto(paragrafos[0], str(valor))
            for extra in paragrafos[1:]:
                extra._element.getparent().remove(extra._element)


RE_DATA_EXTENSO = re.compile(r"\d{1,2} de [a-zç]+ de \d{4}", re.I)


def _atualizar_data_cabecalhos(doc, quando):
    """A data do template privado fica numa caixa de texto do cabeçalho; troca
    todo texto 'DD de mês de AAAA' encontrado em qualquer cabeçalho/rodapé."""
    from docx.oxml.ns import qn
    nova = f"{quando.day:02d} de {MESES[quando.month - 1]} de {quando.year}"
    for sec in doc.sections:
        for parte in (sec.header, sec.first_page_header, sec.even_page_header,
                      sec.footer, sec.first_page_footer, sec.even_page_footer):
            # Acessar ._element de uma parte "linked to previous" faz o python-docx
            # criar um cabeçalho/rodapé vazio que não existia no template.
            if parte.is_linked_to_previous:
                continue
            for t in parte._element.iter(qn("w:t")):
                if t.text and RE_DATA_EXTENSO.search(t.text):
                    t.text = RE_DATA_EXTENSO.sub(nova, t.text)


def _achar_paragrafo(doc, *inicios, contem=None):
    for p in doc.paragraphs:
        t = p.text.strip()
        if any(t.upper().startswith(i.upper()) for i in inicios) or (contem and contem.upper() in t.upper()):
            return p
    return None


def signatario(sid):
    for s in config.SIGNATARIOS:
        if s["id"] == sid:
            return s
    return config.SIGNATARIOS[0]


def signatarios():
    """Lista para a tela, dizendo se a imagem da assinatura existe."""
    return [dict(s, tem_imagem=_imagem_assinatura(s) is not None) for s in config.SIGNATARIOS]


def _imagem_assinatura(s):
    if not s.get("imagem"):
        return None
    p = config.ASSINATURAS_DIR / s["imagem"]
    return p if p.is_file() else None


def _aplicar_assinatura(doc, s):
    """Monta o bloco de assinatura no padrão da IPD, igual nos dois templates:
    Atenciosamente, / nome / cargo / ASSOCIAÇÃO ... - IPD / CNPJ — centralizado.
    Substitui o bloco que vem no template (nome do 1º signatário até o CNPJ)."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm
    modelo = config.SIGNATARIOS[0]["nome"]
    pars = doc.paragraphs
    i = next((k for k, par in enumerate(pars) if par.text.strip() == modelo), None)
    if i is None:
        return False
    inicio = i
    for k in range(i - 1, max(i - 4, -1), -1):
        if pars[k].text.strip().lower().startswith("atenciosamente"):
            inicio = k
            break
    fim = next((k for k in range(i, min(i + 6, len(pars))) if config.CNPJ_IPD in pars[k].text), i + 1)
    fonte = next((r.font for r in pars[i].runs if r.text.strip()), None)
    nome_fonte, tamanho = (fonte.name, fonte.size) if fonte else (None, None)
    ancora = pars[fim]
    linhas = [("Atenciosamente,", False), ("", False), (s["nome"], False), (s["cargo"], False),
              (config.RAZAO_SOCIAL_ASSINATURA, True), ("CNPJ: " + config.CNPJ_IPD, True)]
    imagem = _imagem_assinatura(s)
    for texto, negrito in linhas:
        novo = ancora.insert_paragraph_before()
        pf = novo.paragraph_format
        pf.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pf.left_indent = pf.first_line_indent = Cm(0)
        pf.space_before = pf.space_after = 0
        if texto == "" and imagem:
            novo.add_run().add_picture(str(imagem), height=Cm(1.5))
            continue
        run = novo.add_run(texto)
        run.bold = negrito
        run.italic = False
        if nome_fonte:
            run.font.name = nome_fonte
        if tamanho:
            run.font.size = tamanho
    for par in pars[inicio:fim + 1]:
        par._element.getparent().remove(par._element)
    return True


def gerar(dados, editar=None):
    """dados: {tipo, numero, ano, cliente, contato, objeto, modalidade, signatario, itens:[{mnemonico, descricao, qtd, codigo_sus, valor}], data}
    editar: pasta (relativa a PROPOSTAS_SAIDA) de uma proposta já gerada — gera
    de novo no mesmo lugar e com o mesmo número; a versão anterior vai para
    _versoes_anteriores."""
    tipo = dados["tipo"]
    if tipo not in config.TEMPLATES_PROPOSTA:
        raise ValueError("tipo de proposta inválido")
    template = config.TEMPLATES_PROPOSTA[tipo]
    if not template.is_file():
        raise FileNotFoundError(f"template não encontrado: {template}")
    doc = Document(str(template))
    try:
        numero, ano = int(dados["numero"]), int(dados["ano"])
    except (TypeError, ValueError) as e:
        raise ValueError("número e ano da proposta são inválidos") from e
    if not 1 <= numero <= 999:
        raise ValueError("o número da proposta deve estar entre 1 e 999")
    if not 2020 <= ano <= 2100:
        raise ValueError("o ano da proposta é inválido")
    cliente = dados["cliente"].strip()
    if not cliente:
        raise ValueError("informe o cliente")
    contato = (dados.get("contato") or "").strip()
    itens = dados["itens"]
    if not itens:
        raise ValueError("adicione ao menos um item")
    for item in itens:
        try:
            qtd = float(item.get("qtd") or 1)
            valor = float(item["valor"])
        except (TypeError, ValueError, KeyError) as e:
            raise ValueError("há item com quantidade ou valor inválido") from e
        if not math.isfinite(qtd) or not math.isfinite(valor) or qtd <= 0 or valor < 0:
            raise ValueError("quantidade deve ser positiva e valor não pode ser negativo")
    total = sum(float(i["valor"]) * float(i.get("qtd") or 1) for i in itens)
    # Tabela completa é lista de preços, não orçamento: soma não faz sentido.
    sem_total = str(dados.get("tipo_orcamento") or "").startswith("tabela_")
    quando = dados.get("data") or date.today()
    if isinstance(quando, str):  # veio do proposta.json
        try:
            quando = date.fromisoformat(quando[:10])
        except ValueError:
            quando = date.today()

    if tipo == "publica":
        p = _achar_paragrafo(doc, "COTAÇÃO  Nº", "COTAÇÃO Nº", "COTACAO")
        if p:
            _definir_texto(p, f"COTAÇÃO  Nº {numero:03d}/{ano}")
        p = _achar_paragrafo(doc, "Para:")
        if p:
            _substituir_prefixo(p, "Para: ", cliente)
        p = _achar_paragrafo(doc, "A/C")
        if p:
            if contato:
                _definir_texto(p, f"A/C {contato}")
            else:
                p._element.getparent().remove(p._element)
        p = _achar_paragrafo(doc, "Objeto:")
        if p:
            _substituir_prefixo(p, "Objeto: ", dados.get("objeto") or config.OBJETO_PADRAO)
        tabela = next((t for t in doc.tables if t.rows and "SEQ" in t.rows[0].cells[0].text.upper()), None)
        if tabela is None:
            raise ValueError("não achei a tabela de itens (cabeçalho SEQ) no template")
        linhas = [(f"{i:02d}", it["mnemonico"].upper(), it["descricao"],
                   f"{fmt_quantidade(it.get('qtd'))} UND", fmt_moeda(float(it["valor"])))
                  for i, it in enumerate(itens, start=1)]
        _preencher_tabela(tabela, linhas)
        p = _achar_paragrafo(doc, "TOTAL DO ORÇAMENTO")
        if p and sem_total:
            p._element.getparent().remove(p._element)
        elif p:
            _definir_texto(p, f"TOTAL DO ORÇAMENTO/PROPOSTA: R$ {fmt_moeda(total)}")
        p = _achar_paragrafo(doc, config.CIDADE_EMISSAO + ",")
        if p:
            _definir_texto(p, data_extenso(quando))
    else:
        p = _achar_paragrafo(doc, "PROPOSTA ")
        if p:
            _definir_texto(p, f"PROPOSTA {numero:03d} - {ano} – {cliente}")
        # No template o nome do cliente vem no parágrafo logo após o "A".
        pars = doc.paragraphs
        for i, par in enumerate(pars):
            if par.text.strip() == "A" and i + 1 < len(pars):
                _definir_texto(pars[i + 1], cliente)
                break
        p = _achar_paragrafo(doc, "A/C")
        if p:
            if contato:
                _definir_texto(p, f"A/C {contato}")
            else:
                p._element.getparent().remove(p._element)
        tabela = next((t for t in doc.tables if t.rows and "MNEM" in t.rows[0].cells[0].text.upper()), None)
        if tabela is None:
            raise ValueError("não achei a tabela de itens (cabeçalho Mnemônico) no template")
        linhas = [(it["mnemonico"].upper(), it["descricao"], fmt_quantidade(it.get("qtd")),
                   it.get("codigo_sus") or "", "R$ " + fmt_moeda(float(it["valor"])))
                  for it in itens]
        _preencher_tabela(tabela, linhas)
        p = _achar_paragrafo(doc, contem="Valores extraídos")
        if p:
            modalidade = dados.get("modalidade")
            nome_tab = config.MODALIDADES.get(modalidade) or (
                config.TABELAS_ADICIONAIS.get(modalidade, {}).get("nome", ""))
            nome_tab = nome_tab.replace("Empresas e Médicos — ", "Empresas ") if nome_tab else "IPD"
            if dados.get("patologia") in config.PATOLOGIAS:
                nome_tab += " e " + config.TABELAS_ADICIONAIS[config.PATOLOGIAS[dados["patologia"]]]["nome"].split(" (")[0]
            texto = f"  Valores extraídos da tabela vigente {nome_tab}."
            if not sem_total:
                texto += f" Valor Total do Orçamento: R$ {fmt_moeda(total)}"
            _definir_texto(p, texto)

    _atualizar_data_cabecalhos(doc, quando)
    _aplicar_assinatura(doc, signatario(dados.get("signatario")))

    if editar:
        return _regravar(doc, dados, editar, numero, ano, cliente, tipo, quando, total)

    # --- salvar: reserva o número antes de criar qualquer arquivo. O catálogo é
    # conferido DEPOIS da reserva: antes dela, duas gerações simultâneas com o
    # mesmo número passariam as duas pela conferência.
    reserva = _reservar_numero(numero, ano)
    slug = nome_arquivo_seguro(cliente)[:60].rstrip(" .")
    try:
        if any(i["numero"] == numero and i["ano"] == ano for i in catalogo()):
            raise ValueError(f"a proposta {numero:03d}/{ano} já existe; escolha outro número")
        pasta = config.PROPOSTAS_SAIDA / f"{numero:03d}-{ano} - {slug}"
        pasta.mkdir(parents=True, exist_ok=True)
        prefixo = "Cotacao" if tipo == "publica" else "Proposta"
        docx_path = pasta / f"{prefixo} {numero:03d}-{ano} - {slug}.docx"
        if docx_path.exists():
            raise ValueError(f"o arquivo da proposta {numero:03d}/{ano} já existe")
        doc.save(str(docx_path))
        _salvar_dados(pasta, dados, quando)
        return _resultado(pasta, docx_path, converter_pdf(docx_path), total)
    finally:
        reserva.unlink(missing_ok=True)


def _resultado(pasta, docx_path, pdf_path, total):
    base = config.PROPOSTAS_SAIDA.resolve()
    rel = lambda p: p.resolve().relative_to(base).as_posix()  # noqa: E731
    return {"pasta": str(pasta), "pasta_rel": rel(pasta), "docx": str(docx_path), "docx_rel": rel(docx_path),
            "pdf": str(pdf_path) if pdf_path else None, "pdf_rel": rel(pdf_path) if pdf_path else None, "total": total}


# --- editar uma proposta já gerada ----------------------------------------------

def _salvar_dados(pasta, dados, quando):
    guardar = {k: dados.get(k) for k in ("tipo", "numero", "ano", "cliente", "contato", "objeto",
                                         "modalidade", "patologia", "signatario", "itens") + CAMPOS_CLASSIF}
    guardar["data"] = quando.isoformat()
    try:
        (pasta / ARQ_DADOS).write_text(json.dumps(guardar, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _pasta_proposta(rel):
    """Pasta de uma proposta gerada pela Central, sem sair de PROPOSTAS_SAIDA."""
    base = config.PROPOSTAS_SAIDA.resolve()
    try:
        pasta = (base / rel).resolve()
        pasta.relative_to(base)
    except (ValueError, OSError) as e:
        raise ValueError("proposta não encontrada") from e
    if pasta == base or not pasta.is_dir() or PASTA_ANTERIORES in pasta.parts or PASTA_EXCLUIDAS in pasta.parts:
        raise ValueError("proposta não encontrada")
    return pasta


def excluir(rel):
    """Tira uma proposta gerada pela Central do catálogo: a pasta vai para
    _excluidas (dá para recuperar) e o número dela volta a ficar livre.
    Devolve (pasta antiga relativa, pasta nova)."""
    pasta = _pasta_proposta(rel)
    base = config.PROPOSTAS_SAIDA.resolve()
    if pasta.parent != base:
        raise ValueError("só dá para excluir a proposta inteira")
    destino = base / PASTA_EXCLUIDAS / f"{pasta.name} (excluída {datetime.now():%Y-%m-%d %H%M%S})"
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(pasta), str(destino))
    except OSError as e:
        raise ValueError("não consegui excluir — feche o PDF/Word dessa proposta e tente de novo") from e
    return pasta.relative_to(base).as_posix(), destino


def _pasta_editavel(p):
    """Pasta (relativa) de uma proposta da Central que dá para reabrir, ou None."""
    try:
        rel = p.parent.resolve().relative_to(config.PROPOSTAS_SAIDA.resolve())
    except (ValueError, OSError):
        return None
    if not rel.parts:
        return None
    return rel.as_posix() if (p.parent / ARQ_DADOS).is_file() or _docx_da_pasta(p.parent) else None


def recentes(n=6):
    """Últimas propostas geradas pela Central (pastas em PROPOSTAS_SAIDA), mais nova primeiro."""
    base = config.PROPOSTAS_SAIDA
    if not base.is_dir():
        return []
    itens = []
    for pasta in base.iterdir():
        docx = _docx_da_pasta(pasta) if pasta.is_dir() and pasta.name not in (PASTA_ANTERIORES, PASTA_EXCLUIDAS) else None
        if docx is None:
            continue
        pdf = docx.with_suffix(".pdf")
        quando = datetime.fromtimestamp(max(docx.stat().st_mtime,
                                            pdf.stat().st_mtime if pdf.is_file() else 0))
        rel = base.resolve()
        itens.append({
            "titulo": "Proposta " + pasta.name,
            "quando": quando,
            "pasta_rel": pasta.resolve().relative_to(rel).as_posix(),
            "arquivo_rel": (pdf if pdf.is_file() else docx).resolve().relative_to(rel).as_posix(),
        })
    itens.sort(key=lambda x: x["quando"], reverse=True)
    return itens[:n]


def _docx_da_pasta(pasta):
    return next((d for d in sorted(pasta.glob("*.docx")) if not d.name.startswith("~$")), None)


def carregar_para_editar(rel):
    """Dados de uma proposta já gerada, para reabrir na tela e acrescentar itens."""
    pasta = _pasta_proposta(rel)
    arq = pasta / ARQ_DADOS
    if arq.is_file():
        dados = json.loads(arq.read_text(encoding="utf-8"))
    else:
        docx_path = _docx_da_pasta(pasta)
        if not docx_path:
            raise ValueError("essa proposta não tem .docx para reabrir")
        dados = _ler_docx(docx_path)
    dados["pasta_rel"] = pasta.relative_to(config.PROPOSTAS_SAIDA.resolve()).as_posix()
    return dados


def _num_br(texto):
    s = str(texto).replace("R$", "").replace(" ", "").strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _ler_docx(docx_path):
    """Propostas geradas antes de existir o proposta.json: lê do próprio .docx."""
    info = _interpretar_nome(docx_path.name) or {}
    doc = Document(str(docx_path))
    tipo = "publica" if docx_path.name.lower().startswith("cota") else "privada"
    dados = {"tipo": tipo, "numero": info.get("numero"), "ano": info.get("ano"),
             "cliente": info.get("cliente", ""), "contato": "", "objeto": config.OBJETO_PADRAO,
             "modalidade": None, "signatario": config.SIGNATARIOS[0]["id"], "itens": []}
    p = _achar_paragrafo(doc, "A/C")
    if p:
        dados["contato"] = p.text.strip()[3:].strip()
    if tipo == "publica":
        p = _achar_paragrafo(doc, "Para:")
        if p:
            dados["cliente"] = p.text.split(":", 1)[1].strip()
        p = _achar_paragrafo(doc, "Objeto:")
        if p:
            dados["objeto"] = p.text.split(":", 1)[1].strip()
        tabela = next((t for t in doc.tables if t.rows and "SEQ" in t.rows[0].cells[0].text.upper()), None)
        for row in (tabela.rows[1:] if tabela else []):
            c = [x.text.strip() for x in row.cells]
            if len(c) >= 5 and (c[1] or c[2]):
                dados["itens"].append({"mnemonico": c[1], "descricao": c[2],
                                       "qtd": _num_br((c[3].split() or ["1"])[0]) or 1,
                                       "codigo_sus": "", "valor": _num_br(c[4]) or 0})
    else:
        pars = doc.paragraphs
        for i, par in enumerate(pars):
            if par.text.strip() == "A" and i + 1 < len(pars):
                dados["cliente"] = pars[i + 1].text.strip()
                break
        tabela = next((t for t in doc.tables if t.rows and "MNEM" in t.rows[0].cells[0].text.upper()), None)
        for row in (tabela.rows[1:] if tabela else []):
            c = [x.text.strip() for x in row.cells]
            if len(c) >= 5 and (c[0] or c[1]):
                dados["itens"].append({"mnemonico": c[0], "descricao": c[1], "qtd": _num_br(c[2]) or 1,
                                       "codigo_sus": c[3], "valor": _num_br(c[4]) or 0})
    for s in config.SIGNATARIOS:
        if any(par.text.strip() == s["nome"] for par in doc.paragraphs):
            dados["signatario"] = s["id"]
    return dados


def _regravar(doc, dados, rel, numero, ano, cliente, tipo, quando, total):
    """Gera de novo na mesma pasta; a versão anterior vai para _versoes_anteriores."""
    pasta = _pasta_proposta(rel)
    antigo = _docx_da_pasta(pasta)
    info = _interpretar_nome(antigo.name) if antigo else None
    if info and (info["numero"], info["ano"]) != (numero, ano):
        raise ValueError("ao editar, o número e o ano da proposta não mudam")
    anteriores = pasta / PASTA_ANTERIORES / datetime.now().strftime("%Y%m%d-%H%M%S")
    movidos = []
    try:
        for arq in list(pasta.iterdir()):
            if arq.is_file() and not arq.name.startswith("~$") and arq.suffix.lower() in (".docx", ".pdf", ".json"):
                anteriores.mkdir(parents=True, exist_ok=True)
                shutil.move(str(arq), str(anteriores / arq.name))
                movidos.append(arq)
    except OSError as e:
        for arq in movidos:  # desfaz: a proposta fica como estava
            shutil.move(str(anteriores / arq.name), str(arq))
        raise ValueError("não consegui substituir os arquivos — feche o PDF/Word dessa proposta e tente de novo") from e
    slug = nome_arquivo_seguro(cliente)[:60].rstrip(" .")
    prefixo = "Cotacao" if tipo == "publica" else "Proposta"
    docx_path = pasta / f"{prefixo} {numero:03d}-{ano} - {slug}.docx"
    doc.save(str(docx_path))
    _salvar_dados(pasta, dados, quando)
    return _resultado(pasta, docx_path, converter_pdf(docx_path), total)


def nome_arquivo_seguro(nome):
    """Limpa um componente de nome sem destruir letras acentuadas."""
    nome = unicodedata.normalize("NFC", str(nome or ""))
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", nome)
    nome = re.sub(r"\s+", " ", nome).strip(" .")
    return nome or "sem-nome"


def localizar_soffice():
    for c in config.SOFFICE_CANDIDATOS:
        if Path(c).is_file():
            return c
    return shutil.which("soffice")


def converter_pdf(docx_path):
    """PDF pelo LibreOffice (mesmo conversor do "Padronizar arquivo"); se ele não
    estiver instalado ou falhar, tenta pelo Microsoft Word do próprio PC."""
    from core import padronizar
    pdf = docx_path.with_suffix(".pdf")
    try:
        padronizar.docx_para_pdf(docx_path, pdf)
    except (ValueError, OSError):
        pass
    return pdf if pdf.is_file() else None


def word_disponivel():
    """Word instalado? (registro do Windows, sem abrir o programa)."""
    if os.name != "nt":
        return False
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"))
        return True
    except OSError:
        return False
