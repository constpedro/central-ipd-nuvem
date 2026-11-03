"""Itens de proposta a partir de uma lista solta: texto colado (WhatsApp, e-mail,
Excel) ou print de tabela (OCR do Windows).

Cada linha com um valor em reais vira um item: o valor é o da lista (já é o
preço combinado com o cliente) e o resto da linha é usado para achar o exame
na nossa tabela — pelo mnemônico ou, se não houver, pela descrição mais
parecida. O que não for encontrado volta marcado para conferir na tela.
"""

import difflib
import re

from core import tabelas

# 1.234,56 · 1234,56 · 12.50 · R$ 12,5 — o último da linha é o valor.
# Sem centavos, só com "R$" na frente ("R$ 45", "R$ 1.200"): número solto pode ser quantidade.
_RE_VALOR = re.compile(r"(?:R\s?\$\s*)?(?<![\d.,])(\d{1,3}(?:[.\s]\d{3})+,\d{1,2}|\d+[.,]\d{1,2})(?![\d])"
                       r"|R\s?\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?![\d.,])")
_RE_QTD = re.compile(r"\b(\d{1,5})\s*(?:UND|UNID|UN|X)\b\.?", re.I)
_RE_SEQ = re.compile(r"^\s*(?:\d{1,3}[\s.)\-–]+)(?=\D)")
_RE_PREFIXO = re.compile(r"^(?:DOSAGEM|PESQUISA|DETERMINACAO|EXAME)\s+(?:DE|DA|DO|DOS|DAS)\s+")
_IGNORAR = ("TOTAL", "SUBTOTAL", "VALOR TOTAL")
# confusões comuns do OCR em mnemônico (letras e números parecidos)
_TROCA_MN = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B"})


def _chave(desc):
    """Descrição sem os complementos entre colchetes/parênteses: 'UREIA [SORO]' -> 'UREIA'."""
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", tabelas.normalizar(desc))).strip(" -")


def _candidatos(modalidade):
    """[(mnemônico, descrição, chave, penalidade)] — penalidade desempata exames de mesmo
    nome: o de soro/sem complemento ganha do hospitalar, do HP e dos outros materiais."""
    cache = tabelas.carregar()
    fontes = [(it["mnemonico"], it["descricao"]) for it in cache["itens"]]
    fontes += [(x.get("mnemonico") or "", x["descricao"]) for x in cache.get("extras", {}).get(modalidade, [])]
    saida = []
    for mn, desc in fontes:
        d = tabelas.normalizar(desc)
        penal = 2 * ("HOSP" in d or mn.upper().endswith("HP") or " HP" in d)
        # outro material (urina, líquor...) perde até do "hospitalar": nesta tabela o
        # exame comum de soro vem como "POTASSIO [SORO] (HOSPITALAR)" e a urina ganhava
        penal += 3 * ("[" in d and "[SORO]" not in d)
        saida.append((mn, desc, _chave(desc), penal + len(mn) / 100))
    return saida


