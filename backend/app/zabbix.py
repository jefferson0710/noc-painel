"""Leitura de alertas. Dois modos: mock (simulado) e real (API do Zabbix)."""
import json
import os
import ssl
import time
import urllib.error
import urllib.request

MODO = os.getenv("ZABBIX_MODE", "mock").lower()


class Simulado:
    """Alertas de mentira para testar o painel sem acesso ao Zabbix."""

    def __init__(self):
        self._n = 0
        self.itens = {}
        agora = int(time.time())
        # Firewall com 4 links caindo juntos (prazo de 15 min)
        for i in range(1, 5):
            self.adicionar("FW-CLIENTE-A", f"Link OPERADORA-{i} indisponível", 3, agora - 4 * 60)
        # Switch com problema há 28 min (regra padrão de 30 min: vence em ~2 min)
        self.adicionar("SW-CLIENTE-B", "Interface Gi0/24 com erros de CRC", 2, agora - 28 * 60)
        self.adicionar("SRV-CLIENTE-C", "Uso de CPU acima de 90%", 4, agora - 3 * 60)

    def adicionar(self, host, nome, gravidade=3, clock=None):
        self._n += 1
        eventid = str(9000 + self._n)
        self.itens[eventid] = {"eventid": eventid, "host": host, "nome": nome,
                               "gravidade": int(gravidade), "clock": int(clock or time.time())}
        return eventid

    def resolver(self, eventid):
        return self.itens.pop(str(eventid), None) is not None

    def problemas(self):
        return list(self.itens.values())


class Real:
    """Lê os problemas em aberto pela API do Zabbix (problem.get + event.get)."""

    def __init__(self, url, token, modo_auth="bearer", verificar_tls=True, severidades=None):
        self.url, self.token, self.modo_auth = url, token, modo_auth
        self.severidades = severidades  # ex.: [3, 4, 5]; vazio = todas
        self.ctx = None if verificar_tls else ssl._create_unverified_context()

    def _chamar(self, metodo, params):
        corpo = {"jsonrpc": "2.0", "method": metodo, "params": params, "id": 1}
        cabecalhos = {"Content-Type": "application/json-rpc",
                      # Proxies como a Cloudflare costumam bloquear o nome padrão do Python (403)
                      "User-Agent": os.getenv("ZABBIX_USER_AGENT", "PainelNOC/1.0 (uso interno)")}
        if self.modo_auth == "bearer":
            cabecalhos["Authorization"] = f"Bearer {self.token}"
        else:
            corpo["auth"] = self.token
        req = urllib.request.Request(self.url, json.dumps(corpo).encode(), cabecalhos)
        try:
            with urllib.request.urlopen(req, timeout=20, context=self.ctx) as r:
                dados = json.load(r)
        except urllib.error.HTTPError as e:
            trecho = " ".join(e.read(400).decode("utf-8", "replace").split())[:300]
            raise RuntimeError(f"HTTP {e.code} {e.reason} - resposta do servidor: {trecho or '(vazia)'}")
        if "error" in dados:
            e = dados["error"]
            raise RuntimeError(f"Zabbix: {e.get('message')} - {e.get('data')}")
        return dados["result"]

    def problemas(self):
        params = {
            "output": ["eventid", "name", "severity", "clock"],
            "source": 0, "object": 0,
            "suppressed": False,           # ignora o que está em manutenção
            "sortfield": ["eventid"], "sortorder": "ASC",
        }
        if self.severidades:
            params["severities"] = self.severidades
        probs = self._chamar("problem.get", params)
        if not probs:
            return []
        eventos = self._chamar("event.get", {
            "eventids": [p["eventid"] for p in probs],
            "output": ["eventid"], "selectHosts": ["hostid", "name"],
        })
        host_do_evento = {e["eventid"]: (e["hosts"][0]["name"] if e.get("hosts") else "desconhecido")
                          for e in eventos}
        return [{"eventid": p["eventid"], "host": host_do_evento.get(p["eventid"], "desconhecido"),
                 "nome": p["name"], "gravidade": int(p["severity"]), "clock": int(p["clock"])}
                for p in probs]


def criar_cliente():
    if MODO == "real":
        # Padrão 3,4,5 (Average, High, Disaster), igual à tela de Problemas do NOC. Vazio = todas.
        sevs = [int(v) for v in os.getenv("ZABBIX_SEVERIDADES", "3,4,5").split(",") if v.strip()]
        return Real(os.environ["ZABBIX_URL"], os.environ["ZABBIX_TOKEN"],
                    os.getenv("ZABBIX_AUTH", "bearer").lower(),
                    os.getenv("ZABBIX_VERIFY_TLS", "1") != "0", sevs)
    return Simulado()
