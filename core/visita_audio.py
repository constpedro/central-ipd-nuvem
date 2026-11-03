# core/visita_audio.py — Visita por áudio: transcreve o áudio (Whisper) e lança a visita como tarefa no GesMkt
"""
Visita por áudio.

1. O áudio (gravado na própria Central ou vindo do WhatsApp: .ogg/.opus/.webm/.mp3/.m4a)
   é transcrito pelo faster-whisper, rodando nesta máquina. O modelo é baixado só no
   primeiro uso (WHISPER_MODELO no ambiente; padrão "small") e o áudio é apagado assim
   que vira texto.
2. Do texto saem sugestões para os campos (roteiro "Cliente: ... Pedido: ... Objeção: ...
   Próximo passo: ligar dia 5").
3. Na hora de lançar, a visita vira tarefa concluída dentro da negociação do cliente; se
   o cliente ainda não tem negociação, cria conta + negociação no Saúde Privada / Prospecte.
   O próximo passo vira uma tarefa agendada na mesma negociação.

A API pública do GesMkt (/api/v1) não tem tarefas nem contas, então aqui usamos a API do
próprio site (POST /api/crm {action, data}), com o mesmo login do .env da automacao-crm.
"""
import os
import re
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests

import config

SITE = "https://gesmkt-foggueiira.vercel.app"
FUNIL_NOVO = "78d74d7c-2553-4b00-aabc-7fa862eb39bb"   # SAÚDE PRIVADA
ETAPA_NOVO = "b4e3ad41-19ec-4b37-a7c7-1e9bb25680b6"   # Prospecte
FUSO = "-03:00"
EXTENSOES = (".ogg", ".opus", ".oga", ".webm", ".mp3", ".m4a", ".wav", ".mp4", ".aac", ".amr")
PASTA_AUDIOS = config.CACHE_DIR / "visita_audio"

# ---------------------------------------------------------------- transcrição

_modelo = {"obj": None}
_lock_modelo = threading.Lock()   # um áudio por vez: o Whisper já usa todos os núcleos
_trabalhos = {}                   # id -> {"status", "texto", "erro", "inicio", "posicao"}
_fila_lock = threading.Lock()


def whisper_instalado():
    try:
        import faster_whisper  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


def _carregar_modelo():
    if _modelo["obj"] is None:
        from faster_whisper import WhisperModel  # noqa: PLC0415
        nome = os.environ.get("WHISPER_MODELO", "small")
        _modelo["obj"] = WhisperModel(nome, device="cpu", compute_type="int8",
                                      download_root=str(config.CACHE_DIR / "whisper"))
    return _modelo["obj"]


def _transcrever(tid, caminho):
    t = _trabalhos[tid]
    try:
        with _lock_modelo:
            t["status"] = "carregando" if _modelo["obj"] is None else "transcrevendo"
            modelo = _carregar_modelo()
            t["status"] = "transcrevendo"
            partes, info = modelo.transcribe(str(caminho), language="pt", vad_filter=True, beam_size=5)
            textos = []
            for p in partes:
                textos.append(p.text.strip())
                t["progresso"] = min(99, int(100 * p.end / max(info.duration, 1)))
        t["texto"] = " ".join(x for x in textos if x)
        t["status"] = "pronto"
    except Exception as e:  # noqa: BLE001 — formato estranho, modelo sem internet etc.
        t["status"], t["erro"] = "erro", str(e)
    finally:
        try:
            Path(caminho).unlink()   # só o texto fica guardado
        except OSError:
            pass


def iniciar_transcricao(arq):
    """Guarda o áudio enviado e começa a transcrição em segundo plano. Devolve o id."""
    ext = Path(arq.filename or "").suffix.lower() or ".webm"
    if ext not in EXTENSOES:
        raise ValueError("formato de áudio não aceito: " + ext)
    PASTA_AUDIOS.mkdir(parents=True, exist_ok=True)
    tid = uuid.uuid4().hex
    caminho = PASTA_AUDIOS / (tid + ext)
    arq.save(caminho)
    with _fila_lock:
        for k in [k for k, v in _trabalhos.items() if time.time() - v["inicio"] > 6 * 3600]:
            del _trabalhos[k]
        _trabalhos[tid] = {"status": "na fila", "texto": "", "erro": "", "inicio": time.time(), "progresso": 0}
    threading.Thread(target=_transcrever, args=(tid, caminho), daemon=True).start()
    return tid


def situacao(tid):
    t = _trabalhos.get(tid)
    if t is None:
        return None
    r = dict(t)
    if t["status"] == "pronto":
        r["sugestoes"] = sugerir(t["texto"])
    return r


