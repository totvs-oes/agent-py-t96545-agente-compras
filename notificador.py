"""Avisa o comprador quando o agente não consegue registrar uma proposta sozinho."""
import logging
import mimetypes
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)

_REMETENTE = os.environ.get("NOTIFICAR_DE", "agente-compras@iben.eng.br")


def _categoria(motivo: str, metodo: str) -> str:
    m = (motivo or "").lower()
    if "erro llm" in m:
        return "Falha técnica ao analisar o item"
    if "mesmo item da cotação" in m:
        return "Dois itens da proposta caíram no mesmo item da cotação"
    if "especificação divergente" in m:
        return "Especificação diferente da pedida"
    if "unidades diferentes sem conversão" in m:
        return "Unidade de medida diferente, sem conversão segura"
    if "quantidade não bate" in m:
        return "Quantidade diferente da pedida"
    if "total da linha" in m or "preço de referência" in m or "sem preço" in m:
        return "Preço suspeito (possível erro de unidade ou de leitura)"
    if metodo == "sem_match":
        return "Nenhum item da cotação corresponde"
    return "Correspondência incerta"


def _br(valor, casas_max: int = 4) -> str:
    """1234.5 -> '1.234,50'; até 4 casas para preço unitário pequeno (0,483), mínimo 2."""
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return str(valor) if valor is not None else "?"
    texto = f"{v:,.{casas_max}f}".rstrip("0")
    inteiro, _, dec = texto.partition(".")
    dec = dec.ljust(2, "0")
    return f"{inteiro.replace(',', '.')},{dec}"


def _detalhe(motivo: str) -> str:
    """O último trecho do motivo é a trava que barrou o item; o primeiro é a análise do LLM."""
    partes = [p.strip() for p in (motivo or "").split("|") if p.strip()]
    return partes[-1] if partes else ""


def montar_aviso(resultado: dict, caminho_pdf: str | Path, *, assunto_email: str = "",
                 remetente_email: str = "") -> EmailMessage | None:
    """Monta o e-mail de aviso. Devolve None quando não há nada para o comprador resolver."""
    caminho = Path(caminho_pdf)
    pendentes = resultado.get("pendentes") or []
    aprovados = resultado.get("aprovados") or []
    erro = resultado.get("erro")
    sem_itens = not erro and not pendentes and not aprovados
    if sem_itens:
        # Ex.: PDF escaneado ou proposta sem tabela de itens: nada foi lido
        erro = "os itens da proposta (o PDF pode ser escaneado ou estar num formato que o agente não leu)"
    if not erro and not pendentes:
        return None

    cot = resultado.get("codcotacao")
    forn = resultado.get("codfornecedor")
    nome_forn = resultado.get("fornecedor_nome") or ""
    comprador = resultado.get("comprador") or {}

    linhas = []
    saudacao = f"Olá, {comprador['nome'].split()[0].title()}," if comprador.get("nome") else "Olá,"
    linhas.append(saudacao)
    linhas.append("")

    if erro:
        if sem_itens:
            titulo = f"Cotação {cot} – {nome_forn or forn}: nenhum item lido na proposta"
            linhas.append(f"O agente de compras recebeu a proposta de {nome_forn} ({forn}) para a cotação {cot}, "
                          f"mas não conseguiu ler {erro}. Nada foi gravado no RM.")
        else:
            titulo = f"Proposta não identificada: {caminho.name}"
            linhas.append("O agente de compras recebeu uma proposta, mas não conseguiu identificar "
                          f"{erro}. Nada foi gravado no RM.")
        linhas.append("")
        if assunto_email or remetente_email:
            linhas.append(f"E-mail: {assunto_email or '(sem assunto)'} — de {remetente_email or '(remetente desconhecido)'}")
        linhas.append(f"Arquivo: {caminho.name} (em anexo)")
        ranking = (resultado.get("identificacao") or {}).get("ranking_fornecedores") or []
        if ranking:
            linhas.append("")
            linhas.append("Fornecedores convidados e evidências encontradas:")
            for r in ranking:
                linhas.append(f"  - {r['codcfo']} {r['nome']}: {r['motivo'] or 'nenhuma evidência'}")
        linhas.append("")
        if sem_itens:
            linhas.append("O que fazer: abra o PDF em anexo e lance os valores na cotação.")
        else:
            linhas.append("O que fazer: confira a quem pertence a proposta e lance os valores na cotação.")
    else:
        if not aprovados:
            situacao = "Nenhum item foi gravado."
        elif resultado.get("chave_rm"):
            situacao = f"{len(aprovados)} item(ns) já foram gravados na cotação."
        else:
            situacao = f"{len(aprovados)} item(ns) estão prontos para gravar (simulação, nada foi gravado)."
        titulo = f"Cotação {cot} – {nome_forn or forn}: {len(pendentes)} item(ns) para revisar"
        linhas.append(f"O agente de compras leu a proposta de {nome_forn} ({forn}) para a cotação {cot}.")
        linhas.append(f"{situacao} Os itens abaixo precisam da sua análise e NÃO foram gravados:")
        linhas.append("")
        for i, p in enumerate(pendentes, 1):
            f, rm = p["item_forn"], p.get("item_rm") or {}
            qtd = _br(f.get("quantidade"), 3) if f.get("quantidade") is not None else "?"
            linhas.append(f"{i}. {f.get('descricao_fornecedor')}  "
                          f"({qtd} {f.get('unidade') or ''} a R$ {_br(f.get('preco_unitario'))})")
            linhas.append(f"   Motivo: {_categoria(p.get('motivo'), p.get('metodo'))}")
            if rm:
                linhas.append(f"   Item da cotação mais próximo: {rm.get('codigo')} {(rm.get('descricao') or '').strip()} "
                              f"({_br(rm.get('quantidade'), 3)} {rm.get('unidade')})")
            detalhe = _detalhe(p.get("motivo"))
            if detalhe:
                linhas.append(f"   Detalhe: {detalhe}")
            linhas.append("")
        linhas.append(f"Proposta original em anexo ({caminho.name}).")

    linhas.append("")
    linhas.append("— Agente de Compras IBEN (mensagem automática)")

    msg = EmailMessage()
    msg["Subject"] = f"[Agente de Compras] {titulo}"
    msg["From"] = _REMETENTE
    msg["To"] = _destinatario(comprador)
    msg.set_content("\n".join(linhas))
    if caminho.exists():
        tipo, _ = mimetypes.guess_type(caminho.name)
        principal, sub = (tipo or "application/octet-stream").split("/", 1)
        msg.add_attachment(caminho.read_bytes(), maintype=principal, subtype=sub, filename=caminho.name)
    return msg


