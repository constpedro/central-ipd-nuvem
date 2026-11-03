# -*- coding: utf-8 -*-
"""
Banco de dados da Central (SQLite): um arquivo só, data/central.db.

Sem servidor para instalar e o backup continua sendo copiar a pasta data/.
Três tabelas:
  documentos  dados da equipe que antes eram .json soltos em data/ (histórico da
              Fila, robô, contatos, regras de metas, relatórios de consórcio,
              configuração do Painel). A chave é o caminho antigo, relativo a
              data/ (ex.: "robo/estado.json") — assim cada módulo só troca as
              funções de ler/gravar.
  propostas   índice das propostas geradas pela Central (número, cliente,
              valor, etapa...): numeração sem repetir e base para relatórios.
              Os .docx e PDFs continuam como arquivos.
  historico   versão anterior de cada configuração alterada (metas, robô,
              Painel) e das propostas, com data: dá para ver e desfazer.

Ficam como arquivo, de propósito: os caches (dá para apagar sem perder nada),
as listas de consórcios do motor PPI e a configuração da automação do CRM (os
scripts separados leem esses arquivos).

Migração: na primeira vez que a Central liga com o banco, migrar() importa os
.json antigos; e uma leitura que não acha a chave no banco ainda procura o
arquivo antigo (e o importa). Os arquivos antigos não são apagados.
"""
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import config

ARQUIVO = config.DATA_DIR / "central.db"
VERSAO_MIGRACAO = 1

# Chaves (prefixos) cujas versões anteriores vão para o histórico ao serem trocadas.
# Fila e estado do robô ficam fora: mudam o tempo todo e não são configuração.
COM_HISTORICO = ("metas_regras.json", "painel_config.json", "robo/config.json")

_local = threading.local()
_migrado = threading.Event()  # migrar() roda uma vez por processo, na 1ª abertura do banco
_trava = threading.RLock()  # uma gravação por vez (o SQLite também trava, isto evita esperas longas)