# ---------------------------------------------------------------- roteiro falado

def _sem_acento(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


_ROTULOS = {
    "cliente": r"cliente",
    "pedido": r"pedido|demanda|interesse",
    "objecao": r"obje[cç](?:ao|oes|ão|ões)|dificuldade",
    "proximo": r"pr[oó]ximo passo|pr[oó]ximos passos|retorno",
}
_MESES = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto",
          "setembro", "outubro", "novembro", "dezembro"]
_SEMANA = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]


def _data_do_texto(texto, hoje=None):
    """'ligar dia 5', 'em 12/11', 'amanhã', 'semana que vem', 'na sexta' -> 'AAAA-MM-DD' (ou '')."""
    hoje = hoje or datetime.now().date()
    t = _sem_acento(texto)
    if "depois de amanha" in t:
        return str(hoje + timedelta(days=2))
    if "amanha" in t:
        return str(hoje + timedelta(days=1))
    if "semana que vem" in t or "proxima semana" in t:
        return str(hoje + timedelta(days=7 - hoje.weekday()))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m:
        d, mes = int(m.group(1)), int(m.group(2))
        ano = int(m.group(3)) if m.group(3) else hoje.year
        ano += 2000 if ano < 100 else 0
        try:
            data = datetime(ano, mes, d).date()
            if not m.group(3) and data < hoje:
                data = data.replace(year=ano + 1)
            return str(data)
        except ValueError:
            pass
    m = re.search(r"\bdia (\d{1,2})(?: de (" + "|".join(_MESES) + r"))?\b", t)
    if m:
        d = int(m.group(1))
        mes = _MESES.index(m.group(2)) + 1 if m.group(2) else hoje.month
        for soma in range(0, 13):
            ano, mm = hoje.year + (mes - 1 + soma) // 12, (mes - 1 + soma) % 12 + 1
            try:
                data = datetime(ano, mm, d).date()
            except ValueError:
                continue
            if data >= hoje:
                return str(data)
    for i, nome in enumerate(_SEMANA):
        if re.search(r"\b" + nome + r"(?:-feira| feira)?\b", t):
            return str(hoje + timedelta(days=(i - hoje.weekday() - 1) % 7 + 1))
    return ""


def sugerir(texto):
    """Campos tirados do roteiro falado. O que não foi dito fica vazio."""
    achados = []
    for campo, padrao in _ROTULOS.items():
        for m in re.finditer(r"(?i)\b(?:" + padrao + r")\s*(?:[:\-–,]|é\b)?\s*", texto):
            achados.append((m.start(), m.end(), campo))
    achados.sort()
    campos = {k: "" for k in _ROTULOS}
    for i, (_ini, fim, campo) in enumerate(achados):
        prox = achados[i + 1][0] if i + 1 < len(achados) else len(texto)
        if not campos[campo]:
            campos[campo] = texto[fim:prox].strip(" .,;:-–")
    if campos["cliente"]:
        campos["cliente"] = re.split(r"[.,;]", campos["cliente"])[0].strip()[:120]
    campos["data_proximo"] = _data_do_texto(campos["proximo"]) if campos["proximo"] else ""
    return campos


# ---------------------------------------------------------------- GesMkt (site)

def _ler_env(chave):
    pasta = config.CRM_DIR
    if str(pasta) not in sys.path:
        sys.path.insert(0, str(pasta))
    from renovacoes import ler_env  # noqa: PLC0415 — vem do projeto automacao-crm
    return ler_env(chave)


class Site:
    """Sessão no site do GesMkt (cookie). Refaz o login sozinho se a sessão cair."""

    _sessao = None
    _lock = threading.Lock()

    @classmethod
    def _entrar(cls):
        s = requests.Session()
        email, senha = _ler_env("GESMKT_EMAIL"), _ler_env("GESMKT_SENHA")
        if not email or not senha:
            raise RuntimeError("Coloque GESMKT_EMAIL e GESMKT_SENHA no .env da automacao-crm.")
        r = s.post(SITE + "/api/auth/login", json={"email": email, "senha": senha, "manter_conectado": True}, timeout=60)
        if not r.ok or not r.json().get("ok"):
            raise RuntimeError(f"Login no GesMkt falhou ({r.status_code}): {r.text[:200]}")
        cls._sessao = s
        return s

    @classmethod
    def pedir(cls, metodo, caminho, **kw):
        with cls._lock:
            s = cls._sessao or cls._entrar()
        r = s.request(metodo, SITE + caminho, timeout=90, **kw)
        if r.status_code == 401:
            with cls._lock:
                s = cls._entrar()
            r = s.request(metodo, SITE + caminho, timeout=90, **kw)
        try:
            d = r.json()
        except ValueError:
            raise RuntimeError(f"GesMkt {r.status_code}: resposta inesperada") from None
        if not r.ok or not d.get("ok"):
            raise RuntimeError(d.get("error") or f"GesMkt {r.status_code}")
        return d.get("data")

    @classmethod
    def acao(cls, acao, dados):
        return cls.pedir("POST", "/api/crm", json={"action": acao, "data": dados})


