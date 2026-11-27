"""Descobre a cotação do RM e o fornecedor a que uma proposta se refere."""
import re
import unicodedata
from difflib import SequenceMatcher

from rm_client import RmClient

_COTACAO_RE = re.compile(r"\b20\d{2}\.\d{6}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TELEFONE_RE = re.compile(r"\(?\d{2}\)?[\s.-]*9?[\s.]*\d{4}[\s.-]?\d{4}")

# Em homologação os e-mails dos fornecedores estão mascarados com o da IBEN, e domínios
# genéricos são de pessoas físicas: nenhum dos dois identifica a empresa
_DOMINIOS_SEM_VALOR = {"iben.eng.br", "construtoraiben.com.br", "gmail.com", "hotmail.com", "outlook.com",
                       "yahoo.com", "yahoo.com.br", "uol.com.br", "bol.com.br", "terra.com.br", "live.com"}
_PALAVRAS_GENERICAS = {"CERAMICA", "LTDA", "ME", "EPP", "EIRELI", "SA", "S/A", "COMERCIO", "INDUSTRIA", "DE",
                       "DO", "DA", "DOS", "DAS", "E", "MATERIAIS", "MATERIAL", "CONSTRUCAO", "DISTRIBUIDORA"}

SCORE_MINIMO = 0.8
MARGEM_MINIMA = 0.2


def _so_digitos(texto) -> str:
    return re.sub(r"\D", "", str(texto or ""))


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().upper()


def _tokens_distintivos(nome: str) -> set[str]:
    return {t for t in re.findall(r"[A-Z0-9]{3,}", _sem_acento(nome)) if t not in _PALAVRAS_GENERICAS}


def _dominio(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if email and "@" in email else ""


def identificar_cotacao(rm: RmClient, *, assunto: str = "", corpo: str = "", nome_arquivo: str = "",
                        texto_pdf: str = "") -> dict:
    """
    Procura números no padrão 2026.000402 (assunto > corpo > nome do arquivo > PDF) e
    mantém só os que existem no RM. Retorno: {"codcotacao", "doc", "motivo"} (codcotacao None se falhar).
    """
    candidatos = []
    for fonte, texto in (("assunto", assunto), ("corpo", corpo), ("arquivo", nome_arquivo), ("pdf", texto_pdf)):
        for numero in _COTACAO_RE.findall(texto or ""):
            if numero not in [c for c, _ in candidatos]:
                candidatos.append((numero, fonte))

    if not candidatos:
        return {"codcotacao": None, "doc": None, "motivo": "número da cotação não encontrado"}

    validos = []
    for numero, fonte in candidatos:
        try:
            doc = rm.ler_cotacao(numero)
        except Exception:
            continue
        if doc.find("TCCOTACAO") is not None:
            validos.append((numero, fonte, doc))

    if not validos:
        return {"codcotacao": None, "doc": None,
                "motivo": f"nenhum número encontrado existe no RM: {[c for c, _ in candidatos]}"}
    if len(validos) > 1:
        return {"codcotacao": None, "doc": None,
                "motivo": f"mais de uma cotação citada: {[n for n, _, _ in validos]}"}
    numero, fonte, doc = validos[0]
    return {"codcotacao": numero, "doc": doc, "motivo": f"encontrada no {fonte}"}


def _pontuar(forn: dict, *, cnpjs: set[str], emails: set[str], telefones: set[str], nome_extraido: str,
             texto: str) -> tuple[float, str]:
    """Maior evidência de que a proposta é deste fornecedor (0 a 1) e a descrição dela."""
    evidencias = []

    cnpj = _so_digitos(forn.get("cnpj"))
    if len(cnpj) >= 11 and cnpj in cnpjs:
        evidencias.append((1.0, f"CNPJ {forn['cnpj']}"))

    email_cad = (forn.get("email") or "").lower()
    if email_cad and _dominio(email_cad) not in _DOMINIOS_SEM_VALOR:
        if email_cad in emails:
            evidencias.append((0.95, f"e-mail {email_cad}"))
        elif _dominio(email_cad) in {_dominio(e) for e in emails}:
            evidencias.append((0.9, f"domínio {_dominio(email_cad)}"))

    tel = _so_digitos(forn.get("telefone"))[-8:]
    if len(tel) == 8 and tel in telefones:
        evidencias.append((0.8, f"telefone {forn['telefone']}"))

    tokens = _tokens_distintivos(forn.get("nome") or "")
    if tokens:
        texto_norm = _sem_acento(texto)
        presentes = {t for t in tokens if t in texto_norm}
        if presentes == tokens:
            evidencias.append((0.85, f"nome ({', '.join(sorted(tokens))}) no documento"))
        elif nome_extraido:
            sim = SequenceMatcher(None, " ".join(sorted(tokens)),
                                  " ".join(sorted(_tokens_distintivos(nome_extraido)))).ratio()
            if sim >= 0.8:
                evidencias.append((0.8 * sim, f"nome parecido ({nome_extraido}, {sim:.0%})"))

    if not evidencias:
        return 0.0, ""
    melhor = max(evidencias)
    reforco = 0.05 * (len(evidencias) - 1)
    return min(1.0, melhor[0] + reforco), "; ".join(e for _, e in sorted(evidencias, reverse=True))


def identificar_fornecedor(fornecedores: list[dict], *, proposta: dict | None = None, texto_pdf: str = "",
                           remetente: str = "", corpo: str = "") -> dict:
    """
    Escolhe, entre os fornecedores convidados da cotação, o que emitiu a proposta.
    Retorno: {"codcfo", "nome", "score", "motivo", "ranking"} (codcfo None se não houver evidência clara).
    """
    dados = (proposta or {}).get("fornecedor") or {}
    texto = " ".join(filter(None, [texto_pdf, corpo, remetente, " ".join(str(v) for v in dados.values() if v)]))

    cnpjs = {_so_digitos(m) for m in re.findall(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}", texto)}
    emails = {e.lower() for e in _EMAIL_RE.findall(texto)}
    telefones = {_so_digitos(t)[-8:] for t in _TELEFONE_RE.findall(texto)}

    ranking = []
    for forn in fornecedores:
        score, motivo = _pontuar(forn, cnpjs=cnpjs, emails=emails, telefones=telefones,
                                 nome_extraido=dados.get("nome") or "", texto=texto)
        ranking.append({"codcfo": forn["codcfo"], "nome": forn.get("nome"), "score": score, "motivo": motivo})
    ranking.sort(key=lambda r: r["score"], reverse=True)

    if not ranking or ranking[0]["score"] < SCORE_MINIMO:
        return {"codcfo": None, "nome": None, "score": ranking[0]["score"] if ranking else 0.0,
                "motivo": "nenhum fornecedor convidado com evidência suficiente", "ranking": ranking}
    if len(ranking) > 1 and ranking[0]["score"] - ranking[1]["score"] < MARGEM_MINIMA:
        return {"codcfo": None, "nome": None, "score": ranking[0]["score"],
                "motivo": f"empate entre {ranking[0]['codcfo']} e {ranking[1]['codcfo']}", "ranking": ranking}
    melhor = ranking[0]
    return {**melhor, "ranking": ranking}
