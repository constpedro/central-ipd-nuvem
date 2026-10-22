# -*- coding: utf-8 -*-
"""
Automação do CRM (GesMkt; antes RD Station) dentro da Central.

O motor continua sendo o projeto Downloads/automacao-crm (renovacoes.py, crms/,
leitura.py, config.json, .env). Aqui só o chamamos como um trabalho da fila:
cada linha que ele imprime vira log, e cada "[criado] / [criaria] / [ERRO]"
vira um item na tela de andamento.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from werkzeug.security import safe_join

import config

PASTA = config.CRM_DIR
PLANILHAS = PASTA / "planilhas"

# Linha do renovacoes.py -> status do item na tela
_ITEM = re.compile(r"^\[(criado|criaria|trocado|trocaria|ERRO)\]\s+(.*)$")
_STATUS = {"criado": "ok", "trocado": "ok", "criaria": "ok", "trocaria": "ok", "ERRO": "erro"}

_cache = {"quando": 0, "crm": None, "dados": None}


def disponivel():
    return (PASTA / "renovacoes.py").is_file()


def carregar_config():
    try:
        return json.loads((PASTA / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar_config(novos):
    """Atualiza os padrões do config.json (mantém as outras chaves)."""
    cfg = carregar_config()
    for k in ("funil", "nome_card", "produto", "responsavel_id", "responsavel_nome"):
        if k in novos:
            cfg[k] = (novos[k] or "").strip()
    (PASTA / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _cache["dados"] = None
    return cfg


def arquivos(*extensoes):
    """Arquivos da pasta planilhas/, mais novos primeiro."""
    if not PLANILHAS.is_dir():
        return []
    achados = [p for p in PLANILHAS.iterdir() if p.is_file() and p.suffix.lower() in extensoes
               and not p.name.startswith("~$")]
    achados.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"nome": p.name, "ts": p.stat().st_mtime} for p in achados]


def salvar_envio(arq):
    """Guarda um arquivo enviado pelo navegador em planilhas/ e devolve o nome."""
    nome = Path(arq.filename).name.replace("/", "_").replace("\\", "_")
    if safe_join(str(PLANILHAS), nome) is None or nome in ("", ".", ".."):
        raise ValueError("nome de arquivo inválido: " + arq.filename)  # ex.: "..", "CON.xlsx"
    PLANILHAS.mkdir(parents=True, exist_ok=True)
    arq.save(PLANILHAS / nome)
    return nome


def caminho(nome):
    """Caminho de um arquivo de planilhas/ (recusa qualquer coisa fora dela).
    O nome é validado no texto antes de tocar no disco: "\\\\servidor\\x.xlsx"
    faria o Windows conectar nesse servidor só de consultar o caminho."""
    junto = safe_join(str(PLANILHAS), str(nome))
    p = Path(junto).resolve() if junto else None
    if p is None or not p.is_file() or PLANILHAS.resolve() not in p.parents:
        raise ValueError(f"arquivo não encontrado em planilhas/: {nome}")
    return p


def dados(crm, forcar=False):
    """Funis (com etapas) e usuários do CRM. Cache de 10 min — quase nunca mudam."""
    c = _cache
    if not forcar and c["crm"] == crm and c["dados"] and time.time() - c["quando"] < 600:
        return c["dados"]
    if str(PASTA) not in sys.path:
        sys.path.insert(0, str(PASTA))
    import crms  # noqa: PLC0415 — vem do projeto automacao-crm
    from renovacoes import ler_env  # noqa: PLC0415
    try:
        conexao = crms.conectar(crm, ler_env)
        resultado = {"funis": conexao.funis(),
                     "usuarios": sorted(conexao.usuarios(), key=lambda u: u["nome"].lower())}
    except SystemExit as e:  # o motor usa sys.exit para mensagens de configuração
        raise RuntimeError(str(e)) from None
    c.update(quando=time.time(), crm=crm, dados=resultado)
    return resultado


def executar(job):
    """Função de trabalho: roda o renovacoes.py e transforma a saída em andamento."""
    args = job.params["args"]
    # comando_python: no CentralIPD.exe, sys.executable é o próprio .exe — chamá-lo
    # com "-u script.py" subiria uma segunda Central em vez de rodar o script.
    cmd = config.comando_python(PASTA / "renovacoes.py", "--crm", job.params["crm"], *args)
    job.escrever("> renovacoes.py " + " ".join(f'"{a}"' if " " in a else a for a in args[0:]))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, cwd=PASTA, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    job.ao_cancelar(proc.terminate)
    contagem = {"ok": 0, "erro": 0}
    total = None
    resumo = ""
    try:
        for linha in proc.stdout:
            linha = linha.rstrip("\n")
            job.escrever(linha)
            m = re.match(r"^(\d+) cards", linha)
            if m and total is None:
                total = int(m.group(1))
                job.progresso(0, total)
            m = _ITEM.match(linha)
            if m:
                status = _STATUS[m.group(1)]
                contagem[status] += 1
                nome = m.group(2).split("  (")[0].strip()
                with job._lock:
                    job.itens.append({"nome": nome, "status": status, "segundos": None,
                                      "hora": time.strftime("%H:%M")})
                job.progresso(contagem["ok"] + contagem["erro"], max(total or 0, contagem["ok"] + contagem["erro"]))
            if linha.startswith(("Criados", "Seriam criados", "Erros:")):
                resumo = linha
        codigo = proc.wait()
    finally:
        job.ao_cancelar(None)
    if codigo and not job.cancelado:
        ultima = next((l for l in reversed(job.log) if l.strip()), "")
        raise RuntimeError(ultima or f"a automação terminou com código {codigo}")
    return {"acao": job.params["acao"], "enviar": job.params["enviar"], "ok": contagem["ok"],
            "erros": contagem["erro"], "resumo": resumo}
