"""
Agente 2 de ponta a ponta: lê os e-mails de proposta, registra no RM e equaliza as cotações que fecharam.

A caixa de entrada, por enquanto, é uma pasta de arquivos .eml (o formato que o IMAP também entrega).
Sem --salvar é tudo simulação: nada é gravado no RM e nenhum e-mail é movido.
"""
import argparse
import hashlib
import json
import logging
import re
import shutil
import tempfile
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv

from equalizador import MIN_PROPOSTAS, equalizar, texto_quadro
from notificador import _REMETENTE, _destinatario, aviso_quadro, enviar
from registrador import registrar
from rm_client import RmClient

load_dotenv()
log = logging.getLogger("agente2")


def _ler_email(caminho: Path) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(caminho.read_bytes())


def _corpo(msg: EmailMessage) -> str:
    parte = msg.get_body(preferencelist=("plain", "html"))
    if parte is None:
        return ""
    texto = parte.get_content()
    return re.sub(r"<[^>]+>", " ", texto) if parte.get_content_subtype() == "html" else texto


def _pdfs(msg: EmailMessage, destino: Path) -> list[Path]:
    arquivos = []
    for anexo in msg.iter_attachments():
        nome = anexo.get_filename() or ""
        if nome.lower().endswith(".pdf") or anexo.get_content_type() == "application/pdf":
            arq = destino / (Path(nome).name or f"anexo{len(arquivos) + 1}.pdf")
            arq.write_bytes(anexo.get_payload(decode=True))
            arquivos.append(arq)
    return arquivos


def _aviso_sem_pdf(assunto: str, remetente: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[Agente de Compras] E-mail sem proposta em PDF: {assunto or '(sem assunto)'}"
    msg["From"] = _REMETENTE
    msg["To"] = _destinatario({})
    msg.set_content(
        "Olá,\n\nChegou na caixa de compras um e-mail sem proposta em PDF anexada. O agente ainda não lê "
        "proposta escrita no corpo do e-mail ou em planilha, então nada foi lançado no RM.\n\n"
        f"Assunto: {assunto or '(sem assunto)'}\nDe: {remetente or '(desconhecido)'}\n\n"
        "O que fazer: abra o e-mail na caixa compras@ e lance os valores na cotação.\n\n"
        "— Agente de Compras IBEN (mensagem automática)")
    return msg


def processar_email(caminho: Path, *, salvar: bool, notificar: bool) -> dict:
    msg = _ler_email(caminho)
    assunto = str(msg.get("Subject", ""))
    remetente = parseaddr(str(msg.get("From", "")))[1]
    corpo = _corpo(msg)
    saida = {"email": caminho.name, "assunto": assunto, "remetente": remetente, "propostas": []}

    with tempfile.TemporaryDirectory() as tmp:
        pdfs = _pdfs(msg, Path(tmp))
        if not pdfs:
            saida["situacao"] = "sem PDF"
            if notificar:
                saida["aviso"] = enviar(_aviso_sem_pdf(assunto, remetente))
            return saida
        for pdf in pdfs:
            r = registrar(pdf, salvar=salvar, notificar=notificar, assunto=assunto, corpo=corpo, remetente=remetente)
            saida["propostas"].append({
                "arquivo": pdf.name, "codcotacao": r.get("codcotacao"), "codfornecedor": r.get("codfornecedor"),
                "fornecedor": r.get("fornecedor_nome"), "aprovados": len(r.get("aprovados") or []),
                "pendentes": len(r.get("pendentes") or []), "erro": r.get("erro"), "gravado": bool(r.get("chave_rm")),
                "aviso": r.get("aviso"),
            })
    saida["situacao"] = ("com pendência" if any(p["erro"] or p["pendentes"] for p in saida["propostas"])
                         else "registrado")
    return saida


def _situacao_cotacao(rm: RmClient, codcotacao: str) -> dict:
    doc = rm.ler_cotacao(codcotacao)
    cab = doc.find("TCCOTACAO")
    limite = datetime.fromisoformat(cab.findtext("DATLIMRESPTA")) if cab.findtext("DATLIMRESPTA") else None
    cotados = {r.findtext("CODCFO") for r in doc.findall("TCITMORCAMENTO")
               if r.findtext("STSITEM") == "0" and float(r.findtext("VALCOTACAO") or 0) > 0}
    assinatura = hashlib.sha1(json.dumps(sorted(
        (r.findtext("CODCFO"), r.findtext("IDPRD"), r.findtext("VALCOTACAO"), r.findtext("STSITEM"))
        for r in doc.findall("TCITMORCAMENTO"))
        + sorted((o.findtext("CODCFO"), o.findtext("VALORFRETE"), o.findtext("CODCPG"), o.findtext("FRETECIFOUFOB"))
                 for o in doc.findall("TCORCAMENTO"))).encode()).hexdigest()
    prazo_vencido = bool(limite and datetime.now() > limite)
    return {"encerrada": cab.findtext("STSCOTACAO") == "6",
            "propostas": len(cotados), "limite": limite, "prazo_vencido": prazo_vencido,
            "fechada": prazo_vencido or len(cotados) >= MIN_PROPOSTAS, "assinatura": assinatura}


def rodar(caixa: Path, *, salvar: bool, notificar: bool, coligada: int = 42) -> dict:
    caixa.mkdir(parents=True, exist_ok=True)
    arq_estado = caixa / "estado.json"
    estado = json.loads(arq_estado.read_text(encoding="utf-8")) if arq_estado.exists() else {}
    estado.setdefault("cotacoes", {})

    emails = sorted(caixa.glob("*.eml"))
    log.info("Caixa %s: %d e-mail(s) | %s", caixa, len(emails), "GRAVANDO no RM" if salvar else "simulação")
    resultados = []
    for caminho in emails:
        try:
            res = processar_email(caminho, salvar=salvar, notificar=notificar)
            destino = "processados"
        except Exception as e:
            log.exception("Falha ao processar %s", caminho.name)
            res = {"email": caminho.name, "situacao": f"erro: {e}", "propostas": []}
            destino = "com_erro"
        resultados.append(res)
        if salvar:
            (caixa / destino).mkdir(exist_ok=True)
            shutil.move(str(caminho), caixa / destino / caminho.name)
        for p in res["propostas"]:
            if p["codcotacao"]:
                estado["cotacoes"].setdefault(p["codcotacao"], {})

    rm = RmClient(coligada)
    quadros = []
    for cod, info in estado["cotacoes"].items():
        if info.get("encerrada"):
            continue
        try:
            sit = _situacao_cotacao(rm, cod)
        except Exception as e:
            log.warning("Não foi possível ler a cotação %s: %s", cod, e)
            continue
        if sit["encerrada"]:
            log.info("Cotação %s já encerrada no RM; deixa de ser acompanhada", cod)
            info["encerrada"] = True
            continue
        motivo = ("prazo de resposta vencido" if sit["prazo_vencido"]
                  else f"{sit['propostas']} proposta(s)" if sit["fechada"] else None)
        if not motivo:
            log.info("Cotação %s aguardando: %d de %d proposta(s), prazo até %s", cod, sit["propostas"],
                     MIN_PROPOSTAS, f"{sit['limite']:%d/%m/%Y}" if sit["limite"] else "?")
            continue
        if info.get("assinatura_quadro") == sit["assinatura"]:
            log.info("Cotação %s: quadro já enviado e nada mudou", cod)
            continue
        quadro = equalizar(cod, coligada)
        texto = texto_quadro(quadro)
        envio = enviar(aviso_quadro(quadro, texto)) if notificar else None
        quadros.append({"codcotacao": cod, "motivo": motivo, "texto": texto, "aviso": envio})
        if salvar:
            info["assinatura_quadro"] = sit["assinatura"]
            info["quadro_enviado_em"] = datetime.now().isoformat(timespec="seconds")

    if salvar:
        arq_estado.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"emails": resultados, "quadros": quadros}


