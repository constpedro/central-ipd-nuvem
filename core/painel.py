"""Painel comercial: lê todas as negociações do GesMkt e entrega em formato compacto
para a página /painel montar os números.

A API do GesMkt é lenta (dezenas de segundos por chamada) e tem limite, então a última
leitura fica salva em data/cache/painel_gesmkt.json: a página abre na hora com ela e,
se tiver mais de 30 min, uma nova leitura roda em segundo plano."""
import json
import sys
import threading
import time

import config

PASTA = config.CRM_DIR
ARQ = config.CACHE_DIR / "painel_gesmkt.json"
VALIDADE = 1800
_estado = {"dados": None, "quando": 0, "lendo": False, "erro": ""}
_trava = threading.Lock()


def _ler_gesmkt():
    if str(PASTA) not in sys.path:
        sys.path.insert(0, str(PASTA))
    import crms  # noqa: PLC0415 — vem do projeto automacao-crm
    from renovacoes import ler_env  # noqa: PLC0415
    try:
        con = crms.conectar("gesmkt", ler_env)
        nomes = {u["id"]: u["nome"] for u in con.usuarios()}
        funis, deals = [], []
        for i, f in enumerate(con.funis()):
            etapas = {e["id"]: e["nome"] for e in f["etapas"]}
            funis.append(f["nome"])
            for d in con._todas("/crm/negociacoes", funil_id=f["id"]):
                # [funil, título, etapa, responsável, status(a/g/p), valor, criada, fechada, próxima atividade]
                deals.append([i, d.get("titulo") or "", etapas.get(d.get("etapa_id"), "?"),
                              nomes.get(d.get("responsavel_id"), "Sem nome"), (d.get("status") or "a")[0],
                              d.get("valor") or 0, (d.get("created_at") or "")[:10],
                              (d.get("fechado_em") or "")[:10], (d.get("proxima_atividade_em") or "")[:10]])
    except SystemExit as e:  # o motor usa sys.exit para mensagens de configuração
        raise RuntimeError(str(e)) from None
    return {"funis": funis, "deals": deals, "atualizado": time.strftime("%d/%m/%Y %H:%M")}


def _atualizar():
    try:
        dados = _ler_gesmkt()
        ARQ.parent.mkdir(parents=True, exist_ok=True)
        ARQ.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        _estado.update(dados=dados, quando=time.time(), erro="")
    except Exception as e:  # noqa: BLE001 — sem internet, login errado etc.
        _estado["erro"] = str(e)
    finally:
        _estado["lendo"] = False


def dados(forcar=False):
    """Devolve a última cópia na hora; dispara nova leitura se velha (ou se forcar)."""
    with _trava:
        if _estado["dados"] is None and ARQ.exists():
            _estado.update(dados=json.loads(ARQ.read_text(encoding="utf-8")), quando=ARQ.stat().st_mtime)
        velho = time.time() - _estado["quando"] > VALIDADE
        if (forcar or velho) and not _estado["lendo"]:
            _estado.update(lendo=True, erro="")
            threading.Thread(target=_atualizar, daemon=True).start()
    if _estado["dados"] is None:  # primeira vez na máquina: não há o que mostrar ainda
        return {"lendo": _estado["lendo"], "erro": _estado["erro"]}
    return {**_estado["dados"], "lendo": _estado["lendo"], "erro_leitura": _estado["erro"], "ocultos": ocultos()}


def _navegador():
    """Edge (vem em todo Windows) ou Chrome — usados sem janela para 'imprimir' a página em PDF."""
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for rel in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
            if base and (Path(base) / rel).is_file():
                return str(Path(base) / rel)
    return shutil.which("msedge") or shutil.which("chrome")


def gerar_pdf(url, destino):
    """Abre a página do painel (modo impressão) num navegador sem janela e salva em PDF."""
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    exe = _navegador()
    if not exe:
        raise RuntimeError("Não achei o Edge nem o Chrome neste computador para gerar o PDF.")
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as perfil:
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
                        f"--user-data-dir={perfil}", "--virtual-time-budget=15000",
                        f"--print-to-pdf={destino}", url],
                       capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not destino.is_file() or destino.stat().st_size < 1000:
        raise RuntimeError("O navegador não conseguiu gerar o PDF.")
    return destino


def ocultos():
    """Responsáveis que saíram do setor (data/painel_config.json) — valem para todos os computadores."""
    arq = config.DATA_DIR / "painel_config.json"
    try:
        return json.loads(arq.read_text(encoding="utf-8")).get("ocultar_responsaveis", [])
    except (OSError, ValueError):
        return []
