# -*- coding: utf-8 -*-
"""
Central IPD — servidor local.

    python app.py            (ou dois cliques em RODAR.bat)

Abre em http://127.0.0.1:5077. Tudo roda nesta máquina: as consultas ao PPI e ao
SISCAN, a geração dos PDFs, das planilhas por consórcio e das propostas.
"""
import hmac
import ipaddress
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from werkzeug.security import safe_join

if len(sys.argv) > 2 and sys.argv[1] == "--rodar-script":
    # CentralIPD.exe chamado como "python" pelos relatórios em lote e pelo Mapa 3D.
    import runpy
    script = Path(sys.argv[2]).resolve()
    sys.argv = sys.argv[2:]
    sys.path.insert(0, str(script.parent))
    for fluxo in (sys.stdout, sys.stderr):
        if fluxo:
            fluxo.reconfigure(encoding="utf-8", line_buffering=True)
    runpy.run_path(str(script), run_name="__main__")
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402
from core import consorcios, crm as crm_mod, lote, mapa3d as mapa3d_mod, metas, nuvem, padronizar, ppi_engine, propostas, tabelas, teto_ppi  # noqa: E402
from core.jobs import gerenciador  # noqa: E402

NOME_MAQUINA = socket.gethostname()


def _chave_sessao():
    """Chave aleatória gerada na 1ª execução e guardada em data/. Uma chave fixa
    no código deixaria qualquer pessoa da rede forjar a sessão autenticada."""
    arq = config.DATA_DIR / ".chave_sessao"
    try:
        chave = arq.read_text(encoding="ascii").strip()
        if len(chave) >= 32:
            return chave
    except OSError:
        pass
    chave = secrets.token_hex(32)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    arq.write_text(chave, encoding="ascii")
    return chave


app = Flask(__name__, root_path=str(config.APP_DIR))
app.config["TEMPLATES_AUTO_RELOAD"] = True  # mudanças nos templates valem sem reiniciar o servidor
# Lax: o navegador não manda o cookie da sessão em formulários vindos de outro site.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Teto do envio: sem ele um upload enorme fica inteiro na memória do servidor.
# (o Padronizar recusa arquivo acima de 20 MB; aqui cabem várias imagens juntas)
# Arquivos estáticos ficam guardados no navegador por 1 dia (app.css/app.js levam
# ?v= da versão, então uma mudança chega na hora). Pelo celular, cada ida ao servidor custa.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
# Campos de texto do formulário (padrão do Flask: 500 KB). Uma proposta "Tabela
# completa" manda ~2 mil exames (~240 KB hoje); 5 MB dá folga para tabelas maiores.
app.config["MAX_FORM_MEMORY_SIZE"] = 5 * 1024 * 1024
app.secret_key = (os.environ.get("FLASK_SECRET") or secrets.token_hex(32)) if config.NUVEM else _chave_sessao()
if config.NUVEM:
    # atrás do proxy HTTPS do servidor: o Flask precisa saber que o acesso é https
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config["SESSION_COOKIE_SECURE"] = True
    app.permanent_session_lifetime = timedelta(days=90)
if not config.NUVEM:
    app.permanent_session_lifetime = timedelta(days=30)
app.jinja_env.trim_blocks = True
app.jinja_env.lstrip_blocks = True


# --- acesso pela rede -------------------------------------------------------------

_ips_cache = {"quando": 0.0, "ips": frozenset()}


def _ips_desta_maquina():
    """IPs deste PC. Guardados por 60 s: a consulta ao Windows rodaria em toda
    página aberta de outro PC (duas vezes por página)."""
    if time.monotonic() - _ips_cache["quando"] > 60:
        ips = {"127.0.0.1", "::1"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                ips.add(info[4][0])
        except OSError:
            pass
        _ips_cache.update(quando=time.monotonic(), ips=frozenset(ips))
    return _ips_cache["ips"]


def _acesso_local():
    """Quem abre a Central neste PC — por 127.0.0.1 ou pelo IP da rede (192.168...)."""
    if config.NUVEM:
        return False  # na nuvem ninguém é "deste PC": todos passam pela senha
    ip = request.remote_addr or ""
    if ip in ("127.0.0.1", "::1"):
        return True
    # o IP pode mudar (DHCP): a lista é refeita a cada minuto
    return ip in _ips_desta_maquina()


def _nome_aceito(host):
    """O endereço digitado no navegador é um dos nomes desta Central?

    Barra o "DNS rebinding": um site malicioso aberto neste PC faz o próprio
    domínio passar a apontar para 127.0.0.1 e, como aqui não se pede senha, leria
    propostas, tabelas e CRM — e até o token dos formulários. Nesse ataque o
    navegador sempre manda o nome do site atacante; IP digitado, localhost e o
    nome do PC são seguros."""
    nome = (urlsplit("//" + (host or "")).hostname or "").lower().rstrip(".")
    if not nome:
        return False
    try:
        ipaddress.ip_address(nome)
        return True  # IP digitado (127.0.0.1, 192.168...) não sofre rebinding
    except ValueError:
        pass
    maquina = NOME_MAQUINA.lower()
    aceitos = {"localhost", maquina, maquina + ".local"} | {h.lower() for h in getattr(config, "NOMES_EXTRAS", [])}
    return nome in aceitos or nome.endswith(".localhost")


@app.before_request
def _conferir_nome():
    if config.NUVEM:
        return None  # na nuvem todo acesso passa pela senha: o "DNS rebinding" não tem o que explorar
    if not _nome_aceito(request.host):
        return ("Endereço não reconhecido pela Central IPD. Abra pelo nome do computador "
                f"({NOME_MAQUINA}), pelo IP ou, neste PC, por http://127.0.0.1:{request.environ.get('SERVER_PORT', config.PORT)}/"
                " — se a TI criou outro nome para a Central, acrescente-o em NOMES_EXTRAS no config.py."), 400


# Versão na nuvem (celular): só Relatórios PPI/SISCAN, Fila, Propostas e Tabelas.
ENDPOINTS_NUVEM = {
    "login", "sair", "static", "inicio", "ver_job", "cancelar_job", "api_job",
    "relatorios", "listas", "fila", "api_fila", "abrir_saida", "ver_pasta", "pasta_arquivo", "abrir_pasta",
    "propostas_pagina", "proposta_abrir", "proposta_nova", "proposta_editar", "proposta_excluir", "proposta_gerada_arquivo",
    "api_proposta_importar", "tabelas_pagina", "api_tabelas_busca", "api_tabelas_completa",
    "tabelas_conferencia", "conferencia_arquivo",
}


@app.before_request
def _so_areas_da_nuvem():
    if config.NUVEM and request.endpoint not in ENDPOINTS_NUVEM:
        abort(404)


@app.before_request
def _exigir_senha():
    """Quem acessa de outro PC precisa da senha de config.SENHA_ACESSO."""
    if config.NUVEM and not config.SENHA_ACESSO and request.endpoint != "static":
        # sem senha configurada no servidor a Central NÃO abre (nunca fica pública)
        return "Central sem senha configurada no servidor (SENHA_ACESSO).", 503
    if not config.SENHA_ACESSO or _acesso_local() or session.get("autorizado"):
        return None
    if request.endpoint in ("login", "static"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"erro": "faça login"}), 401
    return redirect(url_for("login", proximo=request.full_path.rstrip("?")))


