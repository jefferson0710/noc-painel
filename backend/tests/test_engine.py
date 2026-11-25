import json
import os
import sqlite3
import tempfile
import unittest

os.environ["DB_PATH"] = ":memory:"
os.environ["EMAIL_MODE"] = "dry-run"

from app import db, engine, mailer  # noqa: E402

T0 = 1_700_000_000


def prob(eid, host, nome, grav=3, clock=T0):
    return {"eventid": str(eid), "host": host, "nome": nome, "gravidade": grav, "clock": clock}


def incidentes():
    return db.q("SELECT * FROM incidentes ORDER BY id")


class Alertas(unittest.TestCase):
    def setUp(self):
        db.init()  # banco novo em memória a cada teste

    # ----- um alerta por problema -----
    def test_cada_problema_vira_um_alerta_separado_mesmo_no_mesmo_host(self):
        engine.sincronizar([prob(1, "FW-A", "Link 1 caiu", clock=T0),
                            prob(2, "FW-A", "VPN caiu", clock=T0 + 3600)], agora=T0 + 3700)
        incs = incidentes()
        self.assertEqual(len(incs), 2)
        self.assertEqual({i["host"] for i in incs}, {"FW-A"})
        self.assertEqual(sorted(i["prazo_em"] for i in incs), [T0 + 900, T0 + 3600 + 900])

    def test_prazo_de_um_alerta_nao_mexe_no_outro(self):
        engine.sincronizar([prob(1, "FW-A", "Link 1 caiu", clock=T0),
                            prob(2, "FW-A", "VPN caiu", clock=T0 + 3600)], agora=T0 + 905)
        estados = {i["id"]: i["estado"] for i in incidentes()}
        self.assertEqual(sorted(estados.values()), ["aguardando", "email_pronto"])
        emails = db.q("SELECT assunto FROM emails")
        self.assertEqual([e["assunto"] for e in emails], ["FW-A - Link 1 caiu"])

    def test_vencido_gera_rascunho_com_host_e_problema_no_assunto(self):
        engine.sincronizar([prob(1, "FW-A", "Link 1 caiu")], agora=T0 + 10)
        self.assertEqual(incidentes()[0]["estado"], "aguardando")
        self.assertIsNone(db.one("SELECT * FROM emails"))
        engine.sincronizar([prob(1, "FW-A", "Link 1 caiu")], agora=T0 + 15 * 60)
        self.assertEqual(incidentes()[0]["estado"], "email_pronto")
        e = db.one("SELECT * FROM emails")
        self.assertEqual(e["estado"], "rascunho")
        self.assertEqual(e["assunto"], "FW-A - Link 1 caiu")
        self.assertEqual(json.loads(e["para"]), ["exemplo@exemplo.com.br"])
        self.assertIn("PROBLEM", e["corpo_html"])
        self.assertIn("Noc Computécnica", e["corpo_texto"])
        self.assertRegex(e["texto_antes"], r"^(Bom dia|Boa tarde|Boa noite),")

    def test_resolver_antes_do_prazo_nao_gera_email(self):
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 60)
        engine.sincronizar([], agora=T0 + 120)
        self.assertEqual(incidentes()[0]["estado"], "resolvido")
        self.assertIsNone(db.one("SELECT * FROM emails"))

    def test_resolver_com_rascunho_cancela_o_rascunho(self):
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        self.assertEqual(db.one("SELECT estado FROM emails")["estado"], "rascunho")
        engine.sincronizar([], agora=T0 + 32 * 60)
        self.assertEqual(db.one("SELECT estado FROM emails")["estado"], "cancelado")
        self.assertEqual(incidentes()[0]["estado"], "resolvido")

    def test_enviar_marca_enviado_e_problema_novo_do_mesmo_host_e_outro_alerta(self):
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        eid = db.one("SELECT id FROM emails")["id"]
        self.assertEqual(engine.enviar_email(eid), "simulado")
        self.assertEqual(incidentes()[0]["estado"], "enviado")
        agora = T0 + 40 * 60
        engine.sincronizar([prob(1, "SW-B", "CRC"), prob(2, "SW-B", "Porta caiu", clock=agora)], agora=agora + 5)
        incs = incidentes()
        self.assertEqual([i["estado"] for i in incs], ["enviado", "aguardando"])
        self.assertEqual(incs[1]["prazo_em"], agora + 30 * 60)

    def test_nao_envia_duas_vezes_o_mesmo_email(self):
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        eid = db.one("SELECT id FROM emails")["id"]
        engine.enviar_email(eid)
        with self.assertRaises(ValueError):
            engine.enviar_email(eid)

    def test_envio_automatico(self):
        db.x("UPDATE regras SET envio_automatico=1 WHERE nome='Padrão'")
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        self.assertEqual(db.one("SELECT estado FROM emails")["estado"], "simulado")
        self.assertEqual(incidentes()[0]["estado"], "enviado")

    def test_descartar_nao_gera_novo_rascunho(self):
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        engine.descartar_email(db.one("SELECT id FROM emails")["id"])
        self.assertEqual(incidentes()[0]["estado"], "dispensado")
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 40 * 60)
        self.assertEqual(db.one("SELECT COUNT(*) AS n FROM emails")["n"], 1)
        self.assertEqual(incidentes()[0]["estado"], "dispensado")

    def test_sem_destinatario_nao_envia(self):
        db.x("UPDATE regras SET para='[]'")
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        with self.assertRaises(ValueError):
            engine.enviar_email(db.one("SELECT id FROM emails")["id"])

    # ----- regras -----
    def test_regra_por_trecho_do_problema_tem_prioridade(self):
        db.x("INSERT INTO regras (nome, host_padrao, problema_contem, espera_min, prioridade) "
             "VALUES ('Link','FW-*','link',30,5)")
        engine.sincronizar([prob(1, "FW-A", "Link 1 caiu"), prob(2, "FW-A", "VPN caiu")], agora=T0 + 10)
        prazos = {p["nome"]: i["prazo_em"] for i in incidentes()
                  for p in db.q("SELECT nome FROM problemas WHERE incidente_id=?", (i["id"],))}
        self.assertEqual(prazos["Link 1 caiu"], T0 + 30 * 60)   # regra "Link" (30 min)
        self.assertEqual(prazos["VPN caiu"], T0 + 15 * 60)      # regra FW-* (15 min)

    def test_vale_a_regra_mais_especifica_sem_precisar_de_ordem(self):
        db.x("INSERT INTO regras (nome, host_padrao, espera_min, prioridade) VALUES ('Cliente A', '*CLIENTE-A*', 7, 500)")
        db.x("INSERT INTO regras (nome, host_padrao, espera_min, prioridade) VALUES ('Exato', 'FW-CLIENTE-A', 3, 900)")
        quem = lambda host, prob="x": engine.achar_regra(host, prob)["nome"]
        self.assertEqual(quem("FW-CLIENTE-A"), "Exato")          # nome completo vence
        self.assertEqual(quem("SW-CLIENTE-A"), "Cliente A")      # mais letras que FW-*
        self.assertEqual(quem("FW-OUTRO"), "Firewalls")
        self.assertEqual(quem("QUALQUER"), "Padrão")             # todos por último
        self.assertEqual([r["nome"] for r in engine.regras_ordenadas()][-1], "Padrão")

    def test_regra_com_filtro_de_problema_vence_a_que_so_olha_o_host(self):
        db.x("INSERT INTO regras (nome, host_padrao, problema_contem, espera_min, prioridade) "
             "VALUES ('Só VPN', '*', 'vpn', 5, 999)")
        self.assertEqual(engine.achar_regra("FW-A", "VPN caiu")["nome"], "Só VPN")
        self.assertEqual(engine.achar_regra("FW-A", "Link caiu")["nome"], "Firewalls")

    def test_host_cadastrado_com_cada_problema_e_o_resto(self):
        for prob, espera in (("link", 10), ("vpn", 20), ("", 40)):
            db.x("INSERT INTO regras (nome, host_padrao, problema_contem, espera_min) VALUES ('x', 'Router-X', ?, ?)",
                 (prob, espera))
        esperas = lambda host, nome: engine.achar_regra(host, nome)["espera_min"]
        self.assertEqual(esperas("router-x", "Link OPERADORA caiu"), 10)     # host sem diferenciar maiúscula
        self.assertEqual(esperas("ROUTER-X", "VPN site-to-site caiu"), 20)
        self.assertEqual(esperas("Router-X", "Disco cheio"), 40)              # qualquer outro problema do host
        self.assertEqual(esperas("Router-Y", "Link caiu"), 30)                # host não cadastrado: regra Padrão
        self.assertEqual(esperas("Router-X-2", "Link caiu"), 30)              # sem curinga = nome igual, não "começa com"

    def test_no_mesmo_host_o_problema_com_mais_texto_vence(self):
        db.x("INSERT INTO regras (nome, host_padrao, problema_contem, espera_min) VALUES ('a','SW-1','link',10)")
        db.x("INSERT INTO regras (nome, host_padrao, problema_contem, espera_min) VALUES ('b','SW-1','link gi0/24',99)")
        self.assertEqual(engine.achar_regra("SW-1", "Link Gi0/24 caiu")["espera_min"], 99)
        self.assertEqual(engine.achar_regra("SW-1", "Link Gi0/1 caiu")["espera_min"], 10)

    def test_nome_de_host_com_colchete_ou_interrogacao_e_igual_ao_texto(self):
        db.x("INSERT INTO regras (nome, host_padrao, espera_min) VALUES ('c','SW[1]?',5)")
        self.assertEqual(engine.achar_regra("SW[1]?", "x")["espera_min"], 5)
        self.assertEqual(engine.achar_regra("SW1a", "x")["espera_min"], 30)   # [1] e ? não são curingas

    def test_problemas_vistos_do_host(self):
        engine.sincronizar([prob(1, "SW-B", "Porta caiu"), prob(2, "sw-b", "CRC alto"), prob(3, "SW-C", "outro")], agora=T0)
        self.assertEqual(engine.problemas_vistos("SW-B"), ["CRC alto", "Porta caiu"])

    def test_testar_regra_diz_qual_vale(self):
        r = engine.testar_regra("FW-CLIENTE-A", "Link caiu")
        self.assertEqual((r["regra"]["nome"], r["espera_min"]), ("Firewalls", 15))
        r = engine.testar_regra("SW-X", "qualquer")
        self.assertEqual((r["regra"]["nome"], r["espera_min"]), ("Padrão", 30))
        self.assertEqual(r["para"], ["exemplo@exemplo.com.br"])
        db.x("DELETE FROM regras")
        r = engine.testar_regra("SW-X", "qualquer")
        self.assertIsNone(r["regra"])
        self.assertEqual(r["espera_min"], 30)

    def test_hosts_conhecidos(self):
        engine.sincronizar([prob(1, "SW-B", "a"), prob(2, "fw-a", "b"), prob(3, "SW-B", "c")], agora=T0)
        self.assertEqual(engine.hosts_conhecidos(), ["fw-a", "SW-B"])

    def test_wiki_da_regra_vai_para_o_alerta(self):
        db.x("UPDATE regras SET wiki_url='https://wiki.exemplo/proc?q={host}' WHERE nome='Firewalls'")
        engine.sincronizar([prob(1, "FW-A", "Link 1 caiu")], agora=T0 + 10)
        d = engine.listar_incidentes(agora=T0 + 10)[0]
        self.assertEqual(d["regra"]["wiki_url"], "https://wiki.exemplo/proc?q={host}")

    # ----- lista do painel -----
    def test_incidente_completo_traz_tudo_para_o_front(self):
        engine.sincronizar([prob(1, "FW-A", "Link 1 caiu")], agora=T0 + 15 * 60)
        d = engine.incidente_completo(incidentes()[0], T0 + 15 * 60 + 5, detalhe=True)
        self.assertEqual(d["estado"], "email_pronto")
        self.assertEqual(d["regra"]["espera_min"], 15)
        self.assertEqual(d["email"]["para"], ["exemplo@exemplo.com.br"])
        for campo in ("corpo_html", "texto_antes", "texto_depois", "editado"):
            self.assertIn(campo, d["email"])

    def test_lista_mostra_alerta_mais_recente_primeiro(self):
        engine.sincronizar([prob(1, "SW-A", "antigo", clock=T0), prob(2, "SW-B", "novo", clock=T0 + 600),
                            prob(3, "SW-C", "meio", clock=T0 + 300)], agora=T0 + 700)
        hosts = [i["host"] for i in engine.listar_incidentes(agora=T0 + 700)]
        self.assertEqual(hosts, ["SW-B", "SW-C", "SW-A"])

    def test_alertas_do_mesmo_host_aparecem_separados_do_mais_novo_para_o_mais_antigo(self):
        engine.sincronizar([prob(1, "FW-A", "VPN caiu", clock=T0),
                            prob(2, "FW-A", "Link caiu", clock=T0 + 7 * 3600)], agora=T0 + 7 * 3600 + 5)
        lista = engine.listar_incidentes(agora=T0 + 7 * 3600 + 5)
        self.assertEqual([i["problemas"][0]["nome"] for i in lista], ["Link caiu", "VPN caiu"])
        self.assertTrue(all(len(i["problemas"]) == 1 for i in lista))

    def test_resolvidos_ficam_no_fim_da_lista(self):
        engine.sincronizar([prob(1, "SW-A", "a", clock=T0), prob(2, "SW-B", "b", clock=T0 + 100)], agora=T0 + 200)
        engine.sincronizar([prob(1, "SW-A", "a", clock=T0)], agora=T0 + 210)  # SW-B recuperou
        hosts = [i["host"] for i in engine.listar_incidentes(todos=True, agora=T0 + 220)]
        self.assertEqual(hosts, ["SW-A", "SW-B"])
        self.assertEqual([i["host"] for i in engine.listar_incidentes(agora=T0 + 220)], ["SW-A"])

    # ----- e-mail: enviar antes do prazo, editar, mensagem padrão -----
    def test_enviar_agora_cria_rascunho_antes_do_prazo_sem_enviar_sozinho(self):
        db.x("UPDATE regras SET envio_automatico=1")
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 60)
        self.assertEqual(incidentes()[0]["estado"], "aguardando")
        email_id = engine.preparar_agora(incidentes()[0]["id"])
        e = db.one("SELECT * FROM emails WHERE id=?", (email_id,))
        self.assertEqual(e["estado"], "rascunho")           # não foi enviado sozinho
        self.assertEqual(engine.preparar_agora(incidentes()[0]["id"]), email_id)  # não duplica
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 90)
        self.assertEqual(db.one("SELECT estado FROM emails WHERE id=?", (email_id,))["estado"], "rascunho")
        self.assertEqual(engine.enviar_email(email_id), "simulado")
        self.assertEqual(incidentes()[0]["estado"], "enviado")

    def test_editar_rascunho_a_mao_e_o_painel_nao_sobrescreve(self):
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        email_id = db.one("SELECT id FROM emails")["id"]
        engine.editar_rascunho(email_id, "Assunto novo", "Olá equipe,", "Obrigado.", ["a@x.com", "b@x.com"], ["c@x.com"])
        e = db.one("SELECT * FROM emails WHERE id=?", (email_id,))
        self.assertEqual((e["assunto"], e["editado"]), ("Assunto novo", 1))
        self.assertEqual(json.loads(e["para"]), ["a@x.com", "b@x.com"])
        self.assertIn("Olá equipe,", e["corpo_html"])
        self.assertIn("Obrigado.", e["corpo_texto"])
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 35 * 60)   # nova leitura do Zabbix
        e2 = db.one("SELECT * FROM emails WHERE id=?", (email_id,))
        self.assertEqual((e2["assunto"], e2["texto_antes"]), ("Assunto novo", "Olá equipe,"))
        with self.assertRaises(ValueError):
            engine.editar_rascunho(email_id, "  ", "a", "b", [], [])

    def test_mensagem_padrao_personalizada_vale_para_os_proximos_emails(self):
        mailer.salvar_modelo({"assunto": "[NOC] {host}: {problema} ({gravidade})",
                              "texto_antes": "Prezados, às {hora} começou isto:",
                              "texto_depois": "Att,\nEquipe"})
        engine.sincronizar([prob(1, "SW-B", "CRC", grav=4, clock=T0)], agora=T0 + 31 * 60)
        e = db.one("SELECT * FROM emails")
        self.assertEqual(e["assunto"], "[NOC] SW-B: CRC (High)")
        self.assertTrue(e["texto_antes"].startswith("Prezados, às "))
        self.assertNotIn("{", e["texto_antes"])
        mailer.restaurar_modelo()
        self.assertEqual(mailer.modelo(), mailer.MODELO_PADRAO)

    def test_rascunho_sem_edicao_acompanha_mudanca_da_mensagem_padrao(self):
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        mailer.salvar_modelo({"assunto": "Novo {host}", "texto_antes": "x", "texto_depois": "y"})
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 32 * 60)
        self.assertEqual(db.one("SELECT assunto FROM emails")["assunto"], "Novo SW-B")

    def test_enderecos_recusados_pelo_servidor_ficam_avisados(self):
        engine.sincronizar([prob(1, "SW-B", "CRC")], agora=T0 + 31 * 60)
        original = mailer.enviar
        mailer.enviar = lambda *a, **k: ("enviado", ["errado@x.com", "outro@x.com"])
        try:
            self.assertEqual(engine.enviar_email(db.one("SELECT id FROM emails")["id"]), "enviado")
        finally:
            mailer.enviar = original
        e = db.one("SELECT * FROM emails")
        self.assertEqual(e["estado"], "enviado")
        self.assertIn("errado@x.com, outro@x.com", e["erro"])
        self.assertEqual(incidentes()[0]["estado"], "enviado")

    def test_resumo_do_dia(self):
        engine.sincronizar([prob(1, "SW-B", "CRC"), prob(2, "SW-C", "x")], agora=T0 + 31 * 60)
        engine.enviar_email(db.one("SELECT id FROM emails ORDER BY id LIMIT 1")["id"])
        engine.sincronizar([prob(2, "SW-C", "x")], agora=int(__import__("time").time()))
        r = engine.resumo()
        self.assertEqual(r["emails_hoje"], 1)
        self.assertEqual(r["resolvidos_hoje"], 1)

    def test_previa_da_mensagem_padrao(self):
        assunto, corpo = mailer.previa(mailer.MODELO_PADRAO)
        self.assertEqual(assunto, "FW-CLIENTE-A - Link OPERADORA-1 indisponível")
        self.assertIn("Average", corpo)

    def test_html_do_email_escapa_texto_digitado(self):
        engine.sincronizar([prob(1, "SW-B", "<b>CRC</b>")], agora=T0 + 31 * 60)
        engine.editar_rascunho(db.one("SELECT id FROM emails")["id"], "a", "<script>x</script>", "ok", ["a@x.com"], [])
        corpo = db.one("SELECT corpo_html FROM emails")["corpo_html"]
        self.assertNotIn("<script>", corpo)
        self.assertNotIn("<b>CRC</b>", corpo)


