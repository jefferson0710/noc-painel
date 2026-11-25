"""Regras de negócio: cada problema do Zabbix é um alerta separado, com prazo e e-mail próprios.

Estados do alerta (tabela incidentes):
  aguardando   - ainda dentro do prazo da regra
  email_pronto - prazo vencido, rascunho do e-mail esperando revisão
  enviado      - e-mail enviado (ou simulado)
  dispensado   - decidiu-se não enviar
  resolvido    - o problema recuperou no Zabbix
"""
import json
import re
import time
from datetime import datetime

from . import db, mailer

REGRA_PADRAO = {"id": None, "nome": "Sem regra", "host_padrao": "*", "problema_contem": "", "espera_min": 30,
                "para": "[]", "cc": "[]", "envio_automatico": 0, "observacao": "", "prioridade": 999,
                "wiki_url": ""}


def _especificidade(r):
    """Quanto mais específica a regra, mais cedo ela é conferida: primeiro as que também citam um problema,
    depois as de nome de host mais detalhado (mais letras fora do *) e, no mesmo host, o problema com mais texto.
    'Todos os hosts' fica por último. A prioridade antiga só desempata."""
    return (0 if r["problema_contem"] else 1, -len(r["host_padrao"].replace("*", "")),
            -len(r["problema_contem"]), r["prioridade"], r["id"])


def regras_ordenadas():
    return sorted(db.q("SELECT * FROM regras"), key=_especificidade)


def _combina(host, padrao):
    """Host cadastrado (sem *) tem de ser igual, sem diferenciar maiúscula; com * o curinga vale por qualquer texto."""
    if "*" not in padrao:
        return host == padrao
    return re.fullmatch(".*".join(re.escape(p) for p in padrao.split("*")), host) is not None


def achar_regra(host, nome):
    """Regra mais específica que combina com o host e, se houver, com o texto do problema."""
    h, n = host.lower(), nome.lower()
    for r in regras_ordenadas():
        if not _combina(h, r["host_padrao"].lower()):
            continue
        if r["problema_contem"] and r["problema_contem"].lower() not in n:
            continue
        return r
    return REGRA_PADRAO


def testar_regra(host, problema):
    """Mostra qual regra valeria para um host e um problema (tela de regras)."""
    r = achar_regra(host, problema)
    return {"regra": None if r["id"] is None else {k: r[k] for k in ("id", "nome", "host_padrao", "problema_contem",
                                                                     "espera_min", "envio_automatico")},
            "espera_min": r["espera_min"], "para": _enderecos(r["para"]), "cc": _enderecos(r["cc"])}


def problemas_vistos(host):
    """Nomes de problema que este host já teve (ajuda a cadastrar o nome exatamente como o Zabbix mostra)."""
    return [r["nome"] for r in db.q(
        "SELECT DISTINCT p.nome FROM problemas p JOIN incidentes i ON i.id=p.incidente_id "
        "WHERE lower(i.host)=lower(?) ORDER BY p.nome COLLATE NOCASE", (host,))]


