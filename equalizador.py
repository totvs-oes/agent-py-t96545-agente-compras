"""Quadro comparativo da cotação e sugestão de vencedor (o comprador decide; nada é gravado no RM)."""
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

from pagamento import dias_do_nome
from rm_client import RmClient

load_dotenv()

FAIXA_DESVIO = float(os.environ.get("FAIXA_DESVIO", "0.20"))   # faixa oficial (G-9) ainda não definida
TAXA_MENSAL = float(os.environ.get("TAXA_MENSAL", "0"))       # ajuste financeiro desligado até o time definir
PRAZO_PAGTO_PADRAO = int(os.environ.get("PRAZO_PAGTO_PADRAO", "28"))
MIN_PROPOSTAS = int(os.environ.get("MIN_PROPOSTAS", "3"))
_FRETE = {"1": "CIF", "2": "FOB", "9": "Sem frete"}


def _f(valor, padrao=0.0) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return padrao


def _dias_pagamento(nome_cpg: str) -> int | None:
    dias = dias_do_nome(nome_cpg or "")
    return round(sum(dias) / len(dias)) if dias else None


def _fator_financeiro(dias: int | None) -> float:
    """Traz o valor a pagar para a data do prazo padrão: pagar depois vale menos, antes vale mais."""
    if not TAXA_MENSAL or dias is None:
        return 1.0
    return (1 + TAXA_MENSAL) ** ((PRAZO_PAGTO_PADRAO - dias) / 30)


