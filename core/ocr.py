"""Leitura de texto em imagens (OCR) com o reconhecedor que já vem no Windows.

Usado pelo Padronizar: a foto de uma tabela vira linhas e colunas de verdade,
que seguem o mesmo caminho de uma planilha (Excel, Word e PDF editáveis).
Não precisa de internet nem de instalar nada além dos pacotes winrt do pip.
"""

import asyncio
import io

LARGURA_MIN = 2000   # fotos pequenas são ampliadas: o OCR erra muito em letra miúda
LADO_MAX = 9000      # limite do OcrEngine é 10.000 px


def _motor():
    # pacotes do pip --user: a pasta pode ter sido criada depois que a Central abriu.
    # No CentralIPD.exe os pacotes winrt vão dentro do build (e o "site" do
    # PyInstaller nem tem getusersitepackages).
    import sys
    if not getattr(sys, "frozen", False):
        try:
            import site
            usuario = site.getusersitepackages()
            if usuario not in sys.path:
                sys.path.append(usuario)
        except (ImportError, AttributeError):
            pass
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
    except ImportError:
        raise ValueError("o leitor de texto (OCR) não está instalado neste computador — "
                         "rode: python -m pip install --user winrt-runtime winrt-Windows.Media.Ocr "
                         "winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams "
                         "winrt-Windows.Globalization winrt-Windows.Foundation")
    motor = OcrEngine.try_create_from_language(Language("pt-BR")) or \
        OcrEngine.try_create_from_user_profile_languages()
    if motor is None:
        raise ValueError("o Windows não tem o reconhecimento de texto em português instalado")
    return motor


async def _reconhecer(motor, png):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    w = DataWriter(stream)
    w.write_bytes(png)
    await w.store_async()
    await w.flush_async()
    w.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    return await motor.recognize_async(bitmap)


def _preparar(img):
    from PIL import ImageOps

    img = ImageOps.exif_transpose(img).convert("L")
    larg, alt = img.size
    escala = max(1.0, LARGURA_MIN / larg)
    escala = min(escala, LADO_MAX / max(larg, alt))
    if abs(escala - 1) > 0.01:
        img = img.resize((round(larg * escala), round(alt * escala)))
    return ImageOps.autocontrast(img)


def _palavras(img):
    """[(x0, y0, x1, y1, texto)] de todas as palavras reconhecidas."""
    motor = _motor()

    def rodar(im):
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return asyncio.run(_reconhecer(motor, buf.getvalue()))

    res = rodar(img)
    angulo = res.text_angle
    # foto tirada torta: endireita e lê de novo
    if angulo is not None and abs(angulo) > 0.5:
        img = img.rotate(angulo, expand=True, fillcolor=255)
        res = rodar(img)
    saida = []
    for linha in res.lines:
        for p in linha.words:
            r = p.bounding_rect
            saida.append((r.x, r.y, r.x + r.width, r.y + r.height, p.text))
    return saida


def _agrupar_linhas(palavras):
    """Junta palavras na mesma altura (centro vertical dentro de meia letra)."""
    if not palavras:
        return [], 1
    alturas = sorted(p[3] - p[1] for p in palavras)
    letra = alturas[len(alturas) // 2] or 1
    linhas = []
    for p in sorted(palavras, key=lambda p: (p[1] + p[3]) / 2):
        meio = (p[1] + p[3]) / 2
        if linhas and abs(meio - linhas[-1]["meio"]) <= letra * 0.6:
            l = linhas[-1]
            l["palavras"].append(p)
            l["meio"] = sum((q[1] + q[3]) / 2 for q in l["palavras"]) / len(l["palavras"])
        else:
            linhas.append({"meio": meio, "palavras": [p]})
    return [sorted(l["palavras"], key=lambda p: p[0]) for l in linhas], letra


def _celulas(linha, letra):
    """Palavras próximas viram uma célula; um vão maior que ~1 letra separa células."""
    celulas = []
    for p in linha:
        if celulas and p[0] - celulas[-1][2] <= letra * 1.1:
            c = celulas[-1]
            celulas[-1] = (c[0], c[1], p[2], c[3] + " " + p[4])
        else:
            celulas.append((p[0], 0, p[2], p[4]))
    return [(c[0], c[2], c[3]) for c in celulas]


def _colunas(linhas_celulas, largura):
    """Limites de coluna = faixas do eixo x que quase nenhuma célula ocupa."""
    ocupa = [0] * (largura + 2)
    for cels in linhas_celulas:
        for x0, x1, _ in cels:
            for x in range(max(0, int(x0)), min(largura + 1, int(x1) + 1)):
                ocupa[x] += 1
    tolera = max(0, len(linhas_celulas) // 20)   # títulos largos podem cruzar o vão
    faixas, dentro, ini = [], False, 0
    for x, n in enumerate(ocupa):
        if n > tolera and not dentro:
            dentro, ini = True, x
        elif n <= tolera and dentro:
            dentro = False
            faixas.append((ini, x))
    if dentro:
        faixas.append((ini, len(ocupa)))
    return faixas


_TROCA_NUM = str.maketrans({"o": "0", "O": "0", "l": "1", "I": "1", "S": "5"})


def _corrigir_numero(texto):
    """'1.200,oo' → '1.200,00': em célula que é quase toda número, letra parecida vira dígito."""
    import re
    if re.fullmatch(r"[R$\s]*[\doOlIS.,%\-]+", texto) and sum(c.isdigit() for c in texto) >= len(texto.replace(" ", "")) / 2:
        return texto.translate(_TROCA_NUM)
    return texto


def tabela_de_imagem(dados, nome="imagem"):
    """bytes de PNG/JPEG → [[texto da célula]] (linhas × colunas)."""
    from PIL import Image

    try:
        img = _preparar(Image.open(io.BytesIO(dados)))
    except Exception:  # noqa: BLE001
        raise ValueError(f"'{nome}' não é uma imagem PNG/JPEG válida")
    palavras = _palavras(img)
    if not palavras:
        raise ValueError(f"não encontrei texto legível em '{nome}' — tente uma foto mais nítida e reta")
    linhas, letra = _agrupar_linhas(palavras)
    por_linha = [_celulas(l, letra) for l in linhas]
    faixas = _colunas(por_linha, img.size[0] + int(letra * 10))
    if not faixas:  # nenhuma faixa comum a várias linhas (texto solto): uma coluna só
        faixas = [(0, img.size[0] + int(letra * 10))]

    tabela = []
    for cels in por_linha:
        linha = [""] * len(faixas)
        for x0, x1, texto in cels:
            meio = (x0 + x1) / 2
            i = min(range(len(faixas)),
                    key=lambda k: 0 if faixas[k][0] <= meio <= faixas[k][1]
                    else min(abs(meio - faixas[k][0]), abs(meio - faixas[k][1])))
            linha[i] = (linha[i] + " " + texto).strip()
        tabela.append([_corrigir_numero(c) for c in linha])
    return tabela