def resumo(agora=None):
    """Números do dia para a faixa de resumo do painel (o dia começa à meia-noite no fuso do NOC)."""
    agora = agora or int(time.time())
    inicio = int(datetime.fromtimestamp(agora, mailer._fuso()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    emails = db.one("SELECT COUNT(*) AS n FROM emails WHERE estado IN ('enviado','simulado') AND enviado_em>=?", (inicio,))["n"]
    resolvidos = db.one("SELECT COUNT(*) AS n FROM incidentes WHERE estado='resolvido' AND resolvido_em>=?", (inicio,))["n"]
    return {"emails_hoje": emails, "resolvidos_hoje": resolvidos}


def hosts_conhecidos():
    return [r["host"] for r in db.q("SELECT DISTINCT host FROM incidentes ORDER BY host COLLATE NOCASE")]


def _enderecos(ids_json):
    ids = json.loads(ids_json or "[]")
    if not ids:
        return []
    marcas = ",".join("?" * len(ids))
    return [c["email"] for c in db.q(f"SELECT email FROM contatos WHERE id IN ({marcas}) ORDER BY nome", ids)]


def sincronizar(problemas, agora=None):
    """Recebe a lista atual de problemas abertos e atualiza alertas, prazos e rascunhos."""
    agora = agora or int(time.time())
    with db.lock:
        vistos = set()
        for p in problemas:
            eid = str(p["eventid"])
            vistos.add(eid)
            atual = db.one("SELECT * FROM problemas WHERE eventid=?", (eid,))
            if atual:
                db.x("UPDATE problemas SET ativo=1, nome=?, gravidade=? WHERE eventid=?",
                     (p["nome"], int(p["gravidade"]), eid))
                db.x("UPDATE incidentes SET estado='aguardando', resolvido_em=NULL "
                     "WHERE id=? AND estado='resolvido'", (atual["incidente_id"],))
                continue
            # Problema novo = alerta novo, mesmo que o host já tenha outros alertas
            inc_id = db.x("INSERT INTO incidentes (host, criado_em) VALUES (?, ?)", (p["host"], agora))
            db.x("INSERT INTO problemas (eventid, incidente_id, nome, gravidade, clock) VALUES (?,?,?,?,?)",
                 (eid, inc_id, p["nome"], int(p["gravidade"]), int(p["clock"])))
        for r in db.q("SELECT eventid FROM problemas WHERE ativo=1"):
            if r["eventid"] not in vistos:
                db.x("UPDATE problemas SET ativo=0 WHERE eventid=?", (r["eventid"],))
        for inc in db.q("SELECT * FROM incidentes WHERE estado!='resolvido'"):
            _avaliar(inc, agora)


def _avaliar(inc, agora):
    cancelar = "UPDATE emails SET estado='cancelado' WHERE incidente_id=? AND estado='rascunho'"
    ativos = db.q("SELECT * FROM problemas WHERE incidente_id=? AND ativo=1 ORDER BY clock, eventid",
                  (inc["id"],))
    if not ativos:
        db.x("UPDATE incidentes SET estado='resolvido', resolvido_em=?, prazo_em=NULL WHERE id=?",
             (agora, inc["id"]))
        db.x(cancelar, (inc["id"],))
        return
    pendentes = [p for p in ativos if p["notificado"] == 0]
    if not pendentes:
        mandou = db.one("SELECT 1 AS a FROM emails WHERE incidente_id=? AND estado IN ('enviado','simulado')",
                        (inc["id"],))
        db.x("UPDATE incidentes SET estado=?, prazo_em=NULL WHERE id=?",
             ("enviado" if mandou else "dispensado", inc["id"]))
        db.x(cancelar, (inc["id"],))
        return
    regra = achar_regra(inc["host"], pendentes[0]["nome"])
    # Marca se está usando a regra padrão (host_padrao="*" e sem problema_contem)
    usando_padrao = 1 if (regra["id"] is None or (regra["host_padrao"] == "*" and not regra["problema_contem"])) else 0
    prazo = pendentes[0]["clock"] + regra["espera_min"] * 60
    estado = "aguardando" if agora < prazo else "email_pronto"
    db.x("UPDATE incidentes SET estado=?, prazo_em=?, regra_id=?, usando_padrao=? WHERE id=?",
         (estado, prazo, regra["id"], usando_padrao, inc["id"]))
    if estado == "email_pronto":
        _preparar_email(inc, pendentes, regra, agora)


def _preparar_email(inc, pendentes, regra, agora, automatico=True):
    """Cria (ou atualiza) o rascunho do e-mail. Devolve o id do e-mail."""
    para, cc = _enderecos(regra["para"]), _enderecos(regra["cc"])
    assunto, antes, depois = mailer.rascunho_padrao(inc["host"], pendentes)
    assunto, corpo_html, corpo_texto = mailer.montar(inc["host"], pendentes, assunto, antes, depois)
    eventids = json.dumps([p["eventid"] for p in pendentes])
    novo = (json.dumps(para), json.dumps(cc), assunto, corpo_html, corpo_texto, eventids, antes, depois)
    atual = db.one("SELECT * FROM emails WHERE incidente_id=? AND estado='rascunho'", (inc["id"],))
    if atual:
        if atual["editado"]:
            return atual["id"]  # a pessoa mexeu à mão: o painel não sobrescreve
        antigo = tuple(atual[k] for k in ("para", "cc", "assunto", "corpo_html", "corpo_texto", "eventids",
                                          "texto_antes", "texto_depois"))
        if antigo != novo:
            db.x("UPDATE emails SET para=?, cc=?, assunto=?, corpo_html=?, corpo_texto=?, eventids=?, "
                 "texto_antes=?, texto_depois=? WHERE id=?", novo + (atual["id"],))
        return atual["id"]
    email_id = db.x(
        "INSERT INTO emails (incidente_id, para, cc, assunto, corpo_html, corpo_texto, eventids, texto_antes, "
        "texto_depois, estado, criado_em) VALUES (?,?,?,?,?,?,?,?,?,'rascunho',?)",
        (inc["id"],) + novo + (agora,))
    if automatico and regra["envio_automatico"] and para:
        try:
            enviar_email(email_id)
        except Exception:
            pass  # o erro fica gravado no e-mail; continua como rascunho para tentar à mão
    return email_id


def preparar_agora(incidente_id):
    """Cria o rascunho antes do prazo vencer (botão 'Enviar agora'). Nunca envia sozinho."""
    inc = db.one("SELECT * FROM incidentes WHERE id=?", (incidente_id,))
    if not inc or inc["estado"] == "resolvido":
        raise LookupError("Alerta não encontrado ou já resolvido")
    pendentes = db.q("SELECT * FROM problemas WHERE incidente_id=? AND ativo=1 AND notificado=0 "
                     "ORDER BY clock, eventid", (incidente_id,))
    if not pendentes:
        raise ValueError("Este alerta já foi tratado")
    regra = achar_regra(inc["host"], pendentes[0]["nome"])
    return _preparar_email(inc, pendentes, regra, int(time.time()), automatico=False)


def editar_rascunho(email_id, assunto, antes, depois, para, cc):
    """Grava os ajustes feitos à mão neste e-mail e refaz a prévia."""
    e = db.one("SELECT * FROM emails WHERE id=?", (email_id,))
    if not e:
        raise LookupError("E-mail não encontrado")
    if e["estado"] != "rascunho":
        raise ValueError("Este e-mail já foi enviado ou descartado")
    if not assunto.strip():
        raise ValueError("O assunto não pode ficar vazio")
    inc = db.one("SELECT host FROM incidentes WHERE id=?", (e["incidente_id"],))
    eids = json.loads(e["eventids"])
    marcas = ",".join("?" * len(eids))
    problemas = db.q(f"SELECT eventid, nome, gravidade, clock FROM problemas WHERE eventid IN ({marcas})", eids)
    assunto, corpo_html, corpo_texto = mailer.montar(inc["host"], problemas, assunto, antes, depois)
    db.x("UPDATE emails SET para=?, cc=?, assunto=?, corpo_html=?, corpo_texto=?, texto_antes=?, texto_depois=?, "
         "editado=1 WHERE id=?", (json.dumps(para), json.dumps(cc), assunto, corpo_html, corpo_texto,
                                  antes, depois, email_id))


def _reavaliar(incidente_id):
    inc = db.one("SELECT * FROM incidentes WHERE id=?", (incidente_id,))
    if inc and inc["estado"] != "resolvido":
        _avaliar(inc, int(time.time()))


def enviar_email(email_id):
    """Envia (ou simula) o rascunho. Levanta ValueError/LookupError/RuntimeError em caso de problema."""
    e = db.one("SELECT * FROM emails WHERE id=?", (email_id,))
    if not e:
        raise LookupError("E-mail não encontrado")
    if e["estado"] != "rascunho":
        raise ValueError("Este e-mail não está pendente de envio")
    para, cc = json.loads(e["para"]), json.loads(e["cc"])
    if not para:
        raise ValueError("Sem destinatários: preencha o campo Para (ou defina quem recebe na regra deste host)")
    try:
        estado, recusados = mailer.enviar(para, cc, e["assunto"], e["corpo_html"], e["corpo_texto"])
    except Exception as ex:
        db.x("UPDATE emails SET erro=? WHERE id=?", (f"{type(ex).__name__}: {ex}", email_id))
        raise RuntimeError(f"Falha no envio: {ex}")
    aviso = ("O servidor de e-mail recusou estes endereços: " + ", ".join(recusados)) if recusados else None
    db.x("UPDATE emails SET estado=?, erro=?, enviado_em=? WHERE id=?",
         (estado, aviso, int(time.time()), email_id))
    for eid in json.loads(e["eventids"]):
        db.x("UPDATE problemas SET notificado=1 WHERE eventid=?", (eid,))
    _reavaliar(e["incidente_id"])
    return estado


def descartar_email(email_id):
    """Decide não enviar: o rascunho é descartado e o alerta não gera outro e-mail."""
    e = db.one("SELECT * FROM emails WHERE id=?", (email_id,))
    if not e:
        raise LookupError("E-mail não encontrado")
    if e["estado"] != "rascunho":
        raise ValueError("Este e-mail não está pendente de envio")
    db.x("UPDATE emails SET estado='descartado' WHERE id=?", (email_id,))
    for eid in json.loads(e["eventids"]):
        db.x("UPDATE problemas SET notificado=2 WHERE eventid=?", (eid,))
    _reavaliar(e["incidente_id"])


def incidente_completo(inc, agora, detalhe=False):
    """Monta o alerta no formato que o painel consome."""
    problemas = db.q("SELECT eventid, nome, gravidade, clock, ativo, notificado FROM problemas "
                     "WHERE incidente_id=? ORDER BY ativo DESC, clock DESC, eventid DESC", (inc["id"],))
    regra = None
    if inc["regra_id"]:
        regra = db.one("SELECT id, nome, espera_min, observacao, envio_automatico, wiki_url FROM regras "
                       "WHERE id=?", (inc["regra_id"],))
    campos = "id, estado, para, cc, assunto, erro, criado_em, enviado_em"
    if detalhe:
        campos += ", corpo_html, texto_antes, texto_depois, editado"
    email = db.one(f"SELECT {campos} FROM emails WHERE incidente_id=? ORDER BY id DESC LIMIT 1", (inc["id"],))
    if email:
        email["para"], email["cc"] = json.loads(email["para"]), json.loads(email["cc"])
        # SEMPRE reaplica a mensagem padrão atual ao abrir para revisar (detalhe=True).
        # Assim o usuário vê o modelo mais recente e pode editar ali na hora se quiser.
        if detalhe and email["estado"] == "rascunho":
            m = mailer.modelo()
            assunto, antes, depois = (mailer.aplicar(m[k], inc["host"], problemas)
                                      for k in ("assunto", "texto_antes", "texto_depois"))
            email["assunto"], email["corpo_html"], email["corpo_texto"] = mailer.montar(
                inc["host"], problemas, assunto, antes, depois)
            email["texto_antes"], email["texto_depois"] = antes, depois
    prazo = inc["prazo_em"]
    abertos = [p["clock"] for p in problemas if p["ativo"]]
    ultimo = max(abertos or [p["clock"] for p in problemas] or [inc["criado_em"]])
    return {"id": inc["id"], "host": inc["host"], "estado": inc["estado"], "criado_em": inc["criado_em"],
            "resolvido_em": inc["resolvido_em"], "prazo_em": prazo, "ultimo_alerta_em": ultimo,
            "prazo_restante_s": (prazo - agora) if prazo else None,
            "regra": regra, "problemas": problemas, "email": email}


def listar_incidentes(todos=False, agora=None):
    """Alertas abertos, do mais recente para o mais antigo.
    Com todos=True inclui os resolvidos nas últimas 24 horas, sempre no fim da lista."""
    agora = agora or int(time.time())
    if todos:
        linhas = db.q("SELECT * FROM incidentes WHERE estado!='resolvido' OR resolvido_em>?", (agora - 86400,))
    else:
        linhas = db.q("SELECT * FROM incidentes WHERE estado!='resolvido'")
    itens = [incidente_completo(i, agora) for i in linhas]
    itens.sort(key=lambda d: (d["estado"] == "resolvido", -d["ultimo_alerta_em"], -d["id"]))
    return itens


def alertas_sem_regra(agora=None):
    """Alertas ativos (não resolvidos) que estão usando a regra padrão (host/problema não cadastrado)."""
    agora = agora or int(time.time())
    linhas = db.q("SELECT * FROM incidentes WHERE estado!='resolvido' AND usando_padrao=1")
    itens = [incidente_completo(i, agora) for i in linhas]
    itens.sort(key=lambda d: (-d["ultimo_alerta_em"], -d["id"]))
    return itens
