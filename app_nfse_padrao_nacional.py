
import io
import importlib
import json
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import pandas as pd
import requests
import streamlit as st


# ============================================================
# APP: Analisador de XML NFS-e Padrão Nacional
# Desenvolvido para importar XMLs emitidos e recebidos,
# resumir valores dos serviços, tributos e retenções.
# ============================================================


st.set_page_config(
    page_title="Analisador NFS-e Padrão Nacional",
    page_icon="🧾",
    layout="wide",
)


# -----------------------------
# Funções utilitárias de XML
# -----------------------------
def limpar_texto(valor: Optional[str]) -> str:
    if valor is None:
        return ""
    return str(valor).strip()


def somente_numeros(valor: Any) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def local_name(tag: str) -> str:
    """Remove namespace do XML e padroniza para lower case."""
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    return tag.strip().lower()


def texto_para_numero(valor: Any) -> float:
    """Converte texto de XML em número. Aceita ponto ou vírgula."""
    if valor is None:
        return 0.0

    s = str(valor).strip()
    if not s:
        return 0.0

    # Remove símbolos, mantendo dígitos, ponto, vírgula e sinal
    s = re.sub(r"[^0-9,.\-]", "", s)

    # Caso brasileiro: 1.234,56
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")

    try:
        return float(s)
    except Exception:
        return 0.0


def formatar_cnpj_cpf(doc: Any) -> str:
    doc = somente_numeros(doc)
    if len(doc) == 14:
        return f"{doc[:2]}.{doc[2:5]}.{doc[5:8]}/{doc[8:12]}-{doc[12:]}"
    if len(doc) == 11:
        return f"{doc[:3]}.{doc[3:6]}.{doc[6:9]}-{doc[9:]}"
    return doc


def formatar_cep(cep: Any) -> str:
    cep_limpo = somente_numeros(cep)
    if len(cep_limpo) == 8:
        return f"{cep_limpo[:5]}-{cep_limpo[5:]}"
    return cep_limpo


def carregar_xml_bytes(nome_arquivo: str, conteudo: bytes) -> Optional[ET.Element]:
    try:
        return ET.fromstring(conteudo)
    except Exception:
        try:
            # Alguns XMLs vêm com caracteres antes/depois do XML.
            texto = conteudo.decode("utf-8", errors="ignore")
            ini = texto.find("<")
            fim = texto.rfind(">")
            if ini >= 0 and fim > ini:
                return ET.fromstring(texto[ini:fim + 1].encode("utf-8"))
        except Exception:
            pass
    return None


def mapa_caminhos(root: ET.Element) -> List[Tuple[Tuple[str, ...], ET.Element]]:
    """Cria lista de caminhos do XML para localizar tags por sufixo, ignorando namespace."""
    itens: List[Tuple[Tuple[str, ...], ET.Element]] = []

    def rec(el: ET.Element, caminho: Tuple[str, ...]):
        nome = local_name(el.tag)
        novo = caminho + (nome,)
        itens.append((novo, el))
        for child in list(el):
            rec(child, novo)

    rec(root, tuple())
    return itens


def get_attr_por_tag(caminhos: List[Tuple[Tuple[str, ...], ET.Element]], tag_final: str, attr: str) -> str:
    tag_final = tag_final.lower()
    for path, el in caminhos:
        if path and path[-1] == tag_final:
            v = el.attrib.get(attr)
            if v:
                return limpar_texto(v)
            # procura atributo ignorando case
            for k, val in el.attrib.items():
                if k.lower() == attr.lower():
                    return limpar_texto(val)
    return ""


def get_por_sufixo(caminhos: List[Tuple[Tuple[str, ...], ET.Element]], sufixos: List[Tuple[str, ...]]) -> str:
    """
    Retorna o texto do primeiro elemento cujo caminho termina com um dos sufixos.
    Os sufixos devem ser informados em lower case ou serão normalizados.
    """
    sufixos_norm = [tuple(x.lower() for x in suf) for suf in sufixos]

    for suf in sufixos_norm:
        for path, el in caminhos:
            if len(path) >= len(suf) and path[-len(suf):] == suf:
                txt = limpar_texto(el.text)
                if txt:
                    return txt
    return ""


def get_primeira_tag(caminhos: List[Tuple[Tuple[str, ...], ET.Element]], tags: List[str]) -> str:
    tags_norm = [t.lower() for t in tags]
    for path, el in caminhos:
        if path and path[-1] in tags_norm:
            txt = limpar_texto(el.text)
            if txt:
                return txt
    return ""


def valor(caminhos: List[Tuple[Tuple[str, ...], ET.Element]], sufixos: List[Tuple[str, ...]]) -> float:
    return texto_para_numero(get_por_sufixo(caminhos, sufixos))


def somar_tags(caminhos: List[Tuple[Tuple[str, ...], ET.Element]], tags: List[str]) -> float:
    tags_norm = [t.lower() for t in tags]
    total = 0.0
    for path, el in caminhos:
        if path and path[-1] in tags_norm:
            total += texto_para_numero(el.text)
    return total


def normalizar_data(data: str) -> str:
    data = limpar_texto(data)
    if not data:
        return ""

    # Ex.: 2026-05-01T10:35:00-03:00
    try:
        data_limpa = data.replace("Z", "+00:00")
        return datetime.fromisoformat(data_limpa[:19]).strftime("%d/%m/%Y")
    except Exception:
        pass

    # Ex.: 20260501
    if re.fullmatch(r"\d{8}", data):
        try:
            return datetime.strptime(data, "%Y%m%d").strftime("%d/%m/%Y")
        except Exception:
            pass

    return data[:10]


def data_para_datetime(data: Any) -> Optional[pd.Timestamp]:
    if pd.isna(data):
        return None
    s = str(data).strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return pd.to_datetime(datetime.strptime(s[:10], fmt))
        except Exception:
            continue
    try:
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        return None


