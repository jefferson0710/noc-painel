"""API do painel NOC. Serve também os arquivos do front (pasta frontend/)."""
import asyncio
import base64
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, engine, mailer, zabbix

POLL = int(os.getenv("POLL_SECONDS", "15"))
FRONT_DIR = os.getenv("FRONT_DIR", "frontend")
USUARIO = os.getenv("PAINEL_USUARIO", "")
SENHA = os.getenv("PAINEL_SENHA", "")
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

client = zabbix.criar_cliente()
leitura = {"ultima": None, "erro": None}


def _ler_e_sincronizar():
    engine.sincronizar(client.problemas())


async def _ler_zabbix_em_loop():
    while True:
        try:
            await asyncio.to_thread(_ler_e_sincronizar)
            leitura["ultima"], leitura["erro"] = int(time.time()), None
        except Exception as e:  # o painel continua de pé e mostra o erro no topo
            leitura["erro"] = f"{type(e).__name__}: {e}"
        await asyncio.sleep(POLL)


@asynccontextmanager
async def lifespan(app):
    db.init()
    tarefa = asyncio.create_task(_ler_zabbix_em_loop())
    yield
    tarefa.cancel()


app = FastAPI(title="Painel NOC", lifespan=lifespan)


@app.middleware("http")
async def exigir_login(request: Request, call_next):
    """Login básico (usuário e senha do .env). Sem PAINEL_SENHA, o painel fica aberto."""
    if SENHA:
        ok = False
        cab = request.headers.get("authorization", "")
        if cab.lower().startswith("basic "):
            try:
                u, _, s = base64.b64decode(cab[6:]).decode("utf-8").partition(":")
                ok = (secrets.compare_digest(u.encode(), USUARIO.encode())
                      and secrets.compare_digest(s.encode(), SENHA.encode()))
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Painel NOC"'})
    return await call_next(request)


