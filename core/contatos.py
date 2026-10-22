"""
Contatos do município: Secretaria de Saúde (CNES) e prefeitura (CNPJ na Receita).

Fontes abertas e sem login:
  - CNES (apidadosabertos.saude.gov.br): estabelecimento tipo 68 — "Central de gestão
    em saúde", que é a Secretaria Municipal de Saúde. Traz telefone, e-mail,
    endereço, coordenadas e o CNPJ da prefeitura (entidade mantenedora).
  - BrasilAPI (cadastro da Receita): telefones e e-mail do CNPJ da prefeitura.

Guardado em data/robo/contatos/<código IBGE>.json e renovado a cada VALIDADE_DIAS.
"""
import json
import math
import os
import time

import requests

import config

PASTA = config.DATA_DIR / "robo" / "contatos"
VALIDADE_DIAS = 60
URL_CNES = "https://apidadosabertos.saude.gov.br/cnes/estabelecimentos"
URL_CNPJ = "https://brasilapi.com.br/api/cnpj/v1/{}"
URL_CNPJ_WS = "https://publica.cnpj.ws/cnpj/{}"
VARGINHA = (-21.5513, -45.4303)


def _telefone(bruto):
    num = "".join(ch for ch in str(bruto or "") if ch.isdigit())
    if len(num) in (10, 11):
        return f"({num[:2]}) {num[2:-4]}-{num[-4:]}"
    return num or ""


def distancia_varginha(lat, lon):
    """Distância em linha reta (km) até Varginha."""
    if lat is None or lon is None:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (VARGINHA[0], VARGINHA[1], lat, lon))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(h)))


def _secretaria(codigo):
    r = requests.get(URL_CNES, params={"codigo_municipio": codigo, "codigo_tipo_unidade": 68, "limit": 20},
                     timeout=30)
    r.raise_for_status()
    lista = r.json().get("estabelecimentos", [])
    # A SMS é a de gestão municipal; entre várias, a que tem "SAUDE" no nome vence.
    municipais = [e for e in lista if (e.get("tipo_gestao") or "").strip() == "M"
                  or "MUNICIPAL" in (e.get("descricao_esfera_administrativa") or "")] or lista
    if not municipais:
        return None
    municipais.sort(key=lambda e: "SAUDE" not in (e.get("nome_fantasia") or "").upper())
    e = municipais[0]
    endereco = " ".join(str(e.get(k) or "") for k in ("endereco_estabelecimento", "numero_estabelecimento")).strip()
    if e.get("bairro_estabelecimento"):
        endereco += f" — {e['bairro_estabelecimento']}"
    return {
        "nome": (e.get("nome_fantasia") or "").title(),
        "telefone": _telefone(e.get("numero_telefone_estabelecimento")),
        "email": (e.get("endereco_email_estabelecimento") or "").lower(),
        "endereco": endereco.title(),
        "cnes": e.get("codigo_cnes"),
        "cnpj_prefeitura": e.get("numero_cnpj_entidade"),
        "lat": e.get("latitude_estabelecimento_decimo_grau"),
        "lon": e.get("longitude_estabelecimento_decimo_grau"),
    }


def _prefeitura(cnpj):
    if not cnpj:
        return None
    d = None
    for espera in (0, 8, 20):  # a BrasilAPI devolve 429 quando consultamos rápido demais
        time.sleep(espera)
        r = requests.get(URL_CNPJ.format(cnpj), timeout=30)
        if r.status_code == 200:
            d = r.json()
            break
        if r.status_code != 429:
            break
    if d is None:
        return _prefeitura_cnpjws(cnpj)
    fones = [_telefone(d.get(k)) for k in ("ddd_telefone_1", "ddd_telefone_2", "ddd_fax")]
    endereco = " ".join(str(d.get(k) or "") for k in ("descricao_tipo_de_logradouro", "logradouro", "numero")).strip()
    return {
        "nome": (d.get("razao_social") or "").title(),
        "telefones": [f for f in fones if f],
        "email": (d.get("email") or "").lower(),
        "endereco": endereco.title(),
        "cnpj": cnpj,
    }


def _prefeitura_cnpjws(cnpj):
    """Reserva: publica.cnpj.ws (mais lenta, ~3 consultas/min)."""
    r = requests.get(URL_CNPJ_WS.format(cnpj), timeout=30)
    if r.status_code != 200:
        return None
    d = r.json()
    e = d.get("estabelecimento") or {}
    fones = [_telefone(f"{e.get(f'ddd{i}') or ''}{e.get(f'telefone{i}') or ''}") for i in (1, 2)]
    endereco = " ".join(str(e.get(k) or "") for k in ("tipo_logradouro", "logradouro", "numero")).strip()
    return {
        "nome": (d.get("razao_social") or "").title(),
        "telefones": [f for f in fones if f],
        "email": (e.get("email") or "").lower(),
        "endereco": endereco.title(),
        "cnpj": cnpj,
    }