_cache_base = {"quando": 0, "dados": None}


def base():
    """Funis e usuários do site (cache de 10 min)."""
    c = _cache_base
    if c["dados"] and time.time() - c["quando"] < 600:
        return c["dados"]
    d = Site.pedir("GET", "/api/crm", params={"area": "deals", "pipeline_id": FUNIL_NOVO, "limit": 1})
    c["dados"] = {"funis": [{"id": f["id"], "nome": f["nome"]} for f in d["pipelines"] if f.get("ativo", True)],
                  "usuarios": sorted(({"id": u["id"], "nome": u["nome"]} for u in d["users"] if u.get("ativo", True)),
                                     key=lambda u: u["nome"].lower()),
                  "eu": d.get("capabilities", {}).get("actorId", "")}
    c["quando"] = time.time()
    return c["dados"]


def buscar(termo):
    """Negociações abertas cujo cliente/título contém o termo, em todos os funis."""
    termo = (termo or "").strip()
    if len(termo) < 3:
        return []
    funis = base()["funis"]

    def num_funil(f):
        d = Site.pedir("GET", "/api/crm", params={"area": "deals", "pipeline_id": f["id"], "limit": 15, "search": termo})
        etapas = {e["id"]: e["nome"] for e in d.get("stages", [])}
        return [{"id": n["id"], "titulo": n.get("titulo") or "", "conta_id": n.get("conta_id") or "",
                 "cliente": (n.get("conta") or {}).get("nome") or n.get("titulo") or "",
                 "funil": f["nome"], "etapa": (n.get("etapa") or {}).get("nome") or etapas.get(n.get("etapa_id"), ""),
                 "status": n.get("status", ""), "responsavel_id": n.get("responsavel_id") or ""}
                for n in d.get("deals", [])]

    with ThreadPoolExecutor(max_workers=6) as ex:
        achados = [n for lista in ex.map(num_funil, funis) for n in lista]
    achados.sort(key=lambda n: (n["status"] != "aberta", n["cliente"].lower()))
    return achados[:40]


def _id(resposta, *chaves):
    """O site devolve o registro salvo; o id pode vir direto ou dentro de uma chave."""
    if isinstance(resposta, dict):
        if resposta.get("id"):
            return resposta["id"]
        for k in chaves:
            if isinstance(resposta.get(k), dict) and resposta[k].get("id"):
                return resposta[k]["id"]
    raise RuntimeError("o GesMkt não devolveu o id do registro criado")


def _revisao(resposta):
    if isinstance(resposta, dict):
        if "revision" in resposta:
            return resposta["revision"]
        for v in resposta.values():
            if isinstance(v, dict) and "revision" in v:
                return v["revision"]
    return 1


