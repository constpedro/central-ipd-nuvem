# -*- coding: utf-8 -*-
"""
Relatórios PPI / SISCAN em lote — roda os scripts do gerar_ppi como subprocesso.

Os scripts já fazem tudo (consulta, PDF pelo Chrome, retomada, log de erros);
aqui só montamos a linha de comando, mostramos a saída deles ao vivo no job e,
no fim, listamos os PDFs que ficaram em gerar_ppi/saida/.
"""
import time
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import config
from core import ppi_engine

TIPOS = {
    "ppi0202": {"nome": "PPI - Análises Clínicas", "script": "gerar_ppi_ipd.py", "descricao": "Um PDF por cidade: laboratório clínico (0202)."},
    "anatomia": {"nome": "PPI - Anatomia Patológica", "script": "gerar_ppi_ipd.py", "descricao": "Um PDF por cidade: 0203 sem os 7 procedimentos do SISCAN."},
    "siscan": {"nome": "PPI - SISCAN", "script": "gerar_siscan.py", "descricao": "Um PDF por cidade: os 7 procedimentos, produção real do SISCAN e meta mensal do PPI."},
    "conferir": {"nome": "Conferir totais contra o site", "script": "verificar_totais.py", "descricao": "Não gera PDF — só confere se as somas batem com o total oficial do PPI."},
}
RE_PROGRESSO = re.compile(r"(?:\[(\d+)/(\d+)\]|PROGRESSO\s+(\d+)/(\d+))")
# "Processando até 4 cidades em paralelo" — quantas rodam ao mesmo tempo
RE_PARALELO = re.compile(r"at[ée]\s+(\d+)\s+cidades?\s+em\s+paralelo", re.I)


def arquivos_esperados(tipo, cidades, competencia, subgrupos=None):
    """Nomes dos PDFs que o script vai gerar (para listar o que já existe)."""
    nomes = []
    for cidade in cidades:
        slug = ppi_engine.slugify(cidade)
        if tipo == "ppi":
            for sub in subgrupos or ("0203", "0202"):
                nomes.append(f"ppi_{slug}_{sub}_{competencia}.pdf")
        elif tipo == "ppi0202":
            nomes.append(f"ppi_{slug}_0202_{competencia}.pdf")
        elif tipo == "anatomia":
            nomes.append(f"ppi_{slug}_anatomia_{competencia}.pdf")
        elif tipo == "siscan":
            nomes.append(f"siscan_{slug}_{competencia}_com_ppi.pdf")
        elif tipo == "completo":
            nomes.append(f"siscan_{slug}_{competencia}_com_ppi.pdf")
            nomes.append(f"ppi_{slug}_0202_{competencia}.pdf")
    return nomes