def montar_quadro(doc) -> dict:
    """Lê a cotação (XML do CmpCotacaoData) e monta o quadro equalizado com a sugestão."""
    cab = doc.find("TCCOTACAO")
    data_cot = datetime.fromisoformat(cab.findtext("DATCOTACAO"))
    necessidade = cab.findtext("DATENTREGA")
    necessidade = datetime.fromisoformat(necessidade) if necessidade else None
    julgamento = cab.findtext("TIPOJULGAMENTO") or "G"

    fornecedores = {}
    for o in doc.findall("TCORCAMENTO"):
        cfo = o.findtext("CODCFO")
        fornecedores[cfo] = {
            "codcfo": cfo,
            "nome": o.findtext("NOMEFANTASIA") or cfo,
            "cpg": o.findtext("NOMECPG") or o.findtext("CODCPG"),
            "dias_pagto": _dias_pagamento(o.findtext("NOMECPG")),
            "frete_tipo": _FRETE.get(o.findtext("FRETECIFOUFOB"), o.findtext("FRETECIFOUFOB")),
            "frete_valor": _f(o.findtext("VALORFRETE")),
            "prazo": int(_f(o.findtext("VALPRAZOENTREGA"), -1)),
            "declinou": o.findtext("DECLINADO") == "1",
        }

    itens = {}
    for it in doc.findall("TCCOTACAOITMMOV"):
        chave = (it.findtext("IDMOV"), it.findtext("NSEQITMMOV"))
        itens[chave] = {
            "idprd": it.findtext("IDPRD"),
            "codigo": it.findtext("CODIGOPRD"),
            "descricao": (it.findtext("NOMEFANTASIA") or "").strip(),
            "quantidade": _f(it.findtext("QUANTIDADE")),
            "unidade": it.findtext("CODUND"),
            "preco_ref": _f(it.findtext("PRECOUNITARIO")),
            "propostas": [],
        }

    # Material cotado por fornecedor, para ratear o frete do orçamento entre os itens
    linhas = []
    for r in doc.findall("TCITMORCAMENTO"):
        preco = _f(r.findtext("VALCOTACAO"))
        chave = (r.findtext("IDMOV"), r.findtext("NSEQITMMOV"))
        if r.findtext("STSITEM") != "0" or preco <= 0 or chave not in itens:
            continue
        qtd = _f(r.findtext("QUANTIDADEORC")) or itens[chave]["quantidade"]
        prazo = int(_f(r.findtext("PRAZOENTREGA"), -1))
        linhas.append((chave, r.findtext("CODCFO"), preco, qtd, prazo))
    material_por_cfo = {}
    for _, cfo, preco, qtd, _ in linhas:
        material_por_cfo[cfo] = material_por_cfo.get(cfo, 0) + preco * qtd

    for chave, cfo, preco, qtd, prazo in linhas:
        forn = fornecedores.get(cfo, {"codcfo": cfo, "nome": cfo, "frete_valor": 0, "frete_tipo": None,
                                      "dias_pagto": None, "cpg": None, "prazo": -1})
        item = itens[chave]
        material = preco * qtd
        frete = forn["frete_valor"] * material / material_por_cfo[cfo] if material_por_cfo.get(cfo) else 0
        prazo = prazo if prazo >= 0 else forn["prazo"]
        alertas = []
        if forn["frete_tipo"] is None and not forn["frete_valor"]:
            alertas.append("frete não informado")
        if prazo < 0:
            alertas.append("prazo de entrega não informado")
        elif necessidade and data_cot + timedelta(days=prazo) > necessidade:
            alertas.append(f"entrega em {(data_cot + timedelta(days=prazo)):%d/%m} depois da necessidade "
                           f"({necessidade:%d/%m})")
        item["propostas"].append({
            "codcfo": cfo, "nome": forn["nome"], "preco": preco, "quantidade": qtd, "material": material,
            "frete": frete, "frete_estimado": False, "fator_fin": _fator_financeiro(forn["dias_pagto"]),
            "prazo": prazo, "cpg": forn["cpg"], "frete_tipo": forn["frete_tipo"], "alertas": alertas,
        })

    for item in itens.values():
        # FOB sem valor de frete parece mais barato do que é: estima o frete com o % médio das CIF do item
        refs = [p["frete"] / p["material"] for p in item["propostas"]
                if p["frete_tipo"] == "CIF" and p["frete"] > 0 and p["material"] > 0]
        for p in item["propostas"]:
            if p["frete_tipo"] == "FOB" and not p["frete"]:
                if refs:
                    pct = sum(refs) / len(refs)
                    p["frete"], p["frete_estimado"] = p["material"] * pct, True
                    p["alertas"].append(f"FOB: frete estimado em {pct:.1%} do material (média das propostas CIF)"
                                        .replace(".", ","))
                else:
                    p["alertas"].append("FOB: frete por conta da IBEN, não estimado (nenhuma CIF para comparar)")
            p["total"] = (p["material"] + p["frete"]) * p["fator_fin"]
            p["unitario_equalizado"] = p["total"] / p["quantidade"] if p["quantidade"] else 0

    for item in itens.values():
        item["propostas"].sort(key=lambda p: p["total"])
        item["alertas"] = []
        n = len(item["propostas"])
        if n == 0:
            item["alertas"].append("nenhuma proposta válida")
            continue
        if n < MIN_PROPOSTAS:
            item["alertas"].append(f"só {n} proposta(s); a regra pede {MIN_PROPOSTAS}")
        melhor = item["propostas"][0]
        if item["preco_ref"] > 0:
            desvio = melhor["unitario_equalizado"] / item["preco_ref"] - 1
            item["desvio_ref"] = desvio
            if abs(desvio) > FAIXA_DESVIO:
                item["alertas"].append(f"melhor custo {desvio:+.0%} em relação ao preço de referência "
                                       f"(faixa {FAIXA_DESVIO:.0%})")

    return {
        "codcotacao": cab.findtext("CODCOTACAO"),
        "julgamento": julgamento,
        "data_cotacao": data_cot,
        "necessidade": necessidade,
        "fornecedores": fornecedores,
        "itens": list(itens.values()),
        "sugestao": _sugerir(julgamento, list(itens.values()), fornecedores),
    }


def _sugerir(julgamento: str, itens: list[dict], fornecedores: dict) -> dict:
    """G = um fornecedor para tudo (menor total entre quem cotou todos os itens); P/I = melhor por item."""
    por_item = {it["codigo"]: it["propostas"][0] for it in itens if it["propostas"]}
    total_por_item = sum(p["total"] for p in por_item.values())

    completos = {}
    for cfo in fornecedores:
        props = [next((p for p in it["propostas"] if p["codcfo"] == cfo), None) for it in itens]
        if all(props):
            completos[cfo] = sum(p["total"] for p in props)
    ranking_unico = sorted(completos.items(), key=lambda kv: kv[1])

    if julgamento != "G":
        return {"tipo": "por item", "por_item": por_item, "total": total_por_item, "ranking_unico": ranking_unico}
    if not completos:
        return {"tipo": "por item", "por_item": por_item, "total": total_por_item,
                "obs": "julgamento global, mas nenhum fornecedor cotou todos os itens"}
    cfo, total = min(completos.items(), key=lambda kv: kv[1])
    return {"tipo": "global", "codcfo": cfo, "nome": fornecedores[cfo]["nome"], "total": total,
            "ranking": ranking_unico,
            "por_item": por_item, "total_por_item": total_por_item}


