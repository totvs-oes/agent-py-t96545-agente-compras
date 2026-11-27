import json
import os
import re
from difflib import SequenceMatcher
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

CONFIANCA_MINIMA = float(os.environ.get("TRADUCAO_CONFIANCA_MINIMA", "0.75"))


_PROMPT_TRADUCAO = """\
Você recebe a descrição de um item de um orçamento de fornecedor e uma lista de itens \
de uma cotação do ERP. Identifique qual item da cotação corresponde ao item do fornecedor, \
comparando descrição, unidade de medida e quantidade.

A unidade do fornecedor pode ser diferente da do ERP (ex.: fornecedor vende em milheiro, \
ERP compra em UN). Informe o fator de conversão: quantas unidades do ERP equivalem a 1 unidade \
do fornecedor (milheiro -> UN = 1000; mesma unidade = 1).

A especificação técnica decide o casamento, não a unidade: compare dimensões, espessura, bitola, \
seção (mm²), cor, amperagem, capacidade de interrupção (kA), curva, resistência (MPa), tipo \
(ex.: estrutural x vedação) e material. Nunca escolha um item só porque a unidade ou a quantidade \
é igual. Liste em "divergencias" cada especificação explícita nos dois lados que seja diferente \
(ex.: "espessura 10mm x 18mm", "cor verde x vermelho"). Diferença só de marca ou de forma de \
escrever não é divergência. Especificação que aparece só de um lado (ex.: o fornecedor não informa \
a cor) também NÃO é divergência.

Responda SOMENTE com JSON no formato:
{
  "indice": <índice 0-based do item da cotação que corresponde, ou null se nenhum>,
  "confianca": <0.0 a 1.0: certeza de que o item escolhido é o correto; 0 se indice for null>,
  "fator_conversao": <número; 1 se as unidades forem iguais>,
  "divergencias": [<especificações diferentes entre os dois itens; vazio se nenhuma>],
  "motivo": "<explicação curta>"
}
"""
_NUMERO_RE = re.compile(r"\d+(?:[.,]\d+)?")
_SO_AUSENCIA_RE = re.compile(r"n[ãa]o (especificad|informad|mencionad|indicad|consta)|ausente|sem informa", re.I)

_CLASSES_UNIDADE = {
    "UN": {"UN", "UND", "UNID", "UNIDADE", "UNIDADES", "PC", "PCS", "PÇ", "PÇS", "PCA", "PECA", "PEÇA", "PEÇAS"},
    "M": {"M", "MT", "MTS", "ML", "METRO", "METROS"},
    "KG": {"KG", "KGS", "QUILO", "QUILOS"},
    "RL": {"RL", "R", "ROLO", "ROLOS"},
    "MIL": {"MIL", "MILHEIRO", "MILHEIROS"},
    "BR": {"BR", "BARRA", "BARRAS"},
    "CX": {"CX", "CAIXA", "CAIXAS"},
    "M2": {"M2", "M²"},
    "M3": {"M3", "M³"},
    "L": {"L", "LT", "LITRO", "LITROS"},
    "SC": {"SC", "SACO", "SACOS"},
}


def _classe_unidade(unidade) -> str | None:
    if not unidade:
        return None
    token = re.split(r"[\s/.,]+", str(unidade).strip().upper())[0]
    for classe, nomes in _CLASSES_UNIDADE.items():
        if token in nomes:
            return classe
    return None


def _unidade_sem_conversao(item_forn: dict, item_rm: dict, fator: float) -> str | None:
    """Unidades de classes diferentes com fator 1 = conversão esquecida (ex.: metro x barra)."""
    if fator != 1:
        return None
    a, b = _classe_unidade(item_forn.get("unidade")), _classe_unidade(item_rm.get("unidade"))
    if a and b and a != b:
        return (f"unidades diferentes sem conversão: fornecedor em {item_forn.get('unidade')}, "
                f"RM em {item_rm.get('unidade')}")
    return None

TOLERANCIA_QUANTIDADE = 0.10
# Trava de sanidade (não é a faixa de aprovação de negócio, que ainda não foi definida)
PRECO_RAZAO_MIN = float(os.environ.get("PRECO_RAZAO_MIN", "0.2"))
PRECO_RAZAO_MAX = float(os.environ.get("PRECO_RAZAO_MAX", "5"))