class MigracaoDoBancoAntigo(unittest.TestCase):
    def test_banco_da_versao_anterior_e_atualizado_sem_perder_contatos_e_regras(self):
        with tempfile.TemporaryDirectory() as pasta:
            caminho = os.path.join(pasta, "velho.db")
            c = sqlite3.connect(caminho)
            c.executescript("""
              CREATE TABLE contatos (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, email TEXT NOT NULL UNIQUE);
              CREATE TABLE regras (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL, host_padrao TEXT NOT NULL DEFAULT '*',
                problema_contem TEXT NOT NULL DEFAULT '', espera_min INTEGER NOT NULL DEFAULT 30, para TEXT NOT NULL DEFAULT '[]',
                cc TEXT NOT NULL DEFAULT '[]', envio_automatico INTEGER NOT NULL DEFAULT 0, observacao TEXT NOT NULL DEFAULT '',
                prioridade INTEGER NOT NULL DEFAULT 100);
              CREATE TABLE incidentes (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'aguardando',
                criado_em INTEGER NOT NULL, prazo_em INTEGER, regra_id INTEGER, resolvido_em INTEGER);
              CREATE TABLE problemas (eventid TEXT PRIMARY KEY, incidente_id INTEGER NOT NULL, nome TEXT NOT NULL,
                gravidade INTEGER NOT NULL DEFAULT 0, clock INTEGER NOT NULL, ativo INTEGER NOT NULL DEFAULT 1, notificado INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE emails (id INTEGER PRIMARY KEY AUTOINCREMENT, incidente_id INTEGER NOT NULL, para TEXT NOT NULL, cc TEXT NOT NULL,
                assunto TEXT NOT NULL, corpo_html TEXT NOT NULL, corpo_texto TEXT NOT NULL, eventids TEXT NOT NULL, estado TEXT NOT NULL,
                erro TEXT, criado_em INTEGER NOT NULL, enviado_em INTEGER);
              INSERT INTO contatos (nome, email) VALUES ('Maria', 'maria@x.com');
              INSERT INTO regras (nome, host_padrao, espera_min, para) VALUES ('Minha regra', 'ABC-*', 20, '[1]');
              INSERT INTO incidentes (host, criado_em) VALUES ('ABC-1', 1);
              INSERT INTO problemas (eventid, incidente_id, nome, clock) VALUES ('7', 1, 'x', 1);
              INSERT INTO emails (incidente_id, para, cc, assunto, corpo_html, corpo_texto, eventids, estado, criado_em)
                VALUES (1, '[]', '[]', 'a', 'b', 'c', '[]', 'rascunho', 1);
            """)
            c.commit(); c.close()
            anterior = db.DB_PATH
            try:
                db.DB_PATH = caminho
                db.init()
                self.assertEqual(db.one("SELECT COUNT(*) AS n FROM incidentes")["n"], 0)
                self.assertEqual(db.one("SELECT COUNT(*) AS n FROM problemas")["n"], 0)
                self.assertEqual(db.one("SELECT COUNT(*) AS n FROM emails")["n"], 0)
                self.assertEqual(db.one("SELECT email FROM contatos")["email"], "maria@x.com")
                regra = db.one("SELECT * FROM regras")
                self.assertEqual((regra["nome"], regra["wiki_url"]), ("Minha regra", ""))
                db.x("INSERT INTO incidentes (host, criado_em) VALUES ('Z', 1)")
                db.x("INSERT INTO emails (incidente_id, para, cc, assunto, corpo_html, corpo_texto, eventids, estado, criado_em) "
                     "VALUES (1,'[]','[]','a','b','c','[]','rascunho',1)")
                self.assertEqual(db.one("SELECT editado FROM emails")["editado"], 0)
                # segunda abertura: não apaga de novo
                db.init()
                self.assertEqual(db.one("SELECT COUNT(*) AS n FROM incidentes")["n"], 1)
            finally:
                db.DB_PATH = anterior
                db._conn.close()


if __name__ == "__main__":
    unittest.main()