def executar(job):
    p = job.params
    tipo, cidades, competencia = p["tipo"], p["cidades"], p["competencia"]
    if tipo == "completo":
        etapas = [("gerar_siscan.py", "siscan"), ("gerar_ppi_ipd.py", "ppi0202")]
    elif tipo == "ppi0202":
        etapas = [("gerar_ppi_ipd.py", "ppi0202")]
    elif tipo == "anatomia":
        etapas = [("gerar_ppi_ipd.py", "anatomia")]
    else:
        etapas = [(TIPOS[tipo]["script"], tipo)]
    for nome_script, _ in etapas:
        script = config.GERAR_PPI_DIR / nome_script
        if not script.is_file():
            raise FileNotFoundError(f"script não encontrado: {script}")

    # As cidades vão num arquivo temporário via --lista: nomes compostos e acentos
    # não passam pela linha de comando do Windows sem dor de cabeça.
    fd, tmp = tempfile.mkstemp(prefix="central_cidades_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(cidades) + "\n")

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if tipo in ("siscan", "anatomia"):
        # O relatório por consórcio já consulta e guarda o PPI por cidade.
        # Reaproveitar esse cache evita refazer o 0203 antes do SISCAN.
        env["CENTRAL_IPD_PPI_CACHE_DIR"] = str(config.CACHE_DIR / "ppi")
        # O formulário do TabNet é pesado e serve para todas as cidades.
        env["CENTRAL_IPD_SISCAN_CACHE_DIR"] = str(config.CACHE_DIR / "siscan")
    # Quantos PDFs (ou cidades, no "conferir") cada etapa produz por cidade.
    # Os scripts informam PROGRESSO nessa mesma unidade.
    def _pdfs_por_cidade(etapa):
        if etapa == "ppi":
            return len(p.get("subgrupos") or ("0203", "0202"))
        return 1  # siscan, ppi0202, anatomia, conferir

    total = len(cidades) * sum(_pdfs_por_cidade(etapa) for _, etapa in etapas)
    job.progresso(0, total)

    # Andamento por cidade: o script roda em outro processo, então em vez de
    # interpretar a saída dele olhamos os PDFs que já apareceram na pasta.
    job.definir_itens(cidades)
    arquivos_por_cidade = {
        c: arquivos_esperados(tipo, [c], competencia, p.get("subgrupos")) for c in cidades
    }
    comecos = {}
    inicio_trabalho = time.time() - 2  # folga para o relógio do sistema de arquivos

    def _gerado_neste_trabalho(caminho):
        try:
            return caminho.stat().st_mtime >= inicio_trabalho
        except OSError:
            return False

    ultima_varredura = [0.0]
    # o script processa várias cidades ao mesmo tempo, então quem está
    # "rodando" é quem apareceu na saída e ainda não tem o PDF na pasta
    nomes_baixo = {c: c.lower() for c in cidades}
    # o script informa quantas cidades processa ao mesmo tempo
    em_paralelo = [1]

    def _citada(linha):
        m = RE_PARALELO.search(linha)
        if m:
            em_paralelo[0] = max(1, int(m.group(1)))
            _varrer(forcar=True)  # já mostra as que entraram em paralelo
            return
        baixo = linha.lower()
        for cidade, alvo in nomes_baixo.items():
            if alvo in baixo:
                comecos.setdefault(cidade, time.monotonic())
                job.marcar(cidade, "rodando")

    def _varrer(forcar=False):
        agora = time.monotonic()
        if not forcar and agora - ultima_varredura[0] < 1.0:
            return
        ultima_varredura[0] = agora
        pendentes = []
        n_pdfs = 0
        for cidade in cidades:
            nomes = arquivos_por_cidade[cidade]
            # Os scripts sempre refazem os PDFs; um arquivo anterior a este
            # trabalho é versão antiga que ainda vai ser substituída.
            prontos = [n for n in nomes if _gerado_neste_trabalho(config.SAIDA_DIR / n)]
            n_pdfs += len(prontos)
            if nomes and len(prontos) == len(nomes):
                job.marcar(cidade, "ok",
                           agora - comecos[cidade] if cidade in comecos else None)
            else:
                pendentes.append(cidade)
        # A barra segue os PDFs que já estão na pasta: com cidades em paralelo,
        # a linha PROGRESSO do script chega depois e deixava a barra atrás das
        # "Prontas".
        if n_pdfs > job.feito:
            job.progresso(n_pdfs, total)
        # as primeiras pendentes são as que estão sendo consultadas agora
        for cidade in pendentes[:em_paralelo[0]]:
            comecos.setdefault(cidade, agora)
            job.marcar(cidade, "rodando")
    codigos_saida = []
    try:
        progresso_base = 0
        for nome_script, etapa in etapas:
            if job.cancelado:
                break
            script = config.GERAR_PPI_DIR / nome_script
            cmd = config.comando_python(script, "--lista", tmp, "--competencia", competencia)
            if etapa == "ppi0202":
                cmd += ["--subgrupo", "0202"]
            elif etapa == "anatomia":
                cmd += ["--subgrupo", "0203", "--sem-siscan"]
            elif etapa == "ppi" and p.get("subgrupos"):
                for sub in p["subgrupos"]:
                    cmd += ["--subgrupo", sub]
            if etapa == "siscan" and p.get("referencia"):
                cmd += ["--referencia", p["referencia"]]
            if etapa == "siscan" and p.get("atualizar_siscan"):
                cmd += ["--atualizar-siscan"]

            job.escrever("$ " + " ".join(cmd[2:]).replace(tmp, "<cidades>"))
            proc = subprocess.Popen(
                cmd, cwd=str(config.GERAR_PPI_DIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            job.ao_cancelar(lambda proc=proc: _encerrar(proc))
            if job.cancelado:  # cancelou entre o Popen e o registro acima
                _encerrar(proc)
            for linha in proc.stdout:
                job.escrever(linha)
                _citada(linha)
                _varrer()
                m = RE_PROGRESSO.search(linha)
                if m:
                    feito, limite = ((m.group(1), m.group(2))
                                     if m.group(1) is not None
                                     else (m.group(3), m.group(4)))
                    # Os scripts antigos informam o limite local; o script
                    # paralelo também informa PROGRESSO, mas o total do job é
                    # mantido pela Central.
                    job.progresso(max(job.feito, progresso_base + int(feito)), total)
            proc.wait()
            job.ao_cancelar(None)
            if job.cancelado:
                job.escrever("Rodada cancelada — os PDFs já gerados continuam na pasta.")
                break
            codigos_saida.append(proc.returncode)
            if proc.returncode != 0:
                job.escrever(f"(etapa {etapa} terminou com código {proc.returncode})")
            progresso_base += len(cidades) * _pdfs_por_cidade(etapa)
            # O subprocesso pode encerrar depois de gerar os arquivos sem
            # entregar a linha PROGRESSO ao leitor (por exemplo, quando
            # tudo foi reaproveitado do cache). O fim da etapa é a fonte
            # definitiva para o contador da Central.
            job.progresso(progresso_base, total)
    finally:
        Path(tmp).unlink(missing_ok=True)

    _varrer(forcar=True)
    for cidade in cidades:
        nomes = arquivos_por_cidade[cidade]
        if not nomes:
            # "conferir" não gera PDF: sem isso as cidades ficariam para sempre
            # em "consultando…" / "na fila" na tela do trabalho
            if job.cancelado:
                job.marcar(cidade, "cancelada")
            else:
                job.marcar(cidade, "ok" if codigos_saida and not any(codigos_saida) else "erro")
        elif not all(_gerado_neste_trabalho(config.SAIDA_DIR / n) for n in nomes):
            job.marcar(cidade, "cancelada" if job.cancelado else "erro")

    esperados = arquivos_esperados(tipo, cidades, competencia, p.get("subgrupos"))
    gerados = [n for n in esperados if _gerado_neste_trabalho(config.SAIDA_DIR / n)]
    faltando = [n for n in esperados if n not in gerados]
    log_erros = config.GERAR_PPI_DIR / "log_erros.txt"
    codigo_saida = next((codigo for codigo in codigos_saida if codigo != 0), 0)
    erros = log_erros.read_text(encoding="utf-8").splitlines() if (
        tipo != "conferir" and codigo_saida != 0 and log_erros.is_file()
    ) else []
    if codigo_saida != 0 and tipo != "conferir":
        job.escrever(f"(uma etapa terminou com código {codigo_saida} — veja os erros acima)")
    return {
        "codigo_saida": codigo_saida,
        "arquivos": gerados,
        "faltando": faltando,
        "erros": erros,
        "pasta": str(config.SAIDA_DIR),
    }


def _encerrar(proc):
    """Mata o script e os filhos dele (o Chrome que imprime o PDF)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        proc.kill()


def contar_saida():
    """Quantos PDFs há em saida/ (sem ler data nem tamanho de cada um)."""
    if not config.SAIDA_DIR.is_dir():
        return 0
    with os.scandir(config.SAIDA_DIR) as it:
        return sum(1 for e in it if e.name.lower().endswith(".pdf") and e.is_file())


def listar_saida(limite=60):
    """PDFs mais recentes da pasta saida/."""
    if not config.SAIDA_DIR.is_dir():
        return []
    # scandir traz data e tamanho junto com a listagem: uma consulta ao disco por
    # arquivo em vez de três (glob + stat para ordenar + stat de novo)
    with os.scandir(config.SAIDA_DIR) as it:
        pdfs = [(e.name, e.stat()) for e in it if e.name.lower().endswith(".pdf") and e.is_file()]
    pdfs.sort(key=lambda x: x[1].st_mtime, reverse=True)
    return [{"nome": nome, "tamanho_kb": round(st.st_size / 1024), "modificado": st.st_mtime}
            for nome, st in pdfs[:limite]]