def _criar(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS documentos (
            chave      TEXT PRIMARY KEY,
            valor      TEXT NOT NULL,
            atualizado TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS propostas (
            pasta      TEXT PRIMARY KEY,          -- nome da pasta em data/propostas
            numero     INTEGER NOT NULL,
            ano        INTEGER NOT NULL,
            cliente    TEXT,
            tipo       TEXT,
            signatario TEXT,
            etapa      TEXT,
            total      REAL,
            situacao   TEXT NOT NULL DEFAULT 'ativa',   -- ativa | excluída
            criada     TEXT NOT NULL,
            atualizada TEXT NOT NULL,
            dados      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS propostas_numero ON propostas (ano, numero);
        CREATE TABLE IF NOT EXISTS historico (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            quando TEXT NOT NULL,
            chave  TEXT NOT NULL,
            acao   TEXT NOT NULL,                  -- alterado | apagado | proposta excluída ...
            antes  TEXT
        );
        CREATE INDEX IF NOT EXISTS historico_chave ON historico (chave, id);
    """)


def _con():
    con = getattr(_local, "con", None)
    if con is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(ARQUIVO), timeout=30, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")    # leitura não espera gravação
        con.execute("PRAGMA synchronous=NORMAL")
        with _trava:
            _criar(con)
        _local.con = con
        if not _migrado.is_set():
            _migrado.set()
            migrar()
    return con


def _agora():
    return datetime.now().isoformat(timespec="seconds")


def chave_de(caminho):
    """Caminho dentro de data/ -> chave do banco ("robo/estado.json")."""
    return Path(caminho).resolve().relative_to(config.DATA_DIR.resolve()).as_posix()


def _arquivo_antigo(chave):
    return config.DATA_DIR / chave


# --- documentos -------------------------------------------------------------------

def ler(chave, padrao=None):
    """Valor guardado (dict/list/...). Sem ele no banco, importa o .json antigo se existir."""
    linha = _con().execute("SELECT valor FROM documentos WHERE chave = ?", (chave,)).fetchone()
    if linha:
        return json.loads(linha[0])
    antigo = _arquivo_antigo(chave)
    try:
        valor = json.loads(antigo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao
    gravar(chave, valor, historico=False)
    return valor


def gravar(chave, valor, historico=True):
    texto = json.dumps(valor, ensure_ascii=False, default=str)
    with _trava:
        con = _con()
        con.execute("BEGIN IMMEDIATE")
        try:
            if historico and chave.startswith(COM_HISTORICO):
                antes = con.execute("SELECT valor FROM documentos WHERE chave = ?", (chave,)).fetchone()
                if antes and antes[0] != texto:
                    con.execute("INSERT INTO historico (quando, chave, acao, antes) VALUES (?, ?, 'alterado', ?)",
                                (_agora(), chave, antes[0]))
            con.execute("INSERT INTO documentos (chave, valor, atualizado) VALUES (?, ?, ?) "
                        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor, atualizado = excluded.atualizado",
                        (chave, texto, _agora()))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    _avisar_nuvem()


def _avisar_nuvem():
    """Na nuvem o disco some ao reiniciar: agenda o envio do banco (no PC não faz nada)."""
    from core import nuvem  # noqa: PLC0415 — nuvem usa o banco; importar aqui evita o ciclo
    nuvem.agendar_banco()


def apagar(chave):
    with _trava:
        con = _con()
        con.execute("BEGIN IMMEDIATE")
        try:
            antes = con.execute("SELECT valor FROM documentos WHERE chave = ?", (chave,)).fetchone()
            if antes and chave.startswith(COM_HISTORICO):
                con.execute("INSERT INTO historico (quando, chave, acao, antes) VALUES (?, ?, 'apagado', ?)",
                            (_agora(), chave, antes[0]))
            con.execute("DELETE FROM documentos WHERE chave = ?", (chave,))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    _avisar_nuvem()
    # o .json antigo não pode "ressuscitar" o documento na próxima leitura
    try:
        _arquivo_antigo(chave).unlink(missing_ok=True)
    except OSError:
        pass


def listar(prefixo):
    """[(chave, valor, atualizado)] das chaves que começam com o prefixo, mais recente primeiro."""
    linhas = _con().execute(
        "SELECT chave, valor, atualizado FROM documentos WHERE chave >= ? AND chave < ? ORDER BY atualizado DESC, chave",
        (prefixo, prefixo + "￿")).fetchall()
    return [(c, json.loads(v), a) for c, v, a in linhas]


def contar(prefixo):
    return _con().execute("SELECT COUNT(*) FROM documentos WHERE chave >= ? AND chave < ?",
                          (prefixo, prefixo + "￿")).fetchone()[0]


# --- propostas --------------------------------------------------------------------

def registrar_proposta(pasta, dados, total=None):
    """Grava (ou atualiza, ao editar) a proposta da pasta data/propostas/<pasta>."""
    if total is None:
        total = sum(float(i.get("qtd") or 0) * float(i.get("valor") or 0) for i in dados.get("itens") or [])
    agora = _agora()
    texto = json.dumps(dados, ensure_ascii=False, default=str)
    with _trava:
        con = _con()
        con.execute("BEGIN IMMEDIATE")
        try:
            antes = con.execute("SELECT dados FROM propostas WHERE pasta = ?", (pasta,)).fetchone()
            if antes and antes[0] != texto:
                con.execute("INSERT INTO historico (quando, chave, acao, antes) VALUES (?, ?, 'alterado', ?)",
                            (agora, "propostas/" + pasta, antes[0]))
            con.execute("""
                INSERT INTO propostas (pasta, numero, ano, cliente, tipo, signatario, etapa, total, situacao, criada, atualizada, dados)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ativa', ?, ?, ?)
                ON CONFLICT(pasta) DO UPDATE SET numero = excluded.numero, ano = excluded.ano, cliente = excluded.cliente,
                    tipo = excluded.tipo, signatario = excluded.signatario, etapa = excluded.etapa, total = excluded.total,
                    situacao = 'ativa', atualizada = excluded.atualizada, dados = excluded.dados""",
                (pasta, int(dados["numero"]), int(dados["ano"]), dados.get("cliente"), dados.get("tipo"),
                 dados.get("signatario"), dados.get("etapa"), round(float(total), 2),
                 dados.get("data") or agora, agora, texto))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def dados_proposta(pasta):
    linha = _con().execute("SELECT dados FROM propostas WHERE pasta = ? AND situacao = 'ativa'", (pasta,)).fetchone()
    return json.loads(linha[0]) if linha else None


def excluir_proposta(pasta):
    with _trava:
        con = _con()
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("UPDATE propostas SET situacao = 'excluída', atualizada = ? WHERE pasta = ?", (_agora(), pasta))
            con.execute("INSERT INTO historico (quando, chave, acao) VALUES (?, ?, 'proposta excluída')",
                        (_agora(), "propostas/" + pasta))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise


def maior_numero(ano):
    """Maior número de proposta ATIVA do ano no banco (0 se nenhuma)."""
    return _con().execute("SELECT COALESCE(MAX(numero), 0) FROM propostas WHERE ano = ? AND situacao = 'ativa'",
                          (int(ano),)).fetchone()[0]


def propostas(ano=None, incluir_excluidas=False):
    """Propostas do banco (para relatórios), mais nova primeiro."""
    sql = "SELECT pasta, numero, ano, cliente, tipo, signatario, etapa, total, situacao, criada, atualizada FROM propostas"
    cond, args = [], []
    if ano:
        cond.append("ano = ?"); args.append(int(ano))  # noqa: E702
    if not incluir_excluidas:
        cond.append("situacao = 'ativa'")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    cols = ("pasta", "numero", "ano", "cliente", "tipo", "signatario", "etapa", "total", "situacao", "criada", "atualizada")
    return [dict(zip(cols, l)) for l in _con().execute(sql + " ORDER BY ano DESC, numero DESC", args)]


def historico(chave=None, limite=50):
    sql, args = "SELECT id, quando, chave, acao FROM historico", []
    if chave:
        sql += " WHERE chave = ?"; args.append(chave)  # noqa: E702
    return [dict(zip(("id", "quando", "chave", "acao"), l))
            for l in _con().execute(sql + " ORDER BY id DESC LIMIT ?", args + [limite])]


# --- migração e cópia -------------------------------------------------------------

# O que era .json solto em data/ e passa a morar no banco (padrões relativos a data/)
MIGRAR = ("metas_regras.json", "painel_config.json", "robo/*.json", "robo/contatos/*.json",
          "jobs/*.json", "consorcios/*.json")


def migrar(log=print):
    """Importa os .json antigos e as propostas geradas (uma vez; não apaga os arquivos)."""
    feita = _con().execute("SELECT valor FROM documentos WHERE chave = '_migracao'").fetchone()
    if feita and json.loads(feita[0]).get("versao", 0) >= VERSAO_MIGRACAO:
        return
    n_docs = n_prop = 0
    with _trava:
        con = _con()
        con.execute("BEGIN IMMEDIATE")
        try:
            for padrao in MIGRAR:
                for arq in sorted(config.DATA_DIR.glob(padrao)):
                    try:
                        valor = json.loads(arq.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    atualizado = datetime.fromtimestamp(arq.stat().st_mtime).isoformat(timespec="seconds")
                    n_docs += con.execute("INSERT OR IGNORE INTO documentos (chave, valor, atualizado) VALUES (?, ?, ?)",
                                          (chave_de(arq), json.dumps(valor, ensure_ascii=False), atualizado)).rowcount
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    # propostas: o proposta.json de cada pasta (as excluídas entram como excluídas)
    from core import propostas as mod  # noqa: PLC0415 — evita import circular
    base = config.PROPOSTAS_SAIDA
    if base.is_dir():
        for arq in sorted(base.glob("*/" + mod.ARQ_DADOS)) + sorted(base.glob(mod.PASTA_EXCLUIDAS + "/*/" + mod.ARQ_DADOS)):
            pasta = arq.parent.name
            if _con().execute("SELECT 1 FROM propostas WHERE pasta = ?", (pasta,)).fetchone():
                continue
            try:
                dados = json.loads(arq.read_text(encoding="utf-8"))
                registrar_proposta(pasta, dados)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if arq.parent.parent.name == mod.PASTA_EXCLUIDAS:
                _con().execute("UPDATE propostas SET situacao = 'excluída' WHERE pasta = ?", (pasta,))
            n_prop += 1
    gravar("_migracao", {"versao": VERSAO_MIGRACAO, "quando": _agora(), "documentos": n_docs, "propostas": n_prop},
           historico=False)
    if n_docs or n_prop:
        log(f"Banco de dados: {n_docs} registro(s) e {n_prop} proposta(s) importados dos arquivos antigos.")


def copia_segura(destino):
    """Cópia consistente do banco (mesmo com a Central gravando), para backup/nuvem."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    alvo = sqlite3.connect(str(destino))
    try:
        with _trava:
            _con().backup(alvo)
    finally:
        alvo.close()
    return destino


PASTA_BACKUPS = config.DATA_DIR / "backups"
MANTER_BACKUPS = 14


def backup_do_dia():
    """data/backups/central-AAAA-MM-DD.db (uma vez por dia; guarda os últimos MANTER_BACKUPS)."""
    destino = PASTA_BACKUPS / f"central-{datetime.now():%Y-%m-%d}.db"
    if destino.is_file():
        return None
    copia_segura(destino.with_suffix(".tmp"))
    destino.with_suffix(".tmp").replace(destino)
    for velho in sorted(PASTA_BACKUPS.glob("central-*.db"))[:-MANTER_BACKUPS]:
        velho.unlink(missing_ok=True)
    return destino


def iniciar_backup_diario():
    """Cópia segura do banco ao ligar e a cada 6 h (vale o 1º do dia). Copiar o central.db
    direto com a Central gravando pode pegar o arquivo pela metade; esta cópia não."""
    def laco():
        while True:
            try:
                backup_do_dia()
            except Exception:  # noqa: BLE001 — o backup nunca derruba a Central
                pass
            threading.Event().wait(6 * 3600)
    threading.Thread(target=laco, daemon=True, name="backup-banco").start()


def fechar():
    """Fecha a conexão desta thread (testes)."""
    con = getattr(_local, "con", None)
    if con is not None:
        con.close()
        _local.con = None