def dados_pessoa(caminhos: List[Tuple[Tuple[str, ...], ET.Element]], tipo: str) -> Dict[str, str]:
    """
    tipo: 'prestador' ou 'tomador'
    No padrão nacional:
      Prestador geralmente fica em infNFSe/emit
      Tomador geralmente fica em infNFSe/DPS/infDPS/toma
    """
    if tipo == "prestador":
        doc = get_por_sufixo(caminhos, [
            ("infnfse", "emit", "cnpj"),
            ("infnfse", "emit", "cpf"),
            ("emit", "cnpj"),
            ("emit", "cpf"),

            # Compatibilidade com XMLs ABRASF/municipais
            ("prestadorservico", "identificacaoprestador", "cpfcnpj", "cnpj"),
            ("prestadorservico", "identificacaoprestador", "cpfcnpj", "cpf"),
            ("prestador", "cpfcnpj", "cnpj"),
            ("prestador", "cpfcnpj", "cpf"),
        ])
        nome = get_por_sufixo(caminhos, [
            ("infnfse", "emit", "xnome"),
            ("emit", "xnome"),
            ("prestadorservico", "razaosocial"),
            ("prestador", "razaosocial"),
            ("prestador", "nome"),
        ])
        im = get_por_sufixo(caminhos, [
            ("infnfse", "emit", "im"),
            ("emit", "im"),
            ("prestadorservico", "identificacaoprestador", "inscricaomunicipal"),
        ])
    else:
        doc = get_por_sufixo(caminhos, [
            ("infnfse", "dps", "infdps", "toma", "cnpj"),
            ("infnfse", "dps", "infdps", "toma", "cpf"),
            ("infdps", "toma", "cnpj"),
            ("infdps", "toma", "cpf"),
            ("toma", "cnpj"),
            ("toma", "cpf"),
            ("toma", "caepf"),
            ("toma", "nif"),

            # Compatibilidade com XMLs ABRASF/municipais
            ("tomadorservico", "identificacaotomador", "cpfcnpj", "cnpj"),
            ("tomadorservico", "identificacaotomador", "cpfcnpj", "cpf"),
            ("tomador", "cpfcnpj", "cnpj"),
            ("tomador", "cpfcnpj", "cpf"),
        ])
        nome = get_por_sufixo(caminhos, [
            ("infnfse", "dps", "infdps", "toma", "xnome"),
            ("infdps", "toma", "xnome"),
            ("toma", "xnome"),
            ("tomadorservico", "razaosocial"),
            ("tomador", "razaosocial"),
            ("tomador", "nome"),
        ])
        im = get_por_sufixo(caminhos, [
            ("infdps", "toma", "im"),
            ("toma", "im"),
            ("tomadorservico", "identificacaotomador", "inscricaomunicipal"),
        ])

    return {
        "doc": somente_numeros(doc),
        "doc_formatado": formatar_cnpj_cpf(doc),
        "nome": nome,
        "im": im,
    }


