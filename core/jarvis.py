"""Jarvis: chat da Central que responde perguntas usando os dados do próprio sistema.

Protótipo só de consulta: as ferramentas leem dados (metas, tabelas, propostas,
fila, consórcios) e nunca gravam nada. A chave fica no .env (ANTHROPIC_API_KEY=...).
"""
import json
import os

import config
from core import metas, ppi_engine, propostas, tabelas
from core.jobs import gerenciador

MODELO = "claude-haiku-5-5"

SISTEMA = """Você é o Jarvis, assistente da Central IPD (ipd Exames e Saúde, Minas Gerais).
A Central junta os setores: relatórios PPI/SISCAN de citologia, metas dos municípios,
consórcios, propostas comerciais, tabelas de preço e CRM.
Responda em português do Brasil, curto e direto. Use as ferramentas para buscar dados
reais; nunca invente número, preço ou município. Se a ferramenta não trouxer o dado,
diga que não achou. Quando ajudar, indique a página da Central onde a pessoa resolve
(ex.: /metas, /propostas, /tabelas, /fila, /relatorios, /propostas/nova).
Você só consulta: não gera relatório, não cria proposta e não altera nada."""


def _chave():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = config.APP_DIR / ".env"
    if env.is_file():
        for linha in env.read_text(encoding="utf-8").splitlines():
            k, _, v = linha.partition("=")
            if k.strip() == "ANTHROPIC_API_KEY":
                return v.strip()
    return ""


def disponivel():
    return bool(_chave())


# --- ferramentas (só leitura) -----------------------------------------------------

def _metas(municipio="", situacao=""):
    s = metas.situacao()
    linhas = s["linhas"]
    if municipio:
        alvo = metas.slug_cidade(municipio)
        linhas = [l for l in linhas if alvo in l["slug"]]
    if situacao:
        linhas = [l for l in linhas if l["classe"] == situacao]
    return {
        "resumo": {"abaixo": s["n_abaixo"], "atencao": s["n_atencao"], "ok": s["n_ok"]},
        "municipios": [{"cidade": l["cidade"], "ultimo_mes": l["mes"], "qtd_mes": l["qtd"],
                        "meta_mensal": round(l["meta"]), "pct_acumulado_ano": round(l["pct"]),
                        "situacao": l["classe"], "meses_seguidos_abaixo": l["seguidos"],
                        "historico": l["historico"], "pagina": f"/metas/{l['slug']}"}
                       for l in linhas[:30]],
        "sem_meta": s["sem_meta"][:30],
    }


def _tabela(busca):
    if not config.TABELAS_CACHE.is_file():
        return {"erro": "tabela de preços ainda não importada (página /tabelas)"}
    return {"itens": tabelas.buscar(busca, limite=15)}


def _propostas(busca="", limite=10):
    itens = propostas.catalogo()
    if busca:
        q = busca.lower()
        itens = [i for i in itens if q in i["cliente"].lower() or q in i["arquivo"].lower() or q == str(i["numero"])]
    return {"total": len(itens), "proximo_numero": propostas.proximo_numero(),
            "propostas": [{"numero": i["numero"], "ano": i["ano"], "cliente": i["cliente"],
                           "modificado": i["modificado"], "tipo": i["tipo"],
                           "classificacao": propostas.rotulos_classificacao(i["classif"])}
                          for i in itens[:max(1, min(int(limite), 30))]]}


def _fila():
    return {"trabalhos": [{"titulo": j.titulo, "tipo": j.tipo, "status": j.status, "criado_em": j.criado_em,
                           "progresso": f"{j.feito}/{j.total}" if j.total else ""}
                          for j in gerenciador.recentes(15)]}


def _consorcios(nome=""):
    listas = ppi_engine.listar_listas()
    if nome:
        q = nome.lower()
        listas = [l for l in listas if q in l["nome"].lower() or q in l["titulo"].lower()]
    return {"consorcios": [{"nome": l["nome"], "titulo": l["titulo"], "n_municipios": len(l["cidades"]),
                            "municipios": l["cidades"]} for l in listas]}


FERRAMENTAS = {
    "consultar_metas": (_metas, "Metas de citologia (SISCAN x meta PPI) por município: quantidade do último mês, "
                        "meta mensal, % acumulado no ano e situação (abaixo, atencao, ok).",
                        {"municipio": {"type": "string", "description": "Nome do município (opcional)"},
                         "situacao": {"type": "string", "enum": ["abaixo", "atencao", "ok"],
                                      "description": "Filtrar pela situação (opcional)"}}),
    "buscar_tabela_precos": (_tabela, "Busca exames na tabela de preços por mnemônico ou palavras da descrição.",
                             {"busca": {"type": "string", "description": "Mnemônico ou nome do exame"}}),
    "listar_propostas": (_propostas, "Lista propostas comerciais já feitas (mais novas primeiro), filtrando por "
                         "cliente ou número, e informa o próximo número livre.",
                         {"busca": {"type": "string", "description": "Cliente ou número (opcional)"},
                          "limite": {"type": "integer", "description": "Quantas trazer (padrão 10)"}}),
    "ver_fila": (_fila, "Trabalhos recentes da Central (relatórios, importações, CRM) e o status de cada um.", {}),
    "listar_consorcios": (_consorcios, "Consórcios cadastrados e seus municípios.",
                          {"nome": {"type": "string", "description": "Parte do nome do consórcio (opcional)"}}),
}

_TOOLS = [{"name": n, "description": d, "input_schema": {"type": "object", "properties": p}}
          for n, (_, d, p) in FERRAMENTAS.items()]


def _executar(nome, entrada):
    func = FERRAMENTAS[nome][0]
    try:
        return json.dumps(func(**(entrada or {})), ensure_ascii=False, default=str)[:20000], False
    except Exception as e:  # noqa: BLE001
        return f"Erro ao consultar: {e}", True


def responder(historico):
    """historico: [{"role": "user"|"assistant", "content": "texto"}] -> (resposta, ferramentas usadas)."""
    import anthropic

    client = anthropic.Anthropic(api_key=_chave())
    mensagens = [{"role": m["role"], "content": str(m["content"])[:4000]} for m in historico[-20:]
                 if m.get("role") in ("user", "assistant") and m.get("content")]
    usadas = []
    for _ in range(6):
        r = client.messages.create(model=MODELO, max_tokens=4000, system=SISTEMA, tools=_TOOLS,
                                   output_config={"effort": "low"}, cache_control={"type": "ephemeral"},
                                   messages=mensagens)
        if r.stop_reason != "tool_use":
            texto = "".join(b.text for b in r.content if b.type == "text").strip()
            return texto or "Não consegui responder isso.", usadas
        mensagens.append({"role": "assistant", "content": r.content})
        resultados = []
        for b in r.content:
            if b.type == "tool_use":
                usadas.append(b.name)
                saida, erro = _executar(b.name, b.input)
                resultados.append({"type": "tool_result", "tool_use_id": b.id, "content": saida, "is_error": erro})
        mensagens.append({"role": "user", "content": resultados})
    return "A pergunta precisou de consultas demais; tente ser mais específico.", usadas
