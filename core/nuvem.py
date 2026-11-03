# -*- coding: utf-8 -*-
"""Guarda os dados da versão na nuvem (config.NUVEM).

Lá o disco do servidor é apagado a cada reinício. O que precisa durar — tabelas,
modelos .docx, as propostas geradas e o banco (data/central.db) — fica num
repositório de dados PRIVADO do Hugging Face: é baixado para data/ quando a
Central sobe, e cada proposta gerada ou editada é enviada na hora (com o banco).
Assim a numeração continua de onde parou.

No PC (sem HF_DATASET/HF_TOKEN) nada disso roda.
"""
import atexit
import logging
import os
import shutil
import signal
import threading
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
    restaurar_extras()
    global _pronto
    _pronto = True  # só agora o banco é o de verdade: dali em diante pode enviar


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
    guardar_banco(mensagem)


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


def guardar_banco(mensagem="Banco de dados"):
    """Envia uma cópia consistente do banco (data/central.db): histórico da Fila, propostas, robô..."""
    if not ATIVO:
        return
    import tempfile  # noqa: PLC0415
    from huggingface_hub import HfApi
    from core import banco  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as tmp:
        copia = banco.copia_segura(Path(tmp) / "central.db")
        HfApi(token=TOKEN).upload_file(path_or_fileobj=str(copia), path_in_repo="central.db", repo_id=REPO,
                                       repo_type="dataset", commit_message=mensagem)


guardar_historico = guardar_banco  # nome antigo


# --- salvar sozinho (versão completa) ------------------------------------------
# Qualquer gravação no banco (robô, metas, contatos, CRM, fila...) agenda um envio
# para daqui a ESPERA_BANCO segundos; várias gravações seguidas viram um envio só.
# Ao desligar (o Render avisa antes de dormir/reiniciar) envia o que faltar.
ESPERA_BANCO = 20
_timer = None
_pendente = False
_pronto = False  # vira True quando baixar_dados termina (antes disso o banco não é o de verdade)
_trava = threading.Lock()


def _extras():
    """Pastas fora de data/ que também precisam durar: as listas de cidades."""
    return {"listas": (config.LISTAS_DIR, lambda p: p.suffix == ".txt")}


def agendar_banco():
    """Chamado a cada gravação no banco: envia o banco daqui a ESPERA_BANCO segundos."""
    global _timer, _pendente
    if not ATIVO or not _pronto:
        return
    with _trava:
        _pendente = True
        if _timer is None:
            _timer = threading.Timer(ESPERA_BANCO, _enviar_pendente)
            _timer.daemon = True
            _timer.start()


def _enviar_pendente():
    global _timer, _pendente
    with _trava:
        _timer = None
        if not _pendente:
            return
        _pendente = False
    try:
        guardar_banco("Alterações")
    except Exception:  # noqa: BLE001 — tenta de novo na próxima gravação ou ao desligar
        with _trava:
            _pendente = True
        logging.getLogger(__name__).exception("falha ao guardar o banco na nuvem")


def guardar_extra(nome):
    """Envia uma pasta de _extras() (ex.: listas/) para o repositório."""
    if not ATIVO:
        return
    from huggingface_hub import CommitOperationAdd, HfApi
    pasta, aceita = _extras()[nome]
    if not pasta.is_dir():
        return
    ops = [CommitOperationAdd(path_in_repo=f"{nome}/{p.relative_to(pasta).as_posix()}", path_or_fileobj=str(p))
           for p in pasta.rglob("*") if p.is_file() and aceita(p)]
    if ops:
        HfApi(token=TOKEN).create_commit(repo_id=REPO, repo_type="dataset", operations=ops,
                                         commit_message=f"Dados: {nome}")


def apagar_extra(nome, rel):
    """Apaga um arquivo de uma pasta de _extras() no repositório (ex.: lista apagada)."""
    if not ATIVO:
        return
    from huggingface_hub import HfApi
    try:
        HfApi(token=TOKEN).delete_file(path_in_repo=f"{nome}/{rel}", repo_id=REPO, repo_type="dataset",
                                       commit_message=f"Apaga {nome}/{rel}")
    except Exception:  # noqa: BLE001 — já não estava lá
        pass


def restaurar_extras():
    """Depois de baixar_dados: devolve listas/ para a pasta de verdade (motor_ppi/listas)."""
    for nome, (pasta, _aceita) in _extras().items():
        origem = config.DATA_DIR / nome
        if origem.is_dir():
            shutil.copytree(origem, pasta, dirs_exist_ok=True)


def em_segundo_plano(funcao, *args):
    """Envia sem segurar a página (o envio ao HF leva alguns segundos)."""
    if not ATIVO:
        return

    def rodar():
        try:
            funcao(*args)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("falha ao guardar na nuvem")
    threading.Thread(target=rodar, daemon=True).start()


def _ao_desligar(*_a):
    if _pendente:
        _enviar_pendente()


if ATIVO:
    atexit.register(_ao_desligar)
    try:  # o gunicorn repassa o SIGTERM do Render: envia o que faltar antes de sair
        _sinal_antigo = signal.getsignal(signal.SIGTERM)

        def _sigterm(signum, frame):
            _ao_desligar()
            if callable(_sinal_antigo):
                _sinal_antigo(signum, frame)
        signal.signal(signal.SIGTERM, _sigterm)
    except ValueError:  # fora da thread principal: fica só o atexit
        pass
