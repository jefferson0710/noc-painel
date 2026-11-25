"""Sobe o painel NOC sem Docker.

Coloque este arquivo em noc-painel\\backend e rode, nessa pasta:
    .\\.venv\\Scripts\\python iniciar.py

Opções:
    --simulado         DEMONSTRAÇÃO: alertas de mentira, sem Zabbix e sem token, e nada é enviado.
                       Os alertas recomeçam a cada vez, mas contatos, regras e a mensagem padrão
                       ficam guardados (num banco próprio, separado do uso real).
    --trocar-token     pede o token de novo e substitui o que está guardado
    --esquecer-token   apaga o token guardado neste computador

O token é pedido escondido. Se o pacote "keyring" estiver instalado, ele pode ser guardado
no Gerenciador de Credenciais do Windows (protegido pela sua conta do Windows), e nunca
fica em arquivo de texto.
"""
import getpass
import os
import sys
from pathlib import Path

SERVICO, CONTA = "painel-noc-zabbix", "token"

try:
    import keyring
except ImportError:
    keyring = None

if "--esquecer-token" in sys.argv:
    if keyring is None:
        sys.exit("O pacote keyring não está instalado, então este programa não guardou nenhum token.")
    try:
        keyring.delete_password(SERVICO, CONTA)
        sys.exit("Token apagado deste computador.")
    except Exception:
        sys.exit("Não havia token guardado.")

raiz = Path(__file__).resolve().parent.parent
front = raiz / "frontend"
if not (front / "index.html").exists():
    sys.exit(f"Não achei a pasta frontend em {front}. Rode este arquivo dentro de noc-painel\\backend.")

os.environ.update({
    "FRONT_DIR": str(front),
    "DB_PATH": str(raiz / "dados" / "noc.db"),
    "ZABBIX_MODE": "real",
    "ZABBIX_URL": "https://monitoramento.cba.com.br/api_jsonrpc.php",
    "ZABBIX_AUTH": "bearer",
    "ZABBIX_SEVERIDADES": "2,3,4,5",
    "EMAIL_MODE": "dry-run",
    "POLL_SECONDS": "5",   # no teste, lê o Zabbix a cada 5 s (em produção, 10 a 15 s é suficiente)
})



def limpar_alertas_da_demo(caminho):
    """Os alertas simulados recomeçam do zero a cada vez; contatos, regras e mensagem padrão ficam."""
    if not caminho.exists():
        return
    import sqlite3
    con = sqlite3.connect(caminho)
    try:
        for tabela in ("emails", "problemas", "incidentes"):
            try:
                con.execute(f"DELETE FROM {tabela}")
            except sqlite3.OperationalError:
                pass  # banco ainda sem essas tabelas
        con.commit()
    finally:
        con.close()


simulado = "--simulado" in sys.argv
if simulado:
    os.environ["ZABBIX_MODE"] = "mock"
    os.environ["DB_PATH"] = str(raiz / "dados" / "demo.db")
    limpar_alertas_da_demo(raiz / "dados" / "demo.db")
    print("MODO DEMONSTRAÇÃO: alertas simulados, nenhum e-mail é enviado e o Zabbix não é acessado.")

token = None
if simulado:
    token = "simulado"
elif keyring and "--trocar-token" not in sys.argv:
    try:
        token = keyring.get_password(SERVICO, CONTA)
    except Exception:
        token = None
    if token:
        print("Usando o token guardado neste computador. Se o painel mostrar erro de token, rode com --trocar-token.")

if not token:
    token = getpass.getpass("Cole o token do Zabbix e aperte Enter (nada aparece na tela): ").strip()
    if not token:
        sys.exit("Token vazio. Rode de novo e cole o token quando o programa pedir.")
    if keyring is None:
        print("Dica: para guardar o token e não colar toda vez, rode uma vez:")
        print("    .\\.venv\\Scripts\\python -m pip install keyring")
    elif input("Guardar este token neste computador para não colar de novo? (s/n) ").strip().lower() in ("s", "sim"):
        try:
            keyring.set_password(SERVICO, CONTA, token)
            print("Token guardado no Gerenciador de Credenciais do Windows.")
        except Exception as e:
            print(f"Não consegui guardar o token ({e}). Vai ser preciso colar de novo da próxima vez.")

os.environ["ZABBIX_TOKEN"] = token

if not simulado:
    print(f"Token em uso: {len(token)} caracteres.")
print("Painel em http://localhost:8080  (Ctrl+C para parar)")
print("Estado da leitura: http://localhost:8080/api/saude")

import uvicorn  # noqa: E402  (só depois das variáveis, para o painel lê-las ao iniciar)

uvicorn.run("app.main:app", host="127.0.0.1", port=8080)
