# Finans — sua vida financeira descomplicada

Aplicação financeira em português, com backend **Python (Flask)** e **MySQL 8.4**, interface responsiva e dados separados por usuário.

## O que está incluído

- Cadastro com nome, e-mail e senha; login e encerramento de sessão.
- Opção de manter a sessão por 30 dias, sem salvar a senha no navegador.
- Saldo individual, receitas e despesas categorizadas.
- Extrato mensal com busca, filtros e exportação CSV.
- Gráficos mensais e opção de ocultar valores.
- Transferências internas entre usuários, com revisão antes da confirmação.
- Senhas com Argon2id, cookies HttpOnly, proteção CSRF e limite de tentativas de acesso.
- Valores armazenados em centavos inteiros; transações de banco de dados e bloqueio de contas evitam atualização parcial. Chaves de idempotência evitam cobrar duas vezes ao repetir a mesma solicitação.

Este projeto registra finanças e simula transferências internas. Não possui integração com instituições financeiras, Pix, TED, cartões ou dinheiro real. Receitas registradas manualmente são lançamentos de controle financeiro.

## Executar a versão completa

Requisito: Docker com Docker Compose disponível e em execução.

1. Abra um terminal na pasta deste projeto.
2. Copie `.env.example` para `.env`.
3. Troque `MYSQL_PASSWORD` e `MYSQL_ROOT_PASSWORD` por senhas fortes e diferentes.
4. Execute:

```powershell
docker compose up --build -d
```

5. Abra **http://localhost:8000**, clique em **Criar conta** e cadastre-se.

A conta começa com saldo zero. Registre uma entrada para experimentar os recursos. Para testar transferências, crie uma segunda conta com outro e-mail, saia dela e volte à primeira. Cada usuário só vê seus próprios lançamentos.

O primeiro início cria as tabelas automaticamente em um banco novo. O volume `aurora_mysql` preserva os dados ao reiniciar os contêineres. `docker compose down` para os serviços sem apagar esse volume; **não use `down -v`** para preservar os dados.

O arquivo `.env` é ignorado pelo Git e pelo Docker build. A aplicação usa um usuário de banco próprio; não usa a senha do administrador MySQL.

### Executar Python sem Docker

Com um servidor MySQL 8.4 já instalado, crie um banco `aurora` e um usuário com acesso a esse banco. Instale Python 3.12, crie um ambiente virtual e instale `requirements.txt`. Configure as variáveis de ambiente abaixo no processo do servidor:

- `MYSQL_HOST`: endereço do MySQL.
- `MYSQL_PORT`: porta, normalmente `3306`.
- `MYSQL_DATABASE`: `aurora`.
- `MYSQL_USER` e `MYSQL_PASSWORD`: usuário e senha da aplicação.
- `APP_ORIGIN`: endereço exato do site, incluindo protocolo e porta.
- `COOKIE_SECURE`: `false` somente para desenvolvimento local com HTTP; `true` para HTTPS.

O arquivo `.env` é carregado pelo Docker Compose. Na execução manual, exporte essas variáveis antes de iniciar Python.

```powershell
python -m flask --app backend.app:create_app init-db
waitress-serve --host=127.0.0.1 --port=8000 --call backend.app:create_app
```

O esquema inicial também está documentado em `database/schema.sql`, gerado a partir dos modelos. Use o comando `init-db` como caminho principal. Mudanças futuras em tabelas existentes precisam de migrações; `create_all` não substitui esse processo.

