import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from extrator import extrair_proposta
from rm_client import RmClient
from tradutor import traduzir_itens

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

def _aplicar_precos_por_item(
    doc: ET.Element,
    fornecedor: str,
    mapa_idprd_preco: dict[str, float],
    mudancas: list[str],
):
    """Grava VALCOTACAO/VALNEGOCIADO/STSITEM item a item (por IDPRD)."""
    for tabela in ("TCITMORCAMENTO", "TCITMORCAMENTOAGRUPADO"):
        for row in doc.findall(tabela):
            if row.findtext("CODCFO") != fornecedor:
                continue
            
            idprd = row.findtext("IDPRD") or ""

            if idprd not in mapa_idprd_preco:
                continue

            preco = mapa_idprd_preco[idprd]
            chave = f"{tabela}[{fornecedor}|{idprd}]"

            for campo in ("VALCOTACAO", "VALNEGOCIADO"):
                el = row.find(campo)

                if el is None:
                    el = ET.SubElement(row, campo)

                novo = f"{preco:.4f}"

                if el.text != novo:
                    mudancas.append(f"{chave:<42} {campo:<18} {el.text!s:>12} -> {novo}")
                    el.text = novo

            el_sts = row.find("STSITEM")

            if el_sts is None:
                el_sts = ET.SubElement(row, "STSITEM")

            if el_sts.text != "0":
                mudancas.append(f"{chave:<42} {'STSITEM':<18} {el_sts.text!s:>12} -> 0")
                el_sts.text = "0"

def _dias(proposta: dict, campo: str, data_cotacao: datetime) -> int | None:
    """Usa <campo>_dias; se só houver <campo>_data, converte em dias a partir da data da cotação."""
    dias = proposta.get(f"{campo}_dias")
    if dias is not None:
        return int(dias)
    data = proposta.get(f"{campo}_data")
    if not data:
        return None
    try:
        delta = (datetime.fromisoformat(data).date() - data_cotacao.date()).days
    except ValueError:
        log.warning("Data inválida em %s_data: %r", campo, data)
        return None
    if delta < 0:
        log.warning("%s_data %s é anterior à data da cotação %s; ignorado", campo, data, data_cotacao.date())
        return None
    return delta


def _resumo_observacao(proposta: dict) -> str | None:
    partes = []
    if proposta.get("condicao_pagamento"):
        partes.append(f"Pgto: {proposta['condicao_pagamento']}")
    if proposta.get("frete"):
        partes.append(f"Frete: {proposta['frete']}")
    return " | ".join(partes) if partes else None


