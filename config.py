# -*- coding: utf-8 -*-
"""
Configuração da Central IPD — caminhos, links e dados fixos.

Tudo que depende da máquina fica aqui. Se mudar de PC ou mover uma pasta, é só
ajustar este arquivo.
"""
import os
import sys
from pathlib import Path

# CONGELADO = rodando como CentralIPD.exe (PyInstaller). Aí tudo o que a Central
# precisa fica ao lado do .exe, em vez de Downloads/Área de Trabalho deste PC.
CONGELADO = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if CONGELADO else Path(__file__).parent
DATA_DIR = APP_DIR / "data"
# NUVEM = versão na nuvem, aberta pelo celular (variável CENTRAL_NUVEM no servidor).
# Lá só ficam Relatórios PPI/SISCAN, Fila, Propostas e Tabelas (app.ENDPOINTS_NUVEM), e os
# arquivos de apoio ficam ao lado do app, como no .exe.
NUVEM = bool(os.environ.get("CENTRAL_NUVEM"))
EMPACOTADO = CONGELADO or NUVEM
CACHE_DIR = DATA_DIR / "cache"
UPLOADS_DIR = DATA_DIR / "uploads"
# Saída do "Padronizar arquivo" (planilhas com a identidade visual do IPD).
PADRONIZADOS_DIR = DATA_DIR / "padronizados"

HOME = Path.home()
DOWNLOADS = HOME / "Downloads"
DESKTOP = HOME / "Desktop"

# --- Motor PPI / SISCAN ------------------------------------------------------
# Pasta com gerar_ppi_ipd.py, gerar_siscan.py, verificar_totais.py, listas/ e saida/.
GERAR_PPI_DIR = (Path(os.environ["GERAR_PPI_DIR"]) if os.environ.get("GERAR_PPI_DIR")
                 else APP_DIR / "motor_ppi" if EMPACOTADO else DOWNLOADS / "gerar_ppi")

# Mapa 3D (projeto em Downloads/minas-3d; a Central serve a cópia em mapa3d/)
MINAS3D_DIR = APP_DIR / "minas-3d" if EMPACOTADO else DOWNLOADS / "minas-3d"
MAPA3D_DIR = APP_DIR / "mapa3d"
MAPA3D_PLANILHA = HOME / "Desktop" / "Mapa3D_Consolidado_PPI.xlsx"
MAPA3D_ARQUIVO_DESTINO = HOME / "Desktop" / "Mapa3D_PPI.html"
# Automação do CRM (GesMkt; o RD Station saiu em out/2026): projeto com renovacoes.py, crms/, config.json e .env
CRM_DIR = APP_DIR / "automacao_crm" if EMPACOTADO else DOWNLOADS / "automacao-crm"

LISTAS_DIR = GERAR_PPI_DIR / "listas"
SAIDA_DIR = GERAR_PPI_DIR / "saida"

# Competência padrão (AAAAMM). Se vazio, usa a constante do gerar_ppi_ipd.py.
COMPETENCIA_PADRAO = ""

# Polo de destaque padrão do relatório por consórcio (código IBGE e nome).
POLO_PADRAO = {"codigo": "317070", "nome": "Varginha"}

# Modelos visuais oficiais dos relatórios por consórcio. A exportação preenche
# esses arquivos em vez de recriar uma planilha genérica, preservando logo,
# mesclagens, cores, fórmulas e espaçamentos aprovados.
MODELOS_CONSORCIO_DIR = DATA_DIR / "modelos_consorcio"
RELATORIO_MODELOS = {
    "cismip": MODELOS_CONSORCIO_DIR / "Relatorio_CISMIP_Final_1.xlsx",
    "cisamesp": MODELOS_CONSORCIO_DIR / "Relatorio_CISAMESP_Final_2.xlsx",
    "cissul": MODELOS_CONSORCIO_DIR / "Relatorio_CISSUL_Final.xlsx",
    "cismas": MODELOS_CONSORCIO_DIR / "Relatorio_CISMAS_Final.xlsx",
    "cisalp": MODELOS_CONSORCIO_DIR / "Relatorio_CISALP_Final.xlsx",
    "cislagos": MODELOS_CONSORCIO_DIR / "Relatorio_CISLAGOS_Final.xlsx",
    "cislav": MODELOS_CONSORCIO_DIR / "Relatorio_CISLAV_Final.xlsx",
    "cismarg": MODELOS_CONSORCIO_DIR / "Relatorio_CISMARG_Final.xlsx",
    "cismarpa": MODELOS_CONSORCIO_DIR / "Relatorio_CISMARPA_Final_1.xlsx",
    "louren": MODELOS_CONSORCIO_DIR / "Relatorio_CISSaoLourenco_Final.xlsx",
    "cissm": MODELOS_CONSORCIO_DIR / "Relatorio_CISSM_Final.xlsx",
    "cisver": MODELOS_CONSORCIO_DIR / "Relatorio_CISVER_Final.xlsx",
}
RELATORIO_MODELO_PADRAO = RELATORIO_MODELOS["cismip"]

