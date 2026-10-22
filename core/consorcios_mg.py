"""
Consórcios Intermunicipais de Saúde de MG e seus municípios, tirados do site do
COSECS-MG (cosecsmg.org.br/consorcios): uma página por consórcio com sigla, sede,
telefone, site e a lista de "Municípios consorciados".

Guardado em data/consorcios_mg.json; o robô renova a cada VALIDADE_DIAS.
Só aparecem os consórcios membros do COSECS — os que não são ficam de fora.
"""
import html
import json
import os
import re
import time

import requests

import config
from core import metas

ARQ = config.DATA_DIR / "consorcios_mg.json"
BASE = "https://cosecsmg.org.br"
VALIDADE_DIAS = 30
_UA = {"User-Agent": "Mozilla/5.0 (Central IPD)"}


def _texto(pagina):
    pagina = re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>", "", pagina, flags=re.S)
    linhas = html.unescape(re.sub(r"<[^>]+>", "\n", pagina)).splitlines()
    return [l.strip() for l in linhas if l.strip()]


def _campo(linhas, rotulo):
    for i, l in enumerate(linhas):
        if l.rstrip(":").strip().lower() == rotulo.lower() and i + 1 < len(linhas):
            return linhas[i + 1].lstrip(": ").replace("\xa0", " ").strip()
        if l.lower().startswith(rotulo.lower() + ":") and l.split(":", 1)[1].strip():
            return l.split(":", 1)[1].strip()
    return ""


def samu(c):
    """Consórcio temático de urgência (SAMU) — cobre macrorregiões inteiras."""
    return "SAMU" in c["sigla"].upper() or "URG" in c["sigla"].upper()


def _ler_consorcio(sessao, caminho):
    r = sessao.get(BASE + caminho, timeout=30)
    r.raise_for_status()
    linhas = _texto(r.text)
    try:
        ini = next(i for i, l in enumerate(linhas) if l.lower() == "municípios consorciados")
    except StopIteration:
        ini = None
    cidades = []
    if ini is not None:
        for l in linhas[ini + 1:]:
            if l.lower().startswith("conteúdos relacionados") or len(l) > 45:
                break
            cidades.append(l)
    sede = next((l.split(":", 1)[1].strip() for l in linhas if l.lower().startswith("sede:")), "")
    return {
        "sigla": _campo(linhas, "Sigla"),
        "nome": _campo(linhas, "Nome").title(),
        "sede": sede,
        "telefone": _campo(linhas, "Telefone"),
        "site": _campo(linhas, "Site"),
        "pagina": BASE + caminho,
        "cidades": cidades,
    }


def atualizar(log=print):
    """Baixa todas as páginas do COSECS e grava data/consorcios_mg.json."""
    sessao = requests.Session()
    sessao.headers.update(_UA)
    caminhos = []
    for pag in range(1, 20):
        r = sessao.get(f"{BASE}/consorcios", params={"pagina": pag} if pag > 1 else None, timeout=30)
        r.raise_for_status()
        novos = [c for c in dict.fromkeys(re.findall(r'href="(/consorcios/[a-z0-9-]+-\d+)"', r.text))
                 if c not in caminhos and "patrocinador" not in c]
        if not novos:
            break
        caminhos += novos
    consorcios = []
    for c in caminhos:
        try:
            dados = _ler_consorcio(sessao, c)
        except requests.RequestException as e:
            log(f"{c}: não consegui ler ({e})")
            continue
        if dados["sigla"] and dados["cidades"]:
            consorcios.append(dados)
        time.sleep(0.3)
    if not consorcios:
        raise RuntimeError("o site do COSECS-MG não devolveu nenhum consórcio")
    consorcios.sort(key=lambda c: c["sigla"].upper())
    tmp = ARQ.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": int(time.time()), "consorcios": consorcios}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, ARQ)
    log(f"Consórcios de MG: {len(consorcios)} lidos do COSECS-MG.")
    return consorcios


def _ler():
    try:
        return json.loads(ARQ.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ts": 0, "consorcios": []}


def todos():
    return _ler()["consorcios"]


def vencido():
    return time.time() - _ler().get("ts", 0) > VALIDADE_DIAS * 86400


def por_cidade():
    """{slug da cidade: [siglas]} — uma cidade pode estar em mais de um (ex.: generalista + SAMU)."""
    mapa = {}
    for c in sorted(todos(), key=samu):  # generalista antes do SAMU
        for cidade in c["cidades"]:
            mapa.setdefault(metas.slug_cidade(cidade), []).append(c["sigla"])
    return mapa


def cidades_de(siglas):
    alvo = set(siglas)
    return [cid for c in todos() if c["sigla"] in alvo for cid in c["cidades"]]