def _site_prefeitura(codigo):
    """Reserva para quando a Receita não tem telefone: o site oficial <cidade>.mg.gov.br.

    Procura na página inicial e na de contato telefones fixos com o DDD da cidade
    (formatados, para não pegar números soltos do código da página)."""
    import re
    from core import municipios
    m = municipios.por_codigo().get(codigo) or {}
    if not m.get("nome"):
        return None
    dominio = municipios.chave(m["nome"]) + ".mg.gov.br"
    ddd = m.get("ddd") or ""
    achados, site = [], ""
    padrao = re.compile(r"\(\s*" + ddd + r"\s*\)\s*(\d{4})[\s.-]?(\d{4})|\b" + ddd + r"[\s.-]+(\d{4})[\s.-](\d{4})\b")
    for base in (f"https://www.{dominio}", f"https://{dominio}"):
        for caminho in ("/", "/contato", "/fale-conosco"):
            try:
                r = requests.get(base + caminho, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            except requests.RequestException:
                break  # o endereço nem responde: tenta o próximo
            if r.status_code != 200:
                continue
            site = site or base
            for g in padrao.finditer(r.text):
                a, b = (g.group(1), g.group(2)) if g.group(1) else (g.group(3), g.group(4))
                fone = f"({ddd}) {a}-{b}"
                if a[0] in "2345" and len(set(a + b)) > 2 and fone not in achados:
                    achados.append(fone)
            if achados:
                return {"telefones": achados[:3], "site": base}
    return {"telefones": [], "site": site} if site else None


def _arquivo(codigo):
    return PASTA / f"{codigo}.json"


def _com_ddd(dados, codigo):
    """Telefone cadastrado sem DDD ganha o DDD da cidade."""
    from core import municipios
    ddd = (municipios.por_codigo().get(codigo) or {}).get("ddd")
    if not dados or not ddd:
        return dados

    def ajusta(f):
        num = "".join(ch for ch in f if ch.isdigit())
        return _telefone(ddd + num) if len(num) in (8, 9) else f
    if dados.get("secretaria") and dados["secretaria"].get("telefone"):
        dados["secretaria"]["telefone"] = ajusta(dados["secretaria"]["telefone"])
    if dados.get("prefeitura"):
        dados["prefeitura"]["telefones"] = [ajusta(f) for f in dados["prefeitura"].get("telefones", [])]
    return dados


def guardado(codigo):
    try:
        return _com_ddd(json.loads(_arquivo(codigo).read_text(encoding="utf-8")), codigo)
    except (OSError, ValueError):
        return None


def secretarias(codigos, paralelo=6):
    """{código: Secretaria de Saúde} de várias cidades de uma vez — o CNES aguenta consultas
    simultâneas (ao contrário da Receita), e é o que mais demora para responder."""
    from concurrent.futures import ThreadPoolExecutor

    def uma(cod):
        try:
            return cod, _secretaria(cod)
        except Exception:  # noqa: BLE001 — a busca normal tenta de novo
            return cod, False
    with ThreadPoolExecutor(max_workers=paralelo) as ex:
        return {cod: sms for cod, sms in ex.map(uma, codigos) if sms is not False}


def cnes_no_ar():
    """A API do CNES (Ministério da Saúde) está respondendo? Ela às vezes derruba toda conexão."""
    try:
        requests.get(URL_CNES, params={"limit": 1}, timeout=20).raise_for_status()
        return True
    except requests.RequestException:
        return False


def buscar(codigo, forcar=False, sms=False, sem_cnes=False):
    """Contatos do município (código IBGE de 6 dígitos), do cache quando ainda vale.

    `sms` já consultado (por secretarias()) evita consultar o CNES de novo.
    `sem_cnes` (CNES fora do ar): mantém a Secretaria já guardada, busca só a prefeitura
    pelo site e grava com ts 0 para tentar o CNES na próxima rodada."""
    atual = guardado(codigo)
    if atual and not forcar and time.time() - atual.get("ts", 0) < VALIDADE_DIAS * 86400:
        return atual
    if sem_cnes:
        sms = (atual or {}).get("secretaria")
    elif sms is False:
        sms = _secretaria(codigo)
    try:
        pref = _prefeitura(sms.get("cnpj_prefeitura") if sms else None)
    except requests.RequestException:  # Receita fora do ar: guarda a Secretaria assim mesmo
        pref = None
    if not pref or not pref.get("telefones"):
        try:
            site = _site_prefeitura(codigo)
        except Exception:  # noqa: BLE001 — o site é só reserva
            site = None
        if site:
            pref = dict(pref or {"nome": "", "email": "", "endereco": "", "cnpj": ""})
            pref["telefones"], pref["site"] = site["telefones"], site["site"]
            pref["telefones_do_site"] = bool(site["telefones"])
    # Sem a prefeitura (API fora ou limite), grava com ts antigo para tentar na próxima rodada.
    if sem_cnes and not pref and atual:
        return atual  # nada novo: não troca o que já estava guardado
    dados = {"secretaria": sms, "prefeitura": pref, "ts": int(time.time()) if pref and not sem_cnes else 0,
             "distancia_km": distancia_varginha(sms.get("lat"), sms.get("lon")) if sms else None}
    PASTA.mkdir(parents=True, exist_ok=True)
    tmp = _arquivo(codigo).with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, _arquivo(codigo))
    return _com_ddd(dados, codigo)
