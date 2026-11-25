"""Mensagem padrão, montagem do e-mail de alerta e envio."""
import html
import os
import re
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from zoneinfo import ZoneInfo

from . import db

# Nome, cor de fundo e cor do texto de cada gravidade (cores do tema escuro do Zabbix)
SEVERIDADES = {
    0: ("Not classified", "#97AAB3", "#10181d"),
    1: ("Information", "#7499FF", "#10181d"),
    2: ("Warning", "#FFC859", "#10181d"),
    3: ("Average", "#FFA059", "#10181d"),
    4: ("High", "#E97659", "#10181d"),
    5: ("Disaster", "#E45959", "#ffffff"),
}

# Mensagem padrão: texto antes da tabela do alerta, texto depois, e assunto.
MODELO_PADRAO = {
    "assunto": "{host} - {problema}",
    "texto_antes": "{saudacao},\n\nEstamos com o seguinte alerta em nosso monitoramento:",
    "texto_depois": "Poderiam verificar, por gentileza.\n\nAtenciosamente,\nNoc Computécnica",
}
VARIAVEIS = [
    ("{saudacao}", "Bom dia, Boa tarde ou Boa noite, conforme a hora"),
    ("{host}", "nome do host"),
    ("{problema}", "nome do problema"),
    ("{gravidade}", "Warning, Average, High ou Disaster"),
    ("{hora}", "hora em que o alerta começou"),
]


def modo():
    return "smtp" if os.getenv("EMAIL_MODE", "dry-run").lower() == "smtp" else "dry-run"


def _fuso():
    try:
        return ZoneInfo(os.getenv("TZ_NOC", "America/Sao_Paulo"))
    except Exception:
        return None


def saudacao():
    hora = datetime.now(_fuso()).hour
    return "Bom dia" if hora < 12 else "Boa tarde" if hora < 18 else "Boa noite"


def aplicar(texto, host, problemas):
    """Troca os campos automáticos ({host}, {problema}...) pelos valores do alerta."""
    gravidade = SEVERIDADES.get(max(p["gravidade"] for p in problemas), SEVERIDADES[0])[0]
    inicio = datetime.fromtimestamp(min(p["clock"] for p in problemas), _fuso()).strftime("%H:%M")
    trocas = {"{saudacao}": saudacao(), "{host}": host,
              "{problema}": " | ".join(p["nome"] for p in problemas),
              "{gravidade}": gravidade, "{hora}": inicio}
    for campo, valor in trocas.items():
        texto = texto.replace(campo, valor)
    return texto


# ---------- mensagem padrão (guardada no banco) ----------
def modelo():
    m = dict(MODELO_PADRAO)
    for chave in m:
        r = db.one("SELECT valor FROM config WHERE chave=?", ("modelo_" + chave,))
        if r:
            m[chave] = r["valor"]
    return m


def salvar_modelo(m):
    for chave in MODELO_PADRAO:
        db.x("INSERT INTO config (chave, valor) VALUES (?, ?) "
             "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor", ("modelo_" + chave, m[chave]))


def restaurar_modelo():
    db.x("DELETE FROM config WHERE chave LIKE 'modelo_%'")


def rascunho_padrao(host, problemas):
    """Assunto e textos já preenchidos com os dados do alerta (é o que a pessoa pode editar)."""
    m = modelo()
    return tuple(aplicar(m[k], host, problemas) for k in ("assunto", "texto_antes", "texto_depois"))


def _paragrafos(texto):
    blocos = [b.strip() for b in re.split(r"\n\s*\n", texto.strip()) if b.strip()]
    return "".join("<p>" + html.escape(b).replace("\n", "<br>") + "</p>" for b in blocos)


def montar(host, problemas, assunto, antes, depois):
    """Devolve (assunto, corpo_html, corpo_texto): texto antes, tabela do alerta e texto depois."""
    borda = "border-bottom:1px solid #3a4046;padding:6px 10px"
    linhas, texto_linhas = "", []
    for p in problemas:
        nome_sev, fundo, cor = SEVERIDADES.get(p["gravidade"], SEVERIDADES[0])
        linhas += (
            "<tr>"
            f'<td style="background:{fundo};color:{cor};{borda};white-space:nowrap">{nome_sev}</td>'
            f'<td style="color:#E45959;font-weight:600;{borda}">PROBLEM</td>'
            f'<td style="color:#7fb8e8;{borda}">{html.escape(host)}</td>'
            f'<td style="color:#dfe5e8;{borda}">{html.escape(p["nome"])}</td>'
            "</tr>"
        )
        texto_linhas.append(f'[{nome_sev}] PROBLEM  {host}  {p["nome"]}')
    tabela = ('<table cellspacing="0" cellpadding="0" style="background:#1f2326;border-collapse:collapse;'
              'border:1px solid #3a4046;font-family:Segoe UI,Arial,sans-serif;font-size:13px">'
              f"{linhas}</table>")
    corpo_html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">'
                  f"{_paragrafos(antes)}{tabela}{_paragrafos(depois)}</div>")
    corpo_texto = (antes.strip() + "\n\n" + "\n".join(texto_linhas) + "\n\n" + depois.strip() + "\n")
    return " ".join(assunto.split())[:200], corpo_html, corpo_texto


def previa(m):
    """Prévia da mensagem padrão com um alerta de exemplo. Devolve (assunto, corpo_html)."""
    host = "FW-CLIENTE-A"
    problemas = [{"nome": "Link OPERADORA-1 indisponível", "gravidade": 3, "clock": int(time.time())}]
    assunto, antes, depois = (aplicar(m[k], host, problemas) for k in ("assunto", "texto_antes", "texto_depois"))
    return montar(host, problemas, assunto, antes, depois)[:2]


# ---------- envio ----------
def enviar(para, cc, assunto, corpo_html, corpo_texto):
    """Envia o e-mail. Em modo dry-run só simula.
    Devolve ('enviado' ou 'simulado', lista de endereços que o servidor recusou). Se TODOS forem
    recusados, o smtplib levanta uma exceção e o e-mail continua como rascunho."""
    if modo() != "smtp":
        print(f"[e-mail simulado] para={para} cc={cc} assunto={assunto}", flush=True)
        return "simulado", []
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = ", ".join(para)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = assunto
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(corpo_texto)
    msg.add_alternative(corpo_html, subtype="html")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
        if os.getenv("SMTP_STARTTLS", "1") != "0":
            s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASS", ""))
        recusados = s.send_message(msg, to_addrs=list(para) + list(cc))  # dict dos recusados (se algum aceitou)
    return "enviado", sorted(recusados)