def _token_csrf():
    """Token da sessão que todo formulário POST da Central carrega (campo _csrf).
    Um site de fora até consegue mandar um formulário para cá — quem acessa deste
    PC nem precisa de senha —, mas não tem como saber o token."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = _token_csrf


def _mesmo_site(url):
    return bool(url) and urlsplit(url).netloc == request.host


@app.before_request
def _conferir_csrf():
    if request.method != "POST" or request.endpoint in ("login", "static"):
        return None
    enviado = request.form.get("_csrf", "") or request.headers.get("X-CSRF", "")
    if enviado and hmac.compare_digest(enviado.encode("utf-8"), _token_csrf().encode("utf-8")):
        return None
    # Também acontece com uma página aberta há muito tempo (a sessão mudou).
    flash("Envio bloqueado por segurança: a página estava desatualizada. Confira os dados e envie de novo.", "erro")
    return redirect(request.referrer if _mesmo_site(request.referrer) else url_for("inicio"))


# Tentativas de senha erradas por IP: depois de MAX_TENTATIVAS em JANELA_TENTATIVAS
# segundos, o IP espera BLOQUEIO_LOGIN segundos. Sem isso um programa na rede
# testaria milhares de senhas por minuto.
MAX_TENTATIVAS, JANELA_TENTATIVAS, BLOQUEIO_LOGIN = 5, 600, 300
_falhas_login = {}
_falhas_lock = threading.Lock()


def _segundos_bloqueado(ip):
    agora = time.monotonic()
    with _falhas_lock:
        falhas = [t for t in _falhas_login.get(ip, []) if agora - t < JANELA_TENTATIVAS]
        _falhas_login[ip] = falhas
        if len(falhas) >= MAX_TENTATIVAS:
            return max(0, int(BLOQUEIO_LOGIN - (agora - falhas[-1])))
    return 0


def _registrar_falha(ip):
    with _falhas_lock:
        _falhas_login.setdefault(ip, []).append(time.monotonic())


@app.route("/login", methods=["GET", "POST"])
def login():
    proximo = request.values.get("proximo", "")
    # "/\site.com" o navegador lê como "//site.com" — levaria para fora da Central
    if not proximo.startswith("/") or proximo.startswith(("//", "/\\")):
        proximo = url_for("inicio")
    if request.method == "POST":
        ip = request.remote_addr or ""
        espera = _segundos_bloqueado(ip)
        senha = request.form.get("senha", "")
        if espera:
            flash(f"muitas tentativas erradas — aguarde {max(1, -(-espera // 60))} min e tente de novo", "erro")
        # em bytes: compare_digest recusa (TypeError) str com acento, ex.: "senhá"
        elif hmac.compare_digest(senha.encode("utf-8"), config.SENHA_ACESSO.encode("utf-8")):
            with _falhas_lock:
                _falhas_login.pop(ip, None)
            session.clear()  # sessão nova a cada login (não reaproveita a anterior)
            session.permanent = True
            session["autorizado"] = True
            return redirect(proximo)
        else:
            _registrar_falha(ip)
            flash("senha incorreta", "erro")
    return render_template("login.html", proximo=proximo)


@app.errorhandler(413)
def _envio_grande(_e):
    flash(f"arquivo grande demais — o limite é {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB por envio", "erro")
    return redirect(request.referrer if _mesmo_site(request.referrer) else url_for("inicio"))


@app.route("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


# --- filtros de formatação -----------------------------------------------------

@app.template_filter("moeda")
def f_moeda(v):
    if v is None:
        return "—"
    s = f"{float(v):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return "R$ " + s


@app.template_filter("num")
def f_num(v, casas=0):
    if v is None:
        return "—"
    s = f"{float(v):,.{casas}f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return s


@app.template_filter("dataarq")
def f_dataarq(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


@app.template_filter("datacurta")
def f_datacurta(ts):
    """Data enxuta para listas: 'hoje 10:19', 'ontem', '24 set'."""
    d = datetime.fromtimestamp(ts)
    dias = (datetime.now().date() - d.date()).days
    if dias == 0:
        return "hoje " + d.strftime("%H:%M")
    if dias == 1:
        return "ontem"
    meses = "jan fev mar abr mai jun jul ago set out nov dez".split()
    return f"{d.day} {meses[d.month - 1]}" + ("" if d.year == datetime.now().year else f" {d.year}")


def _arquivo_em(base, rel):
    """Caminho do arquivo `rel` dentro da pasta `base`, ou 404.

    O nome é validado no TEXTO antes de qualquer acesso ao disco: com
    "\\\\servidor\\pasta\\x.pdf" ou "C:/..." na URL, Path(base) / rel vira o
    caminho de fora, e só de consultá-lo (resolve/is_file) o Windows já conecta
    no servidor indicado e entrega a credencial do usuário."""
    junto = safe_join(str(base), rel)  # None se for absoluto, de rede, com "\" ou ".."
    if junto is None:
        abort(404)
    base = Path(base).resolve()
    p = Path(junto).resolve()
    if not p.is_file() or base not in p.parents:
        abort(404)
    return p


def _int_arg(nome, padrao):
    """Parâmetro inteiro da query string; valor ausente ou inválido vira o padrão."""
    try:
        return int(request.args.get(nome, padrao))
    except (TypeError, ValueError):
        return padrao


@app.context_processor
def _globais():
    return {
        "links_externos": config.LINKS_EXTERNOS,
        "crm_url": config.CRM_URL,
        "competencia_padrao": ppi_engine.competencia_padrao(),
        "ano_atual": datetime.now().year,
        "mostrar_cidade": not config.EMPACOTADO,
        "nuvem": config.NUVEM,
        "acesso_local": _acesso_local(),
        "com_senha": bool(config.SENHA_ACESSO),
        "nome_maquina": NOME_MAQUINA,
        "gerar_ppi_dir": str(config.GERAR_PPI_DIR),
        "jobs_ativos": [{"id": j.id, "tipo": j.tipo, "titulo": j.titulo, "status": j.status,
                         "feito": j.feito, "total": j.total} for j in gerenciador.ativos()],
        # muda quando app.css ou app.js mudam: o navegador pode guardá-los sem perguntar de novo
        "versao_css": int(max((config.APP_DIR / "static" / n).stat().st_mtime for n in ("app.css", "app.js"))),
    }


# --- início ---------------------------------------------------------------------

def _ultimos_trabalhos(n):
    """Trabalhos da fila e propostas geradas, juntos, do mais novo para o mais antigo."""
    linhas = []
    for job in gerenciador.recentes(n):
        d = job.como_dict()
        d["_quando"] = job.criado_em
        d["link"] = url_for("ver_job", job_id=job.id)
        linhas.append(d)
    try:
        for p in propostas.recentes(n):
            linhas.append({
                "titulo": p["titulo"], "status": "concluído", "proposta": True,
                "criado_em": p["quando"].strftime("%d/%m/%Y %H:%M"), "_quando": p["quando"],
                "link": url_for("proposta_gerada_arquivo", nome=p["arquivo_rel"]),
                "editar": url_for("proposta_editar", p=p["pasta_rel"]),
            })
    except OSError:
        pass
    linhas.sort(key=lambda d: d["_quando"], reverse=True)
    return linhas[:n]


@app.route("/")
def inicio():
    listas = ppi_engine.listar_listas()
    cache = None
    try:
        cache = tabelas.carregar() if config.TABELAS_CACHE.is_file() else None
    except Exception:  # noqa: BLE001
        cache = None
    return render_template(
        "inicio.html",
        listas=listas,
        n_pdfs=lote.contar_saida(),
        n_consorcios=consorcios.contar(),
        tabela_info=cache,
        jobs=_ultimos_trabalhos(8),
        motor_ok=ppi_engine.motor_disponivel(),
        metas={} if config.NUVEM else metas.situacao(),
    )


# --- jobs -----------------------------------------------------------------------

@app.route("/jobs/<job_id>")
def ver_job(job_id):
    job = gerenciador.obter(job_id) or abort(404)
    return render_template("job.html", job=job.como_dict())


@app.route("/jobs/<job_id>/cancelar", methods=["POST"])
def cancelar_job(job_id):
    job = gerenciador.obter(job_id) or abort(404)
    if not job.cancelar():
        flash("Esse trabalho já terminou.", "erro")
    return redirect(url_for("ver_job", job_id=job.id))


@app.route("/api/jobs/<job_id>")
def api_job(job_id):
    job = gerenciador.obter(job_id) or abort(404)
    desde = _int_arg("desde", 0)
    return jsonify(job.como_dict(log_desde=desde))


# --- relatórios PPI / SISCAN em lote --------------------------------------------

def _lote_e_guardar_cache(job):
    """Na nuvem: roda o relatório e guarda as consultas novas (o disco é apagado ao reiniciar)."""
    try:
        return lote.executar(job)
    finally:
        try:
            nuvem.guardar_cache()
        except Exception:  # noqa: BLE001 — o relatório já está pronto; só perde o atalho
            app.logger.exception("falha ao guardar o cache na nuvem")


@app.route("/relatorios", methods=["GET", "POST"])
def relatorios():
    listas = ppi_engine.listar_listas()
    if request.method == "POST":
        tipo = request.form.get("tipo", "ppi")
        competencia = request.form.get("competencia", "").strip() or ppi_engine.competencia_padrao()
        cidades = ppi_engine.limpar_cidades(request.form.get("cidades", ""))
        nome_lista = request.form.get("lista", "")
        try:
            if nome_lista:
                cidades = ppi_engine.limpar_cidades(cidades + ppi_engine.ler_lista(nome_lista))
            competencia = ppi_engine.validar_competencia(competencia)
            if not cidades:
                raise ValueError("informe ao menos uma cidade ou escolha uma lista")
            if tipo not in lote.TIPOS:
                raise ValueError("tipo inválido")
        except (ValueError, FileNotFoundError) as e:
            flash(str(e), "erro")
            return redirect(url_for("relatorios"))
        subgrupos = request.form.getlist("subgrupo") if tipo == "ppi" else None
        if subgrupos and len(subgrupos) == 2:
            subgrupos = None
        params = {"tipo": tipo, "cidades": cidades, "competencia": competencia,
                  "subgrupos": subgrupos, "referencia": request.form.get("referencia", "").strip(),
                  "atualizar_siscan": request.form.get("atualizar_siscan") == "on"}
        titulo = f"{lote.TIPOS[tipo]['nome']} — {len(cidades)} cidade(s) — {ppi_engine.formatar_competencia(competencia)}"
        job = gerenciador.enfileirar("lote", titulo, _lote_e_guardar_cache if nuvem.ATIVO else lote.executar, params)
        if any(j.id != job.id for j in gerenciador.ativos()):
            flash("Rodada adicionada à fila — começa assim que a atual terminar.", "ok")
            return redirect(url_for("fila"))
        return redirect(url_for("ver_job", job_id=job.id))
    return render_template("relatorios.html", tipos=lote.TIPOS, listas=listas, arquivos=lote.listar_saida(40),
                           jobs=[j.como_dict() for j in gerenciador.recentes(8) if j.tipo == "lote"])


def _fila_lote():
    """Rodadas de relatório PPI/SISCAN: ativas (na ordem da fila) e depois o histórico."""
    jobs = [j for j in gerenciador.recentes(1000) if j.tipo == "lote"]
    ativos = sorted((j for j in jobs if j.status in ("na fila", "rodando")),
                    key=lambda j: (j.status != "rodando", j.criado_em))
    resto = [j for j in jobs if j.status not in ("na fila", "rodando")]
    saida = []
    for j in ativos + resto:
        d = j.como_dict(log_desde=10 ** 9)
        d.pop("log", None)
        d["n_ok"] = sum(1 for i in d["itens"] if i.get("status") == "ok")
        d["n_erro"] = sum(1 for i in d["itens"] if i.get("status") == "erro")
        saida.append(d)
    return saida


@app.route("/fila")
def fila():
    return render_template("fila.html", jobs=_fila_lote())


@app.route("/api/fila")
def api_fila():
    return jsonify(_fila_lote())


@app.route("/metas")
def metas_pagina():
    listas = ppi_engine.listar_listas()
    escolhida = next((l for l in listas if l["nome"] == request.args.get("consorcio")), None)
    s = metas.situacao(escolhida["cidades"] if escolhida else None)
    return render_template("metas.html", s=s, listas=listas, escolhida=escolhida)


@app.route("/metas/<slug>/regra", methods=["POST"])
def meta_regra(slug):
    try:
        metas.salvar_regra(slug, request.form.get("regra", ""))
        flash("Regra da meta atualizada.", "ok")
    except (ValueError, OSError) as e:
        flash(f"Não consegui salvar a regra: {e}", "erro")
    return redirect(url_for("meta_municipio", slug=slug, consorcio=request.args.get("consorcio")))


@app.route("/metas/<slug>")
def meta_municipio(slug):
    d = metas.detalhe(slug)
    if d is None:
        abort(404)
    return render_template("meta_municipio.html", d=d)


@app.route("/relatorios/arquivo/<path:nome>")
def abrir_saida(nome):
    p = _arquivo_em(config.SAIDA_DIR, nome)
    return send_file(p, mimetype="application/pdf", as_attachment=False, download_name=p.name)


NOMES_PASTAS = {"saida": "Saída dos relatórios PPI / SISCAN", "consorcios": "Relatórios de consórcio",
                "propostas": "Propostas", "listas": "Listas de consórcios", "tabelas": "Tabelas de preço",
                "padronizados": "Arquivos padronizados"}


def _pastas():
    return {"saida": config.SAIDA_DIR, "consorcios": consorcios.PASTA, "propostas": config.PROPOSTAS_SAIDA,
            "listas": config.LISTAS_DIR, "tabelas": config.TABELAS_DIR,
            "padronizados": config.PADRONIZADOS_DIR}


@app.route("/pasta/<qual>")
def ver_pasta(qual):
    """A pasta pelo navegador — para quem acessa de outro PC e não tem o Explorer do servidor."""
    base = _pastas().get(qual) or abort(404)
    base.mkdir(parents=True, exist_ok=True)
    arquivos = []
    # os.walk + scandir: a data e o tamanho vêm junto com a listagem, e o caminho
    # relativo é montado direto (pathlib.relative_to custava metade da página)
    for raiz, pastas, nomes in os.walk(base):
        pastas[:] = [d for d in pastas if not d.startswith(".")]
        prefixo = os.path.relpath(raiz, base).replace("\\", "/")
        prefixo = "" if prefixo == "." else prefixo + "/"
        with os.scandir(raiz) as it:
            for e in it:
                if e.is_file() and not e.name.startswith(("~$", ".")):
                    st = e.stat()
                    arquivos.append({"rel": prefixo + e.name, "tamanho_kb": max(1, round(st.st_size / 1024)),
                                     "modificado": st.st_mtime})
    arquivos.sort(key=lambda a: a["modificado"], reverse=True)
    return render_template("pasta.html", qual=qual, nome=NOMES_PASTAS.get(qual, qual), arquivos=arquivos[:1000])


@app.route("/pasta/<qual>/arquivo/<path:rel>")
def pasta_arquivo(qual, rel):
    p = _arquivo_em(_pastas().get(qual) or abort(404), rel)
    inline = p.suffix.lower() in (".pdf", ".txt", ".png", ".jpg")
    return send_file(p, as_attachment=not inline, download_name=p.name)


@app.route("/abrir-pasta/<qual>")
def abrir_pasta(qual):
    p = _pastas().get(qual) or abort(404)
    p.mkdir(parents=True, exist_ok=True)
    if not _acesso_local():
        # O Explorer abriria na tela do servidor, não na de quem clicou.
        return jsonify({"local": False, "maquina": NOME_MAQUINA, "caminho": str(p)})
    os.startfile(str(p))  # abre no Explorer desta máquina
    return jsonify({"local": True, "maquina": NOME_MAQUINA, "caminho": str(p)})


# --- listas de cidades (consórcios) ---------------------------------------------

@app.route("/listas", methods=["GET", "POST"])
def listas():
    if request.method == "POST":
        acao = request.form.get("acao")
        try:
            if acao == "apagar":
                ppi_engine.apagar_lista(request.form["nome"])
                flash("Lista apagada.", "ok")
            else:
                nome = ppi_engine.salvar_lista(request.form.get("nome", ""), request.form.get("cidades", ""),
                                               request.form.get("titulo", ""))
                flash(f"Lista '{nome}' salva.", "ok")
        except (ValueError, OSError) as e:
            flash(str(e), "erro")
        return redirect(url_for("listas"))
    return render_template("listas.html", listas=ppi_engine.listar_listas(), editar=request.args.get("editar"))


# --- relatório por consórcio ----------------------------------------------------

@app.route("/consorcios", methods=["GET", "POST"])
def consorcios_pagina():
    listas = ppi_engine.listar_listas()
    if request.method == "POST":
        nome_lista = request.form.get("lista", "")
        nome = request.form.get("nome", "").strip()
        competencia = request.form.get("competencia", "").strip() or ppi_engine.competencia_padrao()
        cidades = ppi_engine.limpar_cidades(request.form.get("cidades", ""))
        polo_codigo = request.form.get("polo_codigo", "").strip() or config.POLO_PADRAO["codigo"]
        polo_nome = request.form.get("polo_nome", "").strip() or config.POLO_PADRAO["nome"]
        try:
            if nome_lista:
                cidades = ppi_engine.limpar_cidades(ppi_engine.ler_lista(nome_lista) + cidades)
                nome = nome or ppi_engine.titulo_lista(nome_lista)
            competencia = ppi_engine.validar_competencia(competencia)
            if not cidades:
                raise ValueError("escolha uma lista ou informe as cidades")
            if not nome:
                raise ValueError("dê um nome ao consórcio")
        except (ValueError, FileNotFoundError) as e:
            flash(str(e), "erro")
            return redirect(url_for("consorcios_pagina"))
        params = {"nome": nome, "cidades": cidades, "competencia": competencia,
                  "polo_codigo": polo_codigo, "polo_nome": polo_nome}
        job = gerenciador.enfileirar("consorcio", f"Consórcio {nome} — {len(cidades)} municípios — {ppi_engine.formatar_competencia(competencia)}",
                                     consorcios.executar, params)
        return redirect(url_for("ver_job", job_id=job.id))
    return render_template("consorcios.html", listas=listas, relatorios=consorcios.listar(), polo=config.POLO_PADRAO)


@app.route("/consorcios/<relatorio_id>")
def consorcio_ver(relatorio_id):
    r = consorcios.carregar(relatorio_id) or abort(404)
    return render_template("consorcio_ver.html", r=r)


@app.route("/consorcios/arquivo/<path:nome>")
def consorcio_arquivo(nome):
    p = _arquivo_em(consorcios.PASTA, nome)
    return send_file(p, as_attachment=True, download_name=p.name)


# --- propostas ------------------------------------------------------------------

@app.route("/propostas")
def propostas_pagina():
    itens = propostas.catalogo()
    q = request.args.get("q", "").strip().lower()
    if q:
        itens = [i for i in itens if q in i["cliente"].lower() or q in i["arquivo"].lower() or q == str(i["numero"])]
    filtros = {k: request.args.get(k, "") for k in ("tipo_orcamento", "cliente_categoria", "cliente_situacao", "produto", "etapa")}
    for k, v in filtros.items():
        if v:
            if k == "produto":
                itens = [i for i in itens if v in (i["classif"].get("produtos") or [])]
            else:
                itens = [i for i in itens if i["classif"].get(k) == v]
    for i in itens:
        i["rotulos"] = propostas.rotulos_classificacao(i["classif"])
    filtrando = bool(q or any(filtros.values()))
    return render_template("propostas.html", itens=itens[:300], total=len(itens), q=q, f=filtros, filtrando=filtrando,
                           classif=_opcoes_classificacao(),
                           proximo=propostas.proximo_numero(itens if not filtrando else None))


def _opcoes_classificacao():
    return {"tipo_orcamento": config.CLASSIF_TIPO_ORCAMENTO, "cliente_categoria": config.CLASSIF_CLIENTE,
            "cliente_situacao": config.CLASSIF_SITUACAO_CLIENTE, "produtos": config.CLASSIF_PRODUTOS,
            "etapa": config.CLASSIF_ETAPA}


@app.route("/propostas/abrir")
def proposta_abrir():
    c = request.args.get("c", "")
    alvo = None
    for base in config.PROPOSTAS_PASTAS + config.PROPOSTAS_PASTAS_RASAS:
        # comparação só no texto (abspath não acessa o disco nem a rede): um
        # caminho \\servidor\... fica de fora sem nunca ser consultado
        try:
            rel = Path(os.path.abspath(c)).relative_to(os.path.abspath(base))
        except ValueError:
            continue
        alvo = _arquivo_em(base, rel.as_posix())
        break
    if alvo is None:
        abort(404)
    mimetype = "application/pdf" if alvo.suffix.lower() == ".pdf" else None
    return send_file(alvo, mimetype=mimetype, as_attachment=(mimetype is None), download_name=alvo.name)


@app.route("/propostas/nova", methods=["GET", "POST"])
def proposta_nova():
    if request.method == "POST":
        f = request.form
        itens, erro_item = [], None
        for mn, desc, qtd, sus, val in zip(f.getlist("item_mnemonico"), f.getlist("item_descricao"),
                                          f.getlist("item_qtd"), f.getlist("item_sus"), f.getlist("item_valor")):
            if not (mn.strip() or desc.strip()):
                continue
            valor = tabelas.para_numero(val)
            qtd_num = tabelas.para_numero(qtd)
            if erro_item is None and valor is None:
                erro_item = f"item {mn or desc}: valor inválido"
            elif erro_item is None and (qtd_num is None or qtd_num <= 0):
                erro_item = f"item {mn or desc}: quantidade inválida"
            # o texto digitado fica no item inválido, para voltar à tela como estava
            itens.append({"mnemonico": mn.strip(), "descricao": desc.strip(),
                          "qtd": qtd_num if qtd_num is not None else qtd.strip(),
                          "codigo_sus": sus.strip(), "valor": valor if valor is not None else val.strip()})
        ano = f.get("ano") or datetime.now().year
        try:
            ano_num = int(ano)
        except (TypeError, ValueError):
            ano_num = None  # gerar() rejeita o ano inválido com a mensagem certa
        editar = f.get("editar") or None
        dados = {
            "tipo": f.get("tipo", "publica"), "numero": f.get("numero") or propostas.proximo_numero(ano=ano_num),
            "ano": ano, "cliente": f.get("cliente", ""),
            "contato": f.get("contato", ""), "objeto": f.get("objeto", ""), "modalidade": f.get("modalidade", "faturado"),
            "patologia": f.get("patologia") if f.get("patologia") in config.PATOLOGIAS else "",
            "signatario": f.get("signatario", ""), "itens": itens,
            "tipo_orcamento": f.get("tipo_orcamento", ""), "cliente_categoria": f.get("cliente_categoria", ""),
            "cliente_situacao": f.get("cliente_situacao", ""), "produtos": f.getlist("produtos"),
            "etapa": f.get("etapa", ""),
        }
        if editar:
            # mantém a data original da proposta
            try:
                dados["data"] = datetime.fromisoformat(propostas.carregar_para_editar(editar)["data"]).date()
            except (ValueError, KeyError, TypeError, OSError):
                pass
        try:
            if erro_item:
                raise ValueError(erro_item)
            if not dados["cliente"].strip():
                raise ValueError("informe o cliente")
            if not itens:
                raise ValueError("adicione ao menos um item")
            if not (dados["tipo_orcamento"] and dados["cliente_categoria"] and dados["cliente_situacao"]
                    and dados["produtos"] and dados["etapa"]):
                raise ValueError("preencha a classificação (tipo de orçamento, cliente, produto e etapa)")
            resultado = propostas.gerar(dados, editar=editar)
        except (ValueError, FileNotFoundError) as e:
            flash(str(e), "erro")
            # Volta à tela com tudo o que foi digitado (antes um redirect
            # apagava os itens e a classificação).
            campos = {k: v for k, v in dados.items() if k not in ("numero", "ano", "data")}
            if editar:
                try:
                    edicao = propostas.carregar_para_editar(editar)
                except (ValueError, OSError):
                    return redirect(url_for("propostas_pagina"))
                edicao.update(campos)
                return _tela_proposta(edicao)
            return _tela_proposta(rascunho=dict(campos, numero=f.get("numero", ""), ano=f.get("ano", "")))
        if nuvem.ATIVO:
            try:  # na nuvem o disco é apagado ao reiniciar: guarda a cópia já
                nuvem.guardar_pasta(resultado["pasta"], f"Proposta {resultado['pasta_rel']}")
            except Exception as e:  # noqa: BLE001 — a proposta está pronta; só avisa
                app.logger.exception("falha ao guardar a proposta na nuvem")
                flash(f"proposta gerada, mas a cópia de segurança falhou ({e}) — avise o responsável", "erro")
        return render_template("proposta_gerada.html", r=resultado, dados=dados, editada=bool(editar),
                               quem=propostas.signatario(dados["signatario"]))
    return _tela_proposta()


def _tela_proposta(edicao=None, rascunho=None):
    escolhas = dict(config.MODALIDADES)
    escolhas.update({k: f["nome"] for k, f in config.TABELAS_ADICIONAIS.items() if f.get("proposta")})
    return render_template("proposta_nova.html", proximo=propostas.proximo_numero(), modalidades=escolhas,
                           objeto_padrao=config.OBJETO_PADRAO, soffice=bool(propostas.localizar_soffice() or propostas.word_disponivel()),
                           signatarios=propostas.signatarios(), edicao=edicao, rascunho=rascunho,
                           classif=_opcoes_classificacao(), patologias=config.PATOLOGIAS,
                           regras={"|".join(k): v for k, v in config.REGRAS_TABELA.items()})


@app.route("/propostas/excluir", methods=["POST"])
def proposta_excluir():
    """Exclui uma proposta gerada pela Central (ex.: teste) e libera o número."""
    try:
        antiga, nova = propostas.excluir(request.form.get("p", ""))
    except ValueError as e:
        flash(str(e), "erro")
        return redirect(url_for("propostas_pagina"))
    if nuvem.ATIVO:
        try:
            nuvem.excluir_pasta(antiga, nova, f"Proposta excluída {antiga}")
        except Exception as e:  # noqa: BLE001
            app.logger.exception("falha ao excluir a proposta na nuvem")
            flash(f"excluída aqui, mas a cópia na nuvem não foi atualizada ({e}) — avise o responsável", "erro")
    flash(f"Proposta {antiga} excluída. O número dela está livre de novo.", "ok")
    return redirect(url_for("propostas_pagina"))


@app.route("/propostas/editar")
def proposta_editar():
    try:
        edicao = propostas.carregar_para_editar(request.args.get("p", ""))
    except (ValueError, OSError) as e:
        flash(str(e), "erro")
        return redirect(url_for("propostas_pagina"))
    return _tela_proposta(edicao)


@app.route("/propostas/gerada/<path:nome>")
def proposta_gerada_arquivo(nome):
    p = _arquivo_em(config.PROPOSTAS_SAIDA, nome)
    mimetype = "application/pdf" if p.suffix.lower() == ".pdf" else None
    return send_file(p, mimetype=mimetype, as_attachment=(mimetype is None), download_name=p.name)


# --- tabelas de preço -----------------------------------------------------------

def _importar_tabelas_job(job):
    job.escrever("Lendo as tabelas de preço e referências (leva ~1 min)...")
    c = tabelas.reimportar()
    for mod, f in c["fontes"].items():
        job.escrever(f"{config.MODALIDADES[mod]}: {f['itens']} exames ({f['arquivo']})")
    for chave, f in c.get("fontes_adicionais", {}).items():
        nome = config.TABELAS_ADICIONAIS[chave]["nome"]
        detalhe = f.get("erro") or (str(f["itens"]) + " itens")
        job.escrever(f"{nome}: {detalhe} ({f['arquivo']})")
    job.escrever(f"Catálogo Shift: {c['n_shift']} procedimentos")
    return {"itens": len(c["itens"])}


@app.route("/tabelas")
def tabelas_pagina():
    info = None
    if config.TABELAS_CACHE.is_file():
        info = tabelas.carregar()
    importando = any(j.tipo == "tabelas" and j.status in ("na fila", "rodando") for j in gerenciador.recentes(20))
    return render_template("tabelas.html", info=info, modalidades=config.MODALIDADES, importando=importando,
                           desatualizado=tabelas.cache_desatualizado(), q=request.args.get("q", ""))


@app.route("/tabelas/reimportar", methods=["POST"])
def tabelas_reimportar():
    job = gerenciador.enfileirar("tabelas", "Importar tabelas de preço e referências", _importar_tabelas_job)
    return redirect(url_for("ver_job", job_id=job.id))


@app.route("/api/tabelas/busca")
def api_tabelas_busca():
    if not config.TABELAS_CACHE.is_file():
        return jsonify({"erro": "tabela ainda não importada", "itens": []})
    q = request.args.get("q", "")
    limite = max(1, min(_int_arg("n", 40), 500))
    return jsonify({"itens": tabelas.buscar(q, limite=limite),
                    "extras": tabelas.buscar_adicionais(q, limite=80)})


@app.route("/api/tabelas/completa")
def api_tabelas_completa():
    if not config.TABELAS_CACHE.is_file():
        return jsonify({"erro": "tabela ainda não importada", "itens": []})
    return jsonify({"itens": tabelas.tabela_completa(request.args.get("modalidade", ""), request.args.get("patologia"),
                                                     request.args.getlist("produto"))})


@app.route("/api/proposta/importar", methods=["POST"])
def api_proposta_importar():
    """Lista solta (texto colado ou print) -> itens com o valor da lista e o exame achado na tabela."""
    if not config.TABELAS_CACHE.is_file():
        return jsonify({"erro": "tabela ainda não importada", "itens": []})
    from core import importar_itens
    modalidade = request.form.get("modalidade", "")
    imagem = request.files.get("imagem")
    try:
        if imagem and imagem.filename:
            itens = importar_itens.ler_imagem(imagem.read(), imagem.filename, modalidade)
        else:
            itens = importar_itens.ler(request.form.get("texto", "").splitlines(), modalidade)
    except ValueError as e:
        return jsonify({"erro": str(e), "itens": []})
    except Exception as e:  # noqa: BLE001 — falha do OCR do Windows etc.: a tela mostra a mensagem
        return jsonify({"erro": f"não consegui ler: {e}", "itens": []})
    if not itens:
        return jsonify({"erro": "não achei nenhuma linha com valor em R$", "itens": []})
    return jsonify({"itens": itens})


@app.route("/tabelas/conferencia", methods=["GET", "POST"])
def tabelas_conferencia():
    if request.method == "POST":
        if not config.TABELAS_CACHE.is_file():
            flash("a tabela de preços ainda não foi importada — importe em Tabelas antes de conferir", "erro")
            return redirect(url_for("tabelas_conferencia"))
        arq = request.files.get("arquivo")
        if not arq or not arq.filename:
            flash("escolha um arquivo .csv ou .xlsx", "erro")
            return redirect(url_for("tabelas_conferencia"))
        nome_arquivo = tabelas.nome_arquivo_seguro(arq.filename)
        if not nome_arquivo.lower().endswith((".csv", ".xlsx", ".xlsm")):
            nome_arquivo += ".csv"
        conteudo = arq.read()
        try:
            cabecalho, linhas = tabelas.ler_arquivo_tabular(nome_arquivo, conteudo)
        except Exception as e:  # noqa: BLE001
            flash(f"não consegui ler o arquivo: {e}", "erro")
            return redirect(url_for("tabelas_conferencia"))
        if not cabecalho:
            flash("o arquivo não tem cabeçalho reconhecível", "erro")
            return redirect(url_for("tabelas_conferencia"))
        col_mnem, col_valor, col_desc = tabelas.detectar_colunas(cabecalho)
        try:
            if request.form.get("col_mnem", "") != "":
                col_mnem = int(request.form["col_mnem"])
            if request.form.get("col_valor", "") != "":
                col_valor = int(request.form["col_valor"])
        except ValueError:
            col_mnem = col_valor = None  # volta para a escolha das colunas
        for col in (col_mnem, col_valor):
            if col is not None and not 0 <= col < len(cabecalho):
                col_mnem = col_valor = None
        modalidade = request.form.get("modalidade", "faturado")
        if modalidade not in config.MODALIDADES:
            # também entra no nome do .xlsx gerado: só valores conhecidos
            modalidade = "faturado"
        if col_mnem is None or col_valor is None:
            # primeira passada: mostra o cabeçalho e pede para confirmar as colunas
            return render_template("conferencia.html", modalidades=config.MODALIDADES, cabecalho=cabecalho,
                                   arquivo=nome_arquivo, col_mnem=col_mnem, col_valor=col_valor, modalidade=modalidade)
        resultado = tabelas.conferir(cabecalho, linhas, col_mnem, col_valor, modalidade, col_desc)
        resultado["arquivo"] = nome_arquivo
        config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        nome_x = f"conferencia_{Path(nome_arquivo).stem}_{modalidade}.xlsx"
        tabelas.exportar_conferencia_xlsx(resultado, config.UPLOADS_DIR / nome_x)
        resultado["xlsx"] = nome_x
        return render_template("conferencia.html", modalidades=config.MODALIDADES, resultado=resultado)
    return render_template("conferencia.html", modalidades=config.MODALIDADES)


@app.route("/tabelas/conferencia/arquivo/<path:nome>")
def conferencia_arquivo(nome):
    p = _arquivo_em(config.UPLOADS_DIR, nome)
    return send_file(p, as_attachment=True, download_name=p.name)


# --- padronizar arquivo ---------------------------------------------------------

MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB


# --- Teto PPI de Varginha ----------------------------------------------------------

@app.route("/teto-ppi")
def teto_ppi_pagina():
    publicadas = teto_ppi.competencias_publicadas() if ppi_engine.motor_disponivel() else []
    return render_template("teto_ppi.html", info=teto_ppi.situacao(),
                           competencia=publicadas[0] if publicadas else ppi_engine.competencia_padrao(),
                           publicadas=publicadas[:12], motor=ppi_engine.motor_disponivel(),
                           procedimentos=[c for _, c in teto_ppi.PROCEDIMENTOS],
                           jobs=[j.como_dict() for j in gerenciador.recentes(20) if j.tipo == "teto_ppi"][:6])


@app.route("/teto-ppi/modelo", methods=["POST"])
def teto_ppi_modelo():
    arq = request.files.get("arquivo")
    if not arq or not arq.filename.lower().endswith(".xlsx"):
        flash("escolha a planilha .xlsx do mês anterior", "erro")
    else:
        try:
            teto_ppi.salvar_modelo(arq)
            flash("Planilha-modelo atualizada.", "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"não consegui usar essa planilha: {e}", "erro")
    return redirect(url_for("teto_ppi_pagina"))


@app.route("/teto-ppi/gerar", methods=["POST"])
def teto_ppi_gerar():
    try:
        comp = ppi_engine.validar_competencia(request.form.get("competencia"))
    except ValueError as e:
        flash(str(e), "erro")
        return redirect(url_for("teto_ppi_pagina"))
    job = gerenciador.enfileirar("teto_ppi", f"Teto PPI Varginha · {ppi_engine.formatar_competencia(comp)}",
                                 teto_ppi.executar, {"competencia": comp})
    return redirect(url_for("ver_job", job_id=job.id))


@app.route("/teto-ppi/arquivo/<path:nome>")
def teto_ppi_arquivo(nome):
    p = _arquivo_em(teto_ppi.SAIDA, nome)
    return send_file(p, as_attachment=True, download_name=p.name)


# --- mapa 3D (gerado por minas-3d/tools/empacotar.py) ----------------------------

MAPA3D_DIR = config.MAPA3D_DIR


@app.route("/mundo")
def mundo():
    # Cidade 3D: brincadeira só deste PC, nunca no .exe da equipe
    if config.EMPACOTADO:
        abort(404)
    return render_template("mundo.html")


@app.route("/mapa3d")
def mapa3d_raiz():
    return redirect("/mapa3d/")


@app.route("/mapa")
def mapa3d_pagina():
    return render_template("mapa3d.html", info=mapa3d_mod.situacao(),
                           jobs=[j.como_dict() for j in gerenciador.recentes(20) if j.tipo == "mapa3d"][:6])


@app.route("/mapa/atualizar", methods=["POST"])
def mapa3d_atualizar():
    if any(j.tipo == "mapa3d" for j in gerenciador.ativos()):
        flash("O mapa já está sendo atualizado.", "erro")
        return redirect(url_for("mapa3d_pagina"))
    job = gerenciador.enfileirar("mapa3d", "Atualizar Mapa 3D", mapa3d_mod.executar)
    return redirect(url_for("ver_job", job_id=job.id))


@app.route("/mapa3d/")
@app.route("/mapa3d/<path:rel>")
def mapa3d(rel="index.html"):
    if not (MAPA3D_DIR / "index.html").is_file():
        abort(404)
    return send_from_directory(MAPA3D_DIR, rel, max_age=0)


@app.route("/padronizar", methods=["GET", "POST"])
def padronizar_pagina():
    if request.method == "GET":
        return render_template("padronizar.html", recentes=_padronizados_recentes())

    enviados = [a for a in request.files.getlist("arquivo") if a and a.filename]
    if not enviados:
        flash("escolha um arquivo", "erro")
        return redirect(url_for("padronizar_pagina"))
    planilha_ext = (".xlsx", ".xlsm", ".csv", ".tsv", ".txt")
    nomes = [padronizar.nome_arquivo_seguro(a.filename) for a in enviados]
    exts = [Path(n).suffix.lower() for n in nomes]
    if all(e in padronizar.EXT_IMAGEM for e in exts):
        tipo = "imagem"
    elif len(enviados) > 1:
        flash("vários arquivos de uma vez só para imagens (.png/.jpg); planilhas e documentos vão um por vez", "erro")
        return redirect(url_for("padronizar_pagina"))
    elif exts[0] in padronizar.EXT_DOCUMENTO:
        tipo = "documento"
    elif exts[0] in planilha_ext:
        tipo = "planilha"
    else:
        flash("formato não suportado — envie planilha (.xlsx, .csv), Word (.docx) ou imagem (.png, .jpg)", "erro")
        return redirect(url_for("padronizar_pagina"))
    conteudos = []
    for a in enviados:
        dados = a.read(MAX_UPLOAD + 1)
        if len(dados) > MAX_UPLOAD:
            flash("arquivo maior que 20 MB", "erro")
            return redirect(url_for("padronizar_pagina"))
        conteudos.append(dados)
    nome_envio = nomes[0] if len(nomes) == 1 else f"{len(nomes)} imagens"
    # imagem com OCR: o texto lido vira tabela e segue o caminho das planilhas
    ocr = tipo == "imagem" and request.form.get("ocr") == "on"
    if ocr:
        tipo = "planilha"

    formatos = [f for f in ("xlsx", "docx", "pdf") if request.form.get("fmt_" + f) == "on"]
    if tipo != "planilha":
        formatos = [f for f in formatos if f != "xlsx"]
    if not formatos:
        flash("marque ao menos um formato de saída" + (" (Word ou PDF)" if tipo != "planilha" else ""), "erro")
        return redirect(url_for("padronizar_pagina"))
    titulo = request.form.get("titulo", "").strip() or Path(nomes[0]).stem
    subtitulo = request.form.get("subtitulo", "").strip() or padronizar.subtitulo_padrao()
    opcoes = {"com_logo": request.form.get("logo") == "on",
              "com_zebra": request.form.get("zebra") == "on",
              "com_totais": request.form.get("totais") == "on"}
    geradores = {"xlsx": padronizar.padronizar, "docx": padronizar.gerar_docx, "pdf": padronizar.gerar_pdf}
    base = f"{padronizar.nome_arquivo_seguro(titulo)[:80]} — padronizado"
    try:
        resultado = {"titulo": titulo, "tipo": tipo, "arquivos": [], "abas": None}
        if tipo == "planilha":
            abas = (padronizar.abas_de_imagens(list(zip(nomes, conteudos))) if ocr
                    else padronizar.ler(nome_envio, conteudos[0]))
            for fmt in formatos:
                saida = geradores[fmt](abas, config.PADRONIZADOS_DIR / f"{base}.{fmt}", titulo, subtitulo, **opcoes)
                resultado["arquivos"].append({"fmt": fmt, "nome": f"{base}.{fmt}"})
                resultado["abas"] = resultado["abas"] or (saida["abas"] if isinstance(saida, dict) else saida)
        else:
            # o PDF sai do próprio .docx; se só o PDF foi pedido, o .docx fica de rascunho
            docx = config.PADRONIZADOS_DIR / f"{base}.docx"
            if tipo == "documento":
                resultado["abas"] = padronizar.padronizar_docx(conteudos[0], docx, titulo, opcoes["com_logo"])
            else:
                resultado["abas"] = padronizar.imagens_docx(list(zip(nomes, conteudos)), docx, titulo,
                                                            subtitulo, opcoes["com_logo"])
            if "pdf" in formatos:
                padronizar.docx_para_pdf(docx, config.PADRONIZADOS_DIR / f"{base}.pdf")
            if "docx" not in formatos:
                docx.unlink(missing_ok=True)
            resultado["arquivos"] = [{"fmt": f, "nome": f"{base}.{f}"} for f in formatos]
    except ValueError as e:
        flash(str(e), "erro")
        return redirect(url_for("padronizar_pagina"))
    except Exception as e:  # noqa: BLE001 — arquivo estranho não deve derrubar a página
        flash(f"não consegui ler o arquivo: {e}", "erro")
        return redirect(url_for("padronizar_pagina"))
    resultado["origem"] = nome_envio
    return render_template("padronizar.html", r=resultado, recentes=_padronizados_recentes())


def _padronizados_recentes(limite=12):
    if not config.PADRONIZADOS_DIR.is_dir():
        return []
    arquivos = [p for p in config.PADRONIZADOS_DIR.iterdir()
                if p.is_file() and p.suffix.lower() in (".xlsx", ".docx", ".pdf")]
    arquivos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"nome": p.name, "ts": p.stat().st_mtime, "kb": p.stat().st_size // 1024}
            for p in arquivos[:limite]]


@app.route("/padronizar/arquivo/<path:nome>")
def padronizado_arquivo(nome):
    p = _arquivo_em(config.PADRONIZADOS_DIR, nome)
    pdf = p.suffix.lower() == ".pdf"
    return send_file(p, mimetype="application/pdf" if pdf else None, as_attachment=not pdf, download_name=p.name)


# --- CRM (GesMkt) ----------------------------------------------------------------

ACOES_CRM = {"criar": "Renovações", "avulsos": "Cards da planilha", "responsavel": "Trocar responsável"}
# "Criar cards" é uma aba só: .docx vira "criar", .xlsx/.csv vira "avulsos".


def _arquivo_crm(campo_envio, campo_lista, extensoes):
    """Arquivo enviado agora (salvo em planilhas/) ou escolhido da lista; devolve o caminho."""
    arq = request.files.get(campo_envio)
    if arq and arq.filename:
        if not arq.filename.lower().endswith(extensoes):
            raise ValueError("formato não aceito: " + arq.filename)
        return str(crm_mod.caminho(crm_mod.salvar_envio(arq)))
    nome = request.form.get(campo_lista, "")
    return str(crm_mod.caminho(nome)) if nome else ""


@app.route("/crm", methods=["GET", "POST"])
def crm_pagina():
    cfg = crm_mod.carregar_config()
    if request.method == "GET":
        return render_template(
            "crm.html", cfg=cfg, nome_crm="RD Station" if cfg.get("crm") == "rd" else "GesMkt", crm_url=config.CRM_URL, ok=crm_mod.disponivel(), pasta=str(config.CRM_DIR),
            words=crm_mod.arquivos(".docx"), csvs=[a for a in crm_mod.arquivos(".csv") if "valores" in a["nome"].lower()],
            planilhas=[a for a in crm_mod.arquivos(".xlsx", ".csv")
                       if not a["nome"].startswith("MODELO") and "valores" not in a["nome"].lower()],
            hoje=datetime.now().strftime("%Y-%m-%d"), aba_crm={"avulsos": "criar"}.get(request.args.get("aba", "criar"), request.args.get("aba", "criar")),
            jobs=[j.como_dict() for j in gerenciador.recentes(30) if j.tipo == "crm"][:8])

    f = request.form
    acao, modo = f.get("acao", ""), f.get("modo", "simular")
    if acao not in ACOES_CRM:
        abort(400)
    try:
        if acao in ("criar", "avulsos"):
            arquivo = _arquivo_crm("arquivo_envio", "arquivo", (".docx", ".xlsx", ".csv"))
            if not arquivo:
                raise ValueError("escolha ou envie o Word do mês (.docx) ou a planilha (.xlsx / .csv)")
            acao = "criar" if arquivo.lower().endswith(".docx") else "avulsos"
        if acao == "criar":
            args = ["criar", arquivo]
            valores = _arquivo_crm("csv_envio", "csv", (".csv",))
            if valores:
                args += ["--valores", valores]
            if f.get("a_partir", "").strip():
                args += ["--a-partir-de", f["a_partir"].strip()]
            limite = f.get("limite", "").strip()
            if f.get("sem_valor") == "on":
                args.append("--sem-valor")
        elif acao == "avulsos":
            args, limite = ["avulsos", arquivo], f.get("limite", "").strip()
        else:
            data = f.get("data", "").strip()
            datetime.strptime(data, "%Y-%m-%d")
            args, limite = ["responsavel", "--data", data], f.get("limite", "").strip()
        if limite and not limite.isdigit():
            raise ValueError("a quantidade de cards deve ser um número")
        if modo == "teste":
            limite = "1"
        if limite:
            args += ["--limite", limite]
        if f.get("responsavel"):
            args += ["--responsavel", f["responsavel"]]
        if acao == "responsavel" and not f.get("responsavel"):
            raise ValueError("escolha o novo responsável")
        enviar = modo in ("teste", "enviar")
        if enviar:
            args.append("--enviar")
    except ValueError as e:
        flash(str(e), "erro")
        return redirect(url_for("crm_pagina", aba="responsavel" if acao == "responsavel" else "criar"))
    rotulo = {"simular": "simulação", "teste": "teste com 1 card", "enviar": "no CRM"}[modo]
    titulo = f"CRM · {ACOES_CRM[acao]} — {rotulo}"
    job = gerenciador.enfileirar("crm", titulo, crm_mod.executar,
                                 {"args": args, "crm": cfg.get("crm", "gesmkt"), "acao": acao, "enviar": enviar})
    return redirect(url_for("ver_job", job_id=job.id))


@app.route("/crm/config", methods=["POST"])
def crm_config():
    f = request.form
    if not (f.get("funil", "").strip() and f.get("nome_card", "").strip()):
        flash("Funil e nome do card são obrigatórios.", "erro")
    else:
        try:
            crm_mod.salvar_config({k: f.get(k, "") for k in
                                   ("funil", "nome_card", "produto", "responsavel_id", "responsavel_nome")})
            flash("Configuração do CRM salva.", "ok")
        except OSError as e:
            flash("Não consegui salvar o config.json: " + str(e), "erro")
    return redirect(url_for("crm_pagina", aba=request.args.get("aba", "criar")))


@app.route("/api/crm/dados")
def api_crm_dados():
    try:
        return jsonify(crm_mod.dados(crm_mod.carregar_config().get("crm", "gesmkt"),
                                     forcar=request.args.get("forcar") == "1"))
    except Exception as e:  # noqa: BLE001 — sem token, sem internet etc.
        return jsonify({"erro": str(e)}), 502


@app.route("/crm/modelo")
def crm_modelo():
    p = crm_mod.PLANILHAS / "MODELO saude publica.xlsx"
    if not p.is_file():
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name)


# --- arranque -------------------------------------------------------------------

def _preparar():
    nuvem.baixar_dados()
    for d in (config.DATA_DIR, config.CACHE_DIR, config.UPLOADS_DIR, config.PADRONIZADOS_DIR,
              config.PROPOSTAS_SAIDA, consorcios.PASTA):
        d.mkdir(parents=True, exist_ok=True)
    precisa_importar = not config.TABELAS_CACHE.is_file() or tabelas.cache_desatualizado()
    if precisa_importar and all(p.is_file() for p in config.TABELAS_PRINCIPAIS.values()):
        gerenciador.enfileirar("tabelas", "Importar tabelas de preço e referências", _importar_tabelas_job)


if config.NUVEM:  # na nuvem quem sobe o app é o gunicorn (não passa pelo __main__)
    _preparar()

if __name__ == "__main__":
    _preparar()
    porta = int(os.environ.get("PORT", config.PORT))
    url = f"http://127.0.0.1:{porta}/"
    if "--sem-navegador" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print("Central IPD em " + url + ("  (feche esta janela para encerrar)" if config.CONGELADO
                                      else "  (Ctrl+C para encerrar, R para reiniciar)"))
    if config.HOST not in ("127.0.0.1", "localhost"):
        print(f"Outros computadores da rede: http://{NOME_MAQUINA}:{porta}/"
              + ("" if config.SENHA_ACESSO else "  (SEM senha — defina SENHA_ACESSO em config.py)"))
    if os.name == "nt" and sys.stdin and sys.stdin.isatty() and not config.CONGELADO:  # no .exe não há .bat para religar
        def _tecla_reiniciar():
            # R na janela do servidor: sai com código 3; o .bat vê isso e sobe de novo.
            import msvcrt
            import time
            while True:
                if msvcrt.kbhit() and msvcrt.getwch().lower() == "r":
                    print("\nReiniciando a Central IPD...", flush=True)
                    os._exit(3)
                time.sleep(0.2)
        threading.Thread(target=_tecla_reiniciar, daemon=True).start()
    app.run(host=config.HOST, port=porta, debug=False, threaded=True)