def equalizar(codcotacao: str, coligada: int = 42) -> dict:
    rm = RmClient(coligada)
    doc = rm.ler_cotacao(codcotacao)
    quadro = montar_quadro(doc)
    try:
        quadro["comprador"] = rm.comprador(doc.find("TCCOTACAO").findtext("CODCOMPRADOR"))
    except Exception:
        quadro["comprador"] = {}
    return quadro


def _br(v: float, casas: int = 2) -> str:
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def texto_quadro(q: dict) -> str:
    linhas = [f"Quadro comparativo – cotação {q['codcotacao']} (julgamento "
              f"{'global' if q['julgamento'] == 'G' else 'por item'})", ""]
    for it in q["itens"]:
        linhas.append(f"{it['codigo']} {it['descricao']} – {_br(it['quantidade'])} {it['unidade']} "
                      f"(referência R$ {_br(it['preco_ref'], 4)}/{it['unidade']})")
        for i, p in enumerate(it["propostas"], 1):
            marca = "→" if i == 1 else " "
            linhas.append(f"  {marca} {i}º {p['nome'][:32]:32} R$ {_br(p['preco'], 4):>10}/{it['unidade']}"
                          f"  frete R$ {_br(p['frete']):>9}{'*' if p['frete_estimado'] else ' '}"
                          f" total R$ {_br(p['total']):>12}"
                          f"  ({_br(p['unitario_equalizado'], 4)}/{it['unidade']})  prazo "
                          f"{p['prazo'] if p['prazo'] >= 0 else '?'}d  {p['cpg'] or '?'}  {p['frete_tipo'] or '?'}")
            for a in p["alertas"]:
                linhas.append(f"        ! {a}")
        for a in it["alertas"]:
            linhas.append(f"    ATENÇÃO: {a}")
        linhas.append("")

    s = q["sugestao"]
    linhas.append("SUGESTÃO DO AGENTE (o comprador decide):")
    if s["tipo"] == "global":
        linhas.append(f"  Fornecedor único: {s['codcfo']} {s['nome']} – total equalizado R$ {_br(s['total'])}")
        for cfo, total in s["ranking"][1:]:
            linhas.append(f"    depois: {cfo} {q['fornecedores'][cfo]['nome']} – R$ {_br(total)} "
                          f"(+{total / s['total'] - 1:.1%})")
        if s["total_por_item"] < s["total"]:
            linhas.append(f"  Comprando o melhor de cada item sairia R$ {_br(s['total_por_item'])} "
                          f"(economia de R$ {_br(s['total'] - s['total_por_item'])}), com mais de um fornecedor.")
    else:
        for cod, p in s["por_item"].items():
            linhas.append(f"  {cod}: {p['codcfo']} {p['nome']} – R$ {_br(p['total'])}")
        linhas.append(f"  Total: R$ {_br(s['total'])}")
        if s.get("obs"):
            linhas.append(f"  Obs.: {s['obs']}")
        if s.get("ranking_unico"):
            cfo, total = s["ranking_unico"][0]
            diferenca = total - s["total"]
            linhas.append(f"  Comprando tudo de um fornecedor só, o melhor seria {cfo} "
                          f"{q['fornecedores'][cfo]['nome']}: R$ {_br(total)}"
                          + (f" (R$ {_br(diferenca)} a mais)" if diferenca > 0.005 else ""))
    if any(p["frete_estimado"] for it in q["itens"] for p in it["propostas"]):
        linhas.append("  * frete estimado (proposta FOB): confirme o custo do transporte antes de decidir.")
    if TAXA_MENSAL:
        linhas.append(f"  Valores ajustados para pagamento em {PRAZO_PAGTO_PADRAO} dias à taxa de {TAXA_MENSAL:.2%} a.m.")
    else:
        linhas.append("  Sem ajuste financeiro pelo prazo de pagamento (TAXA_MENSAL não definida).")
    return "\n".join(linhas)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Quadro comparativo e sugestão de vencedor da cotação")
    p.add_argument("--cotacao", required=True)
    p.add_argument("--coligada", type=int, default=42)
    p.add_argument("--notificar", action="store_true", help="envia o quadro ao comprador")
    a = p.parse_args()

    quadro = equalizar(a.cotacao, a.coligada)
    print(texto_quadro(quadro))
    if a.notificar:
        from notificador import aviso_quadro, enviar
        print("\nQuadro enviado:", enviar(aviso_quadro(quadro, texto_quadro(quadro))))