# Classificação pela Forma de Organização SUS (6 primeiros dígitos do código):
# 020301xxxx = CITOPATOLÓGICO, 020302xxxx = ANATOPATOLÓGICO, 0202 = ANÁLISES CLÍNICAS.
PREFIXO_CITO = "020301"
PREFIXO_ANATO = "020302"

# --- Tabelas de preço ---------------------------------------------------------
TABELAS_DIR = DATA_DIR / "tabelas" if EMPACOTADO else DESKTOP / "Tabelas Exames - Propostas" / "TABELAS"
# Tabelas principais: exportações de procedimentos do Shift (CSV ";" latin-1,
# preço na coluna "Quantidade"). Arquivos .pdf no formato antigo também são lidos.
TABELAS_PRINCIPAIS = {
    "particular": TABELAS_DIR / "2026-10" / "PARTICULAR.csv",
    "avista": TABELAS_DIR / "2026-10" / "EMP_AV.csv",
    "faturado": TABELAS_DIR / "2026-10" / "EMP_FAT.csv",
    "plano": TABELAS_DIR / "2026-10" / "PLANO ASSISTENCIAL.csv",
    "saude_publica": TABELAS_DIR / "2026-10" / "SAUDE PÚBLICA.csv",
}
# Ordem do maior para o menor preço (é a ordem das colunas na consulta).
MODALIDADES = {
    "particular": "Particular 2026",
    "avista": "Empresas e Médicos — À vista 2026",
    "faturado": "Empresas e Médicos — Faturado 2026",
    "plano": "Plano Assistencial",
    "saude_publica": "Saúde Pública 2026",
}
# Fontes adicionais exibidas na consulta "Tabelas". Elas ficam separadas das
# três modalidades oficiais porque têm estruturas e regras de preço diferentes.
# A planilha CISSM preenchida_v2 é uma versão alternativa da mesma tabela e não
# é cadastrada novamente para não duplicar os resultados.
TABELAS_ADICIONAIS = {
    "cislav": {
        "nome": "CISLAV — tabela atualizada",
        "arquivo": TABELAS_DIR / "TABELA CISLAV - ATUALIZADA.xlsx",
        "tipo": "xlsx",
        # aparece também no seletor "Tabela de preço usada" da nova proposta
        "proposta": True,
    },
    "cissm": {
        "nome": "CISSM — São Sebastião do Paraíso",
        "arquivo": TABELAS_DIR / "TABELA CISSM - SÃO SEBASTIÃO PARAISO.xlsx",
        "tipo": "xlsx",
        "proposta": True,
    },
    # As duas de patologia não são escolhidas como "tabela de preço": entram por
    # cima dela nos exames que contêm (TBIO, IMUNMARC...), conforme REGRAS_TABELA.
    "patologia_cheia": {
        "nome": "Patologia cheia (valor inicial)",
        "arquivo": TABELAS_DIR / "2026-10" / "PATOLOGIA CHEIA.xlsx",
        "tipo": "xlsx",
    },
    "patologia_minima": {
        "nome": "Patologia mínima",
        "arquivo": TABELAS_DIR / "2026-10" / "PATOLOGIA MÍNIMA.xlsx",
        "tipo": "xlsx",
    },
    "sus_tuss": {
        "nome": "SUS/TUSS — patologia 2025",
        "arquivo": TABELAS_DIR / "TABELA SUS-TUSS (1).docx",
        "tipo": "docx",
    },
}
# Catálogo de procedimentos exportado do Shift (mnemônico, descrição, área laboratorial).
SHIFT_PROCEDIMENTOS_CSV = TABELAS_DIR / "28627_345_CLINICA DR5 ANDRADAS FAT_Procedimentos.csv"
TABELAS_CACHE = DATA_DIR / "tabelas.json"

