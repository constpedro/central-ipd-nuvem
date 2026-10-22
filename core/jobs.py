# -*- coding: utf-8 -*-
"""
Fila de trabalhos em segundo plano.

Consultar o PPI de 50 cidades leva minutos; a tela não pode ficar travada
esperando. Cada pedido vira um Job, que entra numa fila e é executado por UMA
thread de trabalho (um de cada vez — também é gentileza com os servidores do
PPI e do TabNet). A página acompanha o andamento consultando /api/jobs/<id>.

O histórico fica também em data/jobs/<id>.json: depois de reiniciar a Central os
trabalhos anteriores continuam em "Últimos trabalhos", com log e resultado. O
que estava rodando ou na fila quando a Central caiu volta como "interrompido".
"""
import json
import os
import queue
import threading
import time
import traceback
import uuid
from datetime import datetime

import config

PASTA = config.DATA_DIR / "jobs"
MAX_HISTORICO = 100      # trabalhos guardados (memória e disco)
MAX_LOG_DISCO = 3000     # últimas linhas de log gravadas por trabalho
SALVAR_A_CADA = 10       # segundos entre gravações de um trabalho rodando
FINAIS = ("concluído", "erro", "cancelado", "interrompido")


class Job:
    def __init__(self, tipo, titulo, funcao, params):
        self.id = uuid.uuid4().hex[:10]
        self.tipo = tipo            # "lote", "consorcio", ...
        self.titulo = titulo
        self.funcao = funcao        # funcao(job) -> resultado (dict)
        self.params = params
        self.status = "na fila"     # na fila | rodando | concluído | erro | cancelado | interrompido
        self.feito = 0
        self.total = 0
        self.log = []
        # Uma linha por cidade (ou etapa), para a tela mostrar o andamento
        # sem ninguém precisar ler o log: {"nome", "status", "segundos"}.
        # status: "espera" | "rodando" | "ok" | "erro"
        self.itens = []
        self.resultado = None
        self.erro = None
        self.criado_em = datetime.now()
        self.iniciado_em = None
        self.terminado_em = None
        self._lock = threading.Lock()
        # Pedido de cancelamento: a função de trabalho consulta `cancelado`
        # e encerra o que estiver rodando (ex.: o subprocesso do gerar_ppi).
        self.cancelado = False
        self._ao_cancelar = None
        self._salvo_em = 0.0
        self._lock_disco = threading.Lock()  # uma gravação por vez (mesmo arquivo .tmp)

    # -- histórico em disco ----------------------------------------------------
    def salvar(self):
        """Grava data/jobs/<id>.json. Falha de disco não derruba o trabalho."""
        with self._lock:
            dados = {
                "id": self.id, "tipo": self.tipo, "titulo": self.titulo, "status": self.status,
                "feito": self.feito, "total": self.total, "log": self.log[-MAX_LOG_DISCO:],
                "itens": [dict(i) for i in self.itens], "resultado": self.resultado, "erro": self.erro,
                "criado_em": self.criado_em.isoformat(),
                "iniciado_em": self.iniciado_em.isoformat() if self.iniciado_em else None,
                "terminado_em": self.terminado_em.isoformat() if self.terminado_em else None,
            }
            self._salvo_em = time.monotonic()
        with self._lock_disco:
            try:
                PASTA.mkdir(parents=True, exist_ok=True)
                tmp = PASTA / f"{self.id}.json.tmp"
                tmp.write_text(json.dumps(dados, ensure_ascii=False, default=str), encoding="utf-8")
                os.replace(tmp, PASTA / f"{self.id}.json")
            except OSError:
                pass

    @classmethod
    def do_disco(cls, dados):
        """Trabalho do histórico: só para consulta (não roda de novo)."""
        job = cls(dados["tipo"], dados["titulo"], None, {})
        job.id = dados["id"]
        job.status = dados["status"]
        job.feito, job.total = dados.get("feito", 0), dados.get("total", 0)
        job.log = list(dados.get("log") or [])
        job.itens = list(dados.get("itens") or [])
        job.resultado, job.erro = dados.get("resultado"), dados.get("erro")
        job.criado_em = datetime.fromisoformat(dados["criado_em"])
        job.iniciado_em = datetime.fromisoformat(dados["iniciado_em"]) if dados.get("iniciado_em") else None
        job.terminado_em = datetime.fromisoformat(dados["terminado_em"]) if dados.get("terminado_em") else None
        if job.status not in FINAIS:
            # estava na fila ou rodando quando a Central foi fechada
            job.status = "interrompido"
            job.erro = "a Central foi fechada ou reiniciada antes de este trabalho terminar"
            job.terminado_em = job.terminado_em or job.iniciado_em
            for item in job.itens:
                if item.get("status") in ("rodando", "espera"):
                    item["status"] = "cancelada"
        return job

    def cancelar(self):
        """Pede para parar. Na fila: nem começa. Rodando: encerra o subprocesso."""
        with self._lock:
            if self.status not in ("na fila", "rodando"):
                return False
            self.cancelado = True
            callback = self._ao_cancelar
        self.escrever("Cancelamento pedido pelo usuário — para assim que terminar o passo atual.")
        if callback:
            try:
                callback()
            except Exception:  # noqa: BLE001
                pass
        return True

    def ao_cancelar(self, callback):
        """Registra o que fazer se o usuário cancelar (None desfaz)."""
        with self._lock:
            self._ao_cancelar = callback

    # -- usados pela função de trabalho --------------------------------------
    def escrever(self, linha):
        with self._lock:
            self.log.append(linha.rstrip("\n"))
            gravar = self.status == "rodando" and time.monotonic() - self._salvo_em > SALVAR_A_CADA
        if gravar:  # se a Central cair, o histórico guarda até onde o trabalho chegou
            self.salvar()

    def progresso(self, feito, total):
        with self._lock:
            self.feito = max(0, int(feito))
            self.total = max(0, int(total))

    def definir_itens(self, nomes):
        """Registra as cidades da rodada, todas ainda em espera."""
        with self._lock:
            self.itens = [{"nome": n, "status": "espera", "segundos": None} for n in nomes]

    def marcar(self, nome, status, segundos=None):
        """Muda o status de uma cidade. Ignora nome que não está na lista."""
        with self._lock:
            for item in self.itens:
                if item["nome"] == nome:
                    if item["status"] in ("ok", "erro") and status == "rodando":
                        return  # já terminou; não volta para "rodando"
                    if status == "ok" and item["status"] != "ok":
                        item["hora"] = datetime.now().strftime("%H:%M")
                    item["status"] = status
                    if segundos is not None:
                        item["segundos"] = round(segundos, 1)
                    break

    # -- usados pela interface -----------------------------------------------
    def como_dict(self, log_desde=0):
        with self._lock:
            log = self.log[log_desde:]
            n_log = len(self.log)
            itens = [dict(i) for i in self.itens]
        return {
            "id": self.id,
            "tipo": self.tipo,
            "titulo": self.titulo,
            "status": self.status,
            "feito": self.feito,
            "total": self.total,
            "percentual": round(100 * self.feito / self.total) if self.total else 0,
            "log": log,
            "n_log": n_log,
            "itens": itens,
            "resultado": self.resultado,
            "erro": self.erro,
            "criado_em": self.criado_em.strftime("%d/%m/%Y %H:%M"),
            "duracao": self.duracao(),
        }

    def duracao(self):
        if not self.iniciado_em:
            return ""
        fim = self.terminado_em or datetime.now()
        seg = int((fim - self.iniciado_em).total_seconds())
        return f"{seg // 60}min {seg % 60:02d}s" if seg >= 60 else f"{seg}s"


