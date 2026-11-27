import json
import os
import sys
from pathlib import Path

# garante que o diretório raiz do projeto está no path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from extrator import extrair_proposta
from registrador import registrar
from rm_client import RmClient

# ── Configuração do teste ────────────────────────────────────────────────────

PASTA_PDFS = Path(os.environ.get("PASTA_PDFS", r"C:\iben\agente2\PROCESSOS"))
CODCOTACAO  = os.environ.get("TEST_CODCOTACAO",  "2026.000401")
CODFORNECEDOR = os.environ.get("TEST_FORNECEDOR", "0018740")
COLIGADA    = int(os.environ.get("TEST_COLIGADA", "42"))
# "trecho do nome do PDF=CODCFO;..." — PDF sem correspondência usa TEST_FORNECEDOR
FORNECEDOR_POR_PDF = dict(
    par.split("=", 1) for par in os.environ.get("TEST_FORNECEDOR_POR_PDF", "").split(";") if "=" in par
)


def fornecedor_do_pdf(pdf: Path) -> str:
    for trecho, codcfo in FORNECEDOR_POR_PDF.items():
        if trecho.strip().lower() in pdf.name.lower():
            return codcfo.strip()
    return CODFORNECEDOR

# ── Utilitários de saída ─────────────────────────────────────────────────────

SEP = "─" * 72


def titulo(texto: str):
    print(f"\n{SEP}")
    print(f"  {texto}")
    print(SEP)


def ok(msg: str):   print(f"  ✔  {msg}")
def warn(msg: str): print(f"  ⚠  {msg}")
def erro(msg: str): print(f"  ✘  {msg}")


# ── Passo 1: verifica ambiente ───────────────────────────────────────────────

titulo("1 / Verificação de ambiente")

for var in ("RM_URL", "RM_USER", "RM_PASS", "PDF_EXTRACTOR_URL", "LLM_BASE_API_URL", "TOKEN_API_LLM"):
    val = os.environ.get(var)
    if val:
        exibir = val if "PASS" not in var and "TOKEN" not in var else "***"
        ok(f"{var} = {exibir}")
    else:
        warn(f"{var} não configurado")

pdfs = sorted(
    p for p in PASTA_PDFS.glob("*.pdf") if not p.name.lower().startswith("solicita")
) if PASTA_PDFS.exists() else []
if pdfs:
    ok(f"{len(pdfs)} PDF(s) encontrado(s) em {PASTA_PDFS}")
else:
    erro(f"Nenhum PDF em {PASTA_PDFS} — ajuste PASTA_PDFS no .env")
    sys.exit(1)


# ── Passo 2: lê a cotação do RM ──────────────────────────────────────────────

titulo(f"2 / Leitura da cotação {CODCOTACAO} no RM")

try:
    rm = RmClient(coligada=COLIGADA)
    doc = rm.ler_cotacao(CODCOTACAO)
    itens_rm = rm.itens_cotacao(doc)
    ok(f"Cotação lida — {len(itens_rm)} item(ns)")
    for it in itens_rm:
        print(f"     [{it['codigo']}] {it['descricao']}  qty={it['quantidade']}  un={it['unidade']}")
except Exception as exc:
    erro(f"Falha ao ler cotação: {exc}")
    sys.exit(1)


# ── Passo 3: extrai cada PDF e mostra a proposta ─────────────────────────────

titulo(f"3 / Extração dos PDFs ({len(pdfs)} arquivo(s))")

propostas: list[tuple[Path, dict]] = []

for pdf in pdfs:
    print(f"\n  → {pdf.name}")
    try:
        proposta = extrair_proposta(pdf)
        ok(f"Extraído: {len(proposta.get('itens', []))} item(ns)  "
           f"prazo={proposta.get('prazo_entrega_dias')}d/{proposta.get('prazo_entrega_data')}  "
           f"validade={proposta.get('validade_dias')}d/{proposta.get('validade_data')}  "
           f"cpg={proposta.get('condicao_pagamento')!r}")
        for i, item in enumerate(proposta.get("itens", []), 1):
            print(f"     [{i}] {item.get('descricao_fornecedor')}  "
                  f"cod={item.get('codigo_fornecedor')}  "
                  f"qty={item.get('quantidade')}  "
                  f"un={item.get('unidade')}  "
                  f"preco={item.get('preco_unitario')}")
        propostas.append((pdf, proposta))
    except Exception as exc:
        erro(f"Falha na extração de {pdf.name}: {exc}")


# ── Passo 4: fluxo completo (simulação, sem gravar) ──────────────────────────

titulo("4 / Simulação do fluxo completo")

erros_fluxo = 0
total_aprovados = 0

for pdf, _ in propostas:
    codcfo = fornecedor_do_pdf(pdf)
    print(f"\n  → {pdf.name}  [fornecedor {codcfo}]")
    try:
        resultado = registrar(
            caminho_pdf=pdf,
            codcotacao=CODCOTACAO,
            codfornecedor=codcfo,
            coligada=COLIGADA,
            salvar=False,          # nunca grava neste teste
        )

        aprov = resultado["aprovados"]
        pend  = resultado["pendentes"]
        mud   = resultado["mudancas"]
        total_aprovados += len(aprov)

        ok(f"{len(aprov)} item(ns) aprovado(s)  |  {len(pend)} pendente(s)  |  {len(mud)} mudança(s)")

        for t in aprov:
            print(f"     ✔ {t['item_forn'].get('descricao_fornecedor')!r:40s} "
                  f"→ {t['item_rm']['codigo']}  "
                  f"[{t['metodo']} {t['confianca']:.0%}, fator {t['fator']:g}]")

        for t in pend:
            print(f"     ⚠ {t['item_forn'].get('descricao_fornecedor')!r:40s} "
                  f"sem match  [{t['confianca']:.0%}]  {t['motivo']}")

        if mud:
            print(f"\n     Diff a aplicar:")
            for m in mud:
                print(f"       {m}")
        else:
            warn("Nenhuma alteração calculada (proposta sem dados novos?)")

    except Exception as exc:
        erro(f"Erro no fluxo de {pdf.name}: {exc}")
        erros_fluxo += 1


# ── Resumo final ─────────────────────────────────────────────────────────────

titulo("Resumo")
print(f"  PDFs testados : {len(pdfs)}")
print(f"  Extrações OK  : {len(propostas)}")
print(f"  Erros no fluxo: {erros_fluxo}")
print(f"  Itens casados : {total_aprovados}")

if total_aprovados == 0:
    warn("Nenhum item casou com a cotação — confira se a cotação é do mesmo produto dos PDFs")
elif erros_fluxo == 0 and len(propostas) == len(pdfs):
    ok("Tudo certo — rode com salvar=True quando quiser gravar no RM")
else:
    warn("Revise os erros acima antes de gravar")