def _resumo(execucao: dict) -> str:
    linhas = ["", "=" * 80, "RESUMO DA EXECUÇÃO", "=" * 80]
    for e in execucao["emails"]:
        linhas.append(f"{e['email']}: {e['situacao']}  ({e.get('assunto', '')[:60]})")
        for p in e["propostas"]:
            if p["erro"]:
                linhas.append(f"    {p['arquivo']}: NÃO REGISTRADO — {p['erro']}")
            else:
                linhas.append(f"    {p['arquivo']}: cotação {p['codcotacao']} / {p['codfornecedor']} {p['fornecedor']} — "
                              f"{p['aprovados']} item(ns) ok, {p['pendentes']} pendente(s)"
                              f"{' — gravado' if p['gravado'] else ''}")
    for q in execucao["quadros"]:
        linhas += ["", f"Cotação {q['codcotacao']} fechada ({q['motivo']}):", q["texto"]]
    if not execucao["quadros"]:
        linhas += ["", "Nenhuma cotação fechou nesta execução."]
    return "\n".join(linhas)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Agente 2: registra propostas da caixa e equaliza cotações fechadas")
    p.add_argument("--caixa", default="caixa", help="pasta com os e-mails (.eml) de proposta")
    p.add_argument("--salvar", action="store_true", help="grava no RM e move os e-mails processados")
    p.add_argument("--notificar", action="store_true", help="envia avisos e quadros ao comprador")
    p.add_argument("--coligada", type=int, default=42)
    a = p.parse_args()
    print(_resumo(rodar(Path(a.caixa), salvar=a.salvar, notificar=a.notificar, coligada=a.coligada)))