[Documentação oficial de instalação do MySQL](https://dev.mysql.com/doc/refman/8.4/en/windows-installation.html)

## Demonstração visual

A demonstração visual usa a mesma interface com dados fictícios e alterações temporárias em memória. Ela **não executa o backend Python/MySQL**, não cadastra usuários e não aceita credenciais reais. Atualizar a página reinicia a demonstração.

Para abrir localmente, abra `dist/index.html` no navegador. Para uma hospedagem estática, publique o conteúdo de `dist/`; não há etapa de build.

- Conta fictícia: Marina Costa.
- Destinatário disponível para testar transferência: `pedro@demo.finans`.
- O botão de acesso mostra uma prévia das telas; cadastro e login funcionam na versão completa executada com Python/MySQL.

O backend serve `/config.js` no modo `api` e a demonstração usa o modo `demo`. Não há fallback silencioso: uma falha do servidor na versão completa mostra um erro em vez de substituir os dados por dados fictícios.

## Publicar a versão completa em outro servidor

Use uma hospedagem que execute contêineres/Python e disponha de MySQL. Configure HTTPS na frente do serviço, `APP_ORIGIN` com o domínio correto e `COOKIE_SECURE=true`. Mantenha o banco em rede privada e configure backups do volume. O Compose fornecido publica a porta HTTP apenas em `127.0.0.1` para acesso local ou por um proxy no mesmo servidor.

Antes de usar informações reais, valide o ambiente implantado, as políticas de backup e recuperação e os controles adicionais exigidos pelo seu uso. Esta entrega é uma base funcional, sem certificação bancária, MFA ou recuperação de senha por e-mail.

## API

- `POST /api/auth/register`: nome, e-mail e senha de 12 a 128 caracteres.
- `POST /api/auth/login`: e-mail, senha e `remember` opcional.
- `GET /api/auth/me`: usuário autenticado e token CSRF.
- `POST /api/auth/logout`: revoga a sessão atual.
- `GET /api/dashboard?month=2026-10`: saldo e lançamentos do mês.
- `POST /api/entries`: `kind` (`income` ou `expense`), `amount` decimal em texto, `description` e `category`.
- `POST /api/transfers`: `email` do destinatário e `amount` decimal em texto.
- `GET /api/export?month=2026-10`: CSV do usuário autenticado.
- `GET /api/health`: disponibilidade do banco.

As operações autenticadas de escrita exigem `X-CSRF-Token`. Lançamentos e transferências também exigem `Idempotency-Key` único por operação. Envie `amount` como texto no formato `"125.50"`; nunca como número de ponto flutuante. O servidor identifica o usuário pela sessão, não por um ID enviado pelo navegador.

Execute periodicamente `python -m flask --app backend.app:create_app cleanup-sessions` para remover sessões e contadores de tentativas expirados.

## Validação

20 testes automatizados de backend passaram usando SQLite isolado para teste: cadastro, hash de senha, cookies, login/logout, persistência após reiniciar a aplicação, isolamento entre usuários, transferência com dois registros, saldo insuficiente, idempotência, valores inválidos, CSRF, origem, expiração, limite de tentativas e exportação. O esquema foi compilado para o dialeto MySQL.

Na atualização da identidade Finans, a interface também foi verificada em navegador: renderização, lançamento simulado, busca no extrato, confirmação de transferência, ocultação de valores e telas de acesso/cadastro da demonstração. O visual foi conferido em desktop e em larguras de 320 e 390 pixels. Os ativos locais, a configuração dos modos `demo`/`api`, o nome do CSV e a mensagem de destinatário inexistente foram conferidos pelo cliente de teste Flask.

A execução e a concorrência com um servidor MySQL real e o deploy Docker não foram verificados nesta atualização. A navegação opcional por WebMCP foi verificada na prévia local.

Para repetir os testes:

```powershell
python -m pip install -r requirements.txt pytest==8.4.2
python -m pytest tests -q
```

SQLite é aceito apenas com `TESTING=True`; a versão normal requer MySQL.

## Identidade visual

A identidade Finans foi aplicada à interface compartilhada pelos modos `demo` e `api`: logotipo, favicon, paleta, tipografia, textos, metadados e nomes dos arquivos CSV.

Referências do GitHub, consultadas antes da edição:

- [finans_project](https://github.com/luccafzc/finans_project/tree/66d84a5e31c47b69d3a1c82859c6d206949a8b37): logotipo original `images/logo.png` (copiado sem alteração), Arial, verde `#198754`, verde escuro `#13633d`, amarelo `#ffc107`, azul `#2146eb` e superfícies claras. As telas bancárias orientam a aplicação principal.
- [finans2](https://github.com/luccafzc/finans2/tree/d1ba97831bf623eb796b6045c8b0c81998cf9e34): confirma o mesmo logotipo e o amarelo `#ffc107`. O favicon adapta o símbolo de moeda do logotipo para tamanhos pequenos.

As cores da marca ficam em `dist/styles.css`; os ativos, em `dist/assets/`. O contrato de configuração da interface é `window.FINANS_CONFIG`, servido por `dist/config.js` na demonstração e pelo backend em `/config.js` no modo completo. Atualize frontend e backend juntos.

### Compatibilidade das instalações existentes

Os identificadores internos `aurora` (banco, usuário MySQL e usuário do contêiner), `aurora_mysql` (volume) e `aurora_session` (cookie) foram preservados para manter dados e sessões existentes. São identificadores técnicos legados, não a identidade pública do website. Esta mudança não exige migração de banco nem alteração de `.env`, rotas ou contratos da API.
