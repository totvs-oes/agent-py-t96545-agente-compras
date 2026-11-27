"""Traduz a condição de pagamento da proposta para o código (CODCPG) do RM."""
import os
import re

# Códigos que a IBEN usa nas cotações; desempatam nomes repetidos no cadastro ("28 DIAS" = 099 e "28 D")
_PREFERIDAS = [c.strip() for c in os.environ.get("CPG_PREFERIDAS", "099,012,003,025").split(",") if c.strip()]

_NOME_DIAS_RE = re.compile(r"^(ENTRADA\s*/\s*)?(\d{1,3}(?:\s*/\s*\d{1,3})*)\s*(DIAS|DIA|DDL|DD|D)$", re.I)
_TEXTO_DIAS_RE = re.compile(r"(?<![\d/.])(\d{1,3}(?:\s*/\s*\d{1,3})*)\s*(?:DIAS|DIA|DDL|DD|D)\b", re.I)


def dias_do_nome(nome: str) -> list[int] | None:
    """'30/60/90 DIAS' -> [30, 60, 90]; 'A VISTA' -> [0]; 'ENTRADA/30 DIAS' -> [0, 30]; parcelas/medição -> None."""
    nome = (nome or "").strip().upper()
    if nome in ("A VISTA", "À VISTA"):
        return [0]
    m = _NOME_DIAS_RE.match(nome)
    if not m:
        return None
    dias = [int(d) for d in re.split(r"\s*/\s*", m.group(2))]
    return [0] + dias if m.group(1) else dias


def dias_do_texto(texto: str) -> list[int] | None:
    """Plano B quando o LLM não devolve os dias: procura '28 DDL', '30/60 dias' no texto da condição."""
    texto = texto or ""
    if re.search(r"\b(à|a)\s+vista\b", texto, re.I):
        return [0]
    m = _TEXTO_DIAS_RE.search(texto)
    return [int(d) for d in re.split(r"\s*/\s*", m.group(1))] if m else None


def codcpg_para(dias: list[int] | None, condicoes: list[dict]) -> dict | None:
    """Condição do RM com exatamente os mesmos dias. None se não houver (o comprador decide)."""
    if not dias:
        return None
    candidatas = [c for c in condicoes if dias_do_nome(c["nome"]) == list(dias)]
    if not candidatas:
        return None

    def ordem(c):
        cod = c["codcpg"]
        preferida = _PREFERIDAS.index(cod) if cod in _PREFERIDAS else len(_PREFERIDAS)
        return (preferida, not (cod.isdigit() and len(cod) == 3), cod)

    return sorted(candidatas, key=ordem)[0]