def _quantidade_diverge(item_forn: dict, item_rm: dict, fator: float) -> str | None:
    """Confere qtd_forn × fator ≈ qtd_rm. Devolve o motivo se divergir, None se ok ou sem dados."""
    try:
        qtd_forn = float(item_forn.get("quantidade"))
        qtd_rm = float(item_rm.get("quantidade"))
    except (TypeError, ValueError):
        return None
    if qtd_rm <= 0:
        return None
    convertida = qtd_forn * fator
    if abs(convertida - qtd_rm) / qtd_rm > TOLERANCIA_QUANTIDADE:
        return (f"quantidade não bate: {qtd_forn} {item_forn.get('unidade')} × {fator:g} = "
                f"{convertida:g}, RM pede {qtd_rm:g} {item_rm.get('unidade')}")
    return None


def _preco_suspeito(item_forn: dict, item_rm: dict, fator: float) -> str | None:
    """Detecta preço com unidade trocada: contra o total da linha e contra o preço de referência do RM."""
    preco = item_forn.get("preco_unitario")
    if preco is None:
        return "sem preço unitário"
    try:
        qtd = float(item_forn.get("quantidade"))
        total = float(item_forn.get("valor_total"))
        if qtd > 0 and total > 0 and preco > 0:
            razao = total / (preco * qtd)
            if razao >= 10 or razao <= 0.1:
                return (f"preço × quantidade ({preco:g} × {qtd:g}) não bate com o total da linha "
                        f"({total:g}); unidade do preço provavelmente trocada")
    except (TypeError, ValueError):
        pass
    try:
        ref = float(item_rm.get("preco_ref"))
    except (TypeError, ValueError):
        return None
    if ref <= 0:
        return None
    convertido = preco / fator
    razao = convertido / ref
    if not PRECO_RAZAO_MIN <= razao <= PRECO_RAZAO_MAX:
        return (f"preço convertido {convertido:.4f}/{item_rm.get('unidade')} está {razao:.2f}× "
                f"o preço de referência {ref:.4f}")
    return None


def _normalizar(texto: str) -> str:
    return " ".join(texto.upper().split())

def _similaridade(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalizar(a), _normalizar(b)).ratio()


def _numeros(texto: str) -> set[float]:
    return {float(n.replace(",", ".")) for n in _NUMERO_RE.findall(texto or "")}


def _llm_casar(
        item_forn: dict,
        itens_rm: list[dict],
        client: OpenAI,
        model: str,
) -> dict:
    """ Pede ao LLm para identificar o item correspondente. Retorna dict com indice/confianca/motivo """
    lista = "\n".join(
        f"[{i}] cod={r['codigo']} desc={r['descricao']} qty={r['quantidade']} un={r['unidade']}"
        for i, r in enumerate(itens_rm)
    )

    pergunta = (
        f"Item do fornecedor:\n"
        f"  descricao={item_forn.get('descricao_fornecedor')}\n"
        f"  codigo_fornecedor={item_forn.get('codigo_fornecedor')}\n"
        f"  quantidade={item_forn.get('quantidade')}\n"
        f"  unidade={item_forn.get('unidade')}\n\n"
        f"Itens da cotação:\n{lista}"
    )

    resp = client.chat.completions.create(
        model=model,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _PROMPT_TRADUCAO},
            {"role": "user", "content": pergunta},
        ],
    )
    return json.loads(resp.choices[0].message.content)


