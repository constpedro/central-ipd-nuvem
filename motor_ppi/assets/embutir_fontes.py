# -*- coding: utf-8 -*-
"""
Baixa as fontes IBM Plex do Google Fonts uma única vez e gera assets/fontes.css
com as fontes embutidas em base64. Os templates passam a usar esse CSS em vez do
<link> para fonts.googleapis.com — o Chrome não espera mais a internet na hora de
imprimir o PDF (isso custava 20-60 s por relatório).

Rode de novo só se quiser trocar as fontes:
    python assets/embutir_fontes.py
"""
import base64
import re
from pathlib import Path

import requests

URL = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;0,700;1,400"
       "&family=IBM+Plex+Mono:wght@500&display=swap")
# Com User-Agent de Chrome o Google devolve woff2 (menor) e só o subconjunto latin.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
SAIDA = Path(__file__).parent / "fontes.css"


def main():
    css = requests.get(URL, headers=UA, timeout=30).text
    # fica só com os blocos latin (os templates são em português; latin cobre acentos)
    blocos = re.findall(r"/\* (\w[\w-]*) \*/\s*(@font-face\s*\{.*?\})", css, re.S)
    saida = []
    for subconjunto, bloco in blocos:
        if subconjunto != "latin":
            continue
        url = re.search(r"url\((https://[^)]+)\)", bloco).group(1)
        dados = requests.get(url, headers=UA, timeout=30).content
        b64 = base64.b64encode(dados).decode("ascii")
        bloco = bloco.replace(url, f"data:font/woff2;base64,{b64}")
        saida.append(bloco)
    SAIDA.write_text("/* IBM Plex Sans / Mono embutidas — gerado por embutir_fontes.py */\n" + "\n".join(saida) + "\n",
                     encoding="utf-8")
    print(f"{len(saida)} fontes embutidas em {SAIDA} ({SAIDA.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