def extrair_nfse(nome_arquivo: str, conteudo: bytes, cnpj_empresa: str = "") -> Dict[str, Any]:
    root = carregar_xml_bytes(nome_arquivo, conteudo)
    if root is None:
        return {
            "Arquivo": nome_arquivo,
            "Status Leitura": "Erro",
            "Erro": "Não foi possível ler o XML",
        }

    caminhos = mapa_caminhos(root)

    prestador = dados_pessoa(caminhos, "prestador")
    tomador = dados_pessoa(caminhos, "tomador")

    id_infnfse = get_attr_por_tag(caminhos, "infnfse", "Id")
    chave_id = somente_numeros(id_infnfse)

    chave = get_por_sufixo(caminhos, [
        ("infnfse", "chnfse"),
        ("chnfse",),
    ])
    chave = somente_numeros(chave) or chave_id

    numero = get_por_sufixo(caminhos, [
        ("infnfse", "nnfse"),
        ("nnfse",),
        ("identificacaonfse", "numero"),
        ("numero",),
    ])

    serie = get_por_sufixo(caminhos, [
        ("serie",),
        ("infdps", "serie"),
    ])

    data_emissao = get_por_sufixo(caminhos, [
        ("infnfse", "dps", "infdps", "dhemi"),
        ("infdps", "dhemi"),
        ("dhemi",),
        ("dataemissao",),
    ])
    data_emissao_fmt = normalizar_data(data_emissao)

    competencia = get_por_sufixo(caminhos, [
        ("infdps", "dcompet"),
        ("dcompet",),
        ("competencia",),
    ])
    competencia_fmt = normalizar_data(competencia)

    descricao_servico = get_por_sufixo(caminhos, [
        ("infdps", "serv", "cserv", "xdescserv"),
        ("serv", "cserv", "xdescserv"),
        ("xdescserv",),
        ("discriminacao",),
    ])

    codigo_servico = get_por_sufixo(caminhos, [
        ("infdps", "serv", "cserv", "ctribnac"),
        ("serv", "cserv", "ctribnac"),
        ("codigolistaservico",),
        ("itemlistaservico",),
    ])

    municipio_prestacao = get_por_sufixo(caminhos, [
        ("infdps", "serv", "locprest", "clocprestacao"),
        ("locprest", "clocprestacao"),
        ("codigomunicipio",),
    ])

    # Valores principais
    v_servico = valor(caminhos, [
        ("infdps", "valores", "vservprest", "vserv"),
        ("valores", "vservprest", "vserv"),
        ("vservprest", "vserv"),
        ("vserv",),
        ("valorservicos",),
    ])

    v_desc_incond = valor(caminhos, [
        ("infdps", "valores", "vdesccondincond", "vdescincond"),
        ("vdesccondincond", "vdescincond"),
        ("vdescincond",),
        ("descontoincondicionado",),
    ])

    v_desc_cond = valor(caminhos, [
        ("infdps", "valores", "vdesccondincond", "vdesccond"),
        ("vdesccondincond", "vdesccond"),
        ("vdesccond",),
        ("descontocondicionado",),
    ])

    descontos = v_desc_incond + v_desc_cond
    valor_contabil = max(v_servico - descontos, 0)

    # ISS
    base_iss = valor(caminhos, [
        ("infnfse", "valores", "vbc"),
        ("valores", "vbc"),
        ("basecalculo",),
    ])

    aliq_iss = valor(caminhos, [
        ("infnfse", "valores", "paliqaplic"),
        ("valores", "paliqaplic"),
        ("aliquota",),
    ])

    v_iss = valor(caminhos, [
        ("infnfse", "valores", "vissqn"),
        ("valores", "vissqn"),
        ("valoriss",),
    ])

    tp_ret_iss = get_por_sufixo(caminhos, [
        ("infdps", "valores", "trib", "tribmun", "tpretissqn"),
        ("tribmun", "tpretissqn"),
        ("tpretissqn",),
        ("issretido",),
    ])

    iss_retido = 0.0
    iss_destacado = v_iss
    if limpar_texto(tp_ret_iss) in ("1", "true", "sim", "s"):
        iss_retido = v_iss
        iss_destacado = 0.0

    # PIS/COFINS
    base_pis_cofins = valor(caminhos, [
        ("tribfed", "piscofins", "vbcpiscofins"),
        ("piscofins", "vbcpiscofins"),
        ("vbcpiscofins",),
    ])

    aliq_pis = valor(caminhos, [
        ("tribfed", "piscofins", "paliqpis"),
        ("piscofins", "paliqpis"),
        ("paliqpis",),
    ])

    v_pis = valor(caminhos, [
        ("tribfed", "piscofins", "vpis"),
        ("piscofins", "vpis"),
        ("vpis",),
        ("valorpis",),
    ])

    aliq_cofins = valor(caminhos, [
        ("tribfed", "piscofins", "paliqcofins"),
        ("piscofins", "paliqcofins"),
        ("paliqcofins",),
    ])

    v_cofins = valor(caminhos, [
        ("tribfed", "piscofins", "vcofins"),
        ("piscofins", "vcofins"),
        ("vcofins",),
        ("valorcofins",),
    ])

    tp_ret_pis_cofins = get_por_sufixo(caminhos, [
        ("tribfed", "piscofins", "tpretpiscofins"),
        ("piscofins", "tpretpiscofins"),
        ("tpretpiscofins",),
    ])

    # Retenções federais padrão nacional
    v_irrf = valor(caminhos, [
        ("tribfed", "vretirrf"),
        ("vretirrf",),
        ("valorir",),
    ])

    v_csll = valor(caminhos, [
        ("tribfed", "vretcsll"),
        ("vretcsll",),
        ("valorcsll",),
    ])

    v_inss_cp = valor(caminhos, [
        ("tribfed", "vretcp"),
        ("vretcp",),
        ("valorinss",),
    ])

    # Alguns XMLs municipais usam tags explícitas de retenção
    v_ret_pis_extra = valor(caminhos, [
        ("vretpis",),
        ("valorpisretido",),
    ])
    v_ret_cofins_extra = valor(caminhos, [
        ("vretcofins",),
        ("valorcofinsretido",),
    ])

    # Se tpRetPisCofins vier explícito como sem retenção, zera PIS/COFINS retidos.
    # Caso contrário, mantém a regra de compatibilidade entre layouts.
    tp_ret_pis_cofins_norm = limpar_texto(tp_ret_pis_cofins).lower()
    sem_ret_pis_cofins = tp_ret_pis_cofins_norm in ("0", "2", "false", "nao", "não", "n")

    if sem_ret_pis_cofins:
        pis_retido = 0.0
        cofins_retido = 0.0
    else:
        # Preferência: se houver vRetPIS/vRetCOFINS explícito, usa; se não, usa vPis/vCofins.
        pis_retido = v_ret_pis_extra if v_ret_pis_extra else v_pis
        cofins_retido = v_ret_cofins_extra if v_ret_cofins_extra else v_cofins

    # Outros tributos/retidos
    v_outros_retidos = somar_tags(caminhos, [
        "vretoutrasretencoes",
        "outrasretencoes",
        "valoroutrasretencoes",
    ])

    # IBS/CBS - Reforma Tributária
    ibs_bc = valor(caminhos, [
        ("ibscbs", "valores", "vbc"),
        ("valores", "vbcibscbs"),
        ("vbcibscbs",),
    ])

    ibs_valor = valor(caminhos, [
        ("ibscbs", "totcibs", "gibs", "vibstot"),
        ("totcibs", "gibs", "vibstot"),
        ("vibstot",),
    ])

    cbs_valor = valor(caminhos, [
        ("ibscbs", "totcibs", "gcbs", "vcbs"),
        ("totcibs", "gcbs", "vcbs"),
        ("vcbs",),
    ])

    cst_ibscbs = get_por_sufixo(caminhos, [
        ("gibscbs", "cst"),
        ("ibscbs", "cst"),
        ("cst",),
    ])

    cclass_trib = get_por_sufixo(caminhos, [
        ("gibscbs", "cclasstrib"),
        ("ibscbs", "cclasstrib"),
        ("cclasstrib",),
    ])

    total_retido = (
        iss_retido
        + pis_retido
        + cofins_retido
        + v_irrf
        + v_csll
        + v_inss_cp
        + v_outros_retidos
    )

    valor_liquido_estimado = max(valor_contabil - total_retido, 0)

    cnpj_empresa = somente_numeros(cnpj_empresa)
    tipo_nota = "Não identificada"
    if cnpj_empresa:
        if cnpj_empresa == prestador["doc"]:
            tipo_nota = "Emitida"
        elif cnpj_empresa == tomador["doc"]:
            tipo_nota = "Recebida"

    # Alertas úteis
    alertas = []
    if not chave:
        alertas.append("Sem chave identificada")
    if not numero:
        alertas.append("Sem número")
    if not prestador["doc"]:
        alertas.append("Sem CNPJ/CPF do prestador")
    if not tomador["doc"]:
        alertas.append("Sem CNPJ/CPF do tomador")
    if v_servico == 0:
        alertas.append("Valor do serviço zerado/não encontrado")
    if total_retido > 0:
        alertas.append("Possui retenção")

    return {
        "Arquivo": nome_arquivo,
        "Status Leitura": "OK",
        "Tipo Nota": tipo_nota,
        "Chave NFS-e": chave,
        "Número": numero,
        "Série": serie,
        "Data Emissão": data_emissao_fmt,
        "Competência": competencia_fmt,
        "Prestador CNPJ/CPF": prestador["doc_formatado"],
        "Prestador Nome": prestador["nome"],
        "Prestador IM": prestador["im"],
        "Tomador CNPJ/CPF": tomador["doc_formatado"],
        "Tomador Nome": tomador["nome"],
        "Tomador IM": tomador["im"],
        "Município Prestação": municipio_prestacao,
        "Código Serviço": codigo_servico,
        "Descrição Serviço": descricao_servico,
        "Valor Serviço": v_servico,
        "Desconto Incond.": v_desc_incond,
        "Desconto Cond.": v_desc_cond,
        "Total Descontos": descontos,
        "Valor Contábil": valor_contabil,
        "Base ISS": base_iss,
        "Alíquota ISS": aliq_iss,
        "ISS Destacado": iss_destacado,
        "ISS Retido": iss_retido,
        "Indicador Ret. ISS": tp_ret_iss,
        "Base PIS/COFINS": base_pis_cofins,
        "Alíquota PIS": aliq_pis,
        "PIS": v_pis,
        "PIS Retido/Informado": pis_retido,
        "Alíquota COFINS": aliq_cofins,
        "COFINS": v_cofins,
        "COFINS Retido/Informado": cofins_retido,
        "Indicador Ret. PIS/COFINS": tp_ret_pis_cofins,
        "IRRF Retido": v_irrf,
        "CSLL Retido": v_csll,
        "INSS/CP Retido": v_inss_cp,
        "Outras Retenções": v_outros_retidos,
        "Total Retido": total_retido,
        "Valor Líquido Estimado": valor_liquido_estimado,
        "Base IBS/CBS": ibs_bc,
        "IBS": ibs_valor,
        "CBS": cbs_valor,
        "CST IBS/CBS": cst_ibscbs,
        "cClassTrib IBS/CBS": cclass_trib,
        "Alertas": " | ".join(alertas),
        "Erro": "",
    }


