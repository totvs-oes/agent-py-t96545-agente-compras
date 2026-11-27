import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from xml.sax.saxutils import escape
import requests
from dotenv import load_dotenv

load_dotenv()

_DECIMAL_RE = re.compile(r"^-?\d+\.\d+$")
_CAMPOS_TEXTO = ("COD", "NUM", "SERIE", "NOME", "DSC", "TXT", "OBS", "CGC", "TEL", "EMAIL")

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Variavel de ambiente '{name}' não configurada no .env")
    return value


class RmClient:
    """Encapsula as chamadas SOAP ao TOTVS RM (wsDataServer / CmpCotacaoData)"""

    def __init__(self, coligada: int = 42):
        url = _require_env("RM_URL")
        if "173926" in url:
            raise RuntimeError("RM_URL aponta para PRODUÇÃO (173926). Use o host de homologação 173927")

        self._url = url.rstrip("/")
        self._endpoint = f"{self._url}/wsDataServer/IwsDataServer"
        self._user = _require_env("RM_USER")
        self._password = _require_env("RM_PASS")
        self.coligada = coligada

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    @staticmethod
    def _set(row: ET.Element, campo: str, valor, mudancas: list, chave: str):
        el = row.find(campo)
        if el is None:
            el = ET.SubElement(row, campo)
        antigo = el.text
        novo = str(valor)
        if antigo != novo:
            el.text = novo
            mudancas.append(f"{chave:<36} {campo:<18} {antigo!s:>12} -> {novo}")

    @staticmethod
    def _decimais_para_rm(doc: ET.Element) -> ET.Element:
        copia = ET.fromstring(ET.tostring(doc))
        for el in copia.iter():
            if el.tag.startswith(_CAMPOS_TEXTO):
                continue
            if el.text and _DECIMAL_RE.match(el.text.strip()):
                el.text = el.text.strip().replace(".", ",")
        return copia

    def _contexto(self) -> str:
        return f"CODCOLIGADA={self.coligada};CODFILIAL=1;CODSISTEMA=T;CODUSUARIO={self._user}"


    def _soap(self, action: str, dataserver: str = "", chave: str = "", xml: str = "", filtro: str = "") -> str:
        if action == "ReadRecord":
            body = (
                f"<tot:ReadRecord><tot:DataServerName>{dataserver}</tot:DataServerName>"
                f"<tot:PrimaryKey>{chave}</tot:PrimaryKey>"
                f"<tot:Contexto>{self._contexto()}</tot:Contexto></tot:ReadRecord>"
            )
        elif action == "ReadView":
            body = (
                f"<tot:ReadView><tot:DataServerName>{dataserver}</tot:DataServerName>"
                f"<tot:Filtro>{escape(filtro)}</tot:Filtro>"
                f"<tot:Contexto>{self._contexto()}</tot:Contexto></tot:ReadView>"
            )
        else:  # SaveRecord
            body = (
                f"<tot:SaveRecord><tot:DataServerName>{dataserver}</tot:DataServerName>"
                f"<tot:XML><![CDATA[{xml}]]></tot:XML>"
                f"<tot:Contexto>{self._contexto()}</tot:Contexto></tot:SaveRecord>"
            )

        envelope = (
            '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:tot="http://www.totvs.com/"><soapenv:Header/><soapenv:Body>'
            f"{body}</soapenv:Body></soapenv:Envelope>"
        )

        r = requests.post(
            self._endpoint,
            data=envelope.encode("utf-8"),
            auth=(self._user, self._password),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"http://www.totvs.com/IwsDataServer/{action}",
            },
            timeout=120,
        )
        root = ET.fromstring(r.content)
        fault = root.find(".//{http://schemas.xmlsoap.org/soap/envelope/}Fault")

        if fault is not None:
            raise RuntimeError(fault.findtext("faultstring"))

        result = root.find(f".//{{http://www.totvs.com/}}{action}Result")

        return result.text if result is not None else ""


    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def ler_cotacao(self, codcotacao: str) -> ET.Element:
        """Lê a cotação do RM e retorna o XML como ElementTree (sem os blocos de imagem)"""
        xml = self._soap(
            "ReadRecord",
            "CmpCotacaoData",
            f"{escape(codcotacao)};{self.coligada}",
        )
        doc = ET.fromstring(xml)
        for img in doc.findall("_x0024_IMAGES"):
            doc.remove(img)
        return doc

    def aplicar_orcamento(
            self,
            doc: ET.Element,
            fornecedor: str,
            preco: float | None = None,
            prazo: int | None = None,
            validade: int | None = None,
            contato: str | None = None,
            num_orcamento: str | None = None,
            observacao: str | None = None,
            codcpg: str | None = None,
            frete_cif_fob: int | None = None,
            valor_frete: float | None = None,
    ) -> list[str]:
        """
        Altera somente as linhas do fornecedor informado no XML da cotação.
        Retorna a lista de mudanças (campo: antes -> depois sem gravar nada)
        """
        mud = []
        achou = False

        data_cot = datetime.fromisoformat(doc.find("TCCOTACAO").findtext("DATCOTACAO"))
        data_entrega = (data_cot + timedelta(days=prazo)).isoformat() if prazo and prazo > 0 else None
        data_validade = (data_cot + timedelta(days=validade)).isoformat() if validade and validade > 0 else None

        for row in doc.findall("TCORCAMENTO"):
            if row.findtext("CODCFO") != fornecedor:
                continue
            achou = True

            k = f"TCORCAMENTO[{fornecedor}]"

            if prazo is not None:
                self._set(row, "VALPRAZOENTREGA", prazo, mud, k)
                if data_entrega:
                    self._set(row, "DATAENTREGAORC", data_entrega, mud, k)
                
            if validade is not None:
                self._set(row, "VALPRAZOVALIDADE", validade, mud, k)
                if data_validade:
                    self._set(row, "DATAVALIDADEORC", data_validade, mud, k)

            if contato:
                self._set(row, "DSCCONTATO", contato, mud, k)

            if num_orcamento:
                self._set(row, "CODMOVIMENTOCFO", num_orcamento, mud, k)

            if observacao:
                self._set(row, "TXTOBSERVACAO", observacao, mud, k)

            if codcpg:
                self._set(row, "CODCPG", codcpg, mud, k)

            if frete_cif_fob is not None:
                self._set(row, "FRETECIFOUFOB", frete_cif_fob, mud, k)

            if valor_frete is not None:
                self._set(row, "VALORFRETE", f"{valor_frete:.4f}", mud, k)

        for tabela in ("TCITMORCAMENTO", "TCITMORCAMENTOAGRUPADO"):
            for row in doc.findall(tabela):
                if row.findtext("CODCFO") != fornecedor:
                    continue
                k = f"{tabela}[{fornecedor}|{row.findtext("IDPRD")}]"

                if preco is not None:
                    self._set(row, "VALCOTACAO", f"{preco:.4f}", mud, k)
                    self._set(row, "VALNEGOCIADO", f"{preco:.4f}", mud, k)
                    self._set(row, "STSITEM", "0", mud, k)

                if prazo is not None:
                    self._set(row, "PRAZOENTREGA", prazo, mud, k)

                    if data_entrega:
                        self._set(row, "DATAENTREGA", data_entrega, mud, k)

        if not achou:
            raise ValueError(f"Fornecedor {fornecedor} não está nesta cotação")
        return mud

    def salvar_cotacao(self, doc: ET.Element) -> str:
        """Grava o XML modificado no RM. Retorna a chave confirmada pelo RM"""
        xml = ET.tostring(self._decimais_para_rm(doc), encoding="unicode")

        resposta = self._soap(
            "SaveRecord",
            "CmpCotacaoData",
            xml=xml,
        )
        codcotacao = doc.find("TCCOTACAO").findtext("CODCOTACAO", "")

        if codcotacao not in (resposta or "").split("\n")[0] or "   at " in (resposta or ""):
            raise RuntimeError(f"RM recusou a gravação:\n{(resposta or '').split('=====')[0].strip()}")

        return resposta


    def consulta_sql(self, codigo: str, parametros: dict | None = None, coligada: int = 0, sistema: str = "T") -> list[dict]:
        """Executa uma consulta SQL cadastrada no RM (Gestão → Consultas SQL) pela API REST."""
        params = None
        if parametros:
            # Formato aceito pelo RM: "NOME=valor;NOME=valor", sem aspas
            params = {"parameters": ";".join(f"{k}={v}" for k, v in parametros.items())}
        r = requests.get(
            f"{self._url}/api/framework/v1/consultaSQLServer/RealizaConsulta/{codigo}/{coligada}/{sistema}",
            params=params,
            auth=(self._user, self._password),
            timeout=120,
        )
        if not r.ok:
            raise RuntimeError(f"Consulta SQL {codigo} falhou [{r.status_code}]: {r.text[:300]}")
        return r.json()

    def traducoes_fornecedor(self, codfornecedor: str) -> dict[str, str]:
        """
        De-para código do produto no fornecedor -> CODIGOPRD do RM (tabela TPRDCFOCOLAB,
        alimentada pela importação de XML de NF-e). Códigos ligados a mais de um produto são
        descartados, porque não dá para saber qual é o certo.
        """
        linhas = self.consulta_sql("AG2_TRAD_PRODUTO", {"CODCOLIGADA": self.coligada, "CODCFO": codfornecedor})
        por_codigo: dict[str, set[str]] = {}
        for linha in linhas:
            cod_forn = str(linha.get("CODPRDFORNECEDOR") or "").strip().upper()
            if cod_forn and linha.get("CODIGOPRD"):
                por_codigo.setdefault(cod_forn, set()).add(linha["CODIGOPRD"])
        return {cod: next(iter(prds)) for cod, prds in por_codigo.items() if len(prds) == 1}

    def condicoes_pagamento(self) -> list[dict]:
        """Condições de pagamento ativas da coligada (TCPG, via MovCPGData)."""
        xml = self._soap("ReadView", "MovCPGData", filtro=f"TCPG.CODCOLIGADA={self.coligada}")
        vistos, saida = set(), []
        for row in ET.fromstring(xml or "<x/>"):
            cod = row.findtext("CODCPG")
            if row.tag.upper() != "TCPG" or cod in vistos or row.findtext("INATIVO") == "1":
                continue
            vistos.add(cod)
            saida.append({"codcpg": cod, "nome": (row.findtext("NOME") or "").strip()})
        return saida

    def comprador(self, codcomprador: str) -> dict:
        """Nome e e-mail do comprador (TVEN, via MovVenData). E-mail pode vir vazio no cadastro."""
        cod = re.sub(r"[^\w.-]", "", codcomprador or "")
        xml = self._soap("ReadView", "MovVenData",
                         filtro=f"TVEN.CODCOLIGADA={self.coligada} AND TVEN.CODVEN='{cod}'")
        for row in ET.fromstring(xml or "<x/>"):
            if row.tag.upper() == "TVEN":
                return {"codigo": cod, "nome": row.findtext("NOME"), "email": row.findtext("EMAIL")}
        return {"codigo": cod, "nome": None, "email": None}

    def fornecedores_cotacao(self, doc: ET.Element) -> list[dict]:
        """Fornecedores convidados (um TCORCAMENTO por fornecedor) com os dados do cadastro."""
        return [
            {
                "codcfo": o.findtext("CODCFO"),
                "nome": o.findtext("NOMEFANTASIA") or o.findtext("NOME"),
                "cnpj": o.findtext("CGCCFO"),
                "email": o.findtext("EMAIL"),
                "telefone": o.findtext("TELEFONE") or o.findtext("TELCONTATO"),
            }
            for o in doc.findall("TCORCAMENTO")
        ]

    def itens_cotacao(self, doc: ET.Element) -> list[dict]:
        """Retorna os itens da cotação como lista de dicts (código, descrição, quantidade, unidade, preço ref.)"""
        return [
            {
                "idprd": it.findtext("IDPRD"),
                "codigo": it.findtext("CODIGOPRD"),
                "descricao": it.findtext("NOMEFANTASIA"),
                "quantidade": it.findtext("QUANTIDADE"),
                "unidade": it.findtext("CODUND"),
                "preco_ref": it.findtext("PRECOUNITARIO"),
            }
            for it in doc.findall("TCCOTACAOITMMOV")
        ]