def traduzir_itens(
    itens_fornecedor: list[dict],
    itens_rm: list[dict],
    tabela_traducao: dict[str, str] | None = None,
    token: str | None = None,
) -> list[dict]:
    """
    Casa cada item do fornecedor com um item da cotação do RM.

    Parâmetros:
        itens_fornecedor  : lista vinda do extrator.py
        itens_rm          : lista vinda de RmClient.itens_cotacao()
        tabela_traducao   : dict {cod_forn: CODIGOPRD_rm} carregado de TPRDCFOCOLAB
        token             : token LLM (fallback: TOKEN_API_LLM do .env)

    Retorno: lista de dicts com as chaves:
        item_forn    : item original do fornecedor
        item_rm      : item correspondente da cotação (ou None)
        confianca    : float 0-1
        metodo       : "exato_desc" | "exato_cod" | "tabela" | "llm" | "sem_match"
        aprovado     : True se confiança >= CONFIANCA_MINIMA
        motivo       : texto explicativo
        fator        : unidades do RM por unidade do fornecedor (preço RM = preço forn / fator)
    """

    tok = token or os.environ.get("TOKEN_API_LLM", "")
    base_url = os.environ.get("LLM_BASE_API_URL")
    model = os.environ.get("LLM_MODEL", "gpt-4o")

    if not base_url:
        raise RuntimeError("LLM_BASE_API_URL não configurado no .env")

    client = OpenAI(base_url=base_url, api_key=tok or "no-key")

    # Indice rápido por código RM
    por_codigo = {_normalizar(r["codigo"]): r for r in itens_rm}

    resultados = []

    for item_forn in itens_fornecedor:
        desc_forn = item_forn.get("descricao_fornecedor", "")
        cod_forn = str(item_forn.get("codigo_fornecedor") or "").strip()

        # 1. De-para do RM (TPRDCFOCOLAB). Só vale se o produto estiver nesta cotação:
        # a NF-e às vezes liga o código a um produto genérico ("FATURAMENTO DIRETO...").
        if tabela_traducao and cod_forn:
            cod_rm = tabela_traducao.get(cod_forn.upper())

            if cod_rm:
                item_rm = por_codigo.get(_normalizar(cod_rm))

                if item_rm:
                    motivo = f"código do fornecedor {cod_forn} -> {cod_rm} (TPRDCFOCOLAB)"
                    divergencia = _quantidade_diverge(item_forn, item_rm, 1.0)
                    resultados.append({
                        "item_forn": item_forn,
                        "item_rm": item_rm,
                        "confianca": 1.0,
                        "metodo": "tabela",
                        "aprovado": divergencia is None,
                        "motivo": f"{motivo} | {divergencia}" if divergencia else motivo,
                        "fator": 1.0,
                    })
                    continue

        # 2. Correspondência exata por descrição (fuzzy alto)
        melhor_score = 0.0
        melhor_rm = None

        for r in itens_rm:
            s = _similaridade(desc_forn, r["descricao"] or "")
            if s > melhor_score:
                melhor_score = s
                melhor_rm = r

        # "OSB 18MM" e "OSB 10MM" são quase idênticos como texto: só aceita se os números baterem
        if melhor_score >= 0.92 and _numeros(desc_forn) == _numeros(melhor_rm["descricao"]):
            resultados.append({
                "item_forn": item_forn,
                "item_rm": melhor_rm,
                "confianca": melhor_score,
                "metodo": "exato_desc",
                "aprovado": True,
                "motivo": f"similaridade {melhor_score:.2f}",
                "fator": 1.0,
            })
            continue


        # 3. LLM

        try:
            llm_resp = _llm_casar(item_forn, itens_rm, client, model)
            idx = llm_resp.get("indice")
            conf = float(llm_resp.get("confianca", 0.0))
            fator = float(llm_resp.get("fator_conversao") or 1.0)
            divergencias = [d for d in (llm_resp.get("divergencias") or [])
                            if d and not _SO_AUSENCIA_RE.search(str(d))]
            motivo = llm_resp.get("motivo", "")
            item_rm = itens_rm[idx] if idx is not None and 0 <= idx < len(itens_rm) else None
        except Exception as e:
            item_rm, conf, fator, divergencias, motivo = None, 0.0, 1.0, [], f"erro LLM: {e}"

        if item_rm is None:
            conf = 0.0
        aprovado = item_rm is not None and conf >= CONFIANCA_MINIMA and fator > 0
        if aprovado and divergencias:
            aprovado = False
            motivo = f"{motivo} | especificação divergente: {'; '.join(map(str, divergencias))}"
        if aprovado:
            divergencia = _quantidade_diverge(item_forn, item_rm, fator)
            if divergencia:
                aprovado = False
                motivo = f"{motivo} | {divergencia}"

        resultados.append({
            "item_forn": item_forn,
            "item_rm": item_rm,
            "confianca": conf,
            "metodo": "llm" if item_rm else "sem_match",
            "aprovado": aprovado,
            "motivo": motivo,
            "fator": fator,
        })

    for r in resultados:
        if r["aprovado"]:
            suspeita = (_unidade_sem_conversao(r["item_forn"], r["item_rm"], r["fator"])
                        or _preco_suspeito(r["item_forn"], r["item_rm"], r["fator"]))
            if suspeita:
                r["aprovado"] = False
                r["motivo"] = f"{r['motivo']} | {suspeita}"

    # Dois itens do fornecedor no mesmo item da cotação: um deles está errado, e não dá para saber qual
    por_item = {}
    for r in resultados:
        if r["aprovado"]:
            por_item.setdefault(r["item_rm"]["idprd"], []).append(r)
    for repetidos in por_item.values():
        if len(repetidos) > 1:
            nomes = ", ".join(str(r["item_forn"].get("descricao_fornecedor")) for r in repetidos)
            for r in repetidos:
                r["aprovado"] = False
                r["motivo"] = f"{r['motivo']} | mais de um item do fornecedor casou com o mesmo item da cotação ({nomes})"

    return resultados