import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

_PDF_API_URL = os.environ.get("PDF_EXTRACTOR_URL", "http://localhost:8000")
_TOKEN = os.environ.get("TOKEN_API_LLM", "")

_PROMPT = (
    "Você é um assistente de compras. Extraia do orçamento do fornecedor os dados abaixo. "
    "Use null quando a informação não estiver presente. "
    "Para itens com múltiplas linhas de produto, liste todos. "
    "Preços sempre em reais, sem símbolo. preco_unitario é o preço do material na unidade de venda do fornecedor, "
    "copiado exatamente como aparece no documento: NUNCA converta unidade nem divida/multiplique o valor "
    "(se o documento diz R$ 2.694,81 por milheiro, preco_unitario = 2694.81 e unidade = milheiro). "
    "Se houver preço FOB e CIF, use o FOB (sem frete). valor_total é o total da linha como aparece no documento. "
    "Unidade: copie a unidade de venda como está no documento (ex.: UN, milheiro, MIL PÇS, CX, M3). "
    "contato é o vendedor/representante do FORNECEDOR que assina ou emite a proposta; "
    "nunca use o nome ou e-mail do cliente (comprador, obra, IBEN). "
    "Em fornecedor, preencha os dados da empresa que EMITE a proposta (razão social, CNPJ, e-mail, telefone), "
    "nunca os do cliente. "
    "Preencha prazo_entrega somente se o documento disser explicitamente o prazo de ENTREGA do material; "
    "não use prazo de pagamento (ex.: '30 DD', '15 DD' do frete), validade nem prazo de faturamento. "
    "Prazo de entrega e validade: se o documento informar em dias, preencha os campos *_dias; "
    "se informar uma data, preencha os campos *_data no formato AAAA-MM-DD e deixe *_dias null. "
    "Nunca calcule dias a partir de datas."
)

_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "num_orcamento": {"type": ["string", "null"]},
        "fornecedor": {
            "type": "object",
            "properties": {
                "nome": {"type": ["string", "null"]},
                "cnpj": {"type": ["string", "null"]},
                "email": {"type": ["string", "null"]},
                "telefone": {"type": ["string", "null"]},
            },
        },
        "contato": {"type": ["string", "null"]},
        "condicao_pagamento": {"type": ["string", "null"]},
        "prazo_entrega_dias": {"type": ["integer", "null"]},
        "prazo_entrega_data": {"type": ["string", "null"], "format": "date"},
        "validade_dias": {"type": ["integer", "null"]},
        "validade_data": {"type": ["string", "null"], "format": "date"},
        "frete": {"type": ["string", "null"]},
        "itens": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "descricao_fornecedor": {"type": "string"},
                    "codigo_fornecedor":    {"type": ["string", "null"]},
                    "quantidade":          {"type": ["number", "null"]},
                    "unidade":             {"type": ["string", "null"]},
                    "preco_unitario":      {"type": ["number", "null"]},
                    "valor_total":         {"type": ["number", "null"]},
                },
                "required": ["descricao_fornecedor", "preco_unitario"],
            },
        },
    },
    "required": ["itens"],
})


def transcrever_pdf(caminho_pdf: str | Path) -> str:
    """Texto bruto do PDF (sem LLM). Usado para achar números com regex: cotação, CNPJ, telefone."""
    caminho = Path(caminho_pdf)
    with caminho.open("rb") as f:
        resp = requests.post(
            f"{_PDF_API_URL}/api/v1/transcricoes/",
            files={"arquivo": (caminho.name, f, "application/pdf")},
            timeout=60,
        )
    if not resp.ok:
        raise RuntimeError(f"Erro na API de transcrição[{resp.status_code}]: {resp.text[:300]}")
    return resp.json().get("texto", "")


def extrair_proposta(caminho_pdf: str | Path, token: str | None = None) -> dict:
    """ 
        Envia o PDF para a API de extração e retorna o dict da proposta.
        Levanta RuntimeError em caso de falha HTTP.
    """
    caminho = Path(caminho_pdf)
    tok = token or _TOKEN

    with caminho.open("rb") as f:
        resp = requests.post(
            f"{_PDF_API_URL}/api/v1/extracoes/",
            headers={"Authorization": f"Bearer {tok}"} if tok else {},
            files={"arquivo": (caminho.name, f, "application/pdf")},
            data={"prompt": _PROMPT, "schema": _SCHEMA, "temperature": 0.0},
            timeout=60,
        )

    if not resp.ok:
        raise RuntimeError(
            f"Erro na API de extração[{resp.status_code}]: {resp.text[:300]}"
        )

    body = resp.json()
    return body["resultado"]