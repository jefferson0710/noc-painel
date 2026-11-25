# Painel NOC

Backend em Python (FastAPI) que lê os alertas do Zabbix, agrupa por host em incidentes, controla o prazo de cada regra e prepara o e-mail padrão. O painel web é servido pelo próprio backend.

## Rodar no seu computador

1. Instale o Docker (Docker Engine, ou Docker Desktop se a TI liberar a licença).
2. Nesta pasta, copie o arquivo de configuração:
   - Windows (PowerShell): `copy .env.example .env`
   - Linux/Mac: `cp .env.example .env`
3. Abra o `.env` e troque pelo menos `PAINEL_SENHA`.
4. Suba o serviço:
   ```
   docker compose up -d --build
   ```
5. Abra http://localhost:8080 (usuário e senha do `.env`).

Comandos úteis: `docker compose logs -f` (acompanhar), `docker compose down` (parar), `docker compose up -d --build` (aplicar mudanças no código).

## Rodar sem Docker (Windows, só com Python)

Dentro de `noc-painel\\backend`, uma vez só:
```
python -m venv .venv
.\\.venv\\Scripts\\python -m pip install -r requirements.txt
```
Para subir (pede o token na hora, escondido, e não grava em arquivo):
```
.\\.venv\\Scripts\\python iniciar.py
```
Para guardar o token (no Gerenciador de Credenciais do Windows, não em arquivo) e não colar toda vez, rode uma vez `.\\.venv\\Scripts\\python -m pip install keyring`; na execução seguinte o programa pergunta se pode guardar. Use `--trocar-token` para substituir e `--esquecer-token` para apagar.
Para uma demonstração sem Zabbix e sem token: `.\\.venv\\Scripts\\python iniciar.py --simulado`.
Edite o endereço do Zabbix e as gravidades no começo do `iniciar.py`. Esse modo abre sem login e só no próprio computador.

## Primeiro teste (sem Zabbix)

Com `ZABBIX_MODE=mock` e `EMAIL_MODE=dry-run` (valores iniciais) o painel mostra 3 incidentes simulados.
Em poucos minutos o do `SW-CLIENTE-B` vence o prazo e o e-mail fica pronto para revisar. Nada é enviado.
Use "Criar alerta de teste" para simular outros hosts e "Recuperar" para simular a normalização.

## Ligar ao Zabbix de verdade (só leitura)

No `.env`: `ZABBIX_MODE=real`, `ZABBIX_URL` (termina em `/api_jsonrpc.php`) e `ZABBIX_TOKEN`.
Use um usuário só de leitura nos grupos de hosts do NOC. Depois: `docker compose up -d --build`.
O topo do painel mostra se a leitura está funcionando ou o erro retornado.

## Enviar e-mail de verdade

Só depois de validar tudo em modo teste: `EMAIL_MODE=smtp` e preencher `SMTP_*` com a conta do NOC.

## Testes do motor de regras

```
cd backend
python -m unittest discover -s tests -t . -v
```

## Nome e logo do painel

`PAINEL_NOME` (no `.env` ou nas variáveis do `iniciar.py`) troca o nome mostrado no cabeçalho. Para mostrar o logo da empresa, coloque o arquivo `logo.png` dentro da pasta `frontend`.