def registrar(d):
    """Lança a visita. d: negociacao_id/conta_id (ou cliente_novo), tipo, assunto, descricao,
    responsavel_id, data_visita, proximo, data_proximo. Devolve o resumo do que foi criado."""
    resp = d.get("responsavel_id") or base()["eu"]
    feito = []
    neg, conta = d.get("negociacao_id", ""), d.get("conta_id", "")
    if not neg:
        nome = (d.get("cliente_novo") or "").strip()
        if not nome:
            raise ValueError("escolha a negociação do cliente ou digite o nome de um cliente novo")
        conta = _id(Site.acao("save_account", {"nome": nome}), "account", "conta")
        neg = _id(Site.acao("save_deal", {"titulo": nome[:180], "conta_id": conta, "funil_id": FUNIL_NOVO,
                                          "etapa_id": ETAPA_NOVO, "responsavel_id": resp}), "deal", "negociacao")
        feito.append("conta e negociação criadas no Saúde Privada / Prospecte")
    tipo = d.get("tipo") if d.get("tipo") in ("visita", "ligacao", "tarefa") else "visita"
    descricao = (d.get("descricao") or "").strip()
    quando = (d.get("data_visita") or datetime.now().strftime("%Y-%m-%d")) + "T" + datetime.now().strftime("%H:%M") + ":00" + FUSO
    r = Site.acao("save_task", {"negociacao_id": neg, "conta_id": conta, "assunto": (d.get("assunto") or "Visita Presencial")[:180],
                                "descricao": descricao, "tipo": tipo, "agendada_em": quando, "responsavel_ids": [resp]})
    tarefa = _id(r, "task", "tarefa")
    Site.acao("complete_task", {"id": tarefa, "expected_revision": _revisao(r), "resultado": descricao[:4000]})
    feito.append({"visita": "visita registrada", "ligacao": "ligação registrada"}.get(tipo, "tarefa registrada") + " e concluída")
    proximo = (d.get("proximo") or "").strip()
    if proximo:
        data = d.get("data_proximo") or str(datetime.now().date() + timedelta(days=7))
        Site.acao("save_task", {"negociacao_id": neg, "conta_id": conta, "assunto": proximo[:180], "descricao": proximo,
                                "tipo": d.get("tipo_proximo") if d.get("tipo_proximo") in ("visita", "ligacao", "tarefa") else "tarefa",
                                "agendada_em": data + "T09:00:00" + FUSO, "responsavel_ids": [resp]})
        feito.append("próximo passo agendado para " + datetime.strptime(data, "%Y-%m-%d").strftime("%d/%m/%Y"))
    return {"negociacao_id": neg, "feito": feito, "link": SITE + "/crm/negociacoes"}


# ---------------------------------------------------------------- fila do celular (nuvem -> PC)
# Na nuvem não há Whisper (não cabe nos 512 MB do Render) nem login do GesMkt (decisão do
# usuário): o texto vem do ditado do próprio navegador e a visita fica guardada no banco.
# A Central do PC busca essas visitas, a pessoa confere o cliente e lança no GesMkt; ao
# lançar, o PC avisa a nuvem para tirar a visita da fila.

PREFIXO_FILA = "visitas_celular/"
CAMPOS_FILA = ("texto", "cliente", "tipo", "data_visita", "assunto", "pedido", "objecao", "proximo", "data_proximo", "quem")


def guardar_pendente(d):
    """(nuvem) Guarda a visita ditada no celular até o PC lançar no GesMkt."""
    from core import banco  # noqa: PLC0415
    if not (d.get("texto") or "").strip():
        raise ValueError("o texto da visita está vazio")
    if not (d.get("cliente") or "").strip():
        raise ValueError("digite o nome do cliente")
    vid = uuid.uuid4().hex[:12]
    item = {k: str(d.get(k) or "").strip()[:8000] for k in CAMPOS_FILA}
    item.update(id=vid, criada=datetime.now().strftime("%Y-%m-%d %H:%M"))
    banco.gravar(PREFIXO_FILA + vid, item, historico=False)
    return item


def pendentes():
    from core import banco  # noqa: PLC0415
    return sorted((v for _c, v, _a in banco.listar(PREFIXO_FILA)), key=lambda v: v.get("criada", ""))


def baixar_pendente(vid):
    from core import banco  # noqa: PLC0415
    if not re.fullmatch(r"[0-9a-f]{12}", vid or ""):
        raise ValueError("id inválido")
    banco.apagar(PREFIXO_FILA + vid)


def _env_central(chave):
    """Valor do .env da Central (ao lado do app) — NUVEM_URL / NUVEM_SENHA."""
    arq = config.APP_DIR / ".env"
    try:
        for linha in arq.read_text(encoding="utf-8").splitlines():
            k, _, v = linha.partition("=")
            if k.strip() == chave:
                return v.strip().strip('"')
    except OSError:
        pass
    return os.environ.get(chave, "")


def _nuvem():
    url = (_env_central("NUVEM_URL") or "https://central-ipd.onrender.com").rstrip("/")
    senha = _env_central("NUVEM_SENHA")
    if not senha:
        raise RuntimeError("falta NUVEM_SENHA no .env da Central para buscar as visitas do celular")
    return url, {"X-Chave-Fila": senha}


def pendentes_da_nuvem():
    """(PC) Visitas ditadas no celular que ainda não foram para o GesMkt."""
    url, cab = _nuvem()
    # a nuvem grátis dorme: o primeiro acesso pode levar quase 1 minuto
    r = requests.get(url + "/api/visita-audio/fila", headers=cab, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"a nuvem respondeu {r.status_code}")
    return r.json()


def baixar_na_nuvem(vid):
    url, cab = _nuvem()
    r = requests.post(url + "/api/visita-audio/fila/" + vid + "/feita", headers=cab, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"a nuvem respondeu {r.status_code}")