def ler_uploads(uploaded_files) -> List[Tuple[str, bytes]]:
    arquivos: List[Tuple[str, bytes]] = []

    for up in uploaded_files:
        nome = up.name
        data = up.read()

        if nome.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data), "r") as z:
                    for item in z.infolist():
                        if item.is_dir():
                            continue
                        if item.filename.lower().endswith(".xml"):
                            nome_item = item.filename.replace("\\", "/").split("/")[-1]
                            arquivos.append((nome_item, z.read(item)))
            except Exception as e:
                st.warning(f"Não consegui abrir o ZIP {nome}: {e}")
        elif nome.lower().endswith(".xml"):
            arquivos.append((nome, data))

    return arquivos


def gerar_excel(df: pd.DataFrame, df_resumo_tipo: pd.DataFrame, df_resumo_mes: pd.DataFrame, df_retidos: pd.DataFrame) -> bytes:
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Notas")
        df_resumo_tipo.to_excel(writer, index=False, sheet_name="Resumo Tipo")
        df_resumo_mes.to_excel(writer, index=False, sheet_name="Resumo Mensal")
        df_retidos.to_excel(writer, index=False, sheet_name="Retenções")

        # Ajuste básico de largura
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col_cells in ws.columns:
                max_len = 0
                col_letter = col_cells[0].column_letter
                for cell in col_cells:
                    try:
                        max_len = max(max_len, len(str(cell.value or "")))
                    except Exception:
                        pass
                ws.column_dimensions[col_letter].width = min(max(max_len + 2, 12), 45)

    return output.getvalue()


def gerar_pdf_relatorio(df: pd.DataFrame, titulo: str) -> bytes:
    """Gera PDF simples com resumo e detalhamento das notas."""
    try:
        pagesizes = importlib.import_module("reportlab.lib.pagesizes")
        pdf_canvas = importlib.import_module("reportlab.pdfgen.canvas")
        A4 = pagesizes.A4
        Canvas = pdf_canvas.Canvas
    except Exception:
        return b""

    output = io.BytesIO()
    pdf = Canvas(output, pagesize=A4)
    largura, altura = A4

    def nova_pagina(y_atual: float) -> float:
        if y_atual < 50:
            pdf.showPage()
            return altura - 40
        return y_atual

    def texto_curto(valor: Any, tamanho: int) -> str:
        s = limpar_texto(valor)
        if len(s) <= tamanho:
            return s.ljust(tamanho)
        return (s[: max(tamanho - 1, 1)] + "...")[:tamanho]

    y = altura - 40
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")

    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(30, y, titulo)
    y -= 20

    pdf.setFont("Helvetica", 9)
    pdf.drawString(30, y, f"Gerado em: {agora}")
    y -= 14
    pdf.drawString(30, y, f"Quantidade de notas: {len(df)}")
    y -= 14

    total_servico = float(df["Valor Serviço"].sum()) if "Valor Serviço" in df.columns else 0.0
    total_retido = float(df["Total Retido"].sum()) if "Total Retido" in df.columns else 0.0
    total_liquido = float(df["Valor Líquido Estimado"].sum()) if "Valor Líquido Estimado" in df.columns else 0.0
    pdf.drawString(30, y, f"Total de serviços: {moeda(total_servico)}")
    y -= 14
    pdf.drawString(30, y, f"Total retido: {moeda(total_retido)}")
    y -= 14
    pdf.drawString(30, y, f"Líquido estimado: {moeda(total_liquido)}")
    y -= 20

    if df.empty:
        pdf.drawString(30, y, "Sem registros para detalhamento.")
        pdf.save()
        return output.getvalue()

    colunas = [
        ("Tipo Nota", 10),
        ("Número", 10),
        ("Data Emissão", 10),
        ("Prestador Nome", 22),
        ("Tomador Nome", 22),
        ("V. Serviço", 11),
        ("T. Retido", 11),
    ]

    pdf.setFont("Courier-Bold", 8)
    cabecalho = " ".join(texto_curto(nome, tam) for nome, tam in colunas)
    pdf.drawString(30, y, cabecalho)
    y -= 12

    pdf.setFont("Courier", 8)
    for _, row in df.iterrows():
        y = nova_pagina(y)
        if y == altura - 40:
            pdf.setFont("Courier-Bold", 8)
            pdf.drawString(30, y, cabecalho)
            y -= 12
            pdf.setFont("Courier", 8)

        linha = " ".join([
            texto_curto(row.get("Tipo Nota", ""), 10),
            texto_curto(row.get("Número", ""), 10),
            texto_curto(row.get("Data Emissão", ""), 10),
            texto_curto(row.get("Prestador Nome", ""), 22),
            texto_curto(row.get("Tomador Nome", ""), 22),
            texto_curto(moeda(texto_para_numero(row.get("Valor Serviço", 0))), 11),
            texto_curto(moeda(texto_para_numero(row.get("Total Retido", 0))), 11),
        ])
        pdf.drawString(30, y, linha)
        y -= 10

    y = nova_pagina(y - 6)
    pdf.setFont("Courier", 8)
    pdf.drawString(30, y, "-" * 145)
    y -= 12

    totalizador = (
        f"TOTALIZADOR | Notas: {len(df)} | "
        f"Servicos: {moeda(total_servico)} | "
        f"Retido: {moeda(total_retido)} | "
        f"Liquido estimado: {moeda(total_liquido)}"
    )
    pdf.setFont("Courier-Bold", 8)
    pdf.drawString(30, y, texto_curto(totalizador, 145))

    pdf.save()
    return output.getvalue()