def _achar(texto, por_mn, candidatos):
    """(mnemônico, descrição da tabela, 'mnemonico'|'descricao'|'', [opções]) para o texto da linha."""
    # sem acento antes de quebrar em palavras: "Proteína" virava "Prote" + "na" e o
    # "NA" (sódio) era confirmado como o exame
    tokens = re.findall(r"[A-Z0-9]+", tabelas.normalizar(texto))
    pelo_mn = None
    for t in tokens[:3]:
        for v in (t.upper(), t.upper().translate(_TROCA_MN)):
            if len(v) >= 2 and v in por_mn:
                pelo_mn = (v, por_mn[v], "mnemonico", [])
                break
        if pelo_mn:
            break
    # Linha que é só o código ("HEM 16,00"): vale o mnemônico. Com mais palavras,
    # a descrição decide antes — "T4 livre" é T4L, não o T4 total.
    if pelo_mn and len(tokens) == 1:
        return pelo_mn
    if pelo_mn:  # "HEM Hemograma": as outras palavras já estão na descrição do código
        dtoks = re.findall(r"[A-Z0-9]+", tabelas.normalizar(pelo_mn[1]))
        resto = [t for t in re.findall(r"[A-Z0-9]+", tabelas.normalizar(texto)) if t != pelo_mn[0]]
        if all(any(d.startswith(t) for d in dtoks) for t in resto):
            return pelo_mn
    q = _RE_PREFIXO.sub("", _chave(texto))
    if len(q) < 3:
        return pelo_mn or ("", "", "", [])
    # mnemônico desconhecido no começo da linha (ex.: código do cliente): tenta sem ele
    alvos = [q]
    originais = re.findall(r"[A-Za-z0-9]+", texto)  # caixa original: código vem em MAIÚSCULAS
    if len(tokens) > 1 and originais and originais[0].isupper() and len(originais[0]) <= 8:
        alvos.append(_chave(texto.split(originais[0], 1)[1]))
    nota, contem = [], {}
    for mn, desc, chave, penal in candidatos:
        if not chave:
            continue
        melhor = 0
        for alvo in alvos:
            if alvo == chave:
                melhor = 1.0
                break
            if alvo[:4] != chave[:4] and not set(alvo.split()) & set(chave.split()):
                continue  # filtro rápido: nem começo nem palavra em comum
            melhor = max(melhor, difflib.SequenceMatcher(None, alvo, chave).ratio())
            # Todas as palavras da linha estão no exame ("PSA LIVRE" em "PSAL - ...
            # LIVRE"). Só o exame comum (sem hospitalar/HP/outro material) conta.
            qtoks, ctoks = re.findall(r"[A-Z0-9]+", alvo), re.findall(r"[A-Z0-9]+", chave)
            if (penal < 1 and len(qtoks) >= 2
                    and all(any(c.startswith(t) for c in ctoks) for t in qtoks)):
                contem[mn] = (penal, mn, desc)
        if melhor >= 0.5 or mn in contem:
            nota.append((0 if mn in contem else 1, -round(melhor, 2), penal, mn, desc))
    nota.sort()
    opcoes = [{"mnemonico": mn, "descricao": desc} for _, _, _, mn, desc in nota[:6]]
    # descrição quase igual decide primeiro, como sempre (pela semelhança, não pela ordem das opções)
    certeiro = min((n for n in nota if -n[1] >= 0.8), key=lambda n: (n[1], n[2], n[3]), default=None)
    if certeiro:
        return certeiro[3], certeiro[4], "descricao", opcoes
    # um único exame com todas as palavras: é ele ("PSA livre" -> PSAL). Vários
    # ("Beta HCG": quantitativo, qualitativo, fração livre...) a pessoa escolhe na prévia.
    if len(contem) == 1:
        _, mn, desc = next(iter(contem.values()))
        return mn, desc, "descricao", opcoes
    # Começou com um código mas o resto não bate com ele ("T4 livre", "PSA total"):
    # em vez de confirmar o código, fica para conferir — com ele entre as opções.
    if pelo_mn:
        opcoes = [o for o in opcoes if o["mnemonico"] != pelo_mn[0]]
        # sem outro exame com todas as palavras, o código digitado é o mais provável
        opcoes.insert(len(contem) if contem else 0, {"mnemonico": pelo_mn[0], "descricao": pelo_mn[1]})
    return "", "", "", opcoes


def _linhas_validas(linhas):
    """Junta descrição que quebrou em duas linhas (texto sem valor + valor quase sem texto)."""
    pendente = ""
    for linha in linhas:
        linha = (linha or "").strip()
        if not linha:
            continue
        valores = list(_RE_VALOR.finditer(linha))
        if not valores:
            pendente = linha
            continue
        resto = linha[:valores[-1].start()] + linha[valores[-1].end():]
        if sum(c.isalpha() for c in resto) < 3 and pendente:
            linha = pendente + " " + linha
            valores = list(_RE_VALOR.finditer(linha))
        pendente = ""
        yield linha, valores[-1]


def ler(linhas, modalidade=""):
    """linhas de texto → [{mnemonico, descricao, qtd, valor, achou, opcoes, original}]."""
    candidatos = _candidatos(modalidade)
    por_mn = {}
    for mn, desc, _, _ in candidatos:
        if mn:
            por_mn.setdefault(mn.upper(), desc)
    itens = []
    for linha, m in _linhas_validas(linhas):
        if tabelas.normalizar(linha).startswith(_IGNORAR):
            continue
        valor = tabelas.para_numero((m.group(1) or m.group(2)).replace(" ", "."))
        texto = linha[:m.start()] + " " + linha[m.end():]
        qtd = 1
        q = _RE_QTD.search(texto)
        # coluna de quantidade: número solto logo antes do valor ("Glicose 2 R$ 8,90")
        solto = re.search(r"(?<=[A-Za-zÀ-ú)\]])\s+(\d{1,4})\s*(?:R\s?\$)?\s*$", linha[:m.start()])
        if q:
            qtd, texto = int(q.group(1)) or 1, texto[:q.start()] + " " + texto[q.end():]
        elif solto:
            qtd, texto = int(solto.group(1)) or 1, linha[:solto.start()] + " " + linha[m.end():]
        texto = _RE_SEQ.sub("", texto)
        # "R$", separadores e pontilhado de preenchimento ("Urina ...... R$ 9,50")
        texto = re.sub(r"\s+", " ", re.sub(r"R\s?\$|[|;\t]|\.{2,}|_{2,}|…", " ", texto)).strip(" -–:·.")
        if tabelas.normalizar(texto).startswith(_IGNORAR):  # "1.234,56 Total geral"
            continue
        mn, desc, achou, opcoes = _achar(texto, por_mn, candidatos)
        itens.append({"mnemonico": mn, "descricao": desc or texto, "qtd": qtd, "valor": valor,
                      "achou": achou, "opcoes": opcoes, "original": linha})
    return itens


def ler_imagem(dados, nome, modalidade=""):
    from core import ocr
    linhas = [" ".join(c for c in row if c) for row in ocr.tabela_de_imagem(dados, nome)]
    return ler(linhas, modalidade)