def registrar(
    caminho_pdf: str | Path,
    codcotacao: str,
    codfornecedor: str,
    coligada: int = 42,
    tabela_traducao: dict[str, str] | None = None,
    salvar: bool = False,
    token: str | None = None,
) -> dict:
    """
    Fluxo completo: PDF → extração → tradução → gravação no RM.

    Parâmetros:
        caminho_pdf      : caminho local do PDF da proposta
        codcotacao       : ex. "2026.000401"
        codfornecedor    : ex. "0018740"
        coligada         : padrão 42
        tabela_traducao  : dict {cod_forn_upper: CODIGOPRD_rm} de TPRDCFOCOLAB
        salvar           : False = simula, True = grava de verdade
        token            : token LLM (fallback: .env)

    Retorno: dict com chaves "aprovados", "pendentes", "mudancas", "chave_rm"
    """
    caminho = Path(caminho_pdf)

    log.info("Iniciando registro | cotacao=%s fornecedor=%s pdf=%s", codcotacao, codfornecedor, caminho.name)

    # Extraio proposta do PDF
    log.info("Extraindo proposta do PDF...")
    proposta = extrair_proposta(caminho, token=token)

    log.info(
        "Proposta extraída: %d item(ns) | prazo=%s | validade=%s | cpg=%s",
        len(proposta.get("itens", [])),
        proposta.get("prazo_entrega_dias"),
        proposta.get("validade_dias"),
        proposta.get("condicao_pagamento"),
    )

    # Leio cotação do RM
    log.info("Lendo cotação %s no RM...", codcotacao)

    rm = RmClient(coligada=coligada)
    doc = rm.ler_cotacao(codcotacao)
    itens_rm = rm.itens_cotacao(doc)

    log.info("Cotação lida: %d item(ns) no RM", len(itens_rm))

    if tabela_traducao is None:
        try:
            tabela_traducao = rm.traducoes_fornecedor(codfornecedor)
            log.info("De-para do fornecedor no RM (TPRDCFOCOLAB): %d código(s)", len(tabela_traducao))
        except Exception as e:
            log.warning("Não foi possível carregar o de-para do fornecedor; seguindo só com a descrição: %s", e)
            tabela_traducao = {}

    # Traduz itens do fornecedor -> Códigos RM
    log.info("Traduzindo itens...")
    traducoes = traduzir_itens(
        proposta.get("itens", []),
        itens_rm,
        tabela_traducao=tabela_traducao,
        token=token,
    )

    aprovados = [t for t in traducoes if t["aprovado"]]
    pendentes = [t for t in traducoes if not t["aprovado"]]

    log.info(
        "Tradução: %d aprovado(s) | %d pendente(s)",
        len(aprovados), len(pendentes),
    )

    for p in pendentes:
        log.warning(
            "Sem tradução aprovada | desc_forn=%r | confianca=%.2f | motivo=%s",
            p["item_forn"].get("descricao_fornecedor"),
            p["confianca"],
            p["motivo"],
        )

    if not aprovados:
        log.error("Nenhum item aprovado na tradução. Nada será gravado")
        return {"aprovados": [], "pendentes": pendentes, "mudancas": [], "chave_rm": None}


    # Aplica alterações no XML
    mudancas: list[str] = []

    data_cotacao = datetime.fromisoformat(doc.find("TCCOTACAO").findtext("DATCOTACAO"))

    contato = proposta.get("contato")
    if contato and "iben" in contato.lower():
        log.warning("Contato extraído parece ser da IBEN (%r); mantido o contato do cadastro", contato)
        contato = None

    #Cabecalho do orçamento (prazo, validade, contato, num_orc) - sem preco global
    mudancas += rm.aplicar_orcamento(
        doc,
        fornecedor=codfornecedor,
        preco=None,
        prazo=_dias(proposta, "prazo_entrega", data_cotacao),
        validade=_dias(proposta, "validade", data_cotacao),
        contato=contato,
        num_orcamento=proposta.get("num_orcamento"),
        observacao=_resumo_observacao(proposta),
    )

    #Precos por item (somente os aprovados na tradução), convertidos para a unidade do RM
    mapa_idprd_preco = {
        t["item_rm"]["idprd"]: t["item_forn"]["preco_unitario"] / t["fator"]
        for t in aprovados
        if t["item_rm"] and t["item_forn"].get("preco_unitario") is not None
    }
    _aplicar_precos_por_item(doc, codfornecedor, mapa_idprd_preco, mudancas)

    # 5 Exibe diff e (opcionalmente) salva
    log.info("%d alteração(ões) a aplicar:", len(mudancas))

    for m in mudancas:
        log.info("  %s", m)

    chave_rm = None
    if not salvar:
        log.info("Simulação apenas (salvar=False). Nada foi gravado.")
    else:
        log.info("Gravando no RM...")
        chave_rm = rm.salvar_cotacao(doc)
        log.info("Gravando. Chave RM: %s", chave_rm)

    return {
        "aprovados": aprovados,
        "pendentes": pendentes,
        "mudancas": mudancas,
        "chave_rm": chave_rm,
    }




# ── CLI simples para teste local ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Registra proposta de fornecedor na cotação do RM")
    p.add_argument("pdf",           help="caminho do PDF da proposta")
    p.add_argument("--cotacao",     required=True, help="ex: 2026.000401")
    p.add_argument("--fornecedor",  required=True, help="ex: 0018740")
    p.add_argument("--coligada",    type=int, default=42)
    p.add_argument("--salvar",      action="store_true", help="grava de verdade")
    a = p.parse_args()

    resultado = registrar(
        caminho_pdf=a.pdf,
        codcotacao=a.cotacao,
        codfornecedor=a.fornecedor,
        coligada=a.coligada,
        salvar=a.salvar,
    )

    print(f"\nAprovados : {len(resultado['aprovados'])}")
    print(f"Pendentes : {len(resultado['pendentes'])}")
    print(f"Mudanças  : {len(resultado['mudancas'])}")
    if resultado["chave_rm"]:
        print(f"Chave RM  : {resultado['chave_rm']}")
    if resultado["pendentes"]:
        print("\nItens sem tradução aprovada (encaminhar ao comprador):")
        for item in resultado["pendentes"]:
            print(f"  {item['item_forn'].get('descricao_fornecedor')} "
                  f"[confiança {item['confianca']:.0%}] — {item['motivo']}")