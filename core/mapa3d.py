"""Mapa 3D: atualiza os dados e as versões distribuídas pela fila de trabalhos.

Roda, no projeto minas-3d, os mesmos dois passos que se rodaria à mão:
  1. tools/preparar_dados.py  — lê a planilha consolidada, SISCAN e PPI e gera os CSVs
  2. tools/empacotar.py       — copia o mapa para central-ipd/mapa3d e gera o arquivo único
e copia o arquivo único para a Área de Trabalho.
"""
import os
import shutil
import subprocess
import sys
import time

import config

ETAPAS = [
    ("Ler planilha e fontes", ["tools/preparar_dados.py"]),
    ("Gerar mapa (Central + arquivo único)", ["tools/empacotar.py"]),
]
ARQUIVO_UNICO = "Mapa3D_PPI.html"


def situacao():
    """Datas da última atualização, para a página do mapa."""
    def mtime(p):
        return p.stat().st_mtime if p.is_file() else None
    return {
        "instalado": (config.MAPA3D_DIR / "index.html").is_file(),
        "dados_em": mtime(config.MAPA3D_DIR / "data" / "saude" / "ppi_consolidado.csv"),
        "planilha": str(config.MAPA3D_PLANILHA),
        "planilha_em": mtime(config.MAPA3D_PLANILHA),
        "arquivo_unico": str(config.MAPA3D_ARQUIVO_DESTINO),
        "arquivo_unico_em": mtime(config.MAPA3D_ARQUIVO_DESTINO),
        "projeto_ok": (config.MINAS3D_DIR / "tools" / "empacotar.py").is_file(),
    }


def _encerrar(proc):
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass


def executar(job):
    if not (config.MINAS3D_DIR / "tools" / "empacotar.py").is_file():
        raise RuntimeError(f"Projeto do mapa não encontrado em {config.MINAS3D_DIR}")
    job.definir_itens([nome for nome, _ in ETAPAS] + ["Copiar para a Área de Trabalho"])
    job.progresso(0, len(ETAPAS) + 1)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for k, (nome, args) in enumerate(ETAPAS):
        if job.cancelado:
            return {"ok": False}
        job.marcar(nome, "rodando")
        job.escrever(f"== {nome} ==")
        t0 = time.time()
        proc = subprocess.Popen(config.comando_python(*args), cwd=str(config.MINAS3D_DIR), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        job.ao_cancelar(lambda proc=proc: _encerrar(proc))
        for linha in proc.stdout:
            job.escrever(linha)
        proc.wait()
        job.ao_cancelar(None)
        if proc.returncode != 0:
            job.marcar(nome, "erro", time.time() - t0)
            raise RuntimeError(f"{nome} terminou com erro (código {proc.returncode}). Veja o log acima.")
        job.marcar(nome, "ok", time.time() - t0)
        job.progresso(k + 1, len(ETAPAS) + 1)

    nome = "Copiar para a Área de Trabalho"
    job.marcar(nome, "rodando")
    origem = config.MINAS3D_DIR / "dist" / ARQUIVO_UNICO
    shutil.copy2(origem, config.MAPA3D_ARQUIVO_DESTINO)
    job.escrever(f"Arquivo único copiado para {config.MAPA3D_ARQUIVO_DESTINO}")
    job.marcar(nome, "ok")
    job.progresso(len(ETAPAS) + 1, len(ETAPAS) + 1)
    return {"ok": True, "arquivo": str(config.MAPA3D_ARQUIVO_DESTINO)}