def aviso_quadro(quadro: dict, texto: str) -> EmailMessage:
    """E-mail com o quadro comparativo e a sugestão de vencedor da cotação."""
    comprador = quadro.get("comprador") or {}
    saudacao = f"Olá, {comprador['nome'].split()[0].title()}," if comprador.get("nome") else "Olá,"
    corpo = "\n".join([
        saudacao, "",
        f"Todas as propostas da cotação {quadro['codcotacao']} registradas até agora foram equalizadas "
        "(preço + frete rateado). Segue o quadro comparativo com a sugestão do agente. "
        "Nada foi alterado no RM: a escolha do vencedor é sua.", "",
        texto, "",
        "— Agente de Compras IBEN (mensagem automática)",
    ])
    msg = EmailMessage()
    msg["Subject"] = f"[Agente de Compras] Cotação {quadro['codcotacao']}: quadro comparativo e sugestão"
    msg["From"] = _REMETENTE
    msg["To"] = _destinatario(comprador)
    msg.set_content(corpo)
    return msg


def _destinatario(comprador: dict) -> str:
    # Em teste o aviso nunca vai para o comprador real: NOTIFICAR_PARA tem prioridade
    return os.environ.get("NOTIFICAR_PARA") or (comprador or {}).get("email") or "comprador-nao-cadastrado@invalido"


def enviar(msg: EmailMessage) -> str:
    """Envia por SMTP se SMTP_HOST estiver configurado; senão salva .eml em AVISOS_DIR. Devolve onde foi parar."""
    host = os.environ.get("SMTP_HOST")
    homologacao = "173927" in os.environ.get("RM_URL", "")
    if host and homologacao and not os.environ.get("NOTIFICAR_PARA"):
        log.warning("Homologação sem NOTIFICAR_PARA: o aviso não será enviado a ninguém, só salvo em arquivo")
        host = None
    if host and str(msg["To"]).endswith("@invalido"):
        log.warning("Comprador sem e-mail cadastrado e sem NOTIFICAR_PARA: aviso só salvo em arquivo")
        host = None

    if host:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=60) as smtp:
            smtp.starttls()
            if os.environ.get("SMTP_USER"):
                smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASS", ""))
            smtp.send_message(msg)
        log.info("Aviso enviado para %s", msg["To"])
        return f"enviado para {msg['To']}"

    pasta = Path(os.environ.get("AVISOS_DIR", "avisos"))
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / f"{datetime.now():%Y%m%d-%H%M%S}-{abs(hash(msg['Subject'])) % 10000:04d}.eml"
    arquivo.write_bytes(bytes(msg))
    log.info("Aviso salvo em %s (SMTP não configurado)", arquivo)
    return str(arquivo)
