"""Banco SQLite usando só a biblioteca padrão do Python."""
import json
import os
import sqlite3
import threading

DB_PATH = os.getenv("DB_PATH", "dados/noc.db")
VERSAO = 3
lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS contatos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nome TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS regras (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nome TEXT NOT NULL,
  host_padrao TEXT NOT NULL DEFAULT '*',
  problema_contem TEXT NOT NULL DEFAULT '',
  espera_min INTEGER NOT NULL DEFAULT 30,
  para TEXT NOT NULL DEFAULT '[]',
  cc TEXT NOT NULL DEFAULT '[]',
  envio_automatico INTEGER NOT NULL DEFAULT 0,
  observacao TEXT NOT NULL DEFAULT '',
  prioridade INTEGER NOT NULL DEFAULT 100,
  wiki_url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS incidentes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  host TEXT NOT NULL,
  estado TEXT NOT NULL DEFAULT 'aguardando',
  criado_em INTEGER NOT NULL,
  prazo_em INTEGER,
  regra_id INTEGER,
  resolvido_em INTEGER,
  usando_padrao INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS problemas (
  eventid TEXT PRIMARY KEY,
  incidente_id INTEGER NOT NULL,
  nome TEXT NOT NULL,
  gravidade INTEGER NOT NULL DEFAULT 0,
  clock INTEGER NOT NULL,
  ativo INTEGER NOT NULL DEFAULT 1,
  notificado INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS emails (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incidente_id INTEGER NOT NULL,
  para TEXT NOT NULL,
  cc TEXT NOT NULL,
  assunto TEXT NOT NULL,
  corpo_html TEXT NOT NULL,
  corpo_texto TEXT NOT NULL,
  eventids TEXT NOT NULL,
  estado TEXT NOT NULL,
  erro TEXT,
  criado_em INTEGER NOT NULL,
  enviado_em INTEGER,
  texto_antes TEXT NOT NULL DEFAULT '',
  texto_depois TEXT NOT NULL DEFAULT '',
  editado INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS config (
  chave TEXT PRIMARY KEY,
  valor TEXT NOT NULL
);
"""
# Cada alerta (incidente) tem exatamente um problema do Zabbix.
# problemas.notificado: 0 = pendente, 1 = e-mail enviado, 2 = dispensado (decidiu não enviar)
# emails.estado: rascunho | enviado | simulado | cancelado | descartado


def init():
    global _conn
    pasta = os.path.dirname(DB_PATH)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with lock:
        existia = _conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incidentes'").fetchone() is not None
        _conn.executescript(SCHEMA)
        _migrar(existia)
        _conn.commit()
        _semear()


def _colunas(tabela):
    return {r["name"] for r in _conn.execute(f"PRAGMA table_info({tabela})")}


def _migrar(existia):
    versao = _conn.execute("PRAGMA user_version").fetchone()[0]
    if versao >= VERSAO:
        return
    novas = {
        "regras": [("wiki_url", "TEXT NOT NULL DEFAULT ''")],
        "emails": [("texto_antes", "TEXT NOT NULL DEFAULT ''"),
                   ("texto_depois", "TEXT NOT NULL DEFAULT ''"),
                   ("editado", "INTEGER NOT NULL DEFAULT 0")],
        "incidentes": [("usando_padrao", "INTEGER NOT NULL DEFAULT 0")],
    }
    for tabela, colunas in novas.items():
        atuais = _colunas(tabela)
        for nome, ddl in colunas:
            if nome not in atuais:
                _conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {ddl}")
    if existia and versao < 2:
        # A versão anterior agrupava os alertas por host. Recomeça incidentes, problemas e e-mails
        # (o Zabbix repovoa tudo na próxima leitura). Contatos, regras e a mensagem padrão ficam.
        for tabela in ("emails", "problemas", "incidentes"):
            _conn.execute(f"DELETE FROM {tabela}")
    _conn.execute(f"PRAGMA user_version={int(VERSAO)}")


def q(sql, params=()):
    """Consulta que devolve uma lista de dicionários."""
    with lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def one(sql, params=()):
    linhas = q(sql, params)
    return linhas[0] if linhas else None


def x(sql, params=()):
    """Executa INSERT/UPDATE/DELETE e devolve o id da última linha inserida."""
    with lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def _semear():
    """Dados iniciais de exemplo, só na primeira execução."""
    if not one("SELECT 1 AS a FROM contatos LIMIT 1"):
        x("INSERT INTO contatos (nome, email) VALUES (?, ?)",
          ("Contato de exemplo", "exemplo@exemplo.com.br"))
    if not one("SELECT 1 AS a FROM regras LIMIT 1"):
        para = json.dumps([one("SELECT id FROM contatos ORDER BY id LIMIT 1")["id"]])
        sql = ("INSERT INTO regras (nome, host_padrao, espera_min, para, observacao, prioridade) "
               "VALUES (?, ?, ?, ?, ?, ?)")
        x(sql, ("Firewalls", "FW-*", 15, para,
                "Firewall fora costuma derrubar vários serviços do cliente: atenção total e ligar para o cliente.",
                10))
        x(sql, ("Padrão", "*", 30, para, "", 100))