# --- Propostas ----------------------------------------------------------------
# O catálogo lista só as propostas geradas pela Central (data/propostas). Para
# incluir outras pastas (Zoho, WorkDrive...), acrescente os caminhos aqui.
PROPOSTAS_PASTAS = [DATA_DIR / "propostas"]
PROPOSTAS_PASTAS_RASAS = []
PROPOSTAS_SAIDA = DATA_DIR / "propostas"
# Primeiro número sugerido para o ano corrente, enquanto não houver propostas
# geradas aqui (no WorkDrive a última foi a 089/2026).
PROPOSTA_NUMERO_INICIAL = 90
TEMPLATES_PROPOSTA = {
    "publica": DATA_DIR / "templates" / "saude_publica.docx",
    "privada": DATA_DIR / "templates" / "saude_privada.docx",
}
# Classificação da proposta (só para organizar/filtrar o catálogo; não vai no .docx)
CLASSIF_TIPO_ORCAMENTO = {
    "tabela_credenciamento": "Tabela completa — Novo Credenciamento",
    "tabela_pregao": "Tabela completa — Novo Pregão",
    "tabela_renovacao": "Tabela completa — Renovação",
    "os": "OS (alguns exames)",
    "avulsos": "Exame avulso",
}
CLASSIF_CLIENTE = {
    "consorcio": "Consórcio",
    "prefeitura": "Prefeitura",
    "hospital_publico": "Hospital público",
    "empresa_vista": "Empresas e médicos (à vista)",
    "empresa_faturado": "Empresas e médicos (faturado)",
}
CLASSIF_SITUACAO_CLIENTE = {"novo": "Novo", "atual": "Atual"}
CLASSIF_PRODUTOS = {
    "analises_clinicas": "Análises Clínicas",
    "anatomia_patologica": "Anatomia Patológica",
    "citologia": "Citologia",
}
CLASSIF_ETAPA = {"primeira": "Primeira proposta", "negociacao": "Negociação"}
# Qual tabela usar conforme a classificação (planilha Tabelas.xlsx, out/2026).
# (tipo_orcamento, cliente_categoria, etapa, cliente_situacao) -> (tabela, patologia)
# tabela None = "Acionar Controladoria" (a tela só avisa); patologia: "cheia",
# "minima" ou None (fica o preço da própria tabela). Combinação ausente = sem sugestão.
PATOLOGIAS = {"cheia": "patologia_cheia", "minima": "patologia_minima"}
def _regras_tabela():
    r = {}
    def por(tipo, cliente, pn, pa, nn, na):
        for (etapa, sit), v in zip((("primeira", "novo"), ("primeira", "atual"),
                                    ("negociacao", "novo"), ("negociacao", "atual")), (pn, pa, nn, na)):
            r[(tipo, cliente, etapa, sit)] = v
    C = None  # Acionar Controladoria
    por("tabela_credenciamento", "consorcio", ("cislav", "cheia"), (C, "cheia"), (C, "minima"), (C, "minima"))
    for cli in ("prefeitura", "hospital_publico"):
        por("tabela_credenciamento", cli, ("plano", "cheia"), (C, "cheia"), ("saude_publica", "minima"), (C, "minima"))
        por("tabela_pregao", cli, *[("particular", None)] * 4)
    por("tabela_credenciamento", "empresa_vista", *[("avista", None)] * 4)
    por("tabela_credenciamento", "empresa_faturado", ("faturado", None), ("faturado", None), (C, None), (C, None))
    for cli in CLASSIF_CLIENTE:
        por("tabela_renovacao", cli, *[(C, None)] * 4)
    for tipo in ("os", "avulsos"):
        por(tipo, "consorcio", ("plano", "cheia"), ("cislav", "cheia"), ("saude_publica", "minima"), ("saude_publica", "minima"))
        for cli in ("prefeitura", "hospital_publico"):
            por(tipo, cli, ("plano", "cheia"), ("plano", "cheia"), ("saude_publica", "minima"), ("saude_publica", "minima"))
        por(tipo, "empresa_vista", *[("avista", None)] * 4)
        por(tipo, "empresa_faturado", *[("faturado", None)] * 4)
    return r
