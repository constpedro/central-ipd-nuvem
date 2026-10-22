# -*- coding: utf-8 -*-
"""Guarda os dados da versão na nuvem (config.NUVEM).

Lá o disco do servidor é apagado a cada reinício. O que precisa durar — tabelas,
modelos .docx e as propostas geradas — fica num repositório de dados PRIVADO do
Hugging Face: é baixado para data/ quando a Central sobe, e cada proposta gerada
ou editada é enviada na hora. Assim a numeração continua de onde parou.

No PC (sem HF_DATASET/HF_TOKEN) nada disso roda.
"""
import os
import shutil
from pathlib import Path

import config

REPO = os.environ.get("HF_DATASET", "").strip()
TOKEN = os.environ.get("HF_TOKEN", "").strip()
ATIVO = bool(config.NUVEM and REPO and TOKEN)


def baixar_dados():
    """Traz o repositório de dados para data/ (tabelas.json, templates/, propostas/...)."""
    if not ATIVO:
        return
    from huggingface_hub import snapshot_download
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=REPO, repo_type="dataset", token=TOKEN, local_dir=str(config.DATA_DIR))
    # PDFs dos relatórios guardados (ficam em saida/ no repositório): voltam para a pasta de saída
    guardados = config.DATA_DIR / "saida"
    if guardados.is_dir():
        config.SAIDA_DIR.mkdir(parents=True, exist_ok=True)
        for arq in guardados.iterdir():
            if arq.is_file():
                shutil.copy2(arq, config.SAIDA_DIR / arq.name)


def guardar_relatorios(nomes):
    """Envia os PDFs de relatório recém-gerados (config.SAIDA_DIR) para saida/ no repositório."""
    if not ATIVO or not nomes:
        return
    from huggingface_hub import CommitOperationAdd, HfApi
    ops = [CommitOperationAdd(path_in_repo=f"saida/{n}", path_or_fileobj=str(config.SAIDA_DIR / n))
           for n in nomes if (config.SAIDA_DIR / n).is_file()]
    if ops:
        HfApi(token=TOKEN).create_commit(repo_id=REPO, repo_type="dataset", operations=ops,
                                         commit_message=f"Relatórios ({len(ops)})")


def guardar_pasta(pasta, mensagem):
    """Envia uma pasta de data/ (ex.: a de uma proposta) para o repositório."""
    if not ATIVO:
        return
    from huggingface_hub import HfApi
    pasta = Path(pasta).resolve()
    HfApi(token=TOKEN).upload_folder(
        repo_id=REPO, repo_type="dataset", folder_path=str(pasta),
        path_in_repo=pasta.relative_to(config.DATA_DIR.resolve()).as_posix(), commit_message=mensagem)


def excluir_pasta(rel_antiga, pasta_nova, mensagem):
    """Proposta excluída: apaga a pasta antiga do repositório e guarda a cópia em _excluidas."""
    if not ATIVO:
        return
    from huggingface_hub import HfApi
    api = HfApi(token=TOKEN)
    antiga = (config.PROPOSTAS_SAIDA.resolve() / rel_antiga).relative_to(config.DATA_DIR.resolve()).as_posix()
    api.delete_folder(path_in_repo=antiga, repo_id=REPO, repo_type="dataset", commit_message=mensagem)
    guardar_pasta(pasta_nova, mensagem + " (cópia em _excluidas)")


def guardar_cache():
    """Guarda as consultas já feitas ao SISCAN/PPI (data/cache/siscan e ppi). Sem isso
    cada relatório na nuvem baixaria de novo os meses do estado inteiro (~1 min a mais).
    Só sobe o que mudou."""
    if not ATIVO:
        return
    from huggingface_hub import HfApi
    api = HfApi(token=TOKEN)
    for nome in ("siscan", "ppi"):
        pasta = config.CACHE_DIR / nome
        if pasta.is_dir() and any(pasta.iterdir()):
            api.upload_folder(repo_id=REPO, repo_type="dataset", folder_path=str(pasta),
                              path_in_repo=f"cache/{nome}", commit_message=f"Cache {nome}")


def guardar_historico():
    """Envia o histórico da Fila (data/jobs) (os antigos demais são descartados ao carregar)."""
    if not ATIVO:
        return
    from huggingface_hub import HfApi
    pasta = config.DATA_DIR / "jobs"
    if pasta.is_dir():
        HfApi(token=TOKEN).upload_folder(repo_id=REPO, repo_type="dataset", folder_path=str(pasta),
                                         path_in_repo="jobs", allow_patterns="*.json",
                                         commit_message="Histórico da Fila")