class GerenciadorJobs:
    def __init__(self):
        self.jobs = {}
        self.ordem = []
        self._lock = threading.Lock()
        self.fila = queue.Queue()
        self._carregar_historico()
        self._thread = threading.Thread(target=self._trabalhar, daemon=True)
        self._thread.start()

    def _carregar_historico(self):
        """Trabalhos das execuções anteriores da Central (data/jobs)."""
        if not PASTA.is_dir():
            return
        carregados = []
        for p in PASTA.glob("*.json"):
            try:
                dados = json.loads(p.read_text(encoding="utf-8"))
                job = Job.do_disco(dados)
            except (OSError, ValueError, KeyError, TypeError):
                continue  # arquivo corrompido: ignora
            if dados.get("status") not in FINAIS:  # acabou de virar "interrompido"
                job.log.append("INTERROMPIDO: " + job.erro)
                job.salvar()
            carregados.append(job)
        carregados.sort(key=lambda j: j.criado_em, reverse=True)
        for job in carregados:
            self.jobs[job.id] = job
            self.ordem.append(job.id)
        for tmp in PASTA.glob("*.json.tmp"):
            tmp.unlink(missing_ok=True)
        self._podar()

    def _podar(self):
        """Mantém só os MAX_HISTORICO mais recentes (na memória e no disco)."""
        with self._lock:
            sobra = [i for i in self.ordem[MAX_HISTORICO:] if self.jobs[i].status in FINAIS]
            for i in sobra:
                self.ordem.remove(i)
                del self.jobs[i]
        for i in sobra:
            (PASTA / f"{i}.json").unlink(missing_ok=True)

    def enfileirar(self, tipo, titulo, funcao, params=None):
        job = Job(tipo, titulo, funcao, params or {})
        with self._lock:
            self.jobs[job.id] = job
            self.ordem.insert(0, job.id)
        job.salvar()
        self.fila.put(job)
        self._podar()
        return job

    def obter(self, job_id):
        return self.jobs.get(job_id)

    def recentes(self, n=8):
        with self._lock:
            return [self.jobs[i] for i in self.ordem[:n]]

    def ativos(self):
        """Trabalhos rodando ou na fila, do mais antigo para o mais novo."""
        with self._lock:
            return [self.jobs[i] for i in reversed(self.ordem)
                    if self.jobs[i].status in ("na fila", "rodando")]

    def _trabalhar(self):
        while True:
            job = self.fila.get()
            if job.cancelado:
                job.status = "cancelado"
                job.terminado_em = datetime.now()
                job.salvar()
                self.fila.task_done()
                continue
            job.status = "rodando"
            job.iniciado_em = datetime.now()
            job.salvar()
            try:
                job.resultado = job.funcao(job)
                job.status = "cancelado" if job.cancelado else "concluído"
            except Exception as e:  # noqa: BLE001 — qualquer falha vira status "erro"
                job.status = "cancelado" if job.cancelado else "erro"
                job.erro = str(e)
                job.escrever("ERRO: " + str(e))
                job.escrever(traceback.format_exc())
            finally:
                job.terminado_em = datetime.now()
                job.salvar()
                self.fila.task_done()
            self._guardar_na_nuvem()
            time.sleep(0.2)

    def _guardar_na_nuvem(self):
        """Na nuvem o disco é apagado ao reiniciar: guarda o histórico (data/jobs) junto das propostas."""
        from core import nuvem  # aqui dentro: nuvem não precisa existir no import dos jobs
        try:
            nuvem.guardar_historico()
        except Exception:  # noqa: BLE001 — o trabalho já terminou; só o histórico não sobrevive
            pass

    def recarregar_historico(self):
        """Na nuvem o histórico chega depois (baixado do repositório): junta o que ainda não está aqui."""
        with self._lock:
            ja = set(self.jobs)
        if not PASTA.is_dir():
            return
        for p in PASTA.glob("*.json"):
            if p.stem in ja:
                continue
            try:
                job = Job.do_disco(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError):
                continue
            with self._lock:
                self.jobs[job.id] = job
                self.ordem.append(job.id)
        with self._lock:
            self.ordem.sort(key=lambda i: self.jobs[i].criado_em, reverse=True)
        self._podar()


gerenciador = GerenciadorJobs()