REGRAS_TABELA = _regras_tabela()
OBJETO_PADRAO ="Prestação de serviços de apoio diagnóstico em Anatomia Patológica e Citopatológica."
# Quem pode assinar a proposta (escolhido na tela "Nova proposta"). O primeiro
# é o padrão e o nome dele precisa ser o que está escrito nos templates .docx.
# "imagem" (opcional): arquivo de assinatura em data/assinaturas, posto acima do nome.
ASSINATURAS_DIR = DATA_DIR / "assinaturas"
SIGNATARIOS = [
    {"id": "joao", "nome": "João Ricardo Maritan", "cargo": "Coordenador Comercial"},
    {"id": "solange", "nome": "Solange Serafim", "cargo": "Executiva Comercial de Contas Públicas"},
    {"id": "lucimar", "nome": "Lucimar Cardoso", "cargo": "Representante Comercial Contas Públicas"},
]
RAZAO_SOCIAL_ASSINATURA = "ASSOCIAÇÃO INSTITUTO DE PRESERVAÇÃO E DESENVOLVIMENTO - IPD"
CNPJ_IPD = "26.001.891/0001-94"
ASSINATURA = SIGNATARIOS[0]
CIDADE_EMISSAO = "Varginha"

# LibreOffice, para converter o .docx da proposta em PDF (opcional).
SOFFICE_CANDIDATOS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]

# CRM da empresa (GesMkt — substituiu o RD Station em out/2026). Sem login, o site
# pede a senha e depois volta para as negociações.
CRM_URL = "https://gesmkt-foggueiira.vercel.app/crm/negociacoes"

# --- Atalhos externos (abrem no navegador, no login de quem clicar) -----------
LINKS_EXTERNOS = [
    {"nome": "Zoho Mail", "url": "https://mail.zoho.com/", "descricao": "e-mail da empresa"},
    {"nome": "GesMkt", "url": CRM_URL,
     "descricao": "CRM — negociações"},
    {"nome": "Shift", "url": "https://ipd.shiftcloud.com.br/main/auth/login?returnUrl=%2Fapp",
     "descricao": "Shift Cloud — sistema do laboratório"},
]

# --- Servidor ------------------------------------------------------------------
# 127.0.0.1 = só este computador abre a Central. Desde 01/10/2026 cada pessoa
# usa o CentralIPD.exe no próprio PC e ninguém acessa pela rede, então a porta
# não fica exposta. Para voltar a aceitar os outros computadores da rede (todos
# abrindo http://NOME-DO-PC:5077), troque por "0.0.0.0" — aí valem SENHA_ACESSO
# e a liberação do firewall (LIBERAR_FIREWALL.bat).
HOST = "127.0.0.1"


def comando_python(script, *args):
    """Linha de comando para rodar um script .py como subprocesso. No .exe não há
    python instalado: o próprio CentralIPD.exe roda o script (--rodar-script)."""
    if CONGELADO:
        return [sys.executable, "--rodar-script", str(script), *args]
    return [sys.executable, "-u", str(script), *args]
PORT = 5077
# Nomes além do nome do PC, do localhost e de IPs pelos quais a Central pode ser
# aberta (ex.: um apelido criado pela TI, "central.ipd.local"). Qualquer outro
# nome é recusado — proteção contra sites que se passam pela Central.
NOMES_EXTRAS = []

# Senha pedida a quem acessa de OUTRO computador (nesta máquina não pede).
# Fica no arquivo .env ao lado do app (linha SENHA_ACESSO=...), fora do Git.
# Se o .env não existir, é criado com uma senha aleatória forte.
def _ler_senha_acesso():
    env = APP_DIR / ".env"
    if not env.exists():
        import secrets
        env.write_text(f"SENHA_ACESSO={secrets.token_urlsafe(18)}\n", encoding="utf-8")
    for linha in env.read_text(encoding="utf-8").splitlines():
        chave, _, valor = linha.partition("=")
        if chave.strip() == "SENHA_ACESSO":
            return valor.strip()
    return ""


# Na nuvem a senha vem da variável SENHA_ACESSO do servidor (sem .env).
SENHA_ACESSO = os.environ.get("SENHA_ACESSO", "") if NUVEM else _ler_senha_acesso()
if NUVEM:
    NOMES_EXTRAS = [h.strip().lower() for h in os.environ.get("CENTRAL_HOSTS", "").split(",") if h.strip()]
    # Número de partida do ano (para continuar de onde a equipe está).
    if os.environ.get("PROPOSTA_NUMERO_INICIAL", "").isdigit():
        PROPOSTA_NUMERO_INICIAL = int(os.environ["PROPOSTA_NUMERO_INICIAL"])