# ---------- modelos ----------
class ContatoIn(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegraIn(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    host_padrao: str = "*"
    problema_contem: str = ""
    espera_min: int = Field(30, ge=1, le=1440)
    para: list[int] = []
    cc: list[int] = []
    envio_automatico: bool = False
    observacao: str = ""
    prioridade: int = 100
    wiki_url: str = Field("", max_length=500)


class ModeloIn(BaseModel):
    assunto: str = Field(min_length=1, max_length=300)
    texto_antes: str = Field("", max_length=5000)
    texto_depois: str = Field("", max_length=5000)


class EmailIn(BaseModel):
    assunto: str = Field(max_length=300)
    texto_antes: str = Field("", max_length=5000)
    texto_depois: str = Field("", max_length=5000)
    para: list[str] = []
    cc: list[str] = []


class AlertaTeste(BaseModel):
    host: str = Field(min_length=1, max_length=120)
    nome: str = Field(min_length=1, max_length=200)
    gravidade: int = Field(3, ge=0, le=5)


# ---------- saúde ----------
@app.get("/api/saude")
def saude():
    return {"modo": zabbix.MODO, "email_modo": mailer.modo(), "ultima_leitura": leitura["ultima"],
            "erro": leitura["erro"], "intervalo_s": POLL, "login_ativo": bool(SENHA), "agora": int(time.time()),
            "nome": os.getenv("PAINEL_NOME", "Painel NOC"),
            "logo": os.path.isfile(os.path.join(FRONT_DIR, "logo.png"))}


@app.get("/api/resumo")
def resumo():
    return engine.resumo()


# ---------- alertas ----------
@app.get("/api/incidentes")
def listar_incidentes(todos: bool = False):
    return engine.listar_incidentes(todos)


@app.get("/api/incidentes/{inc_id}")
def detalhar_incidente(inc_id: int):
    inc = db.one("SELECT * FROM incidentes WHERE id=?", (inc_id,))
    if not inc:
        raise HTTPException(404, "Alerta não encontrado")
    return engine.incidente_completo(inc, int(time.time()), detalhe=True)


def _rascunho(inc_id):
    e = db.one("SELECT id FROM emails WHERE incidente_id=? AND estado='rascunho' ORDER BY id DESC", (inc_id,))
    if not e:
        raise HTTPException(404, "Não há e-mail pendente para este alerta")
    return e["id"]


def _enderecos_validos(lista, campo):
    saida = []
    for item in lista:
        for a in re.split(r"[,;\s]+", item.strip()):
            if not a:
                continue
            if not RE_EMAIL.match(a):
                raise HTTPException(400, f"E-mail inválido em {campo}: {a}")
            if a.lower() not in [s.lower() for s in saida]:
                saida.append(a)
    return saida


@app.post("/api/incidentes/{inc_id}/email/preparar")
def preparar_email(inc_id: int):
    try:
        return {"email_id": engine.preparar_agora(inc_id)}
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/incidentes/{inc_id}/email")
def editar_email(inc_id: int, e: EmailIn):
    para, cc = _enderecos_validos(e.para, "Para"), _enderecos_validos(e.cc, "Cc")
    try:
        engine.editar_rascunho(_rascunho(inc_id), e.assunto, e.texto_antes, e.texto_depois, para, cc)
    except (ValueError, LookupError) as ex:
        raise HTTPException(400, str(ex))
    return engine.incidente_completo(db.one("SELECT * FROM incidentes WHERE id=?", (inc_id,)),
                                     int(time.time()), detalhe=True)


@app.post("/api/incidentes/{inc_id}/email/enviar")
def enviar_email(inc_id: int):
    try:
        return {"estado": engine.enviar_email(_rascunho(inc_id))}
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/incidentes/{inc_id}/email/descartar")
def descartar_email(inc_id: int):
    try:
        engine.descartar_email(_rascunho(inc_id))
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/emails")
def historico(limite: int = 200):
    linhas = db.q(
        "SELECT e.id, e.incidente_id, i.host, e.para, e.cc, e.assunto, e.estado, e.erro, e.criado_em, "
        "e.enviado_em FROM emails e JOIN incidentes i ON i.id=e.incidente_id ORDER BY e.id DESC LIMIT ?",
        (max(1, min(limite, 1000)),))
    for e in linhas:
        e["para"], e["cc"] = json.loads(e["para"]), json.loads(e["cc"])
    return linhas


@app.get("/api/hosts")
def hosts():
    return engine.hosts_conhecidos()


@app.get("/api/problemas-vistos")
def problemas_vistos(host: str = ""):
    return engine.problemas_vistos(host.strip())


@app.get("/api/sem-regra")
def sem_regra():
    """Alertas ativos que caíram na regra padrão (host não cadastrado ou problema não cadastrado)."""
    return engine.alertas_sem_regra()


# ---------- mensagem padrão ----------
@app.get("/api/modelo")
def ver_modelo():
    return {"modelo": mailer.modelo(), "padrao": mailer.MODELO_PADRAO,
            "variaveis": [{"campo": c, "descricao": d} for c, d in mailer.VARIAVEIS]}


@app.put("/api/modelo")
def salvar_modelo(m: ModeloIn):
    mailer.salvar_modelo({"assunto": m.assunto, "texto_antes": m.texto_antes, "texto_depois": m.texto_depois})
    return mailer.modelo()


@app.delete("/api/modelo", status_code=204)
def restaurar_modelo():
    mailer.restaurar_modelo()
    return Response(status_code=204)


@app.post("/api/modelo/previa")
def previa_modelo(m: ModeloIn):
    assunto, corpo_html = mailer.previa({"assunto": m.assunto, "texto_antes": m.texto_antes,
                                         "texto_depois": m.texto_depois})
    return {"assunto": assunto, "corpo_html": corpo_html}


# ---------- contatos ----------
@app.get("/api/contatos")
def listar_contatos():
    return db.q("SELECT id, nome, email FROM contatos ORDER BY nome COLLATE NOCASE")


@app.post("/api/contatos", status_code=201)
def criar_contato(c: ContatoIn):
    try:
        novo = db.x("INSERT INTO contatos (nome, email) VALUES (?, ?)", (c.nome.strip(), c.email.strip()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Já existe um contato com este e-mail")
    return db.one("SELECT id, nome, email FROM contatos WHERE id=?", (novo,))


@app.put("/api/contatos/{cid}")
def editar_contato(cid: int, c: ContatoIn):
    if not db.one("SELECT 1 AS a FROM contatos WHERE id=?", (cid,)):
        raise HTTPException(404, "Contato não encontrado")
    try:
        db.x("UPDATE contatos SET nome=?, email=? WHERE id=?", (c.nome.strip(), c.email.strip(), cid))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Já existe um contato com este e-mail")
    return db.one("SELECT id, nome, email FROM contatos WHERE id=?", (cid,))


@app.delete("/api/contatos/{cid}", status_code=204)
def remover_contato(cid: int):
    db.x("DELETE FROM contatos WHERE id=?", (cid,))
    return Response(status_code=204)


# ---------- regras ----------
def _regra_out(r):
    r = dict(r)
    r["para"], r["cc"] = json.loads(r["para"]), json.loads(r["cc"])
    r["envio_automatico"] = bool(r["envio_automatico"])
    return r


def _valores_regra(r: RegraIn):
    wiki = r.wiki_url.strip()
    if wiki and not re.match(r"^https?://", wiki, re.I):
        raise HTTPException(400, "O link do procedimento precisa começar com http:// ou https://")
    return (r.nome.strip(), r.host_padrao.strip() or "*", r.problema_contem.strip(), r.espera_min,
            json.dumps(r.para), json.dumps(r.cc), int(r.envio_automatico), r.observacao.strip(),
            r.prioridade, wiki)


@app.get("/api/regras")
def listar_regras():
    return [_regra_out(r) for r in engine.regras_ordenadas()]


@app.get("/api/regras/testar")
def testar_regra(host: str = "", problema: str = ""):
    return engine.testar_regra(host.strip(), problema.strip())


def _sem_duplicata(r: RegraIn, ignorar_id=None):
    """Um host não pode ter o mesmo problema cadastrado duas vezes."""
    host, prob = r.host_padrao.strip().lower() or "*", r.problema_contem.strip().lower()
    for x in db.q("SELECT id, host_padrao, problema_contem FROM regras"):
        if x["id"] != ignorar_id and x["host_padrao"].lower() == host and x["problema_contem"].lower() == prob:
            raise HTTPException(409, "Este host já tem esse problema cadastrado" if prob
                                else "Este host já tem a regra para qualquer problema")


@app.post("/api/regras", status_code=201)
def criar_regra(r: RegraIn):
    _sem_duplicata(r)
    novo = db.x("INSERT INTO regras (nome, host_padrao, problema_contem, espera_min, para, cc, "
                "envio_automatico, observacao, prioridade, wiki_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                _valores_regra(r))
    return _regra_out(db.one("SELECT * FROM regras WHERE id=?", (novo,)))


@app.put("/api/regras/{rid}")
def editar_regra(rid: int, r: RegraIn):
    if not db.one("SELECT 1 AS a FROM regras WHERE id=?", (rid,)):
        raise HTTPException(404, "Regra não encontrada")
    _sem_duplicata(r, rid)
    db.x("UPDATE regras SET nome=?, host_padrao=?, problema_contem=?, espera_min=?, para=?, cc=?, "
         "envio_automatico=?, observacao=?, prioridade=?, wiki_url=? WHERE id=?", _valores_regra(r) + (rid,))
    return _regra_out(db.one("SELECT * FROM regras WHERE id=?", (rid,)))


@app.delete("/api/regras/{rid}", status_code=204)
def remover_regra(rid: int):
    db.x("DELETE FROM regras WHERE id=?", (rid,))
    return Response(status_code=204)


# ---------- alertas de teste (só no modo simulado) ----------
def _exigir_simulado():
    if zabbix.MODO != "mock":
        raise HTTPException(404, "Disponível só no modo simulado")


@app.post("/api/mock/alerta", status_code=201)
def criar_alerta_teste(a: AlertaTeste):
    _exigir_simulado()
    eventid = client.adicionar(a.host.strip(), a.nome.strip(), a.gravidade)
    _ler_e_sincronizar()
    return {"eventid": eventid}


@app.delete("/api/mock/alerta/{eventid}", status_code=204)
def resolver_alerta_teste(eventid: str):
    _exigir_simulado()
    if not client.resolver(eventid):
        raise HTTPException(404, "Alerta de teste não encontrado")
    _ler_e_sincronizar()
    return Response(status_code=204)


# O front precisa ser montado por último, para não esconder as rotas /api
if os.path.isdir(FRONT_DIR):
    app.mount("/", StaticFiles(directory=FRONT_DIR, html=True), name="front")