def moeda(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


ARQUIVO_EMPRESAS_VERIFICADAS = Path(__file__).with_name("empresas_verificadas.json")
COOLDOWN_CONSULTA_CNPJ_SEGUNDOS = 60


def carregar_empresas_verificadas() -> List[Dict[str, str]]:
    if not ARQUIVO_EMPRESAS_VERIFICADAS.exists():
        return []

    try:
        conteudo = json.loads(ARQUIVO_EMPRESAS_VERIFICADAS.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(conteudo, list):
        return []

    empresas: List[Dict[str, str]] = []
    for item in conteudo:
        if not isinstance(item, dict):
            continue
        nome = limpar_texto(item.get("nome", ""))
        doc = somente_numeros(item.get("doc", ""))
        if nome and len(doc) in (11, 14):
            empresas.append({"nome": nome, "doc": doc})

    return sorted(empresas, key=lambda x: (x["nome"].lower(), x["doc"]))


def salvar_empresas_verificadas(empresas: List[Dict[str, str]]) -> bool:
    try:
        ARQUIVO_EMPRESAS_VERIFICADAS.write_text(
            json.dumps(empresas, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


@st.cache_data(ttl=21600, show_spinner=False)
def consultar_cnpj_receita(cnpj: str) -> Dict[str, str]:
    """Consulta dados cadastrais de CNPJ para apoiar o cadastro de empresa verificada."""
    cnpj_limpo = somente_numeros(cnpj)
    if len(cnpj_limpo) != 14:
        return {"erro": "CNPJ inválido para consulta."}

    headers = {"Accept": "application/json", "User-Agent": "app-nfse-conferencia/1.0"}

    dados = None
    erro_429 = False

    # Provedor principal
    try:
        resp = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}", timeout=8, headers=headers)
        if resp.status_code == 200:
            dados = resp.json()
        elif resp.status_code == 429:
            erro_429 = True
        elif resp.status_code == 404:
            return {"erro": "CNPJ não encontrado na base consultada."}
    except Exception:
        pass

    # Fallback para reduzir indisponibilidade/rate-limit no provedor principal
    if dados is None:
        try:
            resp_fb = requests.get(f"https://www.receitaws.com.br/v1/cnpj/{cnpj_limpo}", timeout=10, headers=headers)
            if resp_fb.status_code == 200:
                dados_fb = resp_fb.json()
                if str(dados_fb.get("status", "")).upper() == "ERROR":
                    msg = limpar_texto(dados_fb.get("message")) or "Falha na consulta do CNPJ no serviço alternativo."
                    return {"erro": msg}

                dados = {
                    "razao_social": dados_fb.get("nome"),
                    "nome_fantasia": dados_fb.get("fantasia"),
                    "descricao_tipo_de_logradouro": "",
                    "logradouro": dados_fb.get("logradouro"),
                    "numero": dados_fb.get("numero"),
                    "complemento": dados_fb.get("complemento"),
                    "bairro": dados_fb.get("bairro"),
                    "municipio": dados_fb.get("municipio"),
                    "uf": dados_fb.get("uf"),
                    "cep": dados_fb.get("cep"),
                    "descricao_situacao_cadastral": dados_fb.get("situacao"),
                }
            elif resp_fb.status_code == 429:
                erro_429 = True
        except Exception:
            pass

    if dados is None:
        if erro_429:
            return {"erro": "Falha ao consultar CNPJ (HTTP 429). Aguarde alguns instantes e tente novamente."}
        return {"erro": "Não foi possível consultar os dados do CNPJ agora."}

    endereco = " ".join(
        x for x in [
            limpar_texto(dados.get("descricao_tipo_de_logradouro")),
            limpar_texto(dados.get("logradouro")),
            limpar_texto(dados.get("numero")),
        ] if x
    )
    complemento = limpar_texto(dados.get("complemento"))
    if complemento:
        endereco = f"{endereco} - {complemento}" if endereco else complemento

    return {
        "cnpj": cnpj_limpo,
        "razao_social": limpar_texto(dados.get("razao_social")),
        "nome_fantasia": limpar_texto(dados.get("nome_fantasia")),
        "endereco": endereco,
        "bairro": limpar_texto(dados.get("bairro")),
        "municipio": limpar_texto(dados.get("municipio")),
        "uf": limpar_texto(dados.get("uf")),
        "cep": somente_numeros(dados.get("cep")),
        "situacao_cadastral": limpar_texto(dados.get("descricao_situacao_cadastral")),
        "erro": "",
    }


def garantir_colunas_base(df: pd.DataFrame) -> pd.DataFrame:
    """Garante colunas mínimas para o fluxo da UI, mesmo quando todos XMLs falham."""
    colunas_texto = [
        "Arquivo", "Status Leitura", "Tipo Nota", "Chave NFS-e", "Número", "Série",
        "Data Emissão", "Competência", "Prestador CNPJ/CPF", "Prestador Nome", "Prestador IM",
        "Tomador CNPJ/CPF", "Tomador Nome", "Tomador IM", "Município Prestação", "Código Serviço",
        "Descrição Serviço", "Indicador Ret. ISS", "CST IBS/CBS", "cClassTrib IBS/CBS", "Alertas", "Erro",
    ]
    for col in colunas_texto:
        if col not in df.columns:
            df[col] = ""

    colunas_numericas = [
        "Valor Serviço", "Desconto Incond.", "Desconto Cond.", "Total Descontos",
        "Valor Contábil", "Base ISS", "Alíquota ISS", "ISS Destacado", "ISS Retido",
        "Base PIS/COFINS", "Alíquota PIS", "PIS", "PIS Retido/Informado",
        "Alíquota COFINS", "COFINS", "COFINS Retido/Informado",
        "IRRF Retido", "CSLL Retido", "INSS/CP Retido", "Outras Retenções",
        "Total Retido", "Valor Líquido Estimado", "Base IBS/CBS", "IBS", "CBS",
    ]
    for col in colunas_numericas:
        if col not in df.columns:
            df[col] = 0.0

    return df


# -----------------------------
# Layout
# -----------------------------
st.title("🧾 Analisador de XML NFS-e Padrão Nacional")
st.caption("Importe XMLs de notas de serviço emitidas e recebidas para conferir valores, tributos e retenções.")

with st.sidebar:
    st.header("Parâmetros")
    empresas_verificadas = carregar_empresas_verificadas()

    if "nova_empresa_nome" not in st.session_state:
        st.session_state["nova_empresa_nome"] = ""
    if "nova_empresa_doc" not in st.session_state:
        st.session_state["nova_empresa_doc"] = ""
    if "cnpj_consultado_ultimo" not in st.session_state:
        st.session_state["cnpj_consultado_ultimo"] = ""
    if "cnpj_cooldown_ate" not in st.session_state:
        st.session_state["cnpj_cooldown_ate"] = 0.0
    if "cnpj_cooldown_doc" not in st.session_state:
        st.session_state["cnpj_cooldown_doc"] = ""
    if "cnpj_consulta_resultado" not in st.session_state:
        st.session_state["cnpj_consulta_resultado"] = {}
    if "cnpj_consulta_doc" not in st.session_state:
        st.session_state["cnpj_consulta_doc"] = ""

    st.subheader("Empresa verificada")
    docs_empresa = {"Nenhuma empresa cadastrada/selecionada": ""}
    for emp in empresas_verificadas:
        rotulo = f"{emp['nome']} - {formatar_cnpj_cpf(emp['doc'])}"
        docs_empresa[rotulo] = emp["doc"]

    opcao_empresa = st.selectbox(
        "Selecionar empresa cadastrada",
        options=list(docs_empresa.keys()),
        index=0,
    )
    doc_empresa_cadastrada = docs_empresa.get(opcao_empresa, "")

    cnpj_empresa_manual = st.text_input(
        "CNPJ/CPF da empresa para separar emitidas e recebidas",
        placeholder="Ex.: 12.345.678/0001-90",
        help="Se informado, o app classifica como Emitida quando a empresa for prestadora e Recebida quando for tomadora.",
    )

    cnpj_digitado = somente_numeros(cnpj_empresa_manual)
    if len(cnpj_digitado) == 14:
        if st.button("Consultar CNPJ", use_container_width=True):
            agora = time.time()
            em_cooldown = (
                st.session_state.get("cnpj_cooldown_doc") == cnpj_digitado
                and agora < float(st.session_state.get("cnpj_cooldown_ate", 0.0))
            )

            if em_cooldown:
                segundos_restantes = int(float(st.session_state.get("cnpj_cooldown_ate", 0.0)) - agora)
                st.info(f"Consulta temporariamente limitada. Tente novamente em {max(segundos_restantes, 1)}s.")
            else:
                dados_cnpj_receita = consultar_cnpj_receita(cnpj_digitado)
                if dados_cnpj_receita.get("erro"):
                    msg = dados_cnpj_receita["erro"]
                    st.info(msg)
                    if "429" in msg:
                        st.session_state["cnpj_cooldown_doc"] = cnpj_digitado
                        st.session_state["cnpj_cooldown_ate"] = time.time() + COOLDOWN_CONSULTA_CNPJ_SEGUNDOS
                else:
                    st.session_state["cnpj_consulta_resultado"] = dados_cnpj_receita
                    st.session_state["cnpj_consulta_doc"] = cnpj_digitado
                    if st.session_state.get("cnpj_consultado_ultimo") != cnpj_digitado:
                        st.session_state["nova_empresa_nome"] = dados_cnpj_receita.get("razao_social", "")
                        st.session_state["nova_empresa_doc"] = cnpj_digitado
                        st.session_state["cnpj_consultado_ultimo"] = cnpj_digitado
                    st.session_state["cnpj_cooldown_doc"] = ""
                    st.session_state["cnpj_cooldown_ate"] = 0.0

        if st.session_state.get("cnpj_consulta_doc") == cnpj_digitado and st.session_state.get("cnpj_consulta_resultado"):
            dados_cnpj_receita = st.session_state["cnpj_consulta_resultado"]
            st.caption("Dados cadastrais do CNPJ consultado")
            st.write(f"Razão social: {dados_cnpj_receita.get('razao_social') or '-'}")
            st.write(f"Nome fantasia: {dados_cnpj_receita.get('nome_fantasia') or '-'}")
            st.write(f"Endereço: {dados_cnpj_receita.get('endereco') or '-'}")
            st.write(
                "Município/UF: "
                f"{dados_cnpj_receita.get('municipio') or '-'}"
                f"/{dados_cnpj_receita.get('uf') or '-'}"
            )
            st.write(f"Bairro: {dados_cnpj_receita.get('bairro') or '-'}")
            st.write(f"CEP: {formatar_cep(dados_cnpj_receita.get('cep') or '') if dados_cnpj_receita.get('cep') else '-'}")
            st.write(f"Situação cadastral: {dados_cnpj_receita.get('situacao_cadastral') or '-'}")

    elif cnpj_digitado and len(cnpj_digitado) != 11:
        st.caption("Digite um CNPJ com 14 dígitos para consultar razão social e endereço.")

    cnpj_empresa = doc_empresa_cadastrada or cnpj_empresa_manual

    st.caption(
        "Se selecionar uma empresa cadastrada, ela terá prioridade sobre o valor digitado manualmente."
    )

    with st.expander("Cadastrar empresa verificada"):
        nome_nova_empresa = st.text_input("Nome da empresa", key="nova_empresa_nome")
        doc_nova_empresa = st.text_input("CNPJ/CPF", key="nova_empresa_doc", placeholder="Somente números ou formatado")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("Salvar empresa", use_container_width=True):
                nome_limpo = limpar_texto(nome_nova_empresa)
                doc_limpo = somente_numeros(doc_nova_empresa)

                if not nome_limpo:
                    st.warning("Informe o nome da empresa.")
                elif len(doc_limpo) not in (11, 14):
                    st.warning("Informe um CNPJ (14 dígitos) ou CPF (11 dígitos) válido.")
                elif any(emp["doc"] == doc_limpo for emp in empresas_verificadas):
                    st.warning("Essa empresa já está cadastrada.")
                else:
                    empresas_verificadas.append({"nome": nome_limpo, "doc": doc_limpo})
                    empresas_verificadas = sorted(empresas_verificadas, key=lambda x: (x["nome"].lower(), x["doc"]))
                    if salvar_empresas_verificadas(empresas_verificadas):
                        st.success("Empresa cadastrada com sucesso.")
                        st.rerun()
                    else:
                        st.error("Não foi possível salvar o cadastro da empresa.")

        with c2:
            if st.button("Remover selecionada", use_container_width=True):
                if not doc_empresa_cadastrada:
                    st.warning("Selecione uma empresa cadastrada para remover.")
                else:
                    empresas_filtradas = [emp for emp in empresas_verificadas if emp["doc"] != doc_empresa_cadastrada]
                    if salvar_empresas_verificadas(empresas_filtradas):
                        st.success("Empresa removida com sucesso.")
                        st.rerun()
                    else:
                        st.error("Não foi possível remover a empresa selecionada.")

    st.markdown("---")
    st.write("**O app lê:**")
    st.write("- XML avulso")
    st.write("- ZIP com vários XMLs")
    st.write("- NFS-e Padrão Nacional e XMLs municipais parecidos")

uploaded_files = st.file_uploader(
    "Importe os XMLs ou um ZIP com XMLs",
    type=["xml", "zip"],
    accept_multiple_files=True,
)

if not uploaded_files:
    st.info("Envie os XMLs de NFS-e para iniciar a análise.")
    st.stop()

arquivos = ler_uploads(uploaded_files)

if not arquivos:
    st.error("Nenhum XML válido foi encontrado nos arquivos enviados.")
    st.stop()

with st.spinner("Lendo XMLs e montando resumo..."):
    registros = [extrair_nfse(nome, conteudo, cnpj_empresa) for nome, conteudo in arquivos]
    df = pd.DataFrame(registros)

df = garantir_colunas_base(df)

# Converte colunas numéricas
colunas_numericas = [
    "Valor Serviço", "Desconto Incond.", "Desconto Cond.", "Total Descontos",
    "Valor Contábil", "Base ISS", "Alíquota ISS", "ISS Destacado", "ISS Retido",
    "Base PIS/COFINS", "Alíquota PIS", "PIS", "PIS Retido/Informado",
    "Alíquota COFINS", "COFINS", "COFINS Retido/Informado",
    "IRRF Retido", "CSLL Retido", "INSS/CP Retido", "Outras Retenções",
    "Total Retido", "Valor Líquido Estimado", "Base IBS/CBS", "IBS", "CBS"
]
for col in colunas_numericas:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

df["Data Base"] = df["Data Emissão"].apply(data_para_datetime)
df["Mês"] = df["Data Base"].dt.to_period("M").astype(str).replace("NaT", "")

# Filtros
st.subheader("Filtros")

f1, f2, f3 = st.columns([1, 1, 2])
with f1:
    tipos = sorted([x for x in df["Tipo Nota"].dropna().unique()])
    tipo_sel = st.multiselect("Tipo de nota", options=tipos, default=tipos)
with f2:
    somente_retidas = st.checkbox("Mostrar somente notas com retenção")
with f3:
    busca = st.text_input("Buscar por prestador, tomador, número, chave ou descrição")

df_filtrado = df.copy()

if tipo_sel:
    df_filtrado = df_filtrado[df_filtrado["Tipo Nota"].isin(tipo_sel)]

if somente_retidas:
    df_filtrado = df_filtrado[df_filtrado["Total Retido"] > 0]

if busca:
    b = busca.lower().strip()
    cols_busca = [
        "Prestador Nome", "Tomador Nome", "Número", "Chave NFS-e",
        "Descrição Serviço", "Arquivo", "Prestador CNPJ/CPF", "Tomador CNPJ/CPF"
    ]
    mask = False
    for col in cols_busca:
        if col in df_filtrado.columns:
            mask = mask | df_filtrado[col].astype(str).str.lower().str.contains(b, na=False)
    df_filtrado = df_filtrado[mask]

# Indicadores
st.subheader("Painel geral")

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("XMLs lidos", len(df_filtrado))
m2.metric("Valor dos serviços", moeda(df_filtrado["Valor Serviço"].sum()))
m3.metric("ISS destacado", moeda(df_filtrado["ISS Destacado"].sum()))
m4.metric("Total retido", moeda(df_filtrado["Total Retido"].sum()))
m5.metric("Líquido estimado", moeda(df_filtrado["Valor Líquido Estimado"].sum()))

m6, m7, m8, m9 = st.columns(4)
m6.metric("Emitidas", int((df_filtrado["Tipo Nota"] == "Emitida").sum()))
m7.metric("Recebidas", int((df_filtrado["Tipo Nota"] == "Recebida").sum()))
m8.metric("Com retenção", int((df_filtrado["Total Retido"] > 0).sum()))
m9.metric("Com erro de leitura", int((df_filtrado["Status Leitura"] != "OK").sum()))

# Resumos
st.subheader("Resumos")

df_resumo_tipo = (
    df_filtrado.groupby("Tipo Nota", dropna=False)
    .agg({
        "Arquivo": "count",
        "Valor Serviço": "sum",
        "Valor Contábil": "sum",
        "ISS Destacado": "sum",
        "ISS Retido": "sum",
        "PIS Retido/Informado": "sum",
        "COFINS Retido/Informado": "sum",
        "IRRF Retido": "sum",
        "CSLL Retido": "sum",
        "INSS/CP Retido": "sum",
        "Total Retido": "sum",
        "IBS": "sum",
        "CBS": "sum",
        "Valor Líquido Estimado": "sum",
    })
    .reset_index()
    .rename(columns={"Arquivo": "Quantidade"})
)

df_resumo_mes = (
    df_filtrado.groupby(["Mês", "Tipo Nota"], dropna=False)
    .agg({
        "Arquivo": "count",
        "Valor Serviço": "sum",
        "ISS Retido": "sum",
        "Total Retido": "sum",
        "IBS": "sum",
        "CBS": "sum",
    })
    .reset_index()
    .rename(columns={"Arquivo": "Quantidade"})
)
df_resumo_mes = df_resumo_mes[df_resumo_mes["Mês"] != ""]

aba1, aba2, aba3, aba4 = st.tabs(["📌 Resumo por tipo", "📅 Resumo mensal", "💰 Retenções", "🧾 Detalhamento"])

with aba1:
    st.dataframe(df_resumo_tipo, use_container_width=True)
    if not df_resumo_tipo.empty:
        chart_data = df_resumo_tipo.set_index("Tipo Nota")[["Valor Serviço", "Total Retido", "Valor Líquido Estimado"]]
        st.bar_chart(chart_data)

with aba2:
    st.dataframe(df_resumo_mes, use_container_width=True)
    if not df_resumo_mes.empty:
        chart_mes = df_resumo_mes.pivot_table(
            index="Mês",
            columns="Tipo Nota",
            values="Valor Serviço",
            aggfunc="sum",
            fill_value=0,
        )
        st.line_chart(chart_mes)

with aba3:
    df_retidos = df_filtrado[df_filtrado["Total Retido"] > 0].copy()
    colunas_retencao = [
        "Arquivo", "Tipo Nota", "Número", "Data Emissão", "Prestador Nome", "Tomador Nome",
        "Valor Serviço", "ISS Retido", "PIS Retido/Informado", "COFINS Retido/Informado",
        "IRRF Retido", "CSLL Retido", "INSS/CP Retido", "Outras Retenções",
        "Total Retido", "Valor Líquido Estimado", "Alertas"
    ]
    colunas_retencao = [c for c in colunas_retencao if c in df_retidos.columns]
    st.dataframe(df_retidos[colunas_retencao], use_container_width=True)

    resumo_tributos = pd.DataFrame({
        "Tributo": [
            "ISS Retido", "PIS Retido/Informado", "COFINS Retido/Informado",
            "IRRF Retido", "CSLL Retido", "INSS/CP Retido", "Outras Retenções"
        ],
        "Valor": [
            df_filtrado["ISS Retido"].sum(),
            df_filtrado["PIS Retido/Informado"].sum(),
            df_filtrado["COFINS Retido/Informado"].sum(),
            df_filtrado["IRRF Retido"].sum(),
            df_filtrado["CSLL Retido"].sum(),
            df_filtrado["INSS/CP Retido"].sum(),
            df_filtrado["Outras Retenções"].sum(),
        ]
    })
    resumo_tributos = resumo_tributos[resumo_tributos["Valor"] > 0]
    if not resumo_tributos.empty:
        st.bar_chart(resumo_tributos.set_index("Tributo"))

with aba4:
    colunas_exibir = [
        "Status Leitura", "Tipo Nota", "Arquivo", "Chave NFS-e", "Número", "Série",
        "Data Emissão", "Competência",
        "Prestador CNPJ/CPF", "Prestador Nome", "Tomador CNPJ/CPF", "Tomador Nome",
        "Município Prestação", "Código Serviço", "Descrição Serviço",
        "Valor Serviço", "Total Descontos", "Valor Contábil",
        "Base ISS", "Alíquota ISS", "ISS Destacado", "ISS Retido",
        "Base PIS/COFINS", "PIS", "COFINS", "IRRF Retido", "CSLL Retido", "INSS/CP Retido",
        "Total Retido", "Valor Líquido Estimado",
        "Base IBS/CBS", "IBS", "CBS", "CST IBS/CBS", "cClassTrib IBS/CBS",
        "Alertas", "Erro"
    ]
    colunas_exibir = [c for c in colunas_exibir if c in df_filtrado.columns]
    st.dataframe(df_filtrado[colunas_exibir], use_container_width=True, height=500)

# Alertas
st.subheader("Alertas de conferência")
df_alertas = df_filtrado[df_filtrado["Alertas"].astype(str).str.strip() != ""]
if df_alertas.empty:
    st.success("Nenhum alerta identificado nos XMLs filtrados.")
else:
    colunas_alerta = ["Arquivo", "Tipo Nota", "Número", "Prestador Nome", "Tomador Nome", "Valor Serviço", "Total Retido", "Alertas"]
    colunas_alerta = [c for c in colunas_alerta if c in df_alertas.columns]
    st.dataframe(
        df_alertas[colunas_alerta],
        use_container_width=True
    )

# Exportação
st.subheader("Exportar")

df_somente_retencao = df_filtrado[df_filtrado["Total Retido"] > 0].copy()

excel_bytes = gerar_excel(
    df_filtrado.drop(columns=["Data Base"], errors="ignore"),
    df_resumo_tipo,
    df_resumo_mes,
    df_somente_retencao.drop(columns=["Data Base"], errors="ignore"),
)
pdf_bytes = gerar_pdf_relatorio(
    df_filtrado.drop(columns=["Data Base"], errors="ignore"),
    "Relatório NFS-e - Geral",
)

c1, c2, c3 = st.columns(3)
with c1:
    st.download_button(
        label="⬇️ Baixar relatório em Excel",
        data=excel_bytes,
        file_name="relatorio_nfse_padrao_nacional.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

with c2:
    csv_bytes = df_filtrado.drop(columns=["Data Base"], errors="ignore").to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig")
    st.download_button(
        label="⬇️ Baixar detalhamento em CSV",
        data=csv_bytes,
        file_name="detalhamento_nfse_padrao_nacional.csv",
        mime="text/csv",
    )

with c3:
    st.download_button(
        label="⬇️ Baixar relatório em PDF",
        data=pdf_bytes,
        file_name="relatorio_nfse_padrao_nacional.pdf",
        mime="application/pdf",
        disabled=not bool(pdf_bytes),
    )
if not pdf_bytes:
    st.caption("Exportação em PDF indisponível neste ambiente. Instale a dependência reportlab.")

st.markdown("### Relatório somente com retenção")
if df_somente_retencao.empty:
    st.info("Não há notas com retenção nos filtros atuais para exportar.")
else:
    df_retencao_resumo_tipo = (
        df_somente_retencao.groupby("Tipo Nota", dropna=False)
        .agg({
            "Arquivo": "count",
            "Valor Serviço": "sum",
            "Valor Contábil": "sum",
            "ISS Destacado": "sum",
            "ISS Retido": "sum",
            "PIS Retido/Informado": "sum",
            "COFINS Retido/Informado": "sum",
            "IRRF Retido": "sum",
            "CSLL Retido": "sum",
            "INSS/CP Retido": "sum",
            "Total Retido": "sum",
            "IBS": "sum",
            "CBS": "sum",
            "Valor Líquido Estimado": "sum",
        })
        .reset_index()
        .rename(columns={"Arquivo": "Quantidade"})
    )

    df_retencao_resumo_mes = (
        df_somente_retencao.groupby(["Mês", "Tipo Nota"], dropna=False)
        .agg({
            "Arquivo": "count",
            "Valor Serviço": "sum",
            "ISS Retido": "sum",
            "Total Retido": "sum",
            "IBS": "sum",
            "CBS": "sum",
        })
        .reset_index()
        .rename(columns={"Arquivo": "Quantidade"})
    )
    df_retencao_resumo_mes = df_retencao_resumo_mes[df_retencao_resumo_mes["Mês"] != ""]

    excel_retencao_bytes = gerar_excel(
        df_somente_retencao.drop(columns=["Data Base"], errors="ignore"),
        df_retencao_resumo_tipo,
        df_retencao_resumo_mes,
        df_somente_retencao.drop(columns=["Data Base"], errors="ignore"),
    )
    pdf_retencao_bytes = gerar_pdf_relatorio(
        df_somente_retencao.drop(columns=["Data Base"], errors="ignore"),
        "Relatório NFS-e - Somente Retenções",
    )

    c4, c5, c6 = st.columns(3)
    with c4:
        st.download_button(
            label="⬇️ Baixar retenções em Excel",
            data=excel_retencao_bytes,
            file_name="relatorio_nfse_somente_retencao.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with c5:
        csv_retencao_bytes = (
            df_somente_retencao
            .drop(columns=["Data Base"], errors="ignore")
            .to_csv(index=False, sep=";", decimal=",")
            .encode("utf-8-sig")
        )
        st.download_button(
            label="⬇️ Baixar retenções em CSV",
            data=csv_retencao_bytes,
            file_name="detalhamento_nfse_somente_retencao.csv",
            mime="text/csv",
        )

    with c6:
        st.download_button(
            label="⬇️ Baixar retenções em PDF",
            data=pdf_retencao_bytes,
            file_name="relatorio_nfse_somente_retencao.pdf",
            mime="application/pdf",
            disabled=not bool(pdf_retencao_bytes),
        )

st.caption(
    "Observação: o valor líquido é estimado pelo app com base no valor contábil menos retenções encontradas no XML. "
    "Confira sempre com o relatório fiscal/contábil antes de transmitir obrigações."
